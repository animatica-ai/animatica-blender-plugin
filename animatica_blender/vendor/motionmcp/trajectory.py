# SPDX-License-Identifier: Apache-2.0
"""The travel trajectory of a motion: the path it moves along, and nothing else.

A client that wants a motion *in place* (a game cycle, a controller-driven
character) must take the travel out. Pinning the root on the ground takes out
far more: the pelvis surges and sways with every step, and pinned, all of it
is gone -- played back by a game at the walk's speed, planted feet slide by it.
So the server returns the travel itself, per sample, and a client removes
exactly that (the inverse of the trajectory, about the vertical) and keeps the
body's own sway, surge, height and twist.

How it is found:

1. **The body.** Forward kinematics from the glTF's own rest pose and
   rotations; the centre of mass (COM) from the body's segments with standard
   mass fractions (Winter 2009); what touches the floor each frame: heels,
   balls of the feet, knees, hands, elbows, back, head -- low and nearly still.
2. **The travel path.** The COM with the gait taken out. A loop's COM is
   averaged over one contact cycle (a stride), which cancels the step-by-step
   sway and surge exactly. A one-shot is read by phases: standing on a fixed
   base (a crouch before a jump, a lean) the path holds still; in the air it
   runs straight and even from take-off to landing; while the contacts change
   (steps, a roll, a crawl) it follows the averaged COM.
3. **The simplest trajectory** a game can re-apply, within tolerance of that
   path: still, a straight line at a constant speed, an arc at a constant speed
   and turn rate, one of those timed by a monotone distance curve (starts,
   stops, jumps; keyed at take-off, landing and stops), or failing those a
   cubic Bezier. Loops may only be still, a line or an arc, which repeat; a
   loop's trajectory closes on exactly the root's travel over the cycle.

Wire form (``MMCP_motion.samples[b].trajectory``), in the glTF frame
(right-handed, +Y up, metres):

    {"model": "line" | "arc" | "still" | "line + distance curve" | ...,
     "loop": bool,
     "params": {...},                 # the model's numbers (speed, turn rate, keys)
     "position": [[x, z], ...],       # per frame, on the ground plane
     "yaw": [...]}                    # per frame, radians about +Y, 0 at frame 0

To play a sample in place, move frame *t* by the inverse of
``T(position[t]) * Ry(yaw[t])`` and then by ``T(position[0])``: the root ends
where it began, facing the way it began.

Numpy only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

__all__ = ["compute_trajectories", "fk_positions", "resolve_roles", "trajectory_from_points"]

# --- the rig ---------------------------------------------------------------------------

_PREFIX = ("mixamorig:", "mixamorig1:", "def-", "org-", "bip01 ", "bip001 ", "b_")
_SIDE_TAG = {"l": ("left", ".l", "_l", " l", "-l", "l_", "l."), "r": ("right", ".r", "_r", " r", "-r", "r_", "r.")}
_LIMB = {  # base name (lower case, side and prefix removed) -> role
    "upleg": "leg", "upperleg": "leg", "thigh": "leg", "hip": "leg",
    "shin": "shin", "calf": "shin", "lowerleg": "shin", "knee": "shin",
    "foot": "foot", "ankle": "foot",
    "toebase": "toe", "toe": "toe", "toes": "toe", "ball": "toe",
    "arm": "arm", "upperarm": "arm",
    "forearm": "forearm", "lowerarm": "forearm", "elbow": "forearm",
    "hand": "hand", "wrist": "hand",
    "handmiddleend": "hand_end", "handend": "hand_end", "handmiddle4": "hand_end", "handmiddle1": "hand_end",
    "middle1": "hand_end", "middle_01": "hand_end",
}


def _side_base(name: str):
    n = name.lower()
    for p in _PREFIX:
        n = n.removeprefix(p)
    for side, tags in _SIDE_TAG.items():
        for t in tags:
            if t in ("left", "right") and n.startswith(t):
                return side, n[len(t):].strip("_.- ")
            if not t.startswith(("left", "right")):
                if n.endswith(t):
                    return side, n[: -len(t)].strip("_.- ")
                if t.endswith(("_", ".")) and n.startswith(t):
                    return side, n[len(t):].strip("_.- ")
    return None, n


def resolve_roles(joint_names: Sequence[str], parents: Mapping[str, str | None],
                  canonical_to_request: Mapping[str, str] | None = None) -> dict[str, int]:
    """Role -> joint index, from the rig's own names and structure: SOMA
    (``LeftLeg`` thigh, ``LeftShin``), Mixamo / Core27 (``LeftUpLeg`` thigh,
    ``LeftLeg`` shin), Unreal (``thigh_l``, ``calf_l``, ``ball_l``), Rigify
    (``DEF-thigh.L``) and the like; the spine and neck from the chain between
    the head and the root. Names it cannot read are looked up through the
    server's retargeting map, as the canonical joint they stand for. Missing
    roles are left out (the COM is made of what is there)."""
    inv = {v: k for k, v in (canonical_to_request or {}).items()}
    index = {n: i for i, n in enumerate(joint_names)}
    out: dict[str, int] = {}

    def assign(key, name):
        if key not in out and name in index:
            out[key] = index[name]

    for attempt in (lambda n: n, lambda n: inv.get(n, n)):
        read = {n: _side_base(attempt(n)) for n in joint_names}
        mixamo_legs = {side for side, base in read.values() if base in ("upleg", "upperleg", "thigh")}
        for n, (side, base) in read.items():
            if side is None:
                continue
            role = _LIMB.get(base)
            if base == "leg":                    # SOMA's thigh, Mixamo's shin
                role = "shin" if side in mixamo_legs else "leg"
            if role is not None:
                assign(f"{side}_{role}", n)
    # the trunk: the root, and the chain from the head down to it
    root = next((n for n in joint_names if parents.get(n) is None), None)
    if root is not None:
        out.setdefault("hips", index[root])
    head = next((n for n in joint_names if _side_base(inv.get(n, n))[1] in ("head", "head_01")), None)
    if head is not None:
        chain, n = [], head
        while n is not None and n in index:
            chain.append(n)
            n = parents.get(n)
        chain.reverse()                           # root .. head
        if chain and chain[0] == root:
            out.setdefault("head", index[head])
            if len(chain) >= 3:
                out.setdefault("neck", index[chain[-2]])
            if len(chain) >= 4:
                out.setdefault("spine", index[chain[1]])
            if len(chain) >= 5:
                out.setdefault("chest", index[chain[-3]])
    return out


# --- forward kinematics -------------------------------------------------------------

def _quat_to_mat(q: np.ndarray) -> np.ndarray:
    """(..., 4) (x, y, z, w) -> (..., 3, 3)."""
    q = q / np.maximum(np.linalg.norm(q, axis=-1, keepdims=True), 1e-12)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    m = np.empty(q.shape[:-1] + (3, 3))
    m[..., 0, 0] = 1 - 2 * (y * y + z * z); m[..., 0, 1] = 2 * (x * y - z * w); m[..., 0, 2] = 2 * (x * z + y * w)
    m[..., 1, 0] = 2 * (x * y + z * w); m[..., 1, 1] = 1 - 2 * (x * x + z * z); m[..., 1, 2] = 2 * (y * z - x * w)
    m[..., 2, 0] = 2 * (x * z - y * w); m[..., 2, 1] = 2 * (y * z + x * w); m[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def fk_positions(joints: Sequence[Mapping[str, Any]], joint_names: Sequence[str],
                 rotations_quat: np.ndarray, root_translations: np.ndarray):
    """World positions ``(B, T, J, 3)`` and rotations ``(B, T, J, 3, 3)`` of the
    joints, as the glTF plays them: a joint's rotation channel replaces its
    rest rotation, the root's translation channel replaces its rest translation."""
    byname = {j["name"]: j for j in joints}
    idx = {n: i for i, n in enumerate(joint_names)}
    B, T, J, _ = rotations_quat.shape
    R = _quat_to_mat(np.asarray(rotations_quat, float))
    pos = np.zeros((B, T, J, 3))
    rot = np.zeros((B, T, J, 3, 3))
    order, seen = [], set()

    def visit(n):
        if n in seen:
            return
        p = byname[n].get("parent")
        if p is not None:
            visit(p)
        seen.add(n)
        order.append(n)

    for n in joint_names:
        visit(n)
    for n in order:
        i = idx[n]
        p = byname[n].get("parent")
        if p is None:
            pos[:, :, i] = root_translations
            rot[:, :, i] = R[:, :, i]
        else:
            k = idx[p]
            off = np.asarray(byname[n]["rest_translation"], float)
            pos[:, :, i] = pos[:, :, k] + rot[:, :, k] @ off
            rot[:, :, i] = rot[:, :, k] @ R[:, :, i]
    return pos, rot


