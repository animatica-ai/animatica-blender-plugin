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
HINT_H = 24          # the next-step line above the bar
FIELD = 210          # px at 1x: the prompt field, before the take's buttons take their share
REACH = 96           # px at 1x: the reach tile -- its falloff curve and "±6 · 100%"
PLACEHOLDER = "Describe what happens here…"
#: the field's editing index while it holds the pose description, not a block's prompt
POSE_FIELD = -2
#: the buttons of the take slot, whose widths the prompt field gives way to
TAKE_IDS = {"connect", "generate", "working", "redo", "keep", "var_prev", "var_label", "var_next", "loop",
            "reject", "generate_pose"}
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
                 "group", "width", "progress", "badge")

    def __init__(self, id, icon, op=None, props=None, *, label="", on=False, rec=False,
                 enabled=True, primary=False, group=0, width=None, progress=None, badge=False):
        self.id, self.icon, self.label, self.op, self.props = id, icon, label, op, props or {}
        self.on, self.rec, self.enabled, self.primary = on, rec, enabled, primary
        self.group, self.width, self.progress = group, width, progress
        self.badge = badge         # a dot on the button: something here wants doing


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


def gate(context) -> tuple:
    """What stands between the artist and a take, the first thing first:
    ``(kind, text, operator)``.

    ``offline`` -- Blender's online access is off; ``connect`` -- no server
    yet; ``sign_in`` -- the cloud needs an account; ``blocked`` -- nothing to
    go on yet (``text`` says what); ``ready``. One answer for the sidebar's
    button and the bar's, each naming the step instead of greying out without
    a word.
    """
    from . import mmcp_client
    s = context.scene.animatica
    if mmcp_client.offline():
        return ("offline", "Allow Online Access", "animatica.allow_online")
    if mmcp_client.cached_model(s.model_id) is None:
        return ("connect", "Connect", "animatica.connect")
    if mmcp_client.needs_sign_in():
        return ("sign_in", "Sign in to Generate", "animatica.signin_generate")
    why = blockers(context)
    if why:
        from .request_builder import MODEL_RIG_MISMATCH
        if why[0].startswith(MODEL_RIG_MISMATCH):
            # the one blocker with a button of its own: the model menu
            return ("model", "Pick a Model", "animatica.toolbar_model")
        return ("blocked", why[0], None)
    return ("ready", "Generate", "animatica.toolbar_generate")


def _prompt_label(settings, here: int) -> str:
    """The prompt of the block under the playhead, whole (the field cuts it
    to fit); empty shows the placeholder."""
    return (settings.prompt_blocks[here].prompt or "").strip() if here >= 0 else ""


#: a pose being made from words (not a take): the bar stays in Pose while it works
_pose_job = {"on": False}


def mode_of(context) -> str:
    """One bar, no modes: kept for the code that still asks."""
    return 'POSE'


def pose_mode_on(context) -> bool:
    return True


def items(context, mode: str | None = None) -> list[Item]:
    """The buttons, in the order the work goes: pose and key the moments,
    say where it goes, say what happens and Generate, judge the take, then
    fine-tune it. One bar: nothing moves to another mode on the way."""
    from . import mmcp_client, properties

    s = context.scene.animatica
    arm = properties._live_armature(s.target_armature)
    if arm is None:
        return _setup_items(context)
    out = _bar_items(context, arm)
    # what makes it: the model, at the far end -- set up once, changed rarely
    if s.model_id not in ("", "NONE") and mmcp_client.cached_model(s.model_id) is not None:
        out.append(Item("model", "model", "animatica.toolbar_model", label=_short(s.model_id),
                        enabled=not s.is_generating, group=10))
    return out + _hint_items(context)


def _hint_items(context) -> list:
    """The next step and why, on a line of its own above the bar."""
    from . import guidance
    if not getattr(context.scene.animatica, "show_hints", True):
        return []
    try:
        h = guidance.next_step(context)
    except Exception:                                # noqa: BLE001 -- a hint never breaks the bar
        return []
    if h is None:
        return []
    return [Item("hint", "", h["op"] or "animatica.hint_why", h["props"], label=h["text"],
                 width="hint", group=99),
            Item("hint_close", "", "animatica.hint_dismiss", {"key": h["id"]}, width="hint_close", group=99)]


def _key_steps(group):
    return [Item("key_prev", "key_prev", "animatica.step_key_pose", {"direction": 'PREV'}, group=group),
            Item("key_next", "key_next", "animatica.step_key_pose", {"direction": 'NEXT'}, group=group)]


def _typing_pose() -> bool:
    from .timeline_overlay import inline_edit_state as edit
    return bool(edit["active"] and edit["index"] == POSE_FIELD)


def _bar_items(context, arm) -> list:
    """Set keyframes, Generate, Accept, then fine-tune: the groups in that
    order. The take's own buttons (Generate, or Accept and Reject while one
    waits) sit after the field that says what it is."""
    from . import curve_edit, handles, key_poses, mmcp_client, wormhole
    s = context.scene.animatica
    here = block_at(s, context.scene.frame_current)
    keyed = key_poses.keyed_here(context.scene)
    n_keys = len(key_poses.take_keys(context.scene))
    autokey = bool(context.scene.tool_settings.use_keyframe_insert_auto)
    active = handles.tool_active(context)

    def can(tool):
        # greyed out when the connected model can't use it
        return mmcp_client.tool_available(s.model_id, tool)

    # 1 pose it
    out = [Item("autopose", "autopose", "animatica.toolbar_autoposer", on=active, group=1)]
    if active or handles.has(context.scene, arm):
        out.append(Item("picker", "picker", "animatica.toolbar_toggle", {"name": "show_picker"},
                        on=bool(s.show_picker), group=1))
    # 2 key it
    prev, nxt = _key_steps(2)
    out.append(prev)
    if not s.is_previewing:
        # in review the take's Key This Frame does it
        out.append(Item("set_key", "set_key_add", "animatica.set_key_pose", group=2))
    out += [nxt,
            # Blender's own record button: one record button, not two
            Item("auto_key", "auto_key", "animatica.toolbar_autokey", on=autokey, rec=autokey, group=2)]
    # 3 where it goes
    out += [Item("waypoint", "waypoint", "animatica.add_waypoint", enabled=can("waypoint"), group=3,
                 badge=bool(_waypoint_hint(context))),
            Item("pin", "pin", "animatica.add_effector_target", enabled=can("pin"), group=3)]
    # 4 what happens: the block's prompt, and a pose of this frame in words at its end
    posing = bool(s.field_pose) and can("describe")
    if posing:
        out.append(Item("prompt", "", "animatica.toolbar_pose_prompt", label=(s.last_pose_prompt or "").strip(),
                        group=4, width="field"))
    else:
        out.append(Item("prompt", "", "animatica.toolbar_prompt_here", label=_prompt_label(s, here),
                        enabled=can("prompt"), group=4, width="field"))
    out.append(Item("pose_text", "mode_pose", "animatica.toolbar_field_pose", on=posing or _typing_pose(),
                    enabled=can("describe") and not s.is_generating, group=4, width="field_btn"))
    # 5-7 the take: Generate, or the take at work, or the take to judge
    kind, text, op = gate(context)
    busy = s.is_generating or s.is_previewing
    # a loop is one way to make it, not a setting: beside Generate
    model = mmcp_client.cached_model(s.model_id) or {}
    if model.get("supports_loop") and not posing:
        one = sum(1 for b in s.prompt_blocks if getattr(b, "enabled", True)) <= 1
        out.append(Item("loop", "loop", "animatica.toolbar_toggle", {"name": "loop"}, on=bool(s.loop),
                        enabled=(one or s.loop) and not s.is_generating, group=5))
    review = _review_items(context, arm, keyed)
    if kind in ("offline", "connect") and not busy:
        out.append(_server_item(kind, text, op, group=5))
    elif kind == "model" and not busy:
        out.append(Item("generate", "model", op, label=text, primary=True, group=5))
    elif kind == "sign_in" and not busy:
        out.append(Item("generate", "generate", op, label=text, primary=True, group=5))
    elif s.is_generating:
        out.append(_working(s))
    elif review:
        out += review
    elif posing:
        # the field describes this frame's pose: its Generate makes that
        why = _describe_blocker(context)
        words = (s.last_pose_prompt or "").strip()
        out.append(Item("generate_pose", "generate", "animatica.toolbar_pose_generate",
                        label="Generate Pose", primary=bool(words) and not why, enabled=not why, group=5))
    else:
        # blocked: the button says what is missing (short enough to read), the tooltip the rest
        if kind == "blocked" and len(text) <= 24:
            label = text
        elif n_keys:
            label = f"Generate · {n_keys} key{'s' if n_keys != 1 else ''}"
        else:
            label = "Generate"
        out.append(Item("generate", "generate", "animatica.toolbar_generate", label=label,
                        primary=kind == "ready", enabled=kind == "ready", group=5))
    # 8 fine-tune it: see the motion around this frame, its path, and how far an edit carries
    out += [
        Item("onion", "onion", "animatica.toolbar_overlay", {"part": "GHOSTS"},
             on=key_poses.ghosts_on(s), group=8),
        Item("wormhole", "wormhole", "animatica.toolbar_wormhole",
             on=bool(s.onion_wormhole) and key_poses.ghosts_on(s) and s.onion_mode == 'FRAMES', group=8),
        Item("trail", "trail", "animatica.toolbar_overlay", {"part": "TRAIL"},
             on=key_poses.trail_on(s), group=8),
    ]
    if active and (autokey or wormhole.editable(context)):
        out.append(Item("reach", "", "animatica.reach_drag", {"part": "bar"}, group=8, width="reach"))
    # a point on the trail picked: even out the motion around it
    if curve_edit.selected() is not None and key_poses.trail_on(s):
        out.append(Item("smooth", "smooth", "animatica.smooth_trail", group=8))
    # hold a hand or foot on one spot for a stretch of frames: always there once
    # there is motion (it was hidden until a foot was picked, and not found)
    from . import pose_edit
    if pose_edit._editing_action(arm) is not None:
        out.append(Item("lock", "lock", "animatica.lock_joint", group=8))
    out.append(Item("options", "options", "animatica.toolbar_menu", {"menu": "ANIMATICA_PT_options"},
                    group=9))
    return out


