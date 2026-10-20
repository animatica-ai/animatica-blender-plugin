"""Assemble a complete MMCP `GenerateRequest` from current Blender state.

This is the integration point: it pulls together everything from the rest of
the addon (capabilities cache, prompt blocks, constraint objects, settings)
and produces the request dict the ``mmcp_client`` POSTs to ``/generate``.
"""

from __future__ import annotations

import random
from typing import Any, Iterable

import bpy
from mathutils import Vector

from . import constraints_ui, coords


PROTOCOL_VERSION = "1.0"
#: MMCP 1.3 added ``ground_height``; a request that carries one says so.
GROUND_PROTOCOL_VERSION = "1.3"


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

def is_locked(block) -> bool:
    """A block set in stone: its motion is the artist's, Generate leaves it."""
    return bool(getattr(block, "locked", False))


def generation_blocks(prompt_blocks, scene=None) -> list:
    """The blocks the next Generate makes.

    All of them until one is locked. After that, one stretch of unlocked
    blocks between locked ones: the stretch under the playhead, else the
    first. Each stretch is its own take, generated into the locked motion
    either side of it, so working through a sequence a block or two at a
    time never touches what was accepted before. Empty when every block is
    locked.
    """
    blocks = list(prompt_blocks or ())
    if not any(is_locked(b) for b in blocks):
        return blocks
    live = sorted((b for b in blocks if getattr(b, "enabled", True)),
                  key=lambda b: int(b.frame_start))
    runs, run = [], []
    for b in live:
        if is_locked(b):
            if run:
                runs.append(run)
            run = []
        else:
            run.append(b)
    if run:
        runs.append(run)
    if not runs:
        return []
    frame = int(getattr(scene, "frame_current", 0)) if scene is not None else None
    if frame is not None:
        for r in runs:
            if int(r[0].frame_start) <= frame <= int(r[-1].frame_end):
                return r
    return runs[0]


def locked_seams(prompt_blocks, lo: int, hi: int, margin: int = 0) -> tuple[bool, bool]:
    """Whether a locked block meets the window ``lo..hi`` on its left, and on
    its right (within ``margin`` frames): there the take starts, or ends, on
    the locked block's pose instead of blending into it."""
    left = right = False
    for b in prompt_blocks or ():
        if not (is_locked(b) and getattr(b, "enabled", True)):
            continue
        if lo - margin <= int(b.frame_end) <= lo:
            left = True
        if hi <= int(b.frame_start) <= hi + margin:
            right = True
    return left, right


#: the start of the blocker that sends the artist to the model menu
MODEL_RIG_MISMATCH = "This model can't animate this character"


def generation_blockers(
    *,
    scene,
    prompt_blocks,
    constraint_objects,
    model_caps: dict[str, Any] | None,
    pose_frames,
    armature_obj=None,
) -> list[str]:
    """Why this generation cannot be sent — in the artist's words, or empty.

    One function so the panel and the request agree: the button greys out for
    exactly the reasons the send would have refused, and neither has to guess
    at the other's rules. Cheap by construction, because the panel calls it on
    every redraw — the caller supplies ``pose_frames`` (from the plan it has
    already computed) rather than having this walk the action again.

    The server rejects all three of these too, but by then the artist has
    waited for a round trip to be told something the addon knew before it
    dialled.
    """
    caps = model_caps or {}
    limits = caps.get("limits") or {}
    out: list[str] = []

    blocks = [b for b in prompt_blocks or () if getattr(b, "enabled", True)]
    if blocks and all(is_locked(b) for b in blocks):
        return ["Every block is locked: unlock one, or add one, to generate"]
    # what the next Generate makes: locked blocks are left as they are
    blocks = [b for b in generation_blocks(blocks, scene) if getattr(b, "enabled", True)]
    texts = [(b.prompt or "").strip() for b in blocks]
    written = [t for t in texts if t]
    has_constraint = bool(pose_frames) or bool(
        (constraint_objects or {}).get("root_paths")
        or (constraint_objects or {}).get("effector_targets")
        or (constraint_objects or {}).get("waypoints")
    )

    # 0. A model that only animates its own skeleton, and a rig that is not
    #    it: nothing else here matters until another model is picked.
    if armature_obj is not None and caps and not caps.get("supports_retargeting", True):
        names = {j.get("name") for j in (caps.get("canonical_skeleton") or {}).get("joints", [])}
        bones = {pb.name for pb in armature_obj.pose.bones}
        if names and not names <= bones:
            return [MODEL_RIG_MISMATCH + f" (it animates its own skeleton, and {armature_obj.name!r} "
                    f"is not that: {len(names - bones)} of its joints are missing). Pick a model that "
                    "retargets, such as Animatica's own"]

    # 1. Nothing to go on. Unconditioned motion between authored poses is a
    #    real request; unconditioned motion between nothing is noise, and the
    #    server has no way to answer it.
    if not written and not has_constraint:
        out.append("Type a prompt first")      # short: the bar's button shows it whole

    # 2. Longer than the model can make in one go. Seconds are the model's
    #    own, since the server converts frames with the model's fps.
    fps = float(caps.get("fps") or scene.render.fps or 24)
    max_seconds = limits.get("max_duration_seconds")
    if max_seconds and fps > 0:
        lo, hi = compute_frame_range(prompt_blocks, armature_obj, scene)
        seconds = (hi - lo + 1) / fps
        if seconds > float(max_seconds) + 1e-6:
            out.append(f"Clip is {seconds:.0f}s — this model tops out at "
                       f"{float(max_seconds):.0f}s")

    # 3. A prompt the server will refuse to read.
    max_prompt = int(limits.get("max_prompt_length") or 0)
    if max_prompt:
        for i, text in enumerate(texts, start=1):
            if len(text) > max_prompt:
                out.append(f"Prompt {i} is {len(text)} characters — the limit "
                           f"is {max_prompt}")
                break

    return out


