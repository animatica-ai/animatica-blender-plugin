# SPDX-License-Identifier: GPL-3.0-or-later
"""The zoetrope: the onion skin spread out in space, and posed where it lies.

An onion skin draws each pose where the character was at that frame, so a
take that stays in place -- a punch, a turn, a crouch -- piles every slice on
the body and none can be read, let alone grabbed. The zoetrope moves each
slice out along a time axis: the past one way, the future the other, both
receding into depth, so the take reads as a tunnel through the frame you are
on.

Each slice carries handles on the joints the Autoposer is steered by (hands,
feet, hips, head). Drag one and that frame's body is solved around it -- the
other joints of *that* frame hold -- and the pose is keyed at that frame. The
playhead does not move: the frame you are working from stays the frame you
see.

Slices and their joints come from key_poses' onion cache (Frames mode); this
module only decides where each slice is drawn, and turns a drag on one back
into the real world.
"""

from __future__ import annotations

import math

import blf
import bpy
import gpu
from bpy.props import IntProperty
from bpy_extras import view3d_utils
from mathutils import Vector

from . import ui_style as st

MAX_PARTS = 96
HANDLE_R = 4.5             # logical px
HIT = 5.0
DEPTH = 0.6                # of the spacing, into the view per slice


def _label(context, bone) -> str:
    """A handle's name as the viewport's handles say it: \u201cL hand\u201d."""
    try:
        from . import curve_edit, key_poses
        from .autoposer import poser
        arm = key_poses._target(_settings(context))
        return poser.control_label(curve_edit._canonical(arm, bone))
    except Exception:                                   # noqa: BLE001
        return bone.rsplit(":", 1)[-1]


def _settings(context):
    return getattr(context.scene, "animatica", None)


def _rv3d(context):
    rv3d = getattr(context, "region_data", None)
    if rv3d is None:
        rv3d = getattr(getattr(context, "space_data", None), "region_3d", None)
    return rv3d


def shown(context) -> bool:
    """The zoetrope: the onion skins spread out into the tunnel."""
    from . import key_poses
    s = _settings(context)
    return bool(s is not None and s.onion_wormhole and key_poses.onion_wanted(context, s))


def editable(context) -> bool:
    """Whether the ghosts can be posed: the zoetrope's slices, or the plain
    onion skins (Frames) where they lie -- the same handles, offset or not."""
    from . import key_poses
    s = _settings(context)
    return bool(s is not None and key_poses.onion_wanted(context, s))


def _ranked(context, s):
    from . import key_poses
    return key_poses.onion_ranked(context.scene, s)


def _hips(entry):
    for name, p in (entry.get("joints") or {}).items():
        if name.rsplit(":", 1)[-1].lower() in {"hips", "pelvis"}:
            return Vector(p)
    pts = list((entry.get("joints") or {}).values())
    return Vector(pts[0]) if pts else None


def offsets(context, s, ranked) -> dict:
    """``{frame: world offset}`` for the slices on show: rank r goes r steps
    across the view (past to the left, future to the right, as a timeline
    reads) and |r| steps back into it. Fixed by the view alone, so slices do
    not shift while the cache fills or the take moves."""
    rv3d = _rv3d(context)
    if rv3d is None or not ranked:
        return {}
    if not s.onion_wormhole:
        return {f: Vector() for f, _r in ranked}        # plain onion skins: where they are
    at = offset_of(context, s)
    return {f: at(f) for f, _r in ranked} if at is not None else {}


