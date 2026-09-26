# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the example scenes the addon offers under "Try an example".

Each example is a finished, ready-to-generate .blend: a set, a character, the
prompt block at the length the idea needs, and the Animatica sidebar open on
it. Picking one in the addon downloads the file and opens it as an untitled
session; pressing Generate is the only thing left to do.

The scenes are built by this script rather than by hand so that they can be
rebuilt when the addon changes shape, and so that what is in them is written
down somewhere a reviewer can read. Run it with the addon zip and the two
character FBXs from animatica-ai/animatica-assets-public::

    blender --background --factory-startup --python tools/build_examples.py -- \\
        --addon dist/animatica-blender-0.6.0-dev.zip \\
        --assets /path/to/fbx/dir --out dist/examples

It writes one .blend per example and ``examples.json``: the manifest the addon
downloads, with each file's size and sha256.

The ladder itself — which prompt, how long, in what order — lives in
``EXAMPLES`` below and nowhere else; the addon reads it from the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import pathlib
import sys

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

FPS = 24

# The characters, from Animatica's public asset repository. Cesium Man is the
# Khronos glTF sample model and is CC-BY-4.0: the credit travels inside every
# file that uses it, and in the manifest.
CHARACTERS = {
    "hero": {
        "fbx": "animatica-hero.fbx",
        "name": "Animatica Hero",
        "credit": "",
    },
    "cesium": {
        "fbx": "cesium-man.fbx",
        "name": "Cesium Man",
        "credit": ("Cesium Man by Cesium, CC-BY-4.0 — "
                   "https://github.com/KhronosGroup/glTF-Sample-Assets/tree/main/Models/CesiumMan"),
    },
}

# Sets, by feel rather than by location: a clean studio for the first steps,
# an open floor for travel, a ring for a combination, a long runway for speed,
# low light for a creep. All procedural — no textures to license or ship.
SETS = {
    "studio":  {"floor": (0.62, 0.62, 0.64), "world": (0.21, 0.22, 0.25), "sun": 2.5, "key": 900},
    "outdoor": {"floor": (0.34, 0.40, 0.30), "world": (0.46, 0.60, 0.78), "sun": 4.0, "key": 0},
    "ring":    {"floor": (0.55, 0.16, 0.14), "world": (0.10, 0.10, 0.12), "sun": 1.5, "key": 1500},
    "runway":  {"floor": (0.30, 0.31, 0.34), "world": (0.66, 0.70, 0.76), "sun": 3.5, "key": 0},
    "dusk":    {"floor": (0.22, 0.22, 0.26), "world": (0.05, 0.06, 0.10), "sun": 0.6, "key": 700},
}