def _points(pos: np.ndarray, rot: np.ndarray, roles: Mapping[str, int]) -> dict:
    """Per role (head, tail) positions ``(T, 3)``, converted to +Z up (x, -z, y)
    so the ground plane is (x, y)."""
    def zup(p):
        return np.stack([p[..., 0], -p[..., 2], p[..., 1]], -1)

    P = {r: pos[:, i] for r, i in roles.items()}
    out = {}
    tails = {"l_hand": "l_hand_end", "r_hand": "r_hand_end", "l_foot": "l_toe", "r_foot": "r_toe"}
    chain = {"hips": "spine", "spine": "chest", "chest": "neck", "neck": "head", "l_arm": "l_forearm",
             "l_forearm": "l_hand", "r_arm": "r_forearm", "r_forearm": "r_hand", "l_leg": "l_shin",
             "l_shin": "l_foot", "r_leg": "r_shin", "r_shin": "r_foot"}
    for r, head in P.items():
        if r.endswith(("_end", "_toe")):
            continue
        t = tails.get(r) or chain.get(r)
        if t is not None and t in P:
            tail = P[t]
        elif r == "head" and "neck" in P:
            tail = head + (head - P["neck"]) * 1.2         # the top of the head
        elif r in ("l_hand", "r_hand"):
            fa = "l_forearm" if r[0] == "l" else "r_forearm"
            tail = head + (head - P[fa]) * 0.6 if fa in P else head
        else:
            tail = head
        out[r] = (zup(head), zup(tail))
    return out


