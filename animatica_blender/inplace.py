# SPDX-License-Identifier: GPL-3.0-or-later
"""In place: take a take's travel out and keep everything the body does.

Pinning the root bone on the ground (what In place used to do) takes out far
more than the travel. The pelvis surges forward and back and sways side to side
with every step; pinned, all of that is gone, and when a game moves the
character along at the walk's speed the planted feet slide by it (a walk's
slide doubled). The travel is only the path the character moves along, so In
place removes exactly that:

1. **The body.** The centre of mass (COM), from the rig's segments with
   standard mass fractions, and what touches the floor each frame: heels,
   balls of the feet, knees, hands, elbows, the back, the head.
2. **The travel path.** The COM with the gait taken out. A loop's COM is
   averaged over one contact cycle (a stride), which cancels the step-by-step
   sway and surge exactly. A one-shot is read by phases: standing on a fixed
   base (a crouch before a jump, a lean) the path holds still; in the air it
   runs straight and even from take-off to landing; while the contacts change
   (steps, a roll, a crawl) it follows the averaged COM.
3. **The simplest trajectory.** A path a game can re-apply: still, a straight
   line at a constant speed, an arc at a constant speed and turn rate, or one
   of those timed by a distance curve (starts, stops, jumps), or failing those
   a cubic Bezier. The simplest one within tolerance wins; loops may only be
   still, a line or an arc, which repeat.
4. **Removed.** Each frame the top bone is moved by the inverse of that
   trajectory, about the vertical: the take stays where it began, facing the
   way it began, and turns only as much as its path does not (an arc's turn
   comes out with the arc). Heights are never touched.

Toggling In place off puts the original keys back: they are kept on the action
until Accept. Numpy only (Blender ships it; not scipy).
"""

from __future__ import annotations

import json
import math
import re

import numpy as np
from mathutils import Matrix, Vector

_FWD = Vector((1.0, 0.0, 0.0))     # the top bone's sideways axis (its own Y runs up the spine): how it turns

BACKUP_KEY = "animatica_inplace"

# --- the rig --------------------------------------------------------------------------

_ROLES = {
    "hips": ["Hips", "pelvis", "hips"], "spine": ["Spine1", "Spine", "spine"], "chest": ["Chest", "Spine2", "chest"],
    "neck": ["Neck1", "Neck", "neck"], "head": ["Head", "head"],
}
_SIDED = {"arm": ["Arm", "UpperArm"], "forearm": ["ForeArm", "LowerArm"], "hand": ["Hand"],
          "leg": ["Leg", "UpLeg", "Thigh"], "shin": ["Shin", "Calf", "LowerLeg"], "foot": ["Foot"], "toe": ["ToeBase", "Toe"]}


def _find(arm, names, side=None):
    """A bone by its possible names, also under the namespace the whole rig
    carries (the bundled Animatic character's ``animatica:Hips``): found bare
    only, the body was just its hips, and no foot ever touched the floor."""
    from .gltf_to_blender import bone_namespace
    pb = arm.pose.bones
    ns = bone_namespace(arm.pose)
    for n in names:
        cands = ([f"{side}{n}", f"{side}_{n}", f"{n}.{side[0]}", f"{n}_{side[0]}", f"mixamorig:{side}{n}"]
                 if side else [n, f"mixamorig:{n}"])
        for cand in cands:
            for full in ((cand, ns + cand) if ns else (cand,)):
                if full in pb:
                    return pb[full]
    return None


def bones(arm) -> dict:
    """The body's bones, by role (canonical names, and common alternatives).
    Missing ones are left out: the COM is made of what is there."""
    out = {role: _find(arm, names) for role, names in _ROLES.items()}
    for side in ("Left", "Right"):
        for role, names in _SIDED.items():
            out[f"{side[0].lower()}_{role}"] = _find(arm, names, side)
    if out["hips"] is None:
        out["hips"] = next((b for b in arm.pose.bones if b.parent is None), None)
    return {k: v for k, v in out.items() if v is not None}


# Winter (2009) segment masses, and where along the segment its centre is.
_SEGMENTS = [("hips", "spine", 0.142, 0.5), ("spine", "chest", 0.139, 0.5), ("chest", "neck", 0.216, 0.5),
             ("neck", ("tail", "head"), 0.081, 0.6)]
