"""Prev/next constraint-frame navigation (Step 7e).

Pure DCC-agnostic; no ``pyfbsdk`` import. The tool window calls these to
resolve a target frame given the active character's constraints and the
current playhead frame, then dispatches the seek to
``bridge.time_bridge.goto_frame``.

UX: wrap-around. Prev from at/before the first authored frame returns the
last; next from at/after the last returns the first. Empty list returns
``None`` so the caller can log instead of seeking. Mirrors
``maya_kimodo`` ``cons_nav_prev/next`` behavior.
"""

from __future__ import annotations

from typing import Iterable


def _sorted_unique(frames: Iterable[int]) -> list[int]:
    return sorted({int(f) for f in frames})


def next_frame(frames: Iterable[int], current: int) -> int | None:
    """Return the smallest constraint frame strictly greater than *current*,
    or wrap to the first frame. ``None`` if *frames* is empty."""
    sorted_frames = _sorted_unique(frames)
    if not sorted_frames:
        return None
    for f in sorted_frames:
        if f > current:
            return f
    return sorted_frames[0]


def prev_frame(frames: Iterable[int], current: int) -> int | None:
    """Return the largest constraint frame strictly less than *current*, or
    wrap to the last frame. ``None`` if *frames* is empty."""
    sorted_frames = _sorted_unique(frames)
    if not sorted_frames:
        return None
    for f in reversed(sorted_frames):
        if f < current:
            return f
    return sorted_frames[-1]
