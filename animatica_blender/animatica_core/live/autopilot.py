"""Steer the character toward a goal point. Pure Python, unit-tested.

The GUI drops a ``ardylive:Goal`` null in the scene; the operator drags it
around and the section feeds its position here once per received packet.
The result is the same ``(move_dir, speed)`` pair the arrow keys produce,
so the autopilot needs no special path through the client.

Braking profile (metres from the goal):

    > ARRIVE_SLOW      cruise speed
    ARRIVE_SLOW..STOP  linear ramp down to SLOW_SPEED
    < ARRIVE_STOP      stop

Stopping and starting use different radii (``ARRIVE_STOP`` vs
``RESTART_DIST``): with a single threshold the character oscillates on the
spot, stepping in and out of the stop zone forever.
"""

from __future__ import annotations

import math

# Radii are sized against the CONTROL LATENCY, not against what looks
# tight on paper: a command only takes effect on the next generated window
# (0.4 s for core8) and the client adds its playback buffer, so at 1.4 m/s
# the character covers ~0.6-1.0 m between "stop" and actually stopping.
# Radii smaller than that make it orbit the goal forever (measured).
ARRIVE_SLOW = 2.5      # m — start braking
ARRIVE_STOP = 0.8      # m — close enough, stop
RESTART_DIST = 1.2     # m — must drift this far out again before restarting
SLOW_SPEED = 0.6       # m/s — speed at the edge of the stop zone


def goal_control(root_xz, goal_xz, cruise_speed, arrived=False):
    """``(move_dir, speed, arrived)`` for one control update.

    *arrived* carries the previous call's verdict — that is what makes the
    stop/restart hysteresis work; pass it back in each time.
    """
    dx = float(goal_xz[0]) - float(root_xz[0])
    dz = float(goal_xz[1]) - float(root_xz[1])
    dist = math.hypot(dx, dz)

    if arrived:
        if dist < RESTART_DIST:
            return (0.0, 1.0), 0.0, True        # hold; goal has not moved
        arrived = False                          # goal moved away: go again

    if dist <= ARRIVE_STOP:
        return (0.0, 1.0), 0.0, True

    direction = (dx / dist, dz / dist)
    if dist >= ARRIVE_SLOW:
        speed = float(cruise_speed)
    else:
        # linear between SLOW_SPEED (at the stop radius) and cruise
        span = max(1e-6, ARRIVE_SLOW - ARRIVE_STOP)
        t = (dist - ARRIVE_STOP) / span
        speed = SLOW_SPEED + (float(cruise_speed) - SLOW_SPEED) * t
        speed = max(min(speed, float(cruise_speed)), SLOW_SPEED)
    return direction, speed, False