# --- the body -------------------------------------------------------------------------

_SEGMENTS = [("hips", "spine", 0.142, 0.5), ("spine", "chest", 0.139, 0.5), ("chest", "neck", 0.216, 0.5),
             ("neck", ("tail", "head"), 0.081, 0.6)]
for _s in ("l", "r"):
    _SEGMENTS += [(f"{_s}_arm", f"{_s}_forearm", 0.028, 0.436), (f"{_s}_forearm", f"{_s}_hand", 0.016, 0.43),
                  (f"{_s}_hand", ("tail", f"{_s}_hand"), 0.006, 0.5), (f"{_s}_leg", f"{_s}_shin", 0.100, 0.433),
                  (f"{_s}_shin", f"{_s}_foot", 0.0465, 0.433), (f"{_s}_foot", ("tail", f"{_s}_foot"), 0.0145, 0.5)]

# (group, role, head|tail, height above the floor when that part is on it (m, for a
# figure with its hips 0.9 m up), torso?)
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


class _Body:
    """A motion, read for its travel: the COM and what touches the floor (+Z up)."""

    def __init__(self, J, fps, loop):
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
        """Which way the body faces, per frame (radians, + turns left, about +Z):
        square to the line across its hips (the shoulders twist with the arms;
        they are only read without hip joints). None without either."""
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

    def touchdowns(self) -> dict:
        """The frames each foot comes down on (sync markers for a game)."""
        out = {}
        for group, name in (("l foot", "LeftFootDown"), ("r foot", "RightFootDown")):
            d = np.zeros(self.n, bool)
            for g, _p, dd in self.contacts:
                if g == group:
                    d |= dd
            out[name] = [i for i in range(1, self.n) if d[i] and not d[i - 1]]
            if self.loop and self.n > 2 and d[0] and not d[-2]:
                out[name].insert(0, 0)                  # a cycle that starts on it
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
        for kind, i, j in runs:
            if kind == "move":
                a = p[i - 1] - avg[i] if i > 0 and not np.isnan(p[i - 1, 0]) else np.zeros(2)
                b = p[j + 1] - avg[j] if j + 1 < n and not np.isnan(p[j + 1, 0]) else np.zeros(2)
                u = np.linspace(0, 1, j - i + 1)[:, None]
                p[i:j + 1] = avg[i:j + 1] + a + (b - a) * u
        for kind, i, j in runs:
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
#: + 2.5 % of the distance, max 4 cm + 5 % (a motion's own wobble about a line
#: is a few % of its length; a real curve is far outside it). Along the path
#: there is no tolerance, because there is no error: the trajectory moves along
#: its shape with the body (see _retime). Same as the Blender addon's inplace.py.
TOL_RMS, TOL_MAX, TOL_REL = 0.015, 0.04, 0.025
#: distance-curve keys at most this far apart (s), besides the take's events
KEY_GAP = 0.25
#: on the spot: never strays further than this (or creeps < 8 cm at < 5 cm/s)
STILL_MAX = 0.06
#: ...or ends within ON_SPOT_NET of where it began and never strays further than
#: ON_SPOT_REACH from it: a jump up that lands 10 cm back, a turn, a stumble
#: caught. That is the body settling, not travel, and in place leaves it as it is
#: (a line taken out of a jump up moved it; one sidestep, ~30 cm, still travels).
ON_SPOT_NET, ON_SPOT_REACH = 0.15, 0.25


