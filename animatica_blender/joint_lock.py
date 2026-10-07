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
#: events that move the view, let through while a hand or foot is picked
_NAVIGATE = {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM',
             'MOUSEROTATE', 'MOUSESMARTZOOM', 'TIMER'}
#: logical px of mouse travel per frame while the span is set
PX_PER_FRAME = 6.0


# ---------------------------------------------------------------------------
# What to lock
# ---------------------------------------------------------------------------

def target(context):
    """``(bone_name, frame)``: the hand or foot to lock, or None. The trail
    point picked first, then a picked handle on a hand or foot at the playhead."""
    from . import carry, curve_edit, key_poses
    arm = key_poses._target(key_poses._settings(context.scene))
    if arm is None:
        return None
    ends = carry.ENDS_OF(arm)
    sel = curve_edit.selected()
    if sel is not None and sel[0] in ends:
        return sel[0], int(sel[1])
    from . import posing
    handles = posing.handles()
    for h in (handles.picked_items(context.scene, arm) if handles is not None else ()):
        if h.autoposer_joint in carry.ENDS:
            b = posing.joint_bone(arm, h.autoposer_joint)
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


# ---------------------------------------------------------------------------
# Non-destructive: what a lock changed is kept, and given back
# ---------------------------------------------------------------------------

def _chain_paths(arm, bone) -> list:
    from . import pose_edit
    pb = arm.pose.bones.get(bone)
    out = []
    while pb is not None and len(out) < 3:
        out.append(f'pose.bones["{pb.name}"].{pose_edit._rotation_path(pb)}')
        pb = pb.parent
    return out


def _curves_of(action, paths) -> dict:
    from . import constraints_ui
    want = set(paths)
    return {(fc.data_path, fc.array_index): fc for fc in constraints_ui.iter_action_fcurves(action)
            if fc.data_path in want}


def capture(arm, action, bone, start, end) -> dict:
    """The keys a lock on ``start``..``end`` may change, as they are now:
    ``{"path|index": [[frame, y, hl_x, hl_y, hr_x, hr_y, interpolation, type], ...]}``,
    and the channels that had no curve at all."""
    lo, hi = start - EASE, end + EASE
    paths = _chain_paths(arm, bone)
    curves = _curves_of(action, paths)
    keys, missing = {}, []
    for path in paths:
        for i in range(4):
            fc = curves.get((path, i))
            if fc is None:
                missing.append(f"{path}|{i}")
                continue
            keys[f"{path}|{i}"] = [
                [int(round(k.co.x)), k.co.y, k.handle_left.x, k.handle_left.y,
                 k.handle_right.x, k.handle_right.y, k.interpolation, k.type]
                for k in fc.keyframe_points if lo <= int(round(k.co.x)) <= hi]
    return {"lo": lo, "hi": hi, "keys": keys, "missing": missing}


def restore(arm, action, orig) -> None:
    """Give back what a lock changed: the keys in its frames as ``capture``
    found them, the keys it added gone."""
    if not orig or action is None:
        return
    from . import constraints_ui
    lo, hi = int(orig["lo"]), int(orig["hi"])
    names = set(orig["keys"]) | set(orig.get("missing", ()))
    for fcurves in constraints_ui._iter_fcurve_collections(action):
        for fc in list(fcurves):
            name = f"{fc.data_path}|{fc.array_index}"
            if name not in names:
                continue
            kps = fc.keyframe_points
            for j in range(len(kps) - 1, -1, -1):
                if lo <= int(round(kps[j].co.x)) <= hi:
                    kps.remove(kps[j])
            for f, y, hlx, hly, hrx, hry, interp, kind in orig["keys"].get(name, ()):
                k = kps.insert(f, y, options={'FAST'})
                k.interpolation, k.type = interp, kind
                k.handle_left_type = k.handle_right_type = 'FREE'
                k.handle_left = (hlx, hly)
                k.handle_right = (hrx, hry)
            fc.update()
            if len(kps) == 0:
                fcurves.remove(fc)     # a curve the lock made, on a channel that had none


def stored(arm) -> list:
    try:
        return list(json.loads(arm.get(PROP, "[]")))
    except (TypeError, ValueError):
        return []


def _store(arm, locks) -> None:
    arm[PROP] = json.dumps(locks)