for _s in ("l", "r"):
    _SEGMENTS += [(f"{_s}_arm", f"{_s}_forearm", 0.028, 0.436), (f"{_s}_forearm", f"{_s}_hand", 0.016, 0.43),
                  (f"{_s}_hand", ("tail", f"{_s}_hand"), 0.006, 0.5), (f"{_s}_leg", f"{_s}_shin", 0.100, 0.433),
                  (f"{_s}_shin", f"{_s}_foot", 0.0465, 0.433), (f"{_s}_foot", ("tail", f"{_s}_foot"), 0.0145, 0.5)]

# (group, role, head|tail, the joint's height above the floor when that part is
# on it (m, for a figure with its hips 0.9 m up), torso?)
_CONTACTS = [("l foot", "l_foot", 0, 0.09, False), ("l foot", "l_foot", 1, 0.06, False),
             ("r foot", "r_foot", 0, 0.09, False), ("r foot", "r_foot", 1, 0.06, False),
             ("l knee", "l_shin", 0, 0.12, False), ("r knee", "r_shin", 0, 0.12, False),
             ("l hand", "l_hand", 1, 0.11, False), ("l hand", "l_hand", 0, 0.16, False),
             ("r hand", "r_hand", 1, 0.11, False), ("r hand", "r_hand", 0, 0.16, False),
             ("l elbow", "l_forearm", 0, 0.12, False), ("r elbow", "r_forearm", 0, 0.12, False),
             ("torso", "l_arm", 0, 0.15, True), ("torso", "r_arm", 0, 0.15, True), ("torso", "hips", 0, 0.18, True),
             ("torso", "spine", 0, 0.15, True), ("torso", "chest", 0, 0.15, True), ("torso", "neck", 0, 0.15, True),
             ("head", "head", 0, 0.13, True), ("head", "head", 1, 0.13, True)]
_GROUPS = ["l foot", "r foot", "l hand", "r hand", "l knee", "r knee", "l elbow", "r elbow", "torso", "head"]


def sample(arm, scene, first: int, last: int) -> dict:
    """Per frame, world positions of the body's bones (head, tail) and the top
    bone's pose matrix (armature space). Leaves the scene on its frame."""
    B = bones(arm)
    top = next(pb for pb in arm.pose.bones if pb.parent is None)
    keep = scene.frame_current
    mw = arm.matrix_world
    heads = {k: [] for k in B}
    tails = {k: [] for k in B}
    tops = []
    for f in range(first, last + 1):
        scene.frame_set(f)
        for k, b in B.items():
            heads[k].append(tuple(mw @ b.head))
            tails[k].append(tuple(mw @ b.tail))
        tops.append(top.matrix.copy())
    scene.frame_set(keep)
    return {"joints": {k: (np.array(heads[k]), np.array(tails[k])) for k in B}, "top": tops}


def _com(J) -> np.ndarray:
    total, acc = 0.0, None
    for a, b, m, u in _SEGMENTS:
        if a not in J:
            continue
        p = J[a][0]
        if isinstance(b, tuple):
            if b[1] not in J:
                continue
            q = J[b[1]][1]
        elif b in J:
            q = J[b][0]
        else:
            continue
        c = m * (p + (q - p) * u)
        acc = c if acc is None else acc + c
        total += m
    if acc is None or total < 0.3:                 # too little of the body found: the hips will do
        return J["hips"][0].copy()
    return acc / total


# --- the travel path ------------------------------------------------------------------

def _box(x, width, loop):
    """Centred moving average, per-frame (fractional) width in frames; one-shots
    shrink the window at their ends, loops wrap (their travel repeating)."""
    n = len(x)
    if loop:
        m, trend = n - 1, x[-1] - x[0]
        k = 3
        xt = np.concatenate([x[:m] + trend * c for c in range(-k, k + 1)] + [x[-1:] + trend * k])
        off = k * m
    else:
        xt, off = x, 0
    N = len(xt)
    c = np.concatenate([[0.0], np.cumsum((xt[1:] + xt[:-1]) / 2)])
    grid = np.arange(N)
    out = np.empty(n)
    for i in range(n):
        h = width[i] / 2 if loop else min(width[i] / 2, i, n - 1 - i)
        u = i + off
        out[i] = xt[u] if h < 1e-6 else (np.interp(u + h, grid, c) - np.interp(u - h, grid, c)) / (2 * h)
    return out