def on_the_spot(path) -> bool:
    """Does this travel path go nowhere (see ON_SPOT_NET)?"""
    path = np.asarray(path, float)
    if len(path) < 2:
        return True
    return (float(np.linalg.norm(path[-1] - path[0])) < ON_SPOT_NET
            and float(np.linalg.norm(path - path[0], axis=1).max()) < ON_SPOT_REACH)


def _pchip(tk, yk, t):
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


def _monotone_keys(s, T, tk):
    ks = np.interp(tk, T, s)
    return np.maximum.accumulate(ks) if ks[-1] >= ks[0] else np.minimum.accumulate(ks)


def _circle(P):
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
    n = len(xy)
    tan = np.gradient(xy, axis=0)
    ang = np.unwrap(np.arctan2(tan[:, 0], -tan[:, 1]))
    moving = np.linalg.norm(tan, axis=1) * fps > 0.05
    if not moving.any():
        return np.zeros(n)
    idx = np.where(moving)[0]
    ang = np.interp(np.arange(n), idx, ang[idx])
    return ang - ang[idx[0]]


#: The trajectory keeps a simple shape -- a game can draw it, or steer along
#: it -- but moves along it with the body, frame by frame: its centre of mass,
#: projected onto the shape. At a constant speed along a line, the body's own
#: speeding up and slowing down within each stride stayed in the pose, and
#: played in place it floated forward and back; timed by the body, only the
#: sway across the path, the height and the facing stay.
MODELS = ("still", "line", "arc", "bezier")
LOOPABLE = ("still", "line", "arc")
#: the COM is smoothed over this (s) before it times the trajectory: frame jitter, not the stride
TIMING_SMOOTH = 0.1
#: past its ends the shape runs straight on this far (m), for a body that starts behind it
_EXTEND = 3.0


def _shape(model, path, T, tk):
    """The travel path laid onto one shape (+Z up): each frame's point on it,
    in order along it, and the shape's numbers; None if it does not apply.
    Headings: 0 faces -Y (the glTF +Z), + turns left."""
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
        cp = [[round(float(a), 3), round(float(-b), 3)] for a, b in Pc]     # glTF (x, z)
        return Bm @ Pc, {"control_points_xz": cp}, _extend(_bezier(Pc))
    return None


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


