# SPDX-License-Identifier: GPL-3.0-or-later
"""Hands that look like hands: a finger pose laid over every generation.

The model has no fingers. Its skeleton ends at the wrist, and every finger on
the rig comes back at rest: dead straight, the whole hand a flat plate. That
reads wrong on anyone standing still, and badly on anyone holding something.
A pistol grip under a flat palm is not being held.

So each hand gets a pose of its own, laid over the generated motion after the
bake. "Relaxed" is the slight curl of a hand at rest, "Grip" closes it around
a handle, and "Flat" leaves the fingers as generated. The keys are typed
GENERATED, so Reject strips them with the rest of the take.

The curl is worked out from each finger's own rest geometry, not from its
bone axes. Each bone turns about the axis that bends it toward the palm, so
a rig whose finger bones roll one way or the other curls the same.
"""

from __future__ import annotations

import math

import bpy
from mathutils import Quaternion, Vector

STYLES = [
    ('RELAXED', "Relaxed", "A slight natural curl, like a hand at rest"),
    ('GRIP', "Gripping", "Closed around a handle, such as a pistol, a sword or a rifle's grip"),
    ('FLAT', "Straight", "Fingers left as generated. The model has no fingers, so they stay straight"),
]

#: degrees per segment, knuckle outwards: metacarpal, then the three phalanges
CURL = {
    'RELAXED': {"finger": (2, 12, 14, 8), "thumb": (6, 8, 10)},
    'GRIP': {"finger": (8, 72, 88, 48), "thumb": (22, 30, 34)},
}
FINGERS = ("Index", "Middle", "Ring", "Pinky")


def _chains(arm, side):
    """``{name: [pose bones, knuckle outward]}`` for the four fingers and the thumb."""
    found = {}
    by_suffix = {pb.name.split(":")[-1]: pb for pb in arm.pose.bones}
    for finger in FINGERS + ("Thumb",):
        chain = []
        i = 1
        while f"{side}Hand{finger}{i}" in by_suffix:
            chain.append(by_suffix[f"{side}Hand{finger}{i}"])
            i += 1
        if chain:
            found[finger] = chain
    return found


def _palm_normal(arm, side, chains):
    """The palm's facing at rest (armature space), from the hand's own bones."""
    hand = next((pb for pb in arm.pose.bones if pb.name.split(":")[-1] == f"{side}Hand"), None)
    if hand is None or "Middle" not in chains or "Index" not in chains or "Pinky" not in chains:
        return None
    wrist = hand.bone.head_local
    along = (chains["Middle"][0].bone.tail_local - wrist).normalized()
    across = chains["Index"][0].bone.head_local - chains["Pinky"][0].bone.head_local
    normal = along.cross(across).normalized()
    # A T- or A-posed rig rests palm-down: the palm faces away from the head.
    up = Vector((0.0, 0.0, 1.0))
    if normal.dot(up) > 0:
        normal = -normal
    return normal


def curl_rotations(arm, side, style):
    """``{bone_name: Quaternion}`` basis rotations for *style* on *side*'s hand."""
    if style not in CURL:
        return {}
    chains = _chains(arm, side)
    normal = _palm_normal(arm, side, chains)
    if normal is None:
        return {}
    # The thumb does not fold into the palm like a finger: it closes across
    # it, toward the fingers. Folded like the others it stuck down like a claw.
    middle = chains.get("Middle")
    thumb_goal = (middle[1].bone.head_local + normal * 0.02) if middle and len(middle) > 1 else None
    out = {}
    for finger, chain in chains.items():
        angles = CURL[style]["thumb" if finger == "Thumb" else "finger"]
        for pb, deg in zip(chain, angles):
            d = (pb.bone.tail_local - pb.bone.head_local).normalized()
            if finger == "Thumb" and thumb_goal is not None:
                axis = d.cross((thumb_goal - pb.bone.head_local).normalized())
            else:
                axis = d.cross(normal)               # turning d toward the palm
            if axis.length < 1e-6:
                continue
            local_axis = pb.bone.matrix_local.to_3x3().inverted() @ axis.normalized()
            out[pb.name] = Quaternion(local_axis.normalized(), math.radians(deg))
    return out


def rotations(arm, settings):
    """Both hands, as the scene asks for them."""
    out = {}
    for side, style in (("Left", settings.hand_pose_left), ("Right", settings.hand_pose_right)):
        out.update(curl_rotations(arm, side, style))
    return out


def show(arm, settings):
    """Put the hands in their pose now, without keying: what a file opens with."""
    for side, style in (("Left", settings.hand_pose_left), ("Right", settings.hand_pose_right)):
        rots = curl_rotations(arm, side, style) if style != 'FLAT' else {
            pb.name: Quaternion() for chain in _chains(arm, side).values() for pb in chain}
        for name, q in rots.items():
            pb = arm.pose.bones[name]
            if pb.rotation_mode == 'QUATERNION':
                pb.rotation_quaternion = q


def apply(arm, action, settings, frame_range) -> int:
    """Key the hand pose on every frame of *frame_range* in *action*. Returns bones keyed."""
    from . import constraints_ui

    if arm is None or action is None:
        return 0
    rots = rotations(arm, settings)
    if not rots:
        return 0
    lo, hi = int(frame_range[0]), int(frame_range[1])
    curves = {}
    for fc in constraints_ui.iter_action_fcurves(action):
        if fc.data_path.startswith('pose.bones["') and fc.data_path.endswith('"].rotation_quaternion'):
            curves[(fc.data_path[12:-len('"].rotation_quaternion')], fc.array_index)] = fc
    keyed = 0
    for name, q in rots.items():
        if arm.pose.bones[name].rotation_mode != 'QUATERNION':
            continue
        fcs = []
        for i in range(4):
            fc = curves.get((name, i))
            if fc is None:
                fc = _new_curve(arm, action, name, i)
            fcs.append(fc)
        for fc, value in zip(fcs, q):
            _fill(fc, lo, hi, value)
            fc.update()
        keyed += 1
    return keyed


def _new_curve(arm, action, bone, index):
    path = f'pose.bones["{bone}"].rotation_quaternion'
    ad = arm.animation_data
    fcs = getattr(action, "fcurves", None)
    if fcs is not None:
        return fcs.new(path, index=index, action_group=bone)
    from . import gltf_to_blender
    container = gltf_to_blender._action_fcurves_container(arm, action)
    return container.new(path, index=index)


def _fill(fc, lo, hi, value):
    """Every frame in [lo, hi] at *value*, typed GENERATED so Reject takes it away."""
    have = {int(round(k.co.x)): k for k in fc.keyframe_points}
    for f in range(lo, hi + 1):
        k = have.get(f)
        if k is not None:
            d = value - k.co.y
            k.co.y = value
            k.handle_left.y += d
            k.handle_right.y += d
        else:
            k = fc.keyframe_points.insert(f, value, options={'FAST'})
            k.type = 'GENERATED'
