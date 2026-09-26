# SPDX-License-Identifier: GPL-3.0-or-later
"""Make a generated clip loop: a walk cycle, an idle, a run for a game.

The model generates a clip with a start and an end, not a cycle. A walk of
any length ends mid-stride in some other phase than it began, so repeating it
jumps on every wrap. Three steps close it, after the bake:

1. **Cut where the motion comes round.** Find the pair of frames, one early
   and one late, whose poses (and the poses after them) match best, and keep
   only the stretch between them. The loop is then a whole number of strides
   rather than the length the block happened to be, and it skips the start
   from standing that every generated walk opens with.
2. **Blend the seam.** Whatever difference is left between the cut frame and
   the first is spread over the last few frames, on every joint and on the
   root's height, so the last frame lands exactly on the first pose.
3. **Repeat.** Cycles modifiers on the curves play the loop past its end in
   Blender. In place, the root repeats as it is; travelling, its travel
   repeats with offset, so a walking loop keeps walking forward rather than
   snapping back (see set_root_mode).

Keys keep their GENERATED type, so Reject still strips the take.
"""

from __future__ import annotations

import math

from mathutils import Quaternion, Vector

#: frames over which the leftover seam difference is blended in
BLEND_FRAMES = 12
#: the loop's first frame is looked for in this opening share of the clip
START_SEARCH = 0.35
#: and the loop is at least this share of the clip long
MIN_SHARE = 0.5


def _bone_curves(action):
    """``{(bone, prop): [fcurves by index]}`` for pose-bone rotation and location."""
    from . import constraints_ui

    out: dict = {}
    for fc in constraints_ui.iter_action_fcurves(action):
        path = fc.data_path
        if not path.startswith('pose.bones["'):
            continue
        bone, _, prop = path[12:].partition('"].')
        out.setdefault((bone, prop), {})[fc.array_index] = fc
    return {k: [v[i] for i in sorted(v)] for k, v in out.items()}


def _quat(fcs, f):
    return Quaternion([fc.evaluate(f) for fc in fcs])


def _table(rots, lo, hi):
    """Every joint's rotation on every frame, read once: ``{frame: [Quaternion]}``."""
    return {f: [_quat(fcs, f) for fcs in rots] for f in range(lo, hi + 1)}


def _distance(table, a, b):
    """Summed joint rotation difference between frames *a* and *b*, radians."""
    return sum(2.0 * math.acos(min(1.0, abs(x.dot(y)))) for x, y in zip(table[a], table[b]))


def best_span(table, lo, hi):
    """``(start, end)``: the stretch of the clip that best comes back round to itself.

    Not simply the whole clip from its first frame. The model starts a walk
    from standing, so the opening frames are still speeding up; a loop from
    frame 1 carried that acceleration round every cycle (the step across the
    wrap 26% bigger than the rest). The start is looked for in the first
    third, the end in the part at least half a clip later, and each pair is
    scored on the pose and on where it is heading (the frame after).
    """
    n = hi - lo
    best, best_d = (lo, hi), None
    for a in range(lo, lo + max(1, int(START_SEARCH * n)) + 1):
        for c in range(max(a + int(MIN_SHARE * n), a + 8), hi):
            d = _distance(table, a, c) + _distance(table, a + 1, c + 1)
            if best_d is None or d < best_d:
                best, best_d = (a, c), d
    return best


def _set(fc, frame, value):
    for k in fc.keyframe_points:
        if abs(k.co.x - frame) < 0.5:
            d = value - k.co.y
            k.co.y = value
            k.handle_left.y += d
            k.handle_right.y += d
            return


