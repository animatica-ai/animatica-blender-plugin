"""Assemble a complete MMCP `GenerateRequest` from current Blender state.

This is the integration point: it pulls together everything from the rest of
the addon (capabilities cache, prompt blocks, constraint objects, settings)
and produces the request dict the ``mmcp_client`` POSTs to ``/generate``.
"""

from __future__ import annotations

import random
from typing import Any, Iterable

import bpy

from . import constraints_ui, coords
# Re-export — these are pure-bpy rig-probing helpers that now live in
# rig_probe.py; kept importable from here until the builder itself moves
# onto the shared core.
from .rig_probe import (  # noqa: F401
    _BONE_FOLLOWING_CONSTRAINTS,
    _EDIT_EPSILON,
    _GENERATED_ACTION_PREFIX,
    _GENERATED_ACTION_PREFIXES,
    _build_deform_parent_map,
    _closest_deform_ancestor,
    _interp_from_generated,
    _is_face_bone,
    _is_tweak_half,
    _preview_frame_extent,
    _topological_order,
    _user_edited_bones_per_frame,
    armature_to_skeleton,
    compute_frame_range,
    detect_deform_bones,
    emitted_deform_bones,
    is_control_rig,
    is_t_pose_arm_bone,
    t_pose_q_matrix,
)


PROTOCOL_VERSION = "1.0"


def _resolve_seed(value) -> int:
    """Turn a seed setting into a concrete value.

    ``0`` ("auto") becomes a fresh random seed; any positive value passes
    through unchanged. Used for client-side seed recording so that even an
    "auto" generation has a known, reproducible seed.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else random.randint(1, 999999)

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

    # Client-side seed recording. Resolve one concrete clip seed (the global
    # Seed, or a single fresh random when it's 0) and stamp the effective seed
    # onto each enabled block's ``last_used_seed`` so the result is inspectable
    # + reproducible. A block keeps its OWN seed only when it set one (>0) and
    # the server supports per-segment seeds; otherwise it inherits the clip
    # seed. (Inheriting — not rolling an independent random per block — is what
    # makes setting the global Seed reproduce the whole clip.) The same concrete
    # values feed the request below, so what we record is what the server runs.
    supports_seg = bool(model_caps.get("supports_segment_seed"))
    resolved_global = _resolve_seed(settings.seed)
    # Record the concrete clip seed so the user can lock it in (panel) and
    # reproduce a run that was launched with Seed = 0 ("auto").
    try:
        settings.last_used_seed = resolved_global
    except (AttributeError, TypeError):
        pass
    for _b in prompt_blocks:
        if not getattr(_b, "enabled", True):
            continue
        try:
            block_seed = int(getattr(_b, "seed", 0) or 0)
            _b.last_used_seed = (
                block_seed if (supports_seg and block_seed > 0) else resolved_global
            )
        except (AttributeError, TypeError, ValueError):
            pass

    frame_range = compute_frame_range(prompt_blocks, armature_obj, scene)
    segments = build_segments(
        prompt_blocks,
        frame_range,
        supports_segment_seed=supports_seg,
    )

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
        "options":          build_options(settings, seed=resolved_global),
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

def build_segments(
    prompt_blocks,
    frame_range: tuple[int, int],
    *,
    supports_segment_seed: bool = False,
) -> list[dict[str, Any]]:
    """Convert the addon's ``PromptBlock`` collection into MMCP segments.

    Strategy:
      * Span the whole scene range so timeline-relative constraint frames
        line up with the user's authored frame numbers.
      * Each enabled block becomes a TextSegment (or UnconditionedSegment
        if the prompt is empty/whitespace).
      * Gaps before / between / after enabled blocks are filled with
        UnconditionedSegment so the model picks a sensible interpolation.

    When ``supports_segment_seed`` is True (the connected model advertises
    ``supports_segment_seed`` in ``/capabilities``), each block's own seed
    (>0) is attached to its segment so a full-timeline Generate can give every
    block a distinct, reproducible seed. Against servers without that
    capability the seed is omitted — older servers reject unknown segment
    fields (``extra="forbid"``) — and the request-level Seed drives the whole
    clip as before. Gap-filling segments never carry a seed.
    """
    # (start, end, prompt, seed). Reads the resolved ``last_used_seed`` (stamped
    # by build_request just before this call) so the segment carries the exact
    # concrete seed we recorded; 0 means "no per-segment seed for this block".
    enabled: list[tuple[int, int, str, int]] = []
    for b in prompt_blocks:
        if not getattr(b, "enabled", True):
            continue
        s = max(int(b.frame_start), frame_range[0])
        e = min(int(b.frame_end),   frame_range[1])
        if e < s:
            continue
        enabled.append((s, e, (b.prompt or "").strip(), int(getattr(b, "last_used_seed", 0) or 0)))

    if not enabled:
        return []

    enabled.sort(key=lambda t: t[0])

    # Resolve overlaps by bumping the next block past the previous block's end.
    cleaned: list[tuple[int, int, str, int]] = []
    prev_end = frame_range[0] - 1
    for s, e, p, seed in enabled:
        s = max(s, prev_end + 1)
        if s > e:
            continue
        cleaned.append((s, e, p, seed))
        prev_end = e
    if not cleaned:
        return []

    def _with_seed(segment: dict[str, Any], seed: int) -> dict[str, Any]:
        # Attach the per-block seed only when the server can read it and the
        # block set a concrete value (0 == "let the server pick").
        if supports_segment_seed and seed > 0:
            segment["seed"] = seed
        return segment

    segments: list[dict[str, Any]] = []
    cursor = frame_range[0]
    for s, e, prompt, seed in cleaned:
        if s > cursor:
            segments.append({"type": "unconditioned", "duration_frames": s - cursor})
        if prompt:
            segments.append(_with_seed({
                "type":            "text",
                "prompt":          prompt,
                "duration_frames": e - s + 1,
            }, seed))
        else:
            # Empty/whitespace prompt promotes to unconditioned (TextSegment
            # would fail server-side validation on min_length=1).
            segments.append(_with_seed(
                {"type": "unconditioned", "duration_frames": e - s + 1}, seed,
            ))
        cursor = e + 1

    if cursor <= frame_range[1]:
        segments.append({"type": "unconditioned", "duration_frames": frame_range[1] - cursor + 1})
    return segments


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

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

def build_options(settings, *, seed: int | None = None) -> dict[str, Any]:
    steps = QUALITY_PRESETS.get(settings.quality_preset, int(settings.custom_steps))

    # ``seed`` (when provided) is the concrete value resolved + recorded by the
    # caller for client-side seed recording. Falling back to the raw setting
    # (0 -> None = server picks) keeps older callers' behaviour unchanged.
    if seed is not None:
        resolved_seed: int | None = int(seed)
    else:
        resolved_seed = int(settings.seed) if int(settings.seed) > 0 else None

    opts: dict[str, Any] = {
        "diffusion_steps":   int(steps),
        "num_samples":       1,                          # multi-sample UI is future work
        "seed":              resolved_seed,
        "post_processing":   bool(settings.post_processing),
        "transition_frames": int(settings.num_transition_frames),
    }

    if settings.cfg_enabled:
        opts["guidance"] = {
            "type":   "separated",
            "weight": [float(settings.cfg_text), float(settings.cfg_constraint)],
        }
    return opts
