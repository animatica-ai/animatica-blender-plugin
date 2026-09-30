"""The take a turned rig gets through the addon's own operators, in a real
Blender window: Generate, Regenerate (a take spliced into an action with
keys either side), Generate for several characters, Pose, and a Rigify
control rig. The server is stood in for by a stub that answers with a
known take, so no generation is spent and no network is needed.

Every scenario turns the armature object about Z and checks that the
result stands in the world where the take puts it, bone for bone, against
the same take baked on the rig at yaw 0.

Run (a window opens and closes itself when done; the report lands in
``--out``, default ``tests/out/gui``)::

    blender --factory-startup --python tests/test_gui_object_rotation.py

    blender --factory-startup --python tests/test_gui_object_rotation.py -- \
        --rig rigs.blend --arm AF_Lead_2 --take take.json --yaw 174.29

Without ``--rig`` the synthetic rig and take from
``test_bake_object_rotation.py`` are used. ``--rig`` names a .blend holding
the armature ``--arm`` (and, if present, a mesh named ``<arm>_mesh``), and
``--take`` a glTF response for that rig, as the server sends it.
``--keep-open`` leaves the window up afterwards.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
for p in (ROOT, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import addon_utils  # noqa: E402
import bpy  # noqa: E402
from mathutils import Matrix, Quaternion, Vector  # noqa: E402

import test_bake_object_rotation as synthetic  # noqa: E402

TOL = 1e-4
WAIT_SECONDS = 300.0

# What the stub server claims, enough for the operators to build requests.
CAPS = {
    "protocol_version": "1.0",
    "rotation_format": "quaternion_xyzw",
    "coordinate_system": "right_handed_y_up",
    "units": "meters",
    "response_formats": ["gltf_2.0_json"],
    "models": [{
        "id": "kimodo-soma-rp", "model_name": "kimodo-soma-rp", "version": "1.0.0", "fps": 30.0,
        "supports_retargeting": True, "supports_async": False, "supports_segment_seed": True,
        "supports_loop": True, "supports_batch": True, "supports_trajectory": False,
        "supported_constraints": ["root_path", "effector_target", "pose_keyframe"],
        "supported_segments": ["text", "unconditioned", "pose"],
        "supported_guidance_types": ["nocfg", "regular", "separated"],
        "predicted_contact_joints": [], "native_clip_seconds": 10.0, "chunking": "none",
        "recommended_max_duration_seconds": 12.0,
        "limits": {"max_duration_seconds": 30.0, "max_num_samples": 16, "max_constraints_per_request": 64,
                   "max_prompt_length": 1000, "max_request_bytes": 1048576, "max_batch_size": 16},
        "canonical_skeleton": {"joints": [], "coordinate_system": "right_handed_y_up", "units": "meters"},
    }],
}


def parse_args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    args = {"rig": None, "arm": None, "take": None, "yaw": None, "out": os.path.join(TESTS, "out", "gui"), "keep_open": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--keep-open":
            args["keep_open"] = True
        elif a in ("--rig", "--arm", "--take", "--yaw", "--out"):
            args[a[2:]] = argv[i + 1]
            i += 1
        i += 1
    if args["yaw"] is not None:
        args["yaw"] = float(args["yaw"])
    return args


ARGS = parse_args()
OUT = ARGS["out"]
os.makedirs(OUT, exist_ok=True)
REPORT: dict = {"scenarios": {}, "screenshots": [], "args": ARGS}


# ---------------------------------------------------------------------------
# The stub server
# ---------------------------------------------------------------------------

class Stub:
    responder = None            # req -> glTF dict
    requests: list = []


def stub_generate(_client, body):
    """Stands in for ``MmcpClient.generate``."""
    Stub.requests.append(body)
    time.sleep(0.3)
    return json.loads(json.dumps(Stub.responder(body)))


def stub_generate_batch(client, bodies):
    return [stub_generate(client, b) for b in bodies]


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------

def scene():
    return bpy.context.scene


def settings():
    return scene().animatica


def override() -> dict:
    wm = bpy.context.window_manager
    win = wm.windows[0]
    area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return {"window": win, "screen": win.screen, "area": area, "region": region}


def select_only(objs) -> None:
    for o in scene().objects:
        o.select_set(o in objs)
    bpy.context.view_layer.objects.active = objs[0] if objs else None


def copy_rig(src, name: str, yaw_deg: float):
    new = src.copy()
    new.data = src.data.copy()
    new.animation_data_clear()
    new.name = name
    scene().collection.objects.link(new)
    for pb in new.pose.bones:
        pb.matrix_basis = Matrix.Identity(4)
    new.rotation_euler = (src.rotation_euler.x, src.rotation_euler.y, math.radians(yaw_deg))
    mesh = bpy.data.objects.get(src.name + "_mesh")
    if mesh is not None:
        m = mesh.copy()
        m.name = name + "_mesh"
        scene().collection.objects.link(m)
        m.parent = new
        for mod in m.modifiers:
            if mod.type == 'ARMATURE':
                mod.object = new
    bpy.context.view_layer.update()
    return new


def set_blocks(arm, prompt: str, frames) -> None:
    from animatica_blender import properties
    s = settings()
    s.target_armature = arm
    s.prompt_blocks.clear()
    b = s.prompt_blocks.add()
    b.prompt, b.frame_start, b.frame_end, b.enabled, b.seed = prompt, frames[0], frames[-1], True, 0
    s.active_block_index = 0
    properties.save_blocks_to_armature(arm, s)


def world_pose(arm, frames, bones=None) -> dict:
    out = {}
    for f in frames:
        scene().frame_set(f)
        bpy.context.view_layer.update()
        for pb in arm.pose.bones:
            if bones is None or pb.name in bones:
                out[(f, pb.name)] = (arm.matrix_world @ pb.matrix).copy()
    return out


def max_diff(got: dict, want: dict, *, remap=None, rotation_only=False, bones=None) -> tuple[float, str]:
    worst, where = 0.0, ""
    for key, m_want in want.items():
        if bones is not None and key[1] not in bones:
            continue
        m_got = got[remap(key) if remap else key]
        n = 3 if rotation_only else 4
        for i in range(n):
            for j in range(n):
                d = abs(m_got[i][j] - m_want[i][j])
                if d > worst:
                    worst, where = d, str(key)
    return worst, where


def check(name: str, value: float, where: str, tol: float = TOL) -> None:
    REPORT["scenarios"][CURRENT]["checks"].append({"name": name, "max_diff": round(value, 6), "at": where, "ok": value <= tol})
    if value > tol:
        raise AssertionError(f"{name}: differs by {value:.4f} at {where}")


def note(**kw) -> None:
    REPORT["scenarios"][CURRENT].setdefault("notes", {}).update(kw)


def screenshot(tag: str, arm, frames) -> None:
    ov = override()
    space = ov["area"].spaces.active
    r3d = space.region_3d
    space.shading.type = 'SOLID'
    r3d.view_perspective = 'PERSP'
    # Looking at the rig from its front-left, a little above.
    r3d.view_rotation = Quaternion((0.0, 0.0, 1.0), math.radians(-35.0)) @ Quaternion((1.0, 0.0, 0.0), math.radians(75.0))
    root = next(pb for pb in arm.pose.bones if pb.parent is None)
    with bpy.context.temp_override(**ov):
        for f in frames:
            scene().frame_set(f)
            bpy.context.view_layer.update()
            hips = arm.pose.bones.get("Hips") or root
            r3d.view_location = arm.matrix_world @ hips.head
            r3d.view_distance = 4.0
            bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
            path = os.path.join(OUT, f"{tag}_f{f:03d}.png")
            bpy.ops.screen.screenshot(filepath=path)
            REPORT["screenshots"].append(path)


def wait_for(pred, what: str):
    t0 = time.time()
    while not pred():
        if time.time() - t0 > WAIT_SECONDS:
            raise TimeoutError(f"gave up waiting for {what}")
        yield "wait"


# ---------------------------------------------------------------------------
# The rig and take under test
# ---------------------------------------------------------------------------

def load_subject():
    """Returns (armature, take, frames, yaw): the rig as saved (yaw 0), the
    server's take for it, the scene frames it covers, the yaw to test."""
    if ARGS["rig"]:
        with bpy.data.libraries.load(ARGS["rig"], link=False) as (src, dst):
            dst.objects = [n for n in src.objects if n in (ARGS["arm"], ARGS["arm"] + "_mesh")]
        for o in dst.objects:
            scene().collection.objects.link(o)
        arm = bpy.data.objects[ARGS["arm"]]
        arm.animation_data_clear()
        with open(ARGS["take"], encoding="utf-8") as f:
            take = json.load(f)
        yaw = ARGS["yaw"] if ARGS["yaw"] is not None else 180.0
    else:
        arm = synthetic.make_rig("Subject")
        arm.location = (0.0, 0.0, 0.0)
        take = synthetic.GLTF
        yaw = ARGS["yaw"] if ARGS["yaw"] is not None else 180.0
    from animatica_blender import gltf_to_blender
    n = gltf_to_blender.sample_frame_count(take)
    frames = list(range(1, n + 1))
    scene().frame_start, scene().frame_end = frames[0], frames[-1]
    scene().render.fps = int(gltf_to_blender.read_extension_metadata(take).get("fps") or 30)
    return arm, take, frames, yaw