# The ladder. Read top to bottom it is a short course in what the model does:
# one gesture, travel, a beat of effort, two actions in a row, a change of
# speed, a change of direction, a sequence with three beats. Lengths climb with
# the ideas, and each block is exactly as long as what happens in it.
#
# ``travel`` is roughly how far the character goes, in metres — only used to
# frame the view so the whole motion is on screen when the file opens.
EXAMPLES = [
    {"id": "wave",           "title": "Wave",                  "tier": 0, "seconds": 2.0,
     "prompt": "a person waves hello with their right hand",
     "character": "hero",   "set": "studio",  "travel": 0.0,
     "lesson": "One gesture, standing still. The simplest thing to ask for."},
    {"id": "walk",           "title": "Walk",                  "tier": 1, "seconds": 3.0,
     "prompt": "a person walks forward at a steady pace",
     "character": "cesium", "set": "outdoor", "travel": 3.0,
     "lesson": "Travel. The character goes somewhere, and the trail shows the path."},
    {"id": "jump",           "title": "Jump",                  "tier": 1, "seconds": 2.5,
     "prompt": "a person jumps straight up and lands",
     "character": "hero",   "set": "studio",  "travel": 0.0,
     "lesson": "A beat of effort: wind up, leave the ground, land."},
    {"id": "jab-cross",      "title": "Jab, then cross",       "tier": 2, "seconds": 2.0,
     "blocks": [("a person throws a jab", 1.0),
                ("a person throws a right cross", 1.0)],
     "character": "cesium", "set": "ring",    "travel": 0.0,
     "lesson": "Two actions, two blocks. One action per block, each as long as it "
               "takes; the timeline sets the order."},
    {"id": "run-stop",       "title": "Run, then stop",        "tier": 2, "seconds": 5.0,
     # Split into two blocks, the second one was ignored: whatever it said
     # ("slows down and stops", "stops running and stands still", "walks a
     # few steps and stops"), the model carried the run's momentum through
     # and was still at 3 m/s on the last frame. Two waypoints on one spot,
     # reached at frame 100 and still there at 120, say where to stop: it
     # now slows 2.0 -> 0.7 -> 0.0 m/s and stands.
     "blocks": [("a person runs forward", 3.0),
                ("a person slows down and stops", 2.0)],
     "waypoints": [(1, (0.0, 0.0)), (100, (0.0, -9.6)), (120, (0.0, -9.6))],
     "character": "hero",   "set": "runway",  "travel": 10.0,
     "lesson": "One block runs, the next stops, and two circles on one spot say "
               "where: reached by frame 100, still there at 120."},
    {"id": "press-button",   "title": "Press the button",      "tier": 3, "seconds": 3.0,
     # A hand pin: the red sphere is where the right wrist must be at frame 30,
     # just above the button. The Hero faces -Y, so its right hand is on -X.
     # "Reaches out and presses" walked the character round the pillar to
     # reach across itself; "standing in place" keeps the feet planted.
     #
     # Held back until the server re-pins effectors on the client's rig
     # (motionmcp-kimodo-cloud fix/cleanup-snap): until then the retarget's
     # half reach leaves the wrist 6-11 cm beside the button, and a lesson
     # about pins that misses its pin teaches the opposite.
     "hold": "hand pins miss by 6-11 cm until the server fix is deployed",
     "blocks": [("a person standing in place presses a button in front of them "
                 "with their right hand", 1.75),
                ("a person lowers their right arm", 1.25)],
     "props": [{"kind": "button", "at": (-0.2, -0.62), "height": 1.05}],
     "pins": [{"joint": "RightHand", "keys": [(30, (-0.2, -0.54, 1.16))]}],
     "character": "hero",   "set": "studio",  "travel": 0.6,
     "lesson": "A pin says where a hand must be, and when. Drag the red sphere, or "
               "move its keyframe on the timeline, and generate again."},
    {"id": "kick-ball",      "title": "Kick the ball",         "tier": 3, "seconds": 2.0,
     # A foot pin: the right ankle is pinned just behind the ball at the frame
     # of the kick, and the ball rolls away from that frame on.
     "blocks": [("a person kicks a ball with their right foot", 2.0)],
     "props": [{"kind": "ball", "at": (-0.14, -0.5), "kick_frame": 24}],
     "pins": [{"joint": "RightFoot", "keys": [(24, (-0.14, -0.28, 0.14))]}],
     "character": "hero",   "set": "outdoor", "travel": 1.0,
     "lesson": "Pins work on feet too. The foot meets the ball at the pinned frame; "
               "move the ball and its pin together to kick somewhere else."},
    {"id": "strike-pose",    "title": "Walk and strike a pose", "tier": 3, "seconds": 3.5,
     # Key poses, on their own: standing at frame 1, and the victory pose at
     # frame 84 made with Generate Pose. Between them the blocks say walk,
     # then stop and pose; the key pose says exactly which pose, and where.
     "blocks": [("a person walks forward", 2.0),
                ("a person stops and raises both arms in victory", 1.5)],
     "key_poses": [
         {"frame": 1, "at": (0.0, 0.0), "facing": "forward",
          "prompt": "a person stands in a neutral pose with arms relaxed at sides"},
         # 1.5 m out, not 2.2: the walk block covers about 1.3 m in its two
         # seconds, and a pose further on dragged the character 0.9 m through
         # the pose block with its feet sliding.
         {"frame": 84, "at": (0.0, -1.5), "facing": "forward",
          "prompt": "a person stands with both arms raised high above the head in victory"},
     ],
     "character": "hero",   "set": "studio",  "travel": 1.5,
     "lesson": "A key pose is a keyframe the motion has to hit. This one came from "
               "Generate Pose @ Frame; make your own at any frame, or pose it by hand."},
    {"id": "walk-turn-back", "title": "Walk, turn, walk back", "tier": 3, "seconds": 6.0,
     # Waypoints, because words were not enough: tested five ways, one sentence
     # and two blocks, the model brought the character back in some samples
     # and walked it straight out in others. Three waypoints — here, there by
     # frame 72, back by 144 — land within 4 cm of each at its frame, and the
     # turn between is the model's own. On the Hero rather than Cesium Man for
     # now: any root constraint on a character much smaller than the model's
     # body makes the server stretch the root after the retarget, and the feet
     # slide. That is a server fix; this example should not have to wait on it.
     # "Walks forward" on the way back too: after the turn that is back, and
     # "walks back" reads as walking backwards.
     "blocks": [("a person walks forward", 2.5),
                ("a person turns around", 1.0),
                ("a person walks forward", 2.5)],
     "waypoints": [(1, (0.0, 0.0)), (72, (0.0, -2.5)), (144, (0.0, -0.2))],
     "character": "hero",   "set": "outdoor", "travel": 3.0,
     "lesson": "A block per action sets what happens; each circle on the floor says "
               "where to be, and when. Drag one, or change its frame, and generate "
               "again."},
    {"id": "crouch-creep",   "title": "Creep around the crate", "tier": 4, "seconds": 8.0,
     # Three blocks for three beats, and a curved path for the middle one: the
     # waypoints bend round the crate, and facing along the path turns the
     # body with the route instead of leaving it to sidestep.
     # "Crouches down" alone started the clip already crouched (hips at
     # 0.66 m on frame 1) and the first beat was lost; from standing, it
     # crouches from 1.00 m to 0.39 m by frame 36.
     "blocks": [("a person standing upright slowly crouches down", 1.5),
                ("a person creeps forward slowly in a crouch", 4.5),
                ("a person stands up", 2.0)],
     "props": [{"kind": "crate", "at": (0.0, -1.25), "size": 0.6}],
     "waypoints": [(1, (0.0, 0.0)), (36, (0.0, -0.15)), (90, (0.8, -1.25)),
                   (144, (0.0, -2.45)), (192, (0.0, -2.6))],
     "face_along_path": True,
     "character": "hero",   "set": "dusk",    "travel": 2.6,
     "lesson": "Blocks set the beats, waypoints the route, and facing along the path "
               "turns the body with it. Resize a block, or drag a circle, and "
               "generate again."},
    {"id": "step-over",      "title": "Step over the log",     "tier": 3, "seconds": 4.5,
     # A prop in the way. The model does not see the log, only the words and
     # the constraints: with blocks and waypoints alone it planted a foot on
     # it. A pin on each foot above the log, at the frame that foot crosses,
     # lifts it over. A branch rather than a trunk (8 cm, not 12): the model's
     # straddle, from the last plant before to the first after, is ~19 cm.
     "blocks": [("a person walks forward", 1.75),
                ("a person steps over a log", 1.25),
                ("a person walks forward", 1.5)],
     "props": [{"kind": "log", "at": (0.0, -2.05), "length": 1.6, "radius": 0.08}],
     "waypoints": [(1, (0.0, 0.0)), (60, (0.0, -2.05)), (108, (0.0, -3.8))],
     "face_along_path": True,
     "pins": [{"joint": "RightFoot", "keys": [(49, (-0.1, -2.05, 0.36))]},
              {"joint": "LeftFoot", "keys": [(61, (0.1, -2.05, 0.36))]}],
     "character": "hero",   "set": "outdoor", "travel": 3.8,
     "lesson": "The model does not see props; constraints tell it about them. A pin "
               "over the log for each foot, at the frame it crosses, lifts it over."},
    {"id": "lean-counter",   "title": "Lean on the counter",   "tier": 3, "seconds": 3.0,
     # The model cannot see the counter. Walked up to it, the character walked
     # into it until its hands reached the pins upright, and the waypoints
     # yanked it back out: 4.9 m of root path for 0.4 m of travel. Started in
     # front of the counter, it only has to lean. The hands are pinned just over
     # the front edge and then keyed every 8 frames on the top, so they arrive
     # over the edge rather than through it, and stay. The two waypoints hold the
     # standing spot.
     # Held: the hands land (pins exact on 14 of 16 keys) but the model, which
     # cannot see the counter, steps into it -- a shin 9 cm inside at frame 23
     # -- and the fingertips curl into the top. Pinning the feet stops the
     # step and then the hands cannot reach between keys. Needs a model that
     # knows about the obstacle, or a leaning key pose that holds.
     "hold": "the character steps into the counter (shin 9 cm inside)",
     "blocks": [("a person leans forward on a counter with both hands", 3.0)],
     "props": [{"kind": "counter", "at": (0.0, -0.55), "size": (1.4, 0.6), "height": 0.95}],
     "waypoints": [(1, (0.0, 0.0)), (72, (0.0, 0.0))],
     "pins": [{"joint": side, "keys": [(16, (x, -0.2, 1.08))]
                                      + [(f, (x, -0.38, 1.02)) for f in range(24, 73, 8)]}
              for side, x in (("LeftHand", 0.16), ("RightHand", -0.16))],
     "character": "hero",   "set": "studio",  "travel": 0.6,
     "lesson": "A pin holds only on the frames it is keyed, so a hold is keys all the "
               "way through: these hands are keyed every 8 frames on the counter."},
    {"id": "carry-box",      "title": "Carry the box to the table", "tier": 4, "seconds": 6.0,
     # Pick up, carry, put down: the hands are pinned to the box's sides where
     # it sits on the floor and again where it goes on the table, and the box
     # rides between the wrists in between (two Copy Location constraints,
     # keyed on and off), so it follows any take.
     # Held: pickup is exact (both wrists on the box's sides, 0.0 cm) and the
     # box follows the hands onto the table, but the model carries it low and
     # hunched, so the box passes through a thigh (11 cm), and the set-down
     # hands land 4-9 cm off with a 6.8 cm pop of the box on the last frame.
     "hold": "the carry is low and the box passes through the thigh",
     "blocks": [("a person bends down and picks up a box with both hands", 2.0),
                ("a person walks forward carrying a box", 2.5),
                ("a person puts a box down on a table", 1.5)],
     "props": [{"kind": "table", "at": (0.0, -3.2), "size": (1.0, 0.6), "height": 0.75},
               {"kind": "box", "at": (0.0, -0.6), "size": 0.36,
                "carry": {"from": 36, "to": 132, "rest_after": (0.0, -3.05, 0.93)}}],
     "waypoints": [(1, (0.0, 0.0)), (36, (0.0, -0.2)), (120, (0.0, -2.62)), (144, (0.0, -2.62))],
     "face_along_path": True,
     "pins": [{"joint": "LeftHand", "keys": [(36, (0.22, -0.6, 0.2)), (132, (0.22, -3.05, 0.95))]},
              {"joint": "RightHand", "keys": [(36, (-0.22, -0.6, 0.2)), (132, (-0.22, -3.05, 0.95))]}],
     "character": "hero",   "set": "studio",  "travel": 3.2,
     "lesson": "Pins where the hands take the box and where they leave it; the box "
               "follows the hands in between. Move the table and its pins, and "
               "generate again."},
    {"id": "walk-sit",       "title": "Walk to the chair, sit", "tier": 4, "seconds": 5.5,
     # The whole pipeline, as the Animatica skill lays it out: block out, then
     # prompt, then constrain, then generate. The two key poses are made with
     # Generate Pose in a signed-in Blender (tools/finish_example.py) —
     # standing where it starts, seated on the chair and turned to face back
     # the way it came.
     #
     # Key poses alone were not enough. With only the two, the model turned
     # early and backed 1.3–1.6 m to the chair, prompt worded either way. A
     # waypoint in front of the chair at frame 60, facing along the path,
     # makes it walk there face-first: 2.05 m forward, the turn at frame 64,
     # one step back onto the seat.
     # Split into walk / turn / sit, the turn block was ignored: the walk's
     # momentum carried the character on through the chair to y=-3.16 and it
     # spun onto the seat at the end. The third waypoint sets its own facing,
     # turned round, in front of the chair at frame 84: walk to 2.02 m by 60,
     # turn from facing out to facing back by 84, step back and sit.
     "blocks": [("a person walks forward", 2.5),
                ("a person turns around", 1.0),
                ("a person sits down on the chair", 2.0)],
     "waypoints": [(1, (0.0, 0.0)), (60, (0.0, -2.05)), (84, (0.0, -2.3), "back")],
     "face_along_path": True,
     "props": [{"kind": "chair", "at": (0.0, -2.6), "facing": "back"}],
     "key_poses": [
         {"frame": 1, "at": (0.0, 0.0), "facing": "forward",
          "prompt": "a person stands in a neutral pose with arms relaxed at sides"},
         {"frame": 132, "at": (0.0, -2.52), "facing": "back",
          "prompt": "a person sits on a chair with hands resting on their thighs",
          # Generate Pose put both hands in the lap, fingers in the groin.
          # The Autoposer moves them onto the thighs, halfway to the knee.
          "touch_up": [
              {"control": "L_arm_IK_CTRL", "on_thigh": "Left", "t": 0.5, "lift": 0.10},
              {"control": "R_arm_IK_CTRL", "on_thigh": "Right", "t": 0.5, "lift": 0.10},
          ]},
     ],
     "character": "hero",   "set": "studio",  "travel": 2.6,
     "lesson": "Everything at once: a block per action, key poses for how it starts "
               "and ends, waypoints for where it walks — the last one set to face "
               "back. The seated hands were fixed with the Autoposer; try it on "
               "any key pose."},
]