def _review_items(context, arm, keyed) -> list:
    """A take waiting for review: Accept, Redo, Key This Frame, its versions,
    Reject. Empty when there is none."""
    from . import batch, variations
    s = context.scene.animatica
    if not s.is_previewing or batch.pending(s) or batch.failures(s):
        return []
    # no Accept: the take is yours when it comes, fine-tuning it keeps it. What
    # there is to decide: make the rest (Generate, when blocks are left),
    # retry this one, or throw it away
    out = []
    if _more_to_make(context, arm):
        out.append(Item("generate", "generate", "animatica.toolbar_generate", label="Generate",
                        primary=True, group=5))
    out += [
           # one Redo: the block under the playhead, Shift for the whole take
           Item("redo", "redo", "animatica.toolbar_redo", label="Redo", group=6),
           Item("keep", "set_key", "animatica.keep_frame", label="Key This Frame",
                enabled=not keyed, group=6)]
    take = variations.take_of(arm)
    if take is not None and take.count > 1:
        out.append(Item("var_prev", "var_prev", "animatica.show_variation", {"step": -1}, group=6))
        out.append(Item("var_label", "", None, label=f"{take.index + 1}/{take.count}",
                        enabled=False, group=6))
        out.append(Item("var_next", "var_next", "animatica.show_variation", {"step": 1}, group=6))
    out.append(Item("reject", "reject", "animatica.reject", label="Discard", group=7))
    return out


def _more_to_make(context, arm) -> bool:
    """Whether there is a block the take did not cover (and nobody locked):
    something left for Generate while the take is judged."""
    s = context.scene.animatica
    span = arm.get("animatica_take_range")
    if span is None or len(span) < 2:
        return False
    lo, hi = int(span[0]), int(span[1])
    return any(getattr(b, "enabled", True) and not b.locked and not (b.frame_start < hi and b.frame_end > lo)
               for b in s.prompt_blocks)


def _working(s):
    elapsed = float(getattr(s, "generation_elapsed", 0))
    # one button, the width Generate had: the time it has taken, filling, and
    # a click on it cancels -- a Cancel beside it made the bar jump
    return Item("working", "cancel", "animatica.cancel", label=f"{int(elapsed)} s  ·  Cancel",
                group=5, progress=1.0 - math.exp(-elapsed / EXPECTED_SECONDS))


def _setup_items(context) -> list:
    """The bar before there is a character: the way in, so the sidebar is
    never where a first session has to start. Online access if Blender keeps
    the add-on offline; then a character -- the ready-made one, the rig
    already in the scene, or a whole example scene."""
    from . import canonical_skeleton, mmcp_client
    s = context.scene.animatica
    from . import remote_asset
    kind, text, op = gate(context)
    out = []
    # the server's state is shown, but it never hides the way in: a rig of
    # your own, a downloaded character and posing all work without it
    if kind in ("offline", "connect"):
        out.append(_server_item(kind, text, op, group=0, primary=False))
    online = kind != "offline" or remote_asset.is_cached()
    fetching = canonical_skeleton.download_state()
    if fetching["active"]:
        pct = float(fetching["percent"])
        out.append(Item("fetching", "add_character", None, label=f"Fetching the character… {pct:.0f}%",
                        group=1, progress=pct / 100.0))
    else:
        out.append(Item("add_char", "add_character", "animatica.import_canonical_skeleton",
                        label="Add a Character", primary=online, enabled=online, group=1))
        out.append(Item("use_rig", "rig", "animatica.use_selected_rig", label="Use Selected Rig",
                        enabled=_selected_rig(context) is not None, group=1))
    out.append(Item("examples", "examples", "animatica.toolbar_menu", {"menu": "ANIMATICA_MT_examples"},
                    label="Examples", group=2))
    if mmcp_client.cached_model(s.model_id) is not None:
        out.append(Item("model", "model", "animatica.toolbar_model", label=_short(s.model_id), group=10))
    return out


def _server_item(kind, text, op, group, primary=True):
    """Online access, or the connection: what it is doing, in words. A bare
    Connect said nothing about why there was no server, and auto-connect was
    already doing what it offered."""
    from . import mmcp_client
    if kind == "offline":
        return Item("connect", "connect", op, label=text, primary=primary, group=group)
    trying = mmcp_client.connecting() or not mmcp_client.last_connection_error()
    return Item("connect", "connect", None if trying else op,
                label="Connecting\u2026" if trying else "Can't reach Animatica \u00b7 Retry",
                primary=primary and not trying, group=group)


def _selected_rig(context):
    """The armature the artist has selected: an armature, or the one a selected mesh is bound to."""
    ob = context.active_object
    if ob is None:
        return None
    if ob.type == 'ARMATURE':
        return ob
    if ob.type == 'MESH':
        return ob.find_armature()
    return None


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
    """The widest the take's buttons get: the review set (Accept, Redo,
    Reject), or the longest single button. The prompt field gives up what
    the buttons of the moment do not use, so the bar keeps one length and
    no button moves under the mouse when a take arrives."""
    review = (_plain_width("Generate", True, u, size) + _plain_width("Redo", True, u, size)
              + _plain_width("Key This Frame", True, u, size) + GAP * u
              + _plain_width("Discard", True, u, size) + 2 * GROUP_GAP * u)
    single = max(_plain_width(t, True, u, size)
                 for t in ("Generate · 99 keys", "Sign in to Generate", "Allow Online Access", "99 s  ·  Cancel"))
    return max(review, single)


def _width(it: Item, u: float, size: float) -> float:
    if it.width == "field":
        return FIELD * u
    if it.width == "reach":
        return REACH * u
    if it.width == "field_btn":
        return BUTTON * u
    return _plain_width(it.label, bool(it.icon), u, size)


def _measure(its, u, size):
    """Each button's width and the gaps between them, the field already
    taking up what the take's buttons leave of their widest."""
    widths = [_width(it, u, size) for it in its]
    gaps = [(GROUP_GAP if a.group != b.group else GAP) * u for a, b in zip(its, its[1:])]
    take = [i for i, it in enumerate(its) if it.id in TAKE_IDS]
    if take:
        now = sum(widths[i] for i in take) + sum(gaps[i] for i in take[:-1])
        for i, it in enumerate(its):
            if it.width == "field":
                widths[i] = max(120 * u, widths[i] + _take_width(u, size) - now)
    ids = {it.id for it in its}
    if "key_prev" in ids and "autopose" in ids and "set_key" not in ids:
        # Pose in review hides Set Key Pose (the take's Key This Frame does it):
        # the field takes its place, and nothing moves under the mouse
        for i, it in enumerate(its):
            if it.width == "field":
                widths[i] += _plain_width("", True, u, size) + GAP * u
    return widths, gaps


def _grow_field(its, widths, by, floor=120):
    """Give the field ``by`` pixels more (fewer when negative, down to its
    ``floor``, logical px); what it actually took."""
    for i, it in enumerate(its):
        if it.width == "field":
            new = max(floor * _ui(), widths[i] + by)
            taken = new - widths[i]
            widths[i] = new
            return taken
    return 0.0