def _select(body: _Body):
    """The simplest shape within tolerance of the travel path: (xy along it
    timed by the travel path, model, numbers)."""
    path, labels = body.travel_path()
    n, fps = len(path), body.fps
    if n < 3:
        return path, "still", {}, None
    T = np.arange(n) / fps
    dist = float(np.linalg.norm(np.diff(path, axis=0), axis=1).sum())
    tol_rms, tol_max = TOL_RMS + TOL_REL * dist, TOL_MAX + 2 * TOL_REL * dist
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
        _along, across = _split_error(xy, path)
        if m == "still":
            net = float(np.linalg.norm(path[-1] - path[0]))
            ok = mx <= STILL_MAX or (net < 0.08 and dist / max(float(T[-1]), 1e-6) < 0.05) or on_the_spot(path)
        else:
            ok = float(np.sqrt((across ** 2).mean())) <= tol_rms and float(across.max()) <= tol_max
        prm = dict(prm, off_cm=round(float(np.sqrt((across ** 2).mean())) * 100, 1))
        if ok:
            return xy, m, prm, guide
        if best is None or rms < best[4]:
            best = (xy, m, prm, guide, rms)
    return best[:4]


# --- the heading ----------------------------------------------------------------------

#: The heading is where the body faces (square to its hips), not where its
#: path goes: a strafe keeps facing forward, a backpedal does not spin round, a
#: turn on the spot turns. A turn is what lasts: the facing is averaged over a
#: stride (at least HEADING_WINDOW s), and what it ends up turned by counts,
#: not how far it twists on the way (a landing twists the pelvis ~15 degrees
#: and back). Fixed under YAW_STILL of it, a steady turn within YAW_TOL of
#: one, else keyed like the distance curve, only ever turning one way. On the
#: spot only a turn of TURN_ON_SPOT counts. A loop turns steadily, and closes.
#: Same as the Blender addon's inplace.py.
HEADING_WINDOW = 0.5
YAW_STILL = math.radians(4.0)
YAW_TOL_RMS, YAW_TOL_MAX = math.radians(2.0), math.radians(5.0)
TURN_ON_SPOT = math.radians(15.0)


def _heading(body: _Body, on_spot: bool):
    """(heading change from 0, how it was fitted, the raw facing or None)."""
    n = body.n
    raw = body.facing() if n >= 3 else None
    if raw is None:
        return None, "path", None
    _path, labels = body.travel_path()
    T = np.arange(n) / body.fps
    tk = _key_times(labels, T)
    sm = _box(raw, np.maximum(body.window(), HEADING_WINDOW * body.fps), body.loop)
    sm = sm - sm[0]
    rate = float(np.dot(T, sm) / max(float(np.dot(T, T)), 1e-12))
    steady = rate * T
    if body.loop:
        return steady, "steady", raw
    if abs(float(sm[-1])) < (TURN_ON_SPOT if on_spot else YAW_STILL):
        return np.zeros(n), "fixed", raw
    e = np.abs(steady - sm)
    if float(np.sqrt((e ** 2).mean())) <= YAW_TOL_RMS and float(e.max()) <= YAW_TOL_MAX:
        return steady, "steady", raw
    y = _pchip(tk, _monotone_keys(sm, T, tk), T)
    return y - y[0], "keyed", raw


