"""A baked take lands in the world where the server put it, whichever way the
armature object is turned.

The server generates in world space with its own heading (the root path,
waypoints and pose keyframes are all sent in world space), and it returns
joint rotations relative to the rig's rest pose as it stands at yaw 0. The
bake used to fold the object's rotation about Z into every bone as well, so
a character turned 180° in object mode walked backwards with its knees
bending the wrong way, and one turned 30° walked at a 30° skew.

Runs headless, without a server, on a synthetic take::

    blender -b --factory-startup --python tests/test_bake_object_rotation.py

``--factory-startup`` keeps an installed copy of the addon from shadowing
the checkout.
"""

from __future__ import annotations

import base64
import math
import os
import struct
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bpy  # noqa: E402
from mathutils import Matrix, Quaternion, Vector  # noqa: E402

from animatica_blender import _bake_common, constraints_ui, gltf_to_blender  # noqa: E402

FPS = 30
N_FRAMES = 12
START = 1
TOL = 1e-4
YAWS_DEG = (0.0, 90.0, 180.0, 340.0)

# MMCP Y-up -> Blender Z-up, as in the addon.
S = Matrix(((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)))
RX90 = Matrix.Rotation(math.radians(90.0), 3, 'X')

# A Z-up rig with canonical joint names, facing -Y (Blender's front).
BONES = [
    ("Hips",       None,         (0.0, 0.0, 1.0),  (0.0, 0.0, 1.1)),
    ("Spine",      "Hips",       (0.0, 0.0, 1.1),  (0.0, 0.0, 1.4)),
    ("LeftUpLeg",  "Hips",       (0.1, 0.0, 1.0),  (0.1, 0.0, 0.55)),
    ("LeftLeg",    "LeftUpLeg",  (0.1, 0.0, 0.55), (0.1, 0.0, 0.1)),
    ("LeftFoot",   "LeftLeg",    (0.1, 0.0, 0.1),  (0.1, -0.15, 0.05)),
    ("RightUpLeg", "Hips",       (-0.1, 0.0, 1.0), (-0.1, 0.0, 0.55)),
    ("RightLeg",   "RightUpLeg", (-0.1, 0.0, 0.55), (-0.1, 0.0, 0.1)),
    ("RightFoot",  "RightLeg",   (-0.1, 0.0, 0.1), (-0.1, -0.15, 0.05)),
]
JOINTS = [b[0] for b in BONES]
FRAMES = list(range(START, START + N_FRAMES))


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------

def reset_scene() -> None:
    # Not ``read_factory_settings``: that rescans the addons directory and
    # complains about the checkout's package shadowing an installed copy.
    bpy.data.batch_remove(list(bpy.data.objects) + list(bpy.data.armatures) + list(bpy.data.actions))
    bpy.context.scene.render.fps = FPS
    bpy.context.scene.frame_set(1)


def make_rig(name: str, *, y_up: bool = False, yaw_deg: float = 0.0) -> bpy.types.Object:
    """A test armature. ``y_up`` lays the bones out in a Y-up armature space
    and turns the object 90° about X, the way a Mixamo import arrives, so
    the rig stands in the same world rest pose as the Z-up one."""
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    ebones = data.edit_bones
    for bone_name, parent, head, tail in BONES:
        eb = ebones.new(bone_name)
        eb.head = Vector(head)
        eb.tail = Vector(tail)
        if parent is not None:
            eb.parent = ebones[parent]
    if y_up:
        for eb in ebones:
            eb.transform(RX90.transposed().to_4x4(), roll=True)
    bpy.ops.object.mode_set(mode='OBJECT')
    obj.rotation_mode = 'XYZ'
    obj.rotation_euler = (math.radians(90.0) if y_up else 0.0, 0.0, math.radians(yaw_deg))
    # The object's position must not matter either: root positions are world.
    obj.location = (0.7, -1.3, 0.0)
    bpy.context.view_layer.update()
    return obj


def world_pose(arm: bpy.types.Object, frames) -> dict[tuple[int, str], Matrix]:
    scene = bpy.context.scene
    out: dict[tuple[int, str], Matrix] = {}
    for f in frames:
        scene.frame_set(f)
        bpy.context.view_layer.update()
        for pb in arm.pose.bones:
            out[(f, pb.name)] = (arm.matrix_world @ pb.matrix).copy()
    return out


