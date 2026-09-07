"""Pure constraint-frame reconcile helpers (DCC-agnostic, no pyfbsdk).

When a prompt block is moved or resized on the timeline, its interior
constraint pins should travel with it. The frame math is pure and unit-testable,
so it lives here rather than inline in ``gui/tool_window.py`` (which only maps the
live ``ConstraintMarker.frame`` values in and out).

All frames are **absolute** (displayed) scene frames — the same space
``ConstraintMarker.frame`` lives in (see ``core/prompt_model.py``). Spans are
given as inclusive ``[lo, hi]`` pairs matching a block's ``[start, end]``.

promptboxes-animlayers-update Phase 4 (items 1, 6/7).
"""

from __future__ import annotations


def reconcile_pins_on_moves(
    frames: list[int], moves: list[tuple[int, int, int]]
) -> list[int]:
    """Shift each pin by the first whole-block move whose span contains it.

    *frames* is a list of absolute pin frames; *moves* is a list of
    ``(old_lo, old_hi, delta)`` tuples (inclusive absolute spans + the frame
    delta the block moved by). A pin is claimed by the **first** move whose old
    span contains it (``old_lo <= f <= old_hi``) and shifted by that delta;
    remaining moves are skipped for that pin. This "claim-once" rule keeps a pin
    that sits exactly on the shared edge of two touching blocks from being
    shifted twice. Pins outside every move span are returned unchanged.

    Returns a new list; the input is not mutated.
    """
    out = []
    for f in frames:
        nf = f
        for old_lo, old_hi, delta in moves:
            if old_lo <= f <= old_hi:
                nf = f + delta
                break
        out.append(nf)
    return out


def reconcile_pins_on_resize(
    frames: list[int], old_lo: int, old_hi: int, new_lo: int, new_hi: int
) -> list[int]:
    """Map pins inside ``[old_lo, old_hi]`` linearly into ``[new_lo, new_hi]``.

    Each pin's fractional position within the old span is preserved in the new
    span (rounded to the nearest frame), then clamped inside ``[new_lo, new_hi]``.
    Pins outside the old span are returned unchanged. A degenerate old span
    (``old_hi <= old_lo``) collapses every interior pin onto the clamped new
    range. Returns a new list; the input is not mutated.
    """
    old_span = old_hi - old_lo
    new_span = new_hi - new_lo
    out = []
    for f in frames:
        if not (old_lo <= f <= old_hi):
            out.append(f)
            continue
        if old_span <= 0:
            out.append(max(new_lo, min(f, new_hi)))
            continue
        t = (f - old_lo) / old_span
        nf = new_lo + int(round(t * new_span))
        out.append(max(new_lo, min(nf, new_hi)))
    return out
