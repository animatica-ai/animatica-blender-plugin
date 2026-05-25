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


# Face / head-detail joints the server's body retarget can't use. Listed by
# the *first* dotted component of the bare bone name (after stripping a
# leading ``DEF-`` if present). Rigify's metarig and its generated DEF
# chain both use these tokens with `.L`/`.R`/`.NNN` suffixes — e.g.
# ``brow.B.L.002``, ``DEF-lid.T.R.001``. We drop the entire face subtree so
# it doesn't reach the body retargeter — analogous to how
# ``hands_follow_forearm`` strips Mixamo finger subtrees inside the
# retarget library. The bones stay on the rig; we just don't send them.
_FACE_BONE_TOKENS: frozenset[str] = frozenset({
    "brow", "cheek", "chin", "ear", "eye", "face", "forehead",
    "jaw", "lid", "lip", "mouth", "nose", "teeth", "temple",
    "tongue",
})


def _is_face_bone(name: str) -> bool:
    """True if ``name`` looks like a face / head-detail bone — matches the
    head token (first dotted component) against :data:`_FACE_BONE_TOKENS`.
    Strips a leading ``DEF-`` first so Rigify's generated chain matches the
    same way as the bare metarig names.
    """
    bare = name[4:] if name.startswith("DEF-") else name
    head_token = bare.split(".", 1)[0].lower()
    return head_token in _FACE_BONE_TOKENS


# Rigify splits each major limb segment into two halves at generate time —
# ``DEF-upper_arm.L``/``DEF-upper_arm.L.001``, ``DEF-forearm.L``/`.001``,
# ``DEF-thigh.L``/`.001``, ``DEF-shin.L``/`.001``. The ``.001`` halves are
# pure skinning helpers for bendy-bone smoothing — they don't appear on
# the metarig (which has a single bone per segment) and they don't map to
# anything in the canonical SOMA chain (shoulder/elbow/wrist =
# upper_arm/forearm/hand, 3 joints per arm). Sending them doubles the
# joint count on every limb and confuses the bone classifier's
# region/side picks. We drop them at the request edge.
_TWEAK_HALF_SEGMENTS: frozenset[str] = frozenset({
    "upper_arm", "forearm", "thigh", "shin",
})


def _is_tweak_half(name: str) -> bool:
    """True if ``name`` is a Rigify bendy ``.001`` half of a major limb
    segment — e.g. ``DEF-upper_arm.L.001``, ``DEF-shin.R.001``.

    Matches by stripping a leading ``DEF-``, splitting the rest by ``.``,
    and asserting (a) the first component is in
    :data:`_TWEAK_HALF_SEGMENTS` and (b) the final component is exactly
    ``001``. Conservative enough to leave ``DEF-spine.001`` (a real spine
    segment, not a tweak half) alone.
    """
    bare = name[4:] if name.startswith("DEF-") else name
    parts = bare.split(".")
    if len(parts) < 2 or parts[-1] != "001":
        return False
    return parts[0].lower() in _TWEAK_HALF_SEGMENTS


# Arm-chain bones we rewrite to T-pose layout in the outgoing skeleton.
# Rigify's default human metarig has the upper arm at ~28° below
# horizontal — an A-pose that the bone classifier was not trained on and
# that warps how the reverse retarget maps SOMA77 onto the rig. We send
# horizontal arms instead, then pre-multiply the returned rotation keys
# by the bone's actual rest rotation to compensate (see
# :func:`t_pose_correction_quat` in ``gltf_to_blender``).
_T_POSE_ARM_TOKENS: frozenset[str] = frozenset({"upper_arm", "forearm", "hand"})


def is_t_pose_arm_bone(name: str) -> bool:
    """True if ``name`` is an arm-chain bone subject to T-pose rewriting."""
    bare = name[4:] if name.startswith("DEF-") else name
    return bare.split(".", 1)[0].lower() in _T_POSE_ARM_TOKENS


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


