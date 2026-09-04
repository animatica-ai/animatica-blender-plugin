"""Optional on-disk capture of MMCP round-trips for offline diagnosis.

Pure-Python (no pyfbsdk) so it stays inside ``core/`` per the DCC-isolation
rule in CLAUDE.md.

Layout (one folder per generate run)::

    <debug_dir>/<UTC-timestamp>__<tag>/
        request.json    POST body
        response.json   raw glTF document (large; contains base64 buffers)
        motion.json     parse_gltf() output reduced to shapes + first-frame
                        samples — small but spot-checkable
        meta.json       url, mode, model id, retargeting flag, plugin
                        version, skeleton-block source, namespace, and the
                        wire-shaping settings in force for the run
        error.json      only on failure: server error envelope or
                        exception text

Filenames are stable so a third-party diff tool (Compare-It, ``git diff``,
``code --diff``) can be pointed at two session folders directly.
"""

from __future__ import annotations

import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any


_TAG_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify(tag: str, *, max_len: int = 40) -> str:
    s = _TAG_SAFE.sub("_", (tag or "").strip()).strip("_")
    return (s[:max_len] or "run").rstrip("_") or "run"


def open_session(*, debug_dir: str, tag: str) -> str | None:
    """Create a fresh session folder under ``debug_dir`` and return its path.

    Returns ``None`` when ``debug_dir`` is empty or the directory cannot
    be created. Never raises — debug capture must not be able to abort a
    generate run.
    """
    if not debug_dir:
        return None
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}__{_slugify(tag)}"
        path = os.path.join(os.path.abspath(debug_dir), name)
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return None


def write_json(session_dir: str | None, name: str, payload: Any) -> None:
    """Best-effort ``json.dump`` into ``<session_dir>/<name>``.

    Swallows IOErrors and serialization failures — debug capture must
    never break a generate run. A no-op when ``session_dir`` is ``None``.
    """
    if not session_dir:
        return
    try:
        path = os.path.join(session_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=_fallback_encoder)
    except (OSError, TypeError, ValueError):
        pass