def offset_of(context, s):
    """``frame -> offset`` for any frame within the reach (None past it):
    the slices' placement made continuous, so a path through every frame
    lands on each slice."""
    from . import key_poses
    rv3d = _rv3d(context)
    if rv3d is None or s is None:
        return None
    c = int(context.scene.frame_current)
    reach = max(1, int(s.trail_radius))
    step = key_poses.wormhole_step(s)
    spacing = float(s.wormhole_spacing)
    right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
    into = rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
    span = key_poses.loop_span(s)
    if span is not None:
        # a loop: the cycle round a ring, the frame you are on at the front --
        # a zoetrope's drum, seen from just above
        period = span[1] - span[0]
        slices = max(4, len(key_poses.onion_frames(context.scene, s)) + 1)
        radius = max(0.45, spacing * slices / (2 * math.pi))

        # each slice spins in place: its travel along the ground taken out, so
        # a walking cycle turns round the character instead of trailing off
        here, where = _hips_now(context, s), _hips_by_frame(s)

        def travel(frame):
            p = where(frame)
            if p is None or here is None:
                return Vector()
            d = p - here
            d.z = 0.0
            return d

        def round_at(frame):
            a = 2 * math.pi * key_poses.frames_from(frame, c, span) / period
            return right * (radius * math.sin(a)) + into * (radius * (1 - math.cos(a))) - travel(frame)
        return round_at

    def at(frame):
        d = frame - c
        if abs(d) > reach:
            return None
        r = d / step
        return right * (r * spacing) + into * (abs(r) * spacing * DEPTH)
    return at


def _hips_now(context, s):
    """The live character's hips (world), or None."""
    from . import key_poses
    from .autoposer import poser
    arm = key_poses._target(s)
    pb = poser.joint_pose_bone(arm, "Hips") if arm is not None else None
    return (arm.matrix_world @ pb.head) if pb is not None else None


def _hips_by_frame(s):
    """``frame -> hips (world)`` from what is already captured: the onion
    skin's joints, else the trail's hips."""
    from . import key_poses
    trail = key_poses._trail
    name = next((b for b in trail.get("bones") or () if b.rsplit(":", 1)[-1].lower() in {"hips", "pelvis"}), None)
    pts = trail.get("points", {}).get(name) if name else None
    index = {f: i for i, f in enumerate(trail.get("frames") or ())}

    def at(frame):
        e = key_poses.onion_entry(frame)
        if e and e.get("joints"):
            h = _hips(e)
            if h is not None:
                return h
        i = index.get(frame)
        return Vector(pts[i]) if pts and i is not None and i < len(pts) else None
    return at


def _slices(context):
    """``[(frame, rank, offset, {bone: world})]`` of the slices drawn now."""
    from . import key_poses
    s = _settings(context)
    ranked = _ranked(context, s)
    off = offsets(context, s, ranked)
    out = []
    for f, r in ranked:
        e = key_poses.onion_entry(f)
        if e is None or not e.get("joints") or f not in off:
            continue
        out.append((f, r, off[f], {n: Vector(p) for n, p in e["joints"].items()}))
    return out


#: the mouse, by area: the slice under it is the one whose handles show
_mouse: dict = {}


def _slice_under(context, slices):
    """The frame of the slice nearest the mouse (within reach), or None."""
    region, rv3d = context.region, _rv3d(context)
    m = _mouse.get(context.area.as_pointer()) if context.area else None
    if m is None or region is None or rv3d is None:
        return None
    u = context.preferences.system.ui_scale
    s = _settings(context)
    tunnel = bool(s and s.onion_wormhole)
    # the plain onion skins lie on the character: a ghost's handles show only
    # when the mouse is on one of its joints, and nearer it than any of the
    # character's own handles -- those always win
    best, best_d = None, (60 if tunnel else 22) * u
    for f, _r, off, joints in slices:
        for p in joints.values():
            co = view3d_utils.location_3d_to_region_2d(region, rv3d, p + off)
            if co is not None:
                d = math.hypot(co.x - m[0], co.y - m[1])
                if d < best_d:
                    best, best_d = f, d
    if best is not None and not tunnel and _live_handle_within(context, m, best_d):
        return None
    return best


def _live_handle_within(context, m, dist) -> bool:
    """Whether one of the character's own handles is nearer ``m`` than ``dist``."""
    try:
        from . import handles
        if not handles.tool_active(context):
            return False
        arm = handles._arm(context)
        if arm is None:
            return False
        return any(math.hypot(hx - m[0], hy - m[1]) <= max(dist, r + HIT * context.preferences.system.ui_scale)
                   for _h, (hx, hy), r in handles._projected(context, arm))
    except Exception:                                   # noqa: BLE001
        return False


