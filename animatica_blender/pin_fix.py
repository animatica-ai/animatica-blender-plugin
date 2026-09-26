# SPDX-License-Identifier: GPL-3.0-or-later
"""A pinned hand or foot ends up on its pin, whatever the server did.

The server honours an effector pin in its own canonical space, then
retargets the motion onto this rig. For a rig with different proportions
that moves the hand: the retarget gives hands half reach so body-relative
placement survives longer or shorter arms. On the Animatica Hero a pinned
wrist came back 6-11 cm beside its pin, which is the difference between
pressing a button and waving at it.

So after the bake, each pin is finished here, on the rig it is for. At each
pinned frame a two-bone IK (upper arm and forearm, or thigh and shin) puts
the limb's end on the pin. The elbow or knee stays on the side it already
bent toward, and the hand or foot keeps its orientation in the world. The
change fades in and out over the frames around the pin, so the limb arrives
rather than snaps. A server that already lands the pin leaves nothing to
correct, and nothing changes.
"""

from __future__ import annotations

import math

import bpy
from mathutils import Quaternion, Vector

#: frames either side of a pin over which the correction fades in and out
EASE_FRAMES = 8


def _pins(scene, arm, lo, hi):
    """``[(end_pose_bone, [(frame, armature_space_target), ...]), ...]``."""
    from . import constants, constraints_ui

    inv = arm.matrix_world.inverted()
    out = []
    for ob in scene.objects:
        joint = ob.get(constants.PROP_TARGET_JOINT)
        if not joint or ob.animation_data is None or ob.animation_data.action is None:
            continue
        pb = arm.pose.bones.get(joint) or constraints_ui.resolve_effector_bone(arm, joint)
        if pb is None:
            continue
        frames = sorted({int(round(k.co.x))
                         for fc in constraints_ui.iter_action_fcurves(ob.animation_data.action)
                         for k in fc.keyframe_points})
        keys = []
        for f in frames:
            if lo <= f <= hi:
                loc = Vector([_eval(ob, "location", i, f) for i in range(3)])
                world = (loc if ob.parent is None
                         else ob.parent.matrix_world @ ob.matrix_parent_inverse @ loc)
                keys.append((f, inv @ world))
        if keys:
            out.append((pb, keys))
    return out


def _eval(ob, path, index, frame):
    from . import constraints_ui

    for fc in constraints_ui.iter_action_fcurves(ob.animation_data.action):
        if fc.data_path == path and fc.array_index == index:
            return fc.evaluate(frame)
    return getattr(ob, path)[index]


def _rot(pb):
    return pb.matrix.to_quaternion()


def _rest_rel(pb):
    """Rest rotation of *pb* relative to its parent's rest (armature space)."""
    rest = pb.bone.matrix_local.to_quaternion()
    if pb.parent is None:
        return rest
    return pb.parent.bone.matrix_local.to_quaternion().inverted() @ rest


def _basis(pb, pose_q, parent_pose_q):
    """The basis rotation that gives *pb* the armature-space rotation *pose_q*."""
    return (_rest_rel(pb).inverted() @ parent_pose_q.inverted() @ pose_q).normalized()


def _two_bone(a, b, c, t):
    """New elbow and end positions reaching for *t*, elbow kept on its side."""
    l1, l2 = (b - a).length, (c - b).length
    d_vec = t - a
    d = min(max(d_vec.length, abs(l1 - l2) + 1e-4), l1 + l2 - 1e-4)
    u = d_vec.normalized() if d_vec.length > 1e-9 else (c - a).normalized()
    pole = b - (a + c) / 2
    side = pole - pole.dot(u) * u
    if side.length < 1e-9:
        side = u.orthogonal()
    side.normalize()
    cos_a = min(max((l1 * l1 + d * d - l2 * l2) / (2 * l1 * d), -1.0), 1.0)
    b2 = a + l1 * (cos_a * u + (1 - cos_a * cos_a) ** 0.5 * side)
    return b2, a + d * u