def compute_frame_range(
    prompt_blocks,
    armature_obj: bpy.types.Object | None,
    scene: bpy.types.Scene,
    *,
    transition_frames: int | None = None,
) -> tuple[int, int]:
    """The generation window.

    **Prompt blocks win outright.** When the user has drawn any enabled block,
    the window is that span and nothing else — the blocks *are* the statement
    of which stretch of the timeline they are generating. It is widened by
    ``transition_frames`` only on a side where the user's action (see
    :func:`blend_action`) has keys beyond the blocks, so the server has
    material there to blend the splice into; a side with nothing to blend
    into stays exactly at the block's edge.

    This used to be a union with the source action's whole keyframe span,
    which quietly defeated splicing: blocking out poses across 200 frames and
    then asking for one 40-frame block generated all 200. The request paid for
    ~160 frames of ``unconditioned`` segment, every authored keyframe outside
    the block was sent as a pose constraint, and the bake overwrote the work
    the user had deliberately left alone.

    With no blocks there is no such statement, so the old behaviour stands:
    the union of the armature's source-action keyframe span (skipping
    Animatica bakes, so a regenerate does not latch onto its own output),
    falling back to the scene range when nothing has been authored at all.

    ``transition_frames`` defaults to the scene's own setting; pass it
    explicitly only to ask "what would this be if…".
    """
    if transition_frames is None:
        settings = getattr(scene, "animatica", None)
        transition_frames = int(getattr(settings, "num_transition_frames", 0) or 0)
    margin = max(0, int(transition_frames))

    # A block with no text is a gap with a rectangle drawn round it: the
    # segment builder turns both into the same ``unconditioned`` stretch. So
    # when anything has been typed, the typed blocks alone are the statement
    # of what to generate. This matters because setting a target armature
    # seeds an empty block across the whole scene — left in the reckoning, it
    # widened every generation to the entire timeline and swept in keyframes
    # far outside the stretch the artist had actually asked about.
    # Locked blocks are not generated (see generation_blocks).
    all_blocks = prompt_blocks
    prompt_blocks = generation_blocks(prompt_blocks, scene)
    speaking = [b for b in prompt_blocks or ()
                if getattr(b, "enabled", True) and (b.prompt or "").strip()]
    considered = speaking or [b for b in prompt_blocks or ()
                              if getattr(b, "enabled", True)]
    block_starts = [int(b.frame_start) for b in considered]
    block_ends = [int(b.frame_end) for b in considered]
    if block_starts and block_ends:
        lo, hi = min(block_starts), max(block_ends)
        # A loop is the block, exactly: its length is the cycle's length, and
        # there is nothing either side to blend into.
        settings = getattr(scene, "animatica", None)
        if getattr(settings, "loop", False) and len(considered) == 1:
            return (lo, hi)
        # Widen by the blend margin only on a side where there is animation
        # to blend into: the user's keys beyond that edge, which the take is
        # spliced against. With nothing there the margin was only frames
        # nobody asked for -- five after the last block, up to five before
        # the first. Never past the scene the user set up, unless a block
        # already reaches beyond it (the block is the authority then).
        before, after = _keys_beyond(blend_action(armature_obj, scene), lo, hi)
        # Not into a locked block: the take starts (ends) on its pose there.
        held_left, held_right = locked_seams(all_blocks, lo, hi, margin)
        before, after = before and not held_left, after and not held_right
        return (
            max(lo - margin, min(lo, int(scene.frame_start))) if before else lo,
            min(hi + margin, max(hi, int(scene.frame_end))) if after else hi,
        )

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
            return (min(kfs), max(kfs))

    return (int(scene.frame_start), int(scene.frame_end))


def blend_action(armature_obj, scene):
    """The animation a take over the blocks blends into: the rig's own action.

    Not an Animatica bake. While a take is showing, the rig plays the preview
    (which carries the user's keys in, but is not theirs); the user's action is
    then the one the preview session keeps, or the one Generate stashed.
    """
    ad = getattr(armature_obj, "animation_data", None) if armature_obj is not None else None
    act = ad.action if ad is not None else None
    if act is not None and not act.name.startswith(_GENERATED_ACTION_PREFIXES):
        return act
    if act is None:
        return None
    src = None
    try:
        from . import preview_session
        if preview_session.get(armature_obj) is not None:
            src = preview_session.source_of(armature_obj)
    except Exception:                        # noqa: BLE001 -- best effort only
        src = None
    if src is None:
        name = getattr(getattr(scene, "animatica", None), "source_action_name", "") or ""
        src = bpy.data.actions.get(name) if name else None
    if src is not None and not src.name.startswith(_GENERATED_ACTION_PREFIXES):
        return src
    return None