def blocks(ex):
    """An example's prompt blocks as ``[(prompt, seconds)]``, one per action.

    One action to a block: "crouch, creep, then stand" in a single box asks
    the model to find three beats and time them itself, and teaches that one
    box holds a sequence. Split, each beat is its own box, as long as that beat
    takes, and the timeline says the order.
    """
    plan = ex.get("blocks") or [(ex["prompt"], ex["seconds"])]
    total = sum(sec for _p, sec in plan)
    if abs(total - ex["seconds"]) > 1e-6:
        raise ValueError(f"{ex['id']}: blocks add up to {total:g}s, not {ex['seconds']:g}s")
    return plan


def summary(ex):
    """One line for the menu's tooltip: the blocks in order."""
    return " → ".join(p for p, _sec in blocks(ex))


# Which way a key pose or a prop faces. The characters face -Y at rest.
FACING = {"forward": 0.0, "back": math.pi}


# ---------------------------------------------------------------------------
# One scene
# ---------------------------------------------------------------------------

def _clear_startup():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)


def _material(name, rgb, roughness=0.8):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    mat.diffuse_color = (*rgb, 1.0)          # what Solid shading shows
    return mat


def _build_set(scene, look):
    spec = SETS[look]

    bpy.ops.mesh.primitive_plane_add(size=60.0, location=(0, 0, 0))
    floor = bpy.context.object
    floor.name = "Floor"
    floor.data.materials.append(_material("Floor", spec["floor"]))

    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = (*spec["world"], 1.0)
    bg.inputs["Strength"].default_value = 1.0
    scene.world = world

    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = spec["sun"]
    sun = bpy.data.objects.new("Sun", sun_data)
    sun.rotation_euler = Euler((math.radians(50), 0, math.radians(35)))
    # A sun lights the same from anywhere; at the origin it drew its direction
    # line up out of the character's feet, across every shot. Up and behind
    # the view instead.
    sun.location = (-6.0, 6.0, 10.0)
    scene.collection.objects.link(sun)

    if spec["key"]:
        key_data = bpy.data.lights.new("Key", type="AREA")
        key_data.energy = spec["key"]
        key_data.size = 3.0
        key = bpy.data.objects.new("Key", key_data)
        key.location = (3.5, -4.0, 4.0)
        key.rotation_euler = Euler((math.radians(55), 0, math.radians(40)))
        scene.collection.objects.link(key)

    # The lamps light renders, not the viewport — Material Preview uses its own
    # lighting — but their gizmos drew black lines through the shot. Hidden in
    # the viewport, like the camera; a render still uses them.
    for obj in scene.collection.objects:
        if obj.type == "LIGHT":
            obj.hide_set(True)


