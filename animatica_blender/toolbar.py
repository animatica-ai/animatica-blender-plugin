# SPDX-License-Identifier: GPL-3.0-or-later
"""The floating toolbar: what you reach for on every beat of a sequence.

Animating a sequence is one loop, over and over: get to the moment (the key
poses either side), pose it (the Autoposer, or a pose described in words), key
it, say where the character goes (a waypoint, a pin) and what happens (the
prompt under the playhead), make the take, and judge it. The bar holds that
loop, in that order, under the character; the N panel keeps setting up
(sign-in, model, character, seeds) and the switches set once per take.

Reviewing a take is a mode, not more buttons: Generate's place becomes
Accept, then this block again / all of it again, the variations, and Reject
at the far edge, away from Accept.

The look follows Blender's: thin monochrome icons (``icons/src/*.svg``, drawn
for this bar, exported to ``icons/*.png`` by ``make icons``), text where a
decision is made (Generate, Accept, Reject, the prompt), filled orange for the
one thing to do now, Blender's blue for a switch that is on, red for
recording keys. The icons are drawn by the bar itself, so they can be sized to
the button and tinted by state; the buttons under them are Blender's 2D button
gizmos, which give the click, the hover and the tooltip -- so the bar hides
with the viewport's Show Gizmo switch, as the navigation buttons do.
"""

from __future__ import annotations

import math
import os
import struct
import time
import zlib

import blf
import bpy
import gpu
from bpy.props import StringProperty
from gpu_extras.batch import batch_for_shader
from mathutils import Matrix

# ---------------------------------------------------------------------------
# Look
# ---------------------------------------------------------------------------

from . import ui_style as st

BAR_COLOR = st.GROUND
BUTTON_COLOR = st.TILE
PRIMARY_COLOR = st.PRIMARY                     # the one thing to do now
PRIMARY_HOVER = st.PRIMARY_HOVER
ON_COLOR = st.ON                               # a switch that is on
REC_COLOR = st.REC                             # recording keys
WHITE = st.WHITE
FILL_COLOR = st.with_alpha(st.SOFT_ORANGE, 0.55)   # progress inside the working button
HOVER_OUTLINE = st.OUTLINE_HOVER

BUTTON = 30          # px at 1x, square
ICON = 18            # the glyph inside it
PAD = 5
GAP = 1              # inside a group: a hairline, the buttons a segmented strip
GROUP_GAP = 13       # between groups
BOTTOM = 14
RADIUS = 6
TEXT_SIZE = 11
PROMPT_CHARS = 16    # of the block's prompt shown on its button
EXPECTED_SECONDS = 30.0


#: the gizmos under each viewport's buttons, by area pointer, for hover
_live: dict = {}


def _ui() -> float:
    return bpy.context.preferences.system.ui_scale


# ---------------------------------------------------------------------------
# What is on it
# ---------------------------------------------------------------------------

class Item:
    __slots__ = ("id", "icon", "label", "op", "props", "on", "rec", "enabled", "primary",
                 "group", "width", "progress")

    def __init__(self, id, icon, op=None, props=None, *, label="", on=False, rec=False,
                 enabled=True, primary=False, group=0, width=None, progress=None):
        self.id, self.icon, self.label, self.op, self.props = id, icon, label, op, props or {}
        self.on, self.rec, self.enabled, self.primary = on, rec, enabled, primary
        self.group, self.width, self.progress = group, width, progress


def block_at(settings, frame: int) -> int:
    """The prompt block under ``frame``, or -1."""
    blocks = settings.prompt_blocks
    for i, b in enumerate(blocks):
        if b.frame_start <= frame < b.frame_end or (frame == b.frame_end and i == len(blocks) - 1):
            return i
    return -1


_cache: dict = {"at": 0.0, "blockers": []}