def _keys_beyond(action, lo: int, hi: int) -> tuple[bool, bool]:
    """Whether *action* has keys before *lo*, and after *hi*."""
    before = after = False
    if action is None:
        return before, after
    for fc in constraints_ui.iter_action_fcurves(action):
        for kp in fc.keyframe_points:
            f = int(round(kp.co.x))
            before = before or f < lo
            after = after or f > hi
            if before and after:
                return before, after
    return before, after


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
    for _b in generation_blocks(prompt_blocks, scene):
        if not getattr(_b, "enabled", True):
            continue
        try:
            block_seed = int(getattr(_b, "seed", 0) or 0)
            _b.last_used_seed = (
                block_seed if (supports_seg and block_seed > 0) else resolved_global
            )
        except (AttributeError, TypeError, ValueError):
            pass

    # Refuse here rather than at the far end: these three are knowable
    # before dialling, and the panel has already greyed the button for the
    # same reasons (see generation_blockers).
    source_for_poses = (
        armature_obj.animation_data.action
        if armature_obj.animation_data and armature_obj.animation_data.action
        else None
    )
    authored, _keyed = constraints_ui.authored_pose_frames(source_for_poses)
    blockers = generation_blockers(
        scene=scene,
        prompt_blocks=prompt_blocks,
        constraint_objects=constraint_objects,
        model_caps=model_caps,
        pose_frames=authored,
        armature_obj=armature_obj,
    )
    if blockers:
        raise BuildError(blockers[0])

    frame_range = compute_frame_range(prompt_blocks, armature_obj, scene)
    seams = locked_seams(prompt_blocks, *frame_range)
    segments = build_segments(
        generation_blocks(prompt_blocks, scene),
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
        locked_seams=seams,
    )

    if not segments and not constraints:
        raise BuildError(
            "No prompts or constraints to generate from. "
            "Add a prompt block on the timeline, draw a root path, or pin an effector"
        )

    _add_ground(constraints, model_caps=model_caps, scene=scene, armature_obj=armature_obj,
                start_frame=frame_range[0])

    # Loop: sampled as a cycle by the model (motionmcp 0.4 ``options.loop``),
    # which needs one segment. Loop is only offered where the model advertises
    # it; a request it cannot loop goes out without it.
    # Not into a gap between the user's keys: that take is spliced into their
    # action, which is not made a cycle (see operators.bake_take), so a cycle
    # asked of the model would only be thrown away.
    loop = bool(getattr(settings, "loop", False) and model_caps.get("supports_loop")
                and len(segments) == 1 and not will_splice(armature_obj, frame_range))

    # A loop whose only constraint is the start anchor goes out without it.
    # The server settles the cycle as a whole, then pins the hips to the
    # anchor on frame 0 alone, and the last frame no longer meets the first:
    # a jolt at every wrap. Without the anchor frame 0 sits at the origin and
    # the operator moves the whole take onto the character (see places_start).
    # Anything else in the request is in world space, the ground included,
    # and keeps the anchor; so does a control rig.
    if loop and not is_control_rig(armature_obj) and constraints == [
            _start_anchor(armature_obj, [], constraint_objects.get("root_paths", ()))]:
        constraints = []

    valid_joint_names = {j["name"] for j in request_skeleton.get("joints", [])}
    _validate_constraint_joints(constraints, valid_joint_names)
    _validate_constraint_count(constraints, model_caps)

    request: dict[str, Any] = {
        "protocol_version": _protocol_version(constraints),
        "model":            model_id,
        "skeleton":         request_skeleton,
        "options":          build_options(settings, seed=resolved_global),
    }

    if segments:
        request["segments"] = segments
    else:
        request["duration_frames"] = total_frames

    if loop:
        request["options"]["loop"] = True

    # Variations: several samples of the one take, as far as the model allows.
    wanted = int(getattr(settings, "variations", 1) or 1)
    if wanted > 1:
        most = int(((model_caps.get("limits") or {}).get("max_num_samples")) or 1)
        request["options"]["num_samples"] = max(1, min(wanted, most))

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
    # Frames the artist kept from the take: held whole, though their values
    # are the take's own (which is why the edit test above passes them over).
    from .pose_edit import kept_frames
    for f in kept_frames(armature_obj):
        if block_start < f < block_end:
            edited_bones_by_frame[f] = {"*"}

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
                bone_names=None if "*" in edited_at_f else edited_at_f,
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

    # Waypoints inside the block go out as on a full Generate: each is one
    # frame, so it lands on the block's own frames (offset by its start), and
    # Redo keeps the route. Left out, the block was made again with no idea
    # where the character had to be.
    if constraint_objects.get("waypoints"):
        from . import waypoints

        constraints.extend(waypoints.request_constraints(
            scene, frame_range,
            face_along_path=bool(getattr(settings, "waypoint_heading", False)),
            marks=constraint_objects["waypoints"],
        ))

    # Effector targets get the existing block-range filter automatically.
    # Legacy root-path curves are deliberately skipped — they describe whole-timeline
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

    _add_ground(constraints, model_caps=model_caps, scene=scene, armature_obj=armature_obj,
                start_frame=block_start)

    valid_joint_names = {j["name"] for j in request_skeleton.get("joints", [])}
    _validate_constraint_joints(constraints, valid_joint_names)
    _validate_constraint_count(constraints, model_caps)

    options = build_options(settings)
    if seed_override is not None:
        # 0 means "let the server pick a random seed" (matches ``build_options``
        # semantics for ``settings.seed``); any positive int is the seed.
        options["seed"] = int(seed_override) if int(seed_override) > 0 else None

    request: dict[str, Any] = {
        "protocol_version": _protocol_version(constraints),
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


def is_auto_rig_pro(armature_obj: bpy.types.Object) -> bool:
    """True for a rig Auto-Rig Pro generated. It marks the object (``arp_rig_type``) and the
    armature (``arp_updated``); it also sets ``rig_id``, so that alone does not tell it from
    Rigify."""
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return False
    return "arp_rig_type" in armature_obj.keys() or "arp_updated" in armature_obj.data.keys()


# Auto-Rig Pro splits each limb segment into two deform halves at its twist: the upper arm into
# ``c_arm_twist_offset`` and ``arm_stretch``, the forearm into ``forearm_stretch`` and
# ``forearm_twist``, the thigh into ``thigh_twist`` and ``thigh_stretch``, the shin into
# ``leg_stretch`` and ``leg_twist``. The second half of each starts halfway down the segment, so
# sent as joints they put an extra joint mid-limb, and the server's HumanIK read the arm a joint
# down -- shoulder at the elbow, elbow mid-forearm -- as Rigify's ``.001`` halves would. They are
# not sent. They still join the tree up (a hand starts where ``forearm_twist`` ends), so the tree
# is built through them and they are then left out (`_arp_bridges`).
_ARP_SECOND_HALVES: frozenset[str] = frozenset({"arm_stretch", "forearm_twist", "thigh_stretch", "leg_twist"})


def _arp_bridges(armature_obj: bpy.types.Object, deform: set[str]) -> set[str]:
    """An Auto-Rig Pro rig's second limb halves among ``deform`` (``arm_stretch.l``...)."""
    if not is_auto_rig_pro(armature_obj):
        return set()
    return {n for n in deform if n.rsplit(".", 1)[0] in _ARP_SECOND_HALVES}


def emitted_deform_bones(armature_obj: bpy.types.Object) -> set[str]:
    """Return the subset of deform bones that :func:`armature_to_skeleton`
    actually serializes — i.e. ``detect_deform_bones`` minus the face
    sub-tree and minus the Rigify bendy ``.001`` tweak halves.

    The pose-keyframe sampler and any other code that needs to know
    "which bones does the server see in this request" should call this
    so it stays in sync with what we emit.
    """
    deform = detect_deform_bones(armature_obj)
    if not deform:
        return deform

    face_drop = {n for n in deform if _is_face_bone(n)}
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
    deform = deform - face_drop
    deform = deform - {n for n in deform if _is_tweak_half(n)}
    deform = deform - _arp_bridges(armature_obj, deform)
    return deform


def will_splice(armature_obj, frame_range) -> bool:
    """True when a generation over ``frame_range`` goes into the rig's own
    action rather than a new take: it holds keys either side of the window
    (the test ``operators.bake_take`` makes)."""
    from .constraints_ui import iter_action_fcurves
    ad = getattr(armature_obj, "animation_data", None)
    action = ad.action if ad is not None else None
    if action is None or is_control_rig(armature_obj):
        return False
    lo, hi = int(frame_range[0]), int(frame_range[1])
    return any(
        not (lo <= int(round(kp.co.x)) <= hi)
        for fc in iter_action_fcurves(action) for kp in fc.keyframe_points
    )


def _same_block(a, b) -> bool:
    """Whether two handles are one block. Every read of a Blender collection
    item hands back a fresh wrapper, so ``is`` alone misses; the pointer is
    the identity there, and plain stand-ins fall back to ``is``/``==``."""
    if a is b:
        return True
    ptr = getattr(a, "as_pointer", None)
    if ptr is not None and hasattr(b, "as_pointer"):
        return ptr() == b.as_pointer()
    return a == b


def redo_makes_loop(settings, prompt_blocks, scene, model_caps, block_index) -> bool:
    """True when redoing block ``block_index`` is redoing the loop take.

    With Loop on, the take is the one block that counts (see
    :func:`compute_frame_range`), so "redo this block" and "redo the take" are
    the same thing: the redo has to ask for a loop, not a plain segment. Needs
    the model to loop at all, and the block to be that one considered block —
    the same reckoning ``compute_frame_range`` uses, so the two never disagree
    about which block the take is.
    """
    if not getattr(settings, "loop", False):
        return False
    if not (model_caps or {}).get("supports_loop"):
        return False
    blocks = list(prompt_blocks or ())
    if not 0 <= block_index < len(blocks):
        return False
    target = blocks[block_index]
    live = [b for b in generation_blocks(prompt_blocks, scene)
            if getattr(b, "enabled", True)]
    considered = [b for b in live if (b.prompt or "").strip()] or live
    return len(considered) == 1 and _same_block(considered[0], target)


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
    *,
    skin_from: bpy.types.Object | None = None,
) -> dict[str, str | None]:
    """Build a parent map for the deform-only skeleton tree.

    Returns ``{deform_bone_name: parent_deform_bone_name | None}`` with
    exactly one bone mapped to ``None`` (the root).

    Strategy:
      1. **Parent walk** — for each DEF bone, walk up ``pb.parent`` looking
         for another DEF. Handles the common case where DEFs form a clean
         chain in the bone hierarchy (Mixamo, plain deform rigs).
      1b. **Rigify's ORG twins** — a Rigify rig keeps the metarig's own tree on
         its ``ORG-`` bones, one per ``DEF-`` bone, under the same name: walk up
         the ``ORG-`` twin and take the first ancestor whose ``DEF-`` twin goes
         out. Guessed spatially instead, a Rigify rig came out rooted at a thigh
         with the spine under it, and the server took it for no biped at all.
      1c. **The joints** — a bone that starts exactly where another deform bone
         ends, and far from where its walk parent ends, hangs off that bone
         (`_connect_joined_bones`): Auto-Rig Pro hangs its limbs off the rig's
         root and the IK/FK mechanism, and the walk put its hands on the chest.
      1d. **Shared starts** — trees whose tops start at one point join under
         the one reaching lowest (`_join_shared_starts`): Auto-Rig Pro's pelvis
         and spine both start at the base of the spine.
      1e. **The skin** — where the walk leaves several trees, a tree that
         moves none of the character's mesh is a helper (a game rip's
         attachment bones, still deforming under a ``root_motion`` bone that
         is not) and stays out; a tree that moves most of it is the body, and
         its top is the root. A stock Rigify rig comes out of the walk as one
         tree, so this never touches it. The skin is ``skin_from``'s, when
         given: the bake builds this map on a copy of the rig, which deforms
         no mesh of its own.
      2. **Spatial fallback** — when the walk yields no DEF ancestor (which
         happens on Rigify because DEFs parent through ``ORG-*``/``MCH-*``
         intermediaries), use rest-pose tail-to-head proximity: each
         orphan's parent is the DEF whose tail is closest to its head.
      3. **Single root** — the body's top from 1e; failing that, among bones
         that still have no parent (orphans with no spatial match, or
         cycle-breakers), the one most apart from the others, then closest
         to the armature's origin. The rest attach under it.

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

    # Step 1b: Rigify's ORG twins carry the metarig's tree.
    bones = armature_obj.data.bones
    for name in deform:
        if name in parent_map or not name.startswith("DEF-"):
            continue
        org = bones.get("ORG-" + name[4:])
        a = org.parent if org is not None else None
        while a is not None:
            if a.name.startswith("ORG-") and ("DEF-" + a.name[4:]) in deform \
                    and ("DEF-" + a.name[4:]) != name:
                parent_map[name] = "DEF-" + a.name[4:]
                break
            a = a.parent

    # Step 1c: a bone that starts exactly where another ends hangs off that one.
    _connect_joined_bones(deform, parent_map, head_w, tail_w)

    # Step 1d: bones that start together, neither one hanging off a joint.
    _join_shared_starts(deform, parent_map, head_w)

    # Step 1e: what the skin says, where the walk left several trees.
    body_root = None
    orphans = [n for n in deform if n not in parent_map]
    if len(orphans) > 1:
        skin = _skin_weights(skin_from or armature_obj)
        top = {n: _walk_to_top(n, parent_map) for n in deform}
        weight: dict[str, float] = {}
        for n, t in top.items():
            weight[t] = weight.get(t, 0.0) + skin.get(n, 0.0)
        total = sum(weight.values())
        if total > 0:
            deform = {n for n in deform if weight[top[n]] > 0}
            parent_map = {n: p for n, p in parent_map.items() if n in deform}
            heaviest = max((o for o in orphans if o in deform), key=weight.get)
            if weight[heaviest] > total / 2:
                body_root = heaviest

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
        root = body_root or max(
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


#: How close (a fraction of the bone's length) a bone's head must sit to another's tail to count
#: as joined to it. Joined bones share the point exactly; 1% leaves room for float noise only.
_JOINED = 0.01


def _hangs_under(name: str, other: str, parent_map: dict[str, str | None]) -> bool:
    """True when ``other`` already hangs (somewhere) under ``name``: parenting ``name`` to it
    would close a loop."""
    seen: set[str] = set()
    cur = other
    while cur is not None and cur not in seen:
        if cur == name:
            return True
        seen.add(cur)
        cur = parent_map.get(cur)
    return False


def _connect_joined_bones(deform: set[str], parent_map: dict[str, str | None],
                          head_w: dict, tail_w: dict) -> None:
    """Step 1c of :func:`_build_deform_parent_map`, in place: the joints win over the walk.

    A control rig whose deform bones are driven by constraints keeps them out of a deform
    hierarchy: Auto-Rig Pro parents its arm and leg ``_stretch`` bones to the rig's root and its
    hand and foot to the IK/FK mechanism, so the walk up the hierarchy skipped whole limbs -- the
    hand came out under the chest, the foot under the pelvis, the upper arm on its own -- and the
    server could not read the rig as a body. The rest pose still says how the body goes together:
    each of those bones starts exactly where the one before it ends. So where the walk's parent
    ends far from where a bone starts -- further than either bone is long: the walk skipped
    something -- and another deform bone ends exactly there, the bone hangs off that one. A walk
    parent near the bone is kept (Rigify's fingers start a little past the hand, at the ends of
    their palm bones, and stay on the hand), and a bone joined to nothing keeps whatever the walk
    found (a shoulder starts beside the chest, not at its end)."""
    for name in sorted(deform):
        h = head_w[name]
        length = max((tail_w[name] - h).length, 1e-6)
        tol = _JOINED * length
        cur = parent_map.get(name)
        if cur is not None and (tail_w[cur] - h).length <= max(length, (tail_w[cur] - head_w[cur]).length):
            continue
        best, best_d = None, tol
        for other in deform:
            if other == name:
                continue
            d = (tail_w[other] - h).length
            if d <= best_d and not _hangs_under(name, other, parent_map):
                best, best_d = other, d
        if best is not None:
            parent_map[name] = best


def _join_shared_starts(deform: set[str], parent_map: dict[str, str | None], head_w: dict) -> None:
    """Step 1d of :func:`_build_deform_parent_map`, in place: tops of trees that start at one point.

    Auto-Rig Pro's pelvis (``root.x``) points down from the base of the spine, and its first
    spine bone starts at the same point, both under controls only: two trees, neither joined to
    the other, and the spatial fallback hung the pelvis off the spine's END, rooting the body at
    the spine. Where trees' tops start together, the one the legs hang from -- the one reaching
    lowest -- is the hips, and the others hang off it."""
    tops = [n for n in sorted(deform) if parent_map.get(n) is None]
    if len(tops) < 2:
        return
    lowest: dict[str, float] = {}
    for n in deform:
        t = _walk_to_top(n, parent_map)
        lowest[t] = min(lowest.get(t, float("inf")), head_w[n].z)
    placed: set[str] = set()
    for a in tops:
        if a in placed:
            continue
        group = [b for b in tops if b not in placed
                 and (head_w[b] - head_w[a]).length <= 1e-4 + 1e-3 * max(head_w[a].length, 1.0)]
        if len(group) < 2:
            continue
        hips = min(group, key=lambda n: (lowest.get(n, head_w[n].z), n))
        for b in group:
            if b != hips:
                parent_map[b] = hips
            placed.add(b)


def _walk_to_top(name: str, parent_map: dict[str, str | None]) -> str:
    """The top of ``name``'s tree in a partial ``parent_map``."""
    seen: set[str] = set()
    while name in parent_map and parent_map[name] is not None and name not in seen:
        seen.add(name)
        name = parent_map[name]
    return name


