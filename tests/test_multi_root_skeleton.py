"""Every rig sends one skeleton tree, and it is the body.

A game rip (Source2Viewer's Deadlock characters, for one) has the body under
``pelvis`` and other trees beside it at the top: ``root_motion`` with its
attachments and IK targets, a weapon, cloth. Every bone deforms, so no
control-rig filter merged them, and each went out as a root. The schema takes
exactly one, so the server refused the whole request: "Malformed MMCP generate
request: 1 problem(s)".

Each rig shape the request can meet is here:

* loose root bones, the weapon first so "the first bone without a parent" is
  the wrong answer, with the pose keys and start anchor on the same tree;
* ``root_motion`` beside the body with more bones than it, skinned (the body
  moves the mesh) and bare (the body is the deeper tree);
* a body split over several roots: refused, naming them;
* a skinned bone beside the body: sent without it, and named as left behind;
* the same rip with ``root_motion`` not deforming, which takes the control-rig
  path: still the body only;
* a flat control rig (every deform bone its own root) and a generated Rigify
  rig: the same tree with or without a mesh, as before;
* a rig with one root: exactly what it always sent.

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


def skin(rig, bones):
    """A mesh the rig deforms, one vertex weighted fully to each of ``bones``."""
    pts = [rig.data.bones[b].head_local.copy() for b in bones]
    me = bpy.data.meshes.new("Body")
    me.from_pydata([tuple(p) for p in pts], [], [])
    ob = bpy.data.objects.new("Body", me)
    bpy.context.scene.collection.objects.link(ob)
    for i, b in enumerate(bones):
        ob.vertex_groups.new(name=b).add([i], 1.0, 'REPLACE')
    ob.modifiers.new("Armature", 'ARMATURE').object = rig
    return ob


def test_root_motion_beside_the_body():
    """``pelvis`` and ``root_motion`` both at the top, both with bones under
    them -- and more under ``root_motion`` (attachments, IK targets) than in
    the body. The body is the tree that moves the skin."""
    clear()
    helpers = [("root_motion", None, (0.0, 0.0, 0.0), (0.0, 0.2, 0.0))] + [
        (f"attach_{i}", "root_motion", (0.05 * i, 0.0, 0.1), (0.05 * i, 0.0, 0.2)) for i in range(12)]
    body = [b for b in GAME_RIP if b[0] in BODY]
    rig = make_rig("RootMotion", helpers + body)
    skin(rig, BODY)
    skel = request_builder.armature_to_skeleton(rig)
    names = [j["name"] for j in skel["joints"]]
    problem = one_tree(skel["joints"])
    check("root_motion beside the body: one tree", not problem, problem)
    check("...the body's, though root_motion has more bones",
          names[:1] == ["pelvis"] and sorted(names) == sorted(BODY), names)
    check("...and it is the request's root",
          getattr(request_builder.main_root(rig), "name", None) == "pelvis")


ROOT_MOTION = [("root_motion", None, (0.0, 0.0, 0.0), (0.0, 0.2, 0.0))] + [
    (f"attach_{i}", "root_motion", (0.05 * i, 0.0, 0.1), (0.05 * i, 0.0, 0.2)) for i in range(12)]


def test_root_motion_without_a_mesh():
    """No skin to tell by: the body is the deeper tree, though root_motion's
    attachments outnumber it."""
    clear()
    rig = make_rig("BareRootMotion", ROOT_MOTION + [b for b in GAME_RIP if b[0] in BODY])
    names = [j["name"] for j in request_builder.armature_to_skeleton(rig)["joints"]]
    check("bare root_motion rig: the body goes out", sorted(names) == sorted(BODY), names)


def test_split_body_refused():
    """Three roots that each move a third of the mesh: no one of them is the
    character, so nothing is sent, and the message names the roots to join."""
    clear()
    split = [("spine_0", None, (0.0, 0.0, 1.1), (0.0, 0.0, 1.4)),
             ("head", "spine_0", (0.0, 0.0, 1.4), (0.0, 0.0, 1.7)),
             ("leg_upper_L", None, (0.1, 0.0, 1.0), (0.1, 0.0, 0.5)),
             ("leg_lower_L", "leg_upper_L", (0.1, 0.0, 0.5), (0.1, 0.0, 0.05)),
             ("leg_upper_R", None, (-0.1, 0.0, 1.0), (-0.1, 0.0, 0.5)),
             ("leg_lower_R", "leg_upper_R", (-0.1, 0.0, 0.5), (-0.1, 0.0, 0.05))]
    rig = make_rig("Split", split)
    skin(rig, [b[0] for b in split])
    try:
        request_builder.armature_to_skeleton(rig)
        check("a body split over three roots is refused", False, "no error")
    except request_builder.BuildError as exc:
        msg = str(exc)
        check("a body split over three roots is refused", True)
        check("...naming the roots to join",
              all(n in msg for n in ("spine_0", "leg_upper_L", "leg_upper_R")) and "Ctrl+P" in msg, msg)


def test_skinned_bone_left_behind():
    """A weapon mesh on a root bone of its own: the body goes out, and the
    weapon is named as left where it is."""
    clear()
    rig = make_rig("Weapon", [b for b in GAME_RIP if b[0] in BODY] + [
        ("weapon_R", None, (0.6, -0.3, 1.0), (0.6, -0.3, 1.2))])
    skin(rig, BODY + ["weapon_R"])
    names = {j["name"] for j in request_builder.armature_to_skeleton(rig)["joints"]}
    check("a skinned weapon root: the body goes out", names == set(BODY), sorted(names))
    left = request_builder.left_behind(rig)
    check("...and the weapon is named as left behind", left == ["weapon_R"], left)
    check("a rig with one root leaves nothing behind",
          request_builder.left_behind(make_rig("One", [b for b in GAME_RIP if b[0] in BODY])) == [])


def test_root_motion_not_deforming():
    """The rip again, with root_motion's Deform off but its attachments still
    deforming: the control-rig path, which used to join the attachments to the
    body by distance and could take one for the root."""
    clear()
    rig = make_rig("RipNoDeform", ROOT_MOTION + [b for b in GAME_RIP if b[0] in BODY],
                   no_deform={"root_motion"})
    skin(rig, BODY)
    skel = request_builder.armature_to_skeleton(rig)
    names = [j["name"] for j in skel["joints"]]
    problem = one_tree(skel["joints"])
    check("root_motion not deforming: one tree", not problem, problem)
    check("...the body only, rooted at the pelvis",
          names[:1] == ["pelvis"] and sorted(names) == sorted(BODY), names)
    check("...and the joint set agrees",
          request_builder.request_joint_set(rig) == set(BODY))


def deform_tree_with_and_without_skin(rig, mesh):
    with_skin = request_builder.request_tree(rig)
    mesh.modifiers.remove(mesh.modifiers[0])
    without = request_builder.request_tree(rig)
    return with_skin, without


def test_flat_control_rig_unchanged():
    """Every deform bone its own root, driven by controls under ``root``: the
    control-rig path joins them by distance. No tree has most of the skin, so
    the skin changes nothing there."""
    clear()
    defs = [(f"DEF-{n}", None, h, t) for n, _p, h, t in GAME_RIP if n in BODY]
    ctrls = [("root", None, (0.0, 0.0, 0.0), (0.0, 0.3, 0.0))] + [
        (f"CTRL-{n}", "root", h, t) for n, _p, h, t in GAME_RIP if n in BODY]
    rig = make_rig("Flat", defs + ctrls, no_deform={c[0] for c in ctrls})
    for n in BODY:
        c = rig.pose.bones[f"DEF-{n}"].constraints.new('COPY_TRANSFORMS')
        c.target, c.subtarget = rig, f"CTRL-{n}"
    mesh = skin(rig, [f"DEF-{n}" for n in BODY])
    check("(the flat rig is a control rig)", request_builder.is_control_rig(rig))
    with_skin, without = deform_tree_with_and_without_skin(rig, mesh)
    check("flat control rig: one tree of every deform bone",
          not one_tree([{"name": n, "parent": p} for n, p in with_skin.items()])
          and set(with_skin) == {f"DEF-{n}" for n in BODY}, with_skin)
    check("...the same tree with the skin as without", with_skin == without,
          (with_skin, without))


def test_rigify_unchanged():
    """A generated Rigify rig, skinned: the same tree as without a mesh, and
    the start anchor reads the root of that tree, its hips -- the joint the
    server's root position is of. Its first root bone, ``root``, is a control
    on the floor: moved by ``torso``, the character stood elsewhere."""
    clear()
    if not addon_utils.enable("rigify", default_set=True):
        check("(Rigify not available: skipped)", True)
        return
    bpy.ops.object.armature_human_metarig_add()
    meta = bpy.context.active_object
    bpy.ops.pose.rigify_generate()
    rig = next(o for o in bpy.data.objects if o.type == 'ARMATURE' and o is not meta)
    mesh = skin(rig, sorted(request_builder.emitted_deform_bones(rig)))
    with_skin, without = deform_tree_with_and_without_skin(rig, mesh)
    check("Rigify: the same tree with the skin as without", with_skin == without)
    check("...rooted at DEF-spine", next(iter(with_skin)) == "DEF-spine", next(iter(with_skin)))
    check("...and the start anchor reads its hips, the root of the tree sent",
          request_builder.main_root(rig) == rig.pose.bones["DEF-spine"], request_builder.main_root(rig))


def rigify_with_extra_pieces():
    """A generated Rigify rig with two deform chains Rigify did not make, both
    under ``root``: a prop bone off to the side that moves no skin, and a
    weighted cape strand. The parent walk leaves each a piece of its own; the
    metarig is gone, so the bake takes its fallback, a copy of the rig."""
    clear()
    if not addon_utils.enable("rigify", default_set=True):
        return None, None
    bpy.ops.object.armature_human_metarig_add()
    meta = bpy.context.active_object
    bpy.ops.pose.rigify_generate()
    rig = next(o for o in bpy.data.objects if o.type == 'ARMATURE' and o is not meta)
    bpy.data.objects.remove(meta, do_unlink=True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    eb = rig.data.edit_bones
    for name, parent, head, tail in (
            ("DEF-prop", "root", (1.5, 0.0, 1.0), (1.5, 0.0, 1.3)),
            ("DEF-cape.001", "root", (0.0, 0.25, 1.4), (0.0, 0.3, 1.0)),
            ("DEF-cape.002", "DEF-cape.001", (0.0, 0.3, 1.0), (0.0, 0.32, 0.6))):
        b = eb.new(name)
        b.head, b.tail, b.parent, b.use_deform = head, tail, eb[parent], True
    bpy.ops.object.mode_set(mode='OBJECT')
    mesh = skin(rig, sorted(request_builder.emitted_deform_bones(rig) - {"DEF-prop"}))
    return rig, mesh


def test_rigify_bake_follows_the_request():
    """The server turns the body against the tree the request sent, so the copy
    the Rigify bake drives must hang every one of those joints the same way --
    though it deforms no mesh to weigh, and carries the face and tweak bones
    the request leaves out. That copy is for a Rigify rig its control-rig
    profile does not know (a human one is keyed through its controls:
    control_bake), so the profile is told to know none here."""
    rig, mesh = rigify_with_extra_pieces()
    if rig is None:
        check("(Rigify not available: skipped)", True)
        return
    from animatica_blender import gltf_to_blender, rigify_bake

    tree = request_builder.request_tree(rig)
    check("(custom Rigify: the body is the request's root)",
          next(iter(tree)) == "DEF-spine", next(iter(tree)))
    check("(...the prop stays out, the cape goes)",
          "DEF-prop" not in tree and {"DEF-cape.001", "DEF-cape.002"} <= set(tree))
    mesh.modifiers[0].object = None
    blind = request_builder._build_deform_parent_map(rig, request_builder.emitted_deform_bones(rig))
    mesh.modifiers[0].object = rig
    check("(blind to the skin, the tree would be rooted elsewhere)",
          [n for n, p in blind.items() if p is None] != ["DEF-spine"],
          [n for n, p in blind.items() if p is None])

    seen: dict[str, str | None] = {}
    heads: dict[str, object] = {}
    real = rigify_bake.apply_anim_to_rigify

    def capture(src, target, **_kw):
        seen.update({b.name: (b.parent.name if b.parent else None) for b in src.data.bones})
        heads.update({b.name: b.head_local.copy() for b in src.data.bones})
        return 0

    rigify_bake.apply_anim_to_rigify = capture
    known = gltf_to_blender.control_profile
    gltf_to_blender.control_profile = lambda _arm: None
    try:
        gltf_to_blender._bake_to_control_rig(
            rig, source_action=bpy.data.actions.new("Take"), frame_start=1, frame_end=2)
    finally:
        rigify_bake.apply_anim_to_rigify = real
        gltf_to_blender.control_profile = known
    check("the bake drove a copy of the rig", bool(seen), len(seen))
    wrong = {n: (p, seen.get(n, "missing")) for n, p in tree.items() if seen.get(n, "missing") != p}
    check("...hanging every joint the server saw from the request's parent", not wrong,
          dict(list(wrong.items())[:4]))
    moved = sorted(n for n, h in heads.items() if (h - rig.data.bones[n].head_local).length > 1e-5)
    check("...and keeping the rig's rest pose: no re-parented bone snaps to its new parent",
          not moved, moved[:4])


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
    test_root_motion_beside_the_body()
    test_root_motion_without_a_mesh()
    test_split_body_refused()
    test_skinned_bone_left_behind()
    test_root_motion_not_deforming()
    test_flat_control_rig_unchanged()
    test_rigify_unchanged()
    test_rigify_bake_follows_the_request()
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
