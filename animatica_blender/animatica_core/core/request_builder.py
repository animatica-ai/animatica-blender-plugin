"""Assemble a complete MMCP ``GenerateRequest`` from animatica app state.

Adapted from ``proscenium_blender/request_builder.py`` — Animatica's own
code (author: Paul Pierzchlewicz, co-founder; the company holds full rights
to both trees, so the GPL licence of the public addon puts no obligation on
this proprietary copy). Measured against today's file the adaptation is a
rewrite: shared API names, ~3% shared source. Pure-Python; no
pyfbsdk imports — lives under ``core/`` per the DCC-isolation rule and is
callable from headless tests.

Server contract:
    * ``protocol_version`` = "1.0"
    * ``model`` / ``skeleton`` come from ``GET /capabilities``
    * ``segments``: list of TextSegment / UnconditionedSegment
    * ``options``: diffusion_steps, num_samples, seed, post_processing,
      transition_frames, guidance (CFG)
    * ``constraints`` (MMCP wire format, translated server-side by
      ``constraints_translator.py``):
        - ``root_path``       frames + positions_xz [+ heading_radians]
        - ``pose_keyframe``   frame + joint_rotations + root_position
        - ``effector_target`` joint + frame + position

Animatica's older ``core/constraints.py`` packs the **server-internal**
shape (``fullbody`` / ``root2d`` / ``left-foot`` / …) — that is NOT what
goes on the wire. This module emits MMCP wire constraints.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

from ..constants import DEFAULT_FPS
from ..mmcp_client import model_supported_segments


PROTOCOL_VERSION = "1.0"

QUALITY_PRESETS = {
    "STANDARD": 50,
    "HALF":     25,
    "QUARTER":  12,
}


class BuildError(Exception):
    """Raised when the current state can't be turned into a valid request."""


# ---------------------------------------------------------------------------
# Frame range
# ---------------------------------------------------------------------------

def compute_frame_range(
    prompts: Iterable[Any],
    fallback: tuple[int, int],
) -> tuple[int, int]:
    """Union of every enabled prompt block's range.

    Falls back to ``fallback`` (typically the AppState scene range) when no
    prompts are authored. Generating only as far as the user actually drew
    content keeps short-segment edits from paying for an empty tail.
    """
    starts: list[int] = []
    ends: list[int] = []
    for b in prompts or ():
        if not getattr(b, "enabled", True):
            continue
        s = int(getattr(b, "start", getattr(b, "frame_start", 0)))
        e = int(getattr(b, "end",   getattr(b, "frame_end",   0)))
        starts.append(s)
        ends.append(e)
    if starts and ends:
        return (min(starts), max(ends))
    return (int(fallback[0]), int(fallback[1]))


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------

def _segment_seed(block: Any) -> int:
    """One prompt block's own seed, or 0 for "no per-segment seed".

    Reads ``block.seed`` first, then ``block.params["seed"]`` (``PromptBox``
    carries per-block settings in its ``params`` dict). Only a concrete
    ``int`` greater than zero counts — 0 means "let the server pick", and
    ``bool`` is not a seed even though Python says it is an ``int``.
    """
    raw = getattr(block, "seed", None)
    if raw is None:
        params = getattr(block, "params", None)
        if isinstance(params, dict):
            raw = params.get("seed")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw > 0 else 0


def build_segments(
    prompts: Iterable[Any],
    frame_range: tuple[int, int],
    *,
    supports_segment_seed: bool = False,
) -> list[dict[str, Any]]:
    """Convert ``PromptBox`` instances into MMCP segments.

    Strategy mirrors proscenium:
      * Span the whole frame range so timeline-relative constraint frames
        line up with the user's authored frame numbers.
      * Each enabled block becomes a TextSegment (or UnconditionedSegment
        if its text is empty/whitespace).
      * Gaps before / between / after enabled blocks are filled with
        UnconditionedSegment so the model picks a sensible interpolation.
      * Overlaps are resolved by bumping the next block past the previous
        block's end.

    When ``supports_segment_seed`` is True (the connected model advertises
    ``supports_segment_seed`` in ``/capabilities``), each block's own seed
    (>0) rides along on its segment so one full-timeline Generate can give
    every block a distinct, reproducible seed. Against servers without that
    capability the seed is omitted — they reject unknown segment fields
    (``extra="forbid"``) — and the request-level seed drives the whole clip
    as before. Gap-filling segments never carry a seed.
    """
    enabled: list[tuple[int, int, str, int]] = []
    for b in prompts:
        if not getattr(b, "enabled", True):
            continue
        s_raw = int(getattr(b, "start", getattr(b, "frame_start", 0)))
        e_raw = int(getattr(b, "end",   getattr(b, "frame_end",   0)))
        text = (getattr(b, "text", None) or getattr(b, "prompt", "") or "").strip()
        s = max(s_raw, frame_range[0])
        e = min(e_raw, frame_range[1])
        if e < s:
            continue
        enabled.append((s, e, text, _segment_seed(b)))

    if not enabled:
        return []

    enabled.sort(key=lambda t: t[0])

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
        # Attach the block's seed only when the server can read it and the
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
            # Empty/whitespace prompt -> unconditioned (TextSegment fails
            # server-side validation on min_length=1).
            segments.append(_with_seed(
                {"type": "unconditioned", "duration_frames": e - s + 1}, seed,
            ))
        cursor = e + 1

    if cursor <= frame_range[1]:
        segments.append({"type": "unconditioned", "duration_frames": frame_range[1] - cursor + 1})
    return segments


def group_contiguous_boxes(boxes: Iterable[Any]) -> list[list[Any]]:
    """Split prompt boxes into contiguous groups for gap-aware generation (#5).

    Boxes that touch or overlap belong to one group; a genuine gap (>=1 empty
    frame with no box covering it) starts a new group. Input order is
    irrelevant — boxes are sorted by ``start`` first.

    A new group begins only when a box's ``start`` lies strictly beyond the
    running maximum ``end`` of the current group (interval-merge). Comparing
    against the running max — not the previous box's ``end`` — keeps
    overlapping or *nested* boxes (e.g. a long box wholly containing a short
    one) in a single group instead of wrongly splitting the tail off.

    Frames are half-open ``[start, end)`` (box ``duration = end - start``), so
    a box whose ``start`` equals the group's max ``end`` is back-to-back
    adjacent (no empty frame between) and stays in the same group; the strict
    ``>`` split fires only when an empty frame actually exists.

    Returns a list of groups, each a list of the original box objects sorted
    by ``start``. A group's absolute span is ``(group[0].start, max(b.end))``.
    """
    ordered = sorted(boxes, key=lambda b: int(getattr(b, "start", 0)))
    groups: list[list[Any]] = []
    group_max_end: int | None = None
    for b in ordered:
        b_start = int(getattr(b, "start", 0))
        b_end   = int(getattr(b, "end", 0))
        if group_max_end is None or b_start > group_max_end:
            groups.append([b])                       # gap (or first box) → new group
            group_max_end = b_end
        else:
            groups[-1].append(b)                     # touches/overlaps current group
            group_max_end = max(group_max_end, b_end)
    return groups


# ---------------------------------------------------------------------------
# Constraints (MMCP wire format)
# ---------------------------------------------------------------------------

# Animatica ConstraintMarker.type values -> MMCP wire constraint type.
# animatica_core/core/prompt_model.py:19 CONSTRAINT_TYPES = (
#   "fullbody", "left-foot", "right-foot", "left-hand", "right-hand", "root2d"
# )
_END_EFFECTOR_TYPES = frozenset({"left-foot", "right-foot", "left-hand", "right-hand"})

# Marker type -> the label the user actually sees (constraints-section selector /
# timeline context menu; mirrors gui/timeline/widget._CONSTRAINT_LABELS, duplicated
# because core/ must not import gui). Warning text only — public so the GUI's own
# "not sent" messages read identically.
MARKER_LABELS = {
    "fullbody":   "Full Body",
    "left-foot":  "Left Leg",
    "right-foot": "Right Leg",
    "left-hand":  "Left Arm",
    "right-hand": "Right Arm",
    "root2d":     "Path",
}


# ---------------------------------------------------------------------------
# Yaw relativization (Solution 1 -- rotated-rig constraint round-trip)
# ---------------------------------------------------------------------------
# When "Use current position" is on and the rig is rotated, apply rotates the
# whole trajectory + root facing by a FLAT Ry(root_yaw) (bridge/animator.py).
# Constraints are captured in absolute world coords, so they must be pre-rotated by
# the inverse Ry(-root_yaw) here, exactly as origin_offset is pre-subtracted, so the
# apply rotation cancels and the constrained frame lands on the user's scene marker.
# XZ only; Y (hip height) never rotates. Ref: plans/melodic-tickling-wind.md.

def _rotate_xz(x: float, z: float, c: float, s: float) -> tuple[float, float]:
    """Rotate ground-plane ``(x, z)`` by ``Ry(−θ)`` where ``c=cos θ, s=sin θ``.

    Inverse of apply's ``Ry(θ)`` transport, whose matrix is ``[[c,0,s],[0,1,0],[-s,0,c]]``
    (apply: ``x' = c·x + s·z; z' = −s·x + c·z``). Rotating by ``−θ`` here gives
    ``x' = c·x − s·z; z' = s·x + c·z``, so ``apply(relativize(v)) == v``.
    """
    return (c * x - s * z, s * x + c * z)


def _relativize_root_quat(quat_xyzw: Any, c: float, s: float) -> list[float]:
    """Pre-rotate a root local quaternion ``[qx,qy,qz,qw]`` by ``Ry(−θ)``.

    Done matrix-level (numpy) in the SAME convention apply uses -- apply
    pre-multiplies ``Ry(θ) @ R`` on the root rotation (``bridge/animator.py``),
    so sending ``Ry(−θ) · R_scene`` makes apply land back on ``R_scene``. No
    quaternion-handedness guessing: the 3×3 built from the quat has 3rd column
    ``(2(xz+wy), 2(yz−wx), 1−2(xx+yy))`` -- the forward basis the capture/apply already
    rely on. ``|s| < 1e-12`` (rig at ~0°) → returned unchanged.
    """
    if abs(s) < 1e-12:
        return [float(q) for q in quat_xyzw]
    x, y, z, w = (float(q) for q in quat_xyzw)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    R = np.array([
        [1 - 2 * (yy + zz), 2 * (xy - wz),     2 * (xz + wy)],
        [2 * (xy + wz),     1 - 2 * (xx + zz), 2 * (yz - wx)],
        [2 * (xz - wy),     2 * (yz + wx),     1 - 2 * (xx + yy)],
    ], dtype=np.float64)
    Ry = np.array([[c, 0.0, -s], [0.0, 1.0, 0.0], [s, 0.0, c]], dtype=np.float64)  # Ry(−θ)
    M = Ry @ R
    # 3×3 → quat [qx,qy,qz,qw] (trace method, branch on largest diagonal term)
    t = float(M[0, 0] + M[1, 1] + M[2, 2])
    if t > 0.0:
        r = math.sqrt(1.0 + t); r2 = 0.5 / r
        qx, qy, qz, qw = (M[2, 1] - M[1, 2]) * r2, (M[0, 2] - M[2, 0]) * r2, (M[1, 0] - M[0, 1]) * r2, 0.5 * r
    elif M[0, 0] >= M[1, 1] and M[0, 0] >= M[2, 2]:
        r = math.sqrt(1.0 + M[0, 0] - M[1, 1] - M[2, 2]); r2 = 0.5 / r
        qx, qy, qz, qw = 0.5 * r, (M[0, 1] + M[1, 0]) * r2, (M[0, 2] + M[2, 0]) * r2, (M[2, 1] - M[1, 2]) * r2
    elif M[1, 1] >= M[2, 2]:
        r = math.sqrt(1.0 - M[0, 0] + M[1, 1] - M[2, 2]); r2 = 0.5 / r
        qx, qy, qz, qw = (M[0, 1] + M[1, 0]) * r2, 0.5 * r, (M[1, 2] + M[2, 1]) * r2, (M[0, 2] - M[2, 0]) * r2
    else:
        r = math.sqrt(1.0 - M[0, 0] - M[1, 1] + M[2, 2]); r2 = 0.5 / r
        qx, qy, qz, qw = (M[0, 2] + M[2, 0]) * r2, (M[1, 2] + M[2, 1]) * r2, 0.5 * r, (M[1, 0] - M[0, 1]) * r2
    return [float(qx), float(qy), float(qz), float(qw)]