def _build_chair(at, facing):
    """A plain chair: seat, four legs, a back. Its seat faces *facing*.

    Built at the origin facing -Y — the way the characters face — then turned
    and moved, so "back" puts the seat facing +Y, toward where a character
    walking out from the origin comes from.
    """
    wood = _material("Chair", (0.42, 0.27, 0.16), roughness=0.6)
    seat_h, half, leg, back_h = 0.46, 0.22, 0.04, 0.48
    parts = [((0, 0, seat_h - 0.025), (half * 2, half * 2, 0.05))]
    for sx in (-1, 1):
        for sy in (-1, 1):
            parts.append(((sx * (half - leg), sy * (half - leg), (seat_h - 0.05) / 2),
                          (leg, leg, seat_h - 0.05)))
    # The back rises from the rear edge: +Y, behind a sitter facing -Y.
    parts.append(((0, half - leg / 2, seat_h + back_h / 2), (half * 2, leg, back_h)))

    # One mesh, not six parented cubes: parenting drew a dashed line from
    # every part to the chair's origin, right under the seat.
    bm = bmesh.new()
    for loc, dims in parts:
        bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.LocRotScale(Vector(loc), None, Vector(dims)))
    mesh = bpy.data.meshes.new("Chair")
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(wood)
    chair = bpy.data.objects.new("Chair", mesh)
    bpy.context.scene.collection.objects.link(chair)
    chair.rotation_euler = (0.0, 0.0, FACING[facing])
    chair.location = (at[0], at[1], 0.0)
    return chair