# ---------------------------------------------------------------------------
# Scenarios: generators that yield while an operator runs
# ---------------------------------------------------------------------------

def scenario_generate(subject, take, frames, yaw, ref):
    from animatica_blender import gltf_to_blender
    arm = copy_rig(subject, "S1_generate", yaw)
    select_only([arm])
    set_blocks(arm, "walks", frames)
    Stub.responder = lambda req: take
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.generate('INVOKE_DEFAULT')
    note(generate=sorted(ret))
    yield from wait_for(lambda: not settings().is_generating, "Generate")
    assert settings().is_previewing, "no preview after Generate"
    check("preview vs yaw-0 bake", *max_diff(world_pose(arm, frames), ref))
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.accept()
    note(accept=sorted(ret), nla=[t.name for t in arm.animation_data.nla_tracks])
    assert arm.animation_data.nla_tracks, "Accept left no NLA track"
    check("accepted (NLA) vs yaw-0 bake", *max_diff(world_pose(arm, frames), ref))
    n = len(frames)
    screenshot("generate", arm, [frames[n // 4], frames[n // 2], frames[3 * n // 4]])


def scenario_regenerate(subject, take, frames, yaw, ref):
    """Keys either side of the blocks make Generate a splice into the
    artist's own action, the path Regenerate Block takes too."""
    arm = copy_rig(subject, "S2_regenerate", yaw)
    arm.animation_data_create()
    action = bpy.data.actions.new("S2 outside keys")
    arm.animation_data.action = action
    before, after = frames[0] - 1, frames[-1] + 20
    for pb in arm.pose.bones:
        pb.rotation_mode = 'QUATERNION'
        for f in (before, after):
            pb.keyframe_insert("rotation_quaternion", frame=f)
    select_only([arm])
    set_blocks(arm, "walks", frames)
    Stub.responder = lambda req: take
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.generate('INVOKE_DEFAULT')
    note(generate=sorted(ret))
    yield from wait_for(lambda: not settings().is_generating, "Generate (splice)")
    note(spliced_in_place=bool(arm.get("animatica_spliced_in_place")), action=arm.animation_data.action.name)
    assert arm.animation_data.action is action, "the splice did not land in the artist's action"
    check("spliced window vs yaw-0 bake", *max_diff(world_pose(arm, frames), ref))
    from animatica_blender import _bake_common
    kept = {int(round(kp.co.x)) for fc in _bake_common.action_fcurves(action, arm) for kp in fc.keyframe_points}
    assert before in kept and after in kept, f"keys outside the window were lost: {sorted(kept)[:3]}…{sorted(kept)[-3:]}"
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.accept()
    note(accept=sorted(ret))
    check("accepted splice vs yaw-0 bake", *max_diff(world_pose(arm, frames), ref))


def scenario_batch(subject, take, frames, yaw, ref):
    a = copy_rig(subject, "S3_batch_a", yaw)
    b = copy_rig(subject, "S3_batch_b", yaw + 90.0)
    for arm in (a, b):
        set_blocks(arm, "walks", frames)
    select_only([a, b])
    Stub.responder = lambda req: take
    s = settings()
    s.batch_direction = 'OWN'
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.generate_batch('INVOKE_DEFAULT')
    note(generate_batch=sorted(ret))
    from animatica_blender import batch
    yield from wait_for(lambda: not settings().is_generating and (batch.pending(settings()) or batch.failures(settings())), "Generate (batch)")
    note(pending=batch.pending(s), failed=batch.failures(s))
    assert not batch.failures(s), f"batch failures: {batch.failures(s)}"
    check("batch preview A vs yaw-0 bake", *max_diff(world_pose(a, frames), ref))
    check("batch preview B (yaw+90) vs yaw-0 bake", *max_diff(world_pose(b, frames), ref))
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.accept_batch()
    note(accept_batch=sorted(ret))
    check("batch accepted A vs yaw-0 bake", *max_diff(world_pose(a, frames), ref))
    check("batch accepted B vs yaw-0 bake", *max_diff(world_pose(b, frames), ref))


def scenario_pose(subject, take, frames, yaw, ref):
    """Pose bakes one frame of a response at the playhead; the root keeps
    its world XY, so the body is compared relative to the root."""
    from animatica_blender import gltf_to_blender
    arm = copy_rig(subject, "S4_pose", yaw)
    select_only([arm])
    settings().target_armature = arm
    k = len(frames) // 2
    single = one_frame_take(take, k)
    Stub.responder = lambda req: single
    target = 50
    scene().frame_set(target)
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.generate_pose('EXEC_DEFAULT', prompt="stands", pose_apply_scope='ALL')
    note(generate_pose=sorted(ret))
    yield from wait_for(lambda: not settings().is_generating, "Pose")
    got = world_pose(arm, [target])
    want = {key: m for key, m in ref.items() if key[0] == frames[k]}
    check("pose orientation vs take frame", *max_diff(got, want, remap=lambda key: (target, key[1]), rotation_only=True))
    root = next(pb.name for pb in arm.pose.bones if pb.parent is None)
    rel_got = {key[1]: m.translation - got[(target, root)].translation for key, m in got.items()}
    rel_want = {key[1]: m.translation - want[(frames[k], root)].translation for key, m in want.items()}
    worst = max(((rel_got[n] - rel_want[n]).length, n) for n in rel_want)
    check("pose body relative to root vs take frame", worst[0], worst[1])
    note(root_z_got=round(got[(target, root)].translation.z, 4), root_z_take=round(want[(frames[k], root)].translation.z, 4))


def one_frame_take(take: dict, k: int) -> dict:
    """Frame ``k`` of ``take`` as a one-frame response, as Pose gets one."""
    from animatica_blender import gltf_to_blender as g
    nodes = take["nodes"]
    rot, pos = {}, {}
    for ch in take["animations"][0]["channels"]:
        name = nodes[ch["target"]["node"]]["name"]
        sampler = take["animations"][0]["samplers"][ch["sampler"]]
        if ch["target"]["path"] == "rotation":
            q = g._read_floats(take, sampler["output"], "VEC4")[4 * k:4 * k + 4]
            rot[name] = [tuple(q)]
        else:
            p = g._read_floats(take, sampler["output"], "VEC3")[3 * k:3 * k + 3]
            pos[name] = [tuple(p)]
    import types
    byname = {n["name"]: types.SimpleNamespace(name=n["name"], parent=None) for n in nodes}
    for n in nodes:
        for c in n.get("children") or ():
            byname[nodes[c]["name"]].parent = byname[n["name"]]
    fps = int(g.read_extension_metadata(take).get("fps") or 30)
    return make_take(list(byname.values()), rot, pos, fps)


def make_take(bones, rot, pos, fps):
    """``synthetic.make_gltf`` for any joint tree (objects with .name/.parent)."""
    import base64
    import struct
    names = [b.name for b in bones]
    idx = {n: i for i, n in enumerate(names)}
    nodes = [{"name": n, "translation": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0]} for n in names]
    for b in bones:
        if b.parent is not None:
            nodes[idx[b.parent.name]].setdefault("children", []).append(idx[b.name])
    comps = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}
    blob, views, acc = bytearray(), [], []

    def push(fl, typ):
        off = len(blob)
        blob.extend(struct.pack(f"<{len(fl)}f", *fl))
        views.append({"buffer": 0, "byteOffset": off, "byteLength": len(fl) * 4})
        acc.append({"bufferView": len(views) - 1, "componentType": 5126, "count": len(fl) // comps[typ], "type": typ})
        return len(acc) - 1

    n = len(next(iter(rot.values())))
    times = push([f / fps for f in range(n)], "SCALAR")
    samplers, channels = [], []
    for name, qs in rot.items():
        o = push([c for q in qs for c in q], "VEC4")
        samplers.append({"input": times, "output": o, "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": idx[name], "path": "rotation"}})
    for name, ps in pos.items():
        o = push([c for p in ps for c in p], "VEC3")
        samplers.append({"input": times, "output": o, "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": idx[name], "path": "translation"}})
    uri = "data:application/octet-stream;base64," + base64.b64encode(bytes(blob)).decode("ascii")
    return {"asset": {"version": "2.0"}, "nodes": nodes,
            "buffers": [{"byteLength": len(blob), "uri": uri}], "bufferViews": views, "accessors": acc,
            "animations": [{"name": "sample_0", "samplers": samplers, "channels": channels}],
            "extensions": {"MMCP_motion": {"fps": fps, "samples": [{"num_frames": n}]}}}


def make_rigify(name: str):
    """A fresh Rigify human (metarig + generated rig) at the origin."""
    with bpy.context.temp_override(**override()):
        bpy.ops.object.armature_human_metarig_add()
        meta = bpy.context.view_layer.objects.active
        meta.name = name + "_metarig"
        bpy.context.view_layer.update()
        bpy.ops.pose.rigify_generate()
    rig = meta.data.rigify_target_rig
    rig.name = name
    return meta, rig


def worst_bones(got: dict, want: dict, n: int = 5) -> list:
    by_bone: dict[str, float] = {}
    for key, m in want.items():
        d = max(abs(got[key][i][j] - m[i][j]) for i in range(4) for j in range(4))
        by_bone[key[1]] = max(by_bone.get(key[1], 0.0), d)
    return sorted(((round(d, 4), b) for b, d in by_bone.items()), reverse=True)[:n]


def max_position_diff(got: dict, want: dict) -> tuple[float, str]:
    worst, where = 0.0, ""
    for key, m in want.items():
        d = (got[key].translation - m.translation).length
        if d > worst:
            worst, where = d, str(key)
    return worst, where


def orientation_residual(got: dict, want: dict, n: int = 5) -> list:
    """The bones whose world orientation differs most, in degrees."""
    by_bone: dict[str, float] = {}
    for key, m in want.items():
        ang = math.degrees((m.to_3x3().inverted() @ got[key].to_3x3()).to_quaternion().angle)
        by_bone[key[1]] = max(by_bone.get(key[1], 0.0), ang)
    return sorted(((round(a, 1), b) for b, a in by_bone.items()), reverse=True)[:n]


def scenario_rigify(subject, take, frames, yaw, ref):
    """A Rigify human: the deform skeleton goes to the server, the take
    comes back onto the controls through the metarig detour. Three fresh
    rigs get the same take: at yaw 0 by a direct bake (the reference), at
    yaw 180 by a direct bake, and at yaw 180 through Generate and Accept;
    the metarigs stay where they were generated, as a user leaves them.

    Positions are held to three centimetres and the keyed extremities exactly.
    Orientations are only recorded: a rig turned in object mode carries
    the take's heading as a torso rotation relative to its rest, and
    Rigify's follow mixers (``neck_follow``, the IK pole parent) blend
    part of that rest-relative rotation into bones nobody keys, so the
    neck deform bone twists and the forearms roll a few degrees. That is
    Rigify's mechanics, not the bake's, and it is what the note shows."""
    from animatica_blender import coords, gltf_to_blender, request_builder
    if not addon_utils.check("rigify")[1]:
        addon_utils.enable("rigify", default_set=True)
    meta_a, rig_a = make_rigify("S5_yaw0_direct")
    note(is_control_rig=request_builder.is_control_rig(rig_a))
    assert request_builder.is_control_rig(rig_a), "Rigify rig not detected as a control rig"
    joints = request_builder.armature_to_skeleton(rig_a)["joints"]
    import types
    byname = {j["name"]: types.SimpleNamespace(name=j["name"], parent=None) for j in joints}
    for j in joints:
        if j["parent"]:
            byname[j["name"]].parent = byname[j["parent"]]
    root = next(j["name"] for j in joints if j["parent"] is None)
    n = 24
    rot = {name: [(0.0, 0.0, 0.0, 1.0)] * n for name in byname}

    def q(axis, a):
        qq = Quaternion(axis, a)
        return (qq.x, qq.y, qq.z, qq.w)

    rot[root] = [q((0, 1, 0), 0.6 + 0.02 * t) for t in range(n)]
    for name, axis, ang in (("DEF-thigh.L", (1, 0, 0), -0.5), ("DEF-shin.L", (1, 0, 0), 0.8),
                            ("DEF-thigh.R", (1, 0, 0), 0.3), ("DEF-shin.R", (1, 0, 0), 0.4),
                            ("DEF-spine.003", (0, 0, 1), 0.2), ("DEF-upper_arm.L", (0, 0, 1), 0.5),
                            ("DEF-forearm.L", (0, 1, 0), 0.7)):
        assert name in rot, f"{name} not in the request skeleton"
        rot[name] = [q(axis, ang + 0.1 * math.sin(0.4 * t)) for t in range(n)]
    scene().frame_set(1)
    bpy.context.view_layer.update()
    rx, ry, rz = coords.blender_pos_to_mmcp((rig_a.matrix_world @ rig_a.pose.bones[root].matrix).translation)
    pos = {root: [(rx + 0.03 * t, ry, rz + 0.02 * t) for t in range(n)]}
    rig_take = make_take(list(byname.values()), rot, pos, 30)
    rig_frames = list(range(1, n + 1))
    scene().render.fps = 30
    scene().frame_start, scene().frame_end = 1, n
    deform = {j["name"] for j in joints}
    note(deform_bones=len(deform), root=root)

    with bpy.context.temp_override(**override()):
        gltf_to_blender.bake_gltf_to_armature(rig_take, rig_a, start_frame=1, action_name="S5 yaw 0 direct")
    p_a = world_pose(rig_a, rig_frames, deform)

    meta_b, rig_b = make_rigify("S5_yaw180_direct")
    rig_b.rotation_euler = (0.0, 0.0, math.radians(180.0))
    bpy.context.view_layer.update()
    with bpy.context.temp_override(**override()):
        gltf_to_blender.bake_gltf_to_armature(rig_take, rig_b, start_frame=1, action_name="S5 yaw 180 direct")
    p_b = world_pose(rig_b, rig_frames, deform)
    note(worst_direct_180=worst_bones(p_b, p_a), orientation_residual_deg_direct_180=orientation_residual(p_b, p_a))
    check("Rigify deform bone positions, direct bake at yaw 180 vs yaw 0", *max_position_diff(p_b, p_a), tol=3e-2)

    meta_c, rig_c = make_rigify("S5_yaw180_generate")
    rig_c.rotation_euler = (0.0, 0.0, math.radians(180.0))
    bpy.context.view_layer.update()
    select_only([rig_c])
    set_blocks(rig_c, "walks", rig_frames)
    Stub.responder = lambda req: rig_take
    with bpy.context.temp_override(**override()):
        ret = bpy.ops.animatica.generate('INVOKE_DEFAULT')
    note(generate=sorted(ret))
    yield from wait_for(lambda: not settings().is_generating, "Generate (Rigify)")
    note(request_joints=len(Stub.requests[-1]["skeleton"]["joints"]))
    p_c = world_pose(rig_c, rig_frames, deform)
    note(worst_generate_180=worst_bones(p_c, p_a), orientation_residual_deg_generate_180=orientation_residual(p_c, p_a))
    check("Rigify deform bone positions, Generate at yaw 180 vs yaw 0 direct", *max_position_diff(p_c, p_a), tol=3e-2)
    check("Rigify hands, feet and head, Generate at yaw 180 vs yaw 0 direct",
          *max_diff(p_c, p_a, bones=("DEF-hand.L", "DEF-hand.R", "DEF-foot.L", "DEF-foot.R", "DEF-spine.006")), tol=1e-3)
    with bpy.context.temp_override(**override()):
        bpy.ops.animatica.accept()
    check("Rigify accepted, deform bone positions vs yaw 0 direct", *max_position_diff(world_pose(rig_c, rig_frames, deform), p_a), tol=3e-2)
    screenshot("rigify", rig_c, [rig_frames[6], rig_frames[18]])
    scene().frame_start, scene().frame_end = frames[0], frames[-1]


SCENARIOS = [
    ("generate", scenario_generate),
    ("regenerate_splice", scenario_regenerate),
    ("batch", scenario_batch),
    ("pose", scenario_pose),
    ("rigify_control_rig", scenario_rigify),
]
CURRENT = ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def setup():
    from animatica_blender import gltf_to_blender, mmcp_client
    bpy.context.preferences.use_preferences_save = False
    mmcp_client.MmcpClient.generate = stub_generate
    mmcp_client.MmcpClient.generate_batch = stub_generate_batch
    mmcp_client.store_capabilities(CAPS)
    settings().model_id = "kimodo-soma-rp"
    subject, take, frames, yaw = load_subject()
    ref_arm = copy_rig(subject, "Reference_yaw0", 0.0)
    gltf_to_blender.bake_gltf_to_armature(take, ref_arm, start_frame=frames[0], action_name="Reference")
    ref = world_pose(ref_arm, frames)
    REPORT["subject"] = {"armature": subject.name, "bones": len(subject.pose.bones), "frames": [frames[0], frames[-1]], "yaw": yaw}
    return subject, take, frames, yaw, ref


def run():
    try:
        subject, take, frames, yaw, ref = setup()
    except Exception:  # noqa: BLE001
        REPORT["setup_error"] = traceback.format_exc()
        return finish()
    queue = list(SCENARIOS)
    gen = None

    def tick():
        global CURRENT
        nonlocal gen
        try:
            if gen is None:
                if not queue:
                    return finish()
                CURRENT, fn = queue.pop(0)
                REPORT["scenarios"][CURRENT] = {"checks": [], "ok": None}
                print(f"--- {CURRENT}")
                gen = fn(subject, take, frames, yaw, ref)
            next(gen)
        except StopIteration:
            REPORT["scenarios"][CURRENT]["ok"] = True
            print(f"ok    {CURRENT}")
            gen = None
        except Exception:  # noqa: BLE001
            REPORT["scenarios"][CURRENT]["ok"] = False
            REPORT["scenarios"][CURRENT]["error"] = traceback.format_exc()
            print(f"FAIL  {CURRENT}\n{traceback.format_exc()}")
            gen = None
        return 0.2

    bpy.app.timers.register(tick, first_interval=0.5)


def finish():
    passed = sum(1 for s in REPORT["scenarios"].values() if s["ok"])
    failed = sum(1 for s in REPORT["scenarios"].values() if s["ok"] is False)
    REPORT["summary"] = {"passed": passed, "failed": failed, "setup_error": "setup_error" in REPORT}
    path = os.path.join(OUT, "report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(REPORT, f, indent=1, default=str)
    print(f"{passed} passed, {failed} failed -> {path}")
    if not ARGS["keep_open"]:
        bpy.ops.wm.quit_blender()
    return None


if __name__ == "__main__":
    # The splash would sit over the viewport in every screenshot.
    bpy.context.preferences.view.show_splash = False
    addon_utils.enable("animatica_blender", default_set=True)
    run()