def assert_same_pose(got: dict, want: dict, label: str, *, remap=None) -> None:
    worst = 0.0
    where = None
    for key, m_want in want.items():
        key_got = remap(key) if remap else key
        m_got = got[key_got]
        for i in range(4):
            for j in range(4):
                d = abs(m_got[i][j] - m_want[i][j])
                if d > worst:
                    worst, where = d, key
    if worst > TOL:
        raise AssertionError(f"{label}: world pose differs by {worst:.4f} at {where}")


# ---------------------------------------------------------------------------
# A synthetic take, in the shape the server returns
# ---------------------------------------------------------------------------

def to_xyzw(q: Quaternion) -> tuple[float, float, float, float]:
    return (q.x, q.y, q.z, q.w)


def synthetic_motion() -> tuple[dict[str, list], dict[str, list]]:
    """Per-joint local rotations (MMCP frame, x y z w) and the root's world
    positions: the hips turn and pitch, the legs swing and the knees bend."""
    rot: dict[str, list] = {n: [] for n in JOINTS}
    pos: dict[str, list] = {"Hips": []}
    for t in range(N_FRAMES):
        heading = 0.4 + 0.05 * t
        rot["Hips"].append(to_xyzw(
            Quaternion((0.0, 1.0, 0.0), heading) @ Quaternion((1.0, 0.0, 0.0), 0.1)))
        rot["Spine"].append(to_xyzw(Quaternion((0.0, 0.0, 1.0), 0.2 * math.sin(0.5 * t))))
        swing = 0.3 * math.sin(0.6 * t)
        rot["LeftUpLeg"].append(to_xyzw(Quaternion((1.0, 0.0, 0.0), -0.4 + swing)))
        rot["LeftLeg"].append(to_xyzw(Quaternion((1.0, 0.0, 0.0), 0.7)))
        rot["LeftFoot"].append(to_xyzw(Quaternion((1.0, 0.0, 0.0), -0.2)))
        rot["RightUpLeg"].append(to_xyzw(Quaternion((1.0, 0.0, 0.0), -0.4 - swing)))
        rot["RightLeg"].append(to_xyzw(Quaternion((1.0, 0.0, 0.0), 0.5)))
        rot["RightFoot"].append(to_xyzw(Quaternion((0.0, 1.0, 0.0), 0.1)))
        pos["Hips"].append((0.05 * t, 1.0 + 0.01 * math.sin(t), 0.03 * t))
    return rot, pos


def make_gltf(rotations: dict[str, list], translations: dict[str, list]) -> dict:
    idx = {n: i for i, n in enumerate(JOINTS)}
    nodes = [{"name": n, "translation": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0, 1.0]}
             for n in JOINTS]
    for name, parent, _h, _t in BONES:
        if parent is not None:
            nodes[idx[parent]].setdefault("children", []).append(idx[name])

    comps = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}
    blob = bytearray()
    views: list[dict] = []
    accessors: list[dict] = []

    def push(floats: list[float], typ: str) -> int:
        offset = len(blob)
        blob.extend(struct.pack(f"<{len(floats)}f", *floats))
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(floats) * 4})
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126,
                          "count": len(floats) // comps[typ], "type": typ})
        return len(accessors) - 1

    n_frames = len(next(iter(rotations.values())))
    times = push([f / FPS for f in range(n_frames)], "SCALAR")
    samplers: list[dict] = []
    channels: list[dict] = []
    for name, quats in rotations.items():
        out = push([c for q in quats for c in q], "VEC4")
        samplers.append({"input": times, "output": out, "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": idx[name], "path": "rotation"}})
    for name, points in translations.items():
        out = push([c for p in points for c in p], "VEC3")
        samplers.append({"input": times, "output": out, "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": idx[name], "path": "translation"}})

    uri = "data:application/octet-stream;base64," + base64.b64encode(bytes(blob)).decode("ascii")
    return {
        "asset": {"version": "2.0"},
        "nodes": nodes,
        "buffers": [{"byteLength": len(blob), "uri": uri}],
        "bufferViews": views,
        "accessors": accessors,
        "animations": [{"name": "sample_0", "samplers": samplers, "channels": channels}],
        "extensions": {"MMCP_motion": {"fps": FPS, "samples": [{"num_frames": n_frames}]}},
    }


