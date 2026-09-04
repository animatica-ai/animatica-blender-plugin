"""Translate Blender state into the shared core's request model.

This is the convergence seam. Everything that decides *what the MMCP request
says* now lives in ``animatica_core.core.request_builder``; this module only
reads Blender (settings, prompt blocks, path curves, effector empties, pose
keys) and hands core the model it understands:

    settings           -> a duck-typed ``AppState`` (:func:`state_from_settings`)
    PromptBlock        -> ``PromptBox``            (:func:`prompt_boxes`)
    curve / empty / key-> ``ConstraintMarker``     (``markers_from_*``)
    armature           -> ``skeleton_override`` + ``root_anchor_xz``

UI, baking and the axis conversion stay in the addon. The frame-0 anchor
injection, seam-copy pass, heading derivation, constraint budget, ``timing``
block and every soft warning are core's job and must NOT be re-implemented
here (diff report §5.K).

Old call site -> new function
----------------------------
``operators.py:568``  ``request_builder.build_request(model_id=…, model_caps=…,
    armature_obj=arm, prompt_blocks=…, settings=…, scene=…,
    constraint_objects=constraints_ui.walk_scene_constraints(scene))``
    -> :func:`build_request(context, settings, arm, model_caps,
       settings.prompt_blocks, warnings)`.
    The scene's constraint objects are walked inside; ``model_id`` comes from
    ``model_caps["id"]``. Catch ``core_adapter.BuildError`` (re-exported from
    core) instead of ``request_builder.BuildError``, and surface ``warnings``
    with ``self.report({'WARNING'}, …)``.

``timeline_operators.py:1308``  ``request_builder.build_request_for_block(
    block_index=idx, …, preview_action=…, source_action=…, seed_override=…)``
    -> :func:`build_block_request(context, settings, arm, model_caps,
       s.prompt_blocks[idx], warnings, preview_action=preview_action,
       seed_override=used_seed)`.
    Returns the request dict only; the splice range the caller needs is
    ``(int(block.frame_start), int(block.frame_end))``. ``source_action`` is
    dropped — the old signature never read it.

``operators.py:1234``  the inline pose request dict
    -> :func:`build_pose_request(settings, arm, model_caps, self.prompt,
       warnings, seed=self.seed)`.

Structure: everything above the "Blender-facing" banner is pure — no ``bpy``
and no ``mathutils``, at module level or anywhere — so the mapping rules are
testable headless (see ``scratchpad/b2/parity_check.py``). Below the banner,
``bpy`` and the addon's own modules are imported lazily inside the functions.
"""

from __future__ import annotations

import random
from typing import Any, Iterable, Mapping, Sequence

# Absolute, not relative, on purpose: the vendored core is importable as
# top-level ``animatica_core`` (``__init__._ensure_on_sys_path``), which is the
# name it uses for itself, and it keeps this module importable outside Blender
# with only the addon directory on ``PYTHONPATH``.
from animatica_core.core.prompt_model import (CharacterState, ConstraintMarker,
                                              PromptBox)
from animatica_core.core.request_builder import (QUALITY_PRESETS,  # noqa: F401
                                                 BuildError)
from animatica_core.core.request_builder import build_pose_request as _core_build_pose_request
from animatica_core.core.request_builder import build_request as _core_build_request


#: Per-control-point frames on a root-path curve ("timed waypoints"). The
#: constant does not exist in ``constants.py`` on this branch — the UI that
#: writes it (and the move of this name into ``constants.py``) is a separate
#: step. Read here so a curve authored by that UI already maps to one marker
#: per point at its own frame; absent, the points are spread evenly over the
#: request's frame range (diff report §3.14).
PROP_POINT_FRAMES = "animatica_point_frames"

