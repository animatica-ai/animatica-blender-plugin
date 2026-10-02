# SPDX-License-Identifier: GPL-3.0-or-later
"""A hand or foot locked in place for a stretch of frames.

A generated take can let a planted foot slide. Every other edit spreads with
the falloff, so none can hold a joint on one spot for a whole contact: a
lock does. The joint is held where it is at the playhead, for the frames of
the span, by the leg's (or arm's) two bones -- the knee bending the way it
already bent, the foot keeping its own turn, the hips left alone -- and it
eases in and out over a few frames either side, so the foot lands and lifts
off instead of snapping.

The span starts as the contact around the playhead (the frames where the
joint is nearly still); with none, the Reach frames on from the playhead.
Moving the mouse sets its end (Ctrl: its start) before the click that locks.

Locks are kept on the rig. A new take over the span is locked again after its
bake, and a drag inside the span holds the locked joint where it is.
"""

from __future__ import annotations

import json
import math
import re

import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

#: the rig's stored locks: JSON ``[{"bone", "start", "end", "pos"}]``, pos in armature space
PROP = "animatica_locks"
#: frames either side of a span over which the lock fades in and out
EASE = 4
#: how far either side of the playhead a contact is looked for
SEARCH = 90
#: a joint moving less than this much per frame (m, or this share of its
#: fastest) is standing still
STILL_M = 0.012
STILL_SHARE = 0.2
STILL_MAX = 0.03
#: logical px of mouse travel per frame while the span is set
PX_PER_FRAME = 6.0


# ---------------------------------------------------------------------------
# What to lock
# ---------------------------------------------------------------------------

def target(context):
    """``(bone_name, frame)``: the hand or foot to lock, or None. The trail
    point picked first, then a picked handle on a hand or foot at the playhead."""
    from . import carry, curve_edit, handles, key_poses
    arm = key_poses._target(key_poses._settings(context.scene))
    if arm is None:
        return None
    ends = carry.ENDS_OF(arm)
    sel = curve_edit.selected()
    if sel is not None and sel[0] in ends:
        return sel[0], int(sel[1])
    from .autoposer import poser
    for h in handles.picked_items(context.scene, arm):
        if h.ap_joint in carry.ENDS:
            b = poser.joint_bone(arm, h.ap_joint)
            if b is not None:
                return b.name, int(context.scene.frame_current)
    return None


# ---------------------------------------------------------------------------
# The joint's path, and the contact around a frame
# ---------------------------------------------------------------------------

class Path:
    """Where ``bone``'s head is (armature space) on the frames around ``f0``,
    read off the curves; nothing is stepped."""

    def __init__(self, arm, action, bone, f0, lo=None, hi=None):
        from . import carry
        self.arm, self.action, self.bone = arm, action, bone
        self.rig = carry.Rig(arm)
        self.names = [pb.name for pb in arm.pose.bones]
        self.now = {pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones}
        self.curves = carry.action_curves(action)
        self.lo = int(f0 - SEARCH if lo is None else lo)
        self.hi = int(f0 + SEARCH if hi is None else hi)
        self.at = {}
        for f in range(self.lo, self.hi + 1):
            mats = self.rig.fk(self.bases(f))
            self.at[f] = mats[bone].translation.copy()

    def bases(self, f) -> dict:
        from . import carry
        return carry.bases_from_action(self.arm, self.action, f, self.names, self.now, self.curves)

    def contact(self, f0) -> tuple | None:
        """The frames around ``f0`` the joint is nearly still on, or None."""
        speed = {f: (self.at[f] - self.at[f - 1]).length for f in range(self.lo + 1, self.hi + 1)}
        if not speed:
            return None
        # a sliding foot still counts (that is what a lock is for), a stepping one not
        still = min(STILL_MAX, max(STILL_M, STILL_SHARE * max(speed.values())))

        def quiet(f):
            return speed.get(f, still + 1) <= still and speed.get(f + 1, still + 1) <= still

        if not quiet(f0) and not quiet(f0 - 1) and not quiet(f0 + 1):
            return None
        a = b = f0
        while a - 1 > self.lo and speed.get(a, still + 1) <= still:
            a -= 1
        while b + 1 < self.hi and speed.get(b + 1, still + 1) <= still:
            b += 1
        return (a, b) if b - a >= 2 else None