def lock(arm, action, bone, start, end, pos, path=None) -> int:
    """Lock ``bone`` on ``pos`` over ``start``..``end``, keeping what it
    changes so the lock can be taken off again. A lock on the same joint that
    overlaps is taken off first. The frames written (0: nothing locked)."""
    for i in reversed([i for i, lk in enumerate(stored(arm))
                       if lk["bone"] == bone and lk["start"] <= end + EASE and lk["end"] >= start - EASE]):
        remove(arm, action, i)
        path = None                     # the motion under it changed back
    orig = capture(arm, action, bone, start, end)
    n = apply(arm, action, bone, start, end, pos, path=path)
    if not n:
        restore(arm, action, orig)
        return 0
    locks = stored(arm)
    locks.append({"bone": bone, "start": int(start), "end": int(end), "pos": [float(x) for x in pos],
                  "orig": orig})
    _store(arm, sorted(locks, key=lambda lk: (lk["start"], lk["bone"])))
    return n


def remove(arm, action, index) -> bool:
    """Take a lock off: its frames as they were before it."""
    locks = stored(arm)
    if not 0 <= index < len(locks):
        return False
    restore(arm, action, locks[index].get("orig"))
    locks.pop(index)
    _store(arm, locks)
    return True


def relock(arm, action, index, start, end) -> int:
    """Change a lock's frames: the motion given back, then locked again over
    the new ones (from the original motion, not over the old lock)."""
    locks = stored(arm)
    if not 0 <= index < len(locks):
        return 0
    lk = locks[index]
    remove(arm, action, index)
    return lock(arm, action, lk["bone"], int(start), int(end), lk["pos"])


def held_at(arm, frame) -> list:
    """The bones locked on ``frame``: a drag holds them where they are."""
    return [lk["bone"] for lk in stored(arm) if lk["start"] <= frame <= lk["end"]
            and lk["bone"] in arm.pose.bones]


def reapply(arm, action, lo, hi) -> int:
    """After a new take: the locks over it hold again. The locks re-applied."""
    n = 0
    locks = stored(arm)
    for lk in locks:
        if lk["bone"] in arm.pose.bones and lk["start"] <= hi and lk["end"] >= lo:
            # the new take is the motion now: what taking the lock off gives back
            lk["orig"] = capture(arm, action, lk["bone"], lk["start"], lk["end"])
            if apply(arm, action, lk["bone"], lk["start"], lk["end"], lk["pos"]):
                n += 1
    _store(arm, locks)
    return n


# ---------------------------------------------------------------------------
# Drawing: the span being set, and the locks on the playhead
# ---------------------------------------------------------------------------

_preview = {"on": False, "points": [], "pos": None, "pick": [], "hover": -1,
            "names": [], "span": None, "name": "", "say": ""}
_handle = [None]
_tl_handle = [None]


def step_text() -> str:
    """The step a lock being made is on, for the line above the bar ("" when none)."""
    return _preview["say"]
_LOCKED = (1.0, 0.78, 0.25, 1.0)
_PATH = (1.0, 1.0, 1.0, 0.55)


def _diamonds(verts, colors, tris, xy, r, color):
    b = len(verts)
    verts += [(xy.x, xy.y + r), (xy.x + r, xy.y), (xy.x, xy.y - r), (xy.x - r, xy.y)]
    colors += [color] * 4
    tris += [(b, b + 1, b + 2), (b, b + 2, b + 3)]


def _text(x, y, text, color, size):
    import blf
    blf.size(0, size)
    blf.color(0, *color)
    blf.position(0, x, y, 0)
    blf.draw(0, text)