def request_tree(armature_obj: bpy.types.Object) -> dict[str, str | None]:
    """The skeleton the request carries: each joint with its parent (``None``
    for the root), parents before children.

    One answer for every rig. `request_joint_set`, `armature_to_skeleton` and
    `main_root` read it, so the skeleton, the pose keys, the pins and the root
    cannot disagree about what a joint is or where the root is:

    * a control rig, or a rig where some bones do not deform, sends its deform
      bones (less the face and Rigify's tweak halves), joined into one tree by
      `_build_deform_parent_map`;
    * any other rig sends its bones as they hang, from the body's root bone
      (`_body_bones`).

    Either way there is one root, the first thing the server's schema checks:
    a rig with several root bones used to go out with all of them, and came
    back as "Malformed MMCP generate request: 1 problem(s)".
    """
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return {}
    deform = emitted_deform_bones(armature_obj)
    if _filters_to_deform(armature_obj, deform):
        # Auto-Rig Pro's second limb halves join the tree up but are not sent: whatever hangs
        # off one hangs off its parent instead
        bridges = _arp_bridges(armature_obj, detect_deform_bones(armature_obj)) - deform
        full = _build_deform_parent_map(armature_obj, deform | bridges)
        parents = {}
        for n, p in full.items():                       # the body's tree: a loose piece is not in it
            if n in bridges:
                continue
            while p in bridges:
                p = full.get(p)
            parents[n] = p
        return {n: parents[n] for n in _topological_order(parents)}
    body = _body_bones(armature_obj)
    return {pb.name: (pb.parent.name if pb.parent else None)
            for pb in armature_obj.pose.bones if pb.name in body}


