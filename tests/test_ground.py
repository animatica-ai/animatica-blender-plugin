"""The ground under the character: read from the scene, sent to the server,
under the waypoints and the Autoposer's floor.

Two roofs like the rooftop chase they were written for: the upper one's top
at z=0, a 2.5 m gap, the lower one's at z=-0.6, the street 6.6 m down. The
character stands on the upper roof; its own body and control shapes must not
count as ground.

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_ground.py

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

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

from animatica_blender import coords, ground, request_builder, waypoints  # noqa: E402

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def box(name, x0, x1, y0, y1, z0, z1):
    bpy.ops.mesh.primitive_cube_add()
    ob = bpy.context.active_object
    ob.name = name
    ob.location = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
    ob.scale = ((x1 - x0) / 2, (y1 - y0) / 2, (z1 - z0) / 2)
    bpy.context.view_layer.update()
    return ob


def build():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    box("BLD_A", -4, 4, -14, 4, -6.6, 0.0)
    box("BLD_B", -4, 4, -34, -16.5, -6.6, -0.6)
    box("STREET", -20, 20, -40, 10, -6.8, -6.6)
    arm_data = bpy.data.armatures.new("Rig")
    arm = bpy.data.objects.new("Rig", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    b = arm_data.edit_bones.new("Hips")
    b.head, b.tail = (0, 0, 0.99), (0, 0, 1.2)
    bpy.ops.object.mode_set(mode='OBJECT')
    body = box("Body", -0.3, 0.3, -0.2, 0.2, 0.0, 1.8)        # the character's mesh, on the roof
    body.modifiers.new("Armature", 'ARMATURE').object = arm
    shape = box("CtrlShape", -0.5, 0.5, -0.5, 0.5, 2.0, 2.1)  # a control shape, above the head
    arm.pose.bones["Hips"].custom_shape = shape
    bpy.context.view_layer.update()
    return arm


def mmcp_root(x, y, z):
    return list(coords.blender_pos_to_mmcp((x, y, z)))


def route_constraints():
    """A key pose on the upper roof at frame 0 and a waypoint on the lower roof at frame 60."""
    x_m, _y, z_m = coords.blender_pos_to_mmcp((0.0, -22.0, 0.0))
    return [
        {"type": "pose_keyframe", "frame": 0, "joint_rotations": {"Hips": [0, 0, 0, 1]},
         "root_position": mmcp_root(0.0, 0.0, 0.99)},
        {"type": "root_path", "frames": [60], "positions_xz": [[x_m, z_m]]},
    ]


def test_rays(arm):
    scene = bpy.context.scene
    z = ground.surface_below(scene, 0.0, 0.0, 2.5, arm=arm)
    check("the character's own body is not ground", z is not None and abs(z) < 1e-4, z)
    check("the lower roof", abs(ground.surface_below(scene, 0, -20, 1.0, arm=arm) + 0.6) < 1e-4)
    check("under the gap: the street",
          abs(ground.surface_below(scene, 0, -15.25, 1.0, arm=arm) + 6.6) < 1e-4)
    check("nothing within reach: None", ground.surface_below(scene, 0, 500, 1.0, arm=arm) is None)


def test_route(arm):
    scene = bpy.context.scene
    c = ground.ground_constraint(scene, arm, route_constraints())
    check("a ground_height constraint", c is not None and c["type"] == "ground_height")
    pts = c["points"]
    # MMCP [x, z, y] with z = -blender_y
    upper = [p for p in pts if p[1] < 14.0]
    lower = [p for p in pts if p[1] > 16.5]
    gap = [p for p in pts if 14.05 < p[1] < 16.45]
    check("upper roof points at 0", upper and all(abs(p[2]) < 1e-3 for p in upper), len(upper))
    check("lower roof points at -0.6", lower and all(abs(p[2] + 0.6) < 1e-3 for p in lower), len(lower))
    check("no points over the gap (the street is not ground)", not gap, len(gap))
    check("lanes either side of the route", any(abs(p[0]) > 0.2 for p in pts))
    check("a few cm apart along the route", 400 < len(pts) < 2000, len(pts))


def test_flat_and_raised(arm):
    scene = bpy.context.scene
    b = bpy.data.objects["BLD_B"]
    b.location.z += 0.6                       # both roofs at 0: the flat the model assumes
    bpy.context.view_layer.update()
    check("flat ground at 0 is not sent", ground.ground_constraint(scene, arm, route_constraints()) is None)
    for name in ("BLD_A", "BLD_B"):
        bpy.data.objects[name].location.z += 5.0   # both roofs 5 m up: flat, but not at 0
    arm.location.z = 5.0
    bpy.context.view_layer.update()
    cons = route_constraints()
    cons[0]["root_position"] = mmcp_root(0.0, 0.0, 5.99)
    c = ground.ground_constraint(scene, arm, cons)
    check("a raised flat roof is sent", c is not None and all(abs(p[2] - 5.0) < 1e-3 for p in c["points"]))
    for name in ("BLD_A", "BLD_B"):
        bpy.data.objects[name].location.z -= 5.0
    b.location.z -= 0.6
    arm.location.z = 0.0
    bpy.context.view_layer.update()


def test_request(arm):
    scene = bpy.context.scene
    caps_old = {"supported_constraints": ["root_path", "effector_target", "pose_keyframe"]}
    caps_new = {"supported_constraints": caps_old["supported_constraints"] + ["ground_height"]}
    a = route_constraints()
    request_builder._add_ground(a, model_caps=caps_old, scene=scene, armature_obj=arm)
    check("not sent to a server that doesn't list it", all(c["type"] != "ground_height" for c in a))
    check("...and the request stays 1.0", request_builder._protocol_version(a) == "1.0")
    b = route_constraints()
    request_builder._add_ground(b, model_caps=caps_new, scene=scene, armature_obj=arm)
    check("sent to a server that lists it", any(c["type"] == "ground_height" for c in b))
    check("...as protocol 1.3", request_builder._protocol_version(b) == "1.3")


def test_waypoints(arm):
    scene = bpy.context.scene
    z = waypoints.ground_z(scene, (0.0, -20.0), arm)
    check("a waypoint over the lower roof is on it", abs(z + 0.6) < 1e-4, z)
    obj = waypoints.create_marker(scene, 30, (0.0, -5.0), owner=arm, z=waypoints.ground_z(scene, (0.0, -5.0), arm))
    check("placed on the upper roof", abs(obj.location.z - waypoints.MARKER_LIFT) < 1e-4, obj.location.z)
    obj.location.x, obj.location.y = 0.0, -20.0          # dragged onto the lower roof
    bpy.context.view_layer.update()
    if abs(obj.location.z - (-0.6 + waypoints.MARKER_LIFT)) > 1e-3:
        # depsgraph handlers do not always fire headless; call it as Blender would
        waypoints._on_depsgraph(scene, bpy.context.evaluated_depsgraph_get())
        dg = type("D", (), {"updates": [type("U", (), {"id": obj, "is_updated_transform": True})()]})()
        waypoints._on_depsgraph(scene, dg)
    check("dragged onto the lower roof, it drops onto it",
          abs(obj.location.z - (-0.6 + waypoints.MARKER_LIFT)) < 1e-3, obj.location.z)


def test_autoposer_shift():
    """pose_on_ground: the targets go down by the floor, the solve comes back up. Animatica
    Marionette's, a separate add-on (its own repo): checked only when it is installed."""
    try:
        from animatica_autoposer.autoposer import poser
    except ImportError:
        print("SKIP the Autoposer's ground shift: Animatica Marionette is not installed")
        return
    import numpy as np

    seen = {}

    class Eng:
        def pose(self, eff, **kw):
            seen["eff"] = eff
            return {"joints": np.zeros((3, 3), np.float32), "root": np.zeros(3, np.float32), "names": []}

    keep = poser.floor_height
    poser.floor_height = lambda arm, eff: -0.6
    try:
        eff = [{"joint": "Hips", "type": "pos", "pos": [0.0, 0.39, 0.0]},
               {"joint": "Head", "type": "rot", "rot": [1, 0, 0, 0, 1, 0]}]
        out = poser.pose_on_ground(Eng(), None, eff, floor=True)
    finally:
        poser.floor_height = keep
    check("targets lowered onto the poser's floor", abs(seen["eff"][0]["pos"][1] - 0.99) < 1e-6)
    check("rotation targets untouched", seen["eff"][1] == eff[1])
    check("the solve is put back on the roof", abs(float(out["joints"][0][1]) + 0.6) < 1e-6
          and abs(float(out["root"][1]) + 0.6) < 1e-6)
    check("the caller's targets untouched", eff[0]["pos"][1] == 0.39)


def main():
    try:
        import addon_utils
        addon_utils.enable("animatica_blender", default_set=False)
        addon_utils.enable("animatica_autoposer", default_set=False)   # when installed: Pro is its own add-on
        arm = build()
        test_rays(arm)
        test_route(arm)
        test_flat_and_raised(arm)
        test_request(arm)
        test_waypoints(arm)
        test_autoposer_shift()
    except Exception:
        traceback.print_exc()
        FAILS.append("crashed")
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
    sys.exit(1 if FAILS else 0)


main()