def label(arm, bone) -> str:
    """``Left foot`` for the rig's own bone name."""
    from . import carry
    j = carry.ENDS_OF(arm).get(bone, bone.split(":")[-1])
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", j).capitalize()


def initial_span(context, path, f0) -> tuple:
    sc = context.scene
    span = path.contact(f0)
    if span is None:
        reach = max(6, int(getattr(sc.animatica, "trail_radius", 6) or 6))
        span = (f0, f0 + reach)
    return max(span[0], sc.frame_start), min(span[1], sc.frame_end)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def _weight(g, start, end) -> float:
    if start <= g <= end:
        return 1.0
    d = start - g if g < start else g - end
    if d > EASE:
        return 0.0
    return 0.5 * (1 + math.cos(math.pi * d / (EASE + 1)))


def apply(arm, action, bone, start, end, pos, *, path=None) -> int:
    """Hold ``bone`` on ``pos`` (armature space) from ``start`` to ``end``,
    easing over EASE frames either side. Keys the limb's two bones and the
    end on each frame of the span (a frame there without a key gets one, as
    motion: a lock between sparse keys would otherwise not hold); the easing
    frames only update keys already there. The frames written."""
    from . import carry, pose_edit
    if action is None:
        return 0
    b2 = arm.pose.bones[bone].parent
    b1 = b2.parent if b2 is not None else None
    if b1 is None:
        return 0
    if path is None:
        path = Path(arm, action, bone, start, start - EASE, end + EASE)
    pos = Vector(pos)
    n = 0
    for g in range(start - EASE, end + EASE + 1):
        w = _weight(g, start, end)
        if w <= 0.0:
            continue
        bases = path.bases(g)
        here = path.at.get(g)
        if here is None:
            here = path.rig.fk(bases)[bone].translation
        if not carry.reach(path.rig, bases, bone, here.lerp(pos, w)):
            continue
        channels = []
        for pb in (b1, b2, arm.pose.bones[bone]):
            loc, q, _s = bases[pb.name].decompose()
            p = pose_edit._rotation_path(pb)
            if p == "rotation_quaternion":
                values = [q.w, q.x, q.y, q.z]
            elif p == "rotation_euler":
                values = list(q.to_euler(pb.rotation_mode, pb.rotation_euler))
            else:
                axis, angle = q.to_axis_angle()
                values = [angle, axis.x, axis.y, axis.z]
            channels += [(f'pose.bones["{pb.name}"].{p}', i, v) for i, v in enumerate(values)]
        # motion, not key poses: a key pose already there stays one, and the
        # keys a lock adds are not sent to the next Generate as poses to hit
        pose_edit.write_channels(action, g, channels, 'GENERATED',
                                 finish=False, existing_only=not (start <= g <= end))
        n += 1
    pose_edit.finish_channels(action)
    return n


def stored(arm) -> list:
    try:
        return list(json.loads(arm.get(PROP, "[]")))
    except (TypeError, ValueError):
        return []


def _store(arm, locks) -> None:
    arm[PROP] = json.dumps(locks)


def add(arm, bone, start, end, pos) -> None:
    # a new lock on the same joint replaces the ones it overlaps
    locks = [lk for lk in stored(arm) if not (lk["bone"] == bone and lk["start"] <= end and lk["end"] >= start)]
    locks.append({"bone": bone, "start": int(start), "end": int(end), "pos": [float(x) for x in pos]})
    _store(arm, sorted(locks, key=lambda lk: (lk["start"], lk["bone"])))


def remove(arm, index) -> None:
    locks = stored(arm)
    if 0 <= index < len(locks):
        locks.pop(index)
        _store(arm, locks)


def held_at(arm, frame) -> list:
    """The bones locked on ``frame``: a drag holds them where they are."""
    return [lk["bone"] for lk in stored(arm) if lk["start"] <= frame <= lk["end"]
            and lk["bone"] in arm.pose.bones]