class Body:
    """A take, read for its travel: the COM and what touches the floor."""

    def __init__(self, S, fps, loop):
        J = S["joints"]
        self.fps, self.loop = fps, loop
        self.com = _com(J)[:, :2]
        self.n = n = len(self.com)
        feet = [J[k][j][:, 2] for k in ("l_foot", "r_foot") if k in J for j in (0, 1)]
        low = np.min(np.stack(feet), 0) if feet else J["hips"][0][:, 2] - 0.9
        self.floor = float(np.percentile(low, 5)) - 0.033
        hip = float(np.median(J["hips"][0][:, 2])) - self.floor
        scale = hip / 0.9 if hip > 0.3 else 1.0
        body_v = np.linalg.norm(np.gradient(self.com, axis=0), axis=1) * fps if n > 1 else np.zeros(n)
        self.contacts = []
        for g, role, k, h, torso in _CONTACTS:
            if role not in J:
                continue
            p = J[role][k]
            v = np.linalg.norm(np.gradient(p[:, :2], axis=0), axis=1) * fps if n > 1 else np.zeros(n)
            d = (p[:, 2] - self.floor < h * scale) & (v < (1.2 if torso else 0.4) * scale + 0.3 * body_v)
            dd = d.copy()
            for i in range(n):
                if d[i] and not ((i > 0 and d[i - 1]) or (i + 1 < n and d[i + 1])):
                    dd[i] = False                                  # a 1-frame blip
            self.contacts.append((g, p, dd))

    def anchor(self):
        """Support centroid; in flight the COM, offset to join take-off and landing."""
        n = self.n
        anc = np.full((n, 2), np.nan)
        for i in range(n):
            pts = [p[i, :2] for g, p, d in self.contacts if d[i]]
            if pts:
                anc[i] = np.mean(pts, 0)
        flight = np.isnan(anc[:, 0])
        if flight.all():
            return self.com.copy(), flight
        off = anc - self.com
        idx = np.where(~flight)[0]
        for j in range(2):
            off[:, j] = np.interp(np.arange(n), idx, off[idx, j])
        return self.com + off, flight

    def window(self):
        """Per frame the local contact cycle (a limb's touch-down to its next), in frames."""
        n, fps = self.n, self.fps
        mids, lens = [], []
        for g in _GROUPS:
            d = np.zeros(n, bool)
            for gg, p, dd in self.contacts:
                if gg == g:
                    d |= dd
            on = [i for i in range(1, n) if d[i] and not d[i - 1]]
            if self.loop and n > 1 and d[0] and not d[-1]:
                on = [0] + on
            pairs = list(zip(on, on[1:]))
            if self.loop and on:
                pairs.append((on[-1], on[0] + (n - 1)))
            for a, b in pairs:
                if b - a >= 0.25 * fps:
                    mids.append((a + b) / 2)
                    lens.append(b - a)
        w = np.full(n, 0.3 * fps)
        for i in range(n):
            near = [L for m, L in zip(mids, lens) if abs(m - i) <= L / 2 + 1]
            if near:
                w[i] = np.clip(np.median(near), 0.3 * fps, 1.8 * fps)
        if self.loop:
            w[:] = np.median(w)
        return w

    def phases(self, anc, flight):
        """Each frame: "base" (standing on an unchanging support, barely
        travelling), "flight" (nothing down) or "move"."""
        n, fps = self.n, self.fps
        sets = [tuple(bool(d[i]) for g, p, d in self.contacts) for i in range(n)]
        lab = np.array(["move"] * n, dtype=object)
        lab[flight] = "flight"
        i = 0
        while i < n:
            if flight[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and sets[j + 1] == sets[i] and not flight[j + 1]:
                j += 1
            dur = (j - i + 1) / fps
            if dur >= 0.4 and np.linalg.norm(anc[j] - anc[i]) < 0.03 and \
                    np.linalg.norm(self.com[j] - self.com[i]) / dur < 0.15:
                lab[i:j + 1] = "base"
            i = j + 1
        return lab

    def replants(self, tol=0.05):
        """Does any part touch down somewhere new (a step, a hand put ahead)?"""
        for g in _GROUPS:
            spots = []
            for gg, p, d in self.contacts:
                if gg != g:
                    continue
                i, n = 0, len(d)
                while i < n:
                    if d[i]:
                        j = i
                        while j + 1 < n and d[j + 1]:
                            j += 1
                        spots.append(p[i:j + 1, :2].mean(0))
                        i = j + 1
                    else:
                        i += 1
            if any(np.linalg.norm(b - a) > tol for a, b in zip(spots, spots[1:])):
                return True
        return False

    def travel_path(self):
        """The COM with the gait taken out (see the module notes), and the phases."""
        n = self.n
        if n < 3:
            return self.com.copy(), np.array(["base"] * n, dtype=object)
        w = self.window()
        avg = np.stack([_box(self.com[:, j], w, self.loop) for j in range(2)], 1)
        anc, flight = self.anchor()
        if self.loop:
            lab = np.array(["move"] * n, dtype=object)
            if not self.replants() and not flight.any():
                u = np.linspace(0, 1, n)[:, None]
                return anc[0] + (anc[-1] - anc[0]) * u, lab           # on the spot
            return avg, lab
        lab = self.phases(anc, flight)
        p = np.full((n, 2), np.nan)
        runs, i = [], 0
        while i < n:
            j = i
            while j + 1 < n and lab[j + 1] == lab[i]:
                j += 1
            runs.append((lab[i], i, j))
            i = j + 1
        for kind, i, j in runs:                  # on a fixed base: hold still
            if kind == "base":
                p[i:j + 1] = avg[i:j + 1].mean(0)
        for kind, i, j in runs:                  # moving: the averaged COM, joined to its neighbours
            if kind == "move":
                a = p[i - 1] - avg[i] if i > 0 and not np.isnan(p[i - 1, 0]) else np.zeros(2)
                b = p[j + 1] - avg[j] if j + 1 < n and not np.isnan(p[j + 1, 0]) else np.zeros(2)
                u = np.linspace(0, 1, j - i + 1)[:, None]
                p[i:j + 1] = avg[i:j + 1] + a + (b - a) * u
        for kind, i, j in runs:                  # in the air: straight and even
            if kind == "flight":
                a = p[i - 1] if i > 0 else avg[i]
                b = p[j + 1] if j + 1 < n and not np.isnan(p[j + 1, 0]) else avg[j]
                u = (np.arange(i, j + 1) - (i - 1)) / (j + 2 - i)
                p[i:j + 1] = a + (b - a) * u[:, None]
        p = np.where(np.isnan(p), avg, p)
        k = np.full(n, 0.2 * self.fps)
        return np.stack([_box(p[:, j], k, False) for j in range(2)], 1), lab


# --- the simplest trajectory ----------------------------------------------------------

#: A model is accepted within these of the travel path, split by direction.
#: Across the path: RMS 1.5 cm + 2.5 % of the distance, max 4 cm + 5 % (a take's
#: own wobble about a line is a few % of its length; a real curve is far outside
#: it: a curving walk, 50 cm off a line). Along it: 2 cm RMS, 5 cm max, whatever
#: the distance -- an error along the path is timing, and in place it is the
#: body sliding back or forward (a run from standing, fitted with too few keys,
#: slid 0.5 m back before it ran). Same as motionmcp.trajectory.
TOL_RMS, TOL_MAX, TOL_REL = 0.015, 0.04, 0.025
ALONG_RMS, ALONG_MAX = 0.02, 0.05
#: distance-curve keys at most this far apart (s), besides the take's events
KEY_GAP = 0.25
#: on the spot: never strays further than this (or creeps under 8 cm at < 5 cm/s)
STILL_MAX = 0.06


def _pchip(tk, yk, t):
    """Monotone cubic (Fritsch-Carlson) through (tk, yk), evaluated at t."""
    tk, yk = np.asarray(tk, float), np.asarray(yk, float)
    h = np.diff(tk)
    d = np.diff(yk) / h
    m = np.zeros_like(yk)
    for i in range(1, len(yk) - 1):
        if d[i - 1] * d[i] > 0:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    m[0], m[-1] = d[0], d[-1]
    i = np.clip(np.searchsorted(tk, t) - 1, 0, len(tk) - 2)
    u = np.clip((t - tk[i]) / h[i], 0, 1)
    h00, h10, h01, h11 = 2 * u ** 3 - 3 * u ** 2 + 1, u ** 3 - 2 * u ** 2 + u, -2 * u ** 3 + 3 * u ** 2, u ** 3 - u ** 2
    return h00 * yk[i] + h10 * h[i] * m[i] + h01 * yk[i + 1] + h11 * h[i] * m[i + 1]


def _key_times(labels, T, want=6, min_gap=0.15):
    """Distance-curve keys at the take's events (take-off, landing, where it
    stops or sets off) and its ends, filled in the longest gaps."""
    n = len(T)
    ev = sorted({0.0, float(T[-1])} | {float(T[i]) for i in range(1, n) if labels[i] != labels[i - 1]})
    keep = [ev[0]]
    for e in ev[1:]:
        if e - keep[-1] >= min_gap:
            keep.append(e)
    keep[-1] = float(T[-1])
    while len(keep) < want or (len(keep) > 1 and float(np.max(np.diff(keep))) > KEY_GAP):
        g = int(np.argmax(np.diff(keep)))
        keep.insert(g + 1, (keep[g] + keep[g + 1]) / 2)
    return np.array(keep)


def _split_error(xy, path):
    """Distance from the travel path, split: along its direction (timing) and across it."""
    tan = np.gradient(path, axis=0)
    ln = np.linalg.norm(tan, axis=1, keepdims=True)
    ok = ln[:, 0] > 1e-6
    if not ok.any():
        e = np.linalg.norm(xy - path, axis=1)
        return np.zeros_like(e), e
    idx = np.where(ok)[0]
    tan = np.where(ok[:, None], tan / np.maximum(ln, 1e-12), 0)
    for j in range(2):                        # standing still: the nearest direction
        tan[:, j] = np.interp(np.arange(len(tan)), idx, tan[idx, j])
    # where it stops and turns back, the two sides cancel: take the nearest real one
    weak = np.linalg.norm(tan, axis=1) < 0.5
    if weak.any():
        near = idx[np.abs(np.arange(len(tan))[weak, None] - idx[None, :]).argmin(1)]
        tan[weak] = tan[near]
    tan /= np.maximum(np.linalg.norm(tan, axis=1, keepdims=True), 1e-12)
    d = xy - path
    along = (d * tan).sum(1)
    return along, np.abs(d[:, 0] * tan[:, 1] - d[:, 1] * tan[:, 0])


def _within(xy, path, dist):
    """Close enough to the travel path: loosely across it, tightly along it."""
    along, across = _split_error(xy, path)
    return (float(np.sqrt((across ** 2).mean())) <= TOL_RMS + TOL_REL * dist
            and float(across.max()) <= TOL_MAX + 2 * TOL_REL * dist
            and float(np.sqrt((along ** 2).mean())) <= ALONG_RMS and float(np.abs(along).max()) <= ALONG_MAX)


def _monotone_keys(s, T, tk):
    ks = np.interp(tk, T, s)
    return np.maximum.accumulate(ks) if ks[-1] >= ks[0] else np.minimum.accumulate(ks)


def _circle(P):
    """Least-squares circle (Kasa): centre and radius; None for a line."""
    x, y = P[:, 0], P[:, 1]
    A = np.stack([x, y, np.ones_like(x)], 1)
    try:
        (D, E, F), *_ = np.linalg.lstsq(A, -(x ** 2 + y ** 2), rcond=None)
    except np.linalg.LinAlgError:
        return None
    c = np.array([-D / 2, -E / 2])
    r2 = c @ c - F
    if r2 <= 0 or r2 > 60.0 ** 2:
        return None
    return c, math.sqrt(r2)


def _tangent_yaw(xy, fps):
    """Heading change along a path (0 at its start, + turns left), held where it is still."""
    n = len(xy)
    tan = np.gradient(xy, axis=0)
    ang = np.unwrap(np.arctan2(tan[:, 0], -tan[:, 1]))
    moving = np.linalg.norm(tan, axis=1) * fps > 0.05
    if not moving.any():
        return np.zeros(n)
    idx = np.where(moving)[0]
    ang = np.interp(np.arange(n), idx, ang[idx])
    return ang - ang[idx[0]]


def _fit(model, path, T, tk, fps):
    """One model fitted to the travel path: (xy, heading change, numbers) or None."""
    n = len(path)
    zero = np.zeros(n)
    if model == "still":
        c = path.mean(0)
        return np.repeat(c[None], n, 0), zero, {}
    if model == "line":
        A = np.stack([np.ones(n), T], 1)
        coef, *_ = np.linalg.lstsq(A, path, rcond=None)
        v = coef[1]
        return A @ coef, zero, {"speed": round(float(np.linalg.norm(v)), 3),
                                "heading_deg": round(math.degrees(math.atan2(v[0], -v[1])), 1)}
    if model in ("arc", "arc + distance curve"):
        circ = _circle(path)
        if circ is None:
            return None
        c, r = circ
        phi = np.unwrap(np.arctan2(path[:, 1] - c[1], path[:, 0] - c[0]))
        if model == "arc":
            A = np.stack([np.ones(n), T], 1)
            (p0, w), *_ = np.linalg.lstsq(A, phi, rcond=None)
            ph = p0 + w * T
            prm = {"speed": round(abs(w) * r, 3), "turn_deg_s": round(math.degrees(w), 2), "radius_m": round(r, 2)}
        else:
            keys = _monotone_keys(phi, T, tk)
            ph = _pchip(tk, keys, T)
            prm = {"radius_m": round(r, 2), "turned_deg": round(math.degrees(keys[-1] - keys[0]), 1),
                   "distance_keys_m": [round(float(abs(k - keys[0]) * r), 3) for k in keys]}
        return c + r * np.stack([np.cos(ph), np.sin(ph)], 1), ph - ph[0], prm
    if model == "line + distance curve":
        d = path[-1] - path[0]
        _, _, vt = np.linalg.svd(path - path.mean(0), full_matrices=False)
        u = vt[0] if vt[0] @ d >= 0 else -vt[0]
        s_raw = (path - path[0]) @ u
        base = (path - s_raw[:, None] * u).mean(0)
        keys = _monotone_keys(s_raw, T, tk)
        s = _pchip(tk, keys, T)
        return base + s[:, None] * u, zero, {"heading_deg": round(math.degrees(math.atan2(u[0], -u[1])), 1),
                                             "distance_keys_m": [round(float(k - keys[0]), 3) for k in keys]}
    if model == "bezier + distance curve":
        seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
        L = seg.sum()
        if L < 1e-6:
            return None
        e_raw = np.concatenate([[0], np.cumsum(seg)]) / L
        e = np.clip(_pchip(tk, _monotone_keys(e_raw, T, tk), T), 0, 1)
        Bm = np.stack([(1 - e) ** 3, 3 * (1 - e) ** 2 * e, 3 * (1 - e) * e ** 2, e ** 3], 1)
        P, *_ = np.linalg.lstsq(Bm, path, rcond=None)
        xy = Bm @ P
        return xy, _tangent_yaw(xy, fps), {"control_points": [[round(float(a), 3) for a in p] for p in P]}
    return None


MODELS = ("still", "line", "arc", "line + distance curve", "arc + distance curve", "bezier + distance curve")
LOOPABLE = ("still", "line", "arc")


def trajectory(body: Body):
    """The simplest model within tolerance of the travel path.
    Returns (xy (n, 2), heading change (n,), model name, its numbers)."""
    path, labels = body.travel_path()
    n, fps = len(path), body.fps
    if n < 3:
        return path, np.zeros(n), "still", {}
    T = np.arange(n) / fps
    dist = float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum())
    tk = _key_times(labels, T)
    best = None
    for m in MODELS:
        if body.loop and m not in LOOPABLE:
            continue
        got = _fit(m, path, T, tk, fps)
        if got is None:
            continue
        xy, yaw, prm = got
        err = np.linalg.norm(xy - path, axis=1)
        rms, mx = float(np.sqrt((err ** 2).mean())), float(err.max())
        if m == "still":
            net = float(np.linalg.norm(path[-1] - path[0]))
            ok = mx <= STILL_MAX or (net < 0.08 and dist / max(float(T[-1]), 1e-6) < 0.05)
        else:
            ok = _within(xy, path, dist)
        prm = {**prm, "off_cm": round(rms * 100, 1)}
        if ok:
            return xy, yaw, m, prm
        if best is None or rms < best[4]:
            best = (xy, yaw, m, prm, rms)
    return best[:4]