def write_error(session_dir: str | None, exc: BaseException) -> None:
    """Persist an exception (with traceback) as ``error.json``."""
    if not session_dir:
        return
    payload: dict[str, Any] = {
        "type":    type(exc).__name__,
        "message": str(exc),
    }
    code = getattr(exc, "code", None)
    if code is not None:
        payload["code"] = code
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details:
        payload["details"] = details
    try:
        payload["traceback"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
    except Exception:
        pass
    write_json(session_dir, "error.json", payload)


# ---------------------------------------------------------------------------
# Crash breadcrumb — a single fixed-path "last reached phase" marker so an
# intermittent MotionBuilder hard-crash (process vanishes) is localizable to a
# phase/frame. A file written-and-closed survives a *process* crash (flushed to
# the OS cache on close), so rewriting at each phase boundary is crash-safe.
# Gated by the GUI behind Debug Capture; the WER .dmp captures the native
# faulting module independently. See context/changes/crash-check/.
# ---------------------------------------------------------------------------

_bc_path: str | None = None
_bc_armed: bool = False
_bc_last_write: float = 0.0
_bc_last_phase: str | None = None


def arm_breadcrumb(path: str, *, rescue: bool = True) -> None:
    """Arm the crash breadcrumb at the fixed *path* and write an initial marker.

    After arming, :func:`breadcrumb` / :func:`stamp_outcome` record the last
    reached phase to *path*; a crash leaves a discoverable ``in_progress``
    marker. Best-effort — never raises.

    *rescue* controls whether a lingering ``in_progress`` at *path* is preserved
    to ``<name>.crashed.json`` (see :func:`_preserve_prior_crash`). Pass
    ``rescue=False`` for an *intra-run* re-arm (e.g. the pose 2-frame fallback
    relaunch), where the prior ``in_progress`` is this same run's live marker —
    not a crash — so rescuing it would produce a false-positive crash file.
    """
    global _bc_path, _bc_armed, _bc_last_write, _bc_last_phase
    if rescue:
        _preserve_prior_crash(path)
    _bc_path = path
    _bc_armed = True
    _bc_last_write = 0.0
    _bc_last_phase = None
    _write_breadcrumb({"status": "in_progress", "phase": "armed"})


def _preserve_prior_crash(path: str) -> None:
    """Rescue a prior run's crash evidence before this run overwrites *path*.

    A lingering ``in_progress`` breadcrumb means the previous run never stamped
    a terminal outcome — i.e. the process vanished mid-run. Since the breadcrumb
    is a single fixed path, the next Generate would clobber it; rename it to
    ``<name>.crashed.json`` first so an intermittent crash survives a retry.
    Best-effort — never raises.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            prior = json.load(fh)
    except (OSError, ValueError):
        return
    if prior.get("status") != "in_progress":
        return
    try:
        root, ext = os.path.splitext(path)
        os.replace(path, root + ".crashed" + (ext or ".json"))
    except OSError:
        pass


def breadcrumb(phase: str, *, throttle: float = 0.2, **fields: Any) -> None:
    """Record the last reached *phase* (+ optional *fields*) to the armed file.

    No-op when disarmed. To stay cheap on a hot loop, a *same-phase* update is
    skipped when less than *throttle* seconds have passed since the last write;
    a phase **change** always writes. Never raises.
    """
    global _bc_last_write, _bc_last_phase
    if not _bc_armed:
        return
    now = time.time()
    if phase == _bc_last_phase and (now - _bc_last_write) < throttle:
        return
    _bc_last_phase = phase
    _bc_last_write = now
    payload: dict[str, Any] = {"status": "in_progress", "phase": phase}
    payload.update(fields)
    _write_breadcrumb(payload)


def stamp_outcome(status: str, **fields: Any) -> None:
    """Write a terminal *status* (``"completed"`` | ``"failed"``) and disarm.

    No-op when disarmed, so it is safe to call from overlapping cleanup paths
    (first call wins — e.g. an ``except`` stamps ``"failed"`` and a following
    ``finally`` ``"completed"`` no-ops). Never raises.
    """
    global _bc_armed
    if not _bc_armed:
        return
    payload: dict[str, Any] = {"status": status, "phase": _bc_last_phase or "done"}
    payload.update(fields)
    _write_breadcrumb(payload)
    _bc_armed = False


def disarm_breadcrumb() -> None:
    """Disarm the breadcrumb without writing a terminal status."""
    global _bc_armed
    _bc_armed = False


def _write_breadcrumb(payload: dict) -> None:
    """Best-effort write of the breadcrumb *payload* to the armed path."""
    if not _bc_path:
        return
    payload = dict(payload)
    payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
    try:
        os.makedirs(os.path.dirname(_bc_path), exist_ok=True)
        with open(_bc_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=_fallback_encoder)
    except (OSError, TypeError, ValueError):
        pass


def summarize_motion(motion_data: dict) -> dict:
    """Reduce ``parse_gltf`` output to a small, diffable JSON summary.

    Numpy arrays are flattened to ``shape`` + ``dtype`` plus the first
    frame's raw values so a reader can sanity-check the rest pose without
    paging through hundreds of frames. Joint names and FPS are echoed
    verbatim. ``foot_contacts`` (a bool array) is reduced to per-joint
    ``True`` counts instead of a frame dump.
    """
    out: dict[str, Any] = {}
    for key in ("fps", "num_frames", "num_joints", "joint_names"):
        if key in motion_data:
            out[key] = motion_data[key]

    arr_keys = ("local_rot_mats", "posed_joints")
    for key in arr_keys:
        arr = motion_data.get(key)
        if arr is None:
            continue
        try:
            shape = tuple(int(s) for s in arr.shape)
            dtype = str(arr.dtype)
            first = arr[0].tolist() if shape and shape[0] > 0 else []
        except Exception:
            shape, dtype, first = None, None, None
        out[key] = {
            "shape":       shape,
            "dtype":       dtype,
            "frame0":      first,
        }

    fc = motion_data.get("foot_contacts")
    if fc is not None:
        try:
            shape  = tuple(int(s) for s in fc.shape)
            dtype  = str(fc.dtype)
            counts = [int(n) for n in fc.sum(axis=0)]
            names  = motion_data.get("joint_names") or []
            true_counts: Any = (
                {n: c for n, c in zip(names, counts) if c}
                if len(names) == len(counts) else counts
            )
        except Exception:
            shape, dtype, true_counts = None, None, None
        out["foot_contacts"] = {
            "shape":       shape,
            "dtype":       dtype,
            "true_counts": true_counts,
        }

    extras = motion_data.get("hierarchy")
    if extras is not None:
        out["hierarchy"] = list(extras)
    rest = motion_data.get("rest_positions")
    if rest is not None:
        try:
            out["rest_positions"] = {k: list(v) for k, v in rest.items()}
        except Exception:
            pass
    return out


def _fallback_encoder(obj: Any):
    """``json.dump`` ``default=`` hook for the odd numpy scalar slipping through."""
    if hasattr(obj, "tolist"):
        try:
            return obj.tolist()
        except Exception:
            pass
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return repr(obj)