def _marker_to_wire(
    marker: Any,
    *,
    frame_override: int | None = None,
    origin_offset: tuple[float, float, float] | None = None,
    root_yaw: float | None = None,
    root_joint_name: str | None = None,
    send_rotations: bool = False,
) -> dict[str, Any] | None:
    """Translate one ``ConstraintMarker`` to its MMCP wire-format dict.

    Marker fields (``ConstraintMarker``):
        ``frame``: int
        ``joint``: str (canonical name)
        ``type``:  one of CONSTRAINT_TYPES
        ``value``: type-specific payload dict

    ``frame_override`` supplies the request-relative frame index (see
    :func:`_collect_constraints`); when ``None`` the marker's own frame is used.

    ``origin_offset`` is the character's scene world position (meters,
    ``(x, y, z)``) at the prompt start frame. Marker positions are captured in
    **absolute** world coords, but the request trajectory is origin-canonical (a
    frame-0 ``[0,0]`` anchor) and the apply step re-adds this same offset to the
    root. Subtracting it here puts every position-bearing constraint in the same
    origin frame as the trajectory, so the apply add is its exact inverse and the
    constrained frames land on the authored scene markers. ``None`` → no shift
    (legacy / "Use current position" off). ``joint_rotations`` and
    ``heading_radians`` are rotations, never offset. (Step 9 P1-FIX)

    ``root_yaw`` (radians) is the rig's frame-0 world yaw. When set, apply rotates
    the whole motion by a flat ``Ry(root_yaw)``, so every position-bearing field is
    pre-rotated here by the inverse ``Ry(−root_yaw)`` (XZ only; Y raw) AFTER the
    origin_offset subtraction -- the apply rotation then cancels and the constrained
    frame lands on the scene marker. For ``pose_keyframe`` the root joint's local
    rotation (``root_joint_name``) is likewise pre-rotated (only that one key; the
    rest of the body is local-to-parent, invariant under a global Y-rotation).
    ``None`` → no rotation. (Solution 1, plans/melodic-tickling-wind.md)

    ``pose_keyframe`` ``root_position`` subtracts the FULL ``origin_offset``
    (Y included) — symmetric with the effector branch, so apply's ``+off_y``
    re-add cancels exactly and a constrained pose lands at its authored scene
    height (WYSIWYG pins; fix-bugs-ivestigate-problems item 1, superseding the
    archived "Ground only" decision).

    ``send_rotations`` gates the optional ``rotations`` array on
    ``effector_target`` wires (Settings -> "Debug: send effector rotations",
    default OFF). See the effector branch below for the evidence trail.

    Returns the wire dict or ``None`` to drop the marker (e.g. malformed).
    """
    mtype = getattr(marker, "type", None)
    frame = int(getattr(marker, "frame", 0)) if frame_override is None else int(frame_override)
    joint = getattr(marker, "joint", "") or ""
    value = getattr(marker, "value", None) or {}

    ox, oy, oz = origin_offset if origin_offset is not None else (0.0, 0.0, 0.0)
    _rot = root_yaw is not None
    _c = math.cos(float(root_yaw)) if root_yaw is not None else 1.0
    _s = math.sin(float(root_yaw)) if root_yaw is not None else 0.0

    if mtype == "root2d":
        xz = value.get("xz")
        if xz is None or len(xz) != 2:
            return None
        # NB: heading is NOT emitted here. Heading on the wire flows EXCLUSIVELY
        # through the gated _assign_root_path_headings (Step 2/3) so it can't leak
        # when send_path_heading is off. The marker's manual aim lives in the
        # separate value["aim_heading_radians"] field, read only by
        # _collect_manual_path_headings -- never by this emitter.
        #
        # NB: ONE single-frame root_path per waypoint is DELIBERATE, not drift.
        # The MMCP docs illustrate a batched multi-frame dict; batching would be a
        # functional downgrade here: (1) the server translator appends waypoints
        # 1:1 and the model conditions on a sparse per-frame scatter, so a batch
        # adds no trajectory smoothing; (2) heading_radians is all-or-nothing per
        # dict server-side, so batching forfeits per-waypoint heading (it would
        # silently kill send_path_heading); (3) the last-wins dedup ordering and
        # the _eph_root_ctx injection both position INDIVIDUAL dicts by list
        # index. Evidence chain: context/changes/constraint-request-match-to-mmcp-
        # convetion/frame.md and doc/debug/motion_path_constraint/{request,response}.json.
        px, pz = float(xz[0]) - ox, float(xz[1]) - oz
        if _rot:
            px, pz = _rotate_xz(px, pz, _c, _s)
        return {
            "type":         "root_path",
            "frames":       [frame],
            "positions_xz": [[px, pz]],
        }

    if mtype == "fullbody":
        # Pose snapshot -> pose_keyframe with joint_rotations (the pose SHAPE) plus
        # root_position (the travel TARGET). root_position is the captured root world
        # position, relativized to the origin-canonical frame by subtracting the FULL
        # origin_offset — XZ (so the prompt-start frame == origin) AND Y, where
        # oy = off_y = the ground fold (composed in tool_window._compute_reseat;
        # since the item-1 follow-up off_y is the plane's Y only — preserve-height
        # no longer adds a delta). Wire Y is therefore the hip's height ABOVE THE
        # GROUND PLANE; apply re-adds the identical off_y, so the round trip
        # cancels exactly and the constrained pose frame lands at its authored
        # scene height — the same math the effector branch below has always used.
        # Kimodo's "Y = absolute hip height" (user guide) means height in the
        # canonical frame whose ground IS Y=0; subtracting the fold restores that
        # frame. This full-offset subtraction supersedes the archived "Ground
        # only" decision (context/archive/2026-07-16-ground-offset-to-
        # constraint-fix/plan.md), which special-cased the pose branch to subtract
        # ground_y_m via a dedicated kwarg (fix-bugs-ivestigate-problems, item 1).
        # Sending root_position tells the model WHERE the root must be at
        # frame K, so the character travels to the point where the pose was authored.
        #
        # We always emit it (no toggle). Empirically (doc/debug/fullbody_constraint_debug):
        # omitting root_position does NOT free the model to "walk to the pin" -- the
        # server still generates locomotion (~0.8 m) but wanders, never reaching the
        # authored point, which reads as "barely moved." Both sister projects
        # (proscenium_blender, maya_kimodo) always send a NON-origin root_position; a
        # capture taken on a static rig at origin relativizes to ~origin (no travel to
        # express), which is correct -- there is genuinely nothing to travel to there.
        # Refs: NVIDIA Kimodo constraints user guide
        #   (research.nvidia.com/labs/sil/projects/kimodo/docs/user_guide/constraints.html
        #    -- root XZ origin-relative, Y = hip height in the canonical
        #    ground-at-0 frame);
        #   MMCP constraints (animatica.ai/mmcp/docs/concepts/constraints -- root_position
        #   optional, [x, y, z]).
        joint_rotations = value.get("joint_rotations") or {}
        jr = {str(k): list(v) for k, v in joint_rotations.items()}
        # Pre-rotate ONLY the root joint's local rotation: apply composes Ry(root_yaw)
        # onto the root, so the root carries the body facing and must be inverse-rotated
        # here; every other joint is local-to-parent and invariant under a global Y.
        if _rot and root_joint_name and root_joint_name in jr:
            jr[root_joint_name] = _relativize_root_quat(jr[root_joint_name], _c, _s)
        out = {
            "type":              "pose_keyframe",
            "frame":             frame,
            "joint_rotations":   jr,
        }
        rp = value.get("root_position")
        if rp is not None and len(rp) == 3:
            rx, rz = float(rp[0]) - ox, float(rp[2]) - oz
            if _rot:
                rx, rz = _rotate_xz(rx, rz, _c, _s)
            out["root_position"] = [rx, float(rp[1]) - oy, rz]
        return out

    if mtype in _END_EFFECTOR_TYPES:
        position = value.get("position")
        if not joint or position is None:
            return None
        # MMCP effector_target schema uses array fields: frames[] + positions[][]
        # (one entry per frame), unlike pose_keyframe's singular frame.
        # Ref: animatica.ai/mmcp/docs/concepts/constraints.
        p = [float(c) for c in position]
        px, py, pz = p[0] - ox, p[1] - oy, p[2] - oz
        if _rot:
            px, pz = _rotate_xz(px, pz, _c, _s)
        out = {
            "type":      "effector_target",
            "joint":     joint,
            "frames":    [frame],
            "positions": [[px, py, pz]],
        }
        # Step 7e.4, gated since Phase 6 of the ground-plane audit: emit the
        # captured joint local quaternion as the optional `rotations` array
        # ONLY when ``send_rotations`` is on (Settings -> "Debug: send
        # effector rotations", default OFF). MMCP spec marks rotations as
        # local-to-parent [qx, qy, qz, qw], index-aligned with
        # frames/positions; the 7c capture
        # (constraint_capture._local_rotation_quat_xyzw) is already in that
        # order. The hosted server IGNORES the field entirely
        # (doc/debug/rotation_probe_20260726/RESULTS.md -- pin-frame delta
        # exactly 0.0 on a byte-identical resend with rotations added), but a
        # local kimodo_server consumes it in _build_end_effector_dict, so the
        # toggle is a real lever on backend="local" only. Absent /
        # wrong-length rotation falls through silently (legacy saves pre-7c
        # lack the field).
        if send_rotations:
            rotation = value.get("rotation")
            if isinstance(rotation, (list, tuple)) and len(rotation) == 4:
                out["rotations"] = [[float(q) for q in rotation]]
        return out

    return None