def _parts(context):
    """The handles that can be grabbed: those of the slice under the mouse
    (or being dragged) -- every slice's at once was a tangle of dots.
    ``[(frame, bone, (x, y), offset)]``."""
    region, rv3d = context.region, _rv3d(context)
    out = []
    if region is None or rv3d is None:
        return out
    slices = _slices(context)
    live = _drag["frame"] if _drag["active"] else _slice_under(context, slices)
    for f, _r, off, joints in slices:
        if f != live:
            continue
        for bone, p in joints.items():
            co = view3d_utils.location_3d_to_region_2d(region, rv3d, p + off)
            if co is not None:
                out.append((f, bone, (co.x, co.y), off))
    return out[:MAX_PARTS]


#: the drag under way, for drawing its preview
_drag = {"active": False, "frame": None, "bone": "", "points": None, "parents": None, "offset": None,
         "error": "", "radius": 0, "bent": None}
_hover: dict = {}


def draw(context, highlighted=True):
    from . import key_poses
    s = _settings(context)
    if key_poses.playing():
        _hover.pop(context.area.as_pointer(), None)
        hide_handles = True       # playing: dots popping under a still mouse read as flicker
    else:
        hide_handles = False
    region, rv3d = context.region, _rv3d(context)
    if s is None or region is None or rv3d is None:
        return
    u = context.preferences.system.ui_scale
    hot = _hover.get(context.area.as_pointer()) if highlighted else None
    slices = _slices(context)
    gpu.state.blend_set('ALPHA')
    arm = key_poses._target(s)
    here = {}
    if arm is not None:
        pb = next((arm.pose.bones.get(n) for n in (slices[0][3] if slices else {})
                   if n.rsplit(":", 1)[-1].lower() in {"hips", "pelvis"}), None)
        if pb is not None:
            here[pb.name] = arm.matrix_world @ pb.head
    live = set() if hide_handles else {p[0] for p in _parts(context)}
    for f, r, off, joints in slices:
        rgb, _a = key_poses.onion_look(s, r, max(1, sum(1 for x in slices if (x[1] < 0) == (r < 0))))
        if f in live:
            for bone, p in joints.items():
                co = view3d_utils.location_3d_to_region_2d(region, rv3d, p + off)
                if co is None:
                    continue
                is_hot = hot == (f, bone)
                rr = HANDLE_R * u * (1.3 if is_hot else 1.0)
                st.polygon(_circle(co.x, co.y, rr + 1.4 * u), (0, 0, 0, 0.45))
                st.polygon(_circle(co.x, co.y, rr), (*rgb, 1.0 if is_hot else 0.85))
        # the slice's frame, at its feet: every third and the two ends (twelve
        # labels a side piled up), and the slice under the mouse
        side = sum(1 for x in slices if (x[1] < 0) == (r < 0))
        if not (f in live or (s.onion_wormhole and (abs(r) % 3 == 0 or abs(r) == side))):
            continue                    # plain onion skins: only the one under the mouse
        low = min(joints.values(), key=lambda v: v.z)
        co = view3d_utils.location_3d_to_region_2d(region, rv3d, low + off)
        if co is not None:
            blf.size(0, 10 * u)
            text = str(f)
            w, h = blf.dimensions(0, text)
            x, y = co.x - w / 2, co.y - 16 * u
            # a backing, as the trail's label: the slice's colour on a wall did not read
            st.rounded((x - 4 * u, y - 3 * u, x + w + 4 * u, y + h + 3 * u), 3 * u, (0.08, 0.08, 0.09, 0.8))
            blf.color(0, *rgb, 1.0)
            blf.position(0, x, y, 0)
            blf.draw(0, text)
    # what the tunnel is: time, read left to right
    hips = _hips({"joints": here}) if here else None
    if hips is not None and slices and s.onion_wormhole:
        foot = hips - Vector((0, 0, hips.z - min(min(j.values(), key=lambda v: v.z).z for _f, _r, _o, j in slices)))
        co = view3d_utils.location_3d_to_region_2d(region, rv3d, foot)
        if co is not None:
            blf.size(0, 10 * u)
            text = "\u25c0 earlier  \u00b7  later \u25b6"
            w = blf.dimensions(0, text)[0]
            blf.color(0, 1, 1, 1, 0.55)
            blf.position(0, co.x - w / 2, co.y - 32 * u, 0)
            blf.draw(0, text)
    if s.onion_wormhole:
        draw_propagation(context, with_offsets=True)    # (the plain view: the handles draw it)
    # the dragged joint's path, bent by the move over the radius (where the motion is)
    if _drag["active"] and _drag.get("bent") and len(_drag["bent"]) > 1:
        pts = [view3d_utils.location_3d_to_region_2d(region, rv3d, p) for p in _drag["bent"]]
        pts = [(p.x, p.y) for p in pts if p is not None]
        if len(pts) > 1:
            st.lines(list(zip(pts, pts[1:])), max(1.5, 2.2 * u), st.with_alpha(st.SOFT_ORANGE, 0.9))
    # the pose the drag would key, where its slice is
    if _drag["active"] and _drag["points"] is not None:
        pts = [view3d_utils.location_3d_to_region_2d(region, rv3d, p + _drag["offset"]) for p in _drag["points"]]
        segs = []
        for i, parent in enumerate(_drag["parents"] or []):
            if parent >= 0 and pts[i] is not None and pts[parent] is not None:
                segs.append(((pts[parent].x, pts[parent].y), (pts[i].x, pts[i].y)))
        if segs:
            st.lines(segs, max(1.5, 2.4 * u), st.with_alpha(st.SOFT_ORANGE, 0.95))
    gpu.state.blend_set('NONE')