def _box_mesh(name, parts, rgb, roughness=0.7):
    """One mesh object from ``[(centre, size)]`` boxes — no parenting lines."""
    bm = bmesh.new()
    for loc, dims in parts:
        bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.LocRotScale(Vector(loc), None, Vector(dims)))
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    mesh.materials.append(_material(name, rgb, roughness))
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _build_button(at, height):
    """A pillar with a red button on top — something for a hand to press."""
    x, y = at
    _box_mesh("Pillar", [((x, y, height / 2), (0.24, 0.24, height))], (0.55, 0.56, 0.6))
    bpy.ops.mesh.primitive_cylinder_add(radius=0.05, depth=0.03, location=(x, y, height + 0.015))
    button = bpy.context.object
    button.name = "Button"
    button.data.materials.append(_material("Button", (0.8, 0.08, 0.06), roughness=0.4))
    return button


def _build_ball(at, kick_frame, frames):
    """A football at *at*, still until *kick_frame*, then sent off the way it was kicked.

    A ball that sat there after the foot went through it read as a miss. It
    rolls, not flies: straight on, slowing, from the frame the pin says the
    foot arrives.
    """
    r = 0.11
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=(at[0], at[1], r), segments=24, ring_count=12)
    ball = bpy.context.object
    ball.name = "Ball"
    ball.data.materials.append(_material("Ball", (0.92, 0.92, 0.9), roughness=0.5))
    ball.keyframe_insert("location", frame=1)
    ball.keyframe_insert("location", frame=kick_frame)
    ball.location = (at[0], at[1] - 2.6, r)
    ball.keyframe_insert("location", frame=frames)
    # ease out, not in: the kick is the fast part
    for fc in (ball.animation_data.action.layers[0].strips[0]
               .channelbag(ball.animation_data.action_slot).fcurves):
        for k in fc.keyframe_points:
            k.interpolation = "LINEAR" if k.co.x < kick_frame else "QUAD"
            k.easing = "EASE_OUT"
    return ball


