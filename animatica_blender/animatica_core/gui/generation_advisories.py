"""The verdicts a generation run reports back to the user, as pure functions.

Three checks ride along with every generation: is the ground correction allowed
to act (and if not, why not), do any authored pins fall in a gap no request will
cover, and is the Story export path usable. Each is a decision plus a sentence
explaining it — and each used to be a window method that could only be read by
running MotionBuilder, even though none of them touches a scene.

Every function here returns its message rather than logging it, so the caller
owns the console and the tests can assert on the exact wording a user sees.

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

import os

# ``ground_measure`` and ``request_builder`` both pull numpy, so they stay
# function-local: importing this module must stay as cheap as it was when
# these were window methods with the same lazy imports.


def ground_correction(motion_data: dict, *, enabled: bool):
    """Ground correction (m) to subtract from a sample's applied root Y.

    Returns ``(correction, message_or_None)``. The correction itself comes from
    :func:`~animatica_core.core.ground_measure.correction_from_summary` — the
    pure gate (setting ON + canonical provenance + std trust). Both its inputs
    ride on *motion_data*: the worker stamps ``skeleton_source`` (computed once
    at request time) and ``ground_summary`` (the probe) onto every sample.

    Returns ``0.0`` whenever the correction must not act, which keeps the apply
    path byte-identical to an uncorrected run. When the gate is ON, every skip
    produces a message naming its reason and every applied value one naming the
    amount, so a run is auditable against the probe's logged offset; when the
    gate is OFF the whole thing is silent.
    """
    from animatica_core.core import ground_measure
    summary = motion_data.get("ground_summary")
    source = motion_data.get("skeleton_source")
    corr = ground_measure.correction_from_summary(
        summary, skeleton_source=source, enabled=bool(enabled),
    )
    if not enabled:
        return 0.0, None
    if corr:
        return corr, (
            f"Ground correction: {corr:+.4f} m subtracted from the applied "
            f"root Y ({summary.get('joint')}, {summary.get('contact_source')}); "
            f"pinned poses shift by the same amount."
        )
    if source != "canonical":
        return corr, (
            "Ground correction skipped — response was retargeted (custom "
            "rig); only canonical-skeleton responses are corrected."
        )
    if not isinstance(summary, dict) or summary.get("offset_m") is None:
        return corr, (
            "Ground correction skipped — no ground measurement for this "
            "response."
        )
    if float(summary.get("std_m", 0.0)) > ground_measure.MAX_TRUSTED_STD_M:
        return corr, (
            f"Ground correction skipped — measurement unreliable "
            f"(std {float(summary.get('std_m', 0.0)):.4f} m > "
            f"{ground_measure.MAX_TRUSTED_STD_M} m trust gate)."
        )
    # trusted measurement of exactly 0.0 — nothing to subtract
    return corr, "Ground correction: measured offset is 0 — nothing to correct."


def constraints_outside_groups(markers, groups, frame_offset: int) -> str | None:
    """Name the pins no group in a gap fan-out will send, or return None.

    ``build_request`` warns about out-of-span markers per request, but a gap
    fan-out issues one request per group, so each group would name its siblings'
    markers as "not sent" while another request sends them. The queue therefore
    runs with ``warn_excluded=False`` and the real question — which markers fall
    in a gap, outside EVERY group — is answered once, here, against the union of
    the groups' absolute spans.

    *groups* is a list of lists of prompt boxes carrying take-LOCAL
    ``start``/``end``; *frame_offset* lifts them into the markers' absolute
    space. At most six markers are named, the rest counted.
    """
    from animatica_core.core.request_builder import MARKER_LABELS
    markers = list(markers or [])
    if not markers or not groups:
        return None
    spans = [
        (min(int(b.start) for b in g) + frame_offset,
         max(int(b.end) for b in g) + frame_offset)
        for g in groups
    ]
    outside = [
        m for m in markers
        if not any(lo <= int(getattr(m, "frame", -1)) <= hi for lo, hi in spans)
    ]
    if not outside:
        return None
    labels = [
        f"{MARKER_LABELS.get(getattr(m, 'type', None), getattr(m, 'type', None) or '?')} "
        f"@ frame {int(getattr(m, 'frame', -1))}"
        for m in outside[:6]
    ]
    more = f" (+{len(outside) - len(labels)} more)" if len(outside) > len(labels) else ""
    return (f"{len(outside)} constraint(s) sit in a gap between prompt blocks and "
            f"were not sent: {', '.join(labels)}{more}.")


def validate_story_path(path: str) -> tuple[bool, str]:
    """Check that *path* is a writable directory.

    Returns ``(True, "")`` when usable, else ``(False, reason)``.
    """
    if not path:
        return False, "path is empty"
    if not os.path.isdir(path):
        return False, f"not a directory: {path}"
    if not os.access(path, os.W_OK):
        return False, f"directory is not writable: {path}"
    return True, ""
