# SPDX-License-Identifier: GPL-3.0-or-later
"""Key poses — showing the plan the model will be handed.

Generation is directed, not authored: the artist keys a few poses, draws a
path, writes prompts into blocks on the timeline, and the server fills in the
motion. Every pose they key becomes one full-body ``pose_keyframe``
constraint in the request — so those frames *are* the plan, and until now
they were invisible. Scrubbing off a key left no trace of it, nothing said
which prompt block a pose belonged to, and a pose sitting outside the
generation window was dropped from the request without a word.

This module draws that plan:

  * a **ghost of every pose you keyed**, tinted with the colour of the prompt
    block it falls inside — the same colour as that block's strip on the
    timeline, so the viewport and the timeline agree about which pose belongs
    to which instruction;
  * the **frame number** beside each ghost, because "where did I put that
    key" is the question this answers;
  * a **trail** through the poses in frame order, which is the route the
    character is being asked to travel;
  * poses that fall **outside the generation window** greyed out and flagged,
    since those are not sent at all — the one failure mode the artist
    otherwise discovers only in the result.

Two caches, refreshed on different terms:

``_plan``   what the request will contain — frames, their block, whether each
            is inside the window. Cheap; recomputed whenever the action, the
            blocks or the scene range change, and kept even while the ghosts
            are switched off so the panel can still report the plan.
``_ghosts`` the geometry. Expensive: capturing a pose means moving the
            playhead and evaluating the depsgraph, so it is baked once per
            edit on a debounced timer and never from a draw callback.
"""

from __future__ import annotations

import math
import time
import zlib

import blf
import bpy
from bpy.app.handlers import persistent
import gpu
import numpy as np
from bpy_extras import view3d_utils
from mathutils import Vector
from gpu_extras.batch import batch_for_shader


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Ceiling on ghosts baked in one pass. Each costs a frame_set and a full
# depsgraph evaluation; past this we keep the earliest and say so in the panel.
MAX_GHOSTS = 64

# Seconds of quiet after the last edit before re-baking. Dragging a bone emits
# depsgraph updates continuously.
REBUILD_DEBOUNCE = 0.35

# ...but no burst may hold the bake off forever. Waiting for quiet assumes the
# noise stops; during playback the depsgraph reports the action updated on
# every frame, which re-armed the wait sixty times a second and meant the
# ghosts never refreshed at all while the animation ran. After this long since
# the first request, the bake happens whatever is still arriving.
REBUILD_MAX_WAIT = 1.2

# Ghost opacity by how many key poses away it is from the playhead. A blockout
# with eight keys drew eight bodies at nearly one weight and read as a crowd;
# the pose you are working between has to come forward and the rest has to
# fall back to context. Beyond the third it is a silhouette saying "there is
# more plan over there", which is all it needs to say.
GHOST_ALPHA_BY_RANK  = (0.42, 0.22, 0.12)
GHOST_ALPHA_FAR      = 0.07
GHOST_ALPHA_ADJACENT = 0.42   # the poses either side of the playhead
GHOST_ALPHA_DROPPED  = 0.13   # a pose the request will not carry
GHOST_ALPHA_EDITING  = 0.60   # the pose currently open for editing

# Bone sticks cover a fraction of the pixels a body does, so the alpha that
# reads as a translucent character leaves a skeleton nearly invisible.
BONE_ALPHA_BOOST = 2.2
BONE_LINE_WIDTH  = 2.0

# A pose inside the window but outside every prompt block: still sent, but it
# belongs to no instruction, so it gets a neutral tint rather than a block's.
UNBLOCKED_COLOR = (0.62, 0.68, 0.78)

# A pose the next take leaves out (outside every block's window).
DROPPED_COLOR   = (0.50, 0.50, 0.52)

# The root trajectory: the path the take travels along without the sway of its
# steps (inplace.py), drawn on the floor. Amber: none of the prompt-block
# colours, and not the white of the playhead, so it never reads as the motion.
ROOT_PATH_COLOR = (1.0, 0.72, 0.18, 0.95)
ROOT_PATH_WIDTH = 3.0
ROOT_PATH_LIFT = 0.004            # m above the floor, so it does not z-fight it
ROOT_PATH_TICK_EVERY = 6          # frames between the small ticks along it
ROOT_PATH_TICK_RADIUS = 2.2
ROOT_PATH_CURRENT_RADIUS = 5.0
ROOT_PATH_CURRENT_COLOR = (1.0, 1.0, 1.0, 0.95)
#: the root trajectory is coloured by its speed: slow, middling, fast (of the
#: take's fastest, or of ROOT_PATH_SPEED_FLOOR m/s if it never goes that fast)
ROOT_PATH_SPEED_RAMP = ((0.20, 0.82, 0.48), (1.00, 0.72, 0.18), (0.96, 0.22, 0.16))
ROOT_PATH_SPEED_FLOOR = 0.5

LABEL_SIZE         = 11
LABEL_COLOR        = (0.92, 0.93, 0.96, 0.95)
LABEL_DROPPED      = (1.00, 0.55, 0.42, 0.95)
LABEL_OFFSET_PX    = 6

# How long "keyed" stays beside a pose that was just committed. Long enough to
# be read after letting go of the mouse, short enough not to become furniture.
FLASH_SECONDS = 2.0
FLASH_COLOR   = (0.55, 1.0, 0.55, 1.0)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# What the next Generate will pin. Cheap to rebuild, so it survives the ghosts
# being switched off — the panel reports from it either way.
#   frames     sorted authored frames
#   entries    frame -> {"block": int | None, "in_range": bool}
#   range      the generation window (inclusive)
#   signature  what the plan was computed from
_plan: dict = {
    "frames": [],
    "entries": {},
    "range": (0, 0),
    "signature": None,
    "dirty": True,
}

# Baked geometry.
#   ghosts     frame -> {"tris": batch | None, "lines": batch | None}
#   roots      frame -> (root world position, label anchor world position)
#   signature  (armature name, action name) the bake came from
#   kind       "MESH" | "BONES" — what was captured
_ghosts: dict = {
    "frames": [],
    "ghosts": {},
    "roots": {},
    "signature": None,
    "clamped": False,
    "kind": "BONES",
    "arm": "",
    # Set whenever something asks for a rebake. The signature alone cannot
    # answer "is this stale": editing a pose changes neither the rig's name
    # nor the action's, so a content change is invisible to it.
    "dirty": True,
}

# The root trajectory, fitted in the same bake pass (it samples the take too):
#   spans      [{"frames": [a, b], "path": [(x, y, z), ...], "label": str}]
#   colors     per point, by speed; "vmax" the speed the ramp tops out at
_root_path: dict = {
    "spans": [],
    "signature": None,
    "dirty": True,
    "arm": "",
    "reuse": False,        # only the path changed (its curve edited): no need to sample the take
}

# Set while a bake owns the playhead, so our own frame_set calls don't look
# like user edits to the depsgraph handler.
_baking = False

#: The frame most recently keyed, and when. Both ways of editing a pose — a
#: control dragged at the playhead, a motion curve dragged at any frame — say
#: so the same way, because they are the same act: this frame is now one of
#: yours.
_flash: dict = {"frame": -1, "at": 0.0}

# Time of the most recent rebuild request; the timer waits for quiet.
_rebuild_requested_at: float | None = None
#: ...and of the first request in the burst, which the ceiling is measured
#: from, so a stream of requests cannot hold the bake off indefinitely.
_rebuild_first_at: float | None = None

# Draw handles are mirrored onto driver_namespace so a module reload finds the
# ones Blender still holds instead of stacking a second pair on top.
_NS_GEOMETRY = "_animatica_key_poses_geometry_handle"
_NS_LABELS = "_animatica_key_poses_label_handle"
_geometry_handle = None
_label_handle = None


def _shader():
    return gpu.shader.from_builtin("UNIFORM_COLOR")


def _line_shader():
    """Thick-line shader.

    ``gpu.state.line_width_set`` is a no-op wider than 1px on the Metal
    backend (macOS), which is what most of this addon's users are on — the
    trail came out as a hairline. The POLYLINE shaders expand the line into
    geometry themselves and give a real width everywhere. GPU *point* size is
    ignored there too, which is why the trail's markers are screen-space
    quads rather than points.
    """
    return gpu.shader.from_builtin("POLYLINE_SMOOTH_COLOR")


def _line_uniform_shader():
    return gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")


def _viewport_size():
    viewport = gpu.state.viewport_get()
    return (float(viewport[2]), float(viewport[3]))


def _px() -> float:
    """Display pixel scale.

    GPU line widths and point sizes are in device pixels, so on a HiDPI screen
    an unscaled 2px line renders as a hairline. Blender scales its own
    overlays by this; ours has to as well or the trail disappears on exactly
    the displays most people work on.
    """
    try:
        return float(bpy.context.preferences.system.pixel_size)
    except AttributeError:
        return 1.0