def _circle(cx, cy, r, n=16):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n)) for k in range(n)]


def hit(context, x, y):
    best, best_d = None, None
    u = context.preferences.system.ui_scale
    for i, (f, bone, (hx, hy), _off) in enumerate(_parts(context)):
        d = math.hypot(hx - x, hy - y)
        if d <= (HANDLE_R + HIT) * u and (best_d is None or d < best_d):
            best, best_d = i, d
    return best


class ANIMATICA_GT_wormhole(bpy.types.Gizmo):
    bl_idname = "ANIMATICA_GT_wormhole"

    def draw(self, context):
        draw(context, True)

    def test_select(self, context, location):
        key = context.area.as_pointer()
        if _mouse.get(key) != tuple(location):
            _mouse[key] = tuple(location)
            context.area.tag_redraw()          # the slice under the mouse shows its handles
        i = hit(context, *location)
        parts = _parts(context)
        key = context.area.as_pointer()
        now = (parts[i][0], parts[i][1]) if i is not None else None
        if _hover.get(key) != now:
            _hover[key] = now
            context.area.tag_redraw()
        return -1 if i is None else i


class ANIMATICA_GGT_wormhole(bpy.types.GizmoGroup):
    bl_idname = "ANIMATICA_GGT_wormhole"
    bl_label = "Zoetrope"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'WINDOW'
    bl_options = {'PERSISTENT', 'SCALE'}

    @classmethod
    def poll(cls, context):
        return editable(context)

    def setup(self, context):
        self.gz = self.gizmos.new(ANIMATICA_GT_wormhole.bl_idname)
        for part in range(MAX_PARTS):
            self.gz.target_set_operator("animatica.wormhole_drag", index=part).index = part
        self.gz.use_tooltip = True
        self.gz.use_draw_hover = False
        self.gz.use_draw_modal = True


