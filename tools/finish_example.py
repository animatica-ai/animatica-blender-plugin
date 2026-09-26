"""Give an example its key poses, in a running, signed-in Blender.

build_examples.py builds everything a background Blender can: the set, the
character, the prompt blocks, the props. Key poses cannot be made there. They
come from Generate Pose, which calls the server as the signed-in user, and
the sign-in lives in the running Blender's preferences. So an example with
``key_poses`` is built as ``<id>.base.blend``, and this finishes it the way an
artist would: go to the frame, Generate Pose, then put the pose where it
belongs and turn it to face the right way.

Generate Pose is modal. Its bake lands on a timer tick *between* script runs,
never during one, so this is a small state machine you advance one run at a
time — from the Blender MCP, or the Python console::

    fe = load("tools/finish_example.py")
    fe.begin("dist/examples/walk-sit.base.blend", "dist/examples")
    fe.step()        # again and again, until it says "done"
    fe.publish()     # after looking at it

``publish`` names the file by its content and updates examples.json beside it,
exactly as build_examples.py does for the rest.
"""

from __future__ import annotations

import importlib.util
import json
import math
import pathlib

import bpy
from mathutils import Matrix, Vector

SEED = 7        # fixed, so rebuilding an example gives it the same poses

_state: dict = {}


def _builder():
    path = pathlib.Path(__file__).with_name("build_examples.py")
    spec = importlib.util.spec_from_file_location("build_examples", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def begin(base_path, out_dir):
    """Open the base file and queue its key poses."""
    base_path = pathlib.Path(base_path).resolve()
    bpy.ops.wm.open_mainfile(filepath=str(base_path))
    scene = bpy.context.scene
    queue = json.loads(scene.get("animatica_key_poses_pending", "[]"))
    if not queue:
        raise RuntimeError(f"{base_path.name} has no key poses waiting")
    _state.clear()
    _state.update(id=scene["animatica_example"], out_dir=pathlib.Path(out_dir).resolve(),
                  queue=queue, current=None, views=_views())
    return f"{_state['id']}: {len(queue)} key poses to make"


def _view_spaces():
    for screen in bpy.data.screens:
        for i, area in enumerate(screen.areas):
            if area.type == "VIEW_3D":
                yield (screen.name, i), area.spaces[0].region_3d


def _views():
    """The builder's framing, as the base file opened with it."""
    return {key: (r3d.view_location.copy(), r3d.view_distance,
                  r3d.view_rotation.copy(), r3d.view_perspective)
            for key, r3d in _view_spaces()}


def _restore_views(views):
    # Saving writes the running window's UI into the file, and the live
    # viewport is whatever it was last turned to — one publish went out
    # opening on a flat side view. Put back what the builder chose.
    for key, r3d in _view_spaces():
        if key in views:
            (r3d.view_location, r3d.view_distance,
             r3d.view_rotation, r3d.view_perspective) = views[key]


def step():
    """Do the next thing, and say what it was."""
    s = bpy.context.scene.animatica
    if s.is_generating:
        return f"generating the pose at frame {_state['current']['frame']}…"
    if _state.get("current") is not None:
        kp = _state.pop("current")
        _state["current"] = None
        if not _keyed_at(s.target_armature, kp["frame"]):
            raise RuntimeError(f"Generate Pose left no key at frame {kp['frame']} "
                               f"— see the Info log")
        settle(s.target_armature, kp)
        return f"frame {kp['frame']}: posed, placed at {tuple(kp['at'])}, facing {kp['facing']}"
    if _state["queue"]:
        kp = _state["queue"].pop(0)
        _state["current"] = kp
        bpy.context.scene.frame_set(kp["frame"])
        win = bpy.context.window_manager.windows[0]
        area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
        with bpy.context.temp_override(window=win, area=area):
            # EXEC, not INVOKE: invoke opens a dialog that waits for a click.
            bpy.ops.animatica.generate_pose("EXEC_DEFAULT", prompt=kp["prompt"],
                                            seed=SEED, preserve_height=False)
        return f"frame {kp['frame']}: asked for “{kp['prompt']}”"
    return "done — look it over, then publish()"


def _keyed_at(arm, frame):
    ad = arm.animation_data
    action = ad and ad.action
    if action is None:
        return False
    from animatica_blender import constraints_ui
    return any(abs(k.co.x - frame) < 0.5
               for fc in constraints_ui.iter_action_fcurves(action)
               for k in fc.keyframe_points)


def _thigh(arm, side):
    """The top of a leg: walk up from the foot to the bone the pelvis holds.

    From the foot because "LeftFoot" is a canonical end-effector every rig
    here carries, while the thigh is LeftUpLeg on one and LeftLeg on another.
    """
    foot = next((pb for pb in arm.pose.bones if pb.name.split(":")[-1] == f"{side}Foot"), None)
    if foot is None:
        raise RuntimeError(f"{arm.name} has no {side}Foot bone")
    bone = foot
    while bone.parent is not None and bone.parent.parent is not None:
        bone = bone.parent
    return bone


def settle(arm, kp):
    """Move the generated pose to ``at`` and turn it to face ``facing``.

    Generate Pose decides the body; where it stands and which way it faces
    are the scene's to say. Turning the whole pose about the vertical through
    the root — the skill's Hips trick, in world space so it does not depend on
    how a rig's root bone is oriented — then keying the root again.
    """
    builder = _builder()
    scene = bpy.context.scene
    scene.frame_set(kp["frame"])
    bpy.context.view_layer.update()

    root = next(pb for pb in arm.pose.bones if pb.parent is None)
    mw = arm.matrix_world
    left = mw @ _thigh(arm, "Left").head
    right = mw @ _thigh(arm, "Right").head
    forward = (left - right).cross(Vector((0, 0, 1)))
    heading = math.atan2(forward.y, forward.x)
    # Rest forward is -Y; "back" is half a turn from it.
    want = math.atan2(-1.0, 0.0) + builder.FACING[kp["facing"]]
    turn = Matrix.Rotation(want - heading, 4, "Z")

    world = mw @ root.matrix
    pivot = world.translation.copy()
    target = Vector((kp["at"][0], kp["at"][1], pivot.z))
    world = Matrix.Translation(target) @ turn @ Matrix.Translation(-pivot) @ world
    root.matrix = mw.inverted() @ world
    bpy.context.view_layer.update()

    root.keyframe_insert("location", frame=kp["frame"])
    rot = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}
    root.keyframe_insert(rot.get(root.rotation_mode, "rotation_euler"), frame=kp["frame"])


def publish():
    """Save the finished example beside its base, named by its content."""
    builder = _builder()
    ex = next(e for e in builder.EXAMPLES if e["id"] == _state["id"])
    scene = bpy.context.scene
    if "animatica_key_poses_pending" in scene:
        del scene["animatica_key_poses_pending"]
    scene.frame_set(1)
    _restore_views(_state["views"])
    out_dir = _state["out_dir"]
    built = out_dir / f"{ex['id']}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(built), compress=True, copy=True)
    manifest_path = out_dir / "examples.json"
    manifest = (json.loads(manifest_path.read_text()) if manifest_path.is_file()
                else {"version": 1, "fps": builder.FPS, "examples": []})
    path = builder.publish(ex, built, manifest)
    builder.write_manifest(out_dir, manifest)
    return path.name