def _settings(scene):
    return getattr(scene, "animatica", None)


def _target(settings):
    from . import properties

    if settings is None:
        return None
    try:
        return properties._live_armature(settings.target_armature)
    except (AttributeError, ReferenceError):
        return None


def _action(arm):
    if arm is None or arm.animation_data is None:
        return None
    return arm.animation_data.action


def _ghost_signature(arm, action, settings):
    """What the baked ghosts depend on.

    The display mode is in here so switching Mesh/Bones re-bakes itself even
    if the property callback is missed.
    """
    if arm is None or action is None:
        return None
    return (arm.name, action.as_pointer(), settings.key_pose_display)  # not its name: keeping a take renames it


def _root_path_signature(arm, action):
    """What the root trajectory depends on: the take, and whether In place has
    taken it out (then the removed path is read back, not fitted again)."""
    if arm is None or action is None:
        return None
    from . import inplace
    return (arm.name, action.name, inplace.applied_mode(action))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def _plan_signature(settings, arm, action, scene):
    """Everything the plan is derived from, cheap enough to check per draw.

    Block geometry is in here, so dragging a strip on the timeline re-tints
    the ghosts without any handler having to notice.
    """
    from . import constraints_ui

    blocks = tuple(
        (int(b.frame_start), int(b.frame_end), bool(b.enabled), bool((b.prompt or "").strip()),
         bool(getattr(b, "locked", False)))
        for b in settings.prompt_blocks
    )
    # With blocks locked, the next Generate makes the stretch under the
    # playhead (request_builder.generation_blocks), so where it is counts.
    if any(b[4] for b in blocks):
        blocks += (("playhead", int(scene.frame_current)),)
    # How many keys there are: a pose keyed with Blender's own I, or by a path
    # that forgets to invalidate, changes it, where the action's name does not
    # -- a frame keyed that way got no ghost and no count until something else
    # happened to refresh the plan.
    curves = list(constraints_ui.iter_action_fcurves(action)) if action else []
    keys = (len(curves), sum(len(fc.keyframe_points) for fc in curves))
    return (
        arm.name if arm else None,
        action.name if action else None,
        keys,
        blocks,
        int(scene.frame_start),
        int(scene.frame_end),
        int(getattr(settings, "num_transition_frames", 0) or 0),
    )


def _owning_block(frame: int, blocks) -> int | None:
    """Index of the enabled block containing ``frame``, if any.

    Blocks cannot overlap (the timeline editor enforces it), so the first
    match is the only one.
    """
    for i, b in enumerate(blocks):
        if not b.enabled:
            continue
        if int(b.frame_start) <= frame <= int(b.frame_end):
            return i
    return None


def plan(scene=None) -> dict:
    """The poses the next Generate will pin, recomputed only when stale.

    Read-only and cheap on the common path, so a draw callback or a panel may
    call it freely.
    """
    from . import constraints_ui, request_builder

    scene = scene or getattr(bpy.context, "scene", None)
    settings = _settings(scene)
    if settings is None:
        return _plan

    arm = _target(settings)
    action = _action(arm)
    signature = _plan_signature(settings, arm, action, scene)
    if not _plan["dirty"] and signature == _plan["signature"]:
        return _plan

    frames, keyed_bones = constraints_ui.authored_pose_frames(action)
    if not keyed_bones:
        frames = []

    try:
        window = request_builder.compute_frame_range(
            settings.prompt_blocks, arm, scene,
        )
    except Exception:                       # noqa: BLE001 — never break a draw
        window = (int(scene.frame_start), int(scene.frame_end))

    entries = {
        f: {
            "block": _owning_block(f, settings.prompt_blocks),
            # sample_pose_keyframes filters by exactly this window, so a pose
            # outside it never reaches the server.
            "in_range": window[0] <= f <= window[1],
        }
        for f in frames
    }

    _plan["frames"] = frames
    _plan["entries"] = entries
    _plan["range"] = window
    _plan["signature"] = signature
    _plan["dirty"] = False
    return _plan


def invalidate_plan() -> None:
    """Mark the plan stale; the next reader recomputes it."""
    _plan["dirty"] = True


def take_keys(scene) -> list:
    """The key poses the next take will hit (inside the generation window)."""
    p = plan(scene)
    return [f for f in p["frames"] if p["entries"][f]["in_range"]]


def keyed_here(scene) -> bool:
    """Whether the frame under the playhead holds one of your key poses."""
    return int(scene.frame_current) in plan(scene)["frames"]


def move_key_pose(scene, src: int, dst: int) -> bool:
    """Move the key pose on frame ``src`` to ``dst``: every key the artist set
    on that frame (any channel; not the take's GENERATED samples), with its
    handles and interpolation. A take's sample already on ``dst`` gives way.
    Refused (False) onto a frame that holds a key pose of its own."""
    from . import constraints_ui

    settings = _settings(scene)
    arm = _target(settings)
    action = _action(arm)
    src, dst = int(src), int(dst)
    if action is None or src == dst:
        return False
    authored, _ = constraints_ui.authored_pose_frames(action)
    if dst in authored or src not in authored:
        return False
    moved = 0
    for fc in constraints_ui.iter_action_fcurves(action):
        kps = fc.keyframe_points
        mine = [i for i, k in enumerate(kps) if int(round(k.co.x)) == src and k.type != 'GENERATED']
        if not mine:
            continue
        keep = []
        for i in mine:
            k = kps[i]
            keep.append({
                "value": k.co.y, "type": k.type, "interpolation": k.interpolation,
                "easing": k.easing, "hl_type": k.handle_left_type, "hr_type": k.handle_right_type,
                "hl": (k.handle_left.x - k.co.x, k.handle_left.y - k.co.y),
                "hr": (k.handle_right.x - k.co.x, k.handle_right.y - k.co.y),
            })
        gone = mine + [i for i, k in enumerate(kps) if int(round(k.co.x)) == dst and k.type == 'GENERATED']
        for i in sorted(set(gone), reverse=True):
            kps.remove(kps[i], fast=True)
        for kd in keep[:1]:                 # one key per channel per frame
            k = kps.insert(dst, kd["value"], options={'FAST'}, keyframe_type=kd["type"])
            k.interpolation, k.easing = kd["interpolation"], kd["easing"]
            k.handle_left_type, k.handle_right_type = kd["hl_type"], kd["hr_type"]
            k.handle_left = (dst + kd["hl"][0], kd["value"] + kd["hl"][1])
            k.handle_right = (dst + kd["hr"][0], kd["value"] + kd["hr"][1])
        fc.update()
        moved += 1
    if not moved:
        return False
    invalidate_plan()
    clear()                                 # the ghosts re-bake where the pose now is
    tag_redraw()
    return True


def dropped_frames(scene=None) -> list[int]:
    """Key poses the request will silently leave out."""
    p = plan(scene)
    return [f for f in p["frames"] if not p["entries"][f]["in_range"]]


def overlay_on(settings) -> bool:
    """Is any part of the plan overlay switched on?

    ``key_pose_overlay`` is the master -- one click to clear the viewport --
    and the parts under it say what the overlay is made of: the ghosts, the
    root path, and whatever the Autoposer samples in the same pass (its motion
    trail, when it is installed). Asking both questions here saves every caller
    from having to remember the master exists.
    """
    if settings is None or not settings.key_pose_overlay:
        return False
    if settings.key_pose_ghosts or getattr(settings, "key_pose_root_path", False):
        return True
    from . import posing
    return any(s.wanted(settings) for s in posing.samplers())


def ghosts_on(settings) -> bool:
    """Should the keyed poses be drawn and baked?"""
    return bool(settings is not None and settings.key_pose_overlay
                and settings.key_pose_ghosts)


def root_path_on(settings) -> bool:
    """Should the root trajectory be drawn and fitted?"""
    return bool(settings is not None and settings.key_pose_overlay
                and getattr(settings, "key_pose_root_path", False))


def timeline_ticks(scene) -> tuple[list[tuple[int, bool]], tuple[int, int]]:
    """``([(frame, in_range), …], window)`` for the timeline lane overlay.

    Whatever the viewport shows: the ghost switch is about the viewport (the
    poses, the trail, their numbers), and the timeline's marks are where the
    key poses are -- what the next take is asked to hit, and the handles to
    retime them by. Hidden with the ghosts, they went too.
    """
    if _settings(scene) is None:
        return [], (0, 0)
    p = plan(scene)
    return [(f, p["entries"][f]["in_range"]) for f in p["frames"]], p["range"]


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