def _shelf_lift(area, region) -> float:
    """How far up the bar goes to clear Blender's asset shelf (the pose
    library, in Pose Mode), which docks at the bottom of the viewport."""
    lift = 0.0
    for r in area.regions:
        if r.type in {'ASSET_SHELF', 'ASSET_SHELF_HEADER'} and r.width > 1 and r.height > 1 \
                and r.y - region.y < region.height / 2:
            lift = max(lift, float(r.y + r.height - region.y))
    return lift


#: what goes first when even the compact bar does not fit, and where it is then:
#: the Options popover beside Generate shows whatever was folded into it
FOLD = ("smooth", "pin", "waypoint", "picker", "reach", "wormhole", "onion", "trail", "model", "auto_key",
        "key_steps", "autopose", "set_key")
#: folded together: one without the other would be half a control
_FOLD_TOGETHER = {"key_steps": ("key_prev", "key_next")}
#: ids folded off the bar at its last layout, by area
_folded: dict = {}


def folded(area) -> set:
    return _folded.get(area.as_pointer(), set()) if area is not None else set()


def layout(context, area, region):
    """The bar's rectangle, each button's ``(item, rect)`` and the dividers'
    x positions, in region pixels."""
    its = items(context)
    if not its:
        return (), [], []
    hint = [it for it in its if it.width in ("hint", "hint_close")]
    its = [it for it in its if it.width not in ("hint", "hint_close")]
    u = _ui()
    size, side, pad = TEXT_SIZE * u, BUTTON * u, PAD * u
    widths, gaps = _measure(its, u, size)
    total = sum(widths) + sum(gaps)
    x0, x1 = _visible_span(area, region)
    room = x1 - x0 - 2 * pad
    gone = set()
    if total > room:
        # a narrow viewport, step by step: the field gives way first...
        total += _grow_field(its, widths, room - total)
    if total > room:
        # ...then the words that have an icon to stand for them: Pose, Motion,
        # the model's name (the tooltips still say them)...
        for it in its:
            if it.id in ("mode_pose", "mode_motion", "model") and it.icon and it.label:
                it.label = ""
        widths, gaps = _measure(its, u, size)
        total = sum(widths) + sum(gaps)
        total += _grow_field(its, widths, room - total)
    floor = 120
    every = list(its)                  # the bar before any folding, in its order
    folded_order = []
    for fid in FOLD:
        # ...then the buttons used least fold into the Options popover; Generate
        # and the Pose / Motion switch never leave the screen
        if total <= room:
            break
        if fid == "auto_key":
            # before the core posing buttons go, the field goes down to a stub
            # -- and stays one: every fold after re-measures it
            floor = 56
            total += _grow_field(its, widths, room - total, floor)
            if total <= room:
                break
        ids = _FOLD_TOGETHER.get(fid, (fid,))
        if not any(it.id in ids for it in its):
            continue
        its = [it for it in its if it.id not in ids]
        gone.update(ids)
        folded_order.append(ids)
        widths, gaps = _measure(its, u, size)
        total = sum(widths) + sum(gaps)
        total += _grow_field(its, widths, room - total, floor)
    # the last fold can free more than was needed, and the field took it: bring
    # back what fits again (the most used first), the field at its stub
    for ids in reversed(folded_order[-3:]):
        trial = [it for it in every if it.id not in gone or it.id in ids]
        tw, tg = _measure(trial, u, size)
        tt = sum(tw) + sum(tg)
        tt += _grow_field(trial, tw, room - tt, floor)
        if tt <= room:
            its, widths, gaps, total = trial, tw, tg, tt
            gone.difference_update(ids)
        else:
            break
    if total > room:
        # ...and last the field, down to a stub you can still click to type in
        for i, it in enumerate(its):
            if it.width == "field":
                cut = min(total - room, widths[i] - 56 * u)
                if cut > 0:
                    widths[i] -= cut
                    total -= cut
    if area is not None:
        _folded[area.as_pointer()] = gone
    x = max(x0 + pad, (x0 + x1) / 2 - total / 2)
    left, y = x, BOTTOM * u + pad + _shelf_lift(area, region)
    rects, dividers = [], []
    gaps = [(GROUP_GAP if a_.group != b_.group else GAP) * u for a_, b_ in zip(its, its[1:])]
    for i, (it, w) in enumerate(zip(its, widths)):
        if i:
            g = gaps[i - 1]
            if g > GAP * u:
                dividers.append(x + g / 2)
            x += g
        rects.append((it, (x, y, x + w, y + side)))
        x += w
    bar = (left - pad, y - pad, x + pad, y + side + pad)
    if hint:
        # the next step, on a slim line just above the bar, as wide as its words
        h = HINT_H * u
        hy = bar[3] + 6 * u
        close = h
        text_w = _text_width(hint[0].label, size) + 30 * u
        hx1 = min(bar[0] + text_w, bar[2] - close - 2 * u)
        rects.append((hint[0], (bar[0], hy, hx1, hy + h)))
        rects.append((hint[1], (hx1 + 2 * u, hy, hx1 + 2 * u + close, hy + h)))
    return bar, rects, dividers


def _draw_folded(layout, context):
    """In a popover: the bar's buttons that had no room on it."""
    gone = folded(context.area)
    if not gone:
        return
    labels = {it.id: (TIPS.get(it.id, (it.id,))[0], it) for it in items(context) if it.id in gone}
    if not labels:
        return
    layout.separator()
    layout.label(text="Off the bar (no room)")
    col = layout.column(align=True)
    for fid in [i for f in FOLD for i in _FOLD_TOGETHER.get(f, (f,))]:
        if fid in labels:
            title, it = labels[fid]
            if not it.op:
                continue
            if fid == "reach":                  # a drag on the bar; here, the two numbers
                sc = context.scene.animatica
                col.prop(sc, "trail_radius", text="Reach")
                col.prop(sc, "edit_strength", text="Intensity", slider=True)
                continue
            row = col.row(align=True)
            row.enabled = it.enabled
            # a switch shows as one: pressed in when it is on
            props = row.operator(it.op, text=title.split("  (")[0], depress=bool(it.on))
            for k, v in it.props.items():
                try:
                    setattr(props, k, v)
                except Exception:                   # noqa: BLE001
                    pass


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


def draw_bar(context, highlighted: bool = True) -> None:
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
    hot_id = hovered(area) if highlighted else ""

    def _hot(_live, it):
        return it.enabled and it.id == hot_id

    live = None
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
        if it.width == "hint":
            _draw_hint(it, rect, u, size, _hot(live, it))
            continue
        if it.width == "hint_close":
            hot = _hot(live, it)
            st.rounded(rect, RADIUS * u, st.TILE_HOVER if hot else (*st.GROUND[:3], 0.92))
            cx, cy, c = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2, 4 * u
            st.lines([((cx - c, cy - c), (cx + c, cy + c)), ((cx - c, cy + c), (cx + c, cy - c))],
                     max(1.0, 1.3 * u), (1, 1, 1, 0.85 if hot else 0.5))
            continue
        if not it.icon and not it.op:          # a plain label, e.g. "2/4"
            _rounded(shader, rect, RADIUS * u, BUTTON_COLOR, left=first, right=last)
            blf.size(0, size)
            tw = blf.dimensions(0, it.label)[0]
            blf.color(0, *WHITE[:3], 0.7)
            blf.position(0, (x0 + x1 - tw) / 2, (y0 + y1) / 2 - size * 0.36, 0)
            blf.draw(0, it.label)
            continue
        if it.width == "field":
            # the field's ground runs on under its pose button: the button is inside it
            btn = rects[i + 1][1] if i + 1 < len(rects) and rects[i + 1][0].width == "field_btn" else None
            _draw_field(it, rect, u, size, _hot(live, it), first, last if btn is None else True,
                        ground=(x0, y0, btn[2], y1) if btn else None)
            continue
        if it.width == "reach":
            _draw_reach(context, rect, u, size, _hot(live, it), first, last)
            continue
        hot = _hot(live, it)
        dim = not it.enabled and it.progress is None
        # hover lifts the segment itself: an outline round one segment of a strip read as a gap
        if it.width == "field_btn":
            # inside the field, at its end: the field draws the ground; a soft
            # square on hover, the accent while a pose is being typed
            from .timeline_overlay import inline_edit_state as _edit
            posing = _edit["active"] and _edit["index"] == POSE_FIELD
            pad = 3 * u
            if hot or posing:
                st.rounded((x0 + pad, y0 + pad, x1 - pad, y1 - pad), (RADIUS - 2) * u,
                           st.with_alpha(st.SOFT_ORANGE, 0.28) if posing else (1, 1, 1, 0.08))
            color = None
        elif dim:
            # unavailable reads as unavailable: hardly a tile, no colour of its own, no hover
            color = st.with_alpha(BUTTON_COLOR, 0.18)
        elif it.primary:
            color = PRIMARY_HOVER if hot else PRIMARY_COLOR
        elif it.on:
            color = st.ON_HOVER if hot else ON_COLOR
        else:
            color = st.TILE_HOVER if hot else BUTTON_COLOR
        if color is not None:
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
            tint = (WHITE[0], WHITE[1], WHITE[2], 0.22)
        if not it.icon:                         # a word, e.g. Pose / Motion: centred
            blf.size(0, size)
            blf.color(0, *tint)
            blf.position(0, (x0 + x1 - blf.dimensions(0, it.label)[0]) / 2, (y0 + y1) / 2 - size * 0.36, 0)
            blf.draw(0, it.label)
            continue
        _icon(it.icon, x0 + side / 2, (y0 + y1) / 2, ICON * u, tint)
        if it.badge:
            r_ = 3.2 * u
            bx, by = x0 + side - 6 * u, y1 - 6 * u
            st.rounded((bx - r_, by - r_, bx + r_, by + r_), r_, st.SOFT_ORANGE)
        if it.label:
            blf.size(0, size)
            blf.color(0, *tint)
            blf.position(0, x0 + side - 2 * u, (y0 + y1) / 2 - size * 0.36, 0)
            blf.draw(0, it.label)
    gpu.state.blend_set('NONE')