#: Canonical end-effector joint -> the ``ConstraintMarker.type`` family core
#: recognises. Keys mirror ``constants.END_EFFECTOR_JOINTS``; core reads the
#: joint NAME from ``marker.joint`` and the family from ``marker.type``, so both
#: have to be set (diff report §5.F).
EFFECTOR_MARKER_TYPES = {
    "LeftHand":  "left-hand",
    "RightHand": "right-hand",
    "LeftFoot":  "left-foot",
    "RightFoot": "right-foot",
}


# ---------------------------------------------------------------------------
# Pure: settings -> state
# ---------------------------------------------------------------------------

def resolve_seed(value) -> int:
    """Turn a seed setting into a concrete value.

    ``0`` ("auto") becomes a fresh random seed; any positive value passes
    through. Client-side seed recording stays in the addon: core would omit
    ``options.seed`` entirely for "auto" and let the server pick, which loses
    reproducibility (diff report §3.12). Verbatim from
    ``request_builder._resolve_seed``.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else random.randint(1, 999999)


class _CoreState:
    """The attribute surface ``core.request_builder`` reads off ``AppState``.

    Values and gates come from diff report §5.A: the gates carry their product
    defaults, and every field core reads is present so a ``getattr`` fallback
    never silently decides behaviour.
    """

    def __init__(
        self, *, steps: int, post_processing: bool, transition_frames: int,
        cfg_type: str, cfg_text_weight: float, cfg_constraint_weight: float,
        seed: int, start_frame: int, total_frames: int, fps: float,
        send_path_heading: bool,
    ) -> None:
        # options
        self.steps = int(steps)
        self.num_samples = 1                    # no multi-sample UI in the addon
        self.animation_mode = ""                # not "existing_take"
        self.post_processing = bool(post_processing)
        self.transition_frames = int(transition_frames)
        self.random_seed = False                # addon semantics: seed == 0 is "auto"
        self.seed = int(seed)
        self.cfg_type = cfg_type
        self.cfg_text_weight = float(cfg_text_weight)
        self.cfg_constraint_weight = float(cfg_constraint_weight)
        # frame math / fps mismatch warning
        self.start_frame = int(start_frame)
        self.total_frames = int(total_frames)
        self.fps = float(fps)
        # feature gates
        self.send_path_heading = bool(send_path_heading)
        self.duplicate_seam_waypoints = True
        self.reorder_same_frame_waypoints = False
        self.send_effector_root_context = False
        self.debug_send_effector_rotations = False
        self.debug_omit_root_anchor = False
        self.debug_omit_timing = False
        self.debug_send_scene_fps = False
        self.match_scene_fps = False
        self.walk_speed_ms = None


def state_from_settings(
    settings,
    *,
    seed: int,
    start_frame: int = 0,
    total_frames: int = 1,
    fps: float = 0.0,
    send_path_heading: bool = False,
) -> _CoreState:
    """Map the addon's settings PropertyGroup onto the core state surface.

    ``seed`` is the already-resolved concrete seed (see :func:`resolve_seed`);
    ``fps`` is the SCENE fps — core sends the MODEL fps on the wire and uses
    this one only to warn about a mismatch.
    """
    steps = QUALITY_PRESETS.get(
        getattr(settings, "quality_preset", ""),
        int(getattr(settings, "custom_steps", 100)),
    )
    return _CoreState(
        steps=steps,
        post_processing=bool(getattr(settings, "post_processing", True)),
        transition_frames=int(getattr(settings, "num_transition_frames", 5)),
        cfg_type="separated" if getattr(settings, "cfg_enabled", False) else "nocfg",
        cfg_text_weight=float(getattr(settings, "cfg_text", 2.0)),
        cfg_constraint_weight=float(getattr(settings, "cfg_constraint", 2.0)),
        seed=seed,
        start_frame=start_frame,
        total_frames=total_frames,
        fps=fps,
        send_path_heading=send_path_heading,
    )


# ---------------------------------------------------------------------------
# Pure: prompt blocks -> PromptBox
# ---------------------------------------------------------------------------

def prompt_boxes(blocks: Iterable[Any]) -> list[PromptBox]:
    """Enabled ``PromptBlock``s as ``PromptBox``es, sorted by start frame.

    Frames stay INCLUSIVE — ``PromptBlock.frame_end`` is the last frame of the
    block and ``core.build_segments`` computes ``end - start + 1``, so the
    segment durations, the gap filling and the empty-prompt promotion to
    ``unconditioned`` are byte-identical to the addon's own ``build_segments``.
    A block's own seed rides in ``params["seed"]``; core only puts it on the
    wire when the model advertises ``supports_segment_seed``.
    """
    boxes: list[PromptBox] = []
    for b in blocks or ():
        if not getattr(b, "enabled", True):
            continue
        params: dict[str, Any] = {}
        try:
            block_seed = int(getattr(b, "seed", 0) or 0)
        except (TypeError, ValueError):
            block_seed = 0
        if block_seed > 0:
            params["seed"] = block_seed
        boxes.append(PromptBox(
            start=int(b.frame_start),
            end=int(b.frame_end),
            text=(getattr(b, "prompt", "") or ""),
            params=params,
        ))
    boxes.sort(key=lambda box: box.start)
    return boxes


# ---------------------------------------------------------------------------
# Pure: scene geometry -> ConstraintMarker
# ---------------------------------------------------------------------------

def markers_from_root_path(
    points_xz: Sequence[Sequence[float]],
    frames: Sequence[int] | None,
    frame_range: tuple[int, int],
) -> list[ConstraintMarker]:
    """One ``root2d`` marker per control point of a path curve.

    ``points_xz`` are already in the MMCP frame. ``frames`` are the curve's own
    per-point scene frames (``PROP_POINT_FRAMES``); ``None`` spreads the points
    evenly across ``frame_range`` inclusive. Core turns each marker into its own
    single-waypoint ``root_path`` wire — a deliberate shape, not drift, and the
    only one its seam-copy and heading passes accept (diff report §3.14).

    A frame list that does not cover every point is treated as absent rather
    than zipped against them: the curve's timing property goes stale the moment
    a control point is added, and silently dropping the tail of the path is a
    worse answer than retiming the whole of it.
    """
    pts = [(float(p[0]), float(p[1])) for p in points_xz or ()]
    if not pts:
        return []
    lo, hi = int(frame_range[0]), int(frame_range[1])
    if frames is not None and len(frames) < len(pts):
        frames = None
    if frames is not None:
        pairs = list(zip((int(f) for f in frames), pts))
    elif len(pts) == 1:
        pairs = [(lo, pts[0])]
    else:
        span = max(0, hi - lo)
        pairs = [
            (lo + int(round(i * span / (len(pts) - 1))), p)
            for i, p in enumerate(pts)
        ]
    return [
        ConstraintMarker(frame=f, joint="", type="root2d",
                         value={"xz": [x, z]})
        for f, (x, z) in pairs
    ]


def markers_from_effector(
    joint: str,
    keyed_positions: Mapping[int, Sequence[float]],
    frame_range: tuple[int, int],
) -> list[ConstraintMarker]:
    """One end-effector marker per KEYED FRAME (not one per empty).

    ``keyed_positions`` maps an ABSOLUTE scene frame to an MMCP position.
    Collapsing every frame of one joint into a single multi-frame
    ``effector_target`` wire is core's job (``_merge_effector_group``); two
    empties on one joint would otherwise race server-side (diff report §3.5).

    The "no keyframes in range -> pin at ``frame_range[0]``" rule (§3.6) is
    applied upstream, by ``constraints_ui.sample_effector_target``, which
    already substitutes the empty's static location at the range start; this
    function only filters and converts.
    """
    joint = (joint or "").strip()
    mtype = EFFECTOR_MARKER_TYPES.get(joint)
    if not joint or mtype is None:
        return []
    lo, hi = int(frame_range[0]), int(frame_range[1])
    out: list[ConstraintMarker] = []
    for f in sorted(keyed_positions):
        if not (lo <= int(f) <= hi):
            continue
        p = keyed_positions[f]
        out.append(ConstraintMarker(
            frame=int(f), joint=joint, type=mtype,
            value={"position": [float(p[0]), float(p[1]), float(p[2])]},
        ))
    return out


def markers_from_pose(
    frame: int,
    joint_rotations: Mapping[str, Sequence[float]],
    root_position: Sequence[float] | None = None,
    fill_mode: str = "rest",
) -> ConstraintMarker:
    """One ``fullbody`` marker from a sampled pose at an ABSOLUTE scene frame.

    ``fill_mode`` stays in ``value``: core's wire emitter ignores it today, so
    it does not reach the server yet (diff report §3.1 — the field is missing
    from the core contract and is being added SDK-side). Keeping it here means
    the addon loses nothing the moment core passes it through.
    """
    marker_value: dict[str, Any] = {
        "joint_rotations": {str(k): list(v) for k, v in (joint_rotations or {}).items()},
        "fill_mode": fill_mode,
    }
    if root_position is not None:
        marker_value["root_position"] = [float(c) for c in root_position]
    return ConstraintMarker(frame=int(frame), joint="", type="fullbody",
                            value=marker_value)


def character_state(
    boxes: Iterable[PromptBox],
    markers: Iterable[ConstraintMarker],
) -> CharacterState:
    """The single ``CharacterState`` a Blender scene maps to.

    Blender has one target armature per generation, so there is no per-character
    split to carry; the id is a constant.
    """
    return CharacterState(
        character_id="blender",
        display_name="Blender",
        prompts=list(boxes),
        constraints=list(markers),
    )


# ===========================================================================
# Blender-facing (bpy imported lazily inside each function)
# ===========================================================================

def _scene_fps(scene) -> float:
    render = getattr(scene, "render", None)
    if render is None:
        return 0.0
    base = float(getattr(render, "fps_base", 1.0) or 1.0)
    return float(render.fps) / base


def _skeleton_override(arm, model_caps: dict[str, Any], model_id: str) -> dict[str, Any]:
    """The skeleton this request ships, mirroring ``request_builder.build_request``.

    The user's own armature, so the server retargets; when the server cannot
    retarget the armature must already mirror the canonical skeleton, which is
    then echoed verbatim. ``armature_to_skeleton`` stays in the addon — core
    does not know Blender (diff report §3.13 / §5.G).
    """
    from . import rig_probe

    canonical = model_caps.get("canonical_skeleton") or {}
    canonical_joint_names = {j["name"] for j in canonical.get("joints", [])}
    if bool(model_caps.get("supports_retargeting", False)):
        return rig_probe.armature_to_skeleton(arm)

    if not canonical_joint_names:
        raise BuildError(f"Model {model_id!r} has no canonical_skeleton.joints")
    missing = canonical_joint_names - {pb.name for pb in arm.pose.bones}
    if missing:
        raise BuildError(
            f"Armature {arm.name!r} is missing {len(missing)} canonical joint(s) "
            f"(first few: {sorted(missing)[:5]}). "
            f"This server does not support retargeting — pick a rig that "
            f"mirrors the canonical skeleton, or use 'Import canonical skeleton'"
        )
    return canonical


def _root_anchor_xz(arm) -> tuple[float, float] | None:
    """The root bone's world XZ in MMCP space, for core's frame-0 anchor.

    Same read as the old ``request_builder._start_anchor``, minus the heading
    (heading is core's, gated by ``state.send_path_heading`` — §3.8) and minus
    the "is frame 0 already pinned?" test, which core does itself. Passed
    verbatim: core neither offsets nor rotates it.
    """
    from . import coords

    root_pb = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    if root_pb is None:
        return None
    root_world = (arm.matrix_world @ root_pb.matrix).translation
    x, _, z = coords.blender_pos_to_mmcp(root_world)
    return (x, z)


def _curve_markers(curve_obj, frame_range: tuple[int, int]) -> list[ConstraintMarker]:
    """Root-path curve -> one ``root2d`` marker per Bezier control point.

    Read straight off the curve rather than through
    ``constraints_ui.sample_root_path``: that helper returns ONE dict holding a
    densely resampled polyline, which cannot be split back into per-waypoint
    markers without inventing frames.
    """
    from . import coords

    data = getattr(curve_obj, "data", None)
    splines = getattr(data, "splines", None) or ()
    if not splines:
        return []
    mw = curve_obj.matrix_world
    points_xz: list[tuple[float, float]] = []
    for bp in splines[0].bezier_points:
        x, _, z = coords.blender_pos_to_mmcp(mw @ bp.co)
        points_xz.append((x, z))

    raw_frames = curve_obj.get(PROP_POINT_FRAMES)
    frames = [int(f) for f in raw_frames] if raw_frames else None
    return markers_from_root_path(points_xz, frames, frame_range)


def _effector_markers(empty_obj, frame_range: tuple[int, int],
                      total_frames: int) -> list[ConstraintMarker]:
    """Effector empty -> one marker per keyed frame.

    Reuses ``constraints_ui.sample_effector_target`` for the scene read (its
    frame-set + depsgraph dance and its "no keys in range" fallback), then
    un-relativizes its frames: the sampler returns request-relative indices
    (``frame - frame_range[0]``) while core wants ABSOLUTE ones (§5.E).
    """
    from . import constraints_ui

    wire = constraints_ui.sample_effector_target(
        empty_obj, frame_range=frame_range, total_frames=total_frames,
    )
    if wire is None:
        return []
    base = int(frame_range[0])
    keyed = {
        int(f) + base: p
        for f, p in zip(wire.get("frames") or (), wire.get("positions") or ())
    }
    return markers_from_effector(wire.get("joint", ""), keyed, frame_range)


def _pose_marker(wire: dict[str, Any], frame: int) -> ConstraintMarker:
    """A ``sample_pose_*`` dict -> ``fullbody`` marker at an absolute frame."""
    return markers_from_pose(
        frame,
        wire.get("joint_rotations") or {},
        wire.get("root_position"),
        wire.get("fill_mode", "rest"),
    )


def _stamp_seeds(settings, blocks, resolved_global: int, supports_seg: bool) -> None:
    """Record the concrete seeds this generation runs with.

    Verbatim from ``request_builder.build_request``: the clip seed lands on
    ``settings.last_used_seed`` so a Seed = 0 run can be locked in, and every
    enabled block records the seed it actually inherits (its own only when it
    pinned one and the model reads per-segment seeds).
    """
    try:
        settings.last_used_seed = resolved_global
    except (AttributeError, TypeError):
        pass
    for b in blocks or ():
        if not getattr(b, "enabled", True):
            continue
        try:
            block_seed = int(getattr(b, "seed", 0) or 0)
            b.last_used_seed = (
                block_seed if (supports_seg and block_seed > 0) else resolved_global
            )
        except (AttributeError, TypeError, ValueError):
            pass


def build_request(
    context,
    settings,
    arm,
    model_caps: dict[str, Any],
    prompt_blocks,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Full-timeline Generate, assembled by core.

    Raises ``BuildError`` (core's) on invalid state; the caller reports it.
    Soft warnings are appended to ``warnings``.
    """
    from . import constants, constraints_ui, rig_probe

    if arm is None or arm.type != 'ARMATURE':
        raise BuildError("Set a target armature first")

    scene = context.scene
    model_id = model_caps.get("id") or getattr(settings, "model_id", "")
    skeleton = _skeleton_override(arm, model_caps, model_id)

    supports_seg = bool(model_caps.get("supports_segment_seed"))
    resolved_global = resolve_seed(settings.seed)
    _stamp_seeds(settings, prompt_blocks, resolved_global, supports_seg)

    frame_range = rig_probe.compute_frame_range(prompt_blocks, arm, scene)
    span = frame_range[1] - frame_range[0] + 1

    objects = constraints_ui.walk_scene_constraints(scene)
    markers: list[ConstraintMarker] = []
    send_path_heading = False
    for curve in objects.get("root_paths", []):
        markers.extend(_curve_markers(curve, frame_range))
        if curve.get(constants.PROP_MATCH_DIRECTION):
            send_path_heading = True
    for empty in objects.get("effector_targets", []):
        markers.extend(_effector_markers(empty, frame_range, span))

    # Pose keys come only from a user-authored action — never from a previous
    # bake, or regenerate would feed the model its own output back.
    src = (
        arm.animation_data.action
        if arm.animation_data and arm.animation_data.action
        else None
    )
    if src is not None and not src.name.startswith(rig_probe._GENERATED_ACTION_PREFIX):
        for wire in constraints_ui.sample_pose_keyframes(
            arm, source_action=src, frame_range=frame_range,
        ):
            markers.append(_pose_marker(wire, int(wire["frame"]) + frame_range[0]))

    state = state_from_settings(
        settings,
        seed=resolved_global,
        start_frame=int(scene.frame_start),
        total_frames=span,
        fps=_scene_fps(scene),
        send_path_heading=send_path_heading,
    )
    return _core_build_request(
        state=state,
        character_state=character_state(prompt_boxes(prompt_blocks), markers),
        model_caps=model_caps,
        skeleton_override=skeleton,
        seed_override=resolved_global,
        frame_offset=0,              # Blender has no takes: marker frames are scene frames
        frame_range_override=frame_range,
        root_anchor_xz=_root_anchor_xz(arm),
        warnings=warnings,
    )


def build_block_request(
    context,
    settings,
    arm,
    model_caps: dict[str, Any],
    block,
    warnings: list[str] | None = None,
    *,
    preview_action=None,
    seed_override: int | None = None,
) -> dict[str, Any]:
    """Regenerate ONE prompt block, assembled by core.

    Scope is the block's own inclusive frame range and its single prompt.
    Continuity comes from boundary poses sampled from ``preview_action`` on the
    far side of each seam (scene frames ``block_start - 1`` / ``block_end + 1``)
    and handed to core as ``anchor_markers`` placed ON the seam frames — core
    reframes every marker against ``frame_range[0]``, so those land on request
    frames 0 and ``total - 1``, exactly where the old builder put them. Bones
    the user actually rotated inside the block become ordinary ``fullbody``
    markers; an edit sitting ON a seam is merged into that seam's pose.

    The caller's splice range is ``(int(block.frame_start), int(block.frame_end))``.
    """
    from . import constraints_ui, rig_probe

    if arm is None or arm.type != 'ARMATURE':
        raise BuildError("Set a target armature first")
    if not getattr(block, "enabled", True):
        raise BuildError("Block is disabled — enable it to regenerate")
    if preview_action is None:
        preview_action = (
            arm.animation_data.action
            if arm.animation_data and arm.animation_data.action
            else None
        )
    if preview_action is None:
        raise BuildError("No preview to regenerate from")

    block_start = int(block.frame_start)
    block_end = int(block.frame_end)
    if block_end < block_start:
        raise BuildError("Block has an empty frame range")
    total_frames = block_end - block_start + 1
    frame_range = (block_start, block_end)

    scene = context.scene
    model_id = model_caps.get("id") or getattr(settings, "model_id", "")
    skeleton = _skeleton_override(arm, model_caps, model_id)

    seed = (
        int(seed_override) if seed_override is not None
        else resolve_seed(getattr(block, "seed", 0) or settings.seed)
    )

    edited_by_frame = rig_probe._user_edited_bones_per_frame(
        preview_action, block_start, block_end,
    )
    preview_min, preview_max = rig_probe._preview_frame_extent(preview_action)
    has_left = preview_min is not None and (block_start - 1) >= preview_min
    has_right = preview_max is not None and (block_end + 1) <= preview_max

    anchor_frames: set[int] = set(edited_by_frame)
    if has_left:
        anchor_frames.add(block_start)
    if has_right:
        anchor_frames.add(block_end)

    anchors: list[ConstraintMarker] = []
    markers: list[ConstraintMarker] = []
    for f in sorted(anchor_frames):
        edited_at_f = edited_by_frame.get(f, set())

        base_wire = None
        if f == block_start and has_left:
            base_wire = constraints_ui.sample_pose_at_frame(
                arm, source_action=preview_action,
                sample_frame=block_start - 1, request_frame=0,
            )
        elif f == block_end and has_right:
            base_wire = constraints_ui.sample_pose_at_frame(
                arm, source_action=preview_action,
                sample_frame=block_end + 1, request_frame=total_frames - 1,
            )

        user_wire = None
        if edited_at_f:
            user_wire = constraints_ui.sample_pose_at_frame(
                arm, source_action=preview_action,
                sample_frame=f, request_frame=f - block_start,
                bone_names=edited_at_f,
            )

        if base_wire is not None and user_wire is not None:
            # Seam stays the base (full body); the user's values win per bone,
            # and their root position wins only if they posed the root.
            base_wire["joint_rotations"].update(user_wire["joint_rotations"])
            if "root_position" in user_wire:
                base_wire["root_position"] = user_wire["root_position"]
            anchors.append(_pose_marker(base_wire, f))
        elif base_wire is not None:
            anchors.append(_pose_marker(base_wire, f))
        elif user_wire is not None:
            markers.append(_pose_marker(user_wire, f))

    # Effector pins inside the block ride along; root paths deliberately do not
    # (a whole-timeline trajectory resampled onto one block would compress the
    # entire curve into it — the boundary poses already carry start/end).
    for empty in constraints_ui.walk_scene_constraints(scene).get("effector_targets", []):
        markers.extend(_effector_markers(empty, frame_range, total_frames))

    state = state_from_settings(
        settings,
        seed=seed,
        start_frame=block_start,
        total_frames=total_frames,
        fps=_scene_fps(scene),
    )
    return _core_build_request(
        state=state,
        character_state=character_state([], markers),
        model_caps=model_caps,
        skeleton_override=skeleton,
        seed_override=seed,
        segments_override=[{
            "text": (getattr(block, "prompt", "") or "").strip(),
            "duration_frames": total_frames,
        }],
        anchor_markers=anchors,
        frame_offset=0,
        frame_range_override=frame_range,
        root_anchor_xz=_root_anchor_xz(arm),
        warnings=warnings,
    )


def build_pose_request(
    settings,
    arm,
    model_caps: dict[str, Any],
    prompt: str,
    warnings: list[str] | None = None,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    """Single-pose Generate, assembled by core.

    ``seed`` is the dialog's own seed (``0`` = let the server pick), which is
    the only seed this path has ever used — the clip Seed is not consulted, and
    nothing is stamped onto ``settings``.
    """
    del warnings  # core's pose builder emits none; kept for call-site symmetry

    if arm is None or arm.type != 'ARMATURE':
        raise BuildError("Set a target armature first")

    model_id = model_caps.get("id") or getattr(settings, "model_id", "")
    skeleton = _skeleton_override(arm, model_caps, model_id)

    try:
        pose_seed = int(seed if seed is not None else getattr(settings, "seed", 0))
    except (TypeError, ValueError):
        pose_seed = 0
    # state.seed <= 0 makes build_options omit the key (server picks), which is
    # what a 0 in the dialog has always meant here.
    state = state_from_settings(settings, seed=max(0, pose_seed))
    return _core_build_pose_request(
        state=state,
        model_caps=model_caps,
        prompt=prompt,
        skeleton_override=skeleton,
        seed_override=pose_seed if pose_seed > 0 else None,
    )
