"""Assemble a complete MMCP `GenerateRequest` from current Blender state.

This is the integration point: it pulls together everything from the rest of
the addon (capabilities cache, prompt blocks, constraint objects, settings)
and produces the request dict the ``mmcp_client`` POSTs to ``/generate``.
"""

from __future__ import annotations

from typing import Any, Iterable

import bpy
from mathutils import Vector

from . import constraints_ui, coords


PROTOCOL_VERSION = "1.0"

QUALITY_PRESETS = {
    "STANDARD": 50,
    "HALF":     25,
    "QUARTER":  12,
}


class BuildError(Exception):
    """Raised when the current state can't be turned into a valid request."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_frame_range(
    prompt_blocks,
    armature_obj: bpy.types.Object | None,
    scene: bpy.types.Scene,
) -> tuple[int, int]:
    """The generation window — union of every piece of user-authored timing
    content on the timeline.

    Pulls from:
      * Enabled ``PromptBlock`` ranges (the addon's "actions" — text segments
        drawn on the timeline strip).
      * The target armature's active source action keyframe span (skipping
        Proscenium-generated bakes so a regenerate doesn't latch onto its
        own previous output).

    Falls back to ``scene.frame_start..scene.frame_end`` only when nothing's
    been authored. Caller-side, this replaces the older "scene-range is the
    request length" heuristic — generating only as far as the user actually
    drew content keeps short-segment edits from paying for an empty 120-frame
    tail.
    """
    starts: list[int] = []
    ends: list[int] = []

    for b in prompt_blocks or ():
        if not getattr(b, "enabled", True):
            continue
        starts.append(int(b.frame_start))
        ends.append(int(b.frame_end))

    src = (
        armature_obj.animation_data.action
        if armature_obj is not None
        and armature_obj.animation_data
        and armature_obj.animation_data.action
        else None
    )
    if src is not None and not src.name.startswith(_GENERATED_ACTION_PREFIX):
        kfs: set[int] = set()
        for fc in constraints_ui.iter_action_fcurves(src):
            for kp in fc.keyframe_points:
                kfs.add(int(round(kp.co.x)))
        if kfs:
            starts.append(min(kfs))
            ends.append(max(kfs))

    if starts and ends:
        return (min(starts), max(ends))
    return (int(scene.frame_start), int(scene.frame_end))


def build_request(
    *,
    model_id: str,
    model_caps: dict[str, Any],
    armature_obj: bpy.types.Object,
    prompt_blocks: list,
    settings,
    scene: bpy.types.Scene,
    constraint_objects: dict[str, list[bpy.types.Object]],
) -> dict[str, Any]:
    """Build the request dict. Raises ``BuildError`` if state is incomplete."""

    if armature_obj is None or armature_obj.type != 'ARMATURE':
        raise BuildError("Set a target armature first")

    canonical = model_caps.get("canonical_skeleton") or {}
    canonical_joint_names = {j["name"] for j in canonical.get("joints", [])}
    supports_retargeting = bool(model_caps.get("supports_retargeting", False))

    # Build the request's skeleton from the user's armature. When the user
    # has imported the canonical skeleton, this matches it 1:1 and the
    # server skips the retarget hop. When they've picked any other rig, the
    # server's retarget pipeline will map it to canonical.
    request_skeleton = armature_to_skeleton(armature_obj)
    armature_bones = {pb.name for pb in armature_obj.pose.bones}

    if not supports_retargeting:
        # Legacy path for servers that can't retarget.
        if not canonical_joint_names:
            raise BuildError(f"Model {model_id!r} has no canonical_skeleton.joints")
        missing = canonical_joint_names - armature_bones
        if missing:
            raise BuildError(
                f"Armature {armature_obj.name!r} is missing {len(missing)} canonical joint(s) "
                f"(first few: {sorted(missing)[:5]}). "
                f"This server does not support retargeting — pick a rig that "
                f"mirrors the canonical skeleton, or use 'Import canonical skeleton'"
            )
        request_skeleton = canonical                  # echo verbatim

    frame_range = compute_frame_range(prompt_blocks, armature_obj, scene)
    segments = build_segments(prompt_blocks, frame_range)

    # Total timeline length matches the scene range so frames in constraints
    # land where the user authored them.
    total_frames = (
        sum(s["duration_frames"] for s in segments)
        if segments
        else (frame_range[1] - frame_range[0] + 1)
    )
    if total_frames < 1:
        raise BuildError("Scene frame range is empty")

    constraints = _collect_constraints(
        armature_obj=armature_obj,
        constraint_objects=constraint_objects,
        frame_range=frame_range,
        total_frames=total_frames,
    )

    if not segments and not constraints:
        raise BuildError(
            "No prompts or constraints to generate from. "
            "Add a prompt block on the timeline, draw a root path, or pin an effector"
        )

    valid_joint_names = {j["name"] for j in request_skeleton.get("joints", [])}
    _validate_constraint_joints(constraints, valid_joint_names)
    _validate_constraint_count(constraints, model_caps)

    request: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "model":            model_id,
        "skeleton":         request_skeleton,
        "options":          build_options(settings),
    }

    if segments:
        request["segments"] = segments
    else:
        request["duration_frames"] = total_frames

    if constraints:
        request["constraints"] = constraints

    return request


def build_request_for_block(
    *,
    block_index: int,
    model_id: str,
    model_caps: dict[str, Any],
    armature_obj: bpy.types.Object,
    prompt_blocks: list,
    settings,
    scene: bpy.types.Scene,
    constraint_objects: dict[str, list[bpy.types.Object]],
    preview_action: bpy.types.Action,
    source_action: bpy.types.Action | None,
    seed_override: int | None = None,
) -> tuple[dict[str, Any], tuple[int, int]]:
    """Build a tight ``GenerateRequest`` covering just one prompt block.

    Used for "regenerate this block only" — the request scope is the single
    block's frame range and the only TextSegment is its prompt. Continuity
    with neighbouring blocks comes from boundary ``pose_keyframe``
    observations sampled from ``preview_action`` at the frames immediately
    before/after the block. The model treats those as hard pins, so the
    new motion starts and ends on top of whatever the previous bake left
    at the seams.

    User-authored anchors inside the block range are picked up directly
    from the preview action (any ``kp.type != 'GENERATED'`` rotation key
    is treated as a user anchor — Blender stamps that type onto manual
    keyframe inserts, and the original bake also promotes constraint
    frames to it). The ``source_action`` parameter is retained for symmetry
    with the full-generate API and forward-compatibility but isn't read on
    the regen path — see the inline note below the effector-targets loop.

    Returns ``(request_dict, (block_start, block_end))`` — the caller needs
    the range to drive the splice step. Raises ``BuildError`` on invalid
    state.
    """
    del source_action  # see docstring — unused on the per-block regen path
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        raise BuildError("Set a target armature first")
    if preview_action is None:
        raise BuildError("No preview to regenerate from")
    if not (0 <= block_index < len(prompt_blocks or ())):
        raise BuildError("No active prompt block selected")

    target = prompt_blocks[block_index]
    if not getattr(target, "enabled", True):
        raise BuildError("Block is disabled — enable it to regenerate")

    block_start = int(target.frame_start)
    block_end = int(target.frame_end)
    if block_end < block_start:
        raise BuildError("Block has an empty frame range")
    total_frames = block_end - block_start + 1

    canonical = model_caps.get("canonical_skeleton") or {}
    canonical_joint_names = {j["name"] for j in canonical.get("joints", [])}
    supports_retargeting = bool(model_caps.get("supports_retargeting", False))

    request_skeleton = armature_to_skeleton(armature_obj)
    armature_bones = {pb.name for pb in armature_obj.pose.bones}
    if not supports_retargeting:
        if not canonical_joint_names:
            raise BuildError(f"Model {model_id!r} has no canonical_skeleton.joints")
        missing = canonical_joint_names - armature_bones
        if missing:
            raise BuildError(
                f"Armature {armature_obj.name!r} is missing {len(missing)} canonical joint(s) "
                f"(first few: {sorted(missing)[:5]}). "
                f"This server does not support retargeting — pick a rig that "
                f"mirrors the canonical skeleton, or use 'Import canonical skeleton'"
            )
        request_skeleton = canonical

    prompt = (target.prompt or "").strip()
    if prompt:
        segments: list[dict[str, Any]] = [{
            "type":            "text",
            "prompt":          prompt,
            "duration_frames": total_frames,
        }]
    else:
        segments = [{
            "type":            "unconditioned",
            "duration_frames": total_frames,
        }]

    frame_range = (block_start, block_end)

    constraints: list[dict[str, Any]] = []

    # Identify bones the user actually rotated at each user-keyed frame in
    # the block range. Pressing I → "Whole Character" stamps a KEYFRAME on
    # every bone using each bone's current value, so most KEYFRAME-typed
    # kps have values identical to the surrounding GENERATED frames; only
    # bones with a real value delta count as user edits.
    edited_bones_by_frame = _user_edited_bones_per_frame(
        preview_action, block_start, block_end,
    )

    # Two kinds of anchor frames feed the model:
    #
    # 1. Block seams (block_start, block_end) → continuity. Sample the FULL
    #    body pose from the neighbouring frame on the *other side* of the
    #    seam (block_start - 1, block_end + 1). This is what keeps the new
    #    motion from snapping at the join with adjacent blocks. If the user
    #    edited bones AT the seam, override the boundary's joint_rotations
    #    for those bones with the user's values — the seam constraint then
    #    pins continuity for the un-edited body and the user's pose for
    #    the edited bones, in one frame.
    #
    # 2. Interior user-edited frames → user pose. For frames strictly
    #    between block_start and block_end with at least one edited bone,
    #    emit a pose_keyframe constraint pinning ONLY those edited bones.
    #    The rest of the body stays free for the model to interpolate.
    preview_min, preview_max = _preview_frame_extent(preview_action)
    has_left_data  = preview_min is not None and (block_start - 1) >= preview_min
    has_right_data = preview_max is not None and (block_end + 1) <= preview_max

    anchor_frames: set[int] = set(edited_bones_by_frame)
    if has_left_data:
        anchor_frames.add(block_start)
    if has_right_data:
        anchor_frames.add(block_end)

    for f in sorted(anchor_frames):
        edited_at_f = edited_bones_by_frame.get(f, set())

        # Build the "boundary base" pose for this anchor frame, if it's a
        # seam frame with neighbour data on the relevant side. Sampled from
        # the *opposite* side of the seam — block_start anchors continuity
        # from frame block_start - 1, block_end anchors continuity into
        # block_end + 1.
        base_constraint: dict[str, Any] | None = None
        if f == block_start and has_left_data:
            base_constraint = constraints_ui.sample_pose_at_frame(
                armature_obj,
                source_action=preview_action,
                sample_frame=block_start - 1,
                request_frame=0,
            )
        elif f == block_end and has_right_data:
            base_constraint = constraints_ui.sample_pose_at_frame(
                armature_obj,
                source_action=preview_action,
                sample_frame=block_end + 1,
                request_frame=total_frames - 1,
            )

        # Build the "user override" pose for this anchor frame — only the
        # bones the user actually rotated at f.
        user_constraint: dict[str, Any] | None = None
        if edited_at_f:
            user_constraint = constraints_ui.sample_pose_at_frame(
                armature_obj,
                source_action=preview_action,
                sample_frame=f,
                request_frame=f - block_start,
                bone_names=edited_at_f,
            )

        # Merge the two when both exist. Boundary stays the base (full
        # body, request_frame correct); user values overwrite per-bone.
        # Root position: prefer the user's if they posed the root, else
        # keep the boundary's continuity anchor.
        if base_constraint is not None and user_constraint is not None:
            base_constraint["joint_rotations"].update(user_constraint["joint_rotations"])
            if "root_position" in user_constraint:
                base_constraint["root_position"] = user_constraint["root_position"]
            constraints.append(base_constraint)
        elif base_constraint is not None:
            constraints.append(base_constraint)
        elif user_constraint is not None:
            constraints.append(user_constraint)

    # Effector targets get the existing block-range filter automatically.
    # Root paths are deliberately skipped — they describe whole-timeline
    # trajectory and resampling onto a single block would compress the
    # entire curve into the block's duration, which is wrong. Boundary
    # ``pose_keyframe`` constraints already give the model enough start/end
    # position information for the slice.
    for empty in constraint_objects.get("effector_targets", []):
        c = constraints_ui.sample_effector_target(
            empty, frame_range=frame_range, total_frames=total_frames,
        )
        if c is not None:
            constraints.append(c)

    # NOTE: we deliberately do NOT also sample pose_keyframes from the
    # source action here, even though that's what the full-generate path
    # does. Reason: during preview, every source-action anchor lives on
    # the preview action as a 'KEYFRAME'-typed kp (the original bake
    # promoted constraint frames to that type), and the interior-anchor
    # pass above already picks them up. Sampling source separately would
    # double-count those frames; the only case it would *not* duplicate is
    # source keys outside the preview's frame range, which by definition
    # don't apply to a per-block regen anyway.

    valid_joint_names = {j["name"] for j in request_skeleton.get("joints", [])}
    _validate_constraint_joints(constraints, valid_joint_names)
    _validate_constraint_count(constraints, model_caps)

    options = build_options(settings)
    if seed_override is not None:
        # 0 means "let the server pick a random seed" (matches ``build_options``
        # semantics for ``settings.seed``); any positive int is the seed.
        options["seed"] = int(seed_override) if int(seed_override) > 0 else None

    request: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "model":            model_id,
        "skeleton":         request_skeleton,
        "options":          options,
        "segments":         segments,
    }
    if constraints:
        request["constraints"] = constraints

    return request, (block_start, block_end)


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

# Bone-following constraint types: when a deform bone carries one of
# these targeting a sibling bone, that's the universal signal that this
# rig has a control layer driving the deform layer (Mixamo Control Rig,
# Rigify, Auto-Rig Pro, custom). Used only for "is this a control rig?"
# detection — the deform set itself comes from ``bone.use_deform``, the
# flag rigs use to mark which bones skin the mesh.
_BONE_FOLLOWING_CONSTRAINTS = frozenset({
    "COPY_TRANSFORMS",
    "COPY_ROTATION",
    "COPY_LOCATION",
    "IK",
})


def detect_deform_bones(armature_obj: bpy.types.Object) -> set[str]:
    """Return the set of bone names that skin the mesh — i.e. bones with
    ``Bone.use_deform`` set. Every standard rig system (Mixamo, Rigify
    DEF-*, Auto-Rig Pro, custom) flags its deform bones with this; the
    flag is what drives vertex skinning, so it's the canonical "this is
    the actual character skeleton" signal that's stable across naming
    conventions and constraint structures.
    """
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return set()
    return {pb.name for pb in armature_obj.pose.bones if pb.bone.use_deform}


def is_control_rig(armature_obj: bpy.types.Object) -> bool:
    """``True`` when the armature's deform layer is driven from a separate
    control layer via constraints — what people mean by "control rig".

    Plain rigs (just the deform bones, animated directly) return False:
    no constraints means no control layer, so request building runs
    against the bones the user authored without filtering.
    """
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return False
    deform_names = detect_deform_bones(armature_obj)
    if not deform_names:
        return False
    bone_names = {pb.name for pb in armature_obj.pose.bones}
    for pb in armature_obj.pose.bones:
        if pb.name not in deform_names:
            continue
        for c in pb.constraints:
            if getattr(c, "mute", False):
                continue
            if getattr(c, "influence", 1.0) <= 0.0:
                continue
            if c.type not in _BONE_FOLLOWING_CONSTRAINTS:
                continue
            if getattr(c, "target", None) is armature_obj and getattr(c, "subtarget", "") in bone_names:
                return True
    return False


def _closest_deform_ancestor(pb: bpy.types.PoseBone, deform: set[str]):
    """Walk up parents until one is in ``deform``. Returns the PoseBone or
    None for the topmost deform bone in the chain."""
    p = pb.parent
    while p is not None and p.name not in deform:
        p = p.parent
    return p


def armature_to_skeleton(armature_obj: bpy.types.Object) -> dict[str, Any]:
    """Serialize the armature's rest layout to the MMCP `Skeleton` shape.

    Positions are expressed in MMCP frame (Y-up, meters). Heads are converted
    through ``armature.matrix_world`` before differencing, so rigs with a
    non-identity world transform (Mixamo's 90° + 0.01 scale is the common
    case) end up in the same world frame as the per-frame ``root_position``.
    Without this, offsets would live in armature-local frame while root pins
    live in world frame, and the 90° / scale would silently misalign them.

    When the armature has a control-rig setup (deform bones driven by
    constraints), only the deform bones are emitted — the server's bone
    classifier would otherwise see Ctrl_*/IK helpers/pole vectors and pick
    wrong slots. Parent links of deform bones get rewritten to skip over
    any non-deform intermediaries, keeping the rest hierarchy intact.
    """
    mw = armature_obj.matrix_world
    pose_bones = list(armature_obj.pose.bones)
    head_world_by_name: dict[str, Vector] = {
        pb.name: mw @ pb.bone.head_local for pb in pose_bones
    }

    deform = detect_deform_bones(armature_obj)
    use_deform_filter = is_control_rig(armature_obj)

    joints: list[dict[str, Any]] = []
    for pb in pose_bones:
        if use_deform_filter and pb.name not in deform:
            continue
        if use_deform_filter:
            parent = _closest_deform_ancestor(pb, deform)
        else:
            parent = pb.parent
        parent_name = parent.name if parent else None
        if parent is None:
            local = head_world_by_name[pb.name]
        else:
            local = head_world_by_name[pb.name] - head_world_by_name[parent.name]
        mx, my, mz = coords.blender_pos_to_mmcp(local)
        joints.append({
            "name":             pb.name,
            "parent":           parent_name,
            "rest_translation": [float(mx), float(my), float(mz)],
            "rest_rotation":    [0.0, 0.0, 0.0, 1.0],
        })
    return {
        "joints":            joints,
        "coordinate_system": "right_handed_y_up",
        "units":             "meters",
    }


def build_segments(prompt_blocks, frame_range: tuple[int, int]) -> list[dict[str, Any]]:
    """Convert the addon's ``PromptBlock`` collection into MMCP segments.

    Strategy:
      * Span the whole scene range so timeline-relative constraint frames
        line up with the user's authored frame numbers.
      * Each enabled block becomes a TextSegment (or UnconditionedSegment
        if the prompt is empty/whitespace).
      * Gaps before / between / after enabled blocks are filled with
        UnconditionedSegment so the model picks a sensible interpolation.
    """
    enabled: list[tuple[int, int, str]] = []
    for b in prompt_blocks:
        if not getattr(b, "enabled", True):
            continue
        s = max(int(b.frame_start), frame_range[0])
        e = min(int(b.frame_end),   frame_range[1])
        if e < s:
            continue
        enabled.append((s, e, (b.prompt or "").strip()))

    if not enabled:
        return []

    enabled.sort(key=lambda t: t[0])

    # Resolve overlaps by bumping the next block past the previous block's end.
    cleaned: list[tuple[int, int, str]] = []
    prev_end = frame_range[0] - 1
    for s, e, p in enabled:
        s = max(s, prev_end + 1)
        if s > e:
            continue
        cleaned.append((s, e, p))
        prev_end = e
    if not cleaned:
        return []

    segments: list[dict[str, Any]] = []
    cursor = frame_range[0]
    for s, e, prompt in cleaned:
        if s > cursor:
            segments.append({"type": "unconditioned", "duration_frames": s - cursor})
        if prompt:
            segments.append({
                "type":            "text",
                "prompt":          prompt,
                "duration_frames": e - s + 1,
            })
        else:
            # Empty/whitespace prompt promotes to unconditioned (TextSegment
            # would fail server-side validation on min_length=1).
            segments.append({"type": "unconditioned", "duration_frames": e - s + 1})
        cursor = e + 1

    if cursor <= frame_range[1]:
        segments.append({"type": "unconditioned", "duration_frames": frame_range[1] - cursor + 1})
    return segments


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

_GENERATED_ACTION_PREFIXES: tuple[str, ...] = (
    "Proscenium_Motion",     # current motion-bake naming
    "Proscenium_Generated",  # legacy motion-bake naming
)
# Kept for back-compat in case anything imports it; ``str.startswith`` accepts
# either a string or a tuple, so the change is transparent at call sites.
_GENERATED_ACTION_PREFIX = _GENERATED_ACTION_PREFIXES


def _collect_constraints(
    *,
    armature_obj: bpy.types.Object,
    constraint_objects: dict[str, list[bpy.types.Object]],
    frame_range: tuple[int, int],
    total_frames: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    for curve in constraint_objects.get("root_paths", []):
        c = constraints_ui.sample_root_path(curve, total_frames=total_frames)
        if c is not None:
            out.append(c)

    for empty in constraint_objects.get("effector_targets", []):
        c = constraints_ui.sample_effector_target(
            empty, frame_range=frame_range, total_frames=total_frames,
        )
        if c is not None:
            out.append(c)

    # Sample pose keyframes ONLY from a user-authored action, not from a
    # previous generation's bake. Otherwise regenerate ends up feeding the
    # model's own output back as constraints — feedback loop, garbled motion.
    src = (
        armature_obj.animation_data.action
        if armature_obj.animation_data and armature_obj.animation_data.action
        else None
    )
    if src is not None and not src.name.startswith(_GENERATED_ACTION_PREFIX):
        out.extend(
            constraints_ui.sample_pose_keyframes(
                armature_obj,
                source_action=src,
                frame_range=frame_range,
            )
        )

    # Anchor the motion's start to wherever the user placed the character.
    # Without this, the generated motion begins at the model's default root
    # (origin) regardless of the armature's world transform — pose keyframes
    # later in the timeline pull the body toward their targets but frame 0
    # stays stuck at (0, 0), which is what the user saw as "the first
    # keyframe gets reset to (0, 0)". Skip if another constraint already
    # pins frame 0.
    anchor = _start_anchor(
        armature_obj,
        out,
        constraint_objects.get("root_paths", ()),
    )
    if anchor is not None:
        out.append(anchor)

    return out


def _preview_frame_extent(
    action: bpy.types.Action | None,
) -> tuple[int | None, int | None]:
    """Min/max keyed frame across every fcurve on ``action``. Used by the
    per-block regen path to decide which boundary frames can supply a pose
    observation — only those that actually lie inside the existing bake.
    """
    if action is None:
        return None, None
    lo: int | None = None
    hi: int | None = None
    for fc in constraints_ui.iter_action_fcurves(action):
        for kp in fc.keyframe_points:
            f = int(round(kp.co.x))
            if lo is None or f < lo:
                lo = f
            if hi is None or f > hi:
                hi = f
    return lo, hi


# Value-delta epsilon for the "did the user actually rotate this bone, or did
# pressing I → Whole Character just re-stamp the existing pose value with a
# KEYFRAME type?" check. Quaternion components are in [-1, 1]; 5e-3 is a few
# tenths of a degree of rotation per component — well below user perception
# but well above float-roundtrip noise from depsgraph evaluation.
_EDIT_EPSILON = 5e-3


def _user_edited_bones_per_frame(
    action: bpy.types.Action | None,
    frame_start: int,
    frame_end: int,
) -> dict[int, set[str]]:
    """Map each user-keyed frame inside ``[frame_start, frame_end]`` to the
    set of bones the user *actually* rotated at that frame.

    Why this isn't just "bones with non-GENERATED keys at frame f":
    pressing ``I`` in Pose mode with Blender's default "Whole Character" or
    "Available" keying set inserts a KEYFRAME on every rotation channel of
    every bone, *using each bone's current evaluated value*. For bones the
    user didn't touch, that value equals the bake's GENERATED kp at frame
    f, so the new KEYFRAME-typed kp has the same value as the kp it
    replaced. Treating "kp.type != 'GENERATED'" as "the user edited this
    bone" would over-constrain the regen request — every joint would be
    pinned to the previous bake's pose at frame f — and the model would
    fall back to producing motion that doesn't transition to the user's
    deliberate edit.

    Heuristic: a bone counts as edited at frame f when any of its rotation
    quaternion components' value at f differs by more than ``_EDIT_EPSILON``
    from the linear interpolation of the surrounding GENERATED keys on the
    same fcurve. Bones whose value matches the natural interpolation are
    the "incidentally keyed" ones — the user pressed I and the value got
    re-stamped without actually changing.

    For dense bakes (a GENERATED key every frame) the linear interp
    collapses to "is the value at f equal to the value at f-1 / f+1?", which
    is a tight delta check. For sparse anchors (a kp with no surrounding
    GENERATED neighbours), the bone is conservatively flagged as edited —
    we can't disprove the user's intent, so we honour it.
    """
    if action is None:
        return {}

    # First pass: collect every non-GENERATED rotation kp in range, grouped
    # by (bone, frame) so we can evaluate "is this bone edited at frame f"
    # across all 4 quaternion components in one pass.
    candidates: dict[tuple[str, int], list[bpy.types.FCurve]] = {}
    kp_value_at: dict[tuple[str, int, int], float] = {}  # (bone, frame, axis) -> value

    for fc in constraints_ui.iter_action_fcurves(action):
        if "rotation" not in fc.data_path:
            continue
        bone = constraints_ui._bone_name_from_data_path(fc.data_path)
        if bone is None:
            continue
        for kp in fc.keyframe_points:
            if kp.type == 'GENERATED':
                continue
            f = int(round(kp.co.x))
            if not (frame_start <= f <= frame_end):
                continue
            candidates.setdefault((bone, f), []).append(fc)
            kp_value_at[(bone, f, int(fc.array_index))] = float(kp.co.y)

    edited: dict[int, set[str]] = {}
    for (bone, f), fcs in candidates.items():
        # Bone is edited if ANY of its rotation channels at frame f differs
        # from the surrounding-GENERATED interpolation at f.
        bone_edited = False
        for fc in fcs:
            axis = int(fc.array_index)
            v_user = kp_value_at[(bone, f, axis)]
            v_interp = _interp_from_generated(fc, f)
            if v_interp is None:
                # No surrounding GENERATED keys to compare against → trust
                # the user; treat as edited.
                bone_edited = True
                break
            if abs(v_user - v_interp) > _EDIT_EPSILON:
                bone_edited = True
                break
        if bone_edited:
            edited.setdefault(f, set()).add(bone)

    return edited


def _interp_from_generated(fc: bpy.types.FCurve, target_frame: int) -> float | None:
    """Linearly interpolate ``fc``'s GENERATED keyframes at ``target_frame``.

    Returns ``None`` when no GENERATED neighbours exist on either side
    (sparse fcurve, no bake context). Used by ``_user_edited_bones_per_frame``
    to decide whether a user kp's value matches what the bake would have
    produced at that frame.
    """
    left: tuple[float, float] | None = None
    right: tuple[float, float] | None = None
    for kp in fc.keyframe_points:
        if kp.type != 'GENERATED':
            continue
        x = float(kp.co.x)
        if x < target_frame and (left is None or x > left[0]):
            left = (x, float(kp.co.y))
        elif x > target_frame and (right is None or x < right[0]):
            right = (x, float(kp.co.y))
        elif x == target_frame:
            # GENERATED kp sitting on target_frame (rare — usually the
            # user's KEYFRAME replaced it) — that's the most direct
            # answer.
            return float(kp.co.y)
    if left is None or right is None:
        return None
    fl, vl = left
    fr, vr = right
    if fr == fl:
        return vl
    return vl + (target_frame - fl) * (vr - vl) / (fr - fl)


def _start_anchor(
    armature_obj: bpy.types.Object,
    existing: list[dict[str, Any]],
    root_path_curves: Iterable[bpy.types.Object] = (),
) -> dict[str, Any] | None:
    if any(_pins_frame_zero(c) for c in existing):
        return None
    root_pb = next(
        (pb for pb in armature_obj.pose.bones if pb.parent is None),
        None,
    )
    if root_pb is None:
        return None
    root_world = (armature_obj.matrix_world @ root_pb.matrix).translation
    x, _, z = coords.blender_pos_to_mmcp(root_world)
    anchor: dict[str, Any] = {
        "type":         "root_path",
        "frames":       [0],
        "positions_xz": [[x, z]],
    }
    # Match :func:`sample_root_path`: when a path curve opts into follow-
    # direction, pin initial heading from the curve tangent so the model does
    # not default to an arbitrary facing at frame 0.
    for curve in root_path_curves:
        h = constraints_ui.root_path_heading_at_start(curve)
        if h is not None:
            anchor["heading_radians"] = [h]
            break
    return anchor


def _pins_frame_zero(c: dict[str, Any]) -> bool:
    t = c.get("type")
    if t == "root_path":
        return 0 in (c.get("frames") or [])
    if t == "pose_keyframe":
        return c.get("frame") == 0 and c.get("root_position") is not None
    return False


def _validate_constraint_joints(
    constraints: list[dict[str, Any]],
    canonical_joint_names: set[str],
) -> None:
    for i, c in enumerate(constraints):
        if c["type"] == "effector_target":
            joint = c.get("joint", "")
            if joint not in canonical_joint_names:
                raise BuildError(
                    f"Constraint #{i} (effector_target) targets unknown joint {joint!r}. "
                    f"Allowed joints come from /capabilities.models[].canonical_skeleton.joints[].name"
                )
        elif c["type"] == "pose_keyframe":
            unknown = sorted(set(c.get("joint_rotations", {})) - canonical_joint_names)
            if unknown:
                raise BuildError(
                    f"Constraint #{i} (pose_keyframe) references unknown joints: {unknown[:5]}"
                )


def _validate_constraint_count(constraints: list[dict[str, Any]], model_caps: dict[str, Any]) -> None:
    limits = model_caps.get("limits") or {}
    cap = int(limits.get("max_constraints_per_request") or 0)
    if cap and len(constraints) > cap:
        raise BuildError(
            f"{len(constraints)} constraints exceeds the model's max of {cap} "
            f"(disable some keyframes / drop an effector pin)"
        )


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def build_options(settings) -> dict[str, Any]:
    steps = QUALITY_PRESETS.get(settings.quality_preset, int(settings.custom_steps))

    opts: dict[str, Any] = {
        "diffusion_steps":   int(steps),
        "num_samples":       1,                          # multi-sample UI is future work
        "seed":              int(settings.seed) if int(settings.seed) > 0 else None,
        "post_processing":   bool(settings.post_processing),
        "transition_frames": int(settings.num_transition_frames),
    }

    if settings.cfg_enabled:
        opts["guidance"] = {
            "type":   "separated",
            "weight": [float(settings.cfg_text), float(settings.cfg_constraint)],
        }
    return opts
