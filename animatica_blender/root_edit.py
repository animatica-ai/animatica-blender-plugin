"""The root trajectory, as a curve the artist can edit.

In place takes out the take's root trajectory, fitted from its centre of mass
(see inplace.py). When the fit is not what the artist wants, *Edit Root
Trajectory* turns it into a Bezier curve on the floor. From then on the curve
is the trajectory: In place takes out the curve instead of the fit, live as
it is edited, and the viewport's Root Trajectory line follows it.

Only the shape comes from the curve. The timing stays the fit's: each frame
keeps how far along the path it was (as a fraction of the length), so a
character that eased into a run still does, along the new path. The heading
changes by as much as the curve's direction does from the fit's.

The curve belongs to one action (an ID property pointing at it). Reset
removes it, and Accept removes it too, since by then the edit is in the keys.
"""

from __future__ import annotations

import json
import math
import time

import bpy
import numpy as np
from mathutils import Vector

#: on the curve object: the action whose root trajectory it is
OWNER_PROP = "animatica_root_edit_of"
#: on the curve object: per spline, its frames and each frame's place along it
SPANS_PROP = "animatica_root_edit"

#: spans shorter than this are on the spot: there is no path to edit
MIN_LENGTH = 0.05
#: at most this much turn per Bezier segment, and this long a segment
SEG_TURN = math.radians(60.0)
SEG_LENGTH = 1.5
MAX_SEGMENTS = 8
#: samples per Bezier segment when a curve is read back
SAMPLES = 48
#: wait for the edit to stop this long before taking the new path out
DEBOUNCE = 0.12

_pending: dict = {"at": None}


# --- finding it -----------------------------------------------------------------------

def find(action):
    """The curve holding *action*'s edited root trajectory, or None."""
    if action is None:
        return None
    for obj in bpy.data.objects:
        if obj.type == 'CURVE' and obj.get(OWNER_PROP) is not None and obj.get(OWNER_PROP) == action:
            return obj
    return None


def _remove(obj) -> None:
    data = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is not None and data.users == 0:
        bpy.data.curves.remove(data)


def discard(action) -> bool:
    """Drop *action*'s edited trajectory (Reset; Accept, once it is in the keys)."""
    obj = find(action)
    if obj is None:
        return False
    _remove(obj)
    return True


def discard_all() -> None:
    """Drop every edited trajectory (Accept, Reject: the preview they edit is over)."""
    for obj in [o for o in bpy.data.objects if o.type == 'CURVE' and OWNER_PROP in o]:
        _remove(obj)


def purge() -> None:
    """Remove curves whose take has gone (a rejected or replaced preview)."""
    for obj in [o for o in bpy.data.objects if o.type == 'CURVE' and OWNER_PROP in o]:
        owner = obj.get(OWNER_PROP)
        if owner is None or getattr(owner, "users", 0) == 0:
            _remove(obj)


# --- building it ----------------------------------------------------------------------

def _knots(xy):
    """Where to put the curve's points: evenly along the path, one segment per
    60 degrees of turn or 1.5 m, and their handles along the path's direction.
    A straight path gets two points, and then the curve *is* the fit."""
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    L = float(cum[-1])
    tan = np.gradient(xy, axis=0)
    ang = np.unwrap(np.arctan2(tan[:, 1], tan[:, 0]))
    moving = np.linalg.norm(tan, axis=1) > 1e-6
    turn = float(np.ptp(ang[moving])) if moving.any() else 0.0
    k = 1 if turn < math.radians(3.0) else int(max(math.ceil(turn / SEG_TURN), math.ceil(L / SEG_LENGTH), 2))
    k = min(k, MAX_SEGMENTS)
    at = np.linspace(0.0, L, k + 1)
    pts = np.stack([np.interp(at, cum, xy[:, j]) for j in range(2)], 1)
    h = max(L / k * 0.02, 1e-4)             # the direction over a short stretch, not one frame
    dirs = []
    for a in at:
        p0 = np.array([np.interp(max(a - h, 0.0), cum, xy[:, j]) for j in range(2)])
        p1 = np.array([np.interp(min(a + h, L), cum, xy[:, j]) for j in range(2)])
        d = p1 - p0
        n = np.linalg.norm(d)
        dirs.append(d / n if n > 1e-9 else (pts[-1] - pts[0]) / max(L, 1e-9))
    return pts, np.array(dirs), L / k, cum / max(L, 1e-9)


def create(arm, action, scene):
    """Turn the take's root trajectory into an editable curve, or return the one
    it already has. None when there is nothing to edit (on the spot)."""
    obj = find(action)
    if obj is not None:
        return obj
    purge()
    from . import inplace
    spans = inplace.fitted_path(arm, action, scene)
    curve = bpy.data.curves.new("Root Trajectory", 'CURVE')
    curve.dimensions = '3D'
    curve.resolution_u = 24
    meta = []
    for span in spans:
        xy = np.asarray(span["path"], float)
        if len(xy) < 3:
            continue
        pts, dirs, step, e = _knots(xy)
        if step * (len(pts) - 1) < MIN_LENGTH:
            continue
        z = float(span.get("floor", 0.0))
        spline = curve.splines.new('BEZIER')
        spline.bezier_points.add(len(pts) - 1)
        for bp, p, d in zip(spline.bezier_points, pts, dirs):
            c = Vector((p[0], p[1], z))
            off = Vector((d[0], d[1], 0.0)) * (step / 3.0)
            bp.co = c
            bp.handle_left_type = bp.handle_right_type = 'ALIGNED'
            bp.handle_left = c - off
            bp.handle_right = c + off
            bp.select_control_point = bp.select_left_handle = bp.select_right_handle = True
        meta.append({"frames": list(span["frames"]), "e": [round(float(v), 6) for v in e]})
    if not meta:
        bpy.data.curves.remove(curve)
        return None
    obj = bpy.data.objects.new("Root Trajectory", curve)
    obj[OWNER_PROP] = action
    obj[SPANS_PROP] = json.dumps({"spans": meta})
    obj.show_in_front = True
    coll = arm.users_collection[0] if arm.users_collection else scene.collection
    coll.objects.link(obj)
    return obj