def _merge_effector_group(wires: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse single-frame ``effector_target`` dicts for one joint into a
    single multi-frame dict, mirroring proscenium_blender.sample_effector_target
    (one dict per joint, ``frames``/``positions`` arrays). Frames are sorted.

    ``rotations`` is **all-or-nothing**: emitted only when *every* frame in the
    group carries a quaternion (and only when the emitter was told to send any
    -- the ``debug_send_effector_rotations`` gate lives in
    :func:`_marker_to_wire`). A rotation-less marker (legacy pre-7c save) makes
    a mixed group position-only -- padding the gaps with identity would feed
    the model a false pose hint, worse than no hint. Dragged pins no longer
    lose their quaternion (constraint_viz flags ``rotation_stale`` instead of
    popping), so a stale rotation still counts toward all-or-nothing;
    :func:`_collect_constraints` warns post-merge when one actually ships.

    A one-marker group collapses to byte-identical output as the pre-grouping
    single-frame dict, so the common single-pin case never regresses.
    """
    entries = [
        (w["frames"][0], w["positions"][0], (w.get("rotations") or [None])[0])
        for w in wires
    ]
    entries.sort(key=lambda e: e[0])
    merged: dict[str, Any] = {
        "type":      "effector_target",
        "joint":     wires[0]["joint"],
        "frames":    [e[0] for e in entries],
        "positions": [e[1] for e in entries],
    }
    rots = [e[2] for e in entries]
    if all(r is not None for r in rots):
        merged["rotations"] = rots
    return merged


def _collect_constraints(
    character_state: Any,
    frame_range: tuple[int, int],
    origin_offset: tuple[float, float, float] | None = None,
    anchor_markers: list[Any] | None = None,
    root_yaw: float | None = None,
    root_joint_name: str | None = None,
    frame_offset: int = 0,
    total_frames: int | None = None,
    *,
    suppress_pose_frames: Iterable[int] | None = None,
    warnings: list[str] | None = None,
    warn_excluded: bool = True,
    inject_root_context: bool = False,
    suppressed_ctx_frames: set[int] | None = None,
    send_rotations: bool = False,
    reorder_same_frame: bool = False,
) -> list[dict[str, Any]]:
    """Walk ``CharacterState.constraints`` and pack into MMCP wire dicts.

    ``frame_offset`` is the take's start frame (absolute). ``frame_range`` is
    take-local (prompt-derived), while ``marker.frame`` / ``anchor_markers[].frame``
    are **absolute**, so gating and reframing happen against the absolute base
    ``frame_range[0] + frame_offset``. Markers outside that absolute span are
    dropped so the server doesn't see pins for frames that aren't in any segment.
    Surviving frames are reframed to request-relative indices
    (``frame - (frame_range[0] + frame_offset)``, clamped to
    ``[0, total_frames - 1]`` when ``total_frames`` is given) so they share the
    server's 0-based timeline with the auto-injected frame-0 anchor; this mirrors
    proscenium_blender._to_timeline_frame. At ``frame_offset == 0`` this is
    byte-identical to the legacy ``frame - frame_range[0]`` behavior. The upper
    clamp binds a pin authored on the inclusive last frame to the last generated
    index instead of reframing one past the valid range (server error).
    ``effector_target`` markers are grouped by joint into one multi-frame dict
    each (Gap A).

    ``origin_offset`` (meters) is subtracted from each marker's absolute world
    position so constraints share the origin-canonical frame with the trajectory
    — for ``pose_keyframe`` this now includes the full Y fold (off_y), exact
    round-trip with apply; see :func:`_marker_to_wire`. Both user markers and
    ``anchor_markers`` get the same treatment. (Step 9 P1-FIX)

    ``anchor_markers`` are EPHEMERAL markers the live caller synthesizes per
    request (never persisted on the character) — currently the ``pin_start``
    full-pose anchor at the prompt-start frame. They go through the SAME
    reframing / relativization / pruning pipeline as user markers; the caller is
    responsible for not duplicating a frame the user already pinned. They are
    appended AFTER user markers so user data wins on an effector-group collision.

    ``suppress_pose_frames`` is a set of ABSOLUTE frames whose **user** fullbody
    markers are dropped — the explicit, type-aware precedence mechanism the
    Keep-start/Keep-end boundary anchors need (Generate Selected, item 5). Append
    order can't express it: a user pin and the synthesized anchor at the same
    frame would otherwise BOTH survive (only effector groups dedup), putting two
    ``pose_keyframe``s on one request frame. Suppression is matched in *reframed*
    space, so it also catches the last-frame clamp collapsing a pin at ``hi-1``
    onto the end anchor's index. Effector pins are never suppressed (anchors are
    fullbody-only); anchor markers themselves are never suppressed.

    ``warnings`` is an optional out-param: user markers dropped as out-of-span get
    one summary entry, and each suppressed boundary pin gets its own. Anchor
    markers never warn (they're synthesized per request, so an out-of-span anchor
    is the caller's own gate, not a user-visible loss).

    ``warn_excluded=False`` silences ONLY the out-of-span summary, for callers that
    fan one generation across several requests (the gap-group queue): each group's
    span legitimately excludes its siblings' markers, so a per-request warning
    would name markers that ARE being sent — by another request. Those callers warn
    once themselves, against the union of every span. Suppression warnings are
    unaffected (they're always a real loss).

    **Ephemeral real-root context** (effector-pin-real-root-context): effector
    markers whose ``value`` carries ``root_xz`` + ``root_heading_radians`` (the
    rig's REAL pelvis XZ/heading, sampled at pin-capture time by
    ``constraint_capture._effector_at``) get a same-frame ``root_path`` waypoint
    auto-injected for every pin frame with no other ``root_path``/``pose_keyframe``
    coverage, appended AFTER the effector wires -- but only when
    ``inject_root_context`` is on, which it is NOT by default (see below). This emulates original
    Kimodo's ``[effector, "Hips"]`` contract, whose root co-pin is always fed the
    current motion's real pose (kimodo/demo/ui.py:2342-2381) — the hosted server
    otherwise fabricates pelvis XZ/height/facing from a T-pose
    (``_build_end_effector_dict``), the slide/push-back mechanism. The hosted
    server resolves same-frame root-channel duplicates deterministically
    LAST-listed wins (live A/B reorder probe 2026-07-26,
    ``doc/debug/reorder_probe_20260726`` + the change's
    ``investigation-2026-07-26-dedup-order.md``; upstream ``scatter_`` remains
    documented-nondeterministic, so treat as measured-not-guaranteed), so every
    root_path that must beat the fabricated root sits at a HIGHER list index
    than its effector wire — user ``root_path`` wires sharing a frame with an
    effector pin are likewise reordered after the effector group so the user's
    own pin wins (safe: heading math sorts by frame, not list position).
    Injected wires carry the private ``_eph_root_ctx`` mark: budget-exempt in
    :func:`_validate_constraint_count`, classified by
    :func:`_warn_uncovered_effector_pins`, excluded from
    :func:`_assign_root_path_headings`, counted by :func:`_pins_frame_zero`, and
    stripped in :func:`build_request` before the request ships. The heading rides
    the wire regardless of the ``send_path_heading`` gate — it exists to contest
    the fabricated ``global_root_heading`` (yaw→0), not to steer path facing.
    One waypoint per frame (first same-frame context wins); markers without the
    fields (pre-change captures) inject nothing and keep the full warning.

    ``inject_root_context`` (Settings -> "Send real body context with hand/foot
    pins") gates that whole injection. **Default False**, matching the AppState
    default since 2026-07-27: off omits the synthesized waypoints entirely (the
    request carries only what the user authored) and on is the opt-in A/B lever.
    NB off is NOT a full revert to the pre-8dea32c wire — the same-frame
    user-waypoint reorder below also landed in 8dea32c and rides its OWN gate
    (``reorder_same_frame``), so an A/B on this gate alone compares content,
    not ordering.

    ``reorder_same_frame`` (Settings -> "Path waypoint wins over same-frame
    hand/foot pins", default OFF since 2026-07-30) picks the DIRECTION of that
    reorder — both states are deterministic, walk position never decides
    (unlike the pre-8dea32c wire, whose creation-order dependence the
    2026-07-30 controlled A/B exposed): on moves a same-frame user
    ``root_path`` after the effector group (authored root wins the server's
    last-listed-wins dedup); off moves it before (the effector's fabricated
    root wins — near-exact pin snap, pelvis free to ignore the waypoint on
    that frame). The ``eff_frames`` set and the injection block below run
    regardless: injected waypoints are appended after the effector wires
    either way, and coverage is resolved by frame, not list position.

    With it off, every frame that WOULD have been injected -- uncovered, and its
    marker carried context -- is reported through the ``suppressed_ctx_frames``
    out-param (mutated in place, mirroring ``warnings``) in **REFRAMED** space,
    so :func:`_warn_uncovered_effector_pins` can tell "suppressed by the setting"
    apart from "pin carries no context at all" -- the two are byte-identical on
    the wire and want opposite advice.

    ``send_rotations`` (Settings -> "Debug: send effector rotations", default
    OFF) is forwarded to :func:`_marker_to_wire`, which emits the optional
    ``rotations`` array on effector wires only when it is on. When an emitted
    rotation came from a marker flagged ``rotation_stale`` (dragged after
    capture -- constraint_viz keeps the quaternion and marks it instead of
    popping), one soft ``warnings`` entry names the affected pins. The check
    runs AFTER the all-or-nothing group merge: a stale quaternion dropped by a
    mixed group never shipped and must not warn.
    """
    if character_state is None and not anchor_markers:
        return []
    fr_lo, fr_hi = frame_range
    base = fr_lo + frame_offset   # absolute lower bound / relativization base
    hi = fr_hi + frame_offset     # absolute upper bound

    def _reframe(f: int) -> int:
        """Absolute frame -> request-relative index, clamped to the valid range.

        A pin at the inclusive upper bound reframes to `total_frames` (one past
        the valid [0, total_frames-1]); the clamp binds it to the last frame
        instead of erroring server-side. NOTE: this collapses every pin at or
        past the last frame onto `total_frames-1`, so two same-joint/type pins at
        hi-1 and hi land on the same reframed frame (last-writer wins in the
        merged group) -- dedup is intentionally out of scope, except for the
        explicit `suppress_pose_frames` pose precedence below.
        """
        rel = f - base
        if total_frames is not None:
            rel = min(rel, total_frames - 1)
        return max(0, rel)

    # Boundary-anchor precedence, resolved in BOTH spaces: absolute (for honest
    # warning wording -- same-frame override vs clamp collision) and reframed
    # (what actually collides on the wire).
    supp_abs = {int(f) for f in (suppress_pose_frames or ()) if base <= int(f) <= hi}
    supp_rel = {_reframe(f) for f in supp_abs}

    out: list[dict[str, Any]] = []
    # joint -> list of single-frame effector wires, plus a placeholder slot in
    # `out` so the merged dict keeps the first marker's ordering.
    eff_groups: dict[str, list[dict[str, Any]]] = {}
    # reframed frame -> (root_x, root_z, heading) real-root context sampled at
    # pin-capture time; first same-frame context wins (same scene frame anyway).
    eff_ctx: dict[int, tuple[float, float, float]] = {}
    # (joint, reframed frame) -> absolute frame for effector wires whose
    # emitted rotation came from a ``rotation_stale`` marker. Resolved to a
    # warning only after the group merge below (see docstring).
    stale_rot: dict[tuple[str, int], int] = {}
    excluded: list[str] = []
    user_markers = list(getattr(character_state, "constraints", []) or [])
    n_user = len(user_markers)
    for i, marker in enumerate(user_markers + list(anchor_markers or [])):
        is_anchor = i >= n_user
        f = int(getattr(marker, "frame", 0))
        mtype = getattr(marker, "type", None)
        if f < base or f > hi:
            if not is_anchor:
                _label = str(mtype or "?")
                excluded.append(f"{MARKER_LABELS.get(_label, _label)} @ frame {f}")
            continue
        rel = _reframe(f)
        if not is_anchor and mtype == "fullbody" and rel in supp_rel:
            if warnings is not None:
                if f in supp_abs:
                    warnings.append(
                        f"Full Body pin @ frame {f} was overridden by the Keep-pose "
                        f"boundary anchor at the same frame and was not sent."
                    )
                else:
                    warnings.append(
                        f"Full Body pin @ frame {f} lands on the same generated "
                        f"frame as a Keep-pose boundary anchor (last-frame clamp) "
                        f"and was not sent."
                    )
            continue
        wire = _marker_to_wire(
            marker, frame_override=rel, origin_offset=origin_offset,
            root_yaw=root_yaw, root_joint_name=root_joint_name,
            send_rotations=send_rotations,
        )
        if wire is None:
            continue
        if wire.get("type") == "effector_target":
            joint = wire["joint"]
            group = eff_groups.get(joint)
            if group is None:
                eff_groups[joint] = group = []
                out.append({"_eff_group": joint})  # placeholder, resolved below
            group.append(wire)
            _mv = getattr(marker, "value", None) or {}
            if "rotations" in wire and _mv.get("rotation_stale"):
                stale_rot[(joint, rel)] = f
            if rel not in eff_ctx:
                _rxz = _mv.get("root_xz")
                _rh  = _mv.get("root_heading_radians")
                if (isinstance(_rxz, (list, tuple)) and len(_rxz) == 2
                        and isinstance(_rh, (int, float))):
                    eff_ctx[rel] = (float(_rxz[0]), float(_rxz[1]), float(_rh))
        else:
            out.append(wire)
    if warnings is not None and warn_excluded and excluded:
        shown = excluded[:6]
        more = f" (+{len(excluded) - len(shown)} more)" if len(excluded) > len(shown) else ""
        warnings.append(
            f"{len(excluded)} constraint(s) sit outside this generation's frame "
            f"span [{base}–{hi}] and were not sent: {', '.join(shown)}{more}."
        )

    # Same-frame root-channel dedup is deterministic LAST-listed wins on the
    # hosted server (live A/B reorder probe 2026-07-26; see docstring), so
    # whichever of {user waypoint, effector wire} sits at the higher list
    # index wins the root channel on a shared frame.
    if eff_groups:
        eff_frames = {w["frames"][0] for grp in eff_groups.values() for w in grp}
        # Deterministic same-frame ordering, direction picked by
        # ``reorder_same_frame`` — walk position never decides (the
        # pre-8dea32c wire let marker creation order pick the winner; live
        # A/B 2026-07-30): ON moves a shared-frame user root_path AFTER the
        # effector group (authored root wins the dedup), OFF moves it BEFORE
        # (fabricated root wins — near-exact pin snap). Safe both ways:
        # heading math (_assign_root_path_headings) sorts by frame, not list
        # position.
        kept:  list[dict[str, Any]] = []
        moved: list[dict[str, Any]] = []
        for c in out:
            if (c.get("type") == "root_path"
                    and any(int(f) in eff_frames for f in (c.get("frames") or []))):
                moved.append(c)
            else:
                kept.append(c)
        out = (kept + moved) if reorder_same_frame else (moved + kept)
        # Ephemeral real-root context injection (see docstring). Coverage is
        # resolved AFTER the walk so a same-frame user waypoint / pose (or
        # Keep-pose anchor) appearing later in marker order still suppresses
        # the injection. Appending the block puts every injected waypoint at a
        # higher index than every effector wire (last-listed wins).
        if eff_ctx:
            covered: set[int] = set()
            for c in out:
                if c.get("type") == "root_path":
                    covered.update(int(f) for f in (c.get("frames") or []))
                elif c.get("type") == "pose_keyframe" and c.get("frame") is not None:
                    covered.add(int(c["frame"]))
            _ec = math.cos(float(root_yaw)) if root_yaw is not None else 1.0
            _es = math.sin(float(root_yaw)) if root_yaw is not None else 0.0
            _ox, _oy, _oz = origin_offset if origin_offset is not None else (0.0, 0.0, 0.0)
            injected: list[dict[str, Any]] = []
            for rel_f in sorted(eff_frames - covered):
                ctx = eff_ctx.get(rel_f)
                if ctx is None:
                    continue
                if not inject_root_context:
                    # Gate off: report the frame instead of injecting it. Coverage
                    # is still resolved above (a user-covered frame never lands
                    # here) and the `ctx is None` skip above keeps genuinely
                    # context-less pins out -- they stay `bare`. rel_f is REFRAMED
                    # by construction (it comes from `eff_frames`, i.e. already-
                    # reframed wire frames), which is the space the classifier
                    # matches on.
                    if suppressed_ctx_frames is not None:
                        suppressed_ctx_frames.add(rel_f)
                    continue
                px, pz = ctx[0] - _ox, ctx[1] - _oz
                if root_yaw is not None:
                    px, pz = _rotate_xz(px, pz, _ec, _es)
                heading = ctx[2] - (float(root_yaw) if root_yaw is not None else 0.0)
                injected.append({
                    "type":            "root_path",
                    "frames":          [rel_f],
                    "positions_xz":    [[px, pz]],
                    "heading_radians": [heading],
                    "_eph_root_ctx":   True,
                })
            out.extend(injected)

    merged = [
        _merge_effector_group(eff_groups[c["_eff_group"]]) if "_eff_group" in c else c
        for c in out
    ]
    if warnings is not None and stale_rot:
        # Post-merge on purpose: _merge_effector_group's all-or-nothing rule
        # drops the whole `rotations` array from a mixed group, so a stale
        # quaternion in one never reached the wire and must not warn.
        shipped: list[str] = []
        for c in merged:
            if c.get("type") != "effector_target" or "rotations" not in c:
                continue
            _j = c.get("joint") or "?"
            for _rf in (c.get("frames") or []):
                f_abs = stale_rot.get((_j, int(_rf)))
                if f_abs is not None:
                    shipped.append(f"{_j} @ frame {f_abs}")
        if shipped:
            shown = shipped[:6]
            more = f" (+{len(shipped) - len(shown)} more)" if len(shipped) > len(shown) else ""
            warnings.append(
                f"{len(shipped)} hand/foot pin(s) shipped a rotation captured "
                f"BEFORE the pin was dragged: {', '.join(shown)}{more}. "
                f"\"Debug: send effector rotations\" is on and the stored "
                f"quaternion no longer matches the dragged position, so the "
                f"pose hint may fight the position target. Recreate the pin "
                f"to refresh its rotation."
            )
    return merged


def _duplicate_seam_waypoints(
    constraints: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> None:
    """Copy each boundary ``root_path`` waypoint onto the preceding index.

    Mutates ``constraints`` in place (appending copies), mirroring
    :func:`_assign_root_path_headings`' idiom.

    Contiguous prompt blocks ship as ONE request with several ``segments`` and a
    single flat ``constraints`` array, but the model consumes them **per
    segment**: it crops the constraint list with a half-open ``>= start & <
    end`` rule against a running frame cumsum (kimodo/constraints.py:125,
    ``crop_move``), rebasing each hit to segment-local 0. A waypoint authored on
    the shared boundary frame therefore reframes to exactly the cumsum index
    ``b`` and belongs to the FOLLOWING segment only -- the preceding one can run
    its entire length steered by nothing but the frame-0 anchor, and whatever
    gap its freely-invented path leaves has to be absorbed at the seam (the
    reported "slide"). Copying the same XZ to ``b - 1`` puts one waypoint in
    each segment so both sides of the seam pin the same position.

    Boundaries are the running cumsum of ``duration_frames``, EXCLUDING 0 and
    the final total (neither is a seam). Per boundary ``b`` a copy is emitted
    only when all hold:

    * ``b - 1 > 0`` -- a copy at index 0 would satisfy :func:`_pins_frame_zero`
      and suppress the auto frame-0 anchor. Reachable: ``duration_frames``
      floors at 1 in both producers, so a 1-frame first block gives ``b == 1``.
    * a source waypoint exists at ``b`` that is not ``_eph_root_ctx``
      (fabricated per-pin pelvis snapshots must not be spread across a seam)
      and carries exactly one waypoint -- the same single-waypoint guard
      :func:`_assign_root_path_headings` uses. This client emits one
      single-frame ``root_path`` per waypoint deliberately, so a multi-frame
      wire is an unexpected shape: skip it rather than copy the wrong position.
    * no ``root_path`` of any origin already sits at ``b - 1`` -- two wires on
      one index is a silent last-wins collision with a user pin.

    Copies carry the private ``_seam_dup`` mark: they are budget-exempt in
    :func:`_validate_constraint_count` (the user authored one waypoint, not two)
    and stripped in ``build_request``'s ship-clean loop before sending. They
    carry no ``heading_radians`` -- user wires have none at this point, and the
    gated :func:`_assign_root_path_headings` runs later and picks them up.
    """
    if len(segments) < 2:
        return

    boundaries: list[int] = []
    cum = 0
    for s in segments[:-1]:              # drop the final total: not a seam
        cum += int(s.get("duration_frames") or 0)
        boundaries.append(cum)

    def _frames(c: dict[str, Any]) -> list[int]:
        return [int(f) for f in (c.get("frames") or [])]

    copies: list[dict[str, Any]] = []
    for b in boundaries:
        if b - 1 <= 0:                   # never index 0 (kills the frame-0 anchor)
            continue
        # Checked against the copies too: adjacent boundaries can be one frame
        # apart when a middle segment is 1 frame long.
        if any(c.get("type") == "root_path" and (b - 1) in _frames(c)
               for c in (constraints + copies)):
            continue
        src = next(
            (c for c in constraints
             if c.get("type") == "root_path"
             and not c.get("_eph_root_ctx")
             and len(c.get("positions_xz") or []) == 1
             and b in _frames(c)),
            None,
        )
        if src is None:
            continue
        copies.append({
            "type":         "root_path",
            "frames":       [b - 1],
            # Copy the position, never alias it: a later mutation of one wire
            # must not reach the other.
            "positions_xz": [list(src["positions_xz"][0])],
            "_seam_dup":    True,
        })
    constraints.extend(copies)


def _assign_root_path_headings(
    constraints: list[dict[str, Any]],
    manual_headings: dict[int, float] | None = None,
) -> None:
    """Stamp ``heading_radians`` on each single-waypoint ``root_path`` wire from
    the path geometry (face toward the *next* waypoint). Mutates in place.

    Step 2 / "Option A": body facing == travel direction. For travel delta
    ``(dx, dz)`` to the next waypoint, ``heading = atan2(dx, dz)`` -- the inverse
    of the MMCP facing ``f(h) = (+sin h, cos h)`` (0 = +Z, +X -> +pi/2). The X sign
    was confirmed empirically on 2026-06-03: a live generate with the earlier
    ``(-sin h, cos h)`` form faced the character to the MIRRORED left/right side
    (forward/back correct), so the X component was flipped to match the server.
    Operates on the WIRE dicts, never on ``marker.value`` -- this is
    the SOLE path heading reaches the wire, so when the caller doesn't run it (gate
    off) nothing leaks (see project_heading_viz_wire_coupling). Origin-offset is
    already applied to ``positions_xz`` but headings are deltas, so it's irrelevant.

    Guards mirror the front-cross viz (``constraint_viz._path_facings``) so the
    visible front and the wire never disagree: <2 waypoints -> no trajectory
    heading; a coincident step (|delta| < 1e-6) reuses the previous heading
    (never the false ``atan2(0,0) = 0`` = +Z); the last waypoint reuses the
    previous heading. The frame-0 anchor, when present, is just the earliest
    waypoint and gets face-toward-waypoint-1 by the same rule.

    Step 3 -- ``manual_headings`` maps a waypoint *frame* to a user-aimed heading
    (radians; captured by rotating the root2d circle, see constraint_viz). It is
    consulted ONLY for the **last** waypoint's frame (the single/last waypoint has
    no "next", so trajectory can't define it). Every other waypoint is recomputed
    from geometry regardless of any stale value, so a waypoint demoted from last
    to intermediate (user added a point after it) loses its old manual aim
    automatically -- the map is never read for a non-last frame.
    """
    manual = manual_headings or {}
    # Ephemeral root-context waypoints (_eph_root_ctx) are excluded on BOTH
    # sides: their captured real heading must never be overwritten by geometry,
    # and they are body-position snapshots, not authored path points -- letting
    # them into the face-toward-next chain would bend user waypoint headings
    # toward wherever the pelvis happened to be at a pin frame.
    waypoints = sorted(
        (
            c for c in constraints
            if c.get("type") == "root_path"
            and not c.get("_eph_root_ctx")
            and len(c.get("positions_xz") or []) == 1
        ),
        key=lambda c: (c.get("frames") or [0])[0],
    )
    if not waypoints:
        return
    last_frame = (waypoints[-1].get("frames") or [0])[0]
    prev_heading: float | None = None
    for i, c in enumerate(waypoints):
        frame = (c.get("frames") or [0])[0]
        # Manual override applies only to the current last (== single) waypoint.
        if frame == last_frame and frame in manual:
            heading: float | None = float(manual[frame])
        else:
            nxt = waypoints[i + 1] if i + 1 < len(waypoints) else None
            heading = prev_heading  # last (no manual) reuses prev; lone wp -> None
            if nxt is not None:
                x0, z0 = c["positions_xz"][0]
                x1, z1 = nxt["positions_xz"][0]
                dx, dz = x1 - x0, z1 - z0
                if math.hypot(dx, dz) >= 1e-6:
                    heading = math.atan2(dx, dz)
        if heading is not None:
            c["heading_radians"] = [float(heading)]
            prev_heading = heading


def _collect_manual_path_headings(
    character_state: Any,
    frame_range: tuple[int, int],
    anchor_markers: list[Any] | None = None,
    root_yaw: float | None = None,
    frame_offset: int = 0,
    *,
    total_frames: int | None = None,
) -> dict[int, float]:
    """Map ``reframed_frame -> aim_heading_radians`` from root2d markers.

    Mirrors :func:`_collect_constraints`' frame filter + reframe against the
    same absolute base (``frame - (frame_range[0] + frame_offset)``, clamped at
    0) so keys line up with the ``root_path`` wire dicts at any take start.
    ``aim_heading_radians`` is the user's manual aim (set by rotating the circle
    in the viewport, captured in constraint_viz); it lives in a field the wire
    emitter never reads, so it only reaches the wire via the gated
    :func:`_assign_root_path_headings`. Anchor markers carry no aim.

    The reframe MUST stay byte-identical to :func:`_collect_constraints`'
    ``_reframe`` -- upper clamp to ``total_frames - 1`` BEFORE the 0 floor. A
    waypoint authored at the inclusive upper bound reframes to ``total_frames``;
    the wire dict gets clamped there but a key filed unclamped here would never
    match it, so ``_assign_root_path_headings``' ``frame == last_frame`` test
    misses and the user's manual aim silently degrades to geometry facing.
    ``total_frames=None`` keeps the historical (unclamped) keys.
    """
    if character_state is None:
        return {}
    fr_lo, fr_hi = frame_range
    base = fr_lo + frame_offset   # absolute base, matching _collect_constraints
    hi = fr_hi + frame_offset
    out: dict[int, float] = {}
    for marker in list(getattr(character_state, "constraints", []) or []):
        if getattr(marker, "type", None) != "root2d":
            continue
        f = int(getattr(marker, "frame", 0))
        if f < base or f > hi:
            continue
        value = getattr(marker, "value", None) or {}
        aim = value.get("aim_heading_radians")
        if aim is not None:
            rel = f - base
            if total_frames is not None:
                rel = min(rel, total_frames - 1)
            # aim is a WORLD heading; relativize to the canonical frame the same way
            # positions_xz are (apply rotates facing by +root_yaw → subtract it here).
            out[max(0, rel)] = float(aim) - (float(root_yaw) if root_yaw is not None else 0.0)
    return out


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
    """Reject constraints that target unknown joints. Animatica wire types only.

    ``effector_target`` is strict (the user deliberately picks one joint, so a
    bad name is a real authoring error). ``pose_keyframe`` extras are pruned
    upstream by :func:`_prune_pose_keyframe_joints`, not rejected here -- the
    scene rig's SOMA-77 carries leaf/finger joints the server's canonical
    skeleton omits, and a full-body capture would otherwise always fail.
    """
    for i, c in enumerate(constraints):
        if c.get("type") == "effector_target":
            joint = c.get("joint", "")
            if joint not in canonical_joint_names:
                raise BuildError(
                    f"Constraint #{i} (effector_target) targets unknown joint {joint!r}. "
                    f"Allowed joints come from /capabilities.models[].canonical_skeleton.joints[].name"
                )


def _prune_pose_keyframe_joints(
    constraints: list[dict[str, Any]],
    canonical_joint_names: set[str],
    warnings: list[str] | None,
) -> list[dict[str, Any]]:
    """Drop ``pose_keyframe`` rotations for joints absent from the server's
    canonical skeleton, returning the filtered constraint list.

    This mirrors Kimodo's own reduction: per the Kimodo constraints docs,
    SOMA constraints are *authored/displayed* on the full ``somaskel77`` rig
    (fingers/toes/face) but the model consumes the reduced ``somaskel30`` body
    skeleton, so finger/toe/face joints are not predicted and carry no usable
    constraint signal. The scene rig is the 77-joint superset; ``/capabilities``
    publishes the reduced canonical set. Prune the extras with a soft warning
    rather than failing the request. A ``pose_keyframe`` left with no known
    joints is dropped entirely.
    Ref: research.nvidia.com/labs/sil/projects/kimodo (constraints user guide).
    """
    pruned: list[dict[str, Any]] = []
    for c in constraints:
        if c.get("type") != "pose_keyframe":
            pruned.append(c)
            continue
        rots = c.get("joint_rotations") or {}
        dropped = sorted(set(rots) - canonical_joint_names)
        if dropped:
            kept = {k: v for k, v in rots.items() if k in canonical_joint_names}
            c = {**c, "joint_rotations": kept}
            if warnings is not None:
                frame = c.get("frame")
                warnings.append(
                    f"pose_keyframe @ frame {frame}: reduced to the model's canonical "
                    f"skeleton -- kept {len(kept)} body joint(s), dropped {len(dropped)} "
                    f"finger/toe/face joint(s) the model doesn't predict (e.g. {dropped[:3]})."
                )
        if not (c.get("joint_rotations") or {}):
            if warnings is not None:
                warnings.append(
                    f"pose_keyframe @ frame {c.get('frame')}: no canonical joints "
                    f"remain after pruning -- constraint skipped."
                )
            continue
        pruned.append(c)
    return pruned


def _validate_constraint_count(
    constraints: list[dict[str, Any]],
    model_caps: dict[str, Any],
) -> None:
    # Two ceilings, deliberately different.
    #
    # The BUDGET is what the user is charged. Ephemeral root-context waypoints
    # (_eph_root_ctx) and seam copies (_seam_dup) are synthesized, not
    # user-authored — like the frame-0 anchor they never charge it (the anchor
    # achieves this by injecting after this check). A seam copy in particular
    # restates ONE authored waypoint on the other side of a segment boundary and
    # carries no timeline marker: charging it would fail a request that shipped
    # yesterday, with a message naming nothing the user can act on.
    #
    # The CEILING is what actually reaches the server, which enforces its own
    # max — and _seam_dup is the one exempt class that SCALES. The anchor is +1
    # and bounded; _eph_root_ctx is gated OFF and its overshoot is pinned as
    # shipped behaviour (tests/test_request_builder_effector_root_ctx.py
    # test_injected_waypoint_is_budget_exempt), so both stay fully exempt here.
    # A default-ON pass adding up to N-1 wires on an N-block layout is different
    # in kind: unchecked, the request passes this gate and is rejected
    # server-side, relocating the very failure the exemption exists to prevent.
    # So copies stay off the user's BUDGET (the message above names things the
    # user can act on) while still counting toward the ceiling below, whose
    # message names the copies as the cause and offers the toggle.
    limits = model_caps.get("limits") or {}
    cap = int(limits.get("max_constraints_per_request") or 0)
    if not cap:
        return

    n = sum(1 for c in constraints if not (c.get("_eph_root_ctx") or c.get("_seam_dup")))
    if n > cap:
        raise BuildError(
            f"{n} constraints exceeds the model's max of {cap} "
            f"(disable some keyframes / drop an effector pin)"
        )

    dups = sum(1 for c in constraints if c.get("_seam_dup"))
    if dups and n + dups > cap:
        raise BuildError(
            f"{n + dups} constraints would be sent, exceeding the model's max "
            f"of {cap}: {n} of yours plus {dups} automatic seam copy(ies) at "
            f"prompt-block boundaries. Drop a keyframe or a boundary waypoint, "
            f"or turn off Settings -> \"Duplicate path waypoints at block "
            f"seams\"."
        )


# Kimodo soft guidance: constrained frames per type "should be < 20, excluding
# root path". Over this the server tends to blur or silently drop the extras.
PER_TYPE_FRAME_SOFT_CAP = 20


def _warn_per_type_cap(constraints: list[dict[str, Any]]) -> list[str]:
    """Soft, non-fatal check of Kimodo's per-type <20-frame guidance.

    Tallies constrained frame *count* per wire ``type``, excluding ``root_path``
    (the trajectory pin is exempt). Returns one warning string per type that
    reaches :data:`PER_TYPE_FRAME_SOFT_CAP`. The hard total cap stays in
    :func:`_validate_constraint_count`.
    """
    counts: dict[str, int] = {}
    for c in constraints:
        t = c.get("type")
        if t == "root_path" or not t:
            continue
        # Tally constrained *frames*, not dicts: post-grouping a single
        # effector_target carries many frames (`frames` array), and a
        # pose_keyframe has a singular `frame` (no `frames` -> counts as 1).
        counts[t] = counts.get(t, 0) + len(c.get("frames") or [c])

    warnings: list[str] = []
    for t in sorted(counts):
        n = counts[t]
        if n >= PER_TYPE_FRAME_SOFT_CAP:
            warnings.append(
                f"{n} '{t}' constraint frames — Kimodo guidance is "
                f"<{PER_TYPE_FRAME_SOFT_CAP} per type (excluding root path); "
                f"the server may blur or ignore the extras."
            )
    return warnings


def _warn_uncovered_effector_pins(
    constraints: list[dict[str, Any]],
    suppressed_frames: set[int] | None = None,
) -> list[str]:
    """Three-tier soft warning for hand/foot pins vs the server's fabricated body.

    The hosted server's ``effector_target`` translation fabricates a full
    rest-pose body around a position-only pin (``_build_end_effector_dict``),
    so a hand/foot pin sent bare also hard-pins pelvis XZ, pelvis height and
    body facing from a T-pose the user never authored — the slide/push-back
    mechanism (confirmed n=3 via ``scripts/measure_effector_pins.py``).
    :func:`_collect_constraints` now auto-injects a same-frame ``root_path``
    waypoint with the rig's REAL pelvis XZ + heading for pins whose marker
    carries the captured context. Each pin frame classifies three ways:

    * **user-covered** — the frame appears in a non-ephemeral ``root_path``'s
      ``frames`` or equals a ``pose_keyframe``'s singular ``frame``: silent
      (the user authored root/body data there).
    * **auto-covered** — the frame appears only in an ephemeral injected
      waypoint (``_eph_root_ctx``): one downgraded info note — real XZ +
      facing rode along, but pelvis height is still approximated server-side
      and the injection rides measured-not-guaranteed dedup ordering.
    * **suppressed** — the frame is in ``suppressed_frames``: the marker DOES
      carry captured context but the "Send real body context with hand/foot
      pins" setting is off, so nothing was injected. Its own info note naming
      the setting as the cause — never the recreate-the-pin hint, which is a
      dead end for a pin that already has what it needs.
    * **bare** — no coverage and no captured context (pre-change markers): the
      full T-pose degradation warning, with a recreate-the-pin hint.

    ``suppressed_frames`` is the out-param :func:`_collect_constraints` fills
    when its ``inject_root_context`` gate is off, in REFRAMED space (the same
    space as ``effector_target["frames"]``). ``None`` (the default) means the
    gate was on, so no frame classifies suppressed.

    Runs POST-context-injection (classification reads the ``_eph_root_ctx``
    mark, still present here) but PRE-frame-0-anchor: the anchor is a plain
    ``root_path`` appended last that cannot protect a frame-0 pin, so it must
    not count as user coverage.

    It runs POST-seam-duplication too, and a ``_seam_dup`` copy counts as
    NEITHER tier. The copy lands on ``b - 1`` because a boundary needed
    steering, not because the user pinned that frame — it carries XZ only, no
    heading — so letting it reach ``covered_user`` would silence this warning
    outright for a pin sitting one frame before a seam, and ``covered_auto``
    would claim facing was sent when it was not. Skipping it classifies such a
    pin exactly as it classified before the copy existed.

    The cost of that choice is one clause: with a copy present there IS a
    ``root_path`` on the pin's frame carrying real XZ, so the ``bare`` and
    ``supp_pins`` texts say "no path waypoint YOU AUTHORED" and hedge the XZ
    half of the T-pose fabrication rather than asserting it flatly. Height and
    facing — the part that actually degrades torso motion — are unaffected by a
    copy and are still stated plainly.

    Pure, no mutation; returns 0-3 strings, severity-ordered (the full ``bare``
    warning first, then the two info tiers), mirroring
    :func:`_warn_per_type_cap`'s shape. Refs: `scripts/measure_effector_pins.py`;
    context/changes/effectors-hands-legs-check-usecurrentposition-ground/frame.md;
    context/changes/effector-pin-real-root-context/plan.md;
    context/changes/constraint-request-match-to-mmcp-convetion/plan.md.
    """
    supp = suppressed_frames or set()
    covered_user: set[int] = set()
    covered_auto: set[int] = set()
    for c in constraints:
        t = c.get("type")
        if t == "root_path":
            if c.get("_seam_dup"):       # a seam copy is steering, not coverage:
                continue                 # XZ only, no heading, and the user never
                                         # authored a waypoint on THIS frame
            tgt = covered_auto if c.get("_eph_root_ctx") else covered_user
            tgt.update(int(f) for f in (c.get("frames") or []))
        elif t == "pose_keyframe" and c.get("frame") is not None:
            covered_user.add(int(c["frame"]))

    bare: list[str] = []
    auto: list[str] = []
    supp_pins: list[str] = []
    for c in constraints:
        if c.get("type") != "effector_target":
            continue
        joint = c.get("joint") or "?"
        for f in (c.get("frames") or []):
            fi = int(f)
            if fi in covered_user:
                continue
            label = f"{joint} @ frame {fi}"
            if fi in covered_auto:
                auto.append(label)
            elif fi in supp:
                supp_pins.append(label)
            else:
                bare.append(label)

    warnings: list[str] = []
    if bare:
        shown = bare[:6]
        more = f" (+{len(bare) - len(shown)} more)" if len(bare) > len(shown) else ""
        warnings.append(
            f"{len(bare)} hand/foot pin(s) carry no body context and no "
            f"path waypoint you authored on their frame: {', '.join(shown)}"
            f"{more}. The server synthesizes a rest-pose body around a "
            f"position-only effector pin, so each such pin also hard-pins "
            f"pelvis height and body facing from a T-pose — and pelvis XZ too, "
            f"unless a path waypoint happens to cover the frame — torso motion may degrade "
            f"near these frames. Recreate the pin to capture and send the "
            f"rig's real body context. Full Body and Path pins are unaffected."
        )
    if auto:
        shown = auto[:6]
        more = f" (+{len(auto) - len(shown)} more)" if len(auto) > len(shown) else ""
        warnings.append(
            f"Real pelvis position and facing were auto-sent with {len(auto)} "
            f"hand/foot pin(s): {', '.join(shown)}{more}. Pelvis height is "
            f"still approximated by the server, and pin accuracy may drop a "
            f"few centimetres as the limb must genuinely reach from the real "
            f"body. (Server-side constraint precedence is measured, not "
            f"guaranteed.)"
        )
    if supp_pins:
        shown = supp_pins[:6]
        more = (
            f" (+{len(supp_pins) - len(shown)} more)"
            if len(supp_pins) > len(shown) else ""
        )
        warnings.append(
            f"{len(supp_pins)} hand/foot pin(s) carry real body context that was "
            f"NOT sent: {', '.join(shown)}{more}. Settings -> \"Send real body "
            f"context with hand/foot pins\" is off (the default), so the server "
            f"fabricates pelvis height and body facing from a T-pose at these "
            f"frames — and pelvis XZ too, unless a path waypoint happens to "
            f"cover the frame. Turn the setting on to send the rig's real pelvis "
            f"position and facing along with each pin."
        )
    return warnings


def _warn_starved_segments(
    constraints: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> list[str]:
    """Soft warning for a prompt block that carries no trajectory steering.

    Boundary duplication (:func:`_duplicate_seam_waypoints`) fixes the common
    case, but the underlying rule is broader: the model crops constraints **per
    segment** (kimodo/constraints.py:125, half-open ``crop_move``), so a segment
    with no authored ``root_path`` waypoint anywhere inside its own frame range
    has nothing to steer toward and invents its whole path. Pins at 0 / 60 / 250
    across a 0-123 / 123-250 layout leave the SECOND block empty, and no seam
    copy helps -- the nearest waypoint isn't on a boundary. Detect and say so;
    never synthesize a waypoint the user didn't author.

    **The threshold is zero, not "fewer than two."** A segment holding exactly
    one authored waypoint knows where it must be and when -- it lacks
    intermediate shaping, not steering. Warning at ``< 2`` would fire on the
    minimal reasonable layout (pin the start, pin the end, two blocks), flagging
    BOTH segments on a request nothing here can improve, which is exactly the
    false-positive noise that makes a soft warning stop being read.

    "Authored" excludes the two synthesized origins: ``_eph_root_ctx``
    (fabricated per-pin pelvis context) and ``_auto_anchor`` (the injected
    frame-0 canonicalization waypoint) -- without that distinction a text-only
    generation, whose only wire IS the anchor, would look pinned. ``_seam_dup``
    copies DO count: the copy carries the user's own XZ and genuinely steers, so
    a boundary pin spread across a seam must silence this warning on both sides.

    A ``pose_keyframe`` carrying ``root_position`` counts as steering too -- a
    Full Body pin tells the model where the root must be and when, which is what
    "steering" means here, and every other site in this module already treats it
    as root coverage (:func:`_pins_frame_zero`,
    :func:`_warn_uncovered_effector_pins`). Counting only ``root_path`` would
    warn "no path waypoint" at a block whose end is pinned by a full pose.

    Returns at most one string. Silent unless the request has more than one
    segment AND carries at least one authored ``root_path`` anywhere -- a
    text-only generation must not warn on every block.

    Block ranges come from the cumsum of ``duration_frames``, which is the space
    the model actually crops in, and it is expected to cover every authored
    frame: boxes in a group are contiguous with ``duration_frames = end -
    start`` (gui/tool_window.py:2352), so the cumsum equals the reframed span
    that :func:`_collect_constraints` clamps into. An authored frame past the
    last block's end is therefore out of contract -- it would be dropped here
    and report the trailing block starved. The one known way to break the
    identity is a group holding OVERLAPPING or nested boxes, which
    :func:`group_contiguous_boxes` admits and which already misaligns every
    constraint in such a request by the same concatenation; that is pre-existing
    and broader than this warning. :func:`_duplicate_seam_waypoints` carries the
    same cumsum dependency.
    """
    if len(segments) < 2:
        return []

    authored: list[int] = []
    for c in constraints:
        t = c.get("type")
        if t == "root_path":
            if c.get("_eph_root_ctx") or c.get("_auto_anchor"):
                continue
            authored.extend(int(f) for f in (c.get("frames") or []))
        elif t == "pose_keyframe" and c.get("root_position") is not None:
            if c.get("frame") is not None:
                authored.append(int(c["frame"]))
    if not authored:                     # text-only: nothing was pinned at all
        return []

    starved: list[str] = []
    lo = 0
    for i, s in enumerate(segments):
        hi = lo + int(s.get("duration_frames") or 0) - 1   # inclusive end
        if not any(lo <= f <= hi for f in authored):
            # Ordinal only, deliberately. `lo`/`hi` are REFRAMED indices: on a
            # take that doesn't start at 0, or in the gap fan-out (one request
            # per group), "frames 0-39" names a span the user cannot find on
            # the timeline. The 1-based block number they can.
            starved.append(f"block {i + 1}")
        lo = hi + 1

    if not starved:
        return []
    return [
        f"{len(starved)} prompt block(s) carry no path waypoint or Full Body "
        f"pin: {', '.join(starved)}. The model steers each block only by the "
        f"constraints inside its own frame range, so these blocks invent their "
        f"own trajectory and may drift from the path you drew. Add a waypoint "
        f"inside each one."
    ]


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def _build_guidance(cfg_type: str, cfg_text_weight: float, cfg_constraint_weight: float):
    """Pack CFG weights for ``options.guidance``.

    ``weight`` length must match ``type``: 0 -> nocfg (key omitted),
    1 -> regular, 2 -> separated.
    """
    if cfg_type == "nocfg":
        return None
    if cfg_type == "regular":
        return {"type": "regular", "weight": [float(cfg_text_weight)]}
    return {
        "type":   "separated",
        "weight": [float(cfg_text_weight), float(cfg_constraint_weight)],
    }


def _clamp_num_samples(requested: int, max_num_samples: int | None) -> int:
    """Floor ``requested`` at 1, then clamp to a present int ``max_num_samples``.

    Mirrors ``maya_kimodo/generator.py:377-384``: when the requested count
    exceeds the advertised cap the result becomes ``max(1, cap)`` -- so a
    degenerate cap of ``0`` floors to 1 (never sends ``0``), matching the sister
    project. A ``None`` or non-int cap is treated as "no ceiling" and ignored.
    """
    effective = max(1, int(requested))
    if max_num_samples is not None:
        try:
            cap = int(max_num_samples)
        except (TypeError, ValueError):
            cap = None
        if cap is not None and effective > cap:
            effective = max(1, cap)
    return effective


def build_options(
    state: Any, *, num_segments: int = 0, max_num_samples: int | None = None,
) -> dict[str, Any]:
    """Assemble the ``options`` block from AppState (or a settings shim).

    Reads from any object exposing the attributes:
        steps, num_samples, seed, random_seed, post_processing,
        transition_frames, cfg_type, cfg_text_weight, cfg_constraint_weight

    ``num_segments`` controls whether ``transition_frames`` is emitted (the
    server only honors it across multi-segment requests).

    ``max_num_samples`` is the model's advertised ceiling
    (``limits.max_num_samples`` from ``/capabilities``); ``num_samples`` is
    floored at 1 and clamped to it when it is a positive int.
    """
    steps = int(getattr(state, "steps", 100))

    # Existing Take merges into the current take, so samples cannot fan into
    # distinct takes -- force 1 regardless of UI state (mirrors the gap-group
    # override in tool_window._on_generate_clicked). The model-cap clamp in
    # _clamp_num_samples still applies on top for Story/New Take.
    requested_samples = getattr(state, "num_samples", 1)
    if getattr(state, "animation_mode", "") == "existing_take":
        requested_samples = 1

    opts: dict[str, Any] = {
        "diffusion_steps": steps,
        "num_samples":     _clamp_num_samples(requested_samples, max_num_samples),
        "post_processing": bool(getattr(state, "post_processing", True)),
    }

    if num_segments > 1:
        opts["transition_frames"] = int(getattr(state, "transition_frames", 5))

    if not bool(getattr(state, "random_seed", False)):
        seed = int(getattr(state, "seed", 0))
        if seed > 0:
            opts["seed"] = seed

    guidance = _build_guidance(
        getattr(state, "cfg_type", "nocfg"),
        getattr(state, "cfg_text_weight", 2.0),
        getattr(state, "cfg_constraint_weight", 2.0),
    )
    if guidance is not None:
        opts["guidance"] = guidance
    return opts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _normalize_segments(
    raw: list[dict[str, Any]],
    *,
    supports_segment_seed: bool = False,
) -> list[dict[str, Any]]:
    """Normalize caller-supplied ``{"text","duration_frames"}`` segments to wire shape.

    Drops empty-text segments, emits
    ``{"type":"text","prompt":<stripped>,"duration_frames":max(1,int)}``.
    Preserves the caller's duration values verbatim (no ``+1`` reframing).

    A caller-supplied per-segment ``seed`` survives only when the model
    advertises ``supports_segment_seed`` — same gate as
    :func:`build_segments`, since older servers reject unknown segment
    fields (``extra="forbid"``).
    """
    out: list[dict[str, Any]] = []
    for s in raw:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        seg: dict[str, Any] = {
            "type":            "text",
            "prompt":          text,
            "duration_frames": max(1, int(s["duration_frames"])),
        }
        if supports_segment_seed:
            seed = s.get("seed")
            if isinstance(seed, int) and not isinstance(seed, bool) and seed > 0:
                seg["seed"] = seed
        out.append(seg)
    return out


def build_request(
    *,
    state: Any,
    character_state: Any,
    model_caps: dict[str, Any],
    skeleton_override: dict[str, Any] | None = None,
    seed_override: int | None = None,
    segments_override: list[dict[str, Any]] | None = None,
    origin_offset: tuple[float, float, float] | None = None,
    root_yaw: float | None = None,
    anchor_markers: list[Any] | None = None,
    warnings: list[str] | None = None,
    frame_offset: int = 0,
    frame_range_override: tuple[int, int] | None = None,
    suppress_pose_frames: Iterable[int] | None = None,
    warn_excluded: bool = True,
    root_anchor_xz: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Build the full MMCP ``/generate`` request body.

    ``state`` is :class:`animatica_core.core.prompt_model.AppState`
    (or any object exposing the same attributes for headless tests).
    ``character_state`` is the active :class:`CharacterState` (may be ``None``
    when there are no constraints to collect, e.g. block regen).
    ``model_caps`` is one entry of ``GET /capabilities .models[]``.

    Overrides preserve the live callers' behavior without re-deriving it:
        ``skeleton_override``  — in-scene wire skeleton (else canonical)
        ``seed_override``      — random/explicit seed the caller computed
        ``segments_override``  — raw ``{"text","duration_frames"}`` segments,
                                 normalized here (bypasses ``build_segments``);
                                 ``frame_range`` stays prompt-derived (via
                                 ``compute_frame_range``) unless
                                 ``frame_range_override`` is also supplied, which
                                 the live path does to align the constraint base
                                 with the segment set (see below)
        ``origin_offset``      — character scene world pos (meters, ``(x,y,z)``)
                                 at the prompt start frame; subtracted IN FULL
                                 (Y included — off_y, the ground fold) from
                                 every absolute marker position so constraints
                                 match the origin-canonical trajectory (Step 9
                                 P1-FIX; pose Y since fix-bugs-ivestigate-
                                 problems item 1). Must equal the offset
                                 re-added on apply.
        ``root_yaw``           — rig frame-0 world yaw (radians). When set, every
                                 position-bearing constraint (and the pose root quat)
                                 is pre-rotated by ``Ry(−root_yaw)`` so apply's flat
                                 ``Ry(root_yaw)`` cancels and constrained frames land on
                                 their scene markers. Must equal the apply ``root_yaw``.
                                 (Solution 1, plans/melodic-tickling-wind.md)
        ``root_anchor_xz``     — the injected frame-0 root anchor's XZ, in the
                                 WIRE frame, taken verbatim: NOT origin_offset-
                                 subtracted, NOT root_yaw-rotated (those apply
                                 to authored markers only). None (default) keeps
                                 the canonical rest anchor ``[0, 0]`` and a
                                 byte-identical request. Only the injected
                                 anchor changes -- a user pin at frame 0 still
                                 suppresses it, the ``_auto_anchor`` mark and
                                 every warning stay as they are. Hosts whose
                                 convention is "start from where the character
                                 stands" (Blender, Max -- Matt, 2026-09-02)
                                 pass the root's world XZ at the start frame.
        ``anchor_markers``     — ephemeral per-request markers (not persisted on
                                 the character): the ``pin_start`` full-pose
                                 anchor at the prompt-start frame, folded into the
                                 constraint pipeline so the model gets a proper
                                 kinematic start pose (else a lone far pose pin
                                 makes the whole body glide to it). See
                                 we-still-have-problem-sunny-narwhal.md.
        ``warnings``           — optional out-param; non-fatal soft-cap warnings
                                 are appended for the caller to surface
        ``frame_offset``       — the take's start frame (absolute); used solely to
                                 relativize absolute constraint frames against the
                                 prompt-derived (take-local) ``frame_range`` — the
                                 gate/relativization base is ``frame_range[0] +
                                 frame_offset``. Default 0 preserves legacy
                                 behavior. Constraint *positions* are unaffected
                                 (those relativize via ``origin_offset``/``root_yaw``);
                                 segment math stays take-local. Must equal the
                                 apply-side offset (small-fixes #16, commit e84600d).
        ``frame_range_override`` — take-local (prompt-derived) frame range of the
                                 blocks that actually produce segments; replaces the
                                 ``compute_frame_range`` result so the constraint
                                 relativization base matches the live segment set.
                                 Default ``None`` preserves legacy behavior (range
                                 over all prompts). Combine with ``frame_offset`` for
                                 the absolute base. Fixes the empty-leading-block
                                 mismatch where ``compute_frame_range`` (all prompts)
                                 and the GUI's non-empty ``included`` set disagree
                                 (constraint-filter-unify, F1).
        ``suppress_pose_frames`` — ABSOLUTE frames whose *user* fullbody markers
                                 are dropped in favor of a same-frame synthesized
                                 ``anchor_markers`` pose (Generate Selected's
                                 Keep-start/Keep-end anchors win at the block
                                 boundaries). Explicit and type-aware — never
                                 append-order-based — so the request can't carry
                                 two ``pose_keyframe``s for one frame. Effector
                                 pins at those frames are untouched. See
                                 :func:`_collect_constraints`.
        ``warn_excluded``      — False silences the out-of-span summary warning
                                 for multi-request fanouts (the gap-group queue),
                                 where a sibling group's markers are excluded here
                                 but sent by another request. Those callers warn
                                 once against the union of every span.

    The ground fold has no separate kwarg: it rides inside ``origin_offset``'s
    Y, which every position-bearing constraint (pose root Y included) subtracts
    in full — the exact inverse of apply's ``+off_y`` re-seat
    (fix-bugs-ivestigate-problems item 1; the old ``ground_y_m`` kwarg from
    ground-offset-to-constraint-fix is superseded and removed).

    Raises ``BuildError`` if the state can't form a valid request.
    """
    if not model_caps:
        raise BuildError("model_caps is empty — call /capabilities first")

    model_id = model_caps.get("id") or ""
    if not model_id:
        raise BuildError("model_caps has no 'id'")

    canonical = model_caps.get("canonical_skeleton") or {}
    canonical_joint_names = {j.get("name") for j in canonical.get("joints", []) if j.get("name")}
    if not canonical_joint_names:
        raise BuildError(f"Model {model_id!r} has no canonical_skeleton.joints")

    # Root joint name for pose_keyframe root-quat relativization (Solution 1): the
    # parentless joint of the skeleton actually sent (override else canonical), with a
    # first-joint fallback (SOMA root = Hips, index 0). Only consulted when root_yaw is set.
    root_joint_name: str | None = None
    if root_yaw is not None:
        _sk_joints = (skeleton_override or canonical).get("joints") or []
        for _j in _sk_joints:
            if not _j.get("parent"):
                root_joint_name = _j.get("name")
                break
        if root_joint_name is None and _sk_joints:
            root_joint_name = _sk_joints[0].get("name")

    prompts = list(getattr(character_state, "prompts", []) or [])
    fallback = (int(getattr(state, "start_frame", 0)),
                int(getattr(state, "start_frame", 0)) + int(getattr(state, "total_frames", 1)) - 1)
    if fallback[1] < fallback[0]:
        fallback = (fallback[0], fallback[0])

    if frame_range_override is not None:
        try:
            _fr_lo, _fr_hi = (int(frame_range_override[0]), int(frame_range_override[1]))
        except (TypeError, ValueError, IndexError):
            raise BuildError(
                "frame_range_override must be a 2-tuple of ints (lo, hi)"
            )
        if _fr_hi < _fr_lo:
            raise BuildError(
                f"frame_range_override hi ({_fr_hi}) < lo ({_fr_lo})"
            )
        frame_range = (_fr_lo, _fr_hi)
    else:
        frame_range = compute_frame_range(prompts, fallback)
    # Per-segment seeds only reach the wire when the model says it reads them
    # (see build_segments) — flat boolean on the /capabilities model entry,
    # read like supports_retargeting.
    supports_segment_seed = bool(model_caps.get("supports_segment_seed"))
    if segments_override is not None:
        segments = _normalize_segments(
            segments_override, supports_segment_seed=supports_segment_seed)
    else:
        segments = build_segments(
            prompts, frame_range, supports_segment_seed=supports_segment_seed)
    total_frames = (
        sum(s["duration_frames"] for s in segments)
        if segments
        else (frame_range[1] - frame_range[0] + 1)
    )
    if total_frames < 1:
        raise BuildError("Frame range is empty")

    # Settings -> "Send real body context with hand/foot pins" (default OFF since
    # 2026-07-27 -- the 8dea32c injection is the opt-in A/B lever, not the
    # baseline). The getattr fallback mirrors the AppState default so a
    # duck-typed state object that predates the field behaves like a fresh one.
    suppressed_ctx_frames: set[int] = set()
    constraints = _collect_constraints(
        character_state, frame_range, origin_offset, anchor_markers,
        root_yaw=root_yaw, root_joint_name=root_joint_name,
        frame_offset=frame_offset, total_frames=total_frames,
        suppress_pose_frames=suppress_pose_frames, warnings=warnings,
        warn_excluded=warn_excluded,
        inject_root_context=getattr(state, "send_effector_root_context", False),
        suppressed_ctx_frames=suppressed_ctx_frames,
        # Settings -> "Debug: send effector rotations" (default OFF): gates the
        # optional `rotations` array on effector wires. Same getattr-with-
        # default pattern as the two gates around it.
        send_rotations=getattr(state, "debug_send_effector_rotations", False),
        # Settings -> "Path waypoint wins over same-frame hand/foot pins"
        # (default ON = the shipped 8dea32c ordering): gates the kept+moved
        # same-frame reorder; OFF restores the pre-8dea32c walk order.
        reorder_same_frame=getattr(state, "reorder_same_frame_waypoints", False),
    )

    if not segments and not constraints:
        raise BuildError(
            "No prompts or constraints to generate from. "
            "Add a prompt block on the timeline or place a constraint marker."
        )

    # Settings -> "Duplicate path waypoints at block seams" (default ON): copy
    # a boundary waypoint onto the preceding segment's last index so both blocks
    # around a seam are steered to the same XZ. Load-bearing placement: AFTER
    # _collect_constraints (the pass reads REFRAMED indices, which is the space
    # the segment cumsum lives in) and BEFORE the prune/joint/count checks, so
    # copies traverse them exactly like every other wire -- the budget exemption
    # is handled inside _validate_constraint_count, not by skipping the checks.
    # The getattr fallback mirrors the AppState default so a duck-typed state
    # predating the field behaves like a fresh one; flip both together.
    if getattr(state, "duplicate_seam_waypoints", True):
        _duplicate_seam_waypoints(constraints, segments)

    constraints = _prune_pose_keyframe_joints(constraints, canonical_joint_names, warnings)
    _validate_constraint_joints(constraints, canonical_joint_names)
    _validate_constraint_count(constraints, model_caps)

    if warnings is not None:
        warnings.extend(_warn_per_type_cap(constraints))
        # Load-bearing placement: AFTER the ephemeral root-context injection
        # (classification reads the _eph_root_ctx mark, still present here)
        # but BEFORE the frame-0 anchor below — the anchor is a plain
        # root_path appended last that cannot protect a frame-0 effector pin,
        # so it must not count as user coverage.
        warnings.extend(
            _warn_uncovered_effector_pins(constraints, suppressed_ctx_frames)
        )

    # Frame-0 root anchor (canonicalization convention). Kimodo expects the
    # trajectory anchored at frame 0 (root at XZ origin); without it the
    # character can land at an arbitrary world position. Mirror Proscenium and
    # inject [0,0] when the user hasn't pinned frame 0 themselves. Gate on
    # _pins_frame_zero (frame 0 *specifically*) so a root2d/pose at frame 60
    # still gets anchored. Added after validation so it doesn't count against
    # the user's max_constraints budget.
    #
    # This anchor is REQUIRED even for a pose_keyframe carrying root_position
    # (10.8 -- reverts the 10.6 suppression). A lone pose at frame K with a
    # non-origin root_position but no frame-0 reference gives the model nothing
    # to start from, so it satisfies "be at the target at frame K" trivially by
    # sitting at the target from frame 0 -> a stationary clip ("stays in the
    # pose, barely moves"). The anchor is animatica's equivalent of maya_kimodo's
    # pin_start: its single-block path auto-injects a frame-0 anchor pose
    # (gui/main_window.py:3094 _make_pin_anchors) and canonicalizes every
    # snapshot's root XZ to origin (:3124 _canonicalize_snapshots_to_origin), so
    # the model always gets the "start at origin, walk to the pose" pair. The
    # 10.6 "slide to pose then walk" symptom that justified suppression predates
    # Branch A (bridge/animator.py self-correcting -gen0 XZ transport), which
    # now pins the start independently -- so re-adding the anchor is safe.
    #
    # Debug toggle (Settings -> "Debug: omit frame-0 root anchor"): suppress the
    # injection to A/B what it actually does. Note every rationale above is
    # PINNED-case evidence -- the pin-free, text-only branch has never been
    # measured. Applies to both branches, so with user waypoints at frames > 0
    # it also changes what _assign_root_path_headings sees below.
    if not getattr(state, "debug_omit_root_anchor", False):
        if not any(_pins_frame_zero(c) for c in constraints):
            constraints.append({
                "type":         "root_path",
                "frames":       [0],
                "positions_xz": [[float(root_anchor_xz[0]),
                                  float(root_anchor_xz[1])]
                                 if root_anchor_xz is not None else [0.0, 0.0]],
                # Private mark: lets _warn_starved_segments tell this injected
                # canonicalization waypoint from a user pin that happens to sit
                # at origin on frame 0. Nothing else reads it -- _pins_frame_zero
                # and _assign_root_path_headings key off type/frames/positions_xz,
                # and the anchor is already budget-exempt by injecting after
                # _validate_constraint_count -- so adding it changes no counts.
                # Stripped in the ship-clean loop below.
                "_auto_anchor": True,
            })

    # Load-bearing placement: AFTER both the seam duplication and the frame-0
    # anchor injection, because each changes the per-segment counts this reads --
    # a seam copy can be the only authored waypoint in a block (it must silence
    # the warning), and the anchor must be present-and-marked so a block covered
    # by nothing else is still reported as starved. Also BEFORE the ship-clean
    # loop, which pops the _auto_anchor mark it classifies on.
    if warnings is not None:
        warnings.extend(_warn_starved_segments(constraints, segments))

    # Step 2/3 A/B gate: derive per-waypoint body heading from the path geometry
    # and send it on the wire (Settings -> "Send path heading"). Default off --
    # heading-on-wire is unproven (maya_kimodo sends none). Done AFTER anchor
    # injection so the anchor (= earliest waypoint) also gets face-toward-next,
    # and AFTER the count/cap checks so it can't change constraint counts (it
    # only annotates existing root_path wires, adds none). The single/last
    # waypoint has no "next", so it takes the user's manual aim (Step 3) when
    # one was captured by rotating the circle. The front-cross viz is always on;
    # only the WIRE heading is gated here.
    if getattr(state, "send_path_heading", False):
        manual = _collect_manual_path_headings(
            character_state, frame_range, anchor_markers, root_yaw=root_yaw,
            frame_offset=frame_offset, total_frames=total_frames,
        )
        _assign_root_path_headings(constraints, manual)

    # Ship-clean: the three private marks are client-internal (budget / warning
    # / heading bookkeeping above) — _eph_root_ctx (fabricated per-pin pelvis
    # context), _seam_dup (boundary copy) and _auto_anchor (the injected frame-0
    # canonicalization waypoint). Strip them so no private field reaches the
    # server, which may reject unknown keys. Last constraint mutation before the
    # request is assembled.
    for c in constraints:
        c.pop("_eph_root_ctx", None)
        c.pop("_seam_dup", None)
        c.pop("_auto_anchor", None)

    # The model's native fps is authoritative -- the server does NOT resample
    # (MMCP v1) and rejects a mismatched timing.fps. Send the model fps from
    # /capabilities (never hardcoded; DEFAULT_FPS is only a malformed-caps
    # fallback), never the scene/display fps. Mirrors maya_kimodo, which reads
    # fps only from the model spec. Ref: animatica.ai/mmcp/docs/sdk.
    #
    # The rejection is MEASURED, not assumed (2026-08-08, via the
    # debug_send_scene_fps probe below): a 24 fps timing against a 30 fps model
    # came back "timing.fps=24.0 does not match model fps=30.0; server-side
    # resampling is not implemented in v1". This claim dated to May 2026
    # (f3b6ec1) and had been unverified until then.
    #
    # TODO(fps-resample): to honor an arbitrary scene fps we must resample our
    # authored frame indices client-side -- segment durations AND constraint
    # frames -- from scene_fps to model fps before sending (scale by
    # model_fps/scene_fps), then invert on apply. Until then we send model fps
    # as-is and warn on mismatch. NOTE this is now the ONLY available route --
    # the probe above settled that waiting for server-side resampling is not an
    # option in v1, so the warning is the sole mitigation that exists today.
    fps = model_caps.get("fps") or DEFAULT_FPS
    scene_fps = getattr(state, "fps", None)
    if warnings is not None and scene_fps and float(scene_fps) != float(fps):
        if getattr(state, "match_scene_fps", False):
            # The frame-placement guarantee below is true but under-describes
            # the defect. Segment durations are authored in SCENE frames and
            # sent unscaled, then consumed as MODEL frames -- so each prompt is
            # generated for scene_fps/model_fps of the real duration the user
            # laid out (80 % at 24 fps against a 30 fps model). That changes
            # what motion comes back, not where its keys land, and it is the
            # part the shipped wording omitted.
            pct = float(scene_fps) / float(fps) * 100.0
            warnings.append(
                f"Scene is {float(scene_fps):g}fps but the model generates at "
                f"{float(fps):g}fps. 'Match scene FPS' is on, so the motion is "
                f"reinterpreted on apply -- authored frames & constraints stay 1:1; "
                f"the clip's real-time pace will follow {float(scene_fps):g}fps. "
                f"Note each block's length is sent as a raw scene-frame count and "
                f"read as model frames, so it is generated for ~{pct:g}% of the "
                f"real duration you laid out -- expect the motion's character to "
                f"differ (a faster, more compressed action), not its frame "
                f"placement. (No server-side resampling in v1.)"
            )
        else:
            warnings.append(
                f"Scene is {float(scene_fps):g}fps but the model generates at "
                f"{float(fps):g}fps, and 'Match scene FPS' is off -- authored "
                f"frames/constraints will land early/late on apply. Enable 'Match "
                f"scene FPS' or set the scene to {float(fps):g}fps to keep timing 1:1."
            )

    # Clamp num_samples to the model's advertised ceiling (never send more than
    # the server can honor). Warn softly when we clamp. Mirrors maya_kimodo.
    limits = model_caps.get("limits") or {}
    max_num_samples = limits.get("max_num_samples")
    options = build_options(
        state, num_segments=len(segments), max_num_samples=max_num_samples,
    )
    if warnings is not None:
        requested = max(1, int(getattr(state, "num_samples", 1)))
        if options["num_samples"] < requested:
            warnings.append(
                f"num_samples={requested} exceeds the model's max of "
                f"{options['num_samples']}; clamped to {options['num_samples']}."
            )
    if seed_override is not None:
        options["seed"] = int(seed_override)

    request: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "model":            model_id,
        "skeleton":         skeleton_override or canonical,
        "options":          options,
    }

    if segments:
        request["segments"] = segments
    else:
        request["duration_frames"] = total_frames

    request["constraints"] = constraints
    # Debug toggle: omit the timing block to probe server behavior without an
    # explicit fps (Settings -> "Debug: omit timing block"). Normally required.
    #
    # Second, narrower probe (Settings -> "Debug: send scene FPS in timing"):
    # ship the SCENE fps instead of the model fps. omit_timing keeps precedence
    # -- it is the stronger experiment and the two are not composable. ONLY this
    # value changes; segment durations and constraint frames stay unscaled, so
    # an accepted request whose clip runs the authored real duration means the
    # server resampled. Ref: prompt_model.debug_send_scene_fps (research Q6).
    if not getattr(state, "debug_omit_timing", False):
        wire_fps = int(fps)
        if getattr(state, "debug_send_scene_fps", False) and scene_fps and float(scene_fps) > 0:
            wire_fps = int(float(scene_fps))
            if warnings is not None and wire_fps != int(fps):
                # The fps-mismatch warning above fires on the same condition and
                # its ADVICE conflicts with the probe: matching the scene to the
                # model removes the very mismatch this measures. Both statements
                # stay true (they describe apply-side behaviour, which the probe
                # doesn't touch), so say which one to act on rather than
                # suppressing either.
                warnings.append(
                    f"Debug: sending timing.fps={wire_fps} (the scene fps) "
                    f"instead of the model's {int(fps)}. This is an experiment "
                    f"-- the server may reject the request outright, or accept "
                    f"it and pace the clip differently. Block durations and "
                    f"constraint frames are NOT rescaled. The fps-mismatch "
                    f"warning above still describes what happens on apply, but "
                    f"do NOT act on its advice while probing: matching the "
                    f"scene to {int(fps)}fps removes the mismatch this test "
                    f"needs. Turn off 'Debug: send scene FPS in timing' to "
                    f"restore normal behaviour."
                )
        request["timing"] = {"fps": wire_fps}
        _check_duration_limit(request, model_caps)

    _inject_default_walk_path(request, model_caps, warnings,
                              getattr(state, "walk_speed_ms", None))
    return request


#: Straight-line default for a model that needs a trajectory. 1.4 m/s is
#: the Live Drive speed field's default — the same walk the operator gets
#: from holding the forward arrow.
DEFAULT_WALK_SPEED_MS = 1.4


def _inject_default_walk_path(request, model_caps, warnings=None,
                              speed_ms=None):
    """Give a trajectory-driven model a forward walk when none was drawn.

    ARDY steers from a root trajectory: in Live Drive every window carries
    one, built from the direction keys, which is why the character travels
    there. A batch request without any Path pin carries no trajectory at
    all, and the model answers with a walk almost in place — measured, 8 cm
    over 99 frames, on a request whose only constraint was the start pose.
    Kimodo travels from the text alone and gets nothing added.

    The synthesised path is what holding the forward arrow produces: a
    straight line down +Z (the MMCP forward axis, matching
    ``live.controls.DIRECTION_VECTORS["forward"]``) at
    :data:`DEFAULT_WALK_SPEED_MS`, sampled once per second so the model
    keeps the shape without being pinned frame by frame.

    Does nothing when the request already has a ``root_path`` — a drawn
    path is the operator's intent and must win.
    """
    if not _model_needs_trajectory(model_caps):
        return
    constraints = request.get("constraints") or []
    if any((c or {}).get("type") == "root_path" for c in constraints):
        return

    fps = float((request.get("timing") or {}).get("fps")
                or model_caps.get("fps") or 0.0)
    frames = sum(int(seg.get("duration_frames") or 0)
                 for seg in (request.get("segments") or []))
    if not frames:
        frames = int(request.get("duration_frames") or 0)
    if fps <= 0 or frames <= 1:
        return

    speed = float(speed_ms if speed_ms else DEFAULT_WALK_SPEED_MS)
    step = max(1, int(round(fps)))          # one waypoint per second
    sampled = list(range(0, frames, step))
    if sampled[-1] != frames - 1:
        sampled.append(frames - 1)
    request["constraints"] = constraints + [{
        "type": "root_path",
        "frames": sampled,
        "positions_xz": [[0.0, speed * (f / fps)] for f in sampled],
    }]
    if warnings is not None:
        warnings.append(
            f"No Path drawn, so {model_caps.get('id') or 'this model'} was "
            f"given a straight walk forward at {speed:g} m/s "
            "(it steers from a trajectory and would otherwise walk in "
            "place). Draw a Path to choose the route."
        )


def _model_needs_trajectory(model_caps):
    """Does this model steer from a root trajectory rather than from text?

    True for the models the realtime stream can drive: that stream exists
    precisely because they take a per-window root trajectory. There is no
    capability flag for this yet, so the streamable set is the honest
    proxy — better than matching on the id's spelling, and it fails safe
    (an unknown model gets nothing added).
    """
    model_id = str((model_caps or {}).get("id") or "").lower()
    return model_id in _TRAJECTORY_DRIVEN_MODELS


#: Kept beside the server's STREAMABLE_MODEL_IDS; both describe the same
#: property (autoregressive, trajectory-steered) from opposite sides.
_TRAJECTORY_DRIVEN_MODELS = frozenset({"ardy-core-rp"})


def _check_duration_limit(request, model_caps):
    """Raise before sending a clip the model is documented to refuse.

    Models differ by a factor of four here — Kimodo allows 30 s, ARDY 8 —
    and neither chunks (``chunking: none``), so a long block is simply not
    generatable by the shorter model. The server rejects it cleanly
    ("total duration 12.00s exceeds max_duration_seconds 8.0"), but only
    after the round trip, and in seconds — while the operator is looking
    at frames on a timeline. Same arithmetic as the server's, on the body
    about to be sent, so the two cannot drift apart.
    """
    limit = (model_caps.get("limits") or {}).get("max_duration_seconds")
    fps = (request.get("timing") or {}).get("fps")
    if not limit or not fps:
        return
    frames = sum(int(s.get("duration_frames") or 0)
                 for s in (request.get("segments") or []))
    if not frames:
        frames = int(request.get("duration_frames") or 0)
    if not frames:
        return
    seconds = frames / float(fps)
    if seconds <= float(limit) + 1e-6:
        return
    model_id = model_caps.get("id") or "this model"
    raise BuildError(
        f"{seconds:.1f}s of motion exceeds what {model_id} can generate in "
        f"one request ({limit:g}s at {fps:g}fps = {int(float(limit) * fps)} "
        f"frames, and it does not chunk). Shorten the prompt block(s) or "
        f"pick a model with a longer limit."
    )


# ---------------------------------------------------------------------------
# Single-pose entry point (Generate Pose at Current Frame)
# ---------------------------------------------------------------------------

def build_pose_request(
    *,
    state: Any,
    model_caps: dict[str, Any],
    prompt: str,
    skeleton_override: dict[str, Any] | None = None,
    seed_override: int | None = None,
    force_fallback: bool = False,
) -> dict[str, Any]:
    """Build a minimal single-pose MMCP ``/generate`` request.

    Capability-adaptive: emits the official ``PoseSegment``
    (``{"type":"pose","prompt":...}`` with no ``duration_frames``) when the
    model advertises ``"pose"`` in ``supported_segments``; otherwise falls back
    to maya_kimodo's known-safe 2-frame ``text`` segment with
    ``post_processing=False`` (``T=1`` crashes the denoiser --
    ``maya_kimodo/generator.py:285-313``).

    Unlike :func:`build_request`, this deliberately bypasses the frame-0
    ``root_path`` anchor, ``transition_frames``, and all constraint collection --
    none apply to a lone pose. ``force_fallback`` forces the 2-frame path; the
    finish handler uses it to auto-degrade when an official 1-frame response
    fails to parse.

    ``model_caps`` is one entry of ``GET /capabilities .models[]``;
    ``skeleton_override`` is the in-scene wire skeleton (else canonical);
    ``seed_override`` is the random/explicit seed the caller computed.

    Raises ``BuildError`` on an empty ``prompt`` or malformed ``model_caps``.
    """
    text = (prompt or "").strip()
    if not text:
        raise BuildError("Pose prompt is empty -- enter a prompt to generate a pose.")

    if not model_caps:
        raise BuildError("model_caps is empty — call /capabilities first")
    model_id = model_caps.get("id") or ""
    if not model_id:
        raise BuildError("model_caps has no 'id'")
    canonical = model_caps.get("canonical_skeleton") or {}
    canonical_joint_names = {j.get("name") for j in canonical.get("joints", []) if j.get("name")}
    if not canonical_joint_names:
        raise BuildError(f"Model {model_id!r} has no canonical_skeleton.joints")

    use_pose = (not force_fallback) and ("pose" in model_supported_segments(model_caps))

    # num_segments=1 -> build_options omits transition_frames (it gates on >1).
    options = build_options(state, num_segments=1)
    # Heterogeneous values (str prompt/type + int duration_frames) — annotate so
    # mypy doesn't infer dict[str, str] from the pose branch and reject the int.
    segments: list[dict[str, Any]]
    if use_pose:
        segments = [{"type": "pose", "prompt": text}]
    else:
        # 2-frame fallback: T=1 crashes the denoiser, and post-processing is
        # forced off (state value overridden) per maya_kimodo's single_pose path.
        segments = [{"type": "text", "prompt": text, "duration_frames": 2}]
        options["post_processing"] = False
    if seed_override is not None:
        options["seed"] = int(seed_override)

    fps = model_caps.get("fps") or DEFAULT_FPS
    request: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "model":            model_id,
        "skeleton":         skeleton_override or canonical,
        "segments":         segments,
        "options":          options,
        "timing":           {"fps": int(fps)},
    }
    return request
