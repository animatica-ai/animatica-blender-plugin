"""The waypoint gizmo's maths: the ground hit under the cursor, the distance to
a ring on screen, the group drag. No scene needed.

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_waypoint_gizmo.py

``--factory-startup`` keeps an installed copy of the addon from shadowing
the checkout.
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
from mathutils import Vector  # noqa: E402

from animatica_blender import waypoint_gizmo, waypoints  # noqa: E402

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def test_ground_hit():
    hit = waypoint_gizmo.ground_hit(Vector((0, 0, 5)), Vector((0, 0, -1)), 0.0)
    check("a ray straight down hits the ground under it",
          hit is not None and (hit - Vector((0, 0, 0))).length < 1e-6, hit)
    check("a horizontal ray never hits",
          waypoint_gizmo.ground_hit(Vector((0, 0, 5)), Vector((1, 0, 0)), 0.0) is None)
    check("a ray pointing up, plane below: behind the camera",
          waypoint_gizmo.ground_hit(Vector((0, 0, 5)), Vector((0, 0, 1)), 0.0) is None)


def test_ring_distance():
    center = (100.0, 100.0)
    ring = [(center[0] + 50 * math.cos(2 * math.pi * i / 64),
             center[1] + 50 * math.sin(2 * math.pi * i / 64)) for i in range(64)]
    on = waypoint_gizmo.ring_distance_px(center, ring, (150.0, 100.0))
    check("a cursor on the ring is ~0 px away", on < 1e-6, on)
    mid = waypoint_gizmo.ring_distance_px(center, ring, center)
    check("a cursor at the centre of a 50 px ring is ~50 px away", abs(mid - 50.0) < 0.5, mid)


def test_apply_drag():
    out = waypoint_gizmo.apply_drag({"A": (1.0, 1.0), "B": (-3.0, 4.0)}, (1.0, -2.0))
    check("both markers move by the delta",
          out == {"A": (2.0, -1.0), "B": (-2.0, 2.0)}, out)


def build_rig():
    """An armature with one root bone, the scene's character."""
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    arm_data = bpy.data.armatures.new("Rig")
    arm = bpy.data.objects.new("Rig", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    b = arm_data.edit_bones.new("Hips")
    b.head, b.tail = (0, 0, 0.99), (0, 0, 1.2)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.scene.animatica.target_armature = arm
    return arm


def test_add_in_pose_mode():
    arm = build_rig()
    scene = bpy.context.scene
    bpy.ops.object.mode_set(mode='POSE')
    scene.frame_set(10)
    bpy.ops.animatica.add_waypoint()
    marker = waypoints.at_frame(scene, 10, arm)
    check("adding in pose mode keeps the rig active and posing",
          bpy.context.view_layer.objects.active == arm and arm.mode == 'POSE',
          (bpy.context.view_layer.objects.active, arm.mode))
    check("adding in pose mode selects the new marker",
          marker is not None and marker.select_get(), marker)


def test_add_in_object_mode():
    arm = bpy.context.scene.animatica.target_armature
    scene = bpy.context.scene
    bpy.ops.object.mode_set(mode='OBJECT')
    scene.frame_set(20)
    bpy.ops.animatica.add_waypoint()
    marker = waypoints.at_frame(scene, 20, arm)
    check("adding in object mode makes the new marker active",
          marker is not None and bpy.context.view_layer.objects.active == marker,
          bpy.context.view_layer.objects.active)


def test_registered():
    # Python gizmo classes don't show up in bpy.types; registration sets bl_rna.
    g = waypoint_gizmo
    check("the gizmo classes are registered",
          all('bl_rna' in c.__dict__ for c in (g.ANIMATICA_GGT_waypoints, g.ANIMATICA_GT_waypoint)))


def main():
    try:
        import addon_utils
        addon_utils.enable("animatica_blender", default_set=False)
        test_ground_hit()
        test_ring_distance()
        test_apply_drag()
        test_add_in_pose_mode()
        test_add_in_object_mode()
        test_registered()
    except Exception:
        traceback.print_exc()
        FAILS.append("crashed")
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    sys.exit(1 if FAILS else 0)


main()
