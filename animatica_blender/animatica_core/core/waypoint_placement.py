"""Where a newly added ``root2d`` waypoint should sit.

Pure DCC-agnostic; no host import. The first waypoint is sampled from the rig,
because it is anchoring the path to where the character actually stands. Every
one after that has neighbours, and the neighbours say far more about where the
new pin belongs than the rig does -- on a rig with no motion yet, sampling puts
every pin in the same spot, and the operator has to drag each one out of the
pile before it means anything.

The rule follows the timeline, not the geometry:

* **between two waypoints** -- interpolate by frame. A pin dropped a third of
  the way between two others starts a third of the way along, which is where
  the path already goes.
* **before the earliest, or after the latest** -- continue the path outward
  from the nearest one, keeping the direction the path already has there. An
  extension of a walk is a step further along it, not a step sideways.
* **only one waypoint exists** -- there is no direction to continue, so offset
  along the character's forward axis (+Z in the y-up, right-handed frame the
  wire speaks).

Returns ``None`` when there is nothing to place against, which tells the caller
to sample the rig exactly as it always has.
"""

from __future__ import annotations

import math

#: Metres between a new waypoint and the one it is placed beside. Roughly a
#: stride: close enough to read as the same path, far enough that the two
#: markers do not overlap in the viewport.
DEFAULT_SPACING_M = 0.75

_FORWARD = (0.0, 1.0)          # +Z in y-up; ``xz`` pairs are (x, z)


def _normalise(dx, dz):
    length = math.hypot(dx, dz)
    if length < 1e-9:
        return None
    return dx / length, dz / length


def place_new_waypoint(frame, existing, *, spacing_m=DEFAULT_SPACING_M):
    """``(x, z)`` in metres for a new waypoint at *frame*, or ``None``.

    *existing* is ``[(frame, (x, z))]`` for the waypoints already authored, in
    any order. A waypoint already at *frame* is ignored -- adding one there
    replaces it, and its own position must not decide its replacement's.
    """
    others = sorted(((int(f), (float(p[0]), float(p[1])))
                     for f, p in (existing or []) if int(f) != int(frame)),
                    key=lambda item: item[0])
    if not others:
        return None

    frame = int(frame)
    before = [item for item in others if item[0] < frame]
    after = [item for item in others if item[0] > frame]

    if before and after:
        (f0, (x0, z0)), (f1, (x1, z1)) = before[-1], after[0]
        span = f1 - f0
        # Guarded even though the sort makes f1 > f0: a caller passing two
        # waypoints on one frame would otherwise divide by zero, and the
        # midpoint is the honest answer when there is no span to walk along.
        t = (frame - f0) / span if span else 0.5
        return (x0 + (x1 - x0) * t, z0 + (z1 - z0) * t)

    # Outside the authored span: continue outward from the nearest waypoint.
    if after:                                   # the new one is the earliest
        anchor, neighbour = after[0], (after[1] if len(after) > 1 else None)
    else:                                       # the new one is the latest
        anchor, neighbour = before[-1], (before[-2] if len(before) > 1 else None)

    (ax, az) = anchor[1]
    direction = None
    if neighbour is not None:
        # Away from the neighbour, so the path keeps going the way it was
        # heading rather than doubling back on itself.
        direction = _normalise(ax - neighbour[1][0], az - neighbour[1][1])
    if direction is None:
        direction = _FORWARD
        if after:
            # Extending backwards in time means stepping back along forward.
            direction = (-direction[0], -direction[1])
    return (ax + direction[0] * spacing_m, az + direction[1] * spacing_m)