def _build_crate(at, size):
    """A crate to go round: the reason a path is curved."""
    return _box_mesh("Crate", [((at[0], at[1], size / 2), (size, size, size))], (0.5, 0.36, 0.2))


def _build_log(at, length, radius):
    """A log lying across the path, along X — something to step over."""
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=length, vertices=24,
                                        location=(at[0], at[1], radius),
                                        rotation=(0.0, math.pi / 2, 0.0))
    log = bpy.context.object
    log.name = "Log"
    log.data.materials.append(_material("Log", (0.33, 0.22, 0.13), roughness=0.9))
    return log


def _build_table(at, size, height, name="Table"):
    """A slab on four legs. *size* is (x, y) of the top."""
    x, y = at
    sx, sy = size
    t, leg = 0.04, 0.05
    parts = [((x, y, height - t / 2), (sx, sy, t))]
    for dx in (-1, 1):
        for dy in (-1, 1):
            parts.append(((x + dx * (sx / 2 - leg), y + dy * (sy / 2 - leg), (height - t) / 2),
                          (leg, leg, height - t)))
    return _box_mesh(name, parts, (0.45, 0.33, 0.24))


def _build_counter(at, size, height):
    """A solid counter to lean on."""
    return _box_mesh("Counter", [((at[0], at[1], height / 2), (size[0], size[1], height))],
                     (0.62, 0.6, 0.56))


def _build_box(at, size):
    """A cardboard box sitting on the floor."""
    return _box_mesh("Box", [((0.0, 0.0, 0.0), (size, size, size))], (0.72, 0.55, 0.34)), size


def _carry(arm, box, rest, carry):
    """The box rides between the hands from ``from`` to ``to``, then rests at ``rest_after``.

    Two Copy Location constraints on the hands' bones — the second at half
    influence, so together they put the box at the midpoint between the wrists
    — keyed on at the pickup frame and off at the set-down frame. They follow
    whatever motion is generated, so the box stays in the hands through any
    take; the pins put the wrists on the box's sides at both ends, which is
    what makes the handover seamless.
    """
    from animatica_blender import constraints_ui

    box.location = rest
    box.keyframe_insert("location", frame=1)
    box.location = carry["rest_after"]
    box.keyframe_insert("location", frame=carry["to"])
    for side, weight in (("LeftHand", 1.0), ("RightHand", 0.5)):
        bone = constraints_ui.resolve_effector_bone(arm, side)
        con = box.constraints.new("COPY_LOCATION")
        con.name = f"Carried by {side}"
        con.target, con.subtarget = arm, bone.name
        for frame, value in ((1, 0.0), (carry["from"], weight), (carry["to"], 0.0)):
            con.influence = value
            box.keyframe_insert(f'constraints["{con.name}"].influence', frame=frame)
    action = box.animation_data.action
    for layer in action.layers:
        for strip in layer.strips:
            for fc in strip.channelbag(box.animation_data.action_slot).fcurves:
                for k in fc.keyframe_points:
                    k.interpolation = "CONSTANT"


def _add_pins(arm, pins):
    """Effector pins, made the way the Pin button makes them.

    Through the addon's own operator, so a pin in an example is exactly what
    an artist gets; then moved to where the hand or foot must be and keyed at
    the frame it must be there.
    """
    from animatica_blender import constraints_ui

    scene = bpy.context.scene
    for pin in pins:
        bone = constraints_ui.resolve_effector_bone(arm, pin["joint"])
        if bone is None:
            raise RuntimeError(f"{arm.name} has no bone for {pin['joint']}")
        before = set(bpy.data.objects)
        bpy.ops.animatica.add_effector_target(mode="BONE", bone=bone.name)
        empty = next(o for o in bpy.data.objects if o not in before)
        empty.animation_data_clear()
        for frame, xyz in pin["keys"]:
            empty.location = xyz
            empty.keyframe_insert("location", frame=frame)