def _draw():
    ctx = bpy.context
    from bpy_extras import view3d_utils
    from . import key_poses
    try:
        arm = key_poses._target(key_poses._settings(ctx.scene))
    except Exception:                                   # noqa: BLE001
        return
    region, rv3d = ctx.region, ctx.region_data
    if arm is None or region is None or rv3d is None:
        return
    mw = arm.matrix_world
    px = ctx.preferences.system.ui_scale

    def at(p):
        return view3d_utils.location_3d_to_region_2d(region, rv3d, mw @ Vector(p))

    verts, colors, tris, texts, line = [], [], [], [], []
    if _preview["pick"]:
        # 1 of 2: the hands and feet, named, the one under the mouse lit
        for i, p in enumerate(_preview["pick"]):
            xy = at(p)
            if xy is None:
                continue
            lit = i == _preview["hover"]
            _diamonds(verts, colors, tris, xy, (12.0 if lit else 8.0) * px, _LOCKED if lit else _PATH)
            name = _preview["names"][i] if i < len(_preview["names"]) else ""
            texts.append((xy.x + 14 * px, xy.y - 5 * px, name, _LOCKED if lit else (1, 1, 1, 0.9)))
    elif _preview["on"]:
        # 2 of 2: the path, the frames that will hold thick, the spot, the ends numbered
        span = _preview["span"]
        for p, locked in _preview["points"]:
            xy = at(p)
            if xy is None:
                continue
            if locked:
                line.append((xy.x, xy.y, 0.0))
            else:
                _diamonds(verts, colors, tris, xy, 3.0 * px, _PATH)
        if _preview["pos"] is not None:
            xy = at(_preview["pos"])
            if xy is not None:
                _diamonds(verts, colors, tris, xy, 10.0 * px, _LOCKED)
                if span:
                    texts.append((xy.x + 14 * px, xy.y + 8 * px,
                                  f"{_preview['name']} stays here, {span[0]}\u2013{span[1]}", _LOCKED))
    else:
        # locked: on a frame a lock holds, its spot and what it is
        f = ctx.scene.frame_current
        for lk in stored(arm):
            if lk["start"] <= f <= lk["end"]:
                xy = at(lk["pos"])
                if xy is None:
                    continue
                _diamonds(verts, colors, tris, xy, 7.0 * px, _LOCKED)
                texts.append((xy.x + 11 * px, xy.y - 5 * px,
                              f"{label(arm, lk['bone'])} locked {lk['start']}\u2013{lk['end']}", _LOCKED))
    if not verts and not line and not texts:
        return
    gpu.state.blend_set('ALPHA')
    try:
        if len(line) >= 2:
            sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            sh.bind()
            sh.uniform_float("viewportSize", (region.width, region.height))
            sh.uniform_float("lineWidth", 6.0 * px)
            sh.uniform_float("color", (*_LOCKED[:3], 0.9))
            batch_for_shader(sh, 'LINE_STRIP', {"pos": line}).draw(sh)
        if verts:
            sh = gpu.shader.from_builtin('SMOOTH_COLOR')
            sh.bind()
            batch_for_shader(sh, 'TRIS', {"pos": verts, "color": colors}, indices=tris).draw(sh)
        for x, y, text, color in texts:
            _text(x, y, text, color, int(12 * px))
    finally:
        gpu.state.blend_set('NONE')


def _draw_timeline():
    """The locks in the Timeline and Dope Sheet: an orange band over each one's
    frames, named; the one being made, brighter."""
    ctx = bpy.context
    from . import key_poses
    try:
        arm = key_poses._target(key_poses._settings(ctx.scene))
    except Exception:                                   # noqa: BLE001
        return
    region = ctx.region
    if arm is None or region is None or not hasattr(region, "view2d"):
        return
    bands = [(lk["start"], lk["end"], f"{label(arm, lk['bone'])} locked", 0.35) for lk in stored(arm)]
    if _preview["on"] and _preview["span"]:
        a, b = _preview["span"]
        bands.append((a, b, f"{_preview['name']}: locking", 0.7))
    if not bands:
        return
    px = ctx.preferences.system.ui_scale
    top = region.height - 26 * px
    h = 14 * px
    verts, tris = [], []
    sh = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    try:
        for a, b, text, alpha in bands:
            x0 = region.view2d.view_to_region(a - 0.5, 0, clip=False)[0]
            x1 = region.view2d.view_to_region(b + 0.5, 0, clip=False)[0]
            if x1 < 0 or x0 > region.width:
                continue
            sh.bind()
            sh.uniform_float("color", (*_LOCKED[:3], alpha))
            batch_for_shader(sh, 'TRIS', {"pos": [(x0, top - h), (x1, top - h), (x1, top), (x0, top)]},
                             indices=[(0, 1, 2), (0, 2, 3)]).draw(sh)
            import blf
            blf.size(0, int(10 * px))
            tw = blf.dimensions(0, text)[0]
            if tw + 8 * px <= x1 - max(x0, 0):
                _text(max(x0, 0) + 4 * px, top - h + 3 * px, text, (0.1, 0.08, 0.02, 1.0), int(10 * px))
            else:   # too narrow to hold its name: written after it
                _text(x1 + 4 * px, top - h + 3 * px, text, _LOCKED, int(10 * px))
    finally:
        gpu.state.blend_set('NONE')