def _pose_color(frame: int, entry: dict, settings) -> tuple[float, float, float]:
    """RGB for one key pose: its block's colour, or neutral, or dropped-grey.

    Reuses the timeline's own strip colour so a ghost and the strip it belongs
    to are visibly the same thing — including the muting the timeline applies
    to an unconditioned or disabled block.
    """
    from . import timeline_overlay

    if not entry["in_range"]:
        return DROPPED_COLOR
    index = entry["block"]
    if index is None:
        return UNBLOCKED_COLOR
    try:
        block = settings.prompt_blocks[index]
    except (IndexError, TypeError):
        return UNBLOCKED_COLOR
    return tuple(timeline_overlay._strip_color(block, index)[:3])


def _rank_from_playhead(frames, current: int) -> dict:
    """How many key poses each one is from the playhead, either way.

    Rank 1 is the pose immediately before or after, which is what an artist is
    working between. Distance in *keys*, not in frames: two keys forty frames
    apart are still neighbours, and a dense passage is not eight times more
    important for being dense.
    """
    ranks = {}
    for i, f in enumerate(reversed([x for x in frames if x < current]), start=1):
        ranks[f] = i
    for i, f in enumerate([x for x in frames if x > current], start=1):
        ranks[f] = i
    return ranks


def _pose_alpha(frame: int, entry: dict, ranks: dict, editing: int = -1) -> float:
    if frame == editing:
        # An open edit session has to be unmissable: clicking a ghost and
        # seeing nothing change is the failure this guards against.
        return GHOST_ALPHA_EDITING
    if not entry["in_range"]:
        return GHOST_ALPHA_DROPPED
    rank = ranks.get(frame, 1)
    if rank <= len(GHOST_ALPHA_BY_RANK):
        return GHOST_ALPHA_BY_RANK[rank - 1]
    return GHOST_ALPHA_FAR


def _editing_frame(settings) -> int:
    """Frame of the ghost being edited (the Autoposer's ghost-click edit), or -1. Never raises
    in a draw."""
    from . import posing
    return posing.editing_frame(settings.id_data) if settings is not None else -1


def _skinned_meshes(arm, context) -> list[bpy.types.Object]:
    """Visible meshes this armature deforms — by modifier or by parenting."""
    out: list[bpy.types.Object] = []
    for obj in context.view_layer.objects:
        if obj.type != 'MESH':
            continue
        try:
            if not obj.visible_get():
                continue
        except RuntimeError:
            continue
        if obj.parent is arm or any(
            m.type == 'ARMATURE' and m.object is arm for m in obj.modifiers
        ):
            out.append(obj)
    return out


def _ghost_bones(arm) -> list[str]:
    """Bones whose sticks stand in for the body.

    On a control rig the deform bones are the body — the controls are handles
    floating around it, and drawing them buries the pose in noise. It is also
    the skeleton the request serializes, so the stick figure is what the
    server will be given.
    """
    from . import request_builder

    try:
        if request_builder.is_control_rig(arm):
            deform = request_builder.emitted_deform_bones(arm)
            if deform:
                return [pb.name for pb in arm.pose.bones if pb.name in deform]
    except Exception:                       # noqa: BLE001 — fall back to all
        pass
    return [pb.name for pb in arm.pose.bones if not pb.bone.hide]


# The end effectors, the root and the head: the joints an editing tool takes a
# body by (Marionette's, when it is installed).
_END_JOINTS = ("Hips", "Head", "LeftHand", "RightHand", "LeftFoot", "RightFoot")


def end_bones(arm) -> list[str]:
    """The bones that play the end joints on this rig, resolved the way effector
    pins are (namespaced, Mixamo and differently-spelled rigs alike); on a
    control rig the deform bone. Walks the constraint stack: never from a draw."""
    from . import constraints_ui, request_builder

    if arm is None or arm.type != 'ARMATURE':
        return []
    allowed = None
    try:
        if request_builder.is_control_rig(arm):
            allowed = request_builder.emitted_deform_bones(arm) or None
    except Exception:                       # noqa: BLE001 -- never break a bake
        allowed = None
    names: list[str] = []
    for joint in _END_JOINTS:
        pb = constraints_ui.resolve_effector_bone(arm, joint, allowed)
        if pb is not None and pb.name not in names:
            names.append(pb.name)
    if not any(n.rsplit(":", 1)[-1] == "Hips" for n in names):
        root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
        if root is not None and root.name not in names:
            names.insert(0, root.name)
    return names


def _motion_extent(action) -> tuple[int, int] | None:
    """First and last keyed frame on ``action`` — where there is motion at all.

    Counts generated samples too: after a generation they *are* the motion,
    and the trail's whole job is to show what the model produced against the
    poses that asked for it.
    """
    from . import constraints_ui

    lo = hi = None
    for fc in constraints_ui.iter_action_fcurves(action):
        for kp in fc.keyframe_points:
            f = int(round(kp.co.x))
            lo = f if lo is None or f < lo else lo
            hi = f if hi is None or f > hi else hi
    if lo is None:
        return None
    return lo, hi


def capture_joints(arm, bone_names, depsgraph) -> dict[str, tuple]:
    """World position of each of ``bone_names`` at the current frame."""
    eval_arm = arm.evaluated_get(depsgraph)
    mw = eval_arm.matrix_world
    out: dict[str, tuple] = {}
    for name in bone_names:
        pb = eval_arm.pose.bones.get(name)
        if pb is not None:
            out[name] = (mw @ pb.head)[:]
    return out


def _capture_meshes(objects, depsgraph):
    """World-space triangles of every evaluated mesh, merged into one batch."""
    verts_all: list[np.ndarray] = []
    tris_all: list[np.ndarray] = []
    offset = 0

    for obj in objects:
        eval_obj = obj.evaluated_get(depsgraph)
        try:
            mesh = eval_obj.to_mesh()
        except RuntimeError:
            continue
        if mesh is None:
            continue
        try:
            # One matrix multiply over the whole mesh beats a per-vertex
            # transform in Python by orders of magnitude.
            mesh.transform(eval_obj.matrix_world)
            if hasattr(mesh, "calc_loop_triangles"):
                mesh.calc_loop_triangles()
            n_verts = len(mesh.vertices)
            n_tris = len(mesh.loop_triangles)
            if n_verts == 0 or n_tris == 0:
                continue
            verts = np.empty(n_verts * 3, 'f')
            tris = np.empty(n_tris * 3, 'i')
            mesh.vertices.foreach_get("co", verts)
            mesh.loop_triangles.foreach_get("vertices", tris)
            verts_all.append(verts.reshape(n_verts, 3))
            tris_all.append(tris.reshape(n_tris, 3) + offset)
            offset += n_verts
        finally:
            eval_obj.to_mesh_clear()

    if not verts_all:
        return None
    return np.concatenate(verts_all), np.concatenate(tris_all)


def _capture_bones(arm, bone_names, depsgraph):
    """World-space head→tail segments for the named bones."""
    eval_arm = arm.evaluated_get(depsgraph)
    mw = eval_arm.matrix_world
    segments: list[tuple[float, float, float]] = []
    for name in bone_names:
        pb = eval_arm.pose.bones.get(name)
        if pb is None:
            continue
        segments.append((mw @ pb.head)[:])
        segments.append((mw @ pb.tail)[:])
    return segments or None


def _anchors(arm, depsgraph, points):
    """``(root, label anchor)`` in world space for one captured pose.

    The label sits above the character's highest point rather than at the
    root, so it clears the body instead of landing in its feet.
    """
    from mathutils import Vector

    eval_arm = arm.evaluated_get(depsgraph)
    root_pb = next((pb for pb in eval_arm.pose.bones if pb.parent is None), None)
    root = (
        (eval_arm.matrix_world @ root_pb.head)
        if root_pb is not None
        else eval_arm.matrix_world.translation.copy()
    )
    top = max((p[2] for p in points), default=root.z)
    return Vector(root), Vector((root.x, root.y, top + 0.08))


# ---------------------------------------------------------------------------
# Bake
# ---------------------------------------------------------------------------