def _build_deform_parent_map(
    armature_obj: bpy.types.Object,
    deform: set[str],
) -> dict[str, str | None]:
    """Build a parent map for the deform-only skeleton tree.

    Returns ``{deform_bone_name: parent_deform_bone_name | None}`` with
    exactly one bone mapped to ``None`` (the root).

    Strategy:
      1. **Parent walk** — for each DEF bone, walk up ``pb.parent`` looking
         for another DEF. Handles the common case where DEFs form a clean
         chain in the bone hierarchy (Mixamo, plain deform rigs).
      2. **Spatial fallback** — when the walk yields no DEF ancestor (which
         happens on Rigify because DEFs parent through ``ORG-*``/``MCH-*``
         intermediaries), use rest-pose tail-to-head proximity: each
         orphan's parent is the DEF whose tail is closest to its head.
      3. **Single root** — among bones that still have no parent (orphans
         with no spatial match, or cycle-breakers), pick the one closest
         to the armature's origin as the root and attach the rest to it.

    The server requires exactly one ``parent: null`` joint per the MMCP
    skeleton schema — without the fallback, Rigify rigs emit ~98 roots
    and the request fails schema validation.
    """
    mw = armature_obj.matrix_world
    head_w: dict[str, "Vector"] = {}
    tail_w: dict[str, "Vector"] = {}
    for name in deform:
        pb = armature_obj.pose.bones[name]
        head_w[name] = mw @ pb.bone.head_local
        tail_w[name] = mw @ pb.bone.tail_local

    parent_map: dict[str, str | None] = {}

    # Step 1: parent walk.
    for name in deform:
        pb = armature_obj.pose.bones[name]
        anc = _closest_deform_ancestor(pb, deform)
        if anc is not None:
            parent_map[name] = anc.name

    # Step 2: spatial fallback for orphans, with cycle prevention.
    orphans = [n for n in deform if n not in parent_map]
    if orphans:
        # Per-orphan: closest DEF tail to my head (any DEF, not just other orphans).
        candidates: dict[str, tuple[str | None, float]] = {}
        for name in orphans:
            h = head_w[name]
            best = None
            best_dist = float("inf")
            for other in deform:
                if other == name:
                    continue
                d = (tail_w[other] - h).length
                if d < best_dist:
                    best_dist = d
                    best = other
            candidates[name] = (best, best_dist)

        # Pick the root: the orphan whose closest-tail distance is largest
        # (= most disconnected from any other bone's tail). Tiebreak by
        # distance to armature origin so the pelvis wins over an outlier.
        armature_origin = mw.translation
        root = max(
            orphans,
            key=lambda n: (candidates[n][1], -(head_w[n] - armature_origin).length),
        )
        parent_map[root] = None

        # Assign remaining orphans in order of increasing dist (closest matches
        # first). Cycle prevention: walking up parent_map from the candidate
        # must not reach ``name``. Stopping at an unresolved bone (one not yet
        # in parent_map) is fine — that bone will get its own parent assigned
        # in a later iteration, and the final tree resolves bottom-up.
        for name in sorted([o for o in orphans if o != root], key=lambda n: candidates[n][1]):
            candidate, _dist = candidates[name]
            cur = candidate
            cycle = False
            visited_in_walk: set[str] = set()
            while cur is not None:
                if cur == name:
                    cycle = True
                    break
                if cur in visited_in_walk:
                    # Existing cycle not involving ``name`` — defensive; shouldn't
                    # happen given how we build, but bail out cleanly if it does.
                    break
                visited_in_walk.add(cur)
                cur = parent_map.get(cur)  # None when cur is root or not yet placed
            parent_map[name] = root if cycle else candidate

    # Sanity: exactly one root.
    roots = [n for n in deform if parent_map.get(n) is None]
    assert len(roots) == 1, (
        f"_build_deform_parent_map produced {len(roots)} roots (expected 1): "
        f"{roots[:5]}{'...' if len(roots) > 5 else ''}"
    )

    return parent_map


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
    wrong slots. Parent links of deform bones get rewritten via
    :func:`_build_deform_parent_map` so the result is a single tree (the
    server's schema requires exactly one ``parent: null`` joint).
    """
    mw = armature_obj.matrix_world
    pose_bones = list(armature_obj.pose.bones)
    head_world_by_name: dict[str, Vector] = {
        pb.name: mw @ pb.bone.head_local for pb in pose_bones
    }

    deform = detect_deform_bones(armature_obj)

    # Drop face / head-detail joints — they have no body-retarget equivalent
    # and confuse the bone classifier's hand/foot/head slot picks. Tokens
    # caught by :func:`_is_face_bone`; also sweep any descendants of a bone
    # whose head token IS face (e.g. ``temple.L`` parented under ``face``)
    # so the whole subtree disappears.
    face_drop: set[str] = {n for n in deform if _is_face_bone(n)}
    # Walk descendants of face-bone roots and add them too.
    if face_drop:
        all_pbs = {pb.name: pb for pb in armature_obj.pose.bones}
        for n in list(face_drop):
            stack = [all_pbs[n]] if n in all_pbs else []
            while stack:
                pb = stack.pop()
                for child in pb.children:
                    if child.bone.use_deform and child.name not in face_drop:
                        face_drop.add(child.name)
                    stack.append(child)
    if face_drop:
        deform = deform - face_drop

    # Drop Rigify bendy ``.001`` mid-segments on the four major limb
    # segments — pure skinning helpers that don't exist on the metarig
    # and don't correspond to canonical SOMA joints. Children of a tweak
    # half are real joints (the next segment), so we DON'T walk
    # descendants here — only the tweak bone itself is removed and the
    # deform parent map will re-wire its children up to the parent.
    tweak_drop: set[str] = {n for n in deform if _is_tweak_half(n)}
    if tweak_drop:
        deform = deform - tweak_drop

    use_deform_filter = is_control_rig(armature_obj)
    parent_map = _build_deform_parent_map(armature_obj, deform) if use_deform_filter else None

    # The server requires parents to appear before their children in joints[].
    # Blender's pose-bone iteration order tracks edit-bone parent links, which
    # holds for plain rigs but breaks once ``_build_deform_parent_map`` rewires
    # parents spatially (e.g. ``DEF-forehead.L → DEF-brow.T.L.002`` may flip
    # the natural order). Emit in topological order: root, then each bone
    # whose parent is already emitted.
    if use_deform_filter:
        emit_order = _topological_order(parent_map)
        ordered_bones = [armature_obj.pose.bones[name] for name in emit_order]
    else:
        ordered_bones = pose_bones

    joints: list[dict[str, Any]] = []
    for pb in ordered_bones:
        if use_deform_filter and pb.name not in deform:
            continue
        if use_deform_filter:
            parent_name = parent_map[pb.name]  # may be None for the root
        else:
            parent_name = pb.parent.name if pb.parent else None

        # Force T-pose layout for the arm sub-chain in the *sent* skeleton.
        # Rigify's metarig drapes arms ~28° below horizontal (A-pose), which
        # the bone classifier was not trained on and which biases the
        # reverse retarget. We rewrite ``forearm`` / ``hand`` parent-local
        # offsets to be a pure horizontal extension of the parent's bone
        # length so the server sees flat arms. The actual armature stays
        # in its real A-pose (the bake reads from the live rig, not the
        # request), so this only affects what the classifier + retarget
        # see — the motion still lands on the real rig.
        tposed = False
        if parent_name is not None and is_t_pose_arm_bone(pb.name):
            bare = pb.name[4:] if pb.name.startswith("DEF-") else pb.name
            head_token = bare.split(".", 1)[0].lower()
            if head_token in ("forearm", "hand"):
                parent_pb = armature_obj.pose.bones.get(parent_name)
                if parent_pb is not None:
                    sign = -1.0 if (".R" in bare) else 1.0
                    length = float(parent_pb.bone.length)
                    mx, my, mz = sign * length, 0.0, 0.0
                    tposed = True

        if not tposed:
            if parent_name is None:
                local = head_world_by_name[pb.name]
            else:
                local = head_world_by_name[pb.name] - head_world_by_name[parent_name]
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


def _topological_order(parent_map: dict[str, str | None]) -> list[str]:
    """Return bone names in parent-before-child order given ``parent_map``.

    BFS from the root so siblings emit in the order they were inserted into
    the map (which mirrors the source iteration). Cycles in ``parent_map``
    would leave some nodes unvisited; asserting catches that since the
    server requires a tree.
    """
    children_of: dict[str, list[str]] = {}
    roots: list[str] = []
    for name, parent in parent_map.items():
        if parent is None:
            roots.append(name)
        else:
            children_of.setdefault(parent, []).append(name)

    order: list[str] = []
    queue = list(roots)
    while queue:
        name = queue.pop(0)
        order.append(name)
        queue.extend(children_of.get(name, ()))

    assert len(order) == len(parent_map), (
        f"_topological_order: visited {len(order)} of {len(parent_map)} bones "
        f"— parent_map has a cycle or disconnected node"
    )
    return order


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