def _redraw(context):
    for area in context.screen.areas if context.screen else ():
        if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR', 'TIMELINE'}:
            area.tag_redraw()


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class ANIMATICA_OT_lock_joint(bpy.types.Operator):
    """Lock a hand or foot where it is at the playhead, for a stretch of frames.
    Fixes a foot that slides while it should be planted. Click this, click the
    foot, move the mouse to set how long (Ctrl: the start), then click"""
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
        from . import carry, key_poses, pose_edit
        arm = key_poses._target(key_poses._settings(context.scene))
        action = pose_edit._editing_action(arm) if arm is not None else None
        if action is None:
            self.report({'WARNING'}, "There is no motion to lock yet. Generate a take or key some poses first")
            return {'CANCELLED'}
        self._arm, self._action = arm, action
        self._region, self._rv3d = context.region, context.region_data
        context.window_manager.modal_handler_add(self)
        got = target(context)
        if got is None or self._rv3d is None:
            # nothing picked: pick it here, by clicking the hand or foot itself
            if self._rv3d is None:
                self.report({'WARNING'}, "Click Lock in Place from the viewport's bar")
                return {'CANCELLED'}
            mats = carry.Rig(arm).fk({pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones})
            self._ends = [b for b in carry.ENDS_OF(arm)]
            _preview.update(pick=[mats[b].translation.copy() for b in self._ends], hover=-1, on=False,
                            names=[label(arm, b).replace("Left ", "L ").replace("Right ", "R ")
                                   for b in self._ends],
                            say="Lock in Place \u00b7 1 of 2: click the hand or foot that should stay put "
                                "\u00b7 Esc cancels")
            self._phase = "pick"
            context.window.cursor_modal_set('EYEDROPPER')
            if context.area:
                context.area.header_text_set("Lock in Place   |   Click the hand or foot to lock   |   Esc: cancel")
            _redraw(context)
            return {'RUNNING_MODAL'}
        self._begin_span(context, event, *got)
        return {'RUNNING_MODAL'}

    def _begin_span(self, context, event, bone, frame):
        self.bone, self.frame = bone, int(frame)
        arm, action = self._arm, self._action
        _preview.update(pick=[], hover=-1, names=[])
        self._phase = "span"
        self._path = Path(arm, action, self.bone, self.frame)
        self._name = label(arm, self.bone)
        self.frame_start, self.frame_end = initial_span(context, self._path, self.frame)
        self._grab = "end"
        self._x0 = event.mouse_x
        self._s0, self._e0 = self.frame_start, self.frame_end
        self._show(context)
        context.window.cursor_modal_set('SCROLL_X')

    def _hovered(self, event) -> int:
        from bpy_extras import view3d_utils
        r = self._region
        x, y = event.mouse_x - r.x, event.mouse_y - r.y
        u = bpy.context.preferences.system.ui_scale
        best, best_d = -1, 40.0 * u
        mw = self._arm.matrix_world
        for i, p in enumerate(_preview["pick"]):
            xy = view3d_utils.location_3d_to_region_2d(r, self._rv3d, mw @ p)
            if xy is None:
                continue
            d = ((xy.x - x) ** 2 + (xy.y - y) ** 2) ** 0.5
            if d < best_d:
                best, best_d = i, d
        return best

    def _show(self, context):
        p = self._path
        n = self.frame_end - self.frame_start + 1
        _preview.update(span=(self.frame_start, self.frame_end), name=self._name,
                        say=f"Lock in Place \u00b7 2 of 2: {self._name}, frames {self.frame_start}\u2013"
                            f"{self.frame_end} ({n}) \u00b7 move the mouse \u2190\u2192 for the end, Ctrl for "
                            "the start \u00b7 click to lock")
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
        if self._phase == "pick":
            if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
                self._end(context)
                return {'CANCELLED'}
            if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
                h = self._hovered(event)
                if h != _preview["hover"]:
                    _preview["hover"] = h
                    if context.area:
                        context.area.header_text_set(
                            f"Lock in Place   |   Click to lock the {label(self._arm, self._ends[h]).lower()}"
                            if h >= 0 else "Lock in Place   |   Click the hand or foot to lock   |   Esc: cancel")
                    _redraw(context)
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                h = self._hovered(event)
                if h >= 0:
                    self._begin_span(context, event, self._ends[h], context.scene.frame_current)
                return {'RUNNING_MODAL'}
            # turning and zooming the view to find the foot still work
            return {'PASS_THROUGH'} if event.type in _NAVIGATE else {'RUNNING_MODAL'}
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
        _preview.update(on=False, points=[], pos=None, pick=[], hover=-1, names=[], span=None, name="", say="")
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
        n = lock(arm, action, self.bone, start, end, pos, path=path)
        if not n:
            self.report({'WARNING'}, f"{label(arm, self.bone)} could not be locked: it needs two parent bones (a leg or an arm)")
            return {'CANCELLED'}
        key_poses.invalidate_plan()
        key_poses.request_rebuild()
        from .operators import keep_take
        keep_take(context)
        _redraw(context)
        self.report({'INFO'}, f"{label(arm, self.bone)} locked in place on frames {start}–{end}")
        return {'FINISHED'}


