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

import blf
import bpy
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
DROPPED_COLOR   = (0.50, 0.50, 0.52)

# The motion trail: where the body actually goes, frame by frame, against the
# key poses that asked it to. Sampled points per bone — a longer action is
# stepped rather than refused, so the shape of the motion still reads.
MAX_TRAIL_FRAMES = 600
TRAIL_WIDTH = 2.5
# Marker radii, in logical pixels — scaled by the display's pixel size at draw
# time. GPU point size is ignored on the Metal backend, so the markers are
# drawn as diamonds in the 2D pass instead of as points in the 3D one.
TRAIL_DOT_RADIUS = 1.8            # one per frame: spacing is speed
TRAIL_KEY_RADIUS = 4.5            # the frames you keyed, on the curve
TRAIL_CURRENT_RADIUS = 3.5
TRAIL_NEUTRAL_COLOR = (0.80, 0.82, 0.88)   # frames under no prompt block
TRAIL_ALPHA = 0.80
# Frames either side of the playhead the trail is drawn at full strength, and
# how faint it goes beyond that. Six curves across a hundred frames at one
# weight is a thicket you cannot read the near motion through; the far parts
# are context, and context belongs in the background.
TRAIL_NEAR_FRAMES = 12
TRAIL_FAR_FACTOR = 0.18
TRAIL_ALPHA_DROPPED = 0.28        # frames the next generation will not touch
TRAIL_CURRENT_COLOR = (1.0, 1.0, 1.0, 0.95)

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

# The motion trail, kept in its own cache with its own signature.
#
# Deliberately NOT stored alongside the ghosts: they are two independent
# overlays that happen to be cheapest to sample in one pass over the playhead.
# Sharing one cache meant a stale or switched-off trail blanked perfectly good
# ghosts, and switching the trail off made the whole overlay disappear until
# something rebaked it.
#   bones      traced bone names, in draw order
#   frames     the frames sampled, ascending (every authored frame included)
#   points     bone -> world position per sampled frame, parallel to frames
_trail: dict = {
    "bones": [],
    "frames": [],
    "points": {},
    "signature": None,
    "dirty": True,
    "arm": "",
}

# Per-frame trail colours are a pure function of the plan, so they are
# computed once per plan change instead of on every redraw.
_trail_colors: dict = {"signature": None, "frames": None, "colors": []}

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
    return (arm.name, action.name, settings.key_pose_display)


def _trail_signature(arm, action):
    """What the sampled trail depends on.

    The traced bones are derived from the rig itself — end effectors, root and
    head — so they are not part of this: resolving them walks the constraint
    stack, which is too much work to repeat on every redraw, and the answer
    only changes when the rig does. Switching armature changes the name here;
    renaming bones on the same rig needs a Refresh.
    """
    if arm is None or action is None:
        return None
    return (arm.name, action.name)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