class ANIMATICA_OT_wormhole_drag(bpy.types.Operator):
    """Pose this frame where its slice is: drag a hand, foot, the hips or the head and the
    Autoposer moves that frame's body, its switched-on handles holding. The frames around it
    follow with the falloff (wheel: how far). Keyed at that frame; the playhead stays where it is"""
    bl_idname = "animatica.wormhole_drag"
    bl_label = "Pose a Zoetrope Slice"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def description(cls, context, properties):
        parts = _parts(context)
        if 0 <= properties.index < len(parts):
            f, bone, _xy, _o = parts[properties.index]
            return (f"{_label(context, bone)} at frame {f}.\n" + cls.__doc__)
        return cls.__doc__

    def invoke(self, context, event):
        from . import carry, handles, key_poses
        from .autoposer import poser
        parts = _parts(context)
        if not (0 <= self.index < len(parts)):
            return {'CANCELLED'}
        f, bone, _xy, off = parts[self.index]
        entry = key_poses.onion_entry(f)
        arm = key_poses._target(_settings(context))
        if entry is None or arm is None or bone not in (entry.get("joints") or {}):
            return {'CANCELLED'}
        why = handles.problem(context, arm)
        if why:
            self.report({'WARNING'}, why)
            return {'CANCELLED'}
        hs = handles.ensure(context.scene, arm)
        canon = poser.canonical_joint(arm, bone)
        h = next((x for x in hs if x.ap_joint == canon and x.ap_ety != 2 and x.ap_kind != "root"), None)
        if h is None:
            self.report({'WARNING'}, f"No handle drives {_label(context, bone)}: add one (Shift A) to pose it")
            return {'CANCELLED'}
        self._arm, self._frame, self._bone, self._off, self._name = arm, f, bone, off.copy(), h.name
        self._start = Vector(entry["joints"][bone]) + off          # where it is drawn
        self._moved = False
        self._error = ""
        self._radius = self._radius0 = int(context.scene.animatica.trail_radius)
        # the rig, posed as that frame is, for the handles to start from; put
        # back after every solve -- the character stays on the frame you are on
        self._keep = {pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones}
        self._carry = carry.Carry(context, arm, f, self._radius, None, dragged=bone,
                                  held=carry.held_ends(arm, hs, h))
        try:
            self._pose(self._carry.before)
            self._snap = handles.snapshot(arm, hs)
            self._floor = handles.floor_cap(arm, self._snap)
            # the solve of that frame as it is: the edit is measured from it
            if not handles.solve(context, arm, handles.effectors(arm, hs, self._snap), self._floor):
                self._carry.set_base({pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones})
        finally:
            self._pose(self._keep)
        _drag.update(active=True, frame=f, bone=bone, points=None, parents=None, offset=off.copy(), error="",
                     radius=self._radius, bent=None)
        self._header(context)
        show_through(context, self._carry, bone)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _pose(self, bases):
        for name, M in bases.items():
            self._arm.pose.bones[name].matrix_basis = M
        bpy.context.view_layer.update()

    def _header(self, context):
        context.area.header_text_set(
            f"Pose frame {self._frame}: {_label(context, self._bone)}, the body follows"
            f"   |   \u00b1{self._radius} frames follow (wheel)   |   Shift: the whole body   |   Esc: put it back"
            + (f"   |   {self._error}" if self._error else ""))

    def _solve(self, context, event):
        """The Autoposer at that frame, the dragged joint where the mouse puts
        it (out of the tunnel, into the world); the frames around follow."""
        from mathutils.geometry import intersect_line_plane
        from . import handles
        region, rv3d = context.region, _rv3d(context)
        co = (event.mouse_region_x, event.mouse_region_y)
        o = view3d_utils.region_2d_to_origin_3d(region, rv3d, co)
        d = view3d_utils.region_2d_to_vector_3d(region, rv3d, co)
        normal = rv3d.view_rotation @ Vector((0.0, 0.0, 1.0))
        drawn = intersect_line_plane(o, o + d * 10000.0, self._start, normal) or self._start
        target = drawn - self._off
        hs = handles.items(context.scene, self._arm)
        h = next((x for x in hs if x.name == self._name), None)
        if h is None:
            return
        if not h.ap_enabled:
            h.ap_enabled = True
        try:
            self._pose(self._carry.before)
            self._error = handles.solve(context, self._arm,
                                        handles.effectors(self._arm, hs, self._snap, h, target, whole=event.shift),
                                        self._floor)
            after = {pb.name: pb.matrix_basis.copy() for pb in self._arm.pose.bones}
        finally:
            self._pose(self._keep)
        if not self._error:
            self._carry.set_after(after)
            self._moved = True
        _drag["error"] = self._error
        self._header(context)
        show_through(context, self._carry, self._bone)

    def modal(self, context, event):
        if event.type == 'INBETWEEN_MOUSEMOVE':
            return {'RUNNING_MODAL'}    # each one a solve: a trackpad's stream of them queued up, and the drag lagged
        if event.type == 'MOUSEMOVE':
            self._solve(context, event)
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            return self._finish(context, cancel=not self._moved)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            return self._finish(context, cancel=True)
        # how far through time it reaches: as proportional editing, wheel down widens
        if event.type in {'WHEELUPMOUSE', 'PAGE_UP', 'WHEELDOWNMOUSE', 'PAGE_DOWN'} and event.value == 'PRESS':
            step = 1 if event.type in {'WHEELDOWNMOUSE', 'PAGE_UP'} else -1
            self._radius = max(0, min(60, self._radius + step))
            _drag["radius"] = self._radius
            self._carry.radius = self._radius
            context.scene.animatica.trail_radius = self._radius
            show_through(context, self._carry, self._bone, force=True)
            self._header(context)
            return {'RUNNING_MODAL'}
        if event.type in {'MIDDLEMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}
        return {'RUNNING_MODAL'}

    def _finish(self, context, cancel=False):
        from . import key_poses
        _drag.update(active=False, points=None, parents=None, error="", bent=None)
        context.area.header_text_set(None)
        if cancel or not self._carry.changed():
            end_through(context, None)
            context.scene.animatica.trail_radius = self._radius0
            return {'CANCELLED'}
        spread = end_through(context, self._carry)
        cur = context.scene.frame_current
        if abs(cur - self._frame) <= self._radius:
            context.scene.frame_set(cur)        # the frame on show was in reach: show it as keyed
        key_poses.flash_keyed(self._frame)
        self.report({'INFO'}, edit_report(self._carry, spread)
                    + f"; the playhead stayed at {context.scene.frame_current}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# An edit carried through time, as it is made: shared by a slice's handles
# and the live character's (the Autopose tool)
# ---------------------------------------------------------------------------

#: the drag under way that reaches through time: its frame, how far, and where
#: the dragged joint goes on each frame within reach, for drawing as it moves
propagation = {"f0": None, "radius": 0, "frames": {}, "bone": "", "at": 0.0, "carry": None,
               "trail": {}}
#: seconds between live ghost captures (a capture evaluates the character)
LIVE_EVERY = 0.06


def _live_frames(context, c) -> list:
    """The ghosts on show that the edit reaches (the edited frame too, when
    it is a ghost itself)."""
    from . import key_poses
    s = _settings(context)
    if s is None or not key_poses.onion_wanted(context, s):
        return []
    lo, hi = c.span()
    cur = context.scene.frame_current
    return [f for f in key_poses.onion_frames(context.scene, s)
            if lo <= f <= hi and f != cur and (f == c.f0 or c.weight(f) > 1e-3)]


def show_through(context, c, bone, *, force=False) -> None:
    """Show the edit through time as it is made: the dragged joint's path
    over the frames in reach, and -- every LIVE_EVERY seconds -- the ghosts
    there re-posed as they will be."""
    import time
    from . import carry, key_poses
    propagation.update(f0=c.f0, radius=c.radius, frames={}, bone=bone, carry=c, trail={})
    mw = c.arm.matrix_world
    # the motion trail's joints on every frame the edit reaches, where the
    # edit puts them: the trail bends with the drag, as the ghosts do
    # (out to the keys either side on a sparse take: the frames between them
    # follow the curve through the new key, past the reach too)
    lo, hi = c.span()
    moves = {g for g in range(lo, hi + 1) if g == c.f0 or c.weight(g) > 1e-3}
    trail_frames = [f for f in key_poses._trail.get("frames") or () if f in moves]
    trail_bones = [b for b in key_poses._trail.get("bones") or () if b in c.rig.parent]
    frames = set(trail_frames) | (moves if bone in c.rig.parent else set())
    for g in sorted(frames):
        mats = c.rig.fk(c.pose_at(g))
        if bone in mats and g in moves:
            propagation["frames"][g] = mw @ mats[bone].translation
        if g in trail_frames:
            for b in trail_bones:
                propagation["trail"].setdefault(b, {})[g] = mw @ mats[b].translation
    now = time.monotonic()
    if c.changed() and (force or now >= propagation["at"]):
        carry.show_live(context, c, _live_frames(context, c))
        done = time.monotonic()
        # the next one after a pause at least twice as long as this took: on a
        # heavy character the ghosts update less often, the drag stays live
        propagation["at"] = done + max(LIVE_EVERY, 2.0 * (done - now))
    key_poses.tag_redraw()


def end_through(context, c) -> int:
    """The drag is over: key the edit (``c``, or nothing), keep the ghosts it
    already shows, and clear the preview. The frames around it keyed."""
    from . import carry, key_poses
    trail = propagation.get("trail") or {}
    propagation.update(f0=None, radius=0, frames={}, bone="", at=0.0, carry=None, trail={})
    if c is None or not c.changed():
        carry.clear_live()
        key_poses.tag_redraw()
        return 0
    carry.show_live(context, c, _live_frames(context, c))      # the final state, exactly
    _keep_trail(trail)
    n = c.write()
    # the ghosts stay as they are: outside the reach nothing changed, inside
    # it they were just captured -- only the rest of the reach is captured again
    lo, hi = c.span()
    keep = {f: e for f, e in key_poses._onion["cache"].items() if not lo <= f <= hi}
    keep.update(key_poses._onion["live"])
    key_poses.seed_onion(keep)
    carry.clear_live()
    key_poses.invalidate_plan()
    key_poses.request_rebuild()
    from .operators import keep_take
    keep_take(context)              # fine-tuning a take is keeping it
    return n


def _keep_trail(over) -> None:
    """Write the drag's trail positions into the baked trail: they are what
    the keys now give, and the trail no longer snaps back to the old path
    until it is baked again."""
    from . import key_poses
    frames = key_poses._trail.get("frames") or []
    index = {f: i for i, f in enumerate(frames)}
    for name, by_frame in over.items():
        pts = key_poses._trail["points"].get(name)
        if not pts:
            continue
        pts = list(pts)
        for f, p in by_frame.items():
            i = index.get(f)
            if i is not None and i < len(pts):
                pts[i] = tuple(p)
        key_poses._trail["points"][name] = pts
    key_poses._trail_colors["signature"] = None


def edit_report(c, n) -> str:
    """What a kept edit did, in a sentence."""
    around = f", {n} frames around it followed" if n else ""
    if getattr(c, "key_type", 'KEYFRAME') == 'GENERATED':
        return f"The take reshaped at frame {c.f0}{around}"
    return f"Key pose at frame {c.f0}{around}"


def clear_propagation() -> None:
    propagation.update(f0=None, radius=0, frames={}, bone="", at=0.0, carry=None, trail={})


def draw_propagation(context, with_offsets: bool) -> None:
    """Where the dragged joint goes through time, in orange: a dot on each
    frame within reach (on its slice in the zoetrope), joined in frame order."""
    if not propagation["frames"]:
        return
    region, rv3d = context.region, _rv3d(context)
    if region is None or rv3d is None:
        return
    u = context.preferences.system.ui_scale
    offs = {f: o for f, _r, o, _j in _slices(context)} if with_offsets else {}
    pts = []
    for g in sorted(propagation["frames"]):
        if with_offsets and g not in offs and g != propagation["f0"]:
            continue
        p = propagation["frames"][g] + (offs.get(g, Vector()) if with_offsets else Vector())
        co = view3d_utils.location_3d_to_region_2d(region, rv3d, p)
        if co is not None:
            pts.append((g, co))
    if len(pts) > 1:
        st.lines([((a.x, a.y), (b.x, b.y)) for (_g, a), (_h, b) in zip(pts, pts[1:])],
                 max(1.2, 1.8 * u), st.with_alpha(st.SOFT_ORANGE, 0.85))
    for g, co in pts:
        r = (4.5 if g == propagation["f0"] else 3.2) * u
        st.polygon(_circle(co.x, co.y, r + 1.2 * u), (0, 0, 0, 0.5))
        st.polygon(_circle(co.x, co.y, r), st.SOFT_ORANGE)


_classes = (ANIMATICA_GT_wormhole, ANIMATICA_GGT_wormhole, ANIMATICA_OT_wormhole_drag)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    _hover.clear()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