ROTATIONS, TRANSLATIONS = synthetic_motion()
GLTF = make_gltf(ROTATIONS, TRANSLATIONS)


def reference_pose() -> dict:
    """The take baked on the Z-up rig at yaw 0: what every other bake must
    reproduce in world space."""
    reset_scene()
    arm = make_rig("Ref")
    gltf_to_blender.bake_gltf_to_armature(GLTF, arm, start_frame=START)
    return world_pose(arm, FRAMES)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reference_faces_where_the_server_said() -> None:
    """At yaw 0 the hips face the server's heading and sit at its position."""
    reset_scene()
    arm = make_rig("Ref")
    gltf_to_blender.bake_gltf_to_armature(GLTF, arm, start_frame=START)
    rest_forward = Vector((0.0, -1.0, 0.0))
    ml_inv = arm.data.bones["Hips"].matrix_local.to_3x3().transposed()
    for t, f in enumerate(FRAMES):
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        hips = arm.matrix_world @ arm.pose.bones["Hips"].matrix
        got_fwd = hips.to_3x3() @ ml_inv @ rest_forward
        qx, qy, qz, qw = ROTATIONS["Hips"][t]
        want_fwd = S @ Quaternion((qw, qx, qy, qz)).to_matrix() @ S.transposed() @ rest_forward
        assert (got_fwd - want_fwd).length < TOL, \
            f"frame {f}: hips face {tuple(got_fwd)}, server said {tuple(want_fwd)}"
        got_pos = arm.matrix_world @ arm.pose.bones["Hips"].head
        px, py, pz = TRANSLATIONS["Hips"][t]
        want_pos = Vector((px, -pz, py))
        assert (got_pos - want_pos).length < TOL, \
            f"frame {f}: hips at {tuple(got_pos)}, server said {tuple(want_pos)}"


def test_single_bake_ignores_object_yaw() -> None:
    want = reference_pose()
    for yaw in YAWS_DEG:
        reset_scene()
        arm = make_rig("Rig", yaw_deg=yaw)
        gltf_to_blender.bake_gltf_to_armature(GLTF, arm, start_frame=START)
        assert_same_pose(world_pose(arm, FRAMES), want, f"single bake, yaw {yaw}")


def test_y_up_rig_matches_z_up_rig() -> None:
    """A Y-up rig turned upright by its object rotation (Mixamo-style) bakes
    to the same world pose, at any yaw on top of that."""
    want = reference_pose()
    for yaw in YAWS_DEG:
        reset_scene()
        arm = make_rig("Rig", y_up=True, yaw_deg=yaw)
        gltf_to_blender.bake_gltf_to_armature(GLTF, arm, start_frame=START)
        assert_same_pose(world_pose(arm, FRAMES), want, f"Y-up rig, yaw {yaw}")


def test_regenerate_splice_matches_single_bake() -> None:
    want = reference_pose()
    for yaw in YAWS_DEG:
        reset_scene()
        arm = make_rig("Rig", yaw_deg=yaw)
        arm.animation_data_create()
        action = bpy.data.actions.new("Take")
        arm.animation_data.action = action
        # Keys either side of the window, so this is a regenerate, not a bake.
        for pb in arm.pose.bones:
            pb.rotation_mode = 'QUATERNION'
            for f in (START - 1, START + N_FRAMES + 5):
                pb.keyframe_insert("rotation_quaternion", frame=f)
        gltf_to_blender.splice_gltf_into_action(
            GLTF, arm, action,
            request_start_frame=START, target_range=(START, START + N_FRAMES - 1),
        )
        assert_same_pose(world_pose(arm, FRAMES), want, f"splice, yaw {yaw}")


def test_per_block_bake_matches_single_bake() -> None:
    want = reference_pose()
    split = START + N_FRAMES // 2
    blocks = [(START, split - 1, "Block A"), (split, START + N_FRAMES - 1, "Block B")]
    for yaw in YAWS_DEG:
        reset_scene()
        arm = make_rig("Rig", yaw_deg=yaw)
        actions = gltf_to_blender.bake_gltf_to_actions_per_block(
            GLTF, arm, blocks=blocks, request_start_frame=START)
        got: dict = {}
        for (fs, fe, _name), action in zip(blocks, actions):
            arm.animation_data.action = action
            _bake_common.ensure_action_slot(arm.animation_data, action, arm)
            got.update(world_pose(arm, range(fs, fe + 1)))
        assert_same_pose(got, want, f"per-block bake, yaw {yaw}")