# --- applying it ----------------------------------------------------------------------

def _root_curves(action, bone):
    from . import constraints_ui
    out = {}
    for fc in constraints_ui.iter_action_fcurves(action):
        if fc.data_path.startswith(f'pose.bones["{bone}"].'):
            prop = fc.data_path.rsplit(".", 1)[-1]
            if prop in ("location", "rotation_quaternion", "rotation_euler"):
                out[(prop, fc.array_index)] = fc
    return out


def is_applied(action) -> bool:
    return action is not None and BACKUP_KEY in action


def removed(action) -> list:
    """What In place took out of *action*, per span: the model and its numbers."""
    if not is_applied(action):
        return []
    return json.loads(action[BACKUP_KEY]).get("removed", [])


def _backup(action, curves, report):
    keys = {f"{p}|{i}": [[k.co.x, k.co.y, k.handle_left.x, k.handle_left.y, k.handle_right.x,
                          k.handle_right.y, k.interpolation] for k in fc.keyframe_points]
            for (p, i), fc in curves.items()}
    action[BACKUP_KEY] = json.dumps({"keys": keys, "removed": report})


def restore(arm, action) -> bool:
    """Put the travel back: the root's original keys, as they were."""
    if arm is None or not is_applied(action):
        return False
    top = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    saved = json.loads(action[BACKUP_KEY])["keys"]
    curves = _root_curves(action, top.name) if top is not None else {}
    for key, pts in saved.items():
        p, i = key.split("|")
        fc = curves.get((p, int(i)))
        if fc is None:
            continue
        kps = fc.keyframe_points
        if len(kps) != len(pts):
            while len(kps):
                kps.remove(kps[0], fast=True)
            kps.add(len(pts))
        for kp, (x, y, lx, ly, rx, ry, interp) in zip(kps, pts):
            kp.co = (x, y)
            kp.handle_left = (lx, ly)
            kp.handle_right = (rx, ry)
            kp.interpolation = interp
        fc.update()
    del action[BACKUP_KEY]
    return True