def request_joint_set(armature_obj: bpy.types.Object) -> set[str]:
    """The joints the request will carry, and the only names a constraint may use.

    One answer for both halves of the request. ``armature_to_skeleton`` emits
    these, and every constraint's ``joint_rotations`` has to be a subset of
    them — name a joint the skeleton does not have and the server rejects the
    request.

    They drifted apart twice, and both times the symptom was a malformed
    request as soon as a control rig existed: the skeleton filtered to deform
    bones only when the rig's deform bones were driven by *constraints*, and
    the boundary sampler took every pose bone unless the same test passed. A
    rig whose controls drive the body another way — the Autoposer writes the
    pose directly — satisfied neither, and its handles went out as joints.

    A bone outside the deform set is a helper whatever moves it. Where every
    bone deforms, this is every bone of the body (see `request_tree`).
    """
    return set(request_tree(armature_obj))


def _filters_to_deform(armature_obj: bpy.types.Object, deform: set[str]) -> bool:
    """Whether the request carries the deform bones only: on a control rig, or
    where some bones do not deform (helpers, or a face we leave out)."""
    return bool(deform) and (
        is_control_rig(armature_obj) or len(deform) < len(armature_obj.pose.bones))


def _body_bones(armature_obj: bpy.types.Object) -> set[str]:
    """A plain rig's bones that go out: all of them, or of a rig with several
    root bones, the tree that is the body.

    A game rip has the body under one bone and others beside it at the top: a
    ``root_motion`` bone with the attachments and IK targets under it, a
    weapon, cloth. The body is the tree that moves the character's mesh, not
    the one with most bones: attachments and IK targets can outnumber it, and
    move no skin. Without a skinned mesh, the deepest tree (a body's chains
    run from the hips to the fingertips), then the one with most bones, then
    the one nearer the rig's origin. The trees beside it stay as they are,
    like a control rig's helpers; hung under the body instead, they would be
    read and written against a parent they do not have in Blender.

    Where no one tree moves most of the mesh, the body itself is in pieces and
    no single tree of it is a character: the request is refused, naming the
    roots to join.
    """
    bones = armature_obj.data.bones
    roots = [b for b in bones if b.parent is None]
    if len(roots) <= 1:
        return {b.name for b in bones}
    trees = {r.name: [r, *r.children_recursive] for r in roots}
    skin = _skin_weights(armature_obj)
    weight = {r.name: sum(skin.get(b.name, 0.0) for b in trees[r.name]) for r in roots}
    body = max(roots, key=lambda r: (weight[r.name], _depth(r), len(trees[r.name]),
                                     -r.head_local.length))
    total = sum(weight.values())
    if total > 0 and weight[body.name] <= total / 2:
        parts = sorted((n for n in weight if weight[n] > 0), key=weight.get, reverse=True)
        raise BuildError(
            f"{armature_obj.name!r} has its body under {len(parts)} separate root bones "
            f"({', '.join(parts[:4])}{', ...' if len(parts) > 4 else ''}). Join them into one "
            "hierarchy (in Edit Mode, select them, then the bone that should hold them, and "
            "Ctrl+P → Keep Offset), then generate again")
    return {b.name for b in trees[body.name]}