def _after_edit(context):
    from . import key_poses
    key_poses.invalidate_plan()
    key_poses.request_rebuild()
    _redraw(context)


class ANIMATICA_OT_unlock_joint(bpy.types.Operator):
    """Remove this lock and give these frames back their motion as it was before"""
    bl_idname = "animatica.unlock_joint"
    bl_label = "Remove Lock"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: bpy.props.IntProperty()

    def execute(self, context):
        from . import key_poses, pose_edit
        arm = key_poses._target(key_poses._settings(context.scene))
        if arm is None:
            return {'CANCELLED'}
        locks = stored(arm)
        if not 0 <= self.index < len(locks):
            return {'CANCELLED'}
        lk = locks[self.index]
        remove(arm, pose_edit._editing_action(arm), self.index)
        _after_edit(context)
        self.report({'INFO'}, f"{label(arm, lk['bone'])} unlocked: frames {lk['start']}\u2013{lk['end']} "
                              "are back as they were")
        return {'FINISHED'}


class ANIMATICA_OT_lock_range(bpy.types.Operator):
    """Change which frames this lock holds. The motion is given back first, then
    locked again over the new frames"""
    bl_idname = "animatica.lock_range"
    bl_label = "Lock Frames"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    index: bpy.props.IntProperty(options={'HIDDEN'})
    frame_start: bpy.props.IntProperty(name="From")
    frame_end: bpy.props.IntProperty(name="To")

    def invoke(self, context, event):
        from . import key_poses
        arm = key_poses._target(key_poses._settings(context.scene))
        locks = stored(arm) if arm is not None else []
        if not 0 <= self.index < len(locks):
            return {'CANCELLED'}
        self.frame_start, self.frame_end = locks[self.index]["start"], locks[self.index]["end"]
        return context.window_manager.invoke_props_dialog(self, width=220, title="Lock these frames")

    def execute(self, context):
        from . import key_poses, pose_edit
        arm = key_poses._target(key_poses._settings(context.scene))
        if arm is None or not 0 <= self.index < len(stored(arm)):
            return {'CANCELLED'}
        a, b = sorted((int(self.frame_start), int(self.frame_end)))
        if b - a < 1:
            self.report({'WARNING'}, "A lock needs at least two frames")
            return {'CANCELLED'}
        name = label(arm, stored(arm)[self.index]["bone"])
        if not relock(arm, pose_edit._editing_action(arm), self.index, a, b):
            return {'CANCELLED'}
        _after_edit(context)
        self.report({'INFO'}, f"{name} now locked on frames {a}\u2013{b}")
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
        row.operator("animatica.lock_range", text="", icon='GREASEPENCIL').index = i
        row.operator("animatica.unlock_joint", text="", icon='X').index = i


_classes = (ANIMATICA_OT_lock_joint, ANIMATICA_OT_unlock_joint, ANIMATICA_OT_lock_range)


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    if _handle[0] is None:
        _handle[0] = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')
    if _tl_handle[0] is None:
        _tl_handle[0] = bpy.types.SpaceDopeSheetEditor.draw_handler_add(_draw_timeline, (), 'WINDOW', 'POST_PIXEL')


def unregister():
    if _handle[0] is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle[0], 'WINDOW')
        _handle[0] = None
    if _tl_handle[0] is not None:
        bpy.types.SpaceDopeSheetEditor.draw_handler_remove(_tl_handle[0], 'WINDOW')
        _tl_handle[0] = None
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