def _draw_hint(it, rect, u, size, hot):
    """The next step: a slim line above the bar -- a Soft Orange dot (this is
    the thing to do), the words, cut to fit. Its tooltip says why."""
    x0, y0, x1, y1 = rect
    st.rounded(rect, RADIUS * u, st.TILE_HOVER if hot else (*st.GROUND[:3], 0.92))
    r = 3.2 * u
    cx, cy = x0 + 11 * u, (y0 + y1) / 2
    st.rounded((cx - r, cy - r, cx + r, cy + r), r, st.SOFT_ORANGE)
    blf.size(0, size)
    text = it.label
    room = x1 - x0 - 30 * u
    if blf.dimensions(0, text)[0] > room:
        while text and blf.dimensions(0, text + "…")[0] > room:
            text = text[:-1]
        text = text.rstrip() + "…"
    blf.color(0, 1, 1, 1, 0.95 if hot else 0.85)
    blf.position(0, x0 + 21 * u, cy - size * 0.36, 0)
    blf.draw(0, text)


def _draw_reach(context, rect, u, size, hot, first, last):
    """The reach of an edit as a tile: the falloff over the frames either side,
    filled as high as Intensity, and the two numbers. Drag it: sideways for
    Reach, up and down for Intensity."""
    from . import reach_widget
    x0, y0, x1, y1 = rect
    _rounded(None, rect, RADIUS * u, st.TILE_HOVER if hot else BUTTON_COLOR, left=first, right=last)
    s = context.scene.animatica
    radius, k = int(s.trail_radius), float(s.edit_strength)
    # the curve, in the tile's left part
    cx0, cx1 = x0 + 7 * u, x0 + 42 * u
    base, top = y0 + 8 * u, y1 - 7 * u
    mid = (cx0 + cx1) / 2
    pts = []
    for i in range(-16, 17):
        d = i / 16.0
        w = reach_widget._unit_share(d) if radius else (1.0 if i == 0 else 0.0)
        pts.append((mid + d * (cx1 - cx0) / 2, base + (top - base) * w * (k if i else 1.0)))
    verts, tris = [], []
    for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
        if ya > base + 0.5 or yb > base + 0.5:
            n = len(verts)       # not k: that is the Intensity, written below (it read 11600%)
            verts += [(xa, base), (xb, base), (xb, yb), (xa, ya)]
            tris += [(n, n + 1, n + 2), (n, n + 2, n + 3)]
    st.triangles(verts, tris, st.with_alpha(st.SOFT_ORANGE, 0.3))    # one batch, not 32
    st.lines(list(zip(pts, pts[1:])), max(1.0, 1.4 * u), st.with_alpha(st.SOFT_ORANGE, 0.95))
    st.lines([((mid, base), (mid, top))], max(1.0, u), (1, 1, 1, 0.6))
    st.lines([((cx0, base), (cx1, base))], max(1.0, u), (1, 1, 1, 0.25))
    # the numbers
    blf.size(0, size * 0.92)
    text = f"\u00b1{radius} \u00b7 {round(k * 100)}%" if radius else "1 frame"
    tw = blf.dimensions(0, text)[0]
    blf.color(0, *WHITE[:3], 0.9 if hot else 0.75)
    blf.position(0, cx1 + ((x1 - 6 * u) - cx1 - tw) / 2, (y0 + y1) / 2 - size * 0.34, 0)
    blf.draw(0, text)


def _draw_field(it, rect, u, size, hot, first, last, ground=None):
    """The prompt, as a text field: inset, darker than the buttons, its text
    from the left (or the placeholder, muted), cut to fit. ``ground``: the
    whole field when a button sits inside its end (the text stays in ``rect``)."""
    from .timeline_overlay import inline_edit_state as edit
    x0, y0, x1, y1 = rect
    s = bpy.context.scene.animatica
    # the one field: the block's prompt, or -- typed from its pose button -- this frame's pose
    pose = (edit["active"] and edit["index"] == POSE_FIELD) or it.op == "animatica.toolbar_pose_prompt"
    typing = edit["active"] and edit["index"] in (POSE_FIELD, block_at(s, bpy.context.scene.frame_current))
    box = ground or rect
    st.rounded(box, RADIUS * u, (0.07, 0.07, 0.075, 1.0 if it.enabled else 0.45), left=first, right=last)
    st.outline(box, RADIUS * u, max(1.0, u),
               st.with_alpha(st.SOFT_ORANGE, 0.9) if typing
               else (1, 1, 1, 0.05 if not it.enabled else 0.30 if hot else 0.12))
    if typing:
        _draw_typing(edit, rect, u, size)
        return
    text = it.label or (f"Describe the pose for frame {bpy.context.scene.frame_current}…"
                        if pose else PLACEHOLDER)
    color = WHITE if it.label else st.MUTED
    if not it.enabled:
        color = (color[0], color[1], color[2], 0.22)
    blf.size(0, size)
    pad = 10 * u
    room = (x1 - x0) - 2 * pad
    if blf.dimensions(0, text)[0] > room:
        while text and blf.dimensions(0, text + "…")[0] > room:
            text = text[:-1]
        text = text.rstrip() + "…"
    blf.color(0, *color)
    blf.position(0, x0 + pad, (y0 + y1) / 2 - size * 0.36, 0)
    blf.draw(0, text)


def _draw_typing(edit, rect, u, size):
    """The field while it is being typed in: the text, scrolled to keep the
    cursor in view, the selection, the cursor."""
    x0, y0, x1, y1 = rect
    pad = 10 * u
    room = (x1 - x0) - 2 * pad
    blf.size(0, size)
    text, cur = edit["text"], edit["cursor"]
    start = _field_trim(text, cur, room)
    shown = text[start:]
    while shown and blf.dimensions(0, shown)[0] > room:
        shown = shown[:-1]
    tx, ty = x0 + pad, (y0 + y1) / 2 - size * 0.36
    sel = edit.get("selection_start")
    if sel is not None and sel != cur:
        a, b = sorted((sel, cur))
        a, b = max(a, start) - start, min(b, start + len(shown)) - start
        if b > a:
            sx0 = tx + blf.dimensions(0, shown[:a])[0]
            sx1 = tx + blf.dimensions(0, shown[:b])[0]
            st.rounded((sx0, y0 + 6 * u, sx1, y1 - 6 * u), 2 * u, st.with_alpha(st.MULBERRY, 0.9))
    blf.color(0, *WHITE)
    blf.position(0, tx, ty, 0)
    blf.draw(0, shown)
    cx = tx + blf.dimensions(0, shown[:max(0, cur - start)])[0]
    st.lines([((cx, y0 + 7 * u), (cx, y1 - 7 * u))], max(1.0, 1.2 * u), st.SOFT_ORANGE)


# ---------------------------------------------------------------------------
# Clicks, hover and tooltips: the bar is one gizmo that knows its buttons
# ---------------------------------------------------------------------------
#
# The buttons used to be Blender's round button gizmos under the drawing. Their
# hit areas were circles inside rectangles (a wide button needed a row of
# them), so clicks had to be precise, and on Windows their own hover highlight
# flashed through. The bar now answers for every button's whole rectangle.

#: the button under the mouse, per area pointer
_hover: dict = {}
MAX_PARTS = 48


def hovered(area) -> str:
    return _hover.get(area.as_pointer(), "") if area is not None else ""