def _depth(bone: bpy.types.Bone) -> int:
    """Bones in the longest chain from ``bone`` down."""
    deepest, stack = 0, [(bone, 1)]
    while stack:
        b, d = stack.pop()
        deepest = max(deepest, d)
        stack.extend((c, d + 1) for c in b.children)
    return deepest


def main_root(armature_obj: bpy.types.Object) -> bpy.types.PoseBone | None:
    """The bone whose place says where the character stands, for the start
    anchor and the ground: the rig's first root bone, as it always was, but on
    a plain rig with several, the body's (`_body_bones`), not a weapon or a
    ``root_motion`` bone that happens to come first."""
    roots = [pb for pb in armature_obj.pose.bones if pb.parent is None]
    if len(roots) > 1 and not _filters_to_deform(armature_obj, emitted_deform_bones(armature_obj)):
        body = _body_bones(armature_obj)
        return next(pb for pb in roots if pb.name in body)
    return roots[0] if roots else None


def left_behind(armature_obj: bpy.types.Object) -> list[str]:
    """Bones that move part of the mesh but hang beside the body, so a take
    leaves them where they are: a weapon or a cloth strand on a root bone of
    its own. Only a plain rig with several root bones has any."""
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return []
    if sum(1 for b in armature_obj.data.bones if b.parent is None) < 2:
        return []
    if _filters_to_deform(armature_obj, emitted_deform_bones(armature_obj)):
        return []
    sent = _body_bones(armature_obj)
    bones = armature_obj.data.bones
    return sorted(n for n, w in _skin_weights(armature_obj).items()
                  if w > 0 and n not in sent and n in bones)


