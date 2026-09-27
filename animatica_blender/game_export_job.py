# SPDX-License-Identifier: GPL-3.0-or-later
"""The background half of Export Game Clip (game_export.py): run by a
separate Blender on a copy of the file, so the live scene is never touched.

    blender -b --factory-startup take.blend --python game_export_job.py -- job.json

Adds the ground ``root`` bone (unless the rig has one), keys it and the bones
under it from the job, cuts a loop to its new start, and writes the engine's
file(s) and the ``.json`` beside them. Prints ``EXPORTED <path>`` per file.
Plain bpy: the addon is not loaded here.
"""

import json
import os
import sys

import bpy
from mathutils import Matrix


def _m(flat):
    return Matrix([flat[i * 4:(i + 1) * 4] for i in range(4)])


def _fcurves(action):
    if hasattr(action, "layers"):
        for layer in action.layers:
            for strip in layer.strips:
                for cb in getattr(strip, "channelbags", []):
                    for fc in cb.fcurves:
                        yield cb, fc
    if hasattr(action, "fcurves"):
        try:
            for fc in action.fcurves:
                yield None, fc
        except (AttributeError, RuntimeError):
            pass


def _supported(op, kwargs):
    """Only the settings this Blender's exporter has (they change between versions)."""
    props = op.get_rna_type().properties
    out = {}
    for k, v in kwargs.items():
        if k not in props:
            continue
        p = props[k]
        if p.type == 'ENUM' and not p.is_enum_flag and v not in {i.identifier for i in p.enum_items}:
            continue
        out[k] = v
    return out


def main():
    job = json.load(open(sys.argv[sys.argv.index("--") + 1]))
    scene = bpy.context.scene
    arm = bpy.data.objects[job["armature"]]
    act = bpy.data.actions[job["action"]]
    ad = arm.animation_data or arm.animation_data_create()
    for track in ad.nla_tracks:
        track.mute = True
    ad.action = act
    if getattr(act, "slots", None) and hasattr(ad, "action_slot") and ad.action_slot is None:
        ad.action_slot = act.slots[0]

    frames = job["frames"]
    f0, n, shift = frames[0], len(frames), job["shift"]
    carriers, root = job["carriers"], job["root"]
    moved = {f'pose.bones["{b}"].{p}' for b in carriers + [root]
             for p in ("location", "rotation_quaternion", "rotation_euler", "rotation_axis_angle")}

    # every other curve: just the clip's frames, a loop cut to start `shift` frames in
    m = n - 1
    for cb, fc in list(_fcurves(act)):
        if fc.data_path in moved:
            (cb.fcurves if cb is not None else act.fcurves).remove(fc)
            continue
        vals = [fc.evaluate(f0 + k) for k in range(n)]
        if shift:
            vals = [vals[(k + shift) % m] for k in range(n)]
        kps = fc.keyframe_points
        while len(kps):
            kps.remove(kps[0], fast=True)
        kps.add(n)
        flat = []
        for k, v in enumerate(vals):
            flat += [float(f0 + k), float(v)]
        kps.foreach_set("co", flat)
        for kp in kps:
            kp.interpolation = 'LINEAR'
        fc.update()

    # the root bone, under which the top bone now hangs
    arm.hide_set(False)
    arm.hide_viewport = False
    bpy.context.view_layer.objects.active = arm
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    arm.select_set(True)
    Ae = _m(job["armature_matrix"])
    if job["add_root"]:
        bpy.ops.object.mode_set(mode='EDIT')
        eb = arm.data.edit_bones
        if "root" in eb:
            raise RuntimeError("the rig has a bone named 'root' that is not its top bone")
        r = eb.new("root")
        r.head = (0.0, 0.0, 0.0)
        r.tail = (0.0, 0.2 / max(Ae.to_scale().x, 1e-6), 0.0)
        r.roll = 0.0
        eb[job["top"]].parent = r
        eb[job["top"]].use_connect = False
        bpy.ops.object.mode_set(mode='OBJECT')
    arm.matrix_world = Ae

    pbs = arm.pose.bones
    keyed = [root] + carriers
    for b in keyed:
        pbs[b].rotation_mode = 'QUATERNION'

    def key(which_root):
        prev = {}
        for k in range(n):
            f = f0 + k
            for b in keyed:
                flat = which_root[k] if b == root else job["basis"][b][k]
                loc, q, _ = _m(flat).decompose()
                if b in prev:
                    q.make_compatible(prev[b])
                prev[b] = q
                pb = pbs[b]
                pb.location = loc
                pb.rotation_quaternion = q
                pb.keyframe_insert("location", frame=f)
                pb.keyframe_insert("rotation_quaternion", frame=f)
        for _cb, fc in _fcurves(act):
            if fc.data_path.startswith('pose.bones["') and any(f'["{b}"]' in fc.data_path for b in keyed):
                for kp in fc.keyframe_points:
                    kp.interpolation = 'LINEAR'

    scene.frame_start, scene.frame_end = f0, f0 + n - 1
    meshes = [o for o in bpy.data.objects if o.type == 'MESH' and (
        o.parent == arm or any(md.type == 'ARMATURE' and md.object == arm for md in o.modifiers))]
    for o in meshes:
        o.hide_set(False)
        o.select_set(True)
    engine, folder, name = job["engine"], job["folder"], job["name"]
    if engine == "UNREAL":
        # Unreal adds a bone for the armature object unless it is named "Armature"
        other = bpy.data.objects.get("Armature")
        if other is not None and other != arm:
            other.name = "Armature.other"
        arm.name = "Armature"

    identity = [[1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]] * n
    variants = {"BOTH": ("RootMotion", "InPlace"), "ROOT_MOTION": ("RootMotion",),
                "IN_PLACE": ("InPlace",)}[job["variants"]]
    written = []
    for variant in variants:
        key(job["root_basis"] if variant == "RootMotion" else identity)
        path = os.path.join(folder, f"{name}_{variant}")
        if engine in ("UNREAL", "UNITY"):
            path += ".fbx"
            op = bpy.ops.export_scene.fbx
            kw = dict(filepath=path, use_selection=True, object_types={'ARMATURE', 'MESH'},
                      add_leaf_bones=False, bake_anim=True, bake_anim_use_all_actions=False,
                      bake_anim_use_nla_strips=False, bake_anim_force_startend_keying=True,
                      bake_anim_simplify_factor=0.0, primary_bone_axis='Y', secondary_bone_axis='X',
                      armature_nodetype='NULL', mesh_smooth_type='FACE', axis_forward='-Z', axis_up='Y',
                      path_mode='COPY', embed_textures=True,
                      apply_scale_options='FBX_SCALE_NONE' if engine == "UNREAL" else 'FBX_SCALE_ALL',
                      bake_space_transform=engine == "UNITY")
        else:
            path += ".glb"
            op = bpy.ops.export_scene.gltf
            kw = dict(filepath=path, export_format='GLB', use_selection=True, export_animations=True,
                      export_force_sampling=True, export_frame_range=True,
                      export_animation_mode='ACTIVE_ACTIONS', export_anim_single_armature=True,
                      export_nla_strips=False, export_yup=True)
        op(**_supported(op, kw))
        written.append(path)
        print("EXPORTED", path)

    meta = dict(job["meta"], clip=name, engine=engine, files=[os.path.basename(p) for p in written],
                variants=list(variants))
    side = os.path.join(folder, f"{name}.json")
    with open(side, "w") as fh:
        json.dump(meta, fh, indent=1)
    print("EXPORTED", side)


try:
    main()
except Exception:
    import traceback
    traceback.print_exc()
    sys.exit(1)