def blockers(context) -> list:
    """Why Generate cannot go, asked of what the N panel asks; twice a second
    at most, since the bar redraws often."""
    now = time.monotonic()
    if now - _cache["at"] < 0.5:
        return _cache["blockers"]
    out = []
    try:
        from . import constraints_ui, key_poses, mmcp_client, properties, request_builder

        s = context.scene.animatica
        out = request_builder.generation_blockers(
            scene=context.scene,
            prompt_blocks=s.prompt_blocks,
            constraint_objects=constraints_ui.walk_scene_constraints(context.scene),
            model_caps=mmcp_client.cached_model(s.model_id),
            pose_frames=key_poses.plan(context.scene)["frames"],
            armature_obj=properties._live_armature(s.target_armature),
        )
    except Exception:                           # noqa: BLE001
        out = []
    _cache.update(at=now, blockers=list(out))
    return _cache["blockers"]


def _prompt_label(settings, here: int) -> str:
    text = (settings.prompt_blocks[here].prompt or "").strip() if here >= 0 else ""
    if not text:
        return "Prompt…"
    return text if len(text) <= PROMPT_CHARS else text[:PROMPT_CHARS - 1].rstrip() + "…"


def items(context) -> list[Item]:
    """The buttons for what is happening now, in groups left to right."""
    from . import batch, mmcp_client, properties, variations
    from .autoposer import poser

    s = context.scene.animatica
    arm = properties._live_armature(s.target_armature)
    if arm is None:
        return []
    here = block_at(s, context.scene.frame_current)

    def can(tool):
        # greyed out when the connected model can't use it
        return mmcp_client.tool_available(s.model_id, tool)

    # Left to right, the way a moment is worked on: make the pose, key it
    # (step between the keyed ones, see them as ghosts), say where the
    # character goes, say what happens and make the take; then the model,
    # set once.
    out = [
        # 1. the pose
        Item("autopose", "autopose", "animatica.toolbar_autoposer", on=poser.has_controls(arm), group=1),
    ]
    if poser.has_controls(arm):
        # the handle picker, beside the Autoposer whose handles it shows
        out.append(Item("picker", "picker", "animatica.toolbar_toggle", {"name": "show_picker"},
                        on=bool(s.show_picker), group=1))
    out += [
        Item("describe", "describe", "animatica.toolbar_describe", enabled=can("describe"), group=1),
        # 2. keys: set one here, between the steps to the one before and after
        #    (your key poses, not every generated key), and record
        Item("key_prev", "key_prev", "animatica.step_key_pose", {"direction": 'PREV'}, group=2),
        Item("set_key", "set_key", "animatica.set_key_pose", group=2),
        Item("key_next", "key_next", "animatica.step_key_pose", {"direction": 'NEXT'}, group=2),
        Item("auto_key", "auto_key", "animatica.toolbar_toggle", {"name": "auto_key_pose"},
             on=bool(s.auto_key_pose), rec=bool(s.auto_key_pose), group=2),
        # ...and seeing them: the key poses as ghosts, their trail, their frames
        Item("ghost", "ghost", "animatica.toolbar_toggle", {"name": "key_pose_overlay"},
             on=bool(s.key_pose_overlay), group=2),
        # 3. where it goes
        Item("waypoint", "waypoint", "animatica.add_waypoint", enabled=can("waypoint"), group=3),
        Item("pin", "pin", "animatica.add_effector_target", enabled=can("pin"), group=3),
        # 4. what happens -- written next to the button that makes it
        Item("prompt", "prompt", "animatica.toolbar_prompt_here", label=_prompt_label(s, here),
             enabled=can("prompt"), group=5),         # in one group with the take it makes
    ]

    # the take: one primary action at a time
    if mmcp_client.cached_model(s.model_id) is None:
        out.append(Item("connect", "connect", "animatica.connect", label="Connect", primary=True,
                        group=5, width="take"))
    elif s.is_generating:
        elapsed = float(getattr(s, "generation_elapsed", 0))
        # one button, the width Generate had: the time it has taken, filling, and
        # a click on it cancels -- a Cancel beside it made the bar jump
        out.append(Item("working", "cancel", "animatica.cancel", label=f"{int(elapsed)} s  ·  Cancel",
                        group=5, width="take",
                        progress=1.0 - math.exp(-elapsed / EXPECTED_SECONDS)))
    elif s.is_previewing and not (batch.pending(s) or batch.failures(s)):
        out.append(Item("accept", "accept", "animatica.accept", label="Accept", primary=True, group=5))
        # one Redo: the block under the playhead, Shift for the whole take
        out.append(Item("redo", "redo", "animatica.toolbar_redo", label="Redo", group=6))
        take = variations.take_of(arm)
        if take is not None and take.count > 1:
            out.append(Item("var_prev", "var_prev", "animatica.show_variation", {"step": -1}, group=6))
            out.append(Item("var_label", "", None, label=f"{take.index + 1}/{take.count}",
                            enabled=False, group=6))
            out.append(Item("var_next", "var_next", "animatica.show_variation", {"step": 1}, group=6))
        out.append(Item("reject", "reject", "animatica.reject", label="Reject", group=7))
    else:
        why = blockers(context)
        out.append(Item("generate", "generate", "animatica.toolbar_generate", label="Generate",
                        primary=not why, enabled=not why, group=5, width="take"))

    # what makes it: the model, at the far end -- set up once, changed rarely
    if mmcp_client.cached_model(s.model_id) is not None:
        out.append(Item("model", "model", "animatica.toolbar_model", label=_short(s.model_id),
                        enabled=not s.is_generating, group=10))
    return out