def forget(action) -> None:
    """Keep the result: drop the original keys kept for switching back (at Accept)."""
    if action is not None and BACKUP_KEY in action:
        del action[BACKUP_KEY]


SERVER_KEY = "animatica_server_trajectory"


def _from_server(action, body, first, last):
    """The server's trajectory for frames first..last (MMCP ``trajectory``), laid
    onto the take as it is here, or None to find one locally.

    The take may have been moved since the server made it -- a loop straightened
    and put back where the character stood, the rig placed in the scene -- so the
    server's path is fitted onto this take's own travel path by a rotation and a
    shift about the vertical. If the two do not agree within tolerance, the take
    was changed more than that (edited keys), and the local trajectory is used."""
    raw = action.get(SERVER_KEY) if action is not None else None
    if not raw:
        return None
    try:
        t = json.loads(raw)
        f0 = int(t["frame_start"])
        pos = np.asarray(t["position"], float)
        yaw = np.asarray(t["yaw"], float)
    except (KeyError, TypeError, ValueError):
        return None
    a, b = first - f0, last - f0
    if a < 0 or b >= len(pos) or b - a < 2:
        return None
    srv = np.stack([pos[a:b + 1, 0], -pos[a:b + 1, 1]], 1)        # glTF (x, z) -> +Z up (x, y)
    local, _ = body.travel_path()
    if len(local) != len(srv):
        return None
    # rigid fit about the vertical (Procrustes, no scale); a path that barely
    # travels only shifts
    ms, ml = srv.mean(0), local.mean(0)
    A, B = srv - ms, local - ml
    if np.linalg.norm(A, axis=1).max() > 0.05:
        h = A.T @ B
        ang = math.atan2(h[0, 1] - h[1, 0], h[0, 0] + h[1, 1])
    else:
        ang = 0.0
    c, s = math.cos(ang), math.sin(ang)
    xy = (A @ np.array([[c, s], [-s, c]])) + ml
    dist = float(np.linalg.norm(np.diff(local, axis=0), axis=1).sum())
    if not _within(xy, local, dist):
        return None                  # a server's older, looser fit, or a take edited since
    y = yaw[a:b + 1] - yaw[a]
    prm = dict(t.get("params") or {}, source="server")
    return xy, y, str(t.get("model", "server")), prm