def rebuild(context=None) -> int:
    """Bake whichever parts of the overlay are out of date.

    The ghosts and whatever the Autoposer samples (its motion trail, when it is
    installed: ``posing.samplers()``) are independent -- either can be switched
    off, and a change to one never invalidates the other -- but all are sampled
    by stepping the playhead, which is by far the most expensive part. So they
    are baked in a single pass over the union of the frames they need.

    Moves the playhead, so never call it from a draw callback: use
    :func:`request_rebuild`, which defers onto a timer.
    """
    global _baking

    context = context or bpy.context
    scene = getattr(context, "scene", None)
    settings = _settings(scene)
    if settings is None:
        return 0

    # A generation owns the playhead and the armature's action while it bakes
    # and samples frame by frame. Stepping the frame under it would corrupt
    # that; the action swap at Accept/Reject leaves a stale signature, and the
    # draw callback asks for the rebake then.
    if settings.is_generating:
        return len(_ghosts["frames"])

    arm = _target(settings)
    action = _action(arm)
    if arm is None or action is None or not overlay_on(settings):
        clear()
        return 0

    ghost_sig = _ghost_signature(arm, action, settings)
    # Either half rebakes when its identity changed (different rig, action or
    # display mode) or when something reported a content change — a keyframe
    # edited, inserted, retimed, the rig moved. Identity alone would miss
    # every edit to the poses themselves.
    need_ghosts = ghosts_on(settings) and (
        _ghosts["dirty"] or _ghosts["signature"] != ghost_sig
    )
    from . import posing
    samplers = [s for s in posing.samplers() if s.wanted(settings) and s.stale(arm, action, settings)]
    root_sig = _root_path_signature(arm, action)
    need_root = root_path_on(settings) and (
        _root_path["dirty"] or _root_path["signature"] != root_sig
    )
    if need_root:
        _bake_root_path(arm, action, scene, root_sig)
    if not need_ghosts and not samplers:
        if need_root:
            tag_redraw()
        return len(_ghosts["frames"])

    key_frames: list[int] = []
    clamped = False
    meshes: list = []
    bone_names: list[str] = []
    if need_ghosts:
        key_frames = list(plan(scene)["frames"])
        clamped = len(key_frames) > MAX_GHOSTS
        if clamped:
            key_frames = key_frames[:MAX_GHOSTS]
        mode = settings.key_pose_display
        meshes = _skinned_meshes(arm, context) if mode in {'AUTO', 'MESH'} else []
        draw_bones = mode == 'BONES' or (mode == 'AUTO' and not meshes)
        bone_names = _ghost_bones(arm) if draw_bones else []
        if not meshes and not bone_names:
            key_frames = []

    sampled = {s: set(s.frames(arm, action, plan(scene)["frames"])) for s in samplers}

    shader = _shader()
    line_shader = _line_uniform_shader()
    ghosts: dict[int, dict] = {}
    roots: dict[int, tuple] = {}
    key_set = set(key_frames)
    every = set(key_set)
    for frames in sampled.values():
        every |= frames
    saved_frame = scene.frame_current
    saved_subframe = scene.frame_subframe

    _baking = True
    try:
        for f in sorted(every):
            scene.frame_set(f)
            # frame_set alone doesn't always push through driver / constraint
            # stacks; without this the evaluated matrices can hold the
            # previous frame's pose.
            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()

            for s, frames in sampled.items():
                if f in frames:
                    s.capture(arm, depsgraph, f)

            if f not in key_set:
                continue

            entry: dict = {"tris": None, "lines": None, "pick": None}
            points = ()
            if meshes:
                captured = _capture_meshes(meshes, depsgraph)
                if captured is not None:
                    verts, tris = captured
                    points = verts
                    entry["tris"] = batch_for_shader(
                        shader, 'TRIS', {"pos": verts}, indices=tris,
                    )
                    # Kept so a click can be tested against the body the
                    # artist actually sees. The BVH is built from it lazily,
                    # on the first click rather than in every bake.
                    entry["pick"] = {
                        "verts": verts, "tris": tris, "bvh": None,
                        "lo": verts.min(axis=0), "hi": verts.max(axis=0),
                    }
            if bone_names:
                segments = _capture_bones(arm, bone_names, depsgraph)
                if segments is not None:
                    points = points if len(points) else segments
                    entry["lines"] = batch_for_shader(
                        line_shader, 'LINES', {"pos": segments},
                    )
                    if entry["pick"] is None:
                        pts = np.asarray(segments, dtype='f')
                        entry["pick"] = {
                            "segments": segments, "bvh": None,
                            "lo": pts.min(axis=0), "hi": pts.max(axis=0),
                        }
            if entry["tris"] is None and entry["lines"] is None:
                continue
            ghosts[f] = entry
            roots[f] = _anchors(arm, depsgraph, points)
    finally:
        scene.frame_set(saved_frame, subframe=saved_subframe)
        try:
            context.view_layer.update()     # its evaluation, while still marked as ours
        except Exception:                   # noqa: BLE001
            pass
        _baking = False
        # The walk above happened with the control-seating handler muted, so
        # the controls are describing whatever frame the bake stopped on.
        posing.reseat(scene)

    # Each half is written back only if it was baked, and its signature is
    # stamped even when it produced nothing — "baked and empty" has to be
    # distinguishable from "never baked", or the draw callback asks forever.
    if need_ghosts:
        _ghosts["frames"] = [f for f in key_frames if f in ghosts]
        _ghosts["ghosts"] = ghosts
        _ghosts["roots"] = roots
        _ghosts["clamped"] = clamped
        _ghosts["kind"] = "BONES" if (bone_names and not meshes) else "MESH"
        _ghosts["signature"] = ghost_sig
        _ghosts["dirty"] = False
        _ghosts["arm"] = arm.name
    for s in samplers:
        s.commit(arm, action)
    tag_redraw()
    return len(_ghosts["frames"])


def _speed_color(u: float) -> tuple:
    """The speed ramp at *u* in 0..1."""
    lo, mid, hi = ROOT_PATH_SPEED_RAMP
    a, b, t = (lo, mid, u * 2.0) if u < 0.5 else (mid, hi, u * 2.0 - 1.0)
    return (*(x + (y - x) * t for x, y in zip(a, b)), ROOT_PATH_COLOR[3])


def _bake_root_path(arm, action, scene, signature) -> None:
    """Fit (or read back) the take's root trajectory for the viewport, and
    colour it by speed."""
    global _baking
    from . import inplace
    spans = []
    reuse, _root_path["reuse"] = _root_path["reuse"], False
    fps = scene.render.fps / scene.render.fps_base
    _baking = True
    try:
        for span in inplace.fitted_path(arm, action, scene, reuse=reuse):
            z = float(span.get("floor", 0.0)) + ROOT_PATH_LIFT
            pts = [(float(x), float(y), z) for x, y in span["path"]]
            speed = []
            for i in range(len(pts)):
                a, b = pts[max(i - 1, 0)], pts[min(i + 1, len(pts) - 1)]
                n = min(i + 1, len(pts) - 1) - max(i - 1, 0)
                speed.append(math.hypot(b[0] - a[0], b[1] - a[1]) * fps / n if n else 0.0)
            spans.append({"frames": list(span["frames"]), "label": span["label"],
                          "path": pts, "speed": speed})
    except Exception as exc:                 # noqa: BLE001 — never fail a bake over it
        print(f"[Animatica] root trajectory: {exc}")
    finally:
        _baking = False
    vmax = max([ROOT_PATH_SPEED_FLOOR] + [v for sp in spans for v in sp["speed"]])
    for sp in spans:
        sp["colors"] = [_speed_color(min(v / vmax, 1.0)) for v in sp["speed"]]
        if sp["speed"]:
            sp["label"] += f"  ·  {min(sp['speed']):.1f}–{max(sp['speed']):.1f} m/s"
    _root_path["vmax"] = vmax
    _root_path["spans"] = spans
    _root_path["signature"] = signature
    _root_path["dirty"] = False
    _root_path["arm"] = arm.name


def clear() -> None:
    """Drop everything baked. The plan is cheap and stays."""
    _root_path["spans"] = []
    _root_path["signature"] = None
    _root_path["dirty"] = True
    _root_path["arm"] = ""
    _root_path["reuse"] = False
    _ghosts["frames"] = []
    _ghosts["ghosts"] = {}
    _ghosts["roots"] = {}
    _ghosts["signature"] = None
    _ghosts["clamped"] = False
    _ghosts["dirty"] = True
    _ghosts["arm"] = ""
    from . import posing
    for s in posing.samplers():
        s.clear()
    tag_redraw()


def state() -> dict:
    """Read-only view of the bake, for the panel."""
    return {
        "count": len(_ghosts["frames"]),
        "clamped": _ghosts["clamped"],
        "kind": _ghosts["kind"],
        "stale": _rebuild_requested_at is not None,
    }


# ---------------------------------------------------------------------------
# Refresh scheduling
# ---------------------------------------------------------------------------