def _short(text: str) -> str:
    return text if len(text) <= PROMPT_CHARS else text[:PROMPT_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------

def _text_width(text: str, size: float) -> float:
    blf.size(0, size)
    return blf.dimensions(0, text)[0]


def _visible_span(area, region) -> tuple[float, float]:
    """The part of the viewport not under the N panel or the tool shelf when
    those overlap it, so the bar centres on what you see."""
    x0, x1 = 0.0, float(region.width)
    for r in area.regions:
        if r.type == 'UI' and r.width > 1 and r.x >= region.x + region.width / 2:
            x1 = min(x1, float(r.x - region.x))
        elif r.type == 'TOOLS' and r.width > 1 and r.x <= region.x + 1:
            x0 = max(x0, float(r.x + r.width - region.x))
    return x0, x1


def _plain_width(label: str, has_icon: bool, u: float, size: float) -> float:
    side = BUTTON * u
    if not label:
        return side
    return (side if has_icon else 0) + _text_width(label, size) + 12 * u


def _take_width(u: float, size: float) -> float:
    """The review set -- Accept, Redo, Reject and the gaps between them --
    which is the widest the take slot gets. Every state of the slot is drawn
    this wide, so the bar never changes length and no button moves under the
    mouse when a take arrives."""
    return (_plain_width("Accept", True, u, size) + _plain_width("Redo", True, u, size)
            + _plain_width("Reject", True, u, size) + 2 * GROUP_GAP * u)


def _width(it: Item, u: float, size: float) -> float:
    if it.width == "take":
        return _take_width(u, size)
    return _plain_width(it.label, bool(it.icon), u, size)


def layout(context, area, region):
    """The bar's rectangle, each button's ``(item, rect)`` and the dividers'
    x positions, in region pixels."""
    its = items(context)
    if not its:
        return (), [], []
    u = _ui()
    size, side, pad = TEXT_SIZE * u, BUTTON * u, PAD * u
    widths = [_width(it, u, size) for it in its]
    gaps = [(GROUP_GAP if a.group != b.group else GAP) * u for a, b in zip(its, its[1:])]
    total = sum(widths) + sum(gaps)
    x0, x1 = _visible_span(area, region)
    x = max(x0 + pad, (x0 + x1) / 2 - total / 2)
    left, y = x, BOTTOM * u + pad
    rects, dividers = [], []
    for i, (it, w) in enumerate(zip(its, widths)):
        if i:
            g = gaps[i - 1]
            if g > GAP * u:
                dividers.append(x + g / 2)
            x += g
        rects.append((it, (x, y, x + w, y + side)))
        x += w
    return (left - pad, y - pad, x + pad, y + side + pad), rects, dividers


def shown(context) -> bool:
    s = getattr(context.scene, "animatica", None)
    return bool(s is not None and getattr(s, "show_toolbar", True))


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _rounded(_shader, rect, r, color, left=True, right=True):
    st.rounded(rect, r, color, left=left, right=right)


def _icon(name: str, cx: float, cy: float, size: float, color) -> None:
    st.icon(name, cx, cy, size, color)


def _hot(live, it) -> bool:
    return it.enabled and any(g.is_highlight for g in live.get(it.id, ()) if not g.hide)


def draw_bar(context) -> None:
    area, region = context.area, context.region
    if area is None or region is None or region.type != 'WINDOW' or not shown(context):
        return
    try:
        bar, rects, dividers = layout(context, area, region)
    except Exception:                           # noqa: BLE001 -- never break the viewport
        return
    if not bar:
        return
    u = _ui()
    live = _live.get(area.as_pointer(), {})
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    _rounded(shader, bar, (RADIUS + 3) * u, BAR_COLOR)
    size = TEXT_SIZE * u
    side = BUTTON * u
    for i, (it, rect) in enumerate(rects):
        x0, y0, x1, y1 = rect
        # A group is one segmented strip: rounded at its ends, square between.
        first = i == 0 or rects[i - 1][0].group != it.group
        last = i == len(rects) - 1 or rects[i + 1][0].group != it.group
        if not it.icon and not it.op:          # a plain label, e.g. "2/4"
            _rounded(shader, rect, RADIUS * u, BUTTON_COLOR, left=first, right=last)
            blf.size(0, size)
            tw = blf.dimensions(0, it.label)[0]
            blf.color(0, *WHITE[:3], 0.7)
            blf.position(0, (x0 + x1 - tw) / 2, (y0 + y1) / 2 - size * 0.36, 0)
            blf.draw(0, it.label)
            continue
        hot = _hot(live, it)
        # hover lifts the segment itself: an outline round one segment of a strip read as a gap
        if it.primary:
            color = PRIMARY_HOVER if hot else PRIMARY_COLOR
        elif it.on:
            color = st.ON_HOVER if hot else ON_COLOR
        else:
            color = st.TILE_HOVER if hot else BUTTON_COLOR
        dim = not it.enabled and it.progress is None
        if dim:
            color = (color[0], color[1], color[2], 0.45)
        _rounded(shader, rect, RADIUS * u, color, left=first, right=last)
        if it.progress is not None:
            # an estimate (no server progress in MMCP), in the accent, never an empty-looking stub
            w = (x1 - x0) * max(0.0, min(it.progress, 0.97))
            if w > 2 * RADIUS * u:
                _rounded(shader, (x0, y0, x0 + w, y1), RADIUS * u, FILL_COLOR,
                         left=first, right=last and w >= x1 - x0 - 1)
        if it.primary:
            tint = st.ON_PRIMARY                   # white on soft orange does not read
        elif it.rec or it.on:
            tint = REC_COLOR
        else:
            tint = WHITE
        if dim:
            tint = (tint[0], tint[1], tint[2], 0.4)
        _icon(it.icon, x0 + side / 2, (y0 + y1) / 2, ICON * u, tint)
        if it.label:
            blf.size(0, size)
            blf.color(0, *tint)
            blf.position(0, x0 + side - 2 * u, (y0 + y1) / 2 - size * 0.36, 0)
            blf.draw(0, it.label)
    gpu.state.blend_set('NONE')


# ---------------------------------------------------------------------------
# The buttons under the icons: Blender's own gizmos (click, hover, tooltip)
# ---------------------------------------------------------------------------

#: every button that can be on the bar, and how many gizmos cover it: a
#: gizmo's click area is round however it is scaled, so a wide button takes several
SLOTS = {
    "key_prev": 1, "key_next": 1, "autopose": 1, "describe": 1, "set_key": 1, "auto_key": 1,
    "waypoint": 1, "pin": 1, "prompt": 6, "connect": 9, "working": 9,
    "generate": 9, "accept": 3, "redo": 3, "var_prev": 1, "var_next": 1,
    "reject": 3, "ghost": 1, "model": 6, "picker": 1,
}


class ANIMATICA_GT_toolbar_bar(bpy.types.Gizmo):
    """The bar, its icons and labels. A gizmo made last in its group, which
    draws it first: under the buttons, which are themselves invisible."""
    bl_idname = "ANIMATICA_GT_toolbar_bar"

    def draw(self, context):
        draw_bar(context)

    def test_select(self, context, location):
        return -1


class ANIMATICA_GGT_toolbar(bpy.types.GizmoGroup):
    bl_idname = "ANIMATICA_GGT_toolbar"
    bl_label = "Animatica Toolbar"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        from . import properties

        s = getattr(context.scene, "animatica", None)
        return bool(shown(context) and properties._live_armature(s.target_armature))

    def setup(self, context):
        self.cells = {}
        for name, n in SLOTS.items():
            row = []
            for _k in range(n):
                gz = self.gizmos.new("GIZMO_GT_button_2d")
                gz.draw_options = set()
                gz.show_drag = False
                gz.use_tooltip = True
                gz.icon = 'NONE'
                gz.hide = True
                row.append([gz, None])
            self.cells[name] = row
        self.bar = self.gizmos.new(ANIMATICA_GT_toolbar_bar.bl_idname)
        self.bar.hide_select = True

    def draw_prepare(self, context):
        area, region = context.area, context.region
        try:
            _bar, rects, _div = layout(context, area, region)
        except Exception:                       # noqa: BLE001
            rects = []
        side = BUTTON * _ui()
        placed = set()
        for it, (x0, y0, x1, y1) in rects:
            row = self.cells.get(it.id)
            if row is None:
                continue
            placed.add(it.id)
            key = (it.op, tuple(sorted(it.props.items())))
            cy = (y0 + y1) / 2
            for k, cell in enumerate(row):
                gz, bound = cell
                cx = x0 + side / 2 + side * 0.8 * k
                if it.op is None or (k and cx - side / 2 >= x1):
                    gz.hide = True
                    continue
                if bound != key:
                    props = gz.target_set_operator(it.op)
                    for name, value in it.props.items():
                        setattr(props, name, value)
                    cell[1] = key
                gz.hide = False
                gz.hide_select = not it.enabled
                gz.scale_basis = side / 2
                gz.matrix_basis = Matrix.Translation((min(cx, x1 - side / 2), cy, 0.0))
        for name, row in self.cells.items():
            if name not in placed:
                for gz, _b in row:
                    gz.hide = True
        _live[area.as_pointer()] = {n: [g for g, _b in row] for n, row in self.cells.items()}


# ---------------------------------------------------------------------------
# The operators the bar needs that the add-on did not have
# ---------------------------------------------------------------------------

_TOGGLE_TIPS = {
    "show_picker": "Handle picker: the character in T-pose with the Autoposer's handles on it. "
                   "Pick them, switch them on or off, set their slack, add or remove them",
    "auto_key_pose": "Auto-key: key the pose as you pose it. Off: only Set Key writes one",
    "key_pose_overlay": "Show the key poses, the trail and the frame numbers in the viewport. "
                        "Off to judge the motion on its own",
}


class ANIMATICA_OT_toolbar_toggle(bpy.types.Operator):
    bl_idname = "animatica.toolbar_toggle"
    bl_label = "Toggle"
    bl_options = {'INTERNAL', 'UNDO'}

    name: StringProperty(options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        return _TOGGLE_TIPS.get(properties.name, "")

    def execute(self, context):
        s = context.scene.animatica
        if not hasattr(s, self.name):
            return {'CANCELLED'}
        setattr(s, self.name, not getattr(s, self.name))
        if self.name == "show_picker" and s.show_picker:
            s.picker_collapsed = False          # opened to be used, not as a folded title
        return {'FINISHED'}


class ANIMATICA_OT_toolbar_generate(bpy.types.Operator):
    bl_idname = "animatica.toolbar_generate"
    bl_label = "Generate"
    bl_options = {'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        why = blockers(context)
        return why[0] if why else "Make the take: motion from your prompts, key poses and waypoints"

    def invoke(self, context, event):
        return bpy.ops.animatica.generate('INVOKE_DEFAULT')


class ANIMATICA_OT_toolbar_redo(bpy.types.Operator):
    """Redo the block under the playhead. Shift: the whole take"""
    bl_idname = "animatica.toolbar_redo"
    bl_label = "Redo"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        s = context.scene.animatica
        here = block_at(s, context.scene.frame_current)
        if event.shift or here < 0:
            return bpy.ops.animatica.generate('INVOKE_DEFAULT')
        return bpy.ops.animatica.regenerate_block('INVOKE_DEFAULT', block_index=here)


class ANIMATICA_MT_toolbar_models(bpy.types.Menu):
    bl_idname = "ANIMATICA_MT_toolbar_models"
    bl_label = "Model"

    def draw(self, context):
        from . import mmcp_client
        s = context.scene.animatica
        layout = self.layout
        for ident, name, info in mmcp_client.cached_model_items():
            row = layout.row()
            row.prop_enum(s, "model_id", ident, text=f"{name}   ({info})" if info else name)
        layout.separator()
        layout.label(text=mmcp_client.get_mmcp_url(), icon='URL')
        layout.operator("animatica.connect", text="Reconnect", icon='FILE_REFRESH')
        op = layout.operator("preferences.addon_show", text="Server Settings…", icon='PREFERENCES')
        op.module = __package__


class ANIMATICA_OT_toolbar_model(bpy.types.Operator):
    """The model that makes the takes. Click to pick another, or change the server"""
    bl_idname = "animatica.toolbar_model"
    bl_label = "Model"
    bl_options = {'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        from . import mmcp_client
        s = context.scene.animatica
        return (f"Model: {s.model_id}, on {mmcp_client.get_mmcp_url()}.\n"
                "Click to pick another model, or change the server")

    def invoke(self, context, event):
        # The bar acts on the press. A menu opened then closed again on the
        # release, so it only stayed while the button was held: open it once
        # the click is over, as a click on any other menu button does.
        if event.value == 'PRESS':
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        return self._open(context)

    def modal(self, context, event):
        if event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'ESC'} and event.value == 'RELEASE' \
                or event.type == 'ESC':
            return self._open(context) if event.type == 'LEFTMOUSE' else {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _open(self, context):
        bpy.ops.wm.call_menu(name=ANIMATICA_MT_toolbar_models.bl_idname)
        return {'FINISHED'}


class ANIMATICA_OT_toolbar_describe(bpy.types.Operator):
    """Describe a pose in words and key it at the playhead"""
    bl_idname = "animatica.toolbar_describe"
    bl_label = "Describe a Pose"
    bl_options = {'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        why = _describe_blocker(context)
        return f"Describe a pose in words and key it at the playhead.\n{why}" if why else \
            "Describe a pose in words and key it at the playhead"

    def invoke(self, context, event):
        why = _describe_blocker(context)
        if why:
            # the click used to do nothing at all: say why
            self.report({'WARNING'}, why)
            return {'CANCELLED'}
        return bpy.ops.animatica.generate_pose('INVOKE_DEFAULT')


def _describe_blocker(context) -> str:
    """Why Describe cannot run here, in the artist's words, or ''."""
    from . import mmcp_client
    s = context.scene.animatica
    caps = mmcp_client.cached_model(s.model_id) if s.model_id else None
    if caps is None:
        return "Connect to the server first"
    if "pose" not in (caps.get("supported_segments") or []):
        return (f"The model '{s.model_id}' on {mmcp_client.get_mmcp_url()} can't make a pose "
                f"from words; pick a model (or a server) that can")
    if bpy.ops.animatica.generate_pose.poll():
        return ""
    return "Not available right now"


class ANIMATICA_OT_toolbar_autoposer(bpy.types.Operator):
    """Autoposer: drag hands, feet and hips and the body follows. Again to give the rig back"""
    bl_idname = "animatica.toolbar_autoposer"
    bl_label = "Autoposer"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        from . import properties
        from .autoposer import poser

        arm = properties._live_armature(context.scene.animatica.target_armature)
        if arm is None:
            return {'CANCELLED'}
        if poser.has_controls(arm):
            return bpy.ops.animatica.give_back_rig('INVOKE_DEFAULT')
        return bpy.ops.autoposer.build_rig('INVOKE_DEFAULT')


class ANIMATICA_OT_toolbar_prompt_here(bpy.types.Operator):
    bl_idname = "animatica.toolbar_prompt_here"
    bl_label = "Prompt"
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def description(cls, context, properties):
        s = context.scene.animatica
        here = block_at(s, context.scene.frame_current)
        text = (s.prompt_blocks[here].prompt or "").strip() if here >= 0 else ""
        if text:
            return f"\u201c{text}\u201d \u2014 click to edit what happens in this block"
        return "Say what happens here: the prompt of the block under the playhead, or a new block from it"

    def invoke(self, context, event):
        from .timeline_overlay import DEFAULT_BLOCK_LENGTH, get_sorted_blocks

        s = context.scene.animatica
        frame = context.scene.frame_current
        i = block_at(s, frame)
        if i < 0:
            after = [st for _i, st, _e in get_sorted_blocks(s.prompt_blocks) if st > frame]
            end = min([frame + DEFAULT_BLOCK_LENGTH, context.scene.frame_end] + after)
            if end - frame < 2:
                self.report({'WARNING'}, "No room for a block here")
                return {'CANCELLED'}
            b = s.prompt_blocks.add()
            b.prompt, b.frame_start, b.frame_end, b.enabled = "", frame, end, True
            i = len(s.prompt_blocks) - 1
        s.active_block_index = i
        return bpy.ops.animatica.edit_strip_prompt('INVOKE_DEFAULT', index=i)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (ANIMATICA_OT_toolbar_toggle, ANIMATICA_OT_toolbar_generate, ANIMATICA_OT_toolbar_redo,
            ANIMATICA_OT_toolbar_autoposer, ANIMATICA_OT_toolbar_describe,
            ANIMATICA_MT_toolbar_models, ANIMATICA_OT_toolbar_model,
            ANIMATICA_OT_toolbar_prompt_here, ANIMATICA_GT_toolbar_bar, ANIMATICA_GGT_toolbar)
_NS = "_animatica_toolbar_handle"            # an older copy drew from a handler


def _tick():
    """Redraw while a take is generating, so the seconds count."""
    try:
        if any(getattr(getattr(sc, "animatica", None), "is_generating", False) for sc in bpy.data.scenes):
            for win in bpy.context.window_manager.windows:
                for a in win.screen.areas:
                    if a.type in {'VIEW_3D', 'DOPESHEET_EDITOR'}:
                        a.tag_redraw()
    except Exception:                           # noqa: BLE001
        pass
    return 0.25


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    old = bpy.app.driver_namespace.pop(_NS, None)
    if old is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(old, 'WINDOW')
        except (ValueError, RuntimeError):
            pass
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=1.0, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _live.clear()
    st.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