def _smooth(t):
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def apply(arm, action, frame_range, in_place=False) -> dict:
    """Close *action* into a loop over part of *frame_range*. Returns what was done."""
    if arm is None or action is None:
        return {}
    lo, hi = int(frame_range[0]), int(frame_range[1])
    if hi - lo < 16:
        return {}
    curves = _bone_curves(action)
    rots = [fcs for (bone, prop), fcs in curves.items()
            if prop == "rotation_quaternion" and len(fcs) == 4]
    if not rots:
        return {}
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    table = _table(rots, lo, hi)
    start, cut = best_span(table, lo, hi)
    seam_before = _distance(table, start, cut)
    root_loc = curves.get((root.name, "location")) if root is not None else None
    if root_loc is not None and len(root_loc) != 3:
        root_loc = None
    if root_loc is not None:
        # Worked in armature space, not per channel: a root bone can rest
        # tilted (Cesium Man's, 4.6 degrees), and then "forward" in its own
        # axes also points a little down -- per channel, a walking loop sank
        # 8 cm a cycle. `rest` takes a location to armature space.
        rest = root.bone.matrix_local.to_3x3()
        where_it_began = rest @ Vector([fc.evaluate(lo) for fc in root_loc])

    # 1. Keep only the loop: drop what comes before its start and after its
    #    end, and slide it back so it begins where the block begins.
    shift = lo - start
    for fcs in curves.values():
        for fc in fcs:
            # By index, from the end: removing a point reallocates the rest,
            # and a reference held across a removal is dead.
            points = fc.keyframe_points
            for i in range(len(points) - 1, -1, -1):
                x = points[i].co.x
                if lo - 0.5 <= x < start - 0.5 or cut + 0.5 < x <= hi + 0.5:
                    points.remove(points[i], fast=True)
            if shift:
                for k in points:
                    if start - 0.5 <= k.co.x <= cut + 0.5:
                        k.co.x += shift
                        k.handle_left.x += shift
                        k.handle_right.x += shift
            fc.update()
    cut += shift

    # 1b. Straight ahead: turn the cycle so its travel points where the rig
    #     faces at rest. See _straighten.
    turned = _straighten(arm, root, curves, lo, cut) if root_loc is not None else 0.0

    # 2. Blend the leftover difference into the last frames, so frame `cut`
    #    is the first pose exactly and the wrap has nothing to jump over.
    k = max(4, min(BLEND_FRAMES, (cut - lo) // 3))
    for fcs in rots:
        q_lo, q_cut = _quat(fcs, lo), _quat(fcs, cut)
        if q_lo.dot(q_cut) < 0:
            q_cut.negate()
        delta = q_lo @ q_cut.inverted()
        for f in range(cut - k, cut + 1):
            w = _smooth((f - (cut - k)) / k)
            q = _quat(fcs, f)
            new = (Quaternion().slerp(delta, w) @ q).normalized()
            if new.dot(q) < 0:
                new.negate()
            for fc, value in zip(fcs, new):
                _set(fc, f, value)
    # The root: it starts where the character stood, and it closes the way
    # the loop will play (in place or travelling, below).
    if root_loc is not None:
        back = where_it_began - rest @ Vector([fc.evaluate(lo) for fc in root_loc])
        back.z = 0.0
        local_back = rest.inverted() @ back
        for f in range(lo, cut + 1):
            for fc, d in zip(root_loc, local_back):
                _set(fc, f, fc.evaluate(f) + d)

    # 3. Repeat past the end.
    for (bone, prop), fcs in curves.items():
        for fc in fcs:
            for mod in [m for m in fc.modifiers if m.type == 'CYCLES']:
                fc.modifiers.remove(mod)
            mod = fc.modifiers.new('CYCLES')
            mod.mode_before = mod.mode_after = 'REPEAT'
            fc.update()
    if root_loc is not None:
        set_root_mode(arm, action, in_place)
    return {"start": start, "cut": cut, "frames": cut - lo, "blend": k,
            "seam_deg_before": math.degrees(seam_before), "turned_deg": turned}


def _loop_span(fc):
    xs = [k.co.x for k in fc.keyframe_points]
    return (int(round(min(xs))), int(round(max(xs)))) if xs else (None, None)


def is_looped(arm, action) -> bool:
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    loc = _bone_curves(action).get((root.name, "location")) if root is not None else None
    return bool(loc) and any(m.type == 'CYCLES' for m in loc[0].modifiers)


def set_root_mode(arm, action, in_place) -> None:
    """Close the root's loop for how it will play: on the spot, or travelling.

    The two want different things of a root bone that rests tilted (Cesium
    Man's, 4.6 degrees, so its "forward" also points down):

    * **In place** pins the root's own X and Z and leaves its Y free, and on
      a tilted root Y is nearly vertical. So Y returns to where it started and
      simply repeats. Offset like the others, an in-place loop climbed 8 cm a
      cycle. X and Z keep their travel under the pin.
    * **Travelling** closes the height in armature space and repeats all three
      channels with offset, so each cycle adds travel on the ground alone.

    Called by apply(), and again when In place is toggled on a looped preview.
    """
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    loc = _bone_curves(action).get((root.name, "location")) if root is not None else None
    if not loc or len(loc) != 3:
        return
    lo, cut = _loop_span(loc[0])
    if lo is None or cut - lo < 8:
        return
    k = max(4, min(BLEND_FRAMES, (cut - lo) // 3))
    rest = root.bone.matrix_local.to_3x3()
    if in_place:
        # Only the channel In place leaves free. X and Z keep their travel,
        # pinned out of sight by the constraint, so turning In place off again
        # brings the walk back; closing them too erased it for good.
        gaps = [0.0, loc[1].evaluate(lo) - loc[1].evaluate(cut), 0.0]
    else:
        at = lambda f: rest @ Vector([fc.evaluate(f) for fc in loc])      # noqa: E731
        gaps = list(rest.inverted() @ Vector((0.0, 0.0, at(lo).z - at(cut).z)))
    for f in range(cut - k, cut + 1):
        w = _smooth((f - (cut - k)) / k)
        for fc, g in zip(loc, gaps):
            if abs(g) > 1e-7:
                _set(fc, f, fc.evaluate(f) + g * w)
    for i, fc in enumerate(loc):
        for mod in fc.modifiers:
            if mod.type == 'CYCLES':
                free = in_place and i == 1          # the channel In place does not pin
                mod.mode_before = mod.mode_after = 'REPEAT' if free else 'REPEAT_OFFSET'
        fc.update()


#: a cycle's heading is straightened only when it is off by less than this; a
#: bigger angle is taken to be meant (a diagonal walk), and left alone
STRAIGHTEN_LIMIT = math.radians(30.0)


def _straighten(arm, root, curves, lo, cut) -> float:
    """Turn the loop about the vertical so it travels straight ahead. Returns degrees.

    A generated walk veers a little: the model's hips swung round a centre
    6.6 degrees off straight, and the walk drifted 12 cm sideways a stride.
    Played once that passes; as a cycle it walks at a slant, and in place the
    character faces off to one side. The direction the cycle travels is
    turned onto the direction the rig faces at rest (its -Y, as every rig the
    addon imports does). The travel is still in the curves when In place is
    on, since that only pins it. Loops that hardly travel, like an idle, are
    left alone.
    """
    loc = curves.get((root.name, "location"))
    rot = curves.get((root.name, "rotation_quaternion"))
    if not loc or len(loc) != 3 or not rot or len(rot) != 4:
        return 0.0
    rest = root.bone.matrix_local.to_3x3()
    at = lambda f: rest @ Vector([fc.evaluate(f) for fc in loc])      # noqa: E731
    travel = at(cut) - at(lo)
    travel.z = 0.0
    if travel.length < 0.2:
        return 0.0
    angle = math.atan2(travel.x, -travel.y)       # 0 when travelling along -Y
    if abs(angle) > STRAIGHTEN_LIMIT or abs(angle) < math.radians(0.5):
        return 0.0
    # Travelling toward +X of -Y is a positive angle; turning back onto -Y is
    # clockwise, the negative of it.
    turn = Quaternion((0.0, 0.0, 1.0), -angle)
    rest_q = rest.to_quaternion()
    inv_rest, inv_rest_q = rest.inverted(), rest_q.inverted()
    pivot = at(lo)
    pivot.z = 0.0
    turn_m = turn.to_matrix()
    for f in range(lo, cut + 1):
        # Rotation: the root's orientation in armature space is rest @ basis.
        q = _quat(rot, f)
        new = (inv_rest_q @ turn @ rest_q @ q).normalized()
        if new.dot(q) < 0:
            new.negate()
        for fc, value in zip(rot, new):
            _set(fc, f, value)
        # Location: turned about the loop's starting spot on the ground.
        p = at(f)
        moved = pivot + turn_m @ (p - pivot)
        moved.z = p.z
        for fc, value in zip(loc, inv_rest @ moved):
            _set(fc, f, value)
    return math.degrees(angle)