def trajectory_from_points(J: Mapping[str, tuple], fps: float, loop: bool = False,
                           root_xy: np.ndarray | None = None, root_turn: float | None = None) -> dict | None:
    """The trajectory of one motion from per-role (head, tail) positions ``(T, 3)``
    in a +Z-up frame (ground plane x, y). A loop closes on ``root_xy`` (the
    root's travel over the cycle, (2,)) and ``root_turn`` (radians), when given.
    Returns the wire dict in that +Z-up frame's (x, y) -- see
    :func:`compute_trajectories` for the glTF frame -- or None without hips."""
    if "hips" not in J:
        return None
    body = _Body(J, fps, loop)
    xy, model, prm, guide = _select(body)
    timed = _retime(guide, body.com, fps, loop) if model != "still" else None
    if timed is not None:
        xy = timed
    yaw, how, raw = _heading(body, model == "still")
    if yaw is None:                    # no hips or shoulders to read a facing from: the path's
        yaw, how = _tangent_yaw(xy, fps), "path"
    if loop and len(xy) > 1:
        u = np.linspace(0, 1, len(xy))
        if root_xy is not None:
            xy = xy + (np.asarray(root_xy, float) - (xy[-1] - xy[0]))[None] * u[:, None]
        # a cycle closes facing the way it began, plus its turn (a line's few
        # degrees of drift were a seam)
        turn = float(raw[-1] - raw[0]) if raw is not None else root_turn
        if turn is not None and (raw is not None or model == "arc"):
            yaw = yaw + (turn - yaw[-1]) * u
    if model != "still" and len(xy) > 1:
        v = np.linalg.norm(np.gradient(xy, axis=0), axis=1) * fps
        prm = dict(prm, speed=round(float(v.mean()), 3),
                   speed_range=[round(float(v.min()), 3), round(float(v.max()), 3)])
        if model == "arc":             # the path's own turn (its direction's), not the body's facing
            turned = math.degrees(float(_tangent_yaw(xy, fps)[-1]))
            prm["turned_deg"] = round(turned, 1)
            prm["turn_deg_s"] = round(turned / max((len(xy) - 1) / fps, 1e-6), 2)
    return {"model": model, "loop": bool(loop), "params": prm, "heading": how,
            "markers": body.touchdowns(), "xy": xy, "yaw": yaw}


def compute_trajectories(*, skeleton: Mapping[str, Any], joint_names: Sequence[str],
                         rotations_quat: np.ndarray, root_translations: np.ndarray, fps: float,
                         loop: bool = False, canonical_to_request: Mapping[str, str] | None = None) -> list:
    """Per sample, the wire ``trajectory`` dict (glTF frame), or None where the
    rig's body could not be read (no hips)."""
    parents = {j["name"]: j.get("parent") for j in skeleton["joints"]}
    roles = resolve_roles(joint_names, parents, canonical_to_request)
    if "hips" not in roles:
        return [None] * int(rotations_quat.shape[0])
    pos, rot = fk_positions(skeleton["joints"], joint_names, rotations_quat, root_translations)
    out = []
    root = next(i for i, n in enumerate(joint_names)
                if next(j for j in skeleton["joints"] if j["name"] == n).get("parent") is None)
    for b in range(pos.shape[0]):
        J = _points(pos[b], rot[b], roles)
        root_xy = root_turn = None
        if loop:
            p0, p1 = pos[b, 0, root], pos[b, -1, root]
            root_xy = np.array([p1[0] - p0[0], -(p1[2] - p0[2])])
            # the root's turn over the cycle, about the vertical: whichever of its
            # own axes lies flattest (a pelvis's up axis says nothing about turning)
            axes = rot[b, 0, root].T
            ax = axes[int(np.argmin(np.abs(axes[:, 1])))]
            a0, a1 = rot[b, 0, root] @ (rot[b, 0, root].T @ ax), rot[b, -1, root] @ (rot[b, 0, root].T @ ax)
            f0, f1 = np.array([a0[0], -a0[2]]), np.array([a1[0], -a1[2]])
            root_turn = math.atan2(f0[0] * f1[1] - f0[1] * f1[0], f0 @ f1)
        t = trajectory_from_points(J, fps, loop, root_xy, root_turn)
        if t is None:
            out.append(None)
            continue
        xy = t.pop("xy")
        yaw = t.pop("yaw")
        t["position"] = [[round(float(x), 4), round(float(-y), 4)] for x, y in xy]    # glTF (x, z)
        t["yaw"] = [round(float(v), 5) for v in yaw]
        out.append(t)
    return out
