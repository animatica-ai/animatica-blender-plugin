"""A rig with several root bones sends one skeleton tree: its body.

A game rip (Source2Viewer's Deadlock characters, for one) has the body under
``pelvis`` and loose bones beside it at the top: a weapon attachment, a cloth
strand. Every bone deforms, so no control-rig filter merges them, and each
went out as a root. The schema takes exactly one, so the server refused the
whole request: "Malformed MMCP generate request: 1 problem(s)".

The rig here is made that way, the weapon bone first so that "the first bone
without a parent" is the wrong answer. The request carries the pelvis tree
only, the pose keys and the start anchor speak of the same tree, and a rig
with one root sends what it always did.

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_multi_root_skeleton.py

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

from animatica_blender import constraints_ui, coords, request_builder  # noqa: E402

FAILS: list[str] = []

BODY = ["pelvis", "spine_0", "head", "leg_upper_L", "leg_lower_L", "leg_upper_R", "leg_lower_R"]

# name, parent, head, tail -- in the order they are made
GAME_RIP = [
    ("weapon_attach", None,          (0.6, -0.3, 1.0), (0.6, -0.3, 1.2)),
    ("pelvis",        None,          (0.0, 0.0, 1.0),  (0.0, 0.0, 1.1)),
    ("spine_0",       "pelvis",      (0.0, 0.0, 1.1),  (0.0, 0.0, 1.4)),
    ("head",          "spine_0",     (0.0, 0.0, 1.4),  (0.0, 0.0, 1.7)),
    ("leg_upper_L",   "pelvis",      (0.1, 0.0, 1.0),  (0.1, 0.0, 0.5)),
    ("leg_lower_L",   "leg_upper_L", (0.1, 0.0, 0.5),  (0.1, 0.0, 0.05)),
    ("leg_upper_R",   "pelvis",      (-0.1, 0.0, 1.0), (-0.1, 0.0, 0.5)),
    ("leg_lower_R",   "leg_upper_R", (-0.1, 0.0, 0.5), (-0.1, 0.0, 0.05)),
    ("cloth_0",       None,          (0.0, 0.2, 1.2),  (0.0, 0.2, 0.9)),
    ("cloth_1",       "cloth_0",     (0.0, 0.2, 0.9),  (0.0, 0.2, 0.6)),
]


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def make_rig(name, bones, *, no_deform=(), location=(0.0, 0.0, 0.0)):
    scene = bpy.context.scene
    rig = bpy.data.objects.new(name, bpy.data.armatures.new(name))
    rig.location = location
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for bone, parent, head, tail in bones:
        eb = rig.data.edit_bones.new(bone)
        eb.head, eb.tail = head, tail
        eb.use_deform = bone not in no_deform
        if parent:
            eb.parent = rig.data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.update()
    return rig


def clear():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)


def one_tree(joints) -> str:
    """What the server's ``Skeleton`` check would say about ``joints``, or ''."""
    names = [j["name"] for j in joints]
    if len(set(names)) != len(names):
        return "joint names must be unique"
    roots = [j["name"] for j in joints if j["parent"] is None]
    if len(roots) != 1:
        return f"exactly one joint must have parent=null; got {len(roots)}: {roots}"
    seen: set[str] = set()
    for j in joints:
        if j["parent"] is not None and j["parent"] not in seen:
            return f"{j['name']!r}'s parent {j['parent']!r} comes later or not at all"
        seen.add(j["name"])
    return ""


def full_request(scene, rig):
    """A Generate over frames 1-60 with a pose keyed at frame 10 on the spine
    and on the weapon bone, the way ``build_request`` makes it."""
    for bone in ("spine_0", "weapon_attach"):
        pb = rig.pose.bones[bone]
        pb.rotation_quaternion = (0.966, 0.259, 0.0, 0.0)
        pb.keyframe_insert("rotation_quaternion", frame=10)
    s = scene.animatica
    s.target_armature = rig
    s.prompt_blocks.clear()
    blk = s.prompt_blocks.add()
    blk.prompt, blk.frame_start, blk.frame_end = "walks forward", 1, 60
    return request_builder.build_request(
        model_id="test", model_caps={"supports_retargeting": True},
        armature_obj=rig, prompt_blocks=s.prompt_blocks, settings=s, scene=scene,
        constraint_objects=constraints_ui.walk_scene_constraints(scene),
    )


def test_game_rip():
    clear()
    rig = make_rig("GameRip", GAME_RIP, location=(2.0, 1.0, 0.0))
    first = next(pb.name for pb in rig.pose.bones if pb.parent is None)
    check("(the rig's first parentless bone is the weapon)", first == "weapon_attach", first)

    skel = request_builder.armature_to_skeleton(rig)
    names = [j["name"] for j in skel["joints"]]
    problem = one_tree(skel["joints"])
    check("the skeleton is one tree", not problem, problem)
    check("...rooted at the pelvis", names[:1] == ["pelvis"], names[:1])
    check("...carrying the body and nothing beside it", sorted(names) == sorted(BODY), names)
    check("the joint set is the same tree",
          request_builder.request_joint_set(rig) == set(BODY))
    check("the request's root is the pelvis",
          getattr(request_builder.main_root(rig), "name", None) == "pelvis")

    scene = bpy.context.scene
    req = full_request(scene, rig)
    problem = one_tree(req["skeleton"]["joints"])
    check("a full Generate request sends one tree", not problem, problem)
    poses = [c for c in req.get("constraints", []) if c["type"] == "pose_keyframe"]
    check("the keyed pose goes out", len(poses) == 1, len(poses))
    if poses:
        rot = poses[0]["joint_rotations"]
        check("...with the spine", "spine_0" in rot, sorted(rot))
        check("...and not the weapon bone beside the body", "weapon_attach" not in rot, sorted(rot))
    pelvis = rig.matrix_world @ rig.pose.bones["pelvis"].matrix.translation
    want_x, _, want_z = coords.blender_pos_to_mmcp(pelvis)
    anchors = [c for c in req.get("constraints", [])
               if c["type"] == "root_path" and c["frames"] == [0]]
    check("the start anchor is there", len(anchors) == 1, len(anchors))
    if anchors:
        x, z = anchors[0]["positions_xz"][0]
        check("...where the pelvis stands, not the weapon",
              abs(x - want_x) < 1e-4 and abs(z - want_z) < 1e-4, (x, z, want_x, want_z))


def test_one_root_unchanged():
    clear()
    body = [b for b in GAME_RIP if b[0] in BODY]
    rig = make_rig("Plain", body)
    skel = request_builder.armature_to_skeleton(rig)
    check("a rig with one root sends every bone, in the rig's order",
          [j["name"] for j in skel["joints"]] == [pb.name for pb in rig.pose.bones],
          [j["name"] for j in skel["joints"]])
    check("...rooted where it always was",
          request_builder.main_root(rig) == next(pb for pb in rig.pose.bones if pb.parent is None))


def test_helpers_still_filtered():
    clear()
    rig = make_rig("Helpers", GAME_RIP, no_deform={"weapon_attach"})
    skel = request_builder.armature_to_skeleton(rig)
    names = {j["name"] for j in skel["joints"]}
    problem = one_tree(skel["joints"])
    check("a rig with a non-deforming helper still sends one tree", not problem, problem)
    check("...of its deform bones", "weapon_attach" not in names and set(BODY) <= names,
          sorted(names))


def main():
    test_game_rip()
    test_one_root_unchanged()
    test_helpers_still_filtered()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        FAILS.append("exception")
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