class ANIMATICA_GT_toolbar_bar(bpy.types.Gizmo):
    """The bar: draws itself, and says which button is under the mouse."""
    bl_idname = "ANIMATICA_GT_toolbar_bar"

    def draw(self, context):
        draw_bar(context, bool(self.is_highlight))

    def test_select(self, context, location):
        area, region = context.area, context.region
        hit = ""
        index = -1
        try:
            _bar, rects, _d = layout(context, area, region)
            x, y = location
            for i, (it, (x0, y0, x1, y1)) in enumerate(rects):
                # the gaps between segments count for the button on their left:
                # a click on a hairline should not fall through to the viewport
                if x0 <= x <= x1 + GAP * _ui() and y0 <= y <= y1 and it.op:
                    hit, index = it.id, min(i, MAX_PARTS - 1)
                    break
        except Exception:                       # noqa: BLE001
            pass
        if _hover.get(area.as_pointer()) != hit:
            _hover[area.as_pointer()] = hit
            area.tag_redraw()
        return index


class ANIMATICA_GGT_toolbar(bpy.types.GizmoGroup):
    bl_idname = "ANIMATICA_GGT_toolbar"
    bl_label = "Animatica Toolbar"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        return shown(context)

    def setup(self, context):
        self.bar = self.gizmos.new(ANIMATICA_GT_toolbar_bar.bl_idname)
        # one part per button position: a part without an operator ignores the click,
        # and a part of its own per button is what makes the tooltip follow the mouse
        for part in range(MAX_PARTS):
            self.bar.target_set_operator("animatica.bar_click", index=part)
        self.bar.use_tooltip = True
        self.bar.use_draw_hover = False


#: what each button does: a title, then a line on it (the tooltip)
TIPS = {
    "autopose": ("Autopose", "Drag a hand, a foot, the hips or the head to pose the whole body. Nothing is added to your rig, and you don't have to pose bone by bone"),
    "picker": ("Handle Picker", "Your character with its handles. Pick a handle, switch it on or off, or set its slack. A handle that is on holds its joint in place while you pose the rest"),
    "key_prev": ("Previous Key Pose", "Jump to your previous key pose before the playhead. Keys inside the take are skipped (Down Arrow stops on every key)"),
    "set_key": ("Set Key Pose  (I)", "Key the whole pose at this frame. Generate passes through every key pose, so key the moments that matter, like a foot contact, a peak or a landing, and the model fills in between"),
    "key_next": ("Next Key Pose", "Jump to your next key pose after the playhead. Keys inside the take are skipped (Up Arrow stops on every key)"),
    "auto_key": ("Auto Keying", "Blender's record button. When it is on, every pose you make is keyed. When it is off, press I to key the poses you want to keep"),
    "onion": ("Onion Skin", "Show the motion around this frame as ghosts, green before and blue after, so you can check that a key pose fits what comes before and after it. Set the mode, opacity and colours in Pose Options"),
    "reach": ("Reach \u00b7 Intensity", "How far an edit spreads to the frames around it, and how strongly they follow. Drag sideways to set Reach (frames on either side) and up or down to set Intensity. A wide reach makes a smooth change through the motion, a narrow one makes a local fix. The ghosts and the Timeline update as you drag, and your next edit uses the new values"),
    "wormhole": ("Zoetrope", "Spread the onion skin out in space, earlier frames to the left and later ones to the right. Frames that overlap become readable side by side, and you can pose each one without moving the playhead"),
    "trail": ("Motion Trail", "Show the path of the hands, feet, hips and head. Good motion moves in arcs, and the trail shows where it doesn't. Click a point on it and drag, and the frames around it follow"),
    "lock": ("Lock in Place", "Hold a hand or foot on one spot for a stretch of frames, so a planted foot stops sliding. Click this, then click the foot. The frames start as the contact around the playhead; move the mouse to set the end (Ctrl: the start), then click. A new take over those frames is locked again"),
    "smooth": ("Smooth Motion Here", "Even out the picked joint's path around the picked point. Use it when a generated take wobbles. It fixes the arc without making a new take"),
    "waypoint": ("Add Waypoint", "Mark where the character should be at this frame. A prompt can say \u201cto the door\u201d, but only a waypoint says where the door is"),
    "pin": ("Pin a Hand or Foot", "Hold a hand or foot to a target, like a rail or a door handle, so the contact stays put instead of sliding"),
    "prompt": ("Prompt", "What happens in the block under the playhead (a block is a stretch of the timeline with its own prompt). Click and type. One action per block, like \u201cwalks to the door\u201d then \u201csits\u201d, is followed more closely than several in one"),
    "generate": ("Generate", "Make the take, the generated motion. It passes through your key poses, follows your prompts and ends at your waypoints"),
    "working": ("Cancel", "Stop generating the take"),
    "redo": ("Redo", "Make the block under the playhead again (Shift: the whole take). The new take still passes through your key poses. To keep a good moment, use Key This Frame first"),
    "reject": ("Discard", "Throw this take away and go back to what you had. Your key poses and prompts stay. There is no Accept button: a take is kept as soon as you start fine-tuning it"),
    "var_prev": ("Previous Version", "Show the previous version of the take"),
    "var_next": ("Next Version", "Show the next version of the take"),
    "options": ("Take Options", "Set Loop, In Place, versions and the Reach of a drag. With versions, one Generate gives you several takes to choose from"),
    "add_char": ("Add a Character", "Add a ready-made rigged character to animate"),
    "use_rig": ("Use Selected Rig", "Animate the armature you have selected (or the one its mesh is bound to)"),
    "examples": ("Examples", "Open an example scene, ready to Generate"),
    "loop": ("Loop", "Make the next take a seamless cycle: its last frame runs into its first and it repeats. Use it for a walk, run or idle in a game. Edits keep it a loop, and the zoetrope shows it as a ring"),
    "pose_text": ("Pose This Frame in Words", "When on, describe this frame's pose in the field and Generate Pose makes it, keyed at the playhead. When off, the field goes back to the take's prompt and Generate. A pose you can describe is often quicker to type than to pose"),
    "pose_prompt": ("Describe a Pose", "Describe a pose in words to key it at the playhead. Click and type, then press Enter. It gives you a starting pose to refine with the handles"),
    "generate_pose": ("Generate Pose", "Make the pose you described and key it at the playhead. You can then refine it with the handles"),
    "keep": ("Key This Frame", "Keep the take's pose at this frame as a key pose. The next Redo passes through it, so the moment stays when you retry"),
}


def tip(context, it) -> str:
    """The tooltip for a button: its title, then what it does here and now."""
    from . import mmcp_client
    from .autoposer import poser
    s = context.scene.animatica
    title, line = TIPS.get(it.id, (it.label or it.id.replace("_", " ").title(), ""))
    if it.id == "autopose":
        arm = properties_live(s)
        from . import handles
        if arm is not None and handles.tool_active(context):
            title, line = "Autopose (on)", "Go back to Blender's Select tool. The pose and keys stay"
    elif it.id == "loop" and not it.enabled:
        line = "A loop is a single block, because the model makes one block as a cycle. Remove or merge the other blocks first"
    elif it.id == "generate_pose":
        why = _describe_blocker(context)
        if why:
            line = why
        else:
            line = (f"Make \u201c{(s.last_pose_prompt or '').strip()}\u201d and key it at frame "
                    f"{context.scene.frame_current}" if (s.last_pose_prompt or "").strip()
                    else "Describe the pose in the field first")
    elif it.id == "pose_prompt" and it.label:
        line = f"\u201c{it.label}\u201d. Click to change it, then press Enter to make it"
    elif it.id == "pose_prompt" and not it.enabled:
        line = _describe_blocker(context) or line
    elif it.id == "options":
        title, line = "Options", ("The next take (Loop, In Place, versions), the onion skin and zoetrope, "
                                  "and whether a described pose stands on the ground")
    elif it.id in ("auto_key", "onion", "trail", "wormhole"):
        title += " (on)" if it.on else " (off)"
    elif it.id == "picker":
        title += " (open)" if s.show_picker else ""
    elif it.id == "prompt":
        text = it.label
        line = (f"\u201c{text}\u201d. Click to change it. One action per block is followed more closely "
                "than several in one" if text
                else "Say what happens in the block under the playhead. Click and type")
    elif it.id == "smooth" and not it.enabled:
        line = "Click a point on the motion trail first. This then evens out the motion around it"
    elif it.id == "keep" and not it.enabled:
        line = "This frame is already a key pose"
    elif it.id == "waypoint" and it.badge:
        line = _waypoint_hint(context) + ". " + line
    elif it.id == "accept":
        from . import key_poses
        n = len(key_poses.take_keys(context.scene))
        line = f"Keep this take. It passes through your {n} key pose{'s' if n != 1 else ''}" if n else line
    elif it.id in ("generate", "connect"):
        kind, text, _op = gate(context)
        from . import key_poses
        n = len(key_poses.take_keys(context.scene))
        if kind == "ready":
            line = (f"Make the take. It passes through your {n} key pose{'s' if n != 1 else ''}"
                    if n else "There are no key poses yet, so the model decides every pose. Press I with the Autopose tool to key one")
            hint = _waypoint_hint(context)
            if hint:
                line += "\n" + hint
        if kind == "blocked":
            title, line = "Generate", text
        elif kind == "model":
            title, line = "Pick a Model", blockers(context)[0]
        elif kind == "sign_in":
            title, line = "Sign in to Generate", "Sign in to your Animatica account to make the take"
        elif kind == "offline":
            title, line = "Allow Online Access", "Animatica makes the motion on its servers. This lets Blender connect to them"
        elif kind == "connect":
            err = mmcp_client.last_connection_error()
            if err and not mmcp_client.connecting():
                title, line = "Can't Reach Animatica", ("Check your internet connection, then click to try "
                                                       "again. You can still add a character and pose it")
            else:
                title, line = "Connecting", "Connecting to Animatica to list the models"
    elif it.id == "hint":
        from . import guidance
        h = guidance.next_step(context)
        title, line = "Next step", (h["why"] if h else "")
        if it.op == "animatica.hint_why":
            line += "\nClick to show the reason in the status bar"
        else:
            line += "\nClick to do it"
    elif it.id == "hint_close":
        title, line = "Hide this hint", "Hide it for the rest of the session. Turn hints on or off in Options"
    elif it.id == "model":
        title = "Model"
        line = f"{s.model_id} on {mmcp_client.get_mmcp_url()}. Click to pick another model or change the server"
    elif it.id == "fetching":
        title, line = "Fetching the Character", "Downloading the character. This happens once, and it is kept for next time"
    if not it.enabled and it.id in ("waypoint", "pin", "prompt", "pose_text", "pose_prompt") \
            and not mmcp_client.tool_available(s.model_id, _TOOL_OF.get(it.id, it.id)):
        line = f"The model {s.model_id} can't use this. Pick another model on the bar to use it"
    elif not it.enabled and it.id == "use_rig":
        line = "Select an armature (or a mesh bound to one) in the viewport first"
    return f"{title}\n{line}" if line else title


