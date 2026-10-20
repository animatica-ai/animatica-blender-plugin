"""A looped take has no start anchor, and is put back where the character stands.

The request always carried a start anchor (``root_path`` at frame 0, where the
root bone stands). The server settles a cycle as a whole and then pins the hips
to that anchor on frame 0 alone, so the last frame no longer met the first: a
small jolt at every wrap. A loop whose only constraint would be the anchor now
goes out without it (``request_builder.build_request``), and the take, which
comes back starting at the origin, is moved rigidly onto the character
(``loop.place``). Anything else in the request is in world space and keeps the
anchor: a ground, a pose key, a pin, a waypoint, a root path; so does a control
rig. Without a constraint on frame 0, ``request_builder.places_start`` says so.

Covered:

* the request: Loop alone sends no constraints; the same without Loop, with a
  pose key, with a ground, and on a control rig keeps the start anchor; the
  anchor and ``start_spot`` agree on where the character stands;
* ``loop.place``: on a rig turned and moved in the world, the root's head on the
  first frame lands on the spot, every frame moves by the same vector, the
  height stays, and the travel inside the take is as it was;
* key types: ``operators.anchor_frames_for`` never counted the anchor's frame,
  so dropping the anchor makes no frame an authored one that was not.

A SOMA30 rig built from the bundled joints stands in for the character.

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_loop_start_anchor.py

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

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=False)

from animatica_blender import constraints_ui, coords, loop, operators, request_builder  # noqa: E402

FAILS: list[str] = []
LOOPS = {"supports_loop": True, "supports_retargeting": True}
WITH_GROUND = dict(LOOPS, supported_constraints=["root_path", "pose_keyframe", "ground_height"])
EPS = 1e-4


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)


def make_rig(location=(2.0, -1.0, 0.0), yaw=90.0):
    """A SOMA30 rig turned about Z and moved, so world and local differ."""
    clear_scene()
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 60
    r = bpy.ops.animatica.import_canonical_skeleton(source='DEFAULT', with_body=False)
    assert r == {'FINISHED'}, r
    arm = next(o for o in scene.objects if o.type == 'ARMATURE')
    arm.location = location
    arm.rotation_euler = (0.0, 0.0, math.radians(yaw))
    bpy.context.view_layer.update()
    return scene, arm


def request(scene, arm, *, loop_on, caps=LOOPS):
    """A Generate over frames 1-60 from one prompt block, the way the operator makes it."""
    s = scene.animatica
    s.target_armature = arm
    s.loop = loop_on
    s.prompt_blocks.clear()
    blk = s.prompt_blocks.add()
    blk.prompt, blk.frame_start, blk.frame_end = "walks forward", 1, 60
    blk.enabled = True
    return request_builder.build_request(
        model_id="test", model_caps=caps, armature_obj=arm, prompt_blocks=s.prompt_blocks,
        settings=s, scene=scene, constraint_objects=constraints_ui.walk_scene_constraints(scene),
    )


def anchors(req):
    return [c for c in req.get("constraints", []) if c["type"] == "root_path" and c["frames"] == [0]]


def key_pose(arm, bone, frame):
    pb = arm.pose.bones[bone]
    pb.rotation_mode = 'QUATERNION'
    pb.rotation_quaternion = (0.966, 0.259, 0.0, 0.0)
    pb.keyframe_insert("rotation_quaternion", frame=frame)


# --- 1. the request ----------------------------------------------------------

def test_request():
    scene, arm = make_rig()
    root = request_builder.main_root(arm)

    req = request(scene, arm, loop_on=True)
    check("Loop alone: options.loop is set", req["options"].get("loop") is True)
    check("...and no constraints at all", "constraints" not in req, req.get("constraints"))
    check("...so nothing places the start", request_builder.places_start(req) is False)

    req = request(scene, arm, loop_on=False)
    a = anchors(req)
    check("no Loop: the start anchor is there", len(a) == 1, len(a))
    check("...and places the start", request_builder.places_start(req) is True)
    check("(no Loop: options.loop is not set)", "loop" not in req["options"])

    spot = request_builder.start_spot(arm)
    world = (arm.matrix_world @ root.matrix).translation
    check("start_spot is the root's world XY",
          spot is not None and abs(spot[0] - world.x) < EPS and abs(spot[1] - world.y) < EPS, spot)
    anchor = request_builder._start_anchor(arm, [], ())
    want_x, _, want_z = coords.blender_pos_to_mmcp((spot[0], spot[1], 0.0))
    x, z = anchor["positions_xz"][0]
    check("the anchor stands where start_spot says",
          abs(x - want_x) < EPS and abs(z - want_z) < EPS and abs(x - spot[0]) < EPS
          and abs(z + spot[1]) < EPS, (x, z, spot))
    if a:
        check("...and is the one the request carries", a[0]["positions_xz"][0] == anchor["positions_xz"][0])

    # a pose key on the first frame pins it with its own root position; a later
    # one leaves frame 0 to the anchor
    key_pose(arm, "Spine1", 1)
    req = request(scene, arm, loop_on=True)
    poses = [c for c in req.get("constraints", []) if c["type"] == "pose_keyframe"]
    check("Loop + a pose key on frame 1: the pose is sent", len(poses) == 1, len(poses))
    check("...and it places the start", request_builder.places_start(req) is True)
    check("...Loop is still set", req["options"].get("loop") is True)

    scene, arm = make_rig()
    key_pose(arm, "Spine1", 20)
    req = request(scene, arm, loop_on=True)
    check("Loop + a pose key on a later frame: the anchor stays", len(anchors(req)) == 1, req.get("constraints"))
    check("...the pose is sent too",
          any(c["type"] == "pose_keyframe" and c["frame"] == 19 for c in req.get("constraints", [])))
    check("...and the start is placed", request_builder.places_start(req) is True)

    scene, arm = make_rig()
    plane = bpy.data.meshes.new("Floor")
    plane.from_pydata([(-8, -8, 0.3), (8, -8, 0.3), (8, 8, 0.3), (-8, 8, 0.3)], [], [(0, 1, 2, 3)])
    floor = bpy.data.objects.new("Floor", plane)
    scene.collection.objects.link(floor)
    bpy.context.view_layer.update()
    req = request(scene, arm, loop_on=True, caps=WITH_GROUND)
    kinds = [c["type"] for c in req.get("constraints", [])]
    check("Loop + a raised ground: the ground is sent", "ground_height" in kinds, kinds)
    check("...with the anchor", len(anchors(req)) == 1, kinds)
    check("...and the start is placed", request_builder.places_start(req) is True)

    scene, arm = make_rig()
    real = request_builder.is_control_rig
    request_builder.is_control_rig = lambda _arm: True
    try:
        req = request(scene, arm, loop_on=True)
    finally:
        request_builder.is_control_rig = real
    check("Loop on a control rig: the anchor stays", len(anchors(req)) == 1, req.get("constraints"))
    check("...and the start is placed", request_builder.places_start(req) is True)
    check("...Loop is still set", req["options"].get("loop") is True)


# --- 2. loop.place -----------------------------------------------------------

LO, CUT = 5, 45


def key_travel(arm):
    """The main root travelling over frames LO..CUT from near the origin, with a
    little bob up and a turn on it: the head moves, its height varies, and the
    rotation must not move the spot."""
    root = request_builder.main_root(arm)
    root.rotation_mode = 'QUATERNION'
    for f in range(LO, CUT + 1):
        t = (f - LO) / 30.0
        root.location = (0.05 + 0.8 * t, 0.03 * math.sin(t * 6.0), 0.02 + 0.5 * t)
        root.rotation_quaternion = (math.cos(t), 0.0, 0.0, math.sin(t))
        root.keyframe_insert("location", frame=f)
        root.keyframe_insert("rotation_quaternion", frame=f)
    return root


def world_heads(scene, arm, root, frames):
    """The root's head in the world on each frame, read from the evaluated pose."""
    keep = scene.frame_current
    out = {}
    for f in frames:
        scene.frame_set(f)
        out[f] = (arm.matrix_world @ root.head).copy()
    scene.frame_set(keep)
    return out