def _set_key(fc, f, value):
    for kp in fc.keyframe_points:
        if round(kp.co.x) == f:
            kp.co.y = value
            return
    kp = fc.keyframe_points.insert(f, value, options={'FAST'})
    kp.type = 'GENERATED'            # the take's, not the artist's: Reject strips it


def apply(arm, action, scene, spans, fps: float | None = None) -> list:
    """Take the travel out of *action* over each span ``(first, last, loop)``
    (a prompt block each; a loop's cycle), keeping the original keys so
    :func:`restore` can put it back. Returns what was taken out, per span."""
    if arm is None or action is None or not spans:
        return []
    restore(arm, action)
    top = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    if top is None:
        return []
    fps = fps or scene.render.fps / scene.render.fps_base
    curves = _root_curves(action, top.name)
    if not all(("location", i) in curves for i in range(3)):
        return []
    rot_prop = "rotation_quaternion" if top.rotation_mode == 'QUATERNION' else "rotation_euler"
    A = arm.matrix_world.copy()
    Ai = A.inverted()
    Ri = top.bone.matrix_local.inverted()
    plan, report = {}, []
    hold = None
    turned = 0.0                      # heading earlier spans took out: a turn carries on
    for first, last, loop in sorted(spans):
        if last - first < 2:
            continue
        S = sample(arm, scene, first, last)
        body = Body(S, fps, loop)
        got = _from_server(action, body, first, last)
        xy, yaw, model, prm = got if got is not None else trajectory(body)
        if loop:
            # A cycle repeats with its own travel added each time (Cycles,
            # repeat with offset): the trajectory taken out must be exactly that
            # travel, or what is left adds up (1.9 cm a cycle on a walk).
            # Spread the difference over the cycle; an arc closes its turn too.
            u = np.linspace(0, 1, len(xy))[:, None]
            w0, w1 = A @ S["top"][0], A @ S["top"][-1]
            true = np.array([w1.translation.x - w0.translation.x, w1.translation.y - w0.translation.y])
            xy = xy + (true - (xy[-1] - xy[0])) * u
            if model == "arc":
                f0, f1 = w0.to_3x3() @ _FWD, w1.to_3x3() @ _FWD
                turn = math.atan2(f0.x * f1.y - f0.y * f1.x, f0.x * f1.x + f0.y * f1.y)
                yaw = yaw + (turn - yaw[-1]) * u[:, 0]
        if hold is None:
            hold = xy[0].copy()
        for k, f in enumerate(range(first, last + 1)):
            y = turned + float(yaw[k])
            c, s = math.cos(-y), math.sin(-y)
            # T(hold) Rz(-y) T(-xy): the root back where the take began, facing as it did
            dx, dy = -xy[k, 0], -xy[k, 1]
            M = Matrix(((c, -s, 0, hold[0] + c * dx - s * dy), (s, c, 0, hold[1] + s * dx + c * dy),
                        (0, 0, 1, 0), (0, 0, 0, 1)))
            plan[f] = (Ri @ (Ai @ M @ A @ S["top"][k]), abs(y) > 1e-5)
        turned += float(yaw[-1])
        report.append({"frames": [first, last], "loop": bool(loop), "model": model, **prm})
    if not plan:
        return []
    _backup(action, curves, report)
    turn = any(r for _, r in plan.values())
    prev_q = None
    for f in sorted(plan):
        loc, q, _ = plan[f][0].decompose()
        for i in range(3):
            _set_key(curves[("location", i)], f, loc[i])
        if turn:
            if rot_prop == "rotation_quaternion":
                if prev_q is not None:
                    q.make_compatible(prev_q)
                prev_q = q
                vals = list(q)
            else:
                vals = list(q.to_euler(top.rotation_mode))
            for i, v in enumerate(vals):
                fc = curves.get((rot_prop, i))
                if fc is not None:
                    _set_key(fc, f, v)
    for fc in curves.values():
        fc.update()
    for r in report:
        nums = {k: v for k, v in r.items() if k not in ("frames", "loop", "model")}
        print(f"[animatica] in place {r['frames'][0]}-{r['frames'][1]}: took out a {r['model']} {json.dumps(nums)}")
    return report