# --- reading it back ------------------------------------------------------------------

def _polyline(obj, spline) -> np.ndarray | None:
    """A Bezier spline sampled densely, in world space (x, y)."""
    bps = spline.bezier_points
    if len(bps) < 2:
        return None
    mw = obj.matrix_world
    t = np.linspace(0.0, 1.0, SAMPLES)[:, None]
    out = []
    for a, b in zip(bps, bps[1:]):
        p0, p1, p2, p3 = (np.array((mw @ v).to_2d()) for v in (a.co, a.handle_right, b.handle_left, b.co))
        pts = ((1 - t) ** 3) * p0 + 3 * ((1 - t) ** 2) * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3
        out.append(pts if not out else pts[1:])
    return np.concatenate(out)


def overlay(action, spans: list, fps: float) -> list:
    """*spans* (from inplace's fit) with each edited one's path replaced by the
    curve's, at the same place along it each frame. The rest pass through."""
    obj = find(action)
    if obj is None:
        return spans
    try:
        meta = json.loads(obj.get(SPANS_PROP, "{}")).get("spans", [])
    except (TypeError, ValueError):
        return spans
    from . import inplace
    splines = list(obj.data.splines)
    by_frames = {tuple(m["frames"]): (m, splines[i]) for i, m in enumerate(meta) if i < len(splines)}
    out = []
    for span in spans:
        got = by_frames.get((span["first"], span["last"]))
        poly = _polyline(obj, got[1]) if got is not None else None
        if poly is None or len(got[0]["e"]) != len(span["xy"]):
            out.append(span)
            continue
        seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        L = float(cum[-1])
        at = np.asarray(got[0]["e"], float) * L
        xy = np.stack([np.interp(at, cum, poly[:, j]) for j in range(2)], 1)
        yaw = span["yaw"] + inplace._tangent_yaw(xy, fps) - inplace._tangent_yaw(span["xy"], fps)
        out.append({**span, "xy": xy, "yaw": yaw, "model": "edited",
                    "prm": {"source": "edited", "length_m": round(L, 3),
                            "off_fit_cm": round(float(np.linalg.norm(xy - span["xy"], axis=1).max()) * 100, 1)}})
    return out


# --- keeping up with edits ------------------------------------------------------------

def _owner_of(update):
    """The action a depsgraph update's curve edits, if it is one of ours."""
    src = getattr(update.id, "original", update.id)
    if isinstance(src, bpy.types.Object):
        return src.get(OWNER_PROP) if src.type == 'CURVE' else None
    if isinstance(src, bpy.types.Curve):
        for obj in bpy.data.objects:
            if obj.data == src and OWNER_PROP in obj:
                return obj.get(OWNER_PROP)
    return None


def _on_root_edit_depsgraph(scene, depsgraph) -> None:
    for update in depsgraph.updates:
        if _owner_of(update) is not None:
            _pending["at"] = time.monotonic()
            if not bpy.app.timers.is_registered(_timer):
                bpy.app.timers.register(_timer, first_interval=DEBOUNCE)
            return


def _timer():
    at = _pending["at"]
    if at is None:
        return None
    wait = DEBOUNCE - (time.monotonic() - at)
    if wait > 0:
        return wait
    _pending["at"] = None
    try:
        refresh(bpy.context.scene)
    except Exception as exc:                 # noqa: BLE001 — a timer must not raise
        print(f"[Animatica] root trajectory edit: {exc}")
    return None


def refresh(scene, *, reuse: bool = True) -> None:
    """The take showing, with its (edited) trajectory taken out again if In
    place is on, and the viewport's line redrawn."""
    from . import inplace, key_poses, operators
    settings = getattr(scene, "animatica", None)
    arm = getattr(settings, "target_armature", None)
    ad = arm.animation_data if arm is not None else None
    action = ad.action if ad is not None else None
    if action is None:
        return
    if inplace.is_applied(action):
        key_poses._baking = True
        try:
            inplace.apply(arm, action, scene, operators._inplace_spans(arm, action), reuse=reuse)
        finally:
            key_poses._baking = False
        key_poses.request_rebuild()
    else:
        key_poses.request_root_refresh()


def register() -> None:
    handlers = bpy.app.handlers.depsgraph_update_post
    for h in [h for h in handlers if getattr(h, "__name__", "") == "_on_root_edit_depsgraph"]:
        handlers.remove(h)
    handlers.append(_on_root_edit_depsgraph)


def unregister() -> None:
    handlers = bpy.app.handlers.depsgraph_update_post
    for h in [h for h in handlers if getattr(h, "__name__", "") == "_on_root_edit_depsgraph"]:
        handlers.remove(h)
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