def request_rebuild(*, coalesce: bool = False) -> None:
    """Ask for a rebake once the scene stops changing.

    Safe from anywhere — property callbacks, handlers, even a draw callback —
    because all it does is arm a timer.

    ``coalesce`` keeps an already-running deadline instead of restarting it,
    and is what a repeating caller must use. The draw callback asks on every
    redraw while the cache is stale; during playback that is 24 times a
    second, and restarting the clock each time starved the timer so the bake
    never ran at all. Edits want the opposite — dragging a bone should keep
    pushing the bake out until the drag ends — so they restart it.
    """
    global _rebuild_requested_at, _rebuild_first_at
    # A request means "what was baked may no longer be true". Only the caller
    # knows that; the caches cannot tell from the rig's name.
    _ghosts["dirty"] = True
    from . import posing
    for s in posing.samplers():
        s.mark_dirty()
    _root_path["dirty"] = True
    _root_path["reuse"] = False
    if not coalesce:
        # an edit: the motion's poses changed -- Marionette's onion skin, when
        # it is installed, is of them too. (A coalescing ask comes from a draw
        # finding a cache stale: it says nothing about the motion.)
        posing.motion_changed()
    _arm_rebuild_timer(coalesce)


def motion_replaced() -> None:
    """The rig's motion was swapped out from under the overlay -- a take thrown
    away or kept, another version shown: what is drawn of it is of the motion
    before. An action rewritten in place tells the change handler nothing
    (Discard after a splice), so everything is let go and baked again."""
    _forget_ghosts()
    _seen["keys"] = _UNSEEN              # the keys looked at afresh
    invalidate_plan()
    from . import posing
    posing.motion_changed(replaced=True)
    request_rebuild()


def request_root_refresh() -> None:
    """Redraw the root trajectory after its curve was edited: only the path
    changed, so the take is not sampled again and the rest stays baked."""
    _root_path["dirty"] = True
    _root_path["reuse"] = True
    _arm_rebuild_timer(False)


def _arm_rebuild_timer(coalesce: bool) -> None:
    global _rebuild_requested_at, _rebuild_first_at
    now = time.monotonic()
    if _rebuild_first_at is None:
        _rebuild_first_at = now
    if not (coalesce and _rebuild_requested_at is not None):
        _rebuild_requested_at = now
    if not bpy.app.timers.is_registered(_rebuild_timer):
        bpy.app.timers.register(_rebuild_timer, first_interval=REBUILD_DEBOUNCE)


def _rebuild_timer():
    global _rebuild_requested_at, _rebuild_first_at

    requested = _rebuild_requested_at
    if requested is None:
        return None
    now = time.monotonic()
    waited = now - requested
    pending = now - (_rebuild_first_at or requested)
    if waited < REBUILD_DEBOUNCE and pending < REBUILD_MAX_WAIT:
        # Asked again while we waited — sit out the rest of the window, unless
        # the whole burst has gone on long enough that waiting for quiet has
        # become waiting forever.
        return min(REBUILD_DEBOUNCE - waited, max(0.05, REBUILD_MAX_WAIT - pending))

    # Held only for a generation, which owns the playhead and the action while
    # it samples frame by frame — stepping the frame under it would corrupt
    # what it reads.
    #
    # Playback used to hold it too, on the same reasoning. It does not need
    # to: a bake restores the frame it started on, and the player cannot
    # advance while the bake holds the main thread, so it resumes exactly
    # where it was. The cost is one hitch the length of a bake, against
    # ghosts that told the truth only when you stopped playing.
    settings = _settings(getattr(bpy.context, "scene", None))
    if settings is not None and settings.is_generating:
        return REBUILD_DEBOUNCE
    if posing():
        return REBUILD_DEBOUNCE        # a drag's pose is unkeyed until it ends: no stepping under it

    _rebuild_requested_at = None
    _rebuild_first_at = None
    try:
        rebuild()
    except Exception as exc:                # noqa: BLE001 — a timer must not raise
        print(f"[Animatica] key-pose bake failed: {exc}")
        clear()
    return None


def flash_keyed(frame: int) -> None:
    """Say that this frame has just become a key pose."""
    _flash["frame"] = int(frame)
    _flash["at"] = time.monotonic()
    tag_redraw()
    if not bpy.app.timers.is_registered(_pulse):
        bpy.app.timers.register(_pulse, first_interval=1 / 30)


def _pulse():
    """Redraw while the Timeline's ring opens out from a new key pose."""
    tag_redraw()
    return 1 / 30 if time.monotonic() - _flash["at"] < 1.3 else None


def _flashing(frame: int) -> bool:
    return (_flash["frame"] == frame
            and time.monotonic() - _flash["at"] < FLASH_SECONDS)


def tag_redraw() -> None:
    """Redraw the viewports and timelines that show the plan."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'DOPESHEET_EDITOR'}:
                area.tag_redraw()


def on_toggle(settings) -> None:
    """A half of the overlay — Ghosts or Trail — was switched on or off."""
    invalidate_plan()
    if overlay_on(settings):
        request_rebuild()
    else:
        clear()


def on_rebake_setting(settings) -> None:
    """A setting that changes the captured geometry changed."""
    if overlay_on(settings):
        request_rebuild()


def on_redraw_setting(_settings=None) -> None:
    """A setting that only changes how the plan is drawn changed."""
    tag_redraw()


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

#: how often (s) the keys are looked at while the animation plays, and from a redraw
KEYS_CHECK_PLAYING = 0.25
KEYS_CHECK_DRAW = 0.3
#: the keys never looked at yet (not the same as "looked at, and there is no action")
_UNSEEN = object()


def _keys_fingerprint(action) -> int:
    """A number that changes with any edit to ``action``'s keys: one added, deleted or
    moved, a handle or an interpolation changed, a curve added, removed or muted."""
    from . import constraints_ui
    crc = 0
    for fc in constraints_ui.iter_action_fcurves(action):
        kps = fc.keyframe_points
        n = len(kps)
        crc = zlib.crc32(f"{fc.data_path}[{fc.array_index}]{n}:{int(fc.mute)}:{len(fc.modifiers)}".encode(), crc)
        if not n:
            continue
        co = np.empty(n * 6, dtype=np.float32)
        kps.foreach_get("co", co[:2 * n])
        kps.foreach_get("handle_left", co[2 * n:4 * n])
        kps.foreach_get("handle_right", co[4 * n:])
        interp = np.empty(n, dtype=np.int32)
        kps.foreach_get("interpolation", interp)
        crc = zlib.crc32(interp.tobytes(), zlib.crc32(co.tobytes(), crc))
    return crc


def _keys_edited(action) -> bool:
    """Whether ``action``'s keys changed since they were last looked at; remembers them."""
    _seen["keys_at"] = time.monotonic()
    fp = None if action is None else (action.as_pointer(), _keys_fingerprint(action))
    if fp == _seen["keys"]:
        return False
    first = _seen["keys"] is _UNSEEN
    _seen["keys"] = fp
    return not first


def _placement(arm) -> tuple:
    """Where the character's object stands (ghosts are baked in world space)."""
    return tuple(round(v, 5) for row in arm.matrix_world for v in row)


def _edited(settings) -> None:
    """The motion changed: what the overlay shows of it is out of date."""
    # The plan is cheap and the panel reports it even with the ghosts off,
    # so it is always invalidated; only the geometry waits on the toggle.
    invalidate_plan()
    if overlay_on(settings) and settings.key_pose_auto_refresh:
        request_rebuild()
    else:
        tag_redraw()


def poll_edits(settings, gap: float = KEYS_CHECK_DRAW) -> None:
    """Catch an edit no update reported (or one looked at too soon to tell): from a
    redraw, and from the recheck a throttled look leaves behind. Cheap to call often --
    the keys are looked at once every ``gap`` seconds at most."""
    if _baking or posing() or settings is None or settings.is_generating:
        return
    if time.monotonic() - _seen["keys_at"] < gap:
        return
    arm = _target(settings)
    if arm is None:
        return
    edited = _keys_edited(_action(arm))
    if _seen["placement"] is None:
        _seen["placement"] = _placement(arm)
    if edited:
        _edited(settings)


def _recheck_keys():
    poll_edits(_settings(getattr(bpy.context, "scene", None)), gap=0.0)
    return None


def _keys_due(gap: float) -> bool:
    """Whether the keys may be looked at again; if not, a look is booked for when they may."""
    wait = gap - (time.monotonic() - _seen["keys_at"])
    if wait <= 0.0:
        return True
    if not bpy.app.timers.is_registered(_recheck_keys):
        bpy.app.timers.register(_recheck_keys, first_interval=wait)
    return False


