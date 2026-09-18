"""Skeleton maths for the runtime: numpy only, no torch, no onnxruntime.

Ports of `autoposer.skeleton` and the rotation plumbing the solver needs, kept byte-compatible
with the training side — same FK convention (`G_j = G_parent @ L_j`, offsets rotated by the
PARENT), same rest construction from bone lengths, same conditioning guards.
"""
from __future__ import annotations

import numpy as np


def sixd_to_mat(r6: np.ndarray) -> np.ndarray:
    """(...,6) -> (...,3,3). Gram-Schmidt on the first two COLUMNS, as trained."""
    r6 = np.asarray(r6, dtype=np.float32).reshape(-1, 6)
    a1, a2 = r6[:, 0:3], r6[:, 3:6]
    b1 = a1 / np.clip(np.linalg.norm(a1, axis=-1, keepdims=True), 1e-8, None)
    a2 = a2 - (b1 * a2).sum(-1, keepdims=True) * b1
    b2 = a2 / np.clip(np.linalg.norm(a2, axis=-1, keepdims=True), 1e-8, None)
    return np.stack([b1, b2, np.cross(b1, b2)], axis=-1).astype(np.float32)


def mat_to_sixd(m: np.ndarray) -> np.ndarray:
    """(...,3,3) -> (...,6): the first two columns, the inverse of `sixd_to_mat`."""
    m = np.asarray(m, dtype=np.float32).reshape(-1, 3, 3)
    return np.concatenate([m[:, :, 0], m[:, :, 1]], axis=-1).astype(np.float32)