#: Vertices read per mesh when weighing which bones move the skin: plenty to
#: tell a body from attachment bones, and a dense mesh stays quick.
_SKIN_SAMPLE = 20000


def _skin_weights(armature_obj: bpy.types.Object) -> dict[str, float]:
    """Each bone's vertex weights, summed over the meshes this armature deforms."""
    totals: dict[str, float] = {}
    for ob in bpy.data.objects:
        if ob.type != 'MESH' or not any(
                m.type == 'ARMATURE' and m.object == armature_obj for m in ob.modifiers):
            continue
        names = {g.index: g.name for g in ob.vertex_groups}
        verts = ob.data.vertices
        for i in range(0, len(verts), max(1, len(verts) // _SKIN_SAMPLE)):
            for g in verts[i].groups:
                name = names.get(g.group)
                if name is not None:
                    totals[name] = totals.get(name, 0.0) + g.weight
    return totals


def armature_to_skeleton(armature_obj: bpy.types.Object) -> dict[str, Any]:
    """Serialize the armature's rest layout to the MMCP `Skeleton` shape.

    Positions are expressed in MMCP frame (Y-up, meters). Heads are converted
    through ``armature.matrix_world`` before differencing, so rigs with a
    non-identity world transform (Mixamo's 90° + 0.01 scale is the common
    case) end up in the same world frame as the per-frame ``root_position``.
    Without this, offsets would live in armature-local frame while root pins
    live in world frame, and the 90° / scale would silently misalign them.

    Which bones go out, under which parents, is `request_tree`'s answer: on a
    control rig the deform bones only (the server's bone classifier would
    otherwise see Ctrl_*/IK helpers/pole vectors and pick wrong slots), joined
    into a single tree; on a rig with several root bones, the body. The
    server's schema requires exactly one ``parent: null`` joint, and parents
    before their children.
    """
    mw = armature_obj.matrix_world
    bones = armature_obj.data.bones
    joints: list[dict[str, Any]] = []
    for name, parent_name in request_tree(armature_obj).items():
        head = mw @ bones[name].head_local
        local = head if parent_name is None else head - mw @ bones[parent_name].head_local
        mx, my, mz = coords.blender_pos_to_mmcp(local)
        joints.append({
            "name":             name,
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
    # Same rule as the frame range: once anything has been typed, untyped
    # blocks are gaps. They used to compete for the timeline, and the overlap
    # resolution below let the earliest one win — so the empty block seeded
    # across the whole scene silently ate every prompt the artist wrote, and
    # the request went out as one long unconditioned stretch.
    has_text = any((b.prompt or "").strip()
                   for b in prompt_blocks if getattr(b, "enabled", True))

    enabled: list[tuple[int, int, str, int]] = []
    for b in prompt_blocks:
        if not getattr(b, "enabled", True):
            continue
        if has_text and not (b.prompt or "").strip():
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

_GENERATED_ACTION_PREFIXES: tuple[str, ...] = (
    "Animatica_Motion",      # current motion-bake naming
    "Proscenium_Motion",     # pre-rename motion-bake naming
    "Proscenium_Generated",  # legacy pre-rename motion-bake naming
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
    locked_seams: tuple[bool, bool] = (False, False),
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # Waypoints first: the way a route is authored now. Legacy curves below
    # still go out for files made before waypoints existed.
    if constraint_objects.get("waypoints"):
        from . import waypoints

        scene = bpy.context.scene
        settings = getattr(scene, "animatica", None)
        out.extend(waypoints.request_constraints(
            scene, frame_range,
            face_along_path=bool(getattr(settings, "waypoint_heading", False)),
            marks=constraint_objects["waypoints"],
        ))

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
    # Deliberately no name check. This used to skip the action outright when it
    # was called ``Animatica_Motion: …``, to avoid feeding a previous bake back
    # in — but it also silenced every pose a user had authored in an action that
    # merely carries that name, which is exactly what a spliced or hand-edited
    # take looks like: the result was a generation with nothing anchoring it.
    # ``sample_pose_keyframes`` filters by keyframe TYPE instead, which answers
    # the same question precisely and per-key rather than per-action.
    if src is not None:
        out.extend(
            constraints_ui.sample_pose_keyframes(
                armature_obj,
                source_action=src,
                frame_range=frame_range,
            )
        )

    # Where a locked block meets the window, the take starts (or ends) on its
    # pose: the whole body, where it stands, at the seam frame -- the same
    # pin a single block's regenerate puts at its seams. A pose the artist
    # keyed on that frame is already there, and is theirs.
    if src is not None and any(locked_seams):
        lo, hi = int(frame_range[0]), int(frame_range[1])
        pinned = {c.get("frame") for c in out if c.get("type") == "pose_keyframe"}
        for held, frame, at in ((locked_seams[0], lo, 0), (locked_seams[1], hi, hi - lo)):
            if not held or at in pinned:
                continue
            c = constraints_ui.sample_pose_at_frame(
                armature_obj, source_action=src, sample_frame=frame, request_frame=at,
            )
            if c is not None:
                out.append(c)

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
    spot = start_spot(armature_obj)
    if spot is None:
        return None
    x, _, z = coords.blender_pos_to_mmcp((spot[0], spot[1], 0.0))
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


def start_spot(armature_obj: bpy.types.Object) -> tuple[float, float] | None:
    """Where the character stands: world XY of the main root's head in the
    current pose, the spot the start anchor pins frame 0 to. ``None`` when
    the rig has no root to read."""
    root_pb = main_root(armature_obj)
    if root_pb is None:
        return None
    root_world = (armature_obj.matrix_world @ root_pb.matrix).translation
    return root_world.x, root_world.y


def places_start(request: dict[str, Any]) -> bool:
    """``True`` when a constraint in ``request`` pins frame 0 in the world.
    A loop sent without one starts at the origin, and the operator has to
    move it onto the character itself."""
    return any(_pins_frame_zero(c) for c in request.get("constraints") or ())


def _pins_frame_zero(c: dict[str, Any]) -> bool:
    t = c.get("type")
    if t == "root_path":
        return 0 in (c.get("frames") or [])
    if t == "pose_keyframe":
        return c.get("frame") == 0 and c.get("root_position") is not None
    return False


def _add_ground(constraints: list[dict[str, Any]], *, model_caps: dict[str, Any],
                scene, armature_obj, start_frame: int | None = None) -> None:
    """The ground along the take's route (MMCP 1.3 ``ground_height``), where
    the server says it understands one. Without it the model walks a route
    that drops off one roof onto a lower one at the old height, in the air.
    ``start_frame`` is the scene frame of the request's frame 0: the key poses
    are read there, so the ground is found under their feet."""
    if "ground_height" not in (model_caps.get("supported_constraints") or ()):
        return
    from . import ground

    try:
        c = ground.ground_constraint(scene, armature_obj, constraints, start_frame=start_frame)
    except Exception as exc:                                  # noqa: BLE001
        # a scene the rays cannot read must not stop a generation
        print(f"[animatica] ground not sent: {exc}")
        return
    if c is not None:
        constraints.append(c)


def _protocol_version(constraints: list[dict[str, Any]]) -> str:
    """The protocol a request needs: 1.3 once it carries the ground."""
    if any(c.get("type") == "ground_height" for c in constraints):
        return GROUND_PROTOCOL_VERSION
    return PROTOCOL_VERSION


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
        "num_samples":       1,                          # Variations raise it (build_request)
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