@persistent
def _on_depsgraph(scene, depsgraph) -> None:
    """React to the edits that change what the overlay shows: the keys of the
    character's action (inserted, deleted, moved, retimed, their handles), and
    the character's object moved (ghosts are baked in world space).

    Blender only says the action or the object was *updated*, and it says so for
    evaluation as well as for edits: every frame of playback, every scrub, every
    frame a bake steps through and back. Telling them apart by circumstance --
    not while playing, not on a frame change, not just after a capture -- dropped
    real edits: a key deleted while the animation played, or just after the
    onion skin captured a frame (it fills in for seconds after every edit), left
    the ghosts and the trail stale for good. So the keys themselves are compared,
    by a fingerprint, with the ones last seen: an evaluation leaves it as it was,
    an edit changes it whenever and however it came. A redraw looks too
    (:func:`poll_edits`), so even an edit nothing reported shows within moments.

    The pose being dragged is still left alone: until it is keyed there is no
    change to show, and rebaking mid-drag would move the playhead under the
    artist. A drag keys (and asks for a rebuild) when it ends.
    """
    if _baking or posing():
        return
    settings = _settings(scene)
    if settings is None:
        return
    arm = _target(settings)
    if arm is None:
        return
    action = _action(arm)
    keys_touched = moved = False
    for update in depsgraph.updates:
        source = getattr(update.id, "original", update.id)
        if action is not None and source == action:
            keys_touched = True
        elif source == arm and update.is_updated_transform:
            moved = True
    if not (keys_touched or moved):
        return
    frame = (int(scene.frame_current), round(float(scene.frame_subframe), 3))
    stepped = frame != _seen["frame"]
    _seen["frame"] = frame
    edited = False
    if keys_touched:
        if playing():
            # every frame of playback touches the action: a few looks a second
            edited = _keys_due(KEYS_CHECK_PLAYING) and _keys_edited(action)
        elif not stepped:
            edited = _keys_edited(action)
        # (a scrub -- the frame changed, nothing playing -- evaluates the keys, it can't edit them)
    if moved:
        place = _placement(arm)
        if not stepped and place != _seen["placement"]:
            edited = True
        _seen["placement"] = place          # an animated object moves with the frame
    if edited:
        _edited(settings)


@persistent
def _on_undo(scene, _depsgraph=None) -> None:
    """Undo / redo rewrites keyframes without the signals above, so the plan
    would otherwise outlive the change it was built from."""
    invalidate_plan()
    settings = _settings(scene)
    if overlay_on(settings):
        request_rebuild()


# ---------------------------------------------------------------------------
# Draw — geometry
# ---------------------------------------------------------------------------

def _overlay_gate(context):
    """``(settings, plan)`` when the overlay should draw at all, else None."""
    space = getattr(context, "space_data", None)
    if space is None or space.type != 'VIEW_3D':
        return None
    if not space.overlay.show_overlays:
        return None
    settings = _settings(context.scene)
    if not overlay_on(settings):
        return None
    if _target(settings) is None:
        # no character (deleted, or none picked): nothing to draw ghosts of --
        # and what was cached of the last one is let go
        _forget_ghosts()
        return None
    return settings, plan(context.scene)


def _forget_ghosts() -> None:
    """Drop every cached ghost (the GPU batches with them)."""
    if _ghosts.get("frames"):
        _ghosts["frames"] = []
        _ghosts["ghosts"] = {}
        _ghosts["dirty"] = True
    from . import posing
    for s in posing.samplers():
        s.clear()


def _stale_ok(cache, arm) -> bool:
    """Whether what was baked is worth drawing while a rebake is pending.

    Yes, unless it came from a different character — the same poses on another
    rig are simply somewhere else.

    This is the difference between an overlay that flickers and one that
    vanishes. A rebake is deliberately held while the animation plays or a
    generation runs, and clicking Generate swaps the action, so the cache goes
    stale at the exact moment it cannot be refreshed: the ghosts went out and
    stayed out until the artist pressed Refresh. Showing the previous bake for
    those few seconds is wrong only in detail, and only briefly; showing
    nothing is wrong in a way that looks broken.
    """
    if not cache["frames"]:
        return False
    return not cache["arm"] or cache["arm"] == arm.name


def _ghosts_ready(settings) -> bool:
    """Whether to draw the ghosts; asks for a rebake when they are stale.

    Checked independently of the trail, and that independence is the point:
    one cache answering for both meant switching the trail off blanked the
    ghosts.

    An empty cache reads as "never baked" rather than "nothing to draw", so a
    file load or an addon reload heals itself; a bake that legitimately found
    nothing stamps its signature, so this cannot spin.
    """
    if not ghosts_on(settings):
        return False
    arm = _target(settings)
    if arm is None:
        return False
    fresh = (
        not _ghosts["dirty"]
        and _ghosts["signature"] == _ghost_signature(arm, _action(arm), settings)
    )
    if fresh:
        return True
    invalidate_plan()
    request_rebuild(coalesce=True)
    return _stale_ok(_ghosts, arm)


def _root_path_ready(settings) -> bool:
    """Whether to draw the root trajectory; asks for a refit when it is stale."""
    if not root_path_on(settings):
        return False
    arm = _target(settings)
    if arm is None:
        return False
    fresh = (
        not _root_path["dirty"]
        and _root_path["signature"] == _root_path_signature(arm, _action(arm))
    )
    if fresh:
        return bool(_root_path["spans"])
    # only the root trajectory is out of date here: the ghosts and the trail ask
    # for themselves, and a request_rebuild would lose "only the path changed"
    _arm_rebuild_timer(True)
    # the previous fit is worth drawing while the refit waits, if it was this rig's
    return bool(_root_path["spans"]) and _root_path["arm"] == arm.name


def refresh_held_by() -> str:
    """Why a pending rebake has not run yet, in words, or an empty string.

    The panel says this: a bake that is waiting is indistinguishable from one
    that is not coming.
    """
    if _rebuild_requested_at is None:
        return ""
    scene = getattr(bpy.context, "scene", None)
    settings = _settings(scene)
    if settings is not None and settings.is_generating:
        return "generating"
    return ""


#: what the change handler last saw: the frame (a change of it, nothing playing, is a
#: scrub, not an edit), the keys' fingerprint and when it was taken, and where the
#: character's object stood
_seen: dict = {"frame": None, "keys": _UNSEEN, "keys_at": 0.0, "placement": None}


def posing() -> bool:
    """Whether a drag is posing the rig right now (the Autoposer's: a handle,
    the trail, a zoetrope slice). Its pose lives on the rig until it is keyed,
    and any capture that steps the playhead and back re-evaluates the action
    over it: the dragged pose was lost."""
    try:
        from . import posing as _posing
        return _posing.dragging()
    except Exception:                       # noqa: BLE001
        return False


def playing() -> bool:
    """Whether any window plays the animation. Asked of every window, not
    bpy.context.screen: a timer has none, and the onion skin's capture,
    blind to playback, moved the playhead under it -- the ghosts flickered."""
    wm = getattr(bpy.context, "window_manager", None)
    for w in getattr(wm, "windows", ()):
        if w.screen is not None and w.screen.is_animation_playing:
            return True
    return False


def loop_span(settings):
    """``(start, cut)`` of the loop the rig plays -- its cycle is start..cut,
    and cut is start again -- or None when it is not a loop."""
    action = _action(_target(settings))
    meta = action.get("animatica_loop") if action is not None else None
    try:
        lo, hi = int(round(float(meta["start"]))), int(round(float(meta["cut"])))
    except (TypeError, KeyError, ValueError):
        return None
    return (lo, hi) if hi - lo >= 4 else None


def loop_wrap(f, span) -> int:
    """Frame ``f`` brought into the cycle."""
    lo, hi = span
    return lo + (int(f) - lo) % (hi - lo)