def _plan_signature(settings, arm, action, scene):
    """Everything the plan is derived from, cheap enough to check per draw.

    Block geometry is in here, so dragging a strip on the timeline re-tints
    the ghosts without any handler having to notice.
    """
    blocks = tuple(
        (int(b.frame_start), int(b.frame_end), bool(b.enabled), bool((b.prompt or "").strip()))
        for b in settings.prompt_blocks
    )
    return (
        arm.name if arm else None,
        action.name if action else None,
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


def dropped_frames(scene=None) -> list[int]:
    """Key poses the request will silently leave out."""
    p = plan(scene)
    return [f for f in p["frames"] if not p["entries"][f]["in_range"]]


def overlay_on(settings) -> bool:
    """Is any part of the plan overlay switched on?

    There is no separate master switch: the two halves the artist picks —
    Ghosts and Trail — are the switch. A panel that turned the whole overlay
    off from its header read as turning *posing* off, which it never did.
    """
    return bool(settings is not None
                and (settings.key_pose_ghosts or settings.key_pose_trail))


def timeline_ticks(scene) -> tuple[list[tuple[int, bool]], tuple[int, int]]:
    """``([(frame, in_range), …], window)`` for the timeline lane overlay.

    Returns nothing while the feature is switched off — the lane should not
    grow marks the artist did not ask for.
    """
    settings = _settings(scene)
    if not overlay_on(settings):
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


def _frame_color(frame: int, settings, p) -> tuple[float, float, float, float]:
    """Colour of one frame of motion: its prompt block, or neutral, dimmed
    when the next generation will not reach it."""
    from . import timeline_overlay

    index = _owning_block(frame, settings.prompt_blocks)
    if index is None:
        rgb = TRAIL_NEUTRAL_COLOR
    else:
        try:
            block = settings.prompt_blocks[index]
            rgb = tuple(timeline_overlay._strip_color(block, index)[:3])
        except (IndexError, TypeError):
            rgb = TRAIL_NEUTRAL_COLOR
    window = p["range"]
    inside = window[0] <= frame <= window[1]
    return (*rgb, TRAIL_ALPHA if inside else TRAIL_ALPHA_DROPPED)


def _trail_fade(frame: int, current: int) -> float:
    """How strongly this frame of the trail is drawn, by distance from the
    playhead. Near motion is what is being worked on; the rest is context."""
    d = abs(frame - current)
    if d <= TRAIL_NEAR_FRAMES:
        return 1.0
    t = min(1.0, (d - TRAIL_NEAR_FRAMES) / float(TRAIL_NEAR_FRAMES * 2))
    return 1.0 + (TRAIL_FAR_FACTOR - 1.0) * t


def _trail_color_list(settings, p, frames, current: int):
    """One colour per sampled frame, cached against the plan and the playhead.

    The colour is what makes the trail a *plan* view rather than a motion
    path: the curve changes colour where the prompt blocks change, so you can
    see which stretch of the motion belongs to which instruction. The alpha is
    what makes it readable: it falls away from the playhead, so the passage
    being worked on is the one that reads.
    """
    key = (p["signature"], len(frames), current)
    if _trail_colors["signature"] == key:
        return _trail_colors["colors"]
    colors = []
    for f in frames:
        r, g, b, a = _frame_color(f, settings, p)
        colors.append((r, g, b, a * _trail_fade(f, current)))
    _trail_colors["signature"] = key
    _trail_colors["colors"] = colors
    return colors


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
    """Frame of the open edit session, or -1. Never raises in a draw."""
    from . import pose_edit

    try:
        session = pose_edit.active_session(settings, _target(settings))
    except Exception:                       # noqa: BLE001 — never break a draw
        return -1
    return -1 if session is None else int(session)


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


# The joints the motion trail follows: the end effectors, the root and the
# head. These are the joints the model is steered by — the hands and feet an
# effector pin targets, the hips carrying the trajectory, the head carrying
# the gaze — so their paths are the ones worth reading. Everything between
# them is interpolation the artist does not direct directly.
_TRAIL_JOINTS = ("Hips", "Head", "LeftHand", "RightHand", "LeftFoot", "RightFoot")


def _trail_bones(arm) -> list[str]:
    """The bones the motion trail follows, resolved onto this rig.

    Canonical joint names go through the same resolver effector pins use, so a
    namespaced rig (``animatica:LeftHand``), a Mixamo one
    (``mixamorig:LeftHand``) and a differently-spelled one (``hand.L``) all
    land on the right bone. On a control rig the deform bone is traced, since
    that is the body — the control is a handle floating beside it, and it is
    the deform skeleton the request carries.

    Not called from a draw callback: resolution walks the rig's constraint
    stack, so the answer is cached in the trail's signature instead.
    """
    from . import constraints_ui, request_builder

    if arm is None or arm.type != 'ARMATURE':
        return []

    allowed = None
    try:
        if request_builder.is_control_rig(arm):
            allowed = request_builder.emitted_deform_bones(arm) or None
    except Exception:                       # noqa: BLE001 — never break a bake
        allowed = None

    names: list[str] = []
    for joint in _TRAIL_JOINTS:
        pb = constraints_ui.resolve_effector_bone(arm, joint, allowed)
        if pb is not None and pb.name not in names:
            names.append(pb.name)

    # A rig that names its root something else still gets its trajectory
    # traced — that is the one line the plan is mostly about.
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


def _trail_frames(action, key_frames: list[int]) -> list[int]:
    """The frames to sample for the trail, ascending.

    Long actions are stepped rather than refused — the shape of the motion
    still reads at every other frame, and every authored frame is kept in the
    sample whatever the step, so the key markers always sit exactly on the
    curve.
    """
    extent = _motion_extent(action)
    if extent is None:
        return []
    lo, hi = extent
    span = hi - lo + 1
    step = max(1, math.ceil(span / MAX_TRAIL_FRAMES))
    frames = set(range(lo, hi + 1, step))
    frames.add(hi)
    frames.update(f for f in key_frames if lo <= f <= hi)
    return sorted(frames)


def _capture_trail(arm, bone_names, depsgraph) -> dict[str, tuple]:
    """World position of each traced bone at the current frame."""
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
    """Bake whichever halves of the overlay are out of date.

    The ghosts and the trail are independent — either can be switched off, and
    a change to one never invalidates the other — but both are sampled by
    stepping the playhead, which is by far the most expensive part. So they
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
    trail_sig = _trail_signature(arm, action)
    # Either half rebakes when its identity changed (different rig, action or
    # display mode) or when something reported a content change — a keyframe
    # edited, inserted, retimed, the rig moved. Identity alone would miss
    # every edit to the poses themselves.
    need_ghosts = settings.key_pose_ghosts and (
        _ghosts["dirty"] or _ghosts["signature"] != ghost_sig
    )
    need_trail = settings.key_pose_trail and (
        _trail["dirty"] or _trail["signature"] != trail_sig
    )
    if not need_ghosts and not need_trail:
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

    trail_bones = _trail_bones(arm) if need_trail else []
    trail_frames = (
        _trail_frames(action, plan(scene)["frames"]) if trail_bones else []
    )

    shader = _shader()
    line_shader = _line_uniform_shader()
    ghosts: dict[int, dict] = {}
    roots: dict[int, tuple] = {}
    trail_points: dict[str, list] = {name: [] for name in trail_bones}
    key_set = set(key_frames)
    trail_set = set(trail_frames)
    saved_frame = scene.frame_current
    saved_subframe = scene.frame_subframe

    _baking = True
    try:
        for f in sorted(key_set | trail_set):
            scene.frame_set(f)
            # frame_set alone doesn't always push through driver / constraint
            # stacks; without this the evaluated matrices can hold the
            # previous frame's pose.
            context.view_layer.update()
            depsgraph = context.evaluated_depsgraph_get()

            if f in trail_set:
                sampled = _capture_trail(arm, trail_bones, depsgraph)
                for name in trail_bones:
                    point = sampled.get(name)
                    if point is not None:
                        trail_points[name].append(point)

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
        _baking = False
        # The walk above happened with the control-seating handler muted, so
        # the controls are describing whatever frame the bake stopped on.
        try:
            from . import autopose_sync
            autopose_sync.reseat_controls(scene)
        except Exception:                   # noqa: BLE001 — never fail a bake
            pass

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
    if need_trail:
        # A bone that never resolved leaves a short list; drop it rather than
        # draw a trail that does not line up with the sampled frames.
        trail_points = {
            name: pts for name, pts in trail_points.items()
            if len(pts) == len(trail_frames)
        }
        _trail["bones"] = [name for name in trail_bones if name in trail_points]
        _trail["frames"] = trail_frames
        _trail["points"] = trail_points
        _trail["signature"] = trail_sig
        _trail["dirty"] = False
        _trail["arm"] = arm.name
        _trail_colors["signature"] = None

    tag_redraw()
    return len(_ghosts["frames"])


def clear() -> None:
    """Drop everything baked. The plan is cheap and stays."""
    _ghosts["frames"] = []
    _ghosts["ghosts"] = {}
    _ghosts["roots"] = {}
    _ghosts["signature"] = None
    _ghosts["clamped"] = False
    _ghosts["dirty"] = True
    _ghosts["arm"] = ""
    _trail["bones"] = []
    _trail["frames"] = []
    _trail["points"] = {}
    _trail["signature"] = None
    _trail["dirty"] = True
    _trail["arm"] = ""
    _trail_colors["signature"] = None
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
    _trail["dirty"] = True
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

def _on_depsgraph(scene, depsgraph) -> None:
    """React to the two edits that change the plan.

    Only two kinds of update can: a change to the **action** (inserting,
    deleting, retiming or auto-keying a pose — Blender tags the Action
    datablock and nothing else), and a transform on the **armature object**
    (ghosts are baked in world space, so sliding the rig leaves them behind).

    Everything else is ignored on purpose, above all the plain pose edit:
    dragging a bone tags the object on every mouse-move, but until it is keyed
    there is no plan change to show, and rebaking mid-drag would move the
    playhead under the artist.
    """
    if _baking:
        return
    settings = _settings(scene)
    if settings is None:
        return
    arm = _target(settings)
    if arm is None:
        return
    # Playback reports the action updated on every frame — animation
    # evaluation touches it — and that is not an edit. Taking it for one meant
    # a rebuild request sixty times a second, each pushing the wait out, so
    # nothing ever refreshed while the animation ran. Playback cannot change a
    # key; the paths that do (keying a pose, dragging a curve) ask for a
    # rebuild themselves and do not come through here.
    screen = getattr(bpy.context, "screen", None)
    if screen is not None and getattr(screen, "is_animation_playing", False):
        return

    action = _action(arm)
    for update in depsgraph.updates:
        source = getattr(update.id, "original", update.id)
        keys_changed = action is not None and source == action
        moved = source == arm and update.is_updated_transform
        if not (keys_changed or moved):
            continue
        # The plan is cheap and the panel reports it even with the ghosts off,
        # so it is always invalidated; only the geometry waits on the toggle.
        invalidate_plan()
        if overlay_on(settings) and settings.key_pose_auto_refresh:
            request_rebuild()
        else:
            tag_redraw()
        return


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
    return settings, plan(context.scene)


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
    if not settings.key_pose_ghosts:
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


def _trail_ready(settings) -> bool:
    """Whether to draw the trail; asks for a rebake when it is stale."""
    if not settings.key_pose_trail:
        return False
    arm = _target(settings)
    if arm is None:
        return False
    fresh = (
        not _trail["dirty"]
        and _trail["signature"] == _trail_signature(arm, _action(arm))
    )
    if fresh:
        return True
    request_rebuild(coalesce=True)
    return _stale_ok(_trail, arm)


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


def _visible_poses(scene, p) -> list[tuple[int, dict]]:
    """The key poses to draw, with their plan entry.

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
    if settings is None or not settings.key_pose_ghosts:
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


def _draw_geometry():
    """POST_VIEW callback: the key-pose ghosts and the motion trail."""
    context = bpy.context
    gate = _overlay_gate(context)
    if gate is None:
        return
    settings, p = gate

    trail_ready = _trail_ready(settings)
    ghosts_ready = _ghosts_ready(settings)
    if not trail_ready and not ghosts_ready:
        return

    visible = _visible_poses(context.scene, p) if ghosts_ready else []
    ranks = _rank_from_playhead(_ghosts["frames"], context.scene.frame_current)
    editing = _editing_frame(settings)
    shader = _shader()
    ghosts = _ghosts["ghosts"]

    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('NONE' if settings.key_pose_xray else 'LESS_EQUAL')
    # Test against the scene's depth but don't write to it: a translucent
    # ghost that wrote depth would punch a hole in every ghost after it.
    gpu.state.depth_mask_set(False)
    try:
        if trail_ready:
            _draw_trail(settings, p)

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
                batches["tris"].draw(shader)
                gpu.state.face_culling_set('NONE')
            if batches["lines"] is not None:
                line_shader = _line_uniform_shader()
                line_shader.bind()
                line_shader.uniform_float("viewportSize", _viewport_size())
                line_shader.uniform_float("lineWidth", BONE_LINE_WIDTH * _px())
                line_shader.uniform_float(
                    "color", (*rgb, min(1.0, alpha * BONE_ALPHA_BOOST)),
                )
                batches["lines"].draw(line_shader)
                shader.bind()
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.depth_mask_set(True)
        gpu.state.depth_test_set('NONE')
        gpu.state.face_culling_set('NONE')


def _draw_trail(settings, p) -> None:
    """The motion itself: where the body actually goes, frame by frame.

    One line per traced bone, coloured per frame, so the path changes colour
    where the prompt blocks change. The per-frame markers along it are drawn
    in the 2D pass (see :func:`_draw_screen`) — together with the ghosts that
    is the whole statement: the poses you asked for, and the motion that
    answers them.
    """
    trail = _trail
    frames = trail["frames"]
    if not trail["bones"] or len(frames) < 2:
        return

    colors = _trail_color_list(settings, p, frames, int(bpy.context.scene.frame_current))
    px = _px()
    viewport = _viewport_size()
    line = _line_shader()

    for name in trail["bones"]:
        points = trail["points"].get(name)
        if not points or len(points) != len(colors):
            continue

        # The path itself, changing colour where the prompt blocks change.
        line.bind()
        line.uniform_float("viewportSize", viewport)
        line.uniform_float("lineWidth", TRAIL_WIDTH * px)
        batch_for_shader(
            line, 'LINE_STRIP', {"pos": points, "color": colors},
        ).draw(line)


def _diamond(x: float, y: float, r: float):
    return ((x, y + r), (x + r, y), (x, y - r), (x - r, y))


def _draw_trail_markers(settings, p, region, rv3d, current: int) -> None:
    """Per-frame markers along the trail, in screen space.

    Screen space because GPU point size is ignored on the Metal backend, and
    because a marker is a piece of UI — it should stay the same size however
    far the camera is from the character, the way Blender's own keyframe
    markers do.

    A small diamond per sampled frame (their spacing is the timing — bunched
    means slow, spread means fast), a bigger one on every frame you keyed, and
    a white one on the playhead.
    """
    trail = _trail
    frames = trail["frames"]
    if not trail["bones"] or len(frames) < 2:
        return

    colors = _trail_color_list(settings, p, frames, current)
    key_frames = set(p["frames"])
    px = _px()

    verts: list[tuple[float, float]] = []
    vert_colors: list[tuple[float, float, float, float]] = []
    indices: list[tuple[int, int, int]] = []

    for name in trail["bones"]:
        points = trail["points"].get(name)
        if not points or len(points) != len(colors):
            continue
        for i, frame in enumerate(frames):
            co = view3d_utils.location_3d_to_region_2d(region, rv3d, points[i])
            if co is None:
                continue        # behind the viewer
            if frame == current:
                radius, color = TRAIL_CURRENT_RADIUS, TRAIL_CURRENT_COLOR
            elif frame in key_frames:
                radius, color = TRAIL_KEY_RADIUS, colors[i]
            else:
                radius, color = TRAIL_DOT_RADIUS, colors[i]
            base = len(verts)
            verts.extend(_diamond(co.x, co.y, radius * px))
            vert_colors.extend([color] * 4)
            indices.extend(((base, base + 1, base + 2), (base, base + 2, base + 3)))

    if not verts:
        return
    shader = gpu.shader.from_builtin("SMOOTH_COLOR")
    shader.bind()
    batch_for_shader(
        shader, 'TRIS', {"pos": verts, "color": vert_colors}, indices=indices,
    ).draw(shader)


def _draw_screen():
    """POST_PIXEL callback: the trail's markers and the frame labels.

    Labels are part of the ghosts — they answer "which frame is that pose" —
    so they follow the ghost toggle, while the markers follow the trail's.
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

    trail_ready = _trail_ready(settings)
    ghosts_ready = _ghosts_ready(settings)
    if not trail_ready and not ghosts_ready:
        return

    gpu.state.blend_set('ALPHA')
    try:
        if trail_ready:
            _draw_trail_markers(settings, p, region, rv3d, context.scene.frame_current)

        if not ghosts_ready or not settings.key_pose_labels:
            return

        roots = _ghosts["roots"]
        editing = _editing_frame(settings)
        font_id = 0
        px = _px()
        blf.size(font_id, int(LABEL_SIZE * px))
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
            width, _height = blf.dimensions(font_id, text)
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
    bl_description = "Re-read the key poses from the armature and re-bake their ghosts"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        settings = _settings(context.scene)
        return settings is not None and _target(settings) is not None

    def execute(self, context):
        settings = _settings(context.scene)
        invalidate_plan()
        if not overlay_on(settings):
            settings.key_pose_ghosts = True   # the update callback bakes
            return {'FINISHED'}
        if rebuild(context) == 0:
            self.report({'INFO'}, "No key poses to show — pose the rig and insert a keyframe")
        return {'FINISHED'}


class ANIMATICA_OT_step_key_pose(bpy.types.Operator):
    bl_idname = "animatica.step_key_pose"
    bl_label = "Jump to Key Pose"
    bl_description = (
        "Move the playhead to the next or previous pose you keyed. Blender's "
        "own keyframe jump stops on every frame of a generated take; this "
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
    if bpy.app.timers.is_registered(_rebuild_timer):
        bpy.app.timers.unregister(_rebuild_timer)
    clear()
    invalidate_plan()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