def _limb_deltas(end, target):
    """Basis-rotation deltas ``{bone_name: Quaternion}`` putting *end* on *target* now."""
    mid = end.parent
    top = mid.parent if mid is not None else None
    if mid is None or top is None:
        return {}
    a, b, c = top.head.copy(), mid.head.copy(), end.head.copy()
    b2, c2 = _two_bone(a, b, c, target)
    r1 = (b - a).rotation_difference(b2 - a)
    r2 = (r1 @ (c - b)).rotation_difference(c2 - b2)
    top_q, mid_q, end_q = _rot(top), _rot(mid), _rot(end)
    new_top = (r1 @ top_q).normalized()
    new_mid = (r2 @ r1 @ mid_q).normalized()
    parent_q = _rot(top.parent) if top.parent is not None else Quaternion()
    new = {
        top: _basis(top, new_top, parent_q),
        mid: _basis(mid, new_mid, new_top),
        end: _basis(end, end_q, new_mid),       # the hand keeps its world orientation
    }
    return {pb.name: (pb.rotation_quaternion.inverted() @ q).normalized() for pb, q in new.items()}


def apply(arm, action, scene, frame_range) -> list:
    """Correct every pin in *frame_range* on *action*. Returns ``[(joint, frame, cm_before)]``."""
    from . import constraints_ui, request_builder

    if arm is None or action is None or request_builder.is_control_rig(arm):
        return []
    lo, hi = int(frame_range[0]), int(frame_range[1])
    pins = _pins(scene, arm, lo, hi)
    if not pins:
        return []
    keep = scene.frame_current
    report = []
    # All deltas measured on the motion as generated, then applied together:
    # correcting one pin must not change what the next one is measured from.
    weights: dict = {}        # (bone, frame) -> (weight, delta)
    try:
        for end, keys in pins:
            chain = [end, end.parent, end.parent.parent if end.parent else None]
            if any(pb is None or pb.rotation_mode != 'QUATERNION' for pb in chain):
                continue
            for f, target in keys:
                scene.frame_set(f)
                bpy.context.view_layer.update()
                report.append((end.name, f, round((end.head - target).length * 100, 1)))
                for name, delta in _limb_deltas(end, target).items():
                    for g in range(max(lo, f - EASE_FRAMES), min(hi, f + EASE_FRAMES) + 1):
                        w = 0.5 * (1 + math.cos(math.pi * abs(g - f) / (EASE_FRAMES + 1)))
                        if w > weights.get((name, g), (0.0, None))[0]:
                            weights[(name, g)] = (w, delta)
    finally:
        scene.frame_set(keep)

    curves = {}
    for fc in constraints_ui.iter_action_fcurves(action):
        if fc.data_path.endswith(".rotation_quaternion") and fc.data_path.startswith('pose.bones["'):
            curves[(fc.data_path[12:-len('"].rotation_quaternion')], fc.array_index)] = fc
    touched = set()
    for (name, g), (w, delta) in weights.items():
        fcs = [curves.get((name, i)) for i in range(4)]
        if any(fc is None for fc in fcs):
            continue
        q = Quaternion([fc.evaluate(g) for fc in fcs])
        step = Quaternion().slerp(delta, w)
        new = (q @ step).normalized()
        if new.dot(q) < 0:                       # stay on the curve's side of the double cover
            new.negate()
        for fc, value in zip(fcs, new):
            _set_key(fc, g, value)
            touched.add(fc)
    for fc in touched:
        fc.update()
    return report


def _set_key(fc, frame, value):
    """Move the key at *frame* to *value*, keeping its type (Reject strips GENERATED ones)."""
    for kp in fc.keyframe_points:
        if abs(kp.co.x - frame) < 0.5:
            d = value - kp.co.y
            kp.co.y = value
            kp.handle_left.y += d
            kp.handle_right.y += d
            return
    kp = fc.keyframe_points.insert(frame, value, options={'FAST'})
    kp.type = 'GENERATED'