def frames_from(f, c, span=None) -> int:
    """Frames from ``c`` to ``f``, signed: in a loop the short way round."""
    d = int(f) - int(c)
    if span is None:
        return d
    p = span[1] - span[0]
    d = (d + p // 2) % p - p // 2
    return d


def _visible_poses(scene, p) -> list[tuple[int, dict]]:
    """The key poses to draw, with their plan entry: every one -- the motion
    plan, the poses the next take passes through.

    The pose under the playhead is suppressed: the rig itself is standing
    there, and a ghost inside it just muddies the silhouette.
    """
    current = scene.frame_current
    return [
        (f, p["entries"].get(f, {"block": None, "in_range": True}))
        for f in _ghosts["frames"]
        if f != current
    ]


# Pixels from a bone stick that still count as a hit. A skeleton ghost has no
# surface to click, so it gets a screen-space tolerance instead — roughly the
# slop Blender allows when clicking a bone.
PICK_BONE_RADIUS = 9.0


def _ghost_bvh(entry):
    """BVH of one ghost's body, built on first use and cached."""
    from mathutils.bvhtree import BVHTree

    pick = entry.get("pick")
    if pick is None or "verts" not in pick:
        return None
    if pick["bvh"] is None:
        pick["bvh"] = BVHTree.FromPolygons(
            [tuple(v) for v in pick["verts"]],
            [tuple(int(i) for i in tri) for tri in pick["tris"]],
            all_triangles=True,
        )
    return pick["bvh"]


def _ray_hits_box(origin, direction, lo, hi) -> bool:
    """Slab test — does the ray enter this ghost's bounding box at all?

    A cheap reject in front of the BVH, which is what makes a click cheap:
    building the tree for every ghost on the first click of a long blockout
    would hitch, and the ray misses nearly all of them.
    """
    tmin, tmax = 0.0, float("inf")
    for axis in range(3):
        d = direction[axis]
        o = origin[axis]
        if abs(d) < 1e-9:
            if o < lo[axis] or o > hi[axis]:
                return False
            continue
        t1 = (lo[axis] - o) / d
        t2 = (hi[axis] - o) / d
        if t1 > t2:
            t1, t2 = t2, t1
        tmin = max(tmin, t1)
        tmax = min(tmax, t2)
        if tmin > tmax:
            return False
    return True


def _scene_depth(context, origin, direction) -> float:
    """Distance to the nearest real geometry along the ray, or infinity.

    Used to keep a ghost from swallowing a click aimed at the character
    standing in front of it: the ghost only wins where it is the thing you
    can actually see.
    """
    try:
        depsgraph = context.evaluated_depsgraph_get()
        hit, location, _n, _i, _obj, _m = context.scene.ray_cast(
            depsgraph, origin, direction,
        )
    except (RuntimeError, ValueError):
        return float("inf")
    if not hit:
        return float("inf")
    return (location - origin).length


def _segment_distance_2d(px, py, a, b) -> float:
    """Pixel distance from ``(px, py)`` to the segment ``a``–``b``."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span <= 1e-9:
        return math.hypot(px - ax, py - ay)
    u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
    return math.hypot(px - (ax + u * dx), py - (ay + u * dy))


def pick_frame(context, x: float, y: float) -> int | None:
    """The key pose whose ghost lies under the region coordinates, if any.

    Bodies are hit-tested by raycasting the geometry that was baked, so the
    answer matches the silhouette on screen rather than a bounding box.
    Skeleton ghosts have no surface, so they are tested in screen space with
    a few pixels of slop. Nearest to the viewer wins when ghosts overlap.
    """
    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return None

    settings = _settings(context.scene)
    if not ghosts_on(settings):
        return None
    if not _ghosts["frames"]:
        return None

    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y))
    if origin is None or direction is None:
        return None

    current = context.scene.frame_current
    best_frame: int | None = None
    best_depth = float("inf")

    for frame in _ghosts["frames"]:
        if frame == current:
            continue        # its ghost is suppressed, so it cannot be clicked
        entry = _ghosts["ghosts"].get(frame)
        if entry is None or entry.get("pick") is None:
            continue

        pick = entry["pick"]
        lo, hi = pick.get("lo"), pick.get("hi")
        if lo is not None and not _ray_hits_box(origin, direction, lo, hi):
            continue

        bvh = _ghost_bvh(entry)
        if bvh is not None:
            location, _normal, _index, distance = bvh.ray_cast(origin, direction)
            if location is not None and distance < best_depth:
                best_frame, best_depth = frame, distance
            continue

        segments = pick.get("segments")
        if not segments:
            continue
        for i in range(0, len(segments) - 1, 2):
            head = view3d_utils.location_3d_to_region_2d(region, rv3d, Vector(segments[i]))
            tail = view3d_utils.location_3d_to_region_2d(region, rv3d, Vector(segments[i + 1]))
            if head is None or tail is None:
                continue
            if _segment_distance_2d(x, y, head, tail) > PICK_BONE_RADIUS * _px():
                continue
            depth = (Vector(segments[i]) - origin).dot(direction)
            if 0.0 < depth < best_depth:
                best_frame, best_depth = frame, depth

    if best_frame is None:
        return None
    # With X-Ray the ghosts are drawn over everything, so clicking one is
    # unambiguous. Without it they are occluded like anything else, and a
    # click landing on the character in front must go to the character.
    if not settings.key_pose_xray and _scene_depth(context, origin, direction) < best_depth:
        return None
    return best_frame


def _depth_only(draw, *, xray: bool) -> None:
    """Lay one ghost's nearest surface into the depth buffer, drawing no colour.

    Alpha adds up. A translucent body drawn straight has one layer of tint over
    the chest, two where an arm crosses it and three across a hand — the shape
    reads as blotches rather than a body. The cure is the standard one: write
    this ghost's depth first, then shade only the fragments that match it, so
    every pixel is tinted exactly once no matter how much of the body stacks up
    behind it.

    ``ALWAYS`` under X-Ray, because there the ghost is meant to ignore the
    scene's depth; the nearest-surface test then becomes last-triangle-wins
    within the ghost, which is uniform all the same.
    """
    gpu.state.color_mask_set(False, False, False, False)
    gpu.state.depth_mask_set(True)
    gpu.state.depth_test_set('ALWAYS' if xray else 'LESS_EQUAL')
    draw()
    gpu.state.depth_mask_set(False)
    gpu.state.color_mask_set(True, True, True, True)
    gpu.state.depth_test_set('EQUAL')


def _draw_geometry():
    """POST_VIEW callback: the key-pose ghosts (the motion plan), the root
    path, and what Marionette samples in the same pass (its motion trail)."""
    context = bpy.context
    gate = _overlay_gate(context)
    if gate is None:
        return
    settings, p = gate
    poll_edits(settings)

    from . import posing
    samplers = [s for s in posing.samplers() if s.ready(settings)]
    ghosts_ready = _ghosts_ready(settings)
    root_ready = _root_path_ready(settings)
    if not samplers and not ghosts_ready and not root_ready:
        return

    visible = _visible_poses(context.scene, p) if ghosts_ready else []
    ranks = _rank_from_playhead(_ghosts["frames"], context.scene.frame_current)
    editing = _editing_frame(settings)
    shader = _shader()
    ghosts = _ghosts["ghosts"]

    xray = settings.key_pose_xray
    scene_depth = 'NONE' if xray else 'LESS_EQUAL'

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set(scene_depth)
    # Nothing writes depth except each ghost's own pre-pass, which turns the
    # mask on for exactly as long as it takes (see _depth_only).
    gpu.state.depth_mask_set(False)
    try:
        if root_ready:
            _draw_root_path()
        for s in samplers:
            s.draw_view(context, settings, p)

        # Faintest first, so the poses nearest the playhead land on top.
        order = sorted(visible, key=lambda item: _pose_alpha(item[0], item[1], ranks, editing))
        for frame, entry in order:
            batches = ghosts.get(frame)
            if batches is None:
                continue
            rgb = _pose_color(frame, entry, settings)
            alpha = _pose_alpha(frame, entry, ranks, editing)
            shader.bind()
            shader.uniform_float("color", (*rgb, alpha))
            if batches["tris"] is not None:
                # Back faces would double the alpha wherever the silhouette
                # folds over itself and turn the ghost into a solid blob.
                gpu.state.face_culling_set('BACK')
                _depth_only(lambda: batches["tris"].draw(shader), xray=xray)
                batches["tris"].draw(shader)          # now depth-test EQUAL
                gpu.state.depth_test_set(scene_depth)
                gpu.state.face_culling_set('NONE')
            if batches["lines"] is not None:
                line_shader = _line_uniform_shader()
                line_shader.bind()
                line_shader.uniform_float("viewportSize", _viewport_size())
                line_shader.uniform_float("lineWidth", BONE_LINE_WIDTH * _px())
                line_shader.uniform_float(
                    "color", (*rgb, min(1.0, alpha * BONE_ALPHA_BOOST)),
                )
                if batches["tris"] is None:
                    # Sticks cross each other as readily as limbs do, so they
                    # get the same single-layer treatment — but only when they
                    # are the whole ghost. Alongside a mesh they would sit
                    # behind its surface and fail the equality test entirely.
                    _depth_only(lambda: batches["lines"].draw(line_shader), xray=xray)
                batches["lines"].draw(line_shader)
                gpu.state.depth_test_set(scene_depth)
                shader.bind()
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.color_mask_set(True, True, True, True)
        gpu.state.depth_mask_set(True)
        gpu.state.depth_test_set('NONE')
        gpu.state.face_culling_set('NONE')


def _diamond(x: float, y: float, r: float):
    return ((x, y + r), (x + r, y), (x, y - r), (x - r, y))


def _draw_root_path() -> None:
    """The root trajectory, on the floor: one line per span, coloured by speed."""
    spans = _root_path["spans"]
    if not spans:
        return
    line = _line_shader()
    line.bind()
    line.uniform_float("viewportSize", _viewport_size())
    line.uniform_float("lineWidth", ROOT_PATH_WIDTH * _px())
    for span in spans:
        if len(span["path"]) >= 2 and len(span.get("colors", ())) == len(span["path"]):
            batch_for_shader(line, 'LINE_STRIP', {"pos": span["path"], "color": span["colors"]}).draw(line)


def _draw_root_path_markers(region, rv3d, current: int) -> None:
    """Ticks along the root trajectory (their spacing is its speed), a marker
    where it is at the playhead, and what it is, written at its start."""
    spans = _root_path["spans"]
    if not spans:
        return
    px = _px()
    verts, colors, indices = [], [], []

    def diamond(co, r, color):
        base = len(verts)
        verts.extend(_diamond(co.x, co.y, r * px))
        colors.extend([color] * 4)
        indices.extend(((base, base + 1, base + 2), (base, base + 2, base + 3)))

    font_id = 0
    blf.size(font_id, int(LABEL_SIZE * px))
    for span in spans:
        first, _last = span["frames"]
        for i, point in enumerate(span["path"]):
            f = first + i
            if f != current and i % ROOT_PATH_TICK_EVERY:
                continue
            co = view3d_utils.location_3d_to_region_2d(region, rv3d, point)
            if co is not None:
                if f == current:
                    diamond(co, ROOT_PATH_CURRENT_RADIUS, ROOT_PATH_CURRENT_COLOR)
                else:
                    diamond(co, ROOT_PATH_TICK_RADIUS, span["colors"][i])
        co = view3d_utils.location_3d_to_region_2d(region, rv3d, span["path"][0])
        if co is not None and span["label"]:
            text = f"root: {span['label']}"
            blf.position(font_id, co.x + 8 * px, co.y - (LABEL_SIZE + 6) * px, 0)
            blf.color(font_id, *ROOT_PATH_COLOR)
            blf.draw(font_id, text)
    if verts:
        shader = gpu.shader.from_builtin("SMOOTH_COLOR")
        shader.bind()
        batch_for_shader(shader, 'TRIS', {"pos": verts, "color": colors}, indices=indices).draw(shader)


def _draw_screen():
    """POST_PIXEL callback: the root path's markers, the samplers' (the
    Autoposer's trail markers) and the frame labels.

    Labels are part of the ghosts -- they answer "which frame is that pose" --
    so they follow the ghost toggle; a sampler's markers follow its own.
    Neither waits on the other.
    """
    context = bpy.context
    gate = _overlay_gate(context)
    if gate is None:
        return
    settings, p = gate

    region = context.region
    rv3d = context.region_data
    if region is None or rv3d is None:
        return

    from . import posing
    samplers = [s for s in posing.samplers() if s.ready(settings)]
    ghosts_ready = _ghosts_ready(settings)
    root_ready = _root_path_ready(settings)
    if not samplers and not ghosts_ready and not root_ready:
        return

    gpu.state.blend_set('ALPHA')
    try:
        if root_ready:
            _draw_root_path_markers(region, rv3d, context.scene.frame_current)
        for s in samplers:
            s.draw_pixel(context, settings, p, region, rv3d, context.scene.frame_current)

        if not ghosts_ready or not settings.key_pose_labels:
            return

        roots = _ghosts["roots"]
        editing = _editing_frame(settings)
        font_id = 0
        px = _px()
        blf.size(font_id, int(LABEL_SIZE * px))
        taken = []                        # labels already drawn: a new one never lands on one
        for frame, entry in _visible_poses(context.scene, p):
            anchor = roots.get(frame)
            if anchor is None:
                continue
            co = view3d_utils.location_3d_to_region_2d(region, rv3d, anchor[1])
            if co is None:
                continue        # behind the viewer
            if _flashing(frame):
                text = f"{frame} · keyed"
            elif frame == editing:
                text = f"{frame} · editing"
            elif entry["in_range"]:
                text = str(frame)
            else:
                text = f"{frame} ✕"
            width, height = blf.dimensions(font_id, text)
            box = (co.x - width * 0.5 - 3 * px, co.y + LABEL_OFFSET_PX * px - 2 * px,
                   co.x + width * 0.5 + 3 * px, co.y + LABEL_OFFSET_PX * px + height + 2 * px)
            if any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in taken):
                continue
            taken.append(box)
            blf.position(font_id, co.x - width * 0.5, co.y + LABEL_OFFSET_PX * px, 0)
            blf.color(font_id, *(
                FLASH_COLOR if _flashing(frame)
                else LABEL_COLOR if entry["in_range"] else LABEL_DROPPED))
            blf.draw(font_id, text)
    finally:
        gpu.state.blend_set('NONE')


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class ANIMATICA_OT_key_poses_refresh(bpy.types.Operator):
    bl_idname = "animatica.key_poses_refresh"
    bl_label = "Refresh Key Poses"
    bl_description = "Read the key poses from the armature again and redraw their ghosts"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        settings = _settings(context.scene)
        return settings is not None and _target(settings) is not None

    def execute(self, context):
        settings = _settings(context.scene)
        invalidate_plan()
        if not overlay_on(settings):
            settings.key_pose_overlay = True  # the update callbacks bake
            settings.key_pose_ghosts = True
            return {'FINISHED'}
        if rebuild(context) == 0:
            self.report({'INFO'}, "No key poses to show. Pose the rig and insert a keyframe")
        return {'FINISHED'}


class ANIMATICA_OT_step_key_pose(bpy.types.Operator):
    bl_idname = "animatica.step_key_pose"
    bl_label = "Jump to Key Pose"
    bl_description = (
        "Move the playhead to the next or previous pose you keyed. Blender's "
        "own keyframe jump stops on every frame of a generated take, and this "
        "stops only on your poses"
    )
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        name="Direction",
        items=[("PREV", "Previous", ""), ("NEXT", "Next", "")],
        default="NEXT",
    )

    def execute(self, context):
        frames = plan(context.scene)["frames"]
        if not frames:
            return {'CANCELLED'}
        current = context.scene.frame_current
        if self.direction == 'PREV':
            candidates = [f for f in frames if f < current]
            target = candidates[-1] if candidates else frames[-1]
        else:
            candidates = [f for f in frames if f > current]
            target = candidates[0] if candidates else frames[0]
        context.scene.frame_set(target)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    ANIMATICA_OT_key_poses_refresh,
    ANIMATICA_OT_step_key_pose,
)

_DEPSGRAPH_HANDLER_NAME = "_on_depsgraph"
_UNDO_HANDLER_NAME = "_on_undo"


def _purge_stale_handlers(handler_list, fn_name: str) -> None:
    """Drop previously-registered copies by name — an addon reload makes a new
    function object each time, so identity checks never match."""
    for h in list(handler_list):
        if getattr(h, "__name__", None) == fn_name:
            handler_list.remove(h)


def register_draw_handlers() -> None:
    """Install the two draw handlers, replacing any the previous module load
    left behind.

    Reusing an existing handle is not enough: the callback Blender holds is
    the *old module's* function, and it keeps drawing from that module's
    globals. After a reload it reads properties that no longer exist, raises
    inside the draw loop, and the overlay silently goes blank. Drop it and
    bind the handler to the code that is running now.
    """
    global _geometry_handle, _label_handle
    ns = bpy.app.driver_namespace
    for key in (_NS_GEOMETRY, _NS_LABELS):
        stale = ns.pop(key, None)
        if stale is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(stale, 'WINDOW')
            except (ValueError, RuntimeError):
                pass
    _geometry_handle = bpy.types.SpaceView3D.draw_handler_add(
        _draw_geometry, (), 'WINDOW', 'POST_VIEW',
    )
    ns[_NS_GEOMETRY] = _geometry_handle
    _label_handle = bpy.types.SpaceView3D.draw_handler_add(
        _draw_screen, (), 'WINDOW', 'POST_PIXEL',
    )
    ns[_NS_LABELS] = _label_handle


def unregister_draw_handlers() -> None:
    global _geometry_handle, _label_handle
    ns = bpy.app.driver_namespace
    for key, handle in ((_NS_GEOMETRY, _geometry_handle), (_NS_LABELS, _label_handle)):
        h = handle or ns.get(key)
        if h is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
            except (ValueError, RuntimeError):
                pass
        ns.pop(key, None)
    _geometry_handle = None
    _label_handle = None


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    _purge_stale_handlers(bpy.app.handlers.depsgraph_update_post, _DEPSGRAPH_HANDLER_NAME)
    bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        _purge_stale_handlers(handlers, _UNDO_HANDLER_NAME)
        handlers.append(_on_undo)
    register_draw_handlers()


def unregister() -> None:
    unregister_draw_handlers()
    _purge_stale_handlers(bpy.app.handlers.depsgraph_update_post, _DEPSGRAPH_HANDLER_NAME)
    for handlers in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        _purge_stale_handlers(handlers, _UNDO_HANDLER_NAME)
    for timer in (_rebuild_timer, _recheck_keys):
        if bpy.app.timers.is_registered(timer):
            bpy.app.timers.unregister(timer)
    clear()
    invalidate_plan()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