class Skeleton:
    """The skeleton `meta.json` describes. Construction is data, never code."""

    def __init__(self, meta: dict):
        self.names = [str(n) for n in meta["joint_names"]]
        self.parents = np.asarray(meta["parents"], dtype=np.int32)
        self.root_idx = int(meta["root_idx"])
        self.foot_joints = np.asarray(meta["foot_joints"], dtype=np.int64)
        self.neutral = np.asarray(meta["neutral_joints"], dtype=np.float32)
        self.njoints = len(self.names)
        self.blen_mean = np.asarray(meta["blen_mean"], dtype=np.float32).reshape(-1)
        self.blen_std = np.asarray(meta["blen_std"], dtype=np.float32).reshape(-1)
        self.cond_dim = int(meta.get("cond_dim", 0))
        self.floor_ref = bool(meta.get("floor_ref", False))
        self._deg_std = float(meta.get("cond_degenerate_std", 1e-3))
        self._z_max = float(meta.get("cond_z_max", 4.5))
        self._index = {n: i for i, n in enumerate(self.names)}

        # rest DIRECTIONS are fixed by the canonical skeleton; only lengths vary per body
        self._dirs = np.zeros((self.njoints, 3), dtype=np.float32)
        for j in range(self.njoints):
            p = int(self.parents[j])
            if p >= 0 and p != j:
                v = self.neutral[j] - self.neutral[p]
                self._dirs[j] = v / max(float(np.linalg.norm(v)), 1e-8)

        depth = np.zeros(self.njoints, dtype=np.int32)
        for j in range(self.njoints):
            p = int(self.parents[j])
            depth[j] = 0 if p < 0 else depth[p] + 1
        self._levels = [np.where(depth == d)[0] for d in range(int(depth.max()) + 1)]

    def joint_id(self, joint) -> int:
        if isinstance(joint, (int, np.integer)):
            return int(joint)
        if joint not in self._index:
            raise KeyError(f"unknown joint {joint!r}")
        return self._index[joint]

    def average_body(self) -> np.ndarray:
        """The canonical average body's bone lengths (the conditioning-stats mean)."""
        return self.blen_mean.copy()

    def body_cond(self, bone_lengths) -> np.ndarray:
        """Bone lengths (metres) -> the z-scored descriptor, guarded for non-SEED bodies.

        A handful of the dims are fixed offsets in the retargeted skeleton (jaw, eyes, finger
        ends) that vary by well under a millimetre across the 522-actor training population.
        A real rig differs there by millimetres, which a ~0.05 mm std turns into z ~= -29 and
        collapses the pose — measured 223 cm of effector error with the IK quietly hiding it.
        So: pin the uninformative dims to the mean, clamp the rest to the observed range.
        """
        b = np.asarray(bone_lengths, dtype=np.float32).reshape(-1)
        z = (b - self.blen_mean) / self.blen_std
        z = np.where(self.blen_std < self._deg_std, 0.0, z)
        return np.clip(z, -self._z_max, self._z_max).reshape(1, -1).astype(np.float32)

    def rest_joints(self, bone_lengths=None) -> np.ndarray:
        """(J,3) rest layout for a body, root at the origin."""
        J = self.njoints
        if bone_lengths is None:
            return (self.neutral - self.neutral[self.root_idx]).astype(np.float32)
        b = np.asarray(bone_lengths, dtype=np.float32).reshape(-1)
        if b.size == J - 1:                       # non-root lengths, in joint order
            full = np.zeros(J, dtype=np.float32)
            full[[j for j in range(J)
                  if self.parents[j] >= 0 and self.parents[j] != j]] = b
            b = full
        out = np.zeros((J, 3), dtype=np.float32)
        for j in range(J):                        # parents precede children in joint order
            p = int(self.parents[j])
            if p >= 0 and p != j:
                out[j] = out[p] + self._dirs[j] * b[j]
        return out

    def bone_lengths(self, rest: np.ndarray) -> np.ndarray:
        """(J,3) rest layout -> the (J-1,) descriptor: each non-root joint's distance to
        its parent, in joint order. The inverse of `rest_joints`, for reading a real rig."""
        rest = np.asarray(rest, dtype=np.float32).reshape(self.njoints, 3)
        out = []
        for j in range(self.njoints):
            p = int(self.parents[j])
            if p >= 0 and p != j:
                out.append(float(np.linalg.norm(rest[j] - rest[p])))
        return np.asarray(out, dtype=np.float32)

    def fk(self, local: np.ndarray, root: np.ndarray, rest: np.ndarray):
        """(J,3,3) local rotations + (3,) root + (J,3) rest -> (global rots, joint positions).

        `rest` is recentred on the root here, so callers may pass a raw rest layout.
        """
        J = self.njoints
        rest = np.asarray(rest, dtype=np.float32).reshape(J, 3)
        rest = rest - rest[self.root_idx]
        g = np.empty((J, 3, 3), dtype=np.float32)
        p = np.empty((J, 3), dtype=np.float32)
        r = self._levels[0]
        g[r] = local[r]
        p[r] = np.asarray(root, dtype=np.float32).reshape(3)
        for lv in self._levels[1:]:
            par = self.parents[lv]
            g[lv] = g[par] @ local[lv]
            p[lv] = p[par] + (g[par] @ (rest[lv] - rest[par])[:, :, None])[:, :, 0]
        return g, p

    #: How far a bone may be rolled to get out of the floor. A toe extends about 60 degrees
    #: at the joint that rolls; past that a foot is not rolling, it is folding over backwards.
    #: Unclamped, a deeply buried foot turned 148.8 degrees — anatomically nonsense, and the
    #: reason this is a clamp and not a guarantee.
    MAX_ROLL_DEG = 60.0

    #: How far a limb may be swung about the line through its ends to get a knee or an elbow
    #: off the floor. That rotation is the hip (or the shoulder) turning in place, which has a
    #: range like any other joint.
    MAX_SWING_DEG = 45.0

    def _pitch_axis(self, g, pivot: int, bone):
        """The axis a bone is allowed to turn about: the joint's own side-to-side axis.

        Rolling about a FIXED anatomical axis is what keeps a toe out of trouble. The minimal
        rotation onto a target direction is not that axis: where the bone points straight down
        it has no heading to keep and the rotation becomes arbitrary, and where it must turn
        nearly 180 degrees the axis is arbitrary outright — either way the toe can come out
        sideways. The joint's lateral axis is defined in every pose, so a roll is always a
        pitch: no yaw, no twist about the bone.
        """
        lat = g[pivot] @ np.array([1.0, 0.0, 0.0], np.float32)     # the joint's own x
        b = bone / max(float(np.linalg.norm(bone)), 1e-9)
        lat = lat - float(lat @ b) * b                              # perpendicular to the bone
        if float(np.linalg.norm(lat)) < 1e-4:                       # the bone IS the x axis
            lat = np.cross(np.array([0.0, 1.0, 0.0], np.float32), b)
        if float(np.linalg.norm(lat)) < 1e-4:
            lat = np.cross(np.array([0.0, 0.0, 1.0], np.float32), b)
        return lat / max(float(np.linalg.norm(lat)), 1e-9)

    def roll_onto_floor(self, local: np.ndarray, root: np.ndarray, rest: np.ndarray,
                        pairs, floor_y: float = 0.0, max_deg: float | None = None):
        """Roll a bone up until the joint below it sits ON the floor, not through it.

        The floor is the one thing a pose has to respect that no effector states, and the
        poser gets it wrong in the same place every time: measured over 60 poses, the ball of
        the foot was below the ground in 45 of them, 1.7 cm deep on average and 4.3 cm at
        worst, with the IK's floor term pulling but not guaranteeing.

        Ported from the retargeter (`ardy/mmcp/hik_retarget.py`), where it fixes the same
        thing one joint further down. `pairs` are (pivot, tip): the PIVOT keeps its position
        and only turns, so whatever pinned it is still satisfied. A tip may be a joint index
        or an offset in the pivot's local frame, which is how a rig's own toe tip — a bone
        SOMA-30 does not have — gets flattened onto the floor too.

        The turn is a PITCH about the joint's lateral axis, in the direction that lifts the
        tip, by at most `max_deg`. That is the whole of what a foot is allowed to do here: it
        may not yaw the toe sideways, twist it about its own length, or fold it back over
        itself, and every one of those is reachable by an unconstrained "rotate onto the
        target direction". The correction is zero where the tip reaches the floor and grows
        continuously with depth, so a foot above the ground is not touched at all, planted or
        mid-air; and where the clamp binds, the tip is left as high as the joint can put it.
        """
        limit = np.radians(self.MAX_ROLL_DEG if max_deg is None else max_deg)
        local = np.array(local, dtype=np.float32, copy=True)
        moved = []
        for pivot, tip in pairs:
            g, p = self.fk(local, root, rest)
            tip_pos = (p[int(tip)] if np.ndim(tip) == 0
                       else p[pivot] + g[pivot] @ np.asarray(tip, dtype=np.float32))
            bone = tip_pos - p[pivot]
            length = float(np.linalg.norm(bone))
            if length < 1e-6 or tip_pos[1] >= floor_y:
                continue
            axis = self._pitch_axis(g, pivot, bone)
            # height of the tip as the bone turns about that axis, which is exact because the
            # axis is perpendicular to the bone: y(t) = A cos t + B sin t
            A = float(bone[1])
            B = float(np.cross(axis, bone)[1])
            amp = float(np.hypot(A, B))
            if amp < 1e-9:
                continue                      # turning about this axis cannot change height
            want = floor_y - float(p[pivot][1])
            phase = float(np.arctan2(A, B))
            if abs(want) <= amp:
                base = float(np.arcsin(float(np.clip(want / amp, -1.0, 1.0))))
                cands = [base - phase, float(np.pi) - base - phase]
            else:                             # out of reach: go as high as the bone can get
                cands = [float(np.pi) / 2.0 - phase]
            cands = [float(np.arctan2(np.sin(t), np.cos(t))) for t in cands]
            lifting = [t for t in cands if A * np.cos(t) + B * np.sin(t) > A + 1e-9]
            if not lifting:
                continue
            t = min(lifting, key=abs)
            t = float(np.clip(t, -limit, limit))
            c, sn = float(np.cos(t)), float(np.sin(t))
            k = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]],
                          [-axis[1], axis[0], 0.0]], np.float32)
            R = (np.eye(3, dtype=np.float32) + sn * k + (1.0 - c) * (k @ k)).astype(np.float32)
            # RIGIDLY: only the pivot's own local rotation changes, so the foot and the toe
            # turn together. Rebuilding every local from the globals instead (which is what
            # the retargeter can afford, its tip being a leaf) holds the joints below at their
            # old WORLD angle -- the foot rolls up, the toe stays pointing down, and the toes
            # read as bent into the ground.
            par = int(self.parents[pivot])
            turned = R @ g[pivot]
            local[pivot] = (g[par].T @ turned) if par >= 0 else turned
            moved.append((pivot, tip, float(tip_pos[1])))
        if not moved:
            return local, 0.0
        g2, p2 = self.fk(local, root, rest)

        def _y(pivot, tip):
            return float(p2[int(tip)][1] if np.ndim(tip) == 0
                         else (p2[pivot] + g2[pivot] @ np.asarray(tip, np.float32))[1])

        before = min(y for _pivot, _tip, y in moved)
        after = min(_y(pivot, tip) for pivot, tip, _y0 in moved)
        return local, (after - before)

    #: (ankle, ball) per side, by name
    FOOT_CHAINS = (("LeftFoot", "LeftToeBase"), ("RightFoot", "RightToeBase"))

    #: (chain root, middle, end) — a middle joint can be lifted off the floor by SWINGING the
    #: chain about the axis through its two ends, which moves it and nothing else.
    LIMB_CHAINS = (("LeftLeg", "LeftShin", "LeftFoot"), ("RightLeg", "RightShin", "RightFoot"),
                   ("LeftArm", "LeftForeArm", "LeftHand"),
                   ("RightArm", "RightForeArm", "RightHand"))

    @staticmethod
    def _angle_between(r0, r1) -> float:
        """Degrees between two rotations — how far a joint has been turned in total."""
        c = (float(np.trace(np.asarray(r0).T @ np.asarray(r1))) - 1.0) / 2.0
        return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))

    def swing_above_floor(self, local, root, rest, chains=None, floor_y: float = 0.0,
                          max_deg: float | None = None):
        """Lift a knee or an elbow out of the floor without moving anything that is pinned.

        A mid-chain joint is the one case where the floor can be enforced for free. Its two
        neighbours fix the chain's axis, and the joint itself rides a circle about that axis —
        the pole-vector circle. Rotating the chain about that axis therefore moves the knee and
        leaves the hip and the ankle exactly where the solver put them, so a pinned foot stays
        pinned. The swing taken is the SMALLEST one that reaches the floor; if the whole circle
        is underground, the highest point on it is taken instead and the caller is told what is
        left.

        A soft penalty cannot do this job: measured on a kneeling pose, the knee sat 10.3 cm
        below the floor with the IK's floor term on the feet and 8.3 cm with it on every joint,
        because an effector at weight 1/tol = 200 outvotes a floor at 5. Geometry does not
        negotiate.
        """
        local = np.array(local, dtype=np.float32, copy=True)
        for a_name, m_name, e_name in (chains or self.LIMB_CHAINS):
            if not all(n in self._index for n in (a_name, m_name, e_name)):
                continue
            a, m, e = (self._index[n] for n in (a_name, m_name, e_name))
            g, p = self.fk(local, root, rest)
            if p[m][1] >= floor_y:
                continue
            axis = p[e] - p[a]
            n = float(np.linalg.norm(axis))
            if n < 1e-6:
                continue                       # a folded limb has no axis to swing about
            axis = axis / n
            centre = p[a] + axis * float((p[m] - p[a]) @ axis)
            u = p[m] - centre                  # the radius vector, at angle 0
            v = np.cross(axis, u)              # a quarter turn further round the circle
            want = floor_y - float(centre[1])
            amp = float(np.hypot(u[1], v[1]))
            if amp < 1e-9:
                continue                       # the circle is level: no swing changes height
            # y(t) = u_y cos t + v_y sin t = amp * sin(t + phase); take the nearest solution
            phase = float(np.arctan2(u[1], v[1]))
            ratio = float(np.clip(want / amp, -1.0, 1.0))
            base = float(np.arcsin(ratio))
            options = [base - phase, float(np.pi) - base - phase]
            t = min(options, key=lambda x: abs(np.arctan2(np.sin(x), np.cos(x))))
            t = float(np.arctan2(np.sin(t), np.cos(t)))
            # a limb swinging about its own axis is the hip or the shoulder turning in place,
            # and that has a range: past it the leg is wrung round rather than swung
            limit = np.radians(self.MAX_SWING_DEG if max_deg is None else max_deg)
            t = float(np.clip(t, -limit, limit))
            c, sn = float(np.cos(t)), float(np.sin(t))
            k = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]],
                          [-axis[1], axis[0], 0.0]], np.float32)
            R = (np.eye(3, dtype=np.float32) + sn * k + (1.0 - c) * (k @ k)).astype(np.float32)
            par = int(self.parents[a])
            turned = R @ g[a]
            local[a] = (g[par].T @ turned) if par >= 0 else turned
        return local

    def enforce_floor(self, local, root, rest, *, floor_y: float = 0.0, toe_tips=None,
                      passes: int = 3, exempt=()):
        """Put every joint on or above the floor, geometrically. A hard constraint.

        The IK's floor term is a penalty and loses: an effector at weight 1/tol = 200 outvotes
        a floor at 5, so a kneeling pose left a knee 10.3 cm underground with the term on the
        feet and 8.3 with it on every joint. This does not negotiate — but it also does not
        need to fight, because every violation has a move that fixes it for free:

        * a MID-CHAIN joint (a knee, an elbow) rides the pole-vector circle about the axis
          through its neighbours, so swinging the chain lifts it and moves neither end;
        * anything else is lifted by ROLLING its parent bone about the parent's position, which
          is the retargeter's toe roll: the parent does not move, so whatever pinned it holds.

        Joints are taken parents-first, so a bone is only ever rolled about a pivot that is
        already out of the floor, and the whole thing is repeated a few times because rolling a
        bone swings its subtree and can put a sibling under. What cannot be fixed is reported
        rather than forced: a joint whose parent is the ROOT and whose root is itself
        underground has no pivot, and `exempt` lets a caller protect joints an animator
        deliberately placed below the ground.

        Returns (local, worst_remaining_y).
        """
        local = np.array(local, dtype=np.float32, copy=True)
        # the pose as it arrived: every clamp below is measured against THIS, so a joint
        # cannot be turned 60 degrees on one sweep and 60 more on the next. Measured on the
        # LOCAL rotations, which are the joint's own bend — a global carries whatever the
        # joints above it did, and spending a knee's budget on a hip's correction is wrong.
        start_local = np.array(local, dtype=np.float32, copy=True)
        middles = {}
        for a, m, e in self.LIMB_CHAINS:
            if all(n in self._index for n in (a, m, e)):
                middles[self._index[m]] = (a, m, e)
        order = sorted(range(self.njoints), key=lambda j: self._depth(j))
        exempt = {self.joint_id(j) for j in exempt}
        for _ in range(max(1, passes)):
            _, p = self.fk(local, root, rest)
            if float(p[:, 1].min()) >= floor_y - 1e-6:
                break
            for j in order:
                if j in exempt or int(self.parents[j]) < 0:
                    continue
                if float(p[j][1]) >= floor_y:
                    continue
                if j in middles:
                    a = self._index[middles[j][0]]
                    budget = self.MAX_SWING_DEG - self._angle_between(start_local[a], local[a])
                    if budget <= 1e-3:
                        continue
                    local = self.swing_above_floor(local, root, rest, [middles[j]], floor_y,
                                                   max_deg=budget)
                else:
                    par = int(self.parents[j])
                    budget = self.MAX_ROLL_DEG - self._angle_between(start_local[par],
                                                                     local[par])
                    if budget <= 1e-3:
                        continue
                    local, _ = self.roll_onto_floor(
                        local, root, rest, [(par, j)], floor_y, max_deg=budget)
                # only after a correction — turning a bone swings its subtree, so the heights
                # below it are stale, but the heights of the joints ABOVE are not. Re-running
                # FK for every joint instead (which is what this did first) costs 30 forward
                # passes per sweep on a pose that usually needs one or two corrections, and
                # took a solve from 7 ms to 21 — outside the frame budget the rig lives in.
                _, p = self.fk(local, root, rest)
        # the rig's own toe tips, which are not joints of this skeleton
        if toe_tips:
            local, _ = self.roll_onto_floor(
                local, root, rest,
                [(self.joint_id(b), v) for b, v in toe_tips.items()], floor_y)
        _, p = self.fk(local, root, rest)
        return local, float(p[:, 1].min())

    def _depth(self, j: int) -> int:
        d, k = 0, int(self.parents[j])
        while k >= 0:
            d, k = d + 1, int(self.parents[k])
        return d

    def floor_joint_set(self, which="feet"):
        """Which joints the IK's floor term should hold above the ground.

        `"feet"` is the checkpoint's own set — the ankles and the balls — and is what the model
        was trained alongside. `"body"` is every joint, which is the honest reading of "the
        floor is solid": a knee, an elbow or a hand has no more business underground than a
        toe. It is not free — the solver's linear system grows by the number of rows added —
        so it is a choice rather than the default, and a control still outranks it: an effector
        pinned below the floor is obeyed, because the animator asked for it.

        Anything else is taken as an explicit list of joint names or indices.
        """
        if which in (None, False, "off", "none"):
            return np.zeros(0, dtype=np.int64)
        if which in (True, "feet"):
            return np.asarray(self.foot_joints, dtype=np.int64)
        if which == "body":
            return np.arange(self.njoints, dtype=np.int64)
        return np.asarray([self.joint_id(j) for j in which], dtype=np.int64)

    def foot_roll_pairs(self, toe_tips=None):
        """What to roll onto the floor, in order: the ball first, then the toe.

        SOMA-30 stops at the BALL, so on its own this skeleton can only put the ball down —
        and a foot whose ball is on the ground while its toes still point into it is exactly
        what that looks like on a character: bent toes, not flat ones. A rig knows better,
        because it has the bone: pass `toe_tips` as {ball joint: offset to the tip}, measured
        on the rig's REST pose in this frame, and the ball is rolled as well until the tip
        lies on the floor. That second roll is the retargeter's original toe roll.
        """
        out = []
        for ankle, ball in self.FOOT_CHAINS:
            if ankle in self._index and ball in self._index:
                out.append((self._index[ankle], self._index[ball]))
        for ball, offset in (toe_tips or {}).items():
            j = self.joint_id(ball)
            v = np.asarray(offset, dtype=np.float32).reshape(3)
            if float(np.linalg.norm(v)) > 1e-4:
                out.append((j, v))
        return out

    def global_to_local(self, g: np.ndarray) -> np.ndarray:
        """`G_j = G_parent @ L_j` -> `L_j = G_parentᵀ @ G_j` (rotations: inverse = transpose)."""
        g = np.asarray(g, dtype=np.float32).reshape(self.njoints, 3, 3)
        out = np.empty_like(g)
        for j in range(self.njoints):
            p = int(self.parents[j])
            out[j] = g[j] if p < 0 else g[p].T @ g[j]
        return out


def rot_from_to(a, b):
    """(3,) -> (3,3): the minimal rotation taking `a` onto `b`. Both are normalised here."""
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    a = a / max(float(np.linalg.norm(a)), 1e-9)
    b = b / max(float(np.linalg.norm(b)), 1e-9)
    v = np.cross(a, b)
    c = float(a @ b)
    s2 = float(v @ v)
    if s2 > 1e-12:
        vx = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]], np.float32)
        return (np.eye(3, dtype=np.float32) + vx + vx @ vx * ((1.0 - c) / max(s2, 1e-12)))
    if c > 0:
        return np.eye(3, dtype=np.float32)
    axis = np.cross(a, np.array([1.0, 0.0, 0.0], np.float32))       # 180 degrees
    if float(axis @ axis) < 1e-12:
        axis = np.cross(a, np.array([0.0, 1.0, 0.0], np.float32))
    axis = axis / max(float(np.linalg.norm(axis)), 1e-9)
    vx = np.array([[0.0, -axis[2], axis[1]], [axis[2], 0.0, -axis[0]],
                   [-axis[1], axis[0], 0.0]], np.float32)
    return (np.eye(3, dtype=np.float32) + 2.0 * vx @ vx).astype(np.float32)
