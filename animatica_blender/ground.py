# SPDX-License-Identifier: GPL-3.0-or-later
"""The ground under the character: what the scene's surfaces say it stands on.

The model plans on one flat floor at Y=0. A rooftop chase is not that: the
character starts on one roof, jumps a gap, lands on another 0.6 m lower and
carries on. Without being told, the take lands and walks on at the old
height, in the air above the second roof; and the Autoposer holds the feet at
the armature's own base plane the same way.

So the surface under the character is read from the scene -- a ray straight
down onto the visible meshes, past the character's own body and control
shapes -- and

* goes to the server as an MMCP 1.3 ``ground_height`` constraint, points on
  the surfaces along the route the take is planned to follow (the key poses
  and waypoints), where the server says it understands one;
* puts a new waypoint on the roof it is placed over;
* gives the Autoposer its floor.

Points over a gap -- where the ray finds nothing to stand on near the route,
only a street far below -- are left out; the character is in the air there.
"""

from __future__ import annotations

import time

import bpy
from mathutils import Vector

#: How far above the hips the ray starts: a step up is found, a hat, a
#: washing line or a ceiling overhead is not.
RAY_HEADROOM = 0.3
#: How far down it looks before deciding there is nothing there.
RAY_DEPTH = 50.0
#: Ground this much lower than the route's ground either side, for a short
#: stretch, is a gap to jump, not a level to land on.
GAP_DEPTH = 2.0
GAP_MAX_METRES = 4.0
#: Something this much higher than the ground either side, for no longer
#: than this, is an obstacle on the roof, not a level.
OBSTACLE_HEIGHT = 0.25
OBSTACLE_MAX_METRES = 2.0
#: Sampling along the route, and either side of it.
SPACING = 0.05
LANE = 0.3
#: Ground within this of Y=0 everywhere changes nothing; it is not sent.
FLAT_EPS = 0.01


# ---------------------------------------------------------------------------
# One ray
# ---------------------------------------------------------------------------

def _excluded(arm) -> set:
    """Names of the objects that are the character, not the world: the rig,
    what it deforms or carries, and the shapes its controls are drawn with."""
    out = set()
    if arm is None:
        return out
    out.add(arm.name)
    for ob in bpy.data.objects:
        if ob.parent is arm:
            out.add(ob.name)
        for m in getattr(ob, "modifiers", ()):
            if m.type == 'ARMATURE' and getattr(m, "object", None) is arm:
                out.add(ob.name)
        # a prop the character carries or wears (a hat on a Child Of)
        for con in getattr(ob, "constraints", ()):
            if getattr(con, "target", None) is arm:
                out.add(ob.name)
    for pb in getattr(arm.pose, "bones", ()) if arm.type == 'ARMATURE' else ():
        if pb.custom_shape is not None:
            out.add(pb.custom_shape.name)
    return out


def _is_ground(ob, skip: set) -> bool:
    """A surface to stand on: a visible mesh drawn solid, not the character.
    A guide curve over a gap, a wireframe helper or a text label is not."""
    return (ob.type == 'MESH' and ob.name not in skip and ob.visible_get()
            and ob.display_type not in {'WIRE', 'BOUNDS'}
            and not ob.get("animatica_is_waypoint"))