def location_key(action, root, frame):
    return tuple(fc.evaluate(frame) for fc in sorted(
        (fc for fc in constraints_ui.iter_action_fcurves(action)
         if fc.data_path == f'pose.bones["{root.name}"].location'), key=lambda fc: fc.array_index))


def test_place():
    scene, arm = make_rig(location=(3.0, -2.0, 0.5), yaw=90.0)
    root = key_travel(arm)
    act = arm.animation_data.action
    frames = range(LO, CUT + 1)
    before = world_heads(scene, arm, root, frames)
    k_lo, k_cut = location_key(act, root, LO), location_key(act, root, CUT)

    spot = (-1.5, 4.0)
    check("(the travel starts off the spot)",
          math.hypot(before[LO].x - spot[0], before[LO].y - spot[1]) > 1.0, tuple(before[LO]))
    shift = loop.place(arm, act, (LO, CUT), spot)
    after = world_heads(scene, arm, root, frames)

    check("the root's head on the first frame is on the spot",
          abs(after[LO].x - spot[0]) < EPS and abs(after[LO].y - spot[1]) < EPS, tuple(after[LO]))
    dx, dy = after[LO].x - before[LO].x, after[LO].y - before[LO].y
    worst = max(max(abs((after[f].x - before[f].x) - dx), abs((after[f].y - before[f].y) - dy))
                for f in frames)
    check("every frame moved by the same vector", worst < EPS, f"{worst:.2e}")
    dz = max(abs(after[f].z - before[f].z) for f in frames)
    check("the height did not change", dz < EPS, f"{dz:.2e}")
    check("the shift is returned in cm", abs(shift - math.hypot(dx, dy) * 100.0) < 1e-2, shift)
    k2_lo, k2_cut = location_key(act, root, LO), location_key(act, root, CUT)
    d = max(abs((k2_cut[i] - k2_lo[i]) - (k_cut[i] - k_lo[i])) for i in range(3))
    check("the travel inside the take (key(cut) - key(lo)) is as it was", d < 1e-6, f"{d:.2e}")

    again = loop.place(arm, act, (LO, CUT), spot)
    check("placed again: nothing moves", again < 1e-3, again)
    check("no root, no shift", loop.place(None, act, (LO, CUT), spot) == 0.0
          and loop.place(arm, None, (LO, CUT), spot) == 0.0)


# --- 3. key types ------------------------------------------------------------

def test_anchor_frames():
    """The anchor's frame was never counted among the artist's, so a request
    without it types every frame as before."""
    scene, arm = make_rig()
    req = request(scene, arm, loop_on=False)
    check("(the request has the anchor, on request frame 0)", len(anchors(req)) == 1)
    lo, hi = 1, 60
    got = operators.anchor_frames_for(req, None, lo, hi)
    check("the anchor's frame is not an authored one", lo not in got and len(got) == 0, sorted(got))

    loop_req = request(scene, arm, loop_on=True)
    check("...the same with the anchor dropped",
          set(operators.anchor_frames_for(loop_req, None, lo, hi)) == set(got))

    scene, arm = make_rig()
    key_pose(arm, "Spine1", 20)
    req = request(scene, arm, loop_on=True)
    got = operators.anchor_frames_for(req, None, lo, hi)
    check("with a pose key on frame 20: only that frame, not the anchor's",
          set(got) == {20} and lo not in got, sorted(got))


def main():
    test_request()
    test_place()
    test_anchor_frames()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        FAILS.append("exception")
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
