# SPDX-License-Identifier: GPL-3.0-or-later
"""Play a sampled cycle as a loop: a walk cycle, an idle, a run for a game.

Loop is the model's to make. A server that advertises ``supports_loop``
(motionmcp 0.4) is asked for ``options.loop`` and samples the block as a
cycle: its last frame is its first again, moved on by one cycle's travel, and
the wrap moves like any other frame. Where the model cannot, there is no Loop
-- a clip made with a start and an end does not come round, and cutting and
blending one after the fact left seams that showed (a march, a boxer's
bounce). What is left to do after the bake:

1. **Straighten.** A generated walk veers a little; the cycle is turned to
   travel straight ahead (see _straighten).
2. **Start where the character stood.** The root is moved back to its spot.
3. **Repeat.** Cycles modifiers on the curves play the loop past its end in
   Blender. The root's travel repeats with offset, so a walking loop keeps
   walking forward rather than snapping back, and In place holds it on the
   spot (see close_root).

Keys keep their GENERATED type, so Reject still strips the take.
"""

from __future__ import annotations

import math

from mathutils import Quaternion, Vector


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


def _seam(rots, a, b):
    """Summed joint rotation difference between frames *a* and *b*, degrees."""
    return math.degrees(sum(2.0 * math.acos(min(1.0, abs(_quat(fcs, a).dot(_quat(fcs, b)))))
                            for fcs in rots))


def _set(fc, frame, value):
    for k in fc.keyframe_points:
        if abs(k.co.x - frame) < 0.5:
            d = value - k.co.y
            k.co.y = value
            k.handle_left.y += d
            k.handle_right.y += d
            return


def apply(arm, action, frame_range) -> dict:
    """Play the cycle in *action* over *frame_range* as a loop. Returns what was done.

    The clip is one the server sampled as a cycle (``options.loop``): frame
    ``hi`` is frame ``lo`` again. The cycle is ``hi - lo`` frames.
    """
    if arm is None or action is None:
        return {}
    lo, cut = int(frame_range[0]), int(frame_range[1])
    if cut - lo < 8:
        return {}
    curves = _bone_curves(action)
    rots = [fcs for (bone, prop), fcs in curves.items()
            if prop == "rotation_quaternion" and len(fcs) == 4]
    if not rots:
        return {}
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    root_loc = curves.get((root.name, "location")) if root is not None else None
    if root_loc is not None and len(root_loc) != 3:
        root_loc = None
    turned = 0.0
    if root_loc is not None:
        # Worked in world space, not per channel: a root bone can rest
        # tilted (Cesium Man's, 4.6 degrees), and then "forward" in its own
        # axes also points a little down. `to_world` takes a location offset
        # to world space.
        to_world = arm.matrix_world.to_3x3() @ root.bone.matrix_local.to_3x3()
        where_it_began = to_world @ Vector([fc.evaluate(lo) for fc in root_loc])

        # 1. Straight ahead: turn the cycle so its travel points where the rig
        #    faces at rest. See _straighten.
        turned = _straighten(arm, root, curves, lo, cut)

        # 2. It starts where the character stood.
        back = where_it_began - to_world @ Vector([fc.evaluate(lo) for fc in root_loc])
        back.z = 0.0
        local_back = to_world.inverted() @ back
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
        close_root(arm, action)
    done = {"start": lo, "cut": cut, "frames": cut - lo,
            "seam_deg": _seam(rots, lo, cut), "turned_deg": turned}
    # Kept on the action: the Preview box says what the loop is, and it
    # travels with the take if it is accepted.
    action["animatica_loop"] = {k: float(v) for k, v in done.items()}
    return done


def _loop_span(fc):
    xs = [k.co.x for k in fc.keyframe_points]
    return (int(round(min(xs))), int(round(max(xs)))) if xs else (None, None)


def close_root(arm, action) -> None:
    """Close the root's height over the loop, and repeat its travel with offset.

    In world space: the height is made the same at both ends of the loop, and
    all three location channels repeat with offset, so each cycle adds travel
    on the ground and nothing else. The same loop then works travelling and in
    place (In place pins the root's world X and Y and keeps its height), and
    on a root bone that rests tilted, where one of its own channels carries
    both travel and height.
    """
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    loc = _bone_curves(action).get((root.name, "location")) if root is not None else None
    if not loc or len(loc) != 3:
        return
    lo, cut = _loop_span(loc[0])
    if lo is None or cut - lo < 8:
        return
    to_world = arm.matrix_world.to_3x3() @ root.bone.matrix_local.to_3x3()
    at = lambda f: to_world @ Vector([fc.evaluate(f) for fc in loc])      # noqa: E731
    # The server closes the height already; what a bake leaves is spread over
    # the cycle, a share a frame, rather than put into the last frames.
    gaps = list(to_world.inverted() @ Vector((0.0, 0.0, at(lo).z - at(cut).z)))
    for f in range(lo, cut + 1):
        w = (f - lo) / (cut - lo)
        for fc, g in zip(loc, gaps):
            if abs(g) > 1e-9:
                _set(fc, f, fc.evaluate(f) + g * w)
    for fc in loc:
        for mod in fc.modifiers:
            if mod.type == 'CYCLES':
                mod.mode_before = mod.mode_after = 'REPEAT_OFFSET'
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
    # World space throughout: the ground is world XY whatever the armature
    # object is turned to (an imported Mixamo rig stands rotated 90 degrees).
    to_world = arm.matrix_world.to_3x3() @ root.bone.matrix_local.to_3x3()
    from_world = to_world.inverted()
    at = lambda f: to_world @ Vector([fc.evaluate(f) for fc in loc])      # noqa: E731
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
    frame_q = arm.matrix_world.to_quaternion() @ root.bone.matrix_local.to_quaternion()
    frame_q_inv = frame_q.inverted()
    pivot = at(lo)
    pivot.z = 0.0
    turn_m = turn.to_matrix()
    for f in range(lo, cut + 1):
        # Rotation: the root's world orientation is frame_q @ basis.
        q = _quat(rot, f)
        new = (frame_q_inv @ turn @ frame_q @ q).normalized()
        if new.dot(q) < 0:
            new.negate()
        for fc, value in zip(rot, new):
            _set(fc, f, value)
        # Location: turned about the loop's starting spot on the ground.
        p = at(f)
        moved = pivot + turn_m @ (p - pivot)
        moved.z = p.z
        for fc, value in zip(loc, from_world @ moved):
            _set(fc, f, value)
    return math.degrees(angle)
