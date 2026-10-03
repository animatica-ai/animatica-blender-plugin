"""An Autoposer edit as the rig shows it: the frame edited is the solve, and a
hand or foot put back in place keeps its elbow or knee a hinge.

Both broke poses. The edited frame was the pose the drag started from with the
solve's change laid over it, bone by bone -- on a pose the poser re-solves
differently, a body no solve had made (wrists bent back, knees the wrong way,
the key then carried into the take generated through it). And the two-bone
reach that holds hands and feet turned the lower bone the shortest way onto its
new spot, bending the elbow sideways.

A small leg and arm on a spine, no model needed. Runs headless::

    blender -b --factory-startup --python tests/test_carry_pose.py
"""

from __future__ import annotations

import math
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bpy  # noqa: E402
from mathutils import Euler, Matrix, Quaternion, Vector  # noqa: E402

from animatica_blender import carry  # noqa: E402

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


BONES = (  # name, parent, head, tail
    ("Hips", None, (0, 0, 1.0), (0, 0, 1.1)),
    ("Spine", "Hips", (0, 0, 1.1), (0, 0, 1.5)),
    ("LeftArm", "Spine", (0.2, 0, 1.45), (0.48, 0, 1.45)),
    ("LeftForeArm", "LeftArm", (0.48, 0, 1.45), (0.74, 0, 1.45)),
    ("LeftHand", "LeftForeArm", (0.74, 0, 1.45), (0.82, 0, 1.45)),
    ("LeftLeg", "Hips", (0.1, 0, 1.0), (0.1, 0, 0.55)),
    ("LeftShin", "LeftLeg", (0.1, 0, 0.55), (0.1, 0, 0.1)),
    ("LeftFoot", "LeftShin", (0.1, 0, 0.1), (0.1, -0.15, 0.0)),
)


def build():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    data = bpy.data.armatures.new("Rig")
    arm = bpy.data.objects.new("Rig", data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    for name, parent, head, tail in BONES:
        eb = data.edit_bones.new(name)
        eb.head, eb.tail = Vector(head), Vector(tail)
        if parent:
            eb.parent = data.edit_bones[parent]
            eb.use_connect = (Vector(head) - data.edit_bones[parent].tail).length < 1e-6
    bpy.ops.object.mode_set(mode='POSE')
    for pb in arm.pose.bones:
        pb.rotation_mode = 'QUATERNION'
    return arm


def rot(axis, deg):
    return Quaternion(Vector(axis), math.radians(deg)).to_matrix().to_4x4()


def swing_off_hinge(basis, hinge_axis):
    """How far (deg) a bone's bend leans off ``hinge_axis`` (its own frame): the
    swing of its rotation, its twist about its length taken out."""
    q = basis.to_quaternion()
    if q.w < 0:
        q.negate()
    tw = Quaternion((q.w, 0.0, q.y, 0.0))
    tw.normalize()
    sw = q @ tw.inverted()
    if math.degrees(sw.angle) < 1e-3:
        return 0.0
    return math.degrees(sw.axis.angle(Vector(hinge_axis)))


def test_reach_keeps_the_hinge(arm):
    rig = carry.Rig(arm)
    bases = {pb.name: Matrix.Identity(4) for pb in arm.pose.bones}
    bases["LeftForeArm"] = rot((0, 0, 1), 50)          # the elbow bent on its hinge (Z here)
    bases["LeftArm"] = rot((1, 0, 0), -30)
    mats = rig.fk(bases)
    keep = mats["LeftHand"].to_3x3()
    # a long way off: up, forward and in, as a hand carried through time goes
    target = mats["LeftHand"].translation + Vector((-0.12, -0.18, 0.15))
    ok = carry.reach(rig, bases, "LeftHand", target)
    mats = rig.fk(bases)
    check("reach puts the hand on its target", ok and (mats["LeftHand"].translation - target).length < 1e-4,
          f"{(mats['LeftHand'].translation - target).length * 100:.3f} cm")
    off = swing_off_hinge(bases["LeftForeArm"], (0, 0, 1))
    check("the elbow still bends on its hinge", off < 0.5, f"{off:.2f} deg off it")
    turned = math.degrees((mats["LeftHand"].to_3x3().to_quaternion().rotation_difference(keep.to_quaternion())).angle)
    check("the hand keeps its orientation", turned < 0.05, f"{turned:.3f} deg")
    # a knee: closer in, so it bends more, still forward on its hinge (X)
    bases = {pb.name: Matrix.Identity(4) for pb in arm.pose.bones}
    bases["LeftShin"] = rot((1, 0, 0), 20)
    mats = rig.fk(bases)
    target = mats["LeftFoot"].translation + Vector((0.08, 0.05, 0.2))
    carry.reach(rig, bases, "LeftFoot", target)
    mats = rig.fk(bases)
    off = swing_off_hinge(bases["LeftShin"], (1, 0, 0))
    q = bases["LeftShin"].to_quaternion()
    check("the knee bends more, on its hinge, the same way", off < 0.5 and q.x * (1 if q.w >= 0 else -1) > 0,
          f"{off:.2f} deg off it, {math.degrees(q.angle):.1f} deg bend")
    check("the foot reaches", (mats["LeftFoot"].translation - target).length < 1e-4)


def test_edited_frame_is_the_solve(arm):
    ctx = bpy.context
    names = [pb.name for pb in arm.pose.bones]
    ident = {n: Matrix.Identity(4) for n in names}
    # the pose the drag starts from, and the poser's own solve of it: not the same body
    before = dict(ident)
    before["Spine"] = rot((1, 0, 0), 30)
    before["LeftArm"] = rot((0, 1, 0), 40)
    base = dict(ident)
    base["Spine"] = rot((1, 0, 0), 5)
    base["LeftArm"] = rot((0, 0, 1), -20)
    # a drag well under way: the solve has the arm up and the hips moved
    after = dict(base)
    after["LeftArm"] = rot((0, 0, 1), 70)
    after["Hips"] = Matrix.Translation((0.0, 0.0, 0.25))
    c = carry.Carry(ctx, arm, 1, 0, before)
    c.set_base(base)
    c.set_after(after)
    shown = c.pose_at(c.f0)
    worst = max((shown[n].to_quaternion().rotation_difference(after[n].to_quaternion()).angle for n in names), default=0)
    moved = max(((shown[n].translation - after[n].translation).length for n in names), default=0)
    check("the edited frame is the Autoposer's pose", math.degrees(worst) < 0.01 and moved < 1e-5,
          f"{math.degrees(worst):.3f} deg, {moved * 100:.4f} cm off it")
    # the first touch of a drag: barely moved, so the pose stays the one it was (nothing pops in)
    tiny = dict(base)
    tiny["Hips"] = Matrix.Translation((0.0, 0.0, 0.004))
    c = carry.Carry(ctx, arm, 1, 0, before)
    c.set_base(base)
    c.set_after(tiny)
    shown = c.pose_at(c.f0)
    rig = carry.Rig(arm)
    fs, fb = rig.fk(shown), rig.fk(before)
    jump = max((fs[n].translation - fb[n].translation).length for n in names)
    check("the first touch does not pop to the poser's pose", jump < 0.006, f"{jump * 100:.2f} cm moved")


def main():
    arm = build()
    for t in (test_reach_keeps_the_hinge, test_edited_frame_is_the_solve):
        try:
            t(arm)
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append(t.__name__)
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    sys.exit(1 if FAILS else 0)


main()
