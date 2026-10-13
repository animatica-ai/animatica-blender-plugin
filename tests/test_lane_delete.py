"""Deleting from the Animatica lane: selected prompt blocks and the current
character's waypoint flags go, with the hovered flag or block as the fallback
when nothing is selected. Nothing off the lane is touched.

Each case builds a fresh scene: a rig as the current character with three
blocks and four flags, plus a flag made for another rig and a plain mesh.

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_lane_delete.py

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

from animatica_blender import timeline_operators, waypoints  # noqa: E402

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def build():
    """A fresh scene; returns (scene, rig, flags, other_flag, mesh, other_rig)."""
    scene = bpy.context.scene
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    scene.animatica.prompt_blocks.clear()

    def new_rig(name):
        rig = bpy.data.objects.new(name, bpy.data.armatures.new(name))
        scene.collection.objects.link(rig)
        return rig

    rig = new_rig("Rig")
    other_rig = new_rig("OtherRig")
    scene.animatica.target_armature = rig
    # Setting the target may have made a block of its own; start from three.
    scene.animatica.prompt_blocks.clear()
    for i, (fs, fe) in enumerate(((0, 40), (40, 80), (80, 120))):
        b = scene.animatica.prompt_blocks.add()
        b.prompt = f"block {i}"
        b.frame_start, b.frame_end = fs, fe
    scene.animatica.active_block_index = 0

    flags = [waypoints.create_marker(scene, f, (f * 0.1, 0.0), owner=rig) for f in (10, 30, 60, 90)]
    other_flag = waypoints.create_marker(scene, 20, (5.0, 5.0), owner=other_rig)
    mesh = bpy.data.objects.new("Cube", bpy.data.meshes.new("Cube"))
    scene.collection.objects.link(mesh)

    bpy.context.view_layer.update()
    deselect_all(scene)
    return scene, rig, flags, other_flag, mesh, other_rig


def deselect_all(scene):
    for o in scene.objects:
        o.select_set(False)
    for b in scene.animatica.prompt_blocks:
        b.selected = False


def case_flags_only():
    scene, rig, flags, other_flag, mesh, _ = build()
    n_blocks = len(scene.animatica.prompt_blocks)
    check("setup: current character owns the 4 flags", len(waypoints.waypoints(scene)) == 4,
          f"{len(waypoints.waypoints(scene))}")
    names = [o.name for o in flags]
    flags[0].select_set(True)
    flags[2].select_set(True)
    res = timeline_operators.delete_lane_selection(scene)
    check("1 two flags selected: returns ([], 2)", res == ([], 2), f"{res}")
    check("1 the two selected flags are gone",
          names[0] not in bpy.data.objects and names[2] not in bpy.data.objects)
    check("1 the other two flags stay", names[1] in bpy.data.objects and names[3] in bpy.data.objects)
    check("1 blocks untouched", len(scene.animatica.prompt_blocks) == n_blocks, f"{len(scene.animatica.prompt_blocks)}")


def case_block_and_flag():
    scene, rig, flags, other_flag, mesh, _ = build()
    n_blocks = len(scene.animatica.prompt_blocks)
    scene.animatica.prompt_blocks[1].selected = True
    scene.animatica.active_block_index = 2
    name = flags[1].name
    flags[1].select_set(True)
    res = timeline_operators.delete_lane_selection(scene)
    check("2 block + flag selected: returns (['block 1'], 1)", res == (["block 1"], 1), f"{res}")
    check("2 the block is gone", len(scene.animatica.prompt_blocks) == n_blocks - 1,
          f"{len(scene.animatica.prompt_blocks)}")
    check("2 the flag is gone", name not in bpy.data.objects)
    idx = scene.animatica.active_block_index
    check("2 active_block_index in range", 0 <= idx < len(scene.animatica.prompt_blocks), f"{idx}")


def case_fallback_pin():
    scene, rig, flags, other_flag, mesh, _ = build()
    n_blocks = len(scene.animatica.prompt_blocks)
    n_flags = len(waypoints.waypoints(scene))
    name = flags[2].name
    res = timeline_operators.delete_lane_selection(scene, fallback_block=0, fallback_pin=flags[2])
    check("3 nothing selected, fallback_pin: returns ([], 1)", res == ([], 1), f"{res}")
    check("3 only that flag is gone", name not in bpy.data.objects
          and len(waypoints.waypoints(scene)) == n_flags - 1)
    check("3 fallback_block ignored: blocks untouched", len(scene.animatica.prompt_blocks) == n_blocks)


def case_fallback_block():
    scene, rig, flags, other_flag, mesh, _ = build()
    n_blocks = len(scene.animatica.prompt_blocks)
    n_flags = len(waypoints.waypoints(scene))
    res = timeline_operators.delete_lane_selection(scene, fallback_block=1)
    check("4 nothing selected, fallback_block: returns (['block 1'], 0)", res == (["block 1"], 0), f"{res}")
    left = [b.prompt for b in scene.animatica.prompt_blocks]
    check("4 that block is gone, the others stay", left == ["block 0", "block 2"], f"{left}")
    check("4 flags untouched", len(waypoints.waypoints(scene)) == n_flags
          and len(scene.animatica.prompt_blocks) == n_blocks - 1)


def case_off_lane():
    scene, rig, flags, other_flag, mesh, other_rig = build()
    n_blocks = len(scene.animatica.prompt_blocks)
    n_flags = len(waypoints.waypoints(scene))
    for o in (mesh, rig, other_flag):
        o.select_set(True)
    res = timeline_operators.delete_lane_selection(scene)
    check("5 mesh, rig, other character's flag selected: returns ([], 0)", res == ([], 0), f"{res}")
    check("5 none of them deleted", all(o.name in bpy.data.objects for o in (mesh, rig, other_flag)))
    check("5 lane untouched", len(waypoints.waypoints(scene)) == n_flags
          and len(scene.animatica.prompt_blocks) == n_blocks)


def case_nothing():
    scene, rig, flags, other_flag, mesh, other_rig = build()
    n_blocks = len(scene.animatica.prompt_blocks)
    n_objs = len(bpy.data.objects)
    res = timeline_operators.delete_lane_selection(scene)
    check("6 nothing selected, no fallbacks: returns ([], 0)", res == ([], 0), f"{res}")
    check("6 nothing deleted", len(bpy.data.objects) == n_objs
          and len(scene.animatica.prompt_blocks) == n_blocks)


def main():
    case_flags_only()
    case_block_and_flag()
    case_fallback_pin()
    case_fallback_block()
    case_off_lane()
    case_nothing()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        FAILS.append("exception")
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
