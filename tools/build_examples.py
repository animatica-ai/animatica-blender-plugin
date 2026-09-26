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

import bpy
from mathutils import Euler, Vector

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
     "prompt": "a person throws a jab followed by a cross",
     "character": "cesium", "set": "ring",    "travel": 0.0,
     "lesson": "Two actions in a row. Order matters, and so does timing."},
    {"id": "run-stop",       "title": "Run, then stop",        "tier": 2, "seconds": 5.0,
     "prompt": "a person runs forward then slows to a stop",
     "character": "hero",   "set": "runway",  "travel": 10.0,
     "lesson": "A change of speed inside one clip."},
    {"id": "walk-turn-back", "title": "Walk, turn, walk back", "tier": 3, "seconds": 6.0,
     # Waypoints, because words were not enough: tested five ways, one sentence
     # and two blocks, the model brought the character back in some samples
     # and walked it straight out in others. Three waypoints — here, there by
     # frame 72, back by 144 — land within 4 cm of each at its frame, and the
     # turn between is the model's own. On the Hero rather than Cesium Man for
     # now: any root constraint on a character much smaller than the model's
     # body makes the server stretch the root after the retarget, and the feet
     # slide. That is a server fix; this example should not have to wait on it.
     "prompt": "a person walks forward, turns around, and walks back",
     "waypoints": [(1, (0.0, 0.0)), (72, (0.0, -2.5)), (144, (0.0, -0.2))],
     "character": "hero",   "set": "outdoor", "travel": 3.0,
     "lesson": "Words set the gait; each circle on the floor says where to be, and "
               "when. Drag one, or change its frame, and generate again."},
    {"id": "crouch-creep",   "title": "Crouch, creep, stand",  "tier": 3, "seconds": 8.0,
     "prompt": "a person crouches, creeps forward slowly, then stands up",
     "character": "hero",   "set": "dusk",    "travel": 2.5,
     "lesson": "Three beats in sequence, each handing over to the next."},
]


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
    scene.collection.objects.link(sun)

    if spec["key"]:
        key_data = bpy.data.lights.new("Key", type="AREA")
        key_data.energy = spec["key"]
        key_data.size = 3.0
        key = bpy.data.objects.new("Key", key_data)
        key.location = (3.5, -4.0, 4.0)
        key.rotation_euler = Euler((math.radians(55), 0, math.radians(40)))
        scene.collection.objects.link(key)


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
    arm = _import_character(ex["character"], assets_dir)
    _frame_view(scene, arm, ex["travel"])

    settings = scene.animatica
    settings.target_armature = arm            # seeds one empty block across the scene
    plan = ex.get("blocks") or [(ex["prompt"], ex["seconds"])]
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
        for frame, xy in ex["waypoints"]:
            waypoints.create_marker(scene, frame, xy, owner=arm)

    from animatica_blender import properties
    properties.save_blocks_to_armature(arm, settings)

    # Attribution travels inside the file, where a CC-BY licence says it must.
    credit = CHARACTERS[ex["character"]]["credit"]
    note = bpy.data.texts.new("About this example")
    note.write(f"{ex['title']} — {ex['lesson']}\n\n"
               f"Prompt: {ex['prompt']}\nLength: {ex['seconds']:g}s ({frames} frames at {FPS} fps)\n\n"
               "Press Generate Motion in the Animatica sidebar.\n")
    if credit:
        note.write(f"\nCharacter: {credit}\n")
    scene["animatica_example"] = ex["id"]
    if credit:
        scene["animatica_example_credit"] = credit

    bpy.ops.file.pack_all()
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
        # Rebuilding a few must not drop the rest from the manifest.
        manifest = json.loads(existing.read_text())
        manifest["examples"] = [e for e in manifest["examples"] if e["id"] not in wanted]
    for ex in EXAMPLES:
        if wanted and ex["id"] not in wanted:
            continue
        built = build_one(ex, assets_dir, out_dir)
        # Content-addressed name: a published file is never overwritten, so a
        # manifest — fresh, or a CDN edge's copy from before the update — always
        # names a file that matches its own checksum. Overwriting in place meant
        # that for minutes after a republish the old list and the new file met,
        # and every download was refused.
        digest = _sha256(built)
        path = built.with_name(f"{ex['id']}.{digest[:8]}.blend")
        built.replace(path)
        for stale in out_dir.glob(f"{ex['id']}.*.blend"):
            if stale != path:
                stale.unlink()
        entry = {k: ex[k] for k in ("id", "title", "tier", "seconds", "prompt", "lesson")}
        entry.update({
            "character": CHARACTERS[ex["character"]]["name"],
            "credit": CHARACTERS[ex["character"]]["credit"],
            "file": path.name,
            "url": (args.base_url.rstrip("/") + "/" + path.name) if args.base_url else "",
            "size": path.stat().st_size,
            "sha256": digest,
        })
        manifest["examples"].append(entry)
        print(f"built {path.name:24} {entry['size'] / 1048576:6.1f} MB  {ex['title']}")

    order = {ex["id"]: i for i, ex in enumerate(EXAMPLES)}
    manifest["examples"].sort(key=lambda e: order.get(e["id"], len(order)))
    (out_dir / "examples.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out_dir / 'examples.json'} ({len(manifest['examples'])} examples)")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    main(argv)
