"""Redo of one block keeps the route: the waypoints inside the block go out
with the request, on the block's own frames, and the ones outside it don't.

A rig with one keyed bone stands in for the take's preview; a block over
frames 10-50; waypoints at 20 and 50 (inside) and 70 (outside).

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_block_regen_waypoints.py

``--factory-startup`` keeps an installed copy of the addon from shadowing
the checkout.
"""

from __future__ import annotations

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=False)

from animatica_blender import constraints_ui, request_builder, waypoints  # noqa: E402

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def build():
    scene = bpy.context.scene
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    rig = bpy.data.objects.new("Rig", bpy.data.armatures.new("Rig"))
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    b = rig.data.edit_bones.new("Hips")
    b.head, b.tail = (0, 0, 1.0), (0, 0, 1.2)
    bpy.ops.object.mode_set(mode='OBJECT')
    pb = rig.pose.bones["Hips"]
    for f in range(1, 81):                      # the take's preview, every frame
        pb.keyframe_insert("rotation_quaternion", frame=f)
    for fc in constraints_ui.iter_action_fcurves(rig.animation_data.action):
        for kp in fc.keyframe_points:
            kp.type = 'GENERATED'
    s = scene.animatica
    s.target_armature = rig
    s.prompt_blocks.clear()
    blk = s.prompt_blocks.add()
    blk.prompt, blk.frame_start, blk.frame_end = "walks", 10, 50
    for f, x in ((20, 1.0), (50, 2.0), (70, 3.0)):
        waypoints.create_marker(scene, f, (x, 0.0), owner=rig)
    bpy.context.view_layer.update()
    return scene, rig


def main():
    scene, rig = build()
    s = scene.animatica
    req, rng = request_builder.build_request_for_block(
        block_index=0, model_id="test", model_caps={"supports_retargeting": True},
        armature_obj=rig, prompt_blocks=s.prompt_blocks, settings=s, scene=scene,
        constraint_objects=constraints_ui.walk_scene_constraints(scene),
        preview_action=rig.animation_data.action, source_action=None,
    )
    roots = [c for c in req.get("constraints", []) if c["type"] == "root_path"]
    check("the block's range", rng == (10, 50), rng)
    check("the two waypoints inside the block go out", len(roots) == 2, len(roots))
    check("...on the block's own frames", sorted(c["frames"][0] for c in roots) == [10, 40],
          [c["frames"] for c in roots])
    check("the waypoint after the block stays out",
          all(c["frames"][0] <= 40 for c in roots))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        FAILS.append("exception")
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