def reapply(arm, action, lo, hi) -> int:
    """After a new take: the locks over it hold again. The locks re-applied."""
    n = 0
    for lk in stored(arm):
        if lk["bone"] in arm.pose.bones and lk["start"] <= hi and lk["end"] >= lo:
            if apply(arm, action, lk["bone"], lk["start"], lk["end"], lk["pos"]):
                n += 1
    return n


# ---------------------------------------------------------------------------
# Drawing: the span being set, and the locks on the playhead
# ---------------------------------------------------------------------------

_preview = {"on": False, "points": [], "pos": None}
_handle = [None]
_LOCKED = (1.0, 0.78, 0.25, 1.0)
_PATH = (1.0, 1.0, 1.0, 0.35)


def _draw():
    ctx = bpy.context
    from . import key_poses
    try:
        arm = key_poses._target(key_poses._settings(ctx.scene))
    except Exception:                                   # noqa: BLE001
        return
    if arm is None:
        return
    mw = arm.matrix_world
    pts, hot = [], []
    if _preview["on"]:
        for p, locked in _preview["points"]:
            (hot if locked else pts).append(tuple(mw @ p))
        if _preview["pos"] is not None:
            hot.append(tuple(mw @ _preview["pos"]))
    else:
        f = ctx.scene.frame_current
        hot = [tuple(mw @ Vector(lk["pos"])) for lk in stored(arm) if lk["start"] <= f <= lk["end"]]
    if not pts and not hot:
        return
    shader = gpu.shader.from_builtin('POINT_UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE')
    try:
        for coords, color, size in ((pts, _PATH, 5.0), (hot, _LOCKED, 9.0)):
            if not coords:
                continue
            gpu.state.point_size_set(size * ctx.preferences.system.ui_scale)
            batch = batch_for_shader(shader, 'POINTS', {"pos": coords})
            shader.bind()
            shader.uniform_float("color", color)
            batch.draw(shader)
    finally:
        gpu.state.point_size_set(1.0)
        gpu.state.blend_set('NONE')