def _import_character(which, assets_dir):
    from animatica_blender import canonical_skeleton as cs

    spec = CHARACTERS[which]
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=str(assets_dir / spec["fbx"]), **cs._FBX_IMPORT)
    new = [o for o in bpy.data.objects if o not in before]
    arm = next(o for o in new if o.type == "ARMATURE")
    mesh = next((o for o in new if o.type == "MESH"), None)
    for obj in new:
        if obj not in (arm, mesh):
            bpy.data.objects.remove(obj, do_unlink=True)
    # The same normalisation the addon applies to its own import, so a rig
    # from an example behaves exactly like one brought in by hand.
    cs._normalise_transform(arm, mesh)
    cs.clear_pose(arm)
    arm.name = spec["name"]
    return arm


def _character_height(arm):
    """How tall the character stands, in metres, from its meshes' world bounds.

    Not ``arm.dimensions``: an FBX armature keeps its centimetre-space bone
    extents under a 0.01 scale, and the Hero read as 29 m tall — the viewport
    opened 88 m away with the character a speck.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    zs = []
    for obj in bpy.data.objects:
        if obj.type != "MESH" or obj.find_armature() is not arm:
            continue
        ev = obj.evaluated_get(depsgraph)
        zs += [(ev.matrix_world @ Vector(c)).z for c in ev.bound_box]
    return (max(zs) - min(zs)) if zs else 1.8


def _frame_view(scene, arm, travel):
    """Point the camera, and the 3D viewport, at the whole of the motion.

    The viewport matters more than the camera: it is what the artist sees the
    moment the file opens, and a character half off-screen reads as broken.
    """
    height = min(max(_character_height(arm), 1.2), 2.5)
    focus = Vector((0.0, -travel * 0.5, height * 0.55))
    distance = max(4.5, height * 3.0, travel * 1.1)
    direction = Vector((0.9, -1.0, 0.45)).normalized()

    cam_data = bpy.data.cameras.new("Camera")
    cam_data.lens = 40
    cam = bpy.data.objects.new("Camera", cam_data)
    cam.location = focus + direction * distance
    cam.rotation_euler = (focus - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(cam)
    scene.camera = cam
    # Hidden in the viewport, not deleted: it frames the render, but seen from
    # the viewport — which starts almost where it stands — its body and frame
    # are drawn right across the shot. Numpad 0 still looks through it.
    cam.hide_set(True)

    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            space = area.spaces[0]
            space.shading.type = "MATERIAL"
            space.show_region_ui = True
            r3d = space.region_3d
            r3d.view_perspective = "PERSP"
            r3d.view_location = focus
            r3d.view_distance = distance
            r3d.view_rotation = (-direction).to_track_quat("-Z", "Y")
            for region in area.regions:
                if region.type == "UI":
                    try:
                        region.active_panel_category = "Animatica"
                    except (AttributeError, TypeError):
                        pass


def build_one(ex, assets_dir, out_dir):
    bpy.ops.wm.read_factory_settings(use_empty=False)
    bpy.ops.preferences.addon_enable(module="animatica_blender")
    scene = bpy.context.scene
    _clear_startup()

    frames = int(round(ex["seconds"] * FPS))
    scene.render.fps = FPS
    scene.frame_start, scene.frame_end = 1, frames
    scene.frame_current = 1
    scene.name = ex["title"]

    _build_set(scene, ex["set"])
    for prop in ex.get("props", ()):
        if prop["kind"] == "chair":
            _build_chair(prop["at"], prop["facing"])
        elif prop["kind"] == "button":
            _build_button(prop["at"], prop["height"])
        elif prop["kind"] == "ball":
            _build_ball(prop["at"], prop["kick_frame"], frames)
        elif prop["kind"] == "crate":
            _build_crate(prop["at"], prop["size"])
        elif prop["kind"] == "log":
            _build_log(prop["at"], prop["length"], prop["radius"])
        elif prop["kind"] == "table":
            _build_table(prop["at"], prop["size"], prop["height"])
        elif prop["kind"] == "counter":
            _build_counter(prop["at"], prop["size"], prop["height"])
    arm = _import_character(ex["character"], assets_dir)
    _frame_view(scene, arm, ex["travel"])

    settings = scene.animatica
    settings.target_armature = arm            # seeds one empty block across the scene
    plan = blocks(ex)
    cursor = 1
    for i, (prompt, seconds) in enumerate(plan):
        block = settings.prompt_blocks[0] if i == 0 else settings.prompt_blocks.add()
        length = int(round(seconds * FPS))
        block.prompt = prompt
        block.frame_start, block.frame_end = cursor, min(cursor + length - 1, frames)
        block.enabled = True
        cursor += length

    if ex.get("waypoints"):
        from animatica_blender import waypoints
        for frame, xy, *facing in ex["waypoints"]:
            waypoints.create_marker(scene, frame, xy, owner=arm,
                                    facing=FACING[facing[0]] if facing else None)
        settings.waypoint_heading = bool(ex.get("face_along_path"))
    if ex.get("pins"):
        _add_pins(arm, ex["pins"])
    for prop in ex.get("props", ()):
        if prop["kind"] == "box":
            box, size = _build_box(prop["at"], prop["size"])
            _carry(arm, box, (prop["at"][0], prop["at"][1], size / 2), prop["carry"])

    from animatica_blender import properties
    properties.save_blocks_to_armature(arm, settings)

    # Attribution travels inside the file, where a CC-BY licence says it must.
    credit = CHARACTERS[ex["character"]]["credit"]
    note = bpy.data.texts.new("About this example")
    note.write(f"{ex['title']} — {ex['lesson']}\n\n"
               + "".join(f"Block: {p} ({sec:g}s)\n" for p, sec in blocks(ex))
               + f"Length: {ex['seconds']:g}s ({frames} frames at {FPS} fps)\n\n"
               "Press Generate Motion in the Animatica sidebar.\n")
    if credit:
        note.write(f"\nCharacter: {credit}\n")
    scene["animatica_example"] = ex["id"]
    if credit:
        scene["animatica_example_credit"] = credit

    bpy.ops.file.pack_all()
    if ex.get("key_poses"):
        # Key poses come from the server, which needs a signed-in Blender:
        # this is a base for tools/finish_example.py, not a finished example.
        scene["animatica_key_poses_pending"] = json.dumps(ex["key_poses"])
        path = out_dir / f"{ex['id']}.base.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True, copy=True)
        return path
    path = out_dir / f"{ex['id']}.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True, copy=True)
    return path


# ---------------------------------------------------------------------------
# All of them, and the manifest
# ---------------------------------------------------------------------------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--addon", required=True, help="the addon zip to build against")
    ap.add_argument("--assets", required=True, help="directory holding the character FBXs")
    ap.add_argument("--out", required=True, help="where the .blend files and examples.json go")
    ap.add_argument("--base-url", default="", help="where the files will be downloaded from")
    ap.add_argument("--only", default="", help="comma-separated example ids to build")
    args = ap.parse_args(argv)

    assets_dir = pathlib.Path(args.assets)
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    bpy.ops.preferences.addon_install(filepath=str(pathlib.Path(args.addon).resolve()), overwrite=True)

    wanted = {s for s in args.only.split(",") if s}
    manifest = {"version": 1, "fps": FPS, "examples": []}
    existing = out_dir / "examples.json"
    if wanted and existing.is_file():
        # Rebuilding a few must not drop the rest from the manifest; publish()
        # replaces each rebuilt one's entry by id.
        manifest = json.loads(existing.read_text())
    for ex in EXAMPLES:
        if wanted and ex["id"] not in wanted:
            continue
        built = build_one(ex, assets_dir, out_dir)
        if ex.get("hold"):
            manifest["examples"] = [e for e in manifest["examples"] if e["id"] != ex["id"]]
            print(f"held  {built.name:24} not in the manifest: {ex['hold']}")
            continue
        if ex.get("key_poses"):
            print(f"base  {built.name:24} needs its key poses: run "
                  f"tools/finish_example.py in a signed-in Blender")
            continue
        publish(ex, built, manifest, args.base_url)

    write_manifest(out_dir, manifest)


def publish(ex, built, manifest, base_url=""):
    """Give a built file its content-addressed name and its manifest entry."""
    # Content-addressed name: a published file is never overwritten, so a
    # manifest — fresh, or a CDN edge's copy from before the update — always
    # names a file that matches its own checksum. Overwriting in place meant
    # that for minutes after a republish the old list and the new file met,
    # and every download was refused.
    digest = _sha256(built)
    path = built.with_name(f"{ex['id']}.{digest[:8]}.blend")
    built.replace(path)
    for stale in built.parent.glob(f"{ex['id']}.*.blend"):
        if stale != path and not stale.name.endswith(".base.blend"):
            stale.unlink()
    entry = {k: ex[k] for k in ("id", "title", "tier", "seconds", "lesson")}
    entry["prompt"] = summary(ex)
    entry.update({
        "character": CHARACTERS[ex["character"]]["name"],
        "credit": CHARACTERS[ex["character"]]["credit"],
        "file": path.name,
        "url": (base_url.rstrip("/") + "/" + path.name) if base_url else "",
        "size": path.stat().st_size,
        "sha256": digest,
    })
    manifest["examples"] = [e for e in manifest["examples"] if e["id"] != ex["id"]]
    manifest["examples"].append(entry)
    print(f"built {path.name:24} {entry['size'] / 1048576:6.1f} MB  {ex['title']}")
    return path


def write_manifest(out_dir, manifest):
    order = {ex["id"]: i for i, ex in enumerate(EXAMPLES)}
    manifest["examples"].sort(key=lambda e: order.get(e["id"], len(order)))
    (out_dir / "examples.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out_dir / 'examples.json'} ({len(manifest['examples'])} examples)")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    main(argv)
