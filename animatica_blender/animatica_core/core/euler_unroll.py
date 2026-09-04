"""Per-axis Euler continuity (DCC-agnostic).

Pure Python floats -- no ``pyfbsdk``, no ``numpy``. Fixes the one-frame
±360° Euler flip: MoBu's ``FBQuaternionToRotation`` returns a *principal-range*
Euler triple per frame, so two adjacent-but-continuous rotations can map to
Euler values differing by ~360° on an axis. Baking each frame independently
therefore lands a single frame on the far side of a wrap -- a visible
one-frame spin that MoBu's "Unroll Rotation" filter repairs post-hoc.

The apply loop calls :func:`unroll_triple` against each joint's *previous
already-unrolled* Euler, so continuity accumulates rather than snapping back
into the principal range each frame (see the "Unroll baseline must accumulate"
note in the Phase 2 plan).

This is representation-only: ``unroll_axis(prev, raw)`` returns a value
congruent to ``raw`` modulo 360°, so the rotation it denotes is unchanged.
"""

import math


def unroll_axis(prev: float, raw: float) -> float:
    """Shift ``raw`` by the multiple of 360° that minimises the jump from ``prev``.

    Returns ``raw + 360 * round((prev - raw) / 360)`` -- a value congruent to
    ``raw`` mod 360° that lands nearest ``prev``. When the two are already within
    180° this is a no-op (``round`` of a sub-0.5 magnitude is 0), so continuous
    input is byte-preserved.

    Non-finite input (NaN/inf, e.g. from a degenerate rotation matrix) is passed
    through unchanged -- ``round()`` would otherwise raise and abort the whole
    apply bake mid-transaction; passing ``raw`` straight through preserves the
    pre-unroll behaviour of handing the value on to ``KeyAdd``.
    """
    delta = prev - raw
    if not math.isfinite(delta):
        return raw
    return raw + 360.0 * round(delta / 360.0)


def unroll_triple(
    prev: tuple[float, float, float],
    raw: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Per-axis :func:`unroll_axis` over an ``(rx, ry, rz)`` Euler triple.

    Each axis is unrolled independently against its own previous value; a wrap
    on one axis never perturbs the others.
    """
    return (
        unroll_axis(prev[0], raw[0]),
        unroll_axis(prev[1], raw[1]),
        unroll_axis(prev[2], raw[2]),
    )
