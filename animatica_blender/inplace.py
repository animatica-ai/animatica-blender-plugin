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
3. **The simplest trajectory.** A shape a game can re-apply: still, a
   straight line, an arc, or failing those a cubic Bezier -- the simplest
   within tolerance across the path wins; loops may only be still, a line or
   an arc, which repeat. Along it, the root moves with the body (its COM,
   projected onto the shape), not at a constant speed: at a constant speed
   the body's speeding up and slowing down within each stride stayed in the
   pose, and in place it floated forward and back.
4. **The heading.** Where the body faces (square to its hips, averaged over a
   stride), not where its path goes: a strafe keeps facing forward, a turn on
   the spot turns. Only a lasting turn counts; a twist that comes back does not.
5. **Removed.** Each frame the top bone is moved by the inverse of that
   trajectory and heading, about the vertical: the take stays where it began,
   facing the way it began. Heights are never touched. A take that goes nowhere
   (a jump up) is left as it is, unless it turns on the spot.

Toggling In place off puts the original keys back: they are kept on the action
until Accept, and then what was taken out is kept instead (ROOT_MOTION_KEY):
it is the root motion a game export puts on a ground root bone
(game_export.py). A path edited with Autoposer Pro re-paths the take instead.
Root keys the artist sets meanwhile (a crouch keyed in place) are kept: put
back with the travel, and In place taken out of them again.
Numpy only (Blender ships it; not scipy).
"""

from __future__ import annotations

import hashlib
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
        self.J = J
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

    def facing(self):
        """Which way the body faces, per frame (world, radians, + turns left):
        square to the line across its hips (the shoulders twist with the arms;
        they are only read on a rig without hip joints). None without either."""
        J = self.J
        for left, right in (("l_leg", "r_leg"), ("l_arm", "r_arm")):
            if left in J and right in J:
                lat = J[right][0][:, :2] - J[left][0][:, :2]
                break
        else:
            return None
        if float(np.linalg.norm(lat, axis=1).min()) < 1e-6:
            return None
        return np.unwrap(np.arctan2(lat[:, 0], -lat[:, 1]))          # up x right

    def support_z(self):
        """How high what it stands on is, per frame, above the take's lowest
        (stairs, a ledge climbed): the lowest part down, held through the air
        and averaged over a stride."""
        n = self.n
        z = np.full(n, np.nan)
        for i in range(n):
            down = [p[i, 2] for g, p, d in self.contacts if d[i]]
            if down:
                z[i] = min(down)
        ok = ~np.isnan(z)
        if not ok.any():
            return np.zeros(n)
        idx = np.where(ok)[0]
        z = np.interp(np.arange(n), idx, z[idx])
        z = _box(z, self.window(), False)
        return z - float(np.percentile(z, 5))

    def touchdowns(self, first: int) -> dict:
        """The frames each foot comes down on (sync markers for a game)."""
        out = {}
        for group, name in (("l foot", "LeftFootDown"), ("r foot", "RightFootDown")):
            d = np.zeros(self.n, bool)
            for g, p, dd in self.contacts:
                if g == group:
                    d |= dd
            out[name] = [first + i for i in range(1, self.n) if d[i] and not d[i - 1]]
            if self.loop and self.n > 2 and d[0] and not d[-2]:
                out[name].insert(0, first)          # a cycle that starts on it
        return out

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
        for kind, i, j in runs:                  # on a fixed base: hold still, over the support
            if kind == "base":
                p[i:j + 1] = anc[i:j + 1].mean(0)
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

#: A shape is accepted within these of the travel path, across it: RMS 1.5 cm
#: + 2.5 % of the distance, max 4 cm + 5 % (a take's own wobble about a line is
#: a few % of its length; a real curve is far outside it: a curving walk, 50 cm
#: off a line). Along the path there is no tolerance, because there is no error:
#: the root moves along its shape with the body (see _retime). Same as
#: motionmcp.trajectory.
TOL_RMS, TOL_MAX, TOL_REL = 0.015, 0.04, 0.025
#: distance-curve keys at most this far apart (s), besides the take's events
KEY_GAP = 0.25
#: on the spot: never strays further than this (or creeps under 8 cm at < 5 cm/s)
STILL_MAX = 0.06
#: ...or ends within ON_SPOT_NET of where it began and never strays further than
#: ON_SPOT_REACH from it: a jump up that lands 10 cm back, a turn, a stumble
#: caught. That is the body settling, not travel, and In place leaves the take
#: as it is (a line taken out of a jump up moved it; one sidestep, ~30 cm,
#: still travels). Same as motionmcp.trajectory.
ON_SPOT_NET, ON_SPOT_REACH = 0.15, 0.25


def on_the_spot(path) -> bool:
    """Does this travel path go nowhere (see ON_SPOT_NET)?"""
    path = np.asarray(path, float)
    if len(path) < 2:
        return True
    return (float(np.linalg.norm(path[-1] - path[0])) < ON_SPOT_NET
            and float(np.linalg.norm(path - path[0], axis=1).max()) < ON_SPOT_REACH)


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
    """Close enough to the travel path, across it (where along it the root is,
    is the body's: see _retime)."""
    _along, across = _split_error(xy, path)
    return (float(np.sqrt((across ** 2).mean())) <= TOL_RMS + TOL_REL * dist
            and float(across.max()) <= TOL_MAX + 2 * TOL_REL * dist)


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


def _shape(model, path, T, tk):
    """The travel path laid onto one shape: each frame's point on it (in
    order along it), and the shape's numbers; None if it does not apply."""
    n = len(path)
    if model == "still":
        return np.repeat(path.mean(0)[None], n, 0), {}, None
    if model == "line":
        m = path.mean(0)
        _, _, vt = np.linalg.svd(path - m, full_matrices=False)
        u = vt[0] if vt[0] @ (path[-1] - path[0]) >= 0 else -vt[0]
        t = (path - m) @ u
        guide = m + np.linspace(t.min() - _EXTEND, t.max() + _EXTEND, 400)[:, None] * u
        return m + t[:, None] * u, {"heading_deg": round(math.degrees(math.atan2(u[0], -u[1])), 1)}, guide
    if model == "arc":
        circ = _circle(path)
        if circ is None:
            return None
        c, r = circ
        d = path - c
        ln = np.linalg.norm(d, axis=1, keepdims=True)
        if float(ln.min()) < 1e-6:
            return None
        phi = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
        ext = min(math.pi / 2, _EXTEND / r)            # round on past both ends
        a = np.linspace(phi.min() - ext, phi.max() + ext, 720)
        if phi[-1] < phi[0]:
            a = a[::-1]
        guide = c + r * np.stack([np.cos(a), np.sin(a)], 1)
        return c + r * d / ln, {"radius_m": round(r, 2)}, guide
    if model == "bezier":
        seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
        L = seg.sum()
        if L < 1e-6:
            return None
        e_raw = np.concatenate([[0], np.cumsum(seg)]) / L
        e = np.clip(_pchip(tk, _monotone_keys(e_raw, T, tk), T), 0, 1)
        Bm = np.stack([(1 - e) ** 3, 3 * (1 - e) ** 2 * e, 3 * (1 - e) * e ** 2, e ** 3], 1)
        Pc, *_ = np.linalg.lstsq(Bm, path, rcond=None)
        return Bm @ Pc, {"control_points": [[round(float(a), 3) for a in p] for p in Pc]}, _extend(_bezier(Pc))
    return None


#: The root keeps a simple shape -- a game can draw it, or steer along it --
#: but moves along it with the body, frame by frame: its centre of mass,
#: projected onto the shape. At a constant speed along a line, the body's own
#: speeding up and slowing down within each stride stayed in the pose, and
#: played in place it floated forward and back; timed by the body, only the
#: sway across the path, the height and the facing stay.
MODELS = ("still", "line", "arc", "bezier")
LOOPABLE = ("still", "line", "arc")
#: the COM is smoothed over this (s) before it times the root: frame jitter, not the stride
TIMING_SMOOTH = 0.1
#: past its ends the shape runs straight on this far (m), for a body that starts behind it
_EXTEND = 3.0


def _bezier(Pc, n=400):
    e = np.linspace(0.0, 1.0, n)[:, None]
    return (1 - e) ** 3 * Pc[0] + 3 * (1 - e) ** 2 * e * Pc[1] + 3 * (1 - e) * e ** 2 * Pc[2] + e ** 3 * Pc[3]


def _extend(P):
    """A path's points in order, run straight on past both ends (_EXTEND m)."""
    keep = np.concatenate([[True], np.linalg.norm(np.diff(P, axis=0), axis=1) > 1e-6])
    P = P[keep]
    if len(P) < 2:
        return P

    def tangent(a, b):
        d = b - a
        return d / max(float(np.linalg.norm(d)), 1e-9)
    k = max(1, len(P) // 20)
    return np.vstack([P[0] - tangent(P[0], P[k]) * _EXTEND, P, P[-1] + tangent(P[-1 - k], P[-1]) * _EXTEND])


def _retime(guide, com, fps, loop):
    """Each frame where the body (*com*, per frame) is along a shape: its
    *guide* (the shape's points in order, run on past its ends), at the
    nearest point to the body's COM."""
    if guide is None or len(guide) < 2 or float(np.linalg.norm(np.diff(guide, axis=0), axis=1).sum()) < 1e-3:
        return None
    D = np.diff(guide, axis=0)
    L = np.linalg.norm(D, axis=1)
    cum = np.concatenate([[0.0], np.cumsum(L)])
    c = np.stack([_box(com[:, j], np.full(len(com), TIMING_SMOOTH * fps), loop) for j in range(2)], 1)
    A = guide[:-1]
    u = np.clip(((c[:, None, :] - A[None]) * D[None]).sum(-1) / np.maximum(L ** 2, 1e-12)[None], 0.0, 1.0)
    d2 = ((c[:, None, :] - (A[None] + u[..., None] * D[None])) ** 2).sum(-1)
    near = d2.argmin(1)
    at = cum[near] + u[np.arange(len(c)), near] * L[near]
    return np.stack([np.interp(at, cum, guide[:, j]) for j in range(2)], 1)


def trajectory(body: Body):
    """The simplest shape within tolerance of the travel path, its points
    along it (timed by the travel path; _fit_spans times them by the body).
    Returns (xy (n, 2), model name, its numbers, its guide for _retime)."""
    path, labels = body.travel_path()
    n, fps = len(path), body.fps
    if n < 3:
        return path, "still", {}, None
    T = np.arange(n) / fps
    dist = float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum())
    tk = _key_times(labels, T)
    best = None
    for m in MODELS:
        if body.loop and m not in LOOPABLE:
            continue
        got = _shape(m, path, T, tk)
        if got is None:
            continue
        xy, prm, guide = got
        err = np.linalg.norm(xy - path, axis=1)
        rms, mx = float(np.sqrt((err ** 2).mean())), float(err.max())
        if m == "still":
            net = float(np.linalg.norm(path[-1] - path[0]))
            ok = mx <= STILL_MAX or (net < 0.08 and dist / max(float(T[-1]), 1e-6) < 0.05) or on_the_spot(path)
        else:
            ok = _within(xy, path, dist)
        prm = {**prm, "off_cm": round(float(np.sqrt((_split_error(xy, path)[1] ** 2).mean())) * 100, 1)}
        if ok:
            return xy, m, prm, guide
        if best is None or rms < best[4]:
            best = (xy, m, prm, guide, rms)
    return best[:4]


# --- the heading ----------------------------------------------------------------------

#: The heading taken out is where the body faces (square to its hips and
#: shoulders, averaged over a stride), not where its path goes: a strafe keeps
#: facing forward, a backpedal does not spin round, a turn on the spot turns.
#: A turn is what lasts: the facing is averaged over a stride (at least
#: HEADING_WINDOW), and what it ends up turned by counts, not how far it
#: twists on the way (a landing twists the pelvis 15 degrees and back). Kept
#: fixed under YAW_STILL of it, a steady turn within YAW_TOL of one, else keyed
#: like the distance curve, only ever turning one way. A loop turns steadily,
#: and closes.
HEADING_WINDOW = 0.5
YAW_STILL = math.radians(4.0)
YAW_TOL_RMS, YAW_TOL_MAX = math.radians(2.0), math.radians(5.0)
#: on the spot, only a turn this large comes out (a turn on the spot): less is
#: the body settling, and the take is left as it is
TURN_ON_SPOT = math.radians(15.0)


def heading(body, labels, T, tk, on_spot: bool):
    """The heading change to take out (0 at the start, + turns left), how it
    was fitted, and the body's facing at the start (world, radians; None when
    the rig has no hips or shoulders to tell, and the heading is left alone)."""
    n = body.n
    raw = body.facing() if n >= 3 else None
    if raw is None:
        return np.zeros(n), "fixed", None
    sm = _box(raw, np.maximum(body.window(), HEADING_WINDOW * body.fps), body.loop)
    f0 = float(sm[0])
    sm = sm - sm[0]
    rate = float(np.dot(T, sm) / max(float(np.dot(T, T)), 1e-12))     # a steady turn from 0
    steady = rate * T
    if body.loop:
        return steady, "steady", f0
    if abs(float(sm[-1])) < (TURN_ON_SPOT if on_spot else YAW_STILL):
        return np.zeros(n), "fixed", f0
    e = np.abs(steady - sm)
    if float(np.sqrt((e ** 2).mean())) <= YAW_TOL_RMS and float(e.max()) <= YAW_TOL_MAX:
        return steady, "steady", f0
    y = _pchip(tk, _monotone_keys(sm, T, tk), T)
    return y - y[0], "keyed", f0


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


#: kept on an accepted take: its root motion (the travel it has, or had taken
#: out), for a game export -- see :func:`root_motion`
ROOT_MOTION_KEY = "animatica_root_motion"


def removed(action) -> list:
    """What In place took out of *action* (or, re-pathed, put in), per span:
    the model, its numbers, the travel path and heading."""
    if not is_applied(action):
        return []
    return json.loads(action[BACKUP_KEY]).get("removed", [])


def applied_mode(action) -> str | None:
    """``"in_place"``, ``"repath"`` (moved onto an edited path) or None."""
    if not is_applied(action):
        return None
    return json.loads(action[BACKUP_KEY]).get("mode", "in_place")


def _backup(action, curves, report, mode, hold):
    keys = {f"{p}|{i}": [[k.co.x, k.co.y, k.handle_left.x, k.handle_left.y, k.handle_right.x,
                          k.handle_right.y, k.interpolation, k.type] for k in fc.keyframe_points]
            for (p, i), fc in curves.items()}
    action[BACKUP_KEY] = json.dumps({"keys": keys, "removed": report, "mode": mode,
                                     "hold": [float(hold[0]), float(hold[1])]})


#: on an action In place is applied to: the root keys as In place left them,
#: and the move on the floor it made each frame -- so a key the artist sets on
#: the root meanwhile (a crouch keyed in place) can be told from In place's
#: own, and kept when the original keys are put back
APPLIED_KEY = "animatica_inplace_applied"
#: a root key further than this from what In place wrote is the artist's
_EDIT_TOL = 1e-5


def _remember_applied(action, curves, moves, A) -> None:
    keys = {f"{p}|{i}": [[k.co.x, k.co.y] for k in fc.keyframe_points] for (p, i), fc in curves.items()}
    action[APPLIED_KEY] = json.dumps({"keys": keys, "moves": moves,
                                      "A": [float(v) for row in A for v in row]})


def _root_basis(top, curves, x):
    """The top bone's pose (its own space) the curves give at frame *x*."""
    loc = Vector([curves[("location", i)].evaluate(x) if ("location", i) in curves else top.location[i]
                  for i in range(3)])
    if top.rotation_mode == 'QUATERNION':
        from mathutils import Quaternion
        rot = Quaternion([curves[("rotation_quaternion", i)].evaluate(x)
                          if ("rotation_quaternion", i) in curves else top.rotation_quaternion[i]
                          for i in range(4)])
        rot.normalize()
    elif top.rotation_mode in ('XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'):
        from mathutils import Euler
        rot = Euler([curves[("rotation_euler", i)].evaluate(x) if ("rotation_euler", i) in curves
                     else top.rotation_euler[i] for i in range(3)], top.rotation_mode).to_quaternion()
    else:
        rot = top.matrix_basis.to_quaternion()
    return Matrix.LocRotScale(loc, rot, None)


def _artist_edits(action, top, curves) -> tuple:
    """Root keys the artist set or changed (or deleted) since In place wrote
    them, as the pose they give, taken back to the take's own travel: In
    place's move at that frame undone. ``({frame: (basis, {channel: type})},
    {channel: [frame, ...]} deleted)``."""
    raw = action.get(APPLIED_KEY)
    if not raw or top is None:
        return {}, {}
    try:
        snap = json.loads(raw)
        A = Matrix([snap["A"][r * 4:r * 4 + 4] for r in range(4)])
        moves = {int(f): v for f, v in snap["moves"].items()}
    except (KeyError, TypeError, ValueError):
        return {}, {}
    changed, deleted = {}, {}
    for chan, pts in snap.get("keys", {}).items():
        p, i = chan.split("|")
        fc = curves.get((p, int(i)))
        if fc is None:
            continue
        was = {round(x, 3): y for x, y in pts}
        now = {round(k.co.x, 3): k for k in fc.keyframe_points}
        for x, kp in now.items():
            if x not in was or abs(was[x] - kp.co.y) > _EDIT_TOL:
                changed.setdefault(x, {})[(p, int(i))] = kp.type
        gone = [x for x in was if x not in now]
        if gone:
            deleted[(p, int(i))] = gone
    if not changed:
        return {}, deleted
    Rl = top.bone.matrix_local
    Ri = Rl.inverted()
    Ai = A.inverted()
    out = {}
    for x, types in changed.items():
        mv = moves.get(int(round(x)))
        C = (Ai @ _rigid(mv[:2], mv[2]) @ A) if mv is not None else Matrix.Identity(4)
        out[x] = (Ri @ C.inverted() @ Rl @ _root_basis(top, curves, x), types)
    return out, deleted


def _put_key(fc, x, value, kind) -> None:
    """The artist's key at *x*: over the take's key there (which the original
    keys just put back typed GENERATED), or a new one. Typed *kind* either
    way, so it goes on reading as theirs."""
    for kp in fc.keyframe_points:
        if abs(kp.co.x - x) < 1e-3:
            d = value - kp.co.y
            kp.co.y = value
            kp.handle_left.y += d
            kp.handle_right.y += d
            kp.type = kind
            return
    kp = fc.keyframe_points.insert(x, value, options={'FAST'})
    kp.type = kind


def _fold_edits(top, curves, edits, deleted) -> None:
    """Write the artist's root edits (see :func:`_artist_edits`) into the
    original keys, just put back: they are the take's now."""
    for chan, xs in deleted.items():
        fc = curves.get(chan)
        if fc is None:
            continue
        for x in xs:
            for kp in fc.keyframe_points:
                if abs(kp.co.x - x) < 1e-3:
                    fc.keyframe_points.remove(kp, fast=True)
                    break
    for x, (basis, types) in sorted(edits.items()):
        loc, q, _ = basis.decompose()
        if top.rotation_mode == 'QUATERNION':
            q.make_compatible(_root_basis(top, curves, x).to_quaternion())
            rot = ("rotation_quaternion", list(q))
        elif top.rotation_mode in ('XYZ', 'XZY', 'YXZ', 'YZX', 'ZXY', 'ZYX'):
            from mathutils import Euler
            was = Euler([curves[("rotation_euler", i)].evaluate(x) if ("rotation_euler", i) in curves
                         else 0.0 for i in range(3)], top.rotation_mode)
            rot = ("rotation_euler", list(q.to_euler(top.rotation_mode, was)))
        else:
            rot = (None, [])
        # The artist's, whatever the key they typed over was: a key set over
        # one of the take's stays typed GENERATED in Blender, and folded back
        # as that it read as the take's -- Reject dropped it, and after Accept
        # the next generation did not follow it.
        types = {c: ('KEYFRAME' if t == 'GENERATED' else t) for c, t in types.items()}
        kind = next(iter(types.values()), 'KEYFRAME')
        chans = [(("location", i), v) for i, v in enumerate(loc)] + [((rot[0], i), v) for i, v in enumerate(rot[1])]
        for chan, v in chans:
            fc = curves.get(chan)
            if fc is None:
                continue
            if chan in types or abs(fc.evaluate(x) - v) > _EDIT_TOL:
                _put_key(fc, x, v, types.get(chan, kind))
    for fc in curves.values():
        fc.update()


def restore(arm, action) -> bool:
    """Put the travel back: the root's original keys, as they were, with any
    the artist set on the root since (see :func:`_artist_edits`)."""
    if arm is None or not is_applied(action):
        return False
    top = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    saved = json.loads(action[BACKUP_KEY])["keys"]
    curves = _root_curves(action, top.name) if top is not None else {}
    edits, deleted = _artist_edits(action, top, curves)
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
        for kp, (x, y, lx, ly, rx, ry, interp, *kind) in zip(kps, pts):
            kp.co = (x, y)
            kp.handle_left = (lx, ly)
            kp.handle_right = (rx, ry)
            kp.interpolation = interp
            if kind:
                kp.type = kind[0]
        fc.update()
    if edits or deleted:
        _fold_edits(top, curves, edits, deleted)
    del action[BACKUP_KEY]
    if APPLIED_KEY in action:
        del action[APPLIED_KEY]
    return True


def record(action) -> dict | None:
    """The root motion *action* has: ``{"mode", "hold", "spans"}``, while In
    place is on or re-pathed, or kept since Accept; None if it was never read."""
    if action is None:
        return None
    if is_applied(action):
        b = json.loads(action[BACKUP_KEY])
        return {"mode": b.get("mode", "in_place"), "hold": b.get("hold"), "spans": b.get("removed", [])}
    raw = action.get(ROOT_MOTION_KEY)
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def forget(action) -> None:
    """Keep the result (at Accept): drop the original keys kept for switching
    back, and keep the root motion it had taken out, for a game export."""
    if action is not None and BACKUP_KEY in action:
        rec = record(action)
        if rec is not None and rec.get("hold") is not None:
            action[ROOT_MOTION_KEY] = json.dumps(rec)
        del action[BACKUP_KEY]
    if action is not None and APPLIED_KEY in action:
        del action[APPLIED_KEY]


def carry(src_action, new_actions, blocks) -> None:
    """Accept, split into blocks: each block's action keeps its share of the root motion."""
    rec = record(src_action)
    if rec is None or rec.get("hold") is None:
        return
    for act, (fs, fe, *_rest) in zip(new_actions, blocks):
        spans = [sp for sp in rec["spans"] if fs <= sp["frames"][0] and sp["frames"][1] <= fe]
        if spans:
            act[ROOT_MOTION_KEY] = json.dumps({**rec, "spans": spans})


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
    if len(local) != len(srv) or on_the_spot(local):
        return None                  # on the spot: left as it is (a server's older fit may not say so)
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
    prm = dict(t.get("params") or {}, source="server")
    for k in ("speed", "turn_deg_s", "distance_keys_m", "key_times_s"):
        prm.pop(k, None)                 # an older server's timing: the body times it here
    model = str(t.get("model", "server")).replace(" + distance curve", "")
    return xy, model, prm, _extend(xy)


def _set_key(fc, f, value):
    for kp in fc.keyframe_points:
        if round(kp.co.x) == f:
            kp.co.y = value
            return
    kp = fc.keyframe_points.insert(f, value, options={'FAST'})
    kp.type = 'GENERATED'            # the take's, not the artist's: Reject strips it


def _rigid(p, yaw) -> Matrix:
    """A move on the floor: turn by *yaw* about the vertical, then to *p* (x, y)."""
    c, s = math.cos(yaw), math.sin(yaw)
    return Matrix(((c, -s, 0, p[0]), (s, c, 0, p[1]), (0, 0, 1, 0), (0, 0, 0, 1)))


def _yaw_of(M) -> float:
    return math.atan2(M[1][0], M[0][0])


def _travel(fit) -> list:
    """Per span, frame by frame: ``R``, the trajectory fitted (what In place
    takes out), and ``E``, the travel the take gets (the artist's edited path,
    else ``R``), as moves on the floor. A span carries on from where the one
    before it ended up, edited or not; a turn carries on too."""
    turned, carry_m, out = 0.0, Matrix.Identity(4), []
    for span in fit:
        ys = turned + np.asarray(span["yaw"], float)
        R = [_rigid(p, y) for p, y in zip(span["xy"], ys)]
        ed = span.get("edit")
        if ed is None:
            E = [carry_m @ r for r in R]
        else:
            E = [carry_m @ _rigid(p, turned + y) for p, y in zip(ed["xy"], ed["yaw"])]
        carry_m = E[-1] @ R[-1].inverted()
        turned = float(ys[-1])
        out.append((R, E))
    return out


def _entry(span, E) -> dict:
    """What is kept of a span: its model and numbers, and the travel it gets
    (path, heading), frame by frame, with its markers and support height."""
    ed = span.get("edit")
    info = {"model": "edited" if ed else span["model"], **span["prm"], **(ed["prm"] if ed else {})}
    yaw = np.unwrap([_yaw_of(M) for M in E])
    return {"frames": [span["first"], span["last"]], "loop": span["loop"], **info,
            "heading": span.get("heading", "fixed"),
            "path": [[round(M[0][3], 4), round(M[1][3], 4)] for M in E],
            "yaw": [round(float(v), 5) for v in yaw],
            "floor": round(float(span["floor"]), 4),
            "facing0": span.get("facing0"),
            "markers": span.get("markers", {}),
            "support_z": [round(float(v), 4) for v in span.get("support_z", np.zeros(len(E)))]}


_BULKY = ("frames", "loop", "model", "path", "yaw", "floor", "facing0", "markers", "support_z")


def read_only(action) -> str | None:
    """Why *action*'s keys cannot be changed here, or None: one linked from
    another file (or a library override's) keeps no edit when the file is
    saved, so In place must not look as if it had worked."""
    if action is None:
        return None
    if action.library is not None or getattr(action, "override_library", None) is not None:
        return f"“{action.name}” is linked from another file, so its keys can't be changed here (make it local first)"
    return None


def apply(arm, action, scene, spans, fps: float | None = None, *, reuse: bool = False,
          mode: str = "in_place") -> list:
    """Over each span ``(first, last, loop)`` (a prompt block each; a loop's
    cycle), take the travel out of *action* (``mode="in_place"``), or move the
    take onto the artist's edited path (``"repath"``, Autoposer Pro's). The
    original keys are kept so :func:`restore` can put them back. Returns what
    was done, per span. ``reuse`` takes the take as last sampled (an edit to
    the path changes the path, not the take)."""
    if arm is None or action is None or not spans:
        return []
    why = read_only(action)
    if why:
        print(f"[Animatica] In place: {why}")
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
    fit = _fit_spans(arm, action, scene, spans, fps, reuse=reuse)
    if not fit:
        return []
    hold = np.array(fit[0]["xy"][0], float)
    H = _rigid(hold, 0.0)
    plan, report, moves = {}, [], {}
    for span, (R, E) in zip(fit, _travel(fit)):
        for k, f in enumerate(range(span["first"], span["last"] + 1)):
            # in place: T(hold) R^-1, the root back where the take began, facing as it did;
            # re-pathed: E R^-1, the take moved from its own path onto the edited one
            M = (H if mode == "in_place" else E[k]) @ R[k].inverted()
            plan[f] = (Ri @ (Ai @ M @ A @ span["S"]["top"][k]), abs(_yaw_of(M)) > 1e-5)
            moves[str(f)] = [float(M[0][3]), float(M[1][3]), float(_yaw_of(M))]
        report.append(_entry(span, E))
    _backup(action, curves, report, mode, hold)
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
    _remember_applied(action, curves, moves, A)
    verb = "in place, took out" if mode == "in_place" else "re-pathed onto"
    for r in report:
        nums = {k: v for k, v in r.items() if k not in _BULKY}
        print(f"[animatica] {verb} {r['frames'][0]}-{r['frames'][1]}: a {r['model']} {json.dumps(nums)}")
    return report


#: the last fit, before any edit: ``reuse`` reads it back instead of sampling again
_last_fit: dict = {"key": None, "spans": []}


def clear_cache() -> None:
    """Forget the last fit (a file load: the take it sampled is gone)."""
    _last_fit["key"], _last_fit["spans"] = None, []


def _digest(action) -> str:
    """A hash of every key on *action* (where, and its handles): a fit is only
    reused on the very take it sampled, not on another with the same names
    (a file opened since, keys set or changed since)."""
    h = hashlib.blake2b(digest_size=16)
    from . import constraints_ui
    for fc in constraints_ui.iter_action_fcurves(action):
        kps = fc.keyframe_points
        h.update(f"{fc.data_path}|{fc.array_index}|{len(kps)}|{int(fc.mute)}".encode())
        if len(kps):
            buf = np.empty(len(kps) * 2, np.float32)
            for attr in ("co", "handle_left", "handle_right"):
                kps.foreach_get(attr, buf)
                h.update(buf.tobytes())
    return h.hexdigest()


def _fit_key(arm, action, spans, fps) -> tuple:
    """What a fit was made from: which rig and action (this session's, not just
    their names), where the rig is, and the keys themselves."""
    uid = getattr(action, "session_uid", None) or action.as_pointer()
    auid = getattr(arm, "session_uid", None) or arm.as_pointer()
    return (auid, uid, arm.name, action.name, tuple(sorted(spans)), round(fps, 3),
            tuple(round(v, 6) for row in arm.matrix_world for v in row), _digest(action))


def _fit_spans(arm, action, scene, spans, fps, *, reuse: bool = False):
    """The trajectory of each span ``(first, last, loop)``: the server's path,
    laid onto the take, or one fitted here, and the heading the body faces
    along it; with the artist's edited path alongside, if there is one
    (Autoposer Pro's). Samples the take (moves the playhead, and puts it back)
    unless ``reuse`` finds it sampled already; writes nothing. Call on the take
    as generated (not in place): apply restores it first."""
    from . import posing
    key = _fit_key(arm, action, spans, fps)
    if reuse and _last_fit["key"] == key:
        return posing.path_overlay(action, _last_fit["spans"], fps)
    A = arm.matrix_world.copy()
    out = []
    for first, last, loop in sorted(spans):
        if last - first < 2:
            continue
        S = sample(arm, scene, first, last)
        body = Body(S, fps, loop)
        # a loop is a line or an arc that repeats: the server fitted the whole
        # take, not this cycle of it
        got = None if loop else _from_server(action, body, first, last)
        xy, model, prm, guide = got if got is not None else trajectory(body)
        timed = _retime(guide, body.com, fps, loop) if model != "still" else None
        if timed is not None:
            xy = timed
        path, labels = body.travel_path()
        T = np.arange(len(path)) / fps
        yaw, how, facing0 = heading(body, labels, T, _key_times(labels, T), model == "still")
        if loop:
            # A cycle repeats with its own travel added each time (Cycles,
            # repeat with offset): the trajectory taken out must be exactly that
            # travel, or what is left adds up (1.9 cm a cycle on a walk).
            # Spread the difference over the cycle, and the turn's too, so the
            # cycle closes facing the way it began (a line's few degrees of drift
            # were a seam).
            u = np.linspace(0, 1, len(xy))[:, None]
            w0, w1 = A @ S["top"][0], A @ S["top"][-1]
            true = np.array([w1.translation.x - w0.translation.x, w1.translation.y - w0.translation.y])
            xy = xy + (true - (xy[-1] - xy[0])) * u
            raw = body.facing()
            if raw is not None:
                turn = float(raw[-1] - raw[0])
            else:
                f0, f1 = w0.to_3x3() @ _FWD, w1.to_3x3() @ _FWD
                turn = math.atan2(f0.x * f1.y - f0.y * f1.x, f0.x * f1.x + f0.y * f1.y)
            yaw = yaw + (turn - yaw[-1]) * u[:, 0]
        if model != "still":
            v = np.linalg.norm(np.gradient(xy, axis=0), axis=1) * fps
            prm = {**prm, "speed": round(float(v.mean()), 3),
                   "speed_range": [round(float(v.min()), 3), round(float(v.max()), 3)]}
            if model == "arc":         # the path's own turn (its direction's), not the body's facing
                turned = math.degrees(float(_tangent_yaw(xy, fps)[-1]))
                prm["turned_deg"] = round(turned, 1)
                prm["turn_deg_s"] = round(turned / max(float(T[-1]), 1e-6), 2)
        out.append({"first": first, "last": last, "loop": bool(loop), "xy": xy, "yaw": yaw,
                    "model": model, "prm": prm, "S": S, "floor": body.floor, "heading": how,
                    "facing0": facing0, "markers": body.touchdowns(first), "support_z": body.support_z()})
    _last_fit["key"], _last_fit["spans"] = key, out
    return posing.path_overlay(action, out, fps)


def describe(span: dict) -> str:
    """A span's trajectory in a few words, for the viewport: ``line · 1.05 m/s``."""
    model = span.get("model", "")
    bits = [model.replace(" + distance curve", ", eased")]
    if "speed" in span:
        bits.append(f"{span['speed']:.2f} m/s")
    if "turn_deg_s" in span:
        bits.append(f"{span['turn_deg_s']:+.0f}°/s")
    keys = span.get("distance_keys_m")
    if keys:
        bits.append(f"{keys[-1]:.2f} m")
    elif "path" in span and len(span["path"]) > 1:
        p = np.asarray(span["path"], float)
        bits.append(f"{float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum()):.2f} m")
    if model == "still":
        bits = ["on the spot"]
    if model == "edited":
        bits = ["edited", f"{span.get('length_m', 0.0):.2f} m"]
        if span.get("off_fit_cm"):
            bits.append(f"{span['off_fit_cm']:.0f} cm off the fit")
    yaw = span.get("yaw")
    if yaw and abs(yaw[-1] - yaw[0]) > math.radians(5.0):
        bits.append(f"turns {math.degrees(yaw[-1] - yaw[0]):+.0f}°")
    if span.get("source") == "server":
        bits.append("server")
    return " · ".join(bits)


def fitted_path(arm, action, scene, *, reuse: bool = False) -> list:
    """The travel of the take showing, per span, for the viewport:
    ``[{"frames": [a, b], "path": [[x, y], ...], "floor": z, "label": str}]``
    (world, +Z up). With In place on (or re-pathed, or accepted) it is what
    was kept; otherwise it is fitted now, as In place would (samples the take;
    writes nothing)."""
    if arm is None or action is None:
        return []
    rec = record(action)
    if rec and rec.get("spans") and all("path" in s for s in rec["spans"]):
        return [{"frames": s["frames"], "path": s["path"], "floor": s.get("floor", 0.0),
                 "label": describe(s)} for s in rec["spans"]]
    from . import operators          # noqa: PLC0415 - lazy: operators imports this module
    fps = scene.render.fps / scene.render.fps_base
    fit = _fit_spans(arm, action, scene, operators._inplace_spans(arm, action), fps, reuse=reuse)
    out = []
    for span, (_R, E) in zip(fit, _travel(fit)):
        e = _entry(span, E)
        out.append({"frames": e["frames"], "floor": e["floor"], "path": e["path"], "label": describe(e)})
    return out


def root_motion(arm, action, scene) -> dict | None:
    """The take's root motion, for a game export, frame by frame over its spans:

    ``travel``     [x, y, heading] of the root (world, +Z up; heading + turns left)
    ``in_place``   the keys have it taken out (held at ``hold``), else they travel
    ``facing0``    the way the body faces at the start (world, radians)
    ``markers``    {"LeftFootDown": [frame, ...], "RightFootDown": [...]}
    ``support_z``  the height of what it stands on, above its lowest
    ``spans``      each span's model and numbers

    Read from what In place kept (on, re-pathed or accepted); a take it never
    touched is fitted now (samples the take). None without a take."""
    if arm is None or action is None:
        return None
    rec = record(action)
    if rec is None or rec.get("hold") is None or not rec.get("spans"):
        from . import operators      # noqa: PLC0415
        fps = scene.render.fps / scene.render.fps_base
        fit = _fit_spans(arm, action, scene, operators._inplace_spans(arm, action), fps)
        if not fit:
            return None
        rec = {"mode": "travelling", "hold": [float(v) for v in fit[0]["xy"][0]],
               "spans": [_entry(sp, E) for sp, (_R, E) in zip(fit, _travel(fit))]}
    frames, travel, sz, markers = [], [], [], {}
    for sp in rec["spans"]:
        a, b = sp["frames"]
        n = b - a + 1
        yaw = sp.get("yaw") or [0.0] * n
        zs = sp.get("support_z") or [0.0] * n
        for k in range(n):
            f = a + k
            if frames and f <= frames[-1]:
                continue                                   # a frame two blocks share
            frames.append(f)
            travel.append([sp["path"][k][0], sp["path"][k][1], yaw[k]])
            sz.append(zs[k])
        for name, fr in (sp.get("markers") or {}).items():
            markers.setdefault(name, []).extend(int(x) for x in fr)
    # blocks that leave a gap between them: the travel between is joined up straight
    full = list(range(frames[0], frames[-1] + 1))
    tr = np.asarray(travel, float)
    tr = np.stack([np.interp(full, frames, tr[:, j]) for j in range(3)], 1)
    sz = np.interp(full, frames, sz)
    first = rec["spans"][0]
    return {"frames": full, "travel": tr.tolist(), "in_place": rec["mode"] == "in_place",
            "hold": rec["hold"], "loop": len(rec["spans"]) == 1 and bool(first.get("loop")),
            "facing0": first.get("facing0"), "markers": {k: sorted(set(v)) for k, v in markers.items()},
            "support_z": sz.tolist(), "floor": first.get("floor", 0.0),
            "spans": [{k: v for k, v in sp.items() if k not in ("path", "yaw", "support_z", "markers")}
                      for sp in rec["spans"]]}