def test_single_pose_matches_single_bake() -> None:
    want = reference_pose()
    source_frame, target_frame = 5, 40
    for yaw in YAWS_DEG:
        reset_scene()
        arm = make_rig("Rig", yaw_deg=yaw)
        gltf_to_blender.bake_single_pose(
            GLTF, arm, source_frame=source_frame, target_frame=target_frame,
            root_translation="full")
        got = world_pose(arm, [target_frame])
        assert_same_pose(
            got, {k: v for k, v in want.items() if k[0] == START + source_frame},
            f"single pose, yaw {yaw}",
            remap=lambda key: (target_frame, key[1]))


def test_pose_keyframes_round_trip() -> None:
    """A pose the user authored on a turned rig goes out as the world pose
    the user sees (its root carrying the object's yaw), and comes back onto
    the rig as exactly that pose."""
    authored_frame, back_frame = 3, 9
    by_yaw: dict[float, dict] = {}
    for yaw in YAWS_DEG:
        reset_scene()
        arm = make_rig("Rig", yaw_deg=yaw)
        arm.animation_data_create()
        action = bpy.data.actions.new("Authored")
        arm.animation_data.action = action
        for pb in arm.pose.bones:
            pb.rotation_mode = 'QUATERNION'
        hips, knee = arm.pose.bones["Hips"], arm.pose.bones["LeftLeg"]
        hips.rotation_quaternion = Quaternion((0.0, 0.0, 1.0), 0.6) @ Quaternion((1.0, 0.0, 0.0), 0.15)
        hips.location = (0.2, 0.1, 0.05)
        knee.rotation_quaternion = Quaternion((1.0, 0.0, 0.0), 0.8)
        for pb in (hips, knee):
            pb.keyframe_insert("rotation_quaternion", frame=authored_frame)
        hips.keyframe_insert("location", frame=authored_frame)
        authored = world_pose(arm, [authored_frame])

        constraint = constraints_ui.sample_pose_at_frame(
            arm, source_action=action, sample_frame=authored_frame, request_frame=0)
        assert constraint is not None
        by_yaw[yaw] = constraint

        # Back through the bake, onto a fresh copy of the rig turned the same way.
        rotations = {name: [tuple(q)] for name, q in constraint["joint_rotations"].items()}
        gltf = make_gltf(rotations, {"Hips": [tuple(constraint["root_position"])]})
        arm2 = make_rig("Rig2", yaw_deg=yaw)
        gltf_to_blender.bake_single_pose(
            gltf, arm2, source_frame=0, target_frame=back_frame, root_translation="full")
        assert_same_pose(
            world_pose(arm2, [back_frame]), authored, f"round trip, yaw {yaw}",
            remap=lambda key: (back_frame, key[1]))

    # What the server sees: the root turned by the object's yaw, every other
    # joint unchanged, because the rest of the body is described relative to
    # the root.
    base = by_yaw[0.0]["joint_rotations"]
    for yaw, constraint in by_yaw.items():
        sent = constraint["joint_rotations"]
        for name in JOINTS:
            if name == "Hips":
                continue
            d = max(abs(a - b) for a, b in zip(sent[name], base[name]))
            assert d < TOL, f"yaw {yaw}: {name} went out as {sent[name]}, expected {base[name]}"
        qx, qy, qz, qw = base["Hips"]
        r_base = Quaternion((qw, qx, qy, qz)).to_matrix()
        r_yaw = S.transposed() @ Matrix.Rotation(math.radians(yaw), 3, 'Z') @ S
        want = (r_yaw @ r_base).to_quaternion()
        qx, qy, qz, qw = sent["Hips"]
        got = Quaternion((qw, qx, qy, qz))
        assert got.rotation_difference(want).angle < 1e-3, \
            f"yaw {yaw}: root went out as {sent['Hips']}, expected the pose turned by the object's yaw"


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
        else:
            print(f"ok    {fn.__name__}")
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    if code:
        sys.exit(code)