def surface_below(scene, x: float, y: float, z_from: float, *, arm=None,
                  exclude: set | None = None) -> float | None:
    """World height of the first surface straight below ``(x, y, z_from)``
    that is not the character, or ``None`` if there is none within reach."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    skip = _excluded(arm) if exclude is None else exclude
    origin = Vector((x, y, z_from))
    down = Vector((0.0, 0.0, -1.0))
    reach = RAY_DEPTH
    for _ in range(16):                       # past at most this many excluded hits
        hit, loc, _n, _i, ob, _m = scene.ray_cast(depsgraph, origin, down, distance=reach)
        if not hit:
            return None
        if ob is not None and not _is_ground(ob, skip):
            step = (origin.z - loc.z) + 1e-4
            origin = Vector((x, y, loc.z - 1e-4))
            reach -= step
            if reach <= 0:
                return None
            continue
        return float(loc.z)
    return None


_CACHE: dict = {}
_CACHE_SECONDS = 0.5


def surface_below_cached(scene, x, y, z_from, *, arm=None):
    """``surface_below``, remembered for half a second at 1 cm: a live
    Autoposer solve asks for the same spot on every mouse move."""
    key = (scene.name, getattr(arm, "name", None), round(x, 2), round(y, 2), round(z_from, 1))
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < _CACHE_SECONDS:
        return hit[1]
    z = surface_below(scene, x, y, z_from, arm=arm)
    if len(_CACHE) > 512:
        _CACHE.clear()
    _CACHE[key] = (now, z)
    return z


# ---------------------------------------------------------------------------
# Along the route, for the request
# ---------------------------------------------------------------------------

def _anchors(constraints: list[dict], reference_z: float) -> list[tuple[int, float, float, float]]:
    """``(frame, x, y, z_ref)`` in Blender world space, one per frame the request
    puts the root somewhere: key poses (with their height) and waypoints."""
    from . import coords

    pts: dict[int, tuple[float, float, float]] = {}
    for c in constraints:
        kind = c.get("type")
        if kind == "pose_keyframe" and c.get("root_position") is not None:
            x, y, z = coords.mmcp_pos_to_blender(c["root_position"])
            pts[int(c["frame"])] = (x, y, z)
        elif kind == "root_path":
            for f, (mx, mz) in zip(c["frames"], c["positions_xz"]):
                x, y, _ = coords.mmcp_pos_to_blender((mx, 0.0, mz))
                pts.setdefault(int(f), (x, y, None))
    out = []
    last_z = reference_z
    for f in sorted(pts):
        x, y, z = pts[f]
        if z is None:
            z = last_z
        last_z = z
        out.append((f, x, y, z))
    return out


def _polyline(anchors) -> list[tuple[float, float, float]]:
    """Points every ``SPACING`` m along straight lines between the anchors (in
    frame order), each ``(x, y, z_ref)``."""
    out = []
    for (_f0, x0, y0, z0), (_f1, x1, y1, z1) in zip(anchors, anchors[1:]):
        length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        n = max(1, int(length / SPACING))
        for k in range(n):
            s = k / float(n)
            out.append((x0 + (x1 - x0) * s, y0 + (y1 - y0) * s, z0 + (z1 - z0) * s))
    f, x, y, z = anchors[-1]
    out.append((x, y, z))
    return out


def _drop_gaps(heights: list, spacing: float) -> list:
    """``None`` where a short stretch is far below the ground either side of it:
    the gap between two roofs, the street under it."""
    out = list(heights)
    n = len(out)
    longest = max(1, int(GAP_MAX_METRES / spacing))
    t = 0
    while t < n:
        before = out[t - 1] if t > 0 else None
        if out[t] is None or before is None or out[t] > before - GAP_DEPTH:
            t += 1
            continue
        end = t
        while end < n and (out[end] is None or out[end] <= before - GAP_DEPTH):
            end += 1
        after = out[end] if end < n else None
        if end - t <= longest and after is not None and after > max(
                h for h in out[t:end] if h is not None) + GAP_DEPTH:
            for k in range(t, end):
                out[k] = None
        t = end
    return out


def _drop_bumps(heights: list, spacing: float) -> list:
    """``None`` where a short stretch stands well above the ground either side
    of it: an air-con unit, a vent, a parapet the route crosses. The character
    goes round it or over it -- that is the motion's business -- but it is not
    a level to walk on, and a foot planted beside it must not be stood on top
    of it. A step up that carries on is kept."""
    out = list(heights)
    n = len(out)
    longest = max(1, int(OBSTACLE_MAX_METRES / spacing))
    t = 1
    while t < n:
        before = out[t - 1]
        if out[t] is None or before is None or out[t] < before + OBSTACLE_HEIGHT:
            t += 1
            continue
        end = t
        while end < n and out[end] is not None and out[end] >= before + OBSTACLE_HEIGHT:
            end += 1
        after = out[end] if end < n else None
        if end - t <= longest and after is not None and after < min(out[t:end]) - OBSTACLE_HEIGHT:
            for k in range(t, end):
                out[k] = None
        t = end
    return out


def ground_constraint(scene, arm, constraints: list[dict], reference_z: float | None = None) -> dict | None:
    """The MMCP ``ground_height`` constraint for the take's planned route --
    points on the surfaces along it and a little either side -- or ``None``
    when there is no route, no ground found, or the ground is the flat Y=0 the
    model assumes anyway."""
    if reference_z is None:
        root = next((pb for pb in arm.pose.bones if pb.parent is None), None) if arm else None
        reference_z = float((arm.matrix_world @ root.head).z) if root is not None else 1.0
    anchors = _anchors(constraints, reference_z)
    if not anchors:
        return None
    skip = _excluded(arm)
    line = _polyline(anchors)
    centre = [surface_below(scene, x, y, z + RAY_HEADROOM, arm=arm, exclude=skip) for x, y, z in line]
    centre = _drop_bumps(_drop_gaps(centre, SPACING), SPACING)
    pts = []
    for i, ((x, y, z_ref), h) in enumerate(zip(line, centre)):
        if h is None:
            continue
        # across the route: the feet are either side of it, and a turn cuts corners
        a = line[max(i - 1, 0)]
        b = line[min(i + 1, len(line) - 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = (dx * dx + dy * dy) ** 0.5 or 1.0
        px, py = -dy / norm, dx / norm
        pts.append((x, y, h))
        for side in (-LANE, LANE):
            lx, ly = x + px * side, y + py * side
            lh = surface_below(scene, lx, ly, z_ref + RAY_HEADROOM, arm=arm, exclude=skip)
            if lh is not None and h - GAP_DEPTH < lh < h + OBSTACLE_HEIGHT:
                pts.append((lx, ly, lh))
    if not pts or all(abs(h) < FLAT_EPS for _x, _y, h in pts):
        return None
    # MMCP is Y-up: (x, y, z) Blender -> (x, z, -y), so a point is [x, -y, height]
    return {"type": "ground_height",
            "points": [[round(x, 3), round(-y, 3), round(h, 4)] for x, y, h in pts]}