#: the model tool behind a bar button, where the names differ
_TOOL_OF = {"pose_text": "describe", "pose_prompt": "describe"}


_PLACES = (" to the ", " towards ", " toward ", " into the ", " onto ", " up to ", " over to ", " across ")


def _waypoint_hint(context) -> str:
    """A prompt that names a place, and no waypoint to say where it is."""
    from . import waypoints
    s = context.scene.animatica
    text = " " + " ".join((b.prompt or "").lower() for b in s.prompt_blocks) + " "
    if any(p in text for p in _PLACES) and not list(waypoints.waypoints(context.scene)):
        return "The prompt names a place. Add a Waypoint to say where it is"
    return ""


def properties_live(s):
    from . import properties
    return properties._live_armature(s.target_armature)


def _item(context, ident):
    return next((it for it in items(context) if it.id == ident), None)


class ANIMATICA_OT_bar_click(bpy.types.Operator):
    """A button on the floating bar"""
    bl_idname = "animatica.bar_click"
    bl_label = "Animatica"
    bl_options = {'INTERNAL', 'UNDO'}  # what the click ran is one undo step: a nested operator records none

    @classmethod
    def description(cls, context, properties):
        it = _item(context, hovered(context.area))
        return tip(context, it) if it is not None else ""

    def invoke(self, context, event):
        it = _item(context, hovered(context.area))
        if it is None:
            return {'CANCELLED'}
        if not it.enabled:
            # a greyed button clicked did nothing, without a word: now it says why,
            # and a Generate waiting for a prompt opens the field for it
            from .request_builder import MODEL_RIG_MISMATCH  # noqa: F401
            if it.id == "generate" and gate(context)[1] == "Type a prompt first":
                bpy.ops.animatica.toolbar_prompt_here('INVOKE_DEFAULT')
                return {'CANCELLED'}
            why = tip(context, it).partition("\n")[2].split("\n")[0]
            if why:
                self.report({'INFO'}, why)
            return {'CANCELLED'}
        if not it.op:
            return {'CANCELLED'}
        group, name = it.op.split(".", 1)
        try:
            result = getattr(getattr(bpy.ops, group), name)('INVOKE_DEFAULT', **it.props)
        except RuntimeError as exc:
            # the button's own error report, raised by bpy.ops: shown as what it
            # says, not as a Python traceback in the status bar
            text = str(exc).strip().splitlines()[-1]
            self.report({'ERROR'}, text.removeprefix("Error: ").strip() or "That didn't work")
            return {'CANCELLED'}
        return {'CANCELLED'} if result == {'CANCELLED'} else {'FINISHED'}


# ---------------------------------------------------------------------------
# The operators the bar needs that the add-on did not have
# ---------------------------------------------------------------------------