def _redraw(context):
    for area in context.screen.areas if context.screen else ():
        if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR', 'TIMELINE'}:
            area.tag_redraw()


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class ANIMATICA_OT_lock_joint(bpy.types.Operator):
    """Lock the picked hand or foot where it is at the playhead, for a stretch of frames.
    Fixes a foot that slides while it should be planted. Move the mouse to set
    the end (Ctrl: the start), then click"""
    bl_idname = "animatica.lock_joint"
    bl_label = "Lock in Place"
    bl_options = {'REGISTER', 'UNDO'}

    bone: bpy.props.StringProperty(options={'HIDDEN'})
    frame: bpy.props.IntProperty(name="Locked at", options={'HIDDEN'})
    frame_start: bpy.props.IntProperty(name="From")
    frame_end: bpy.props.IntProperty(name="To")

    @classmethod
    def poll(cls, context):
        # a script names the bone itself; the click is checked in invoke
        from . import key_poses
        return key_poses._target(key_poses._settings(context.scene)) is not None

    def invoke(self, context, event):
        from . import key_poses, pose_edit
        got = target(context)
        if got is None:
            self.report({'WARNING'}, "Pick a hand or foot first: its point on the motion trail, or its handle")
            return {'CANCELLED'}
        self.bone, self.frame = got
        arm = key_poses._target(key_poses._settings(context.scene))
        action = pose_edit._editing_action(arm)
        if action is None:
            self.report({'WARNING'}, "There is no motion to lock yet")
            return {'CANCELLED'}
        self._path = Path(arm, action, self.bone, self.frame)
        self._name = label(arm, self.bone)
        self.frame_start, self.frame_end = initial_span(context, self._path, self.frame)
        self._grab = "end"
        self._x0 = event.mouse_x
        self._s0, self._e0 = self.frame_start, self.frame_end
        self._show(context)
        context.window.cursor_modal_set('SCROLL_X')
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _show(self, context):
        p = self._path
        _preview.update(on=True, pos=p.at.get(self.frame),
                        points=[(p.at[f], self.frame_start <= f <= self.frame_end)
                                for f in range(max(p.lo, self.frame_start - 24), min(p.hi, self.frame_end + 24) + 1)])
        if context.area:
            n = self.frame_end - self.frame_start + 1
            context.area.header_text_set(
                f"Lock {self._name} at frames {self.frame_start}–{self.frame_end} ({n} frames)   |   "
                "Move: set the end   |   Ctrl+Move: set the start   |   Click or Enter: lock   |   Esc: cancel")
        _redraw(context)

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:                               # noqa: BLE001
            self._end(context)
            raise

    def _modal(self, context, event):
        u = context.preferences.system.ui_scale
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            grab = "start" if event.ctrl else "end"
            if grab != self._grab:
                # switching ends: the travel counts from here
                self._grab, self._x0 = grab, event.mouse_x
                self._s0, self._e0 = self.frame_start, self.frame_end
            d = int(round((event.mouse_x - self._x0) / (PX_PER_FRAME * u)))
            if grab == "end":
                hi = min(self._path.hi, context.scene.frame_end)
                self.frame_end = max(self.frame_start + 1, min(hi, self._e0 + d))
            else:
                lo = max(self._path.lo, context.scene.frame_start)
                self.frame_start = min(self.frame_end - 1, max(lo, self._s0 + d))
            self._show(context)
            return {'RUNNING_MODAL'}
        if event.type in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._end(context)
            return self.execute(context)
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._end(context)
            return {'CANCELLED'}
        return {'RUNNING_MODAL'}

    def _end(self, context):
        _preview.update(on=False, points=[], pos=None)
        context.window.cursor_modal_restore()
        if context.area:
            context.area.header_text_set(None)
        _redraw(context)

    def cancel(self, context):
        self._end(context)

    def execute(self, context):
        from . import key_poses, pose_edit
        arm = key_poses._target(key_poses._settings(context.scene))
        if arm is None or self.bone not in arm.pose.bones:
            return {'CANCELLED'}
        action = pose_edit._editing_action(arm)
        start, end = sorted((int(self.frame_start), int(self.frame_end)))
        anchor = min(max(int(self.frame), start), end)
        path = Path(arm, action, self.bone, anchor, min(start, anchor) - EASE, max(end, anchor) + EASE)
        pos = path.at[anchor]
        n = apply(arm, action, self.bone, start, end, pos, path=path)
        if not n:
            self.report({'WARNING'}, f"{label(arm, self.bone)} could not be locked: it needs two parent bones (a leg or an arm)")
            return {'CANCELLED'}
        add(arm, self.bone, start, end, pos)
        key_poses.invalidate_plan()
        key_poses.request_rebuild()
        from .operators import keep_take
        keep_take(context)
        _redraw(context)
        self.report({'INFO'}, f"{label(arm, self.bone)} locked in place on frames {start}–{end}")
        return {'FINISHED'}


class ANIMATICA_OT_unlock_joint(bpy.types.Operator):
    """Remove this lock. The motion stays as it is now, but a new take over
    these frames is no longer locked"""
    bl_idname = "animatica.unlock_joint"
    bl_label = "Remove Lock"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        from . import key_poses
        arm = key_poses._target(key_poses._settings(context.scene))
        if arm is None:
            return {'CANCELLED'}
        remove(arm, self.index)
        _redraw(context)
        return {'FINISHED'}


def draw_list(layout, arm) -> None:
    """The rig's locks, for the Pose panel: which joint, which frames, a remove button."""
    locks = stored(arm) if arm is not None else []
    if not locks:
        return
    col = layout.column(align=True)
    col.label(text="Locked in place", icon='LOCKED')
    for i, lk in enumerate(locks):
        row = col.row(align=True)
        row.label(text=f"{label(arm, lk['bone'])}  {lk['start']}–{lk['end']}")
        row.operator("animatica.unlock_joint", text="", icon='X').index = i


_classes = (ANIMATICA_OT_lock_joint, ANIMATICA_OT_unlock_joint)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    if _handle[0] is None:
        _handle[0] = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_VIEW')


def unregister():
    if _handle[0] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle[0], 'WINDOW')
        _handle[0] = None
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