_TOGGLE_TIPS = {
    "loop": "Make the next take a seamless cycle, its last frame running into its first. "
            "A loop is a single block. Walk and run cycles work best at two to four seconds. "
            "Edits keep it a loop, and the zoetrope turns into a ring",
    "show_picker": "Show the character in T-pose with the Autoposer's handles on it. "
                   "Pick handles, switch them on or off, set their slack, or add and remove them",
    "auto_key_pose": "Key the pose as you pose it. When off, only Set Key adds a key",
    "key_pose_overlay": "Show the key poses, the trail and the frame numbers in the viewport. "
                        "Turn it off to judge the motion on its own",
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
        return why[0] if why else "Make the take, motion from your prompts, key poses and waypoints"

    def invoke(self, context, event):
        # the take being judged is kept first: Generate makes the rest
        from .operators import keep_take
        keep_take(context)
        return bpy.ops.animatica.generate('INVOKE_DEFAULT')


class ANIMATICA_OT_toolbar_redo(bpy.types.Operator):
    """Make the block under the playhead again. Hold Shift to redo the whole take"""
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
    """The model that makes the takes. Click to pick another model or change the server"""
    bl_idname = "animatica.toolbar_model"
    bl_label = "Model"
    bl_options = {'INTERNAL'}

    @classmethod
    def description(cls, context, properties):
        from . import mmcp_client
        s = context.scene.animatica
        return (f"Model: {s.model_id}, on {mmcp_client.get_mmcp_url()}.\n"
                "Click to pick another model or change the server")

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


_MENU_TIPS = {
    "ANIMATICA_MT_examples": "Open an example scene with a character, prompts and poses, ready to Generate",
    "ANIMATICA_PT_options": "Settings for the next take, how far an edit spreads, the onion skin and the trail",
}


class ANIMATICA_OT_toolbar_menu(bpy.types.Operator):
    """Open a menu from the bar"""
    bl_idname = "animatica.toolbar_menu"
    bl_label = "Menu"
    bl_options = {'INTERNAL'}

    menu: StringProperty(options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        return _MENU_TIPS.get(properties.menu, "")

    def invoke(self, context, event):
        # opened when the click is over: the bar acts on the press, and a menu
        # opened then closed again on the release
        if event.value == 'PRESS':
            context.window_manager.modal_handler_add(self)
            return {'RUNNING_MODAL'}
        return self._open()

    def modal(self, context, event):
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return self._open()
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _open(self):
        if "_PT_" in self.menu:
            bpy.ops.wm.call_panel(name=self.menu, keep_open=True)
        else:
            bpy.ops.wm.call_menu(name=self.menu)
        return {'FINISHED'}


class _Popover:
    """Settings opened from the bar: a popover, as Blender's Overlays are --
    a menu lays rows out in columns, and a switch in one printed its words
    over each other."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_ui_units_x = 13


class ANIMATICA_PT_options(_Popover, bpy.types.Panel):
    bl_idname = "ANIMATICA_PT_options"
    bl_label = "Options"

    def draw(self, context):
        from . import mmcp_client
        s = context.scene.animatica
        layout = self.layout

        def column():
            col = layout.column()
            col.use_property_split = True
            col.use_property_decorate = False
            return col
        # the next take
        layout.label(text="Next Take", icon='PLAY')
        col = column()
        model = mmcp_client.cached_model(s.model_id) or {}
        if model.get("supports_loop"):
            row = col.row()
            row.active = len(s.prompt_blocks) == 1          # a loop is one block
            row.prop(s, "loop", text="Loop" if row.active else "Loop (one block)")
        col.prop(s, "inplace", text="In Place")
        if int((model.get("limits") or {}).get("max_num_samples") or 1) > 1:
            col.prop(s, "variations", text="Versions")
        # fine-tuning
        layout.separator()
        layout.label(text="Edits", icon='POSE_HLT')
        col = column()
        col.prop(s, "trail_radius", text="Reach (frames)")
        col.prop(s, "edit_strength", text="Intensity", slider=True)
        col.prop(s, "pose_on_ground", text="Described Pose on Ground")
        # seeing the motion
        layout.separator()
        layout.label(text="Onion Skin", icon='ONIONSKIN_ON')
        layout.row().prop(s, "onion_mode", expand=True)
        col = column()
        if s.onion_mode == 'FRAMES':
            # the frames an edit reaches: Reach is its range
            wormhole = s.onion_wormhole
            col.prop(s, "wormhole_step" if wormhole else "onion_step", text="Step")
            col.prop(s, "onion_wormhole", text="Zoetrope")
            if wormhole:
                col.prop(s, "wormhole_spacing", text="Spacing")
        else:
            sub = col.column(align=True)
            sub.prop(s, "onion_before", text="Before")
            sub.prop(s, "onion_after", text="After")
        col.prop(s, "onion_opacity", text="Opacity", slider=True)
        col.prop(s, "onion_fade", text="Fade")
        col.prop(s, "onion_color_before", text="Before")
        col.prop(s, "onion_color_after", text="After")
        col.prop(s, "key_pose_trail", text="Motion Trail")
        layout.separator()
        layout.prop(s, "show_hints", text="Next-Step Hints")
        _draw_folded(layout, context)
        layout.separator()
        row = layout.row()
        row.active = False
        row.label(text="More: sidebar (N) \u2192 Animatica")


class ANIMATICA_OT_toolbar_wormhole(bpy.types.Operator):
    """Spread the onion skin out in space so you can see and pose each frame"""
    bl_idname = "animatica.toolbar_wormhole"
    bl_label = "Zoetrope"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        s = context.scene.animatica
        on = not (s.onion_wormhole and s.key_pose_ghosts and s.onion_mode == 'FRAMES')
        s.onion_wormhole = on
        if on:
            # it is a way of showing the Frames onion skin: that, switched on
            s.onion_mode = 'FRAMES'
            s.key_pose_ghosts = True
            s.key_pose_overlay = True
        _redraw_all(context)
        return {'FINISHED'}


class ANIMATICA_OT_toolbar_autokey(bpy.types.Operator):
    """Turn Blender's Auto Keying on or off. When on, every pose you make is keyed"""
    bl_idname = "animatica.toolbar_autokey"
    bl_label = "Auto Keying"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        ts = context.scene.tool_settings
        ts.use_keyframe_insert_auto = not ts.use_keyframe_insert_auto
        for a in context.screen.areas:
            a.tag_redraw()                   # the Timeline's own record button too
        return {'FINISHED'}


class ANIMATICA_OT_toolbar_overlay(bpy.types.Operator):
    """Show or hide the onion skins or the motion trail in the viewport"""
    bl_idname = "animatica.toolbar_overlay"
    bl_label = "Show"
    bl_options = {'INTERNAL'}

    part: bpy.props.EnumProperty(items=(('GHOSTS', "Onion Skin", ""), ('TRAIL', "Motion Trail", "")),
                                 options={'HIDDEN'})

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        from . import key_poses
        s = context.scene.animatica
        on = key_poses.ghosts_on(s) if self.part == 'GHOSTS' else key_poses.trail_on(s)
        if on:
            setattr(s, "key_pose_ghosts" if self.part == 'GHOSTS' else "key_pose_trail", False)
        else:
            # switching a part on switches the whole drawing on, if it was off
            setattr(s, "key_pose_ghosts" if self.part == 'GHOSTS' else "key_pose_trail", True)
            s.key_pose_overlay = True
        _redraw_all(context)
        return {'FINISHED'}


class ANIMATICA_OT_toolbar_pose_prompt(bpy.types.Operator):
    bl_idname = "animatica.toolbar_pose_prompt"
    bl_label = "Describe a Pose"
    bl_description = "Describe a pose in words to key it at the playhead. Click and type, then press Enter"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return bpy.ops.animatica.bar_prompt_edit('INVOKE_DEFAULT', index=POSE_FIELD)


class ANIMATICA_OT_toolbar_field_pose(bpy.types.Operator):
    bl_idname = "animatica.toolbar_field_pose"
    bl_label = "Pose This Frame in Words"
    bl_description = ("Use the field to describe this frame's pose. Generate becomes Generate Pose. "
                      "Click again to go back to the take")
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        s = context.scene.animatica
        s.field_pose = not s.field_pose
        _redraw_all(context)
        if s.field_pose:
            # straight to typing it
            return bpy.ops.animatica.bar_prompt_edit('INVOKE_DEFAULT', index=POSE_FIELD)
        from .timeline_overlay import inline_edit_state as edit
        if edit["active"] and edit["index"] == POSE_FIELD:
            edit["active"] = False
        return {'FINISHED'}


class ANIMATICA_OT_toolbar_pose_generate(bpy.types.Operator):
    bl_idname = "animatica.toolbar_pose_generate"
    bl_label = "Generate Pose"
    bl_description = "Make the pose you described and key it at the playhead"
    bl_options = {'INTERNAL'}

    #: the last pose made, so asking again for the same one gives another
    _last = {"prompt": None, "frame": None}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        import random
        s = context.scene.animatica
        kind, label, op = gate(context)
        if kind in ("offline", "connect", "sign_in"):
            # the step that is missing first, as the bar's own button would
            self.report({'INFO'}, label)
            group, name = ("animatica.signin" if kind == "sign_in" else op).split(".", 1)
            getattr(getattr(bpy.ops, group), name)('INVOKE_DEFAULT')
            return {'CANCELLED'}
        why = _describe_blocker(context)
        if why:
            self.report({'WARNING'}, why)
            return {'CANCELLED'}
        text = (s.last_pose_prompt or "").strip()
        if not text:
            # nothing to make yet: put the cursor where it goes
            return bpy.ops.animatica.bar_prompt_edit('INVOKE_DEFAULT', index=POSE_FIELD)
        frame = context.scene.frame_current
        _pose_job["on"] = True                     # the bar stays in Pose while it works
        again = self._last == {"prompt": text, "frame": frame}
        type(self)._last = {"prompt": text, "frame": frame}
        seed = random.randint(1, 999999) if again else int(s.seed)
        return bpy.ops.animatica.generate_pose('EXEC_DEFAULT', prompt=text, seed=seed,
                                               on_ground=bool(s.pose_on_ground))


class ANIMATICA_OT_use_selected_rig(bpy.types.Operator):
    """Animate the rig you have selected (an armature, or a character mesh bound to one)"""
    bl_idname = "animatica.use_selected_rig"
    bl_label = "Use Selected Rig"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        rig = _selected_rig(context)
        if rig is None:
            self.report({'WARNING'}, "Select your character's armature (or its mesh) first")
            return {'CANCELLED'}
        context.scene.animatica.target_armature = rig
        self.report({'INFO'}, f"Animating {rig.name}")
        return {'FINISHED'}


def _describe_blocker(context) -> str:
    """Why Describe cannot run here, in the artist's words, or ''."""
    from . import mmcp_client
    s = context.scene.animatica
    caps = mmcp_client.cached_model(s.model_id) if s.model_id else None
    if caps is None:
        return "Connect to the server first"
    if "pose" not in (caps.get("supported_segments") or []):
        return (f"The model '{s.model_id}' on {mmcp_client.get_mmcp_url()} can't make a pose "
                f"from words. Pick a model or server that can")
    if bpy.ops.animatica.generate_pose.poll():
        return ""
    return "Not available right now"


class ANIMATICA_OT_toolbar_autoposer(bpy.types.Operator):
    """Drag a hand, a foot or the hips to pose the whole body. Click again to stop"""
    bl_idname = "animatica.toolbar_autoposer"
    bl_label = "Autopose Tool"
    bl_options = {'INTERNAL', 'UNDO'}

    _download = False

    def invoke(self, context, event):
        from . import properties
        from .autoposer import engine, poser

        arm = properties._live_armature(context.scene.animatica.target_armature)
        if arm is None:
            return {'CANCELLED'}
        from . import handles
        if handles.tool_active(context):
            # off: back to Blender's own select tool; the handles stay for next time
            handles.deactivate(context)
            return {'FINISHED'}
        status = engine.status()
        if not (status["runtime"] and status["model"]):
            # Its download comes with its first use, asked once, rather than
            # as a big button in the sidebar that read like a step to do first.
            if engine.fetch_state()["running"] or engine.install_state()["running"]:
                self.report({'INFO'}, f"Downloading the Autoposer… {engine.fetch_percent():.0f}%")
                return {'CANCELLED'}
            if not engine.online():
                self.report({'WARNING'}, engine.offline_message())
                return {'CANCELLED'}
            self._download = True
            return context.window_manager.invoke_confirm(
                self, event, title="Download the Autoposer?",
                message="It runs on your computer, so it is downloaded once (about 225 MB). "
                        "Click the Autoposer again when it is done.",
                confirm_text="Download", icon='INFO')  # invoke_confirm takes NONE, WARNING, QUESTION, ERROR or INFO only
        cleaned = False
        why = handles.activate(context)
        if why:
            self.report({'WARNING'}, why)
            return {'CANCELLED'}
        keyed = context.scene.tool_settings.use_keyframe_insert_auto
        self.report({'INFO'}, "Autopose: drag a hand, a foot, the hips or the head"
                    + (". Auto Keying keys each pose" if keyed else ". Press I to key the pose"))
        return {'FINISHED'}

    def execute(self, context):
        if self._download:
            return bpy.ops.autoposer.download()
        return {'CANCELLED'}


class ANIMATICA_OT_toolbar_prompt_here(bpy.types.Operator):
    bl_idname = "animatica.toolbar_prompt_here"
    bl_label = "What Happens Here"
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def description(cls, context, properties):
        s = context.scene.animatica
        here = block_at(s, context.scene.frame_current)
        text = (s.prompt_blocks[here].prompt or "").strip() if here >= 0 else ""
        if text:
            return f"\u201c{text}\u201d. Click to change what happens in this block"
        return "Say what happens here. Type the prompt for the block under the playhead, or start a new block here"

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
        # typed right in the field, as in any text box
        bpy.ops.animatica.bar_prompt_edit('INVOKE_DEFAULT', index=i)
        return {'FINISHED'}


def _field_rect(context):
    """The prompt field's rect on the bar in this region, or None."""
    try:
        _bar, rects, _d = layout(context, context.area, context.region)
    except Exception:                           # noqa: BLE001
        return None
    return next((r for it, r in rects if it.width == "field"), None)


def _item_at(context, x, y):
    """The bar's button under ``(x, y)`` (region pixels), or None."""
    try:
        _bar, rects, _d = layout(context, context.area, context.region)
    except Exception:                           # noqa: BLE001
        return None
    for it, (x0, y0, x1, y1) in rects:
        if x0 <= x <= x1 and y0 <= y <= y1:
            return it
    return None


from .timeline_operators import InlinePromptEditing  # noqa: E402


class ANIMATICA_OT_bar_prompt_edit(InlinePromptEditing, bpy.types.Operator):
    """Type the prompt in the bar's field. It edits the same block as the Timeline,
    from the 3D view"""
    bl_idname = "animatica.bar_prompt_edit"
    bl_label = "Type the Prompt"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    index: bpy.props.IntProperty(name="Strip Index", default=0)

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D'

    def invoke(self, context, event):
        if self.index != POSE_FIELD:
            return super().invoke(context, event)
        from .timeline_overlay import inline_edit_state as st_
        text = context.scene.animatica.last_pose_prompt or ""
        st_.update(active=True, index=POSE_FIELD, text=text, cursor=len(text), original=text,
                   selection_start=None)
        context.window_manager.modal_handler_add(self)
        _redraw_all(context)
        return {"RUNNING_MODAL"}

    def _commit(self, context):
        if self.index != POSE_FIELD:
            return super()._commit(context)
        from .timeline_overlay import inline_edit_state as st_
        context.scene.animatica.last_pose_prompt = st_["text"].strip()
        st_["active"] = False
        _redraw_all(context)

    #: the mouse moving, the wheel, the view turning: not typing, so not ours --
    #: swallowed, the bar never saw the mouse reach the button clicked next
    _PASS = {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE',
             'TRACKPADPAN', 'TRACKPADZOOM', 'MOUSEROTATE', 'TIMER', 'TIMER_REPORT', 'WINDOW_DEACTIVATE'}

    def modal(self, context, event):
        if event.type in self._PASS:
            return {"PASS_THROUGH"}
        if (self.index == POSE_FIELD and event.type in {"RET", "NUMPAD_ENTER"}
                and event.value == "PRESS"):
            # Enter makes the pose described
            self._commit(context)
            bpy.ops.animatica.toolbar_pose_generate('INVOKE_DEFAULT')
            return {"FINISHED"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            rect = _field_rect(context)
            inside = rect is not None and rect[0] <= event.mouse_region_x <= rect[2] \
                and rect[1] <= event.mouse_region_y <= rect[3]
            if not inside:
                # keep what was typed, and let the click do what it was for
                # (Generate straight after typing generates). The bar's own
                # buttons do not see a click while this has the keyboard, so
                # one clicked is run from here.
                self._commit(context)
                it = _item_at(context, event.mouse_region_x, event.mouse_region_y)
                if it is not None and it.op and it.enabled:
                    group, name = it.op.split(".", 1)
                    getattr(getattr(bpy.ops, group), name)('INVOKE_DEFAULT', **it.props)
                    return {"FINISHED"}
                return {"FINISHED", "PASS_THROUGH"}
            self._place_cursor(event.mouse_region_x - rect[0])
            _redraw_all(context)
            return {"RUNNING_MODAL"}
        result = super().modal(context, event)
        _redraw_all(context)
        return result

    def _place_cursor(self, x):
        from .timeline_overlay import inline_edit_state as st_
        u = _ui()
        blf.size(0, TEXT_SIZE * u)
        text = st_["text"]
        start = _field_trim(text, st_["cursor"])
        x -= 10 * u
        best, pos = None, start
        for k in range(start, len(text) + 1):
            d = abs(blf.dimensions(0, text[start:k])[0] - x)
            if best is None or d < best:
                best, pos = d, k
        st_["cursor"] = pos
        st_["selection_start"] = None

    def _cancel(self, context):
        super()._cancel(context)
        _redraw_all(context)


def _redraw_all(context):
    for a in context.screen.areas:
        if a.type in {'VIEW_3D', 'DOPESHEET_EDITOR'}:
            a.tag_redraw()


#: how far the field's text is scrolled, so the cursor stays in view
_scroll = {"start": 0}


def _field_trim(text, cursor, room=None):
    """The first character shown in the field: scrolled only as far as the
    cursor needs."""
    start = min(_scroll["start"], cursor)
    if room is not None:
        while start < cursor and blf.dimensions(0, text[start:cursor])[0] > room:
            start += 1
    _scroll["start"] = start
    return start


class ANIMATICA_PT_overlay(bpy.types.Panel):
    """Blender's Overlays popover: where a viewport drawing is switched off,
    so the add-on's are there too -- its own section, after Blender's."""
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'HEADER'
    bl_parent_id = "VIEW3D_PT_overlay"
    bl_label = "Animatica"
    bl_order = 100

    @classmethod
    def poll(cls, context):
        from . import properties
        s = getattr(context.scene, "animatica", None)
        return s is not None and properties._live_armature(s.target_armature) is not None

    def draw_header(self, context):
        self.layout.prop(context.scene.animatica, "key_pose_overlay", text="")

    def draw(self, context):
        s = context.scene.animatica
        layout = self.layout
        layout.active = s.key_pose_overlay
        col = layout.column()
        col.prop(s, "key_pose_trail", text="Motion Trail")
        col.prop(s, "key_pose_ghosts", text="Onion Skin (Pose)")
        onion = layout.column()
        onion.active = s.key_pose_overlay and s.key_pose_ghosts
        onion.row().prop(s, "onion_mode", expand=True)
        split = onion.column()
        split.use_property_split = True
        split.use_property_decorate = False
        if s.onion_mode in {'FRAMES', 'KEYFRAMES'}:
            sub = split.column(align=True)
            sub.prop(s, "onion_before", text="Before")
            sub.prop(s, "onion_after", text="After")
        if s.onion_mode == 'FRAMES':
            split.prop(s, "onion_step", text="Step")
            split.prop(s, "onion_wormhole", text="Zoetrope")
        split.prop(s, "onion_opacity", text="Opacity", slider=True)
        split.prop(s, "onion_fade", text="Fade")
        split.prop(s, "onion_color_before", text="Before")
        split.prop(s, "onion_color_after", text="After")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (ANIMATICA_OT_toolbar_toggle, ANIMATICA_OT_toolbar_generate, ANIMATICA_OT_toolbar_redo,
            ANIMATICA_OT_toolbar_autoposer, ANIMATICA_OT_toolbar_pose_prompt,
ANIMATICA_OT_toolbar_autokey, ANIMATICA_OT_toolbar_overlay,
            ANIMATICA_OT_toolbar_wormhole, ANIMATICA_PT_overlay,
            ANIMATICA_OT_toolbar_pose_generate, ANIMATICA_OT_toolbar_field_pose, ANIMATICA_PT_options,
            ANIMATICA_MT_toolbar_models, ANIMATICA_OT_toolbar_model,
            ANIMATICA_OT_toolbar_menu, ANIMATICA_OT_use_selected_rig,
            ANIMATICA_OT_toolbar_prompt_here, ANIMATICA_OT_bar_prompt_edit, ANIMATICA_OT_bar_click,
            ANIMATICA_GT_toolbar_bar, ANIMATICA_GGT_toolbar)
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


_keymaps: list = []


def unregister():
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError):
            pass
    _keymaps.clear()
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _live.clear()
    st.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
