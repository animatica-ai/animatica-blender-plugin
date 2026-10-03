# SPDX-License-Identifier: GPL-3.0-or-later
"""An Autoposer edit carried through time.

A drag on a handle -- on the live character or on a zoetrope slice -- is
solved at its frame by the Autoposer, the whole body following, exactly as a
single-frame edit is. What the solve changed, bone by bone (a turn and a
move in each bone's own parent space), is then laid over the frames around
it with the falloff: an animation layer, its weight fading with the distance
in time. The frames keep their own motion and gain a share of the edit, so
the take bends smoothly instead of each frame being solved anew (that found
a slightly different body every frame, and the take shook).

Two things are then put exactly where they belong on each frame: the joint
being dragged (a hand or foot follows the falloff's share of the move, the
path bent like a curve) and the hands and feet the artist holds (they stay
where they were on that frame: a planted foot does not slide).

Everything here is computed from the action and the bones' rest matrices --
no frame is stepped, nothing is evaluated -- so it runs on every mouse move,
and the ghosts on the frames within reach follow the drag as it happens.
"""

from __future__ import annotations

import math

import bpy
from mathutils import Matrix, Quaternion, Vector

#: the joints a hold or a drag puts exactly in place on each frame
ENDS = ("LeftHand", "RightHand", "LeftFoot", "RightFoot")
#: live ghosts: at most this many frames re-captured per drag update
LIVE_MAX = 16
#: seconds between re-reading the curves for the live preview of a sparse action
PROBE_EVERY = 0.08
#: how far (metres) a drag moves the body before the frame it edits is the
#: Autoposer's own pose; up to there it eases out of the pose as it stood
SETTLE = 0.15


def _split(M):
    loc, q, s = M.decompose()
    return loc, q, s


def _compose(loc, q, s):
    return Matrix.LocRotScale(loc, q, s)


def _order(arm) -> list:
    """Pose bones parents first."""
    depth = {}

    def d(b):
        if b.name not in depth:
            depth[b.name] = 0 if b.parent is None else d(b.parent) + 1
        return depth[b.name]
    return sorted(arm.pose.bones, key=lambda pb: d(pb.bone))


class Rig:
    """The armature's hierarchy, for forward kinematics in plain math."""

    def __init__(self, arm):
        self.arm = arm
        self.order = [pb.name for pb in _order(arm)]
        self.parent = {}
        self.rel = {}
        for pb in arm.pose.bones:
            b = pb.bone
            self.parent[pb.name] = b.parent.name if b.parent else None
            self.rel[pb.name] = (b.parent.matrix_local.inverted() @ b.matrix_local) if b.parent \
                else b.matrix_local.copy()

    def fk(self, bases) -> dict:
        """Pose-space matrix of every bone for ``bases`` (name -> basis)."""
        out = {}
        for n in self.order:
            p = self.parent[n]
            M = self.rel[n] @ bases[n]
            out[n] = (out[p] @ M) if p else M
        return out

    def basis_for(self, mats, name, M) -> Matrix:
        """The basis that puts ``name`` at pose-space ``M``, its parent as in ``mats``."""
        p = self.parent[name]
        parent = (mats[p] @ self.rel[name]) if p else self.rel[name]
        return parent.inverted() @ M


def _rotate_about(rig, bases, mats, name, pivot, q):
    M = Matrix.Translation(pivot) @ q.to_matrix().to_4x4() @ Matrix.Translation(-pivot) @ mats[name]
    bases[name] = rig.basis_for(mats, name, M)


def reach(rig, bases, end, target) -> bool:
    """Two-bone IK in plain math: ``end``'s head (pose space) onto ``target``
    by its two parents, the end keeping its orientation. ``bases`` is changed
    in place.

    The elbow or knee stays a hinge: the lower bone only opens or closes about
    the axis it already bends on, then the limb swings whole from the shoulder
    or hip. Turning each bone the shortest way onto its new spot instead bent
    the elbow sideways -- by up to 70 degrees on a hand carried through time."""
    b2 = rig.parent.get(end)
    b1 = rig.parent.get(b2) if b2 else None
    if b1 is None:
        return False
    mats = rig.fk(bases)
    A, B, C = mats[b1].translation, mats[b2].translation, mats[end].translation
    keep = mats[end].to_3x3().normalized()
    l1, l2 = (B - A).length, (C - B).length
    to = target - A
    if l1 < 1e-6 or l2 < 1e-6 or to.length < 1e-6:
        return False
    d = max(abs(l1 - l2) + 1e-4, min(to.length, l1 + l2 - 1e-4))
    hinge = (B - A).cross(C - B)
    if hinge.length < math.sin(math.radians(0.5)) * l1 * l2:
        return _reach_straight(rig, bases, mats, b1, b2, end, target, keep)
    hinge.normalize()
    bend_now = (B - A).angle(C - B)
    bend = math.pi - math.acos(max(-1.0, min(1.0, (l1 * l1 + l2 * l2 - d * d) / (2 * l1 * l2))))
    _rotate_about(rig, bases, mats, b2, B, Quaternion(hinge, bend - bend_now))
    mats = rig.fk(bases)
    _rotate_about(rig, bases, mats, b1, A, (mats[end].translation - A).rotation_difference(to))
    mats = rig.fk(bases)
    M = Matrix.Translation(mats[end].translation) @ keep.to_4x4()
    bases[end] = rig.basis_for(mats, end, M)
    return True


def _reach_straight(rig, bases, mats, b1, b2, end, target, keep) -> bool:
    """`reach` for a limb with no bend to keep: it bends in the plane of where
    it pointed and where it goes."""
    A, B, C = mats[b1].translation, mats[b2].translation, mats[end].translation
    l1, l2 = (B - A).length, (C - B).length
    to = target - A
    d = max(1e-4, min(to.length, l1 + l2 - 1e-4))
    u = to.normalized()
    bend = (B - A) - u * (B - A).dot(u)
    if bend.length < 1e-6:
        bend = u.orthogonal()
    bend.normalize()
    a = (l1 * l1 - l2 * l2 + d * d) / (2 * d)
    h = math.sqrt(max(l1 * l1 - a * a, 0.0))
    B_new = A + u * a + bend * h
    _rotate_about(rig, bases, mats, b1, A, (B - A).rotation_difference(B_new - A))
    mats = rig.fk(bases)
    Bn, Cn = mats[b2].translation, mats[end].translation
    _rotate_about(rig, bases, mats, b2, Bn, (Cn - Bn).rotation_difference(A + u * d - Bn))
    mats = rig.fk(bases)
    M = Matrix.Translation(mats[end].translation) @ keep.to_4x4()
    bases[end] = rig.basis_for(mats, end, M)
    return True


def action_curves(action) -> dict:
    from . import constraints_ui
    return {(fc.data_path, fc.array_index): fc
            for fc in constraints_ui.iter_action_fcurves(action)} if action is not None else {}


def bases_from_action(arm, action, frame, names, fallback, curves=None) -> dict:
    """The bases ``names`` have at ``frame`` as the action plays them --
    read off the curves, no frame stepped. A channel with no curve keeps
    ``fallback``'s value. ``curves``: the action's, when already gathered."""
    if curves is None:
        curves = action_curves(action)
    out = {}
    for n in names:
        pb = arm.pose.bones[n]
        loc, q, s = _split(fallback[n])
        base = f'pose.bones["{n}"].'

        def val(path, i, default):
            fc = curves.get((base + path, i))
            return fc.evaluate(frame) if fc is not None else default
        loc = Vector([val("location", i, loc[i]) for i in range(3)])
        mode = pb.rotation_mode
        if mode == 'QUATERNION':
            q = Quaternion([val("rotation_quaternion", i, q[i]) for i in range(4)])
            if q.magnitude > 1e-8:
                q.normalize()
            else:
                q = Quaternion()
        elif mode == 'AXIS_ANGLE':
            aa0 = [q.angle, *q.axis] if q.angle > 1e-9 else [0.0, 0.0, 1.0, 0.0]
            aa = [val("rotation_axis_angle", i, aa0[i]) for i in range(4)]
            q = Quaternion(Vector(aa[1:4]), aa[0]) if Vector(aa[1:4]).length > 1e-9 else Quaternion()
        else:
            e0 = q.to_euler(mode)
            from mathutils import Euler
            q = Euler([val("rotation_euler", i, e0[i]) for i in range(3)], mode).to_quaternion()
        s = Vector([val("scale", i, s[i]) for i in range(3)])
        out[n] = _compose(loc, q, s)
    return out


class Carry:
    """One drag's edit through time.

    ``before`` is the pose the drag started from at ``f0``; ``set_after``
    takes the pose the solve gave there. ``pose_at(g)`` is then frame ``g``
    as the edit leaves it, and ``write`` keys it all."""

    def __init__(self, context, arm, f0, radius, before, *, dragged="", held=()):
        from . import pose_edit
        self.arm, self.f0, self.radius = arm, int(f0), int(radius)
        self.rig = Rig(arm)
        self.action = pose_edit._editing_action(arm)
        self.names = [pb.name for pb in arm.pose.bones]
        self.edited = {pb.name for pb in pose_edit._edited_bones(arm)}
        if before is None:      # the frame as the action plays it (not the frame on show)
            now = {pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones}
            before = bases_from_action(arm, self.action, self.f0, self.names, now)
        self.before = {n: before[n].copy() for n in self.names}
        # a loop: the edit is made on the cycle's own frames, round its seam,
        # and its last frame kept its first again -- so it stays a loop
        from . import key_poses
        settings = getattr(context.scene, "animatica", None)
        self.loop = key_poses.loop_span(settings) if settings is not None else None
        self._wrapped = False
        if self.loop is not None:
            w = key_poses.loop_wrap(self.f0, self.loop)
            self._wrapped = w != self.f0      # the playhead on a repeat of the cycle
            self.f0 = w
        self.after = self.before
        self.base = self.before        # what the edit is measured from (see set_base)
        self.delta = {}
        self.dragged = dragged if dragged in ENDS_OF(arm) else ""
        self.held = [b for b in held if b in ENDS_OF(arm) and b != self.dragged]
        # a joint locked on this frame is held too, or the drag moved it off its spot
        from . import joint_lock
        self._locked = [b for b in joint_lock.held_at(arm, self.f0) if b != self.dragged and b not in self.held]
        self.held += self._locked
        self._held = list(self.held)
        self._orig = {}
        self._dragged_move = Vector()

    # -- the edit --------------------------------------------------------

    def set_base(self, base) -> None:
        """The solve of the pose with nothing moved. The poser never gives a
        pose back exactly: measured from ``before`` from the first move on,
        that difference popped in. The first stretch of a drag eases out of it
        (see set_after)."""
        self.base = {n: base[n].copy() for n in self.names}

    def set_after(self, after) -> None:
        """The pose the solve gave at f0, and the edit: each bone's change from
        the pose the drag started from.

        The frame being edited shows the Autoposer's pose itself. Laying only
        what the drag changed (``after`` against ``base``) over the pose as it
        stood turned each bone by a change solved for another pose: on a pose
        the poser does not give back closely -- a generated crouch it re-solved
        with the head 27 cm away -- that made bodies no solve had made, the
        torso twisted, a wrist bent back, an elbow the wrong way, and the
        dragged hand 10 cm off the cursor. So the edit hands over from that to
        the solve within the first SETTLE of what the drag moves: no pop on the
        first move, the poser's own pose once the drag is under way."""
        self.after = {n: after[n].copy() for n in self.names}
        fb, fa = self.rig.fk(self.base), self.rig.fk(self.after)
        moved = max(((fa[n].translation - fb[n].translation).length for n in self.edited), default=0.0)
        k = min(1.0, moved / SETTLE)
        k = k * k * (3.0 - 2.0 * k)
        shown = dict(self.before)
        self.delta = {}
        for n in self.edited:
            lb, qb, sb = _split(self.before[n])
            l0, q0, _s0 = _split(self.base[n])
            l1, q1, _s1 = _split(self.after[n])
            dq = q1 @ q0.inverted()
            if dq.w < 0.0:
                dq.negate()
            # what the drag changed, on the pose as it stood, handing over to the solve
            lc, qc = lb + (l1 - l0), dq @ qb
            if qc.dot(q1) < 0.0:
                qc.negate()
            loc, q = lc.lerp(l1, k), qc.slerp(q1, k)
            shown[n] = _compose(loc, q, sb)
            dq = q @ qb.inverted()
            if dq.w < 0.0:
                dq.negate()
            dl = loc - lb
            if dl.length > 1e-7 or dq.angle > 1e-6:
                self.delta[n] = (dl, dq)
        if self.dragged:
            m0 = self.rig.fk(self.before)[self.dragged].translation
            m1 = self.rig.fk(shown)[self.dragged].translation
            self._dragged_move = m1 - m0
        else:
            self._dragged_move = Vector()

    def move_whole(self, on) -> None:
        """Shift: the whole body moves, so the hands and feet the handles hold
        go with it -- held back where they were, they were left 30-40 cm
        behind. A joint locked in place stays on its spot."""
        self.held = list(self._locked) if on else list(self._held)

    def changed(self) -> bool:
        return bool(self.delta)

    def _key_weight(self, g) -> float:
        """The share of the edit a frame that holds a key (or f0) is given."""
        from . import key_poses
        from .curve_edit import reach_weight
        if g == self.f0:
            return 1.0
        d = key_poses.frames_from(g, self.f0, self.loop)
        if d == 0:
            return 1.0                       # the cycle's last frame is its first
        return reach_weight(d, self.radius) if self.radius > 0 else 0.0

    def weight(self, g) -> float:
        """The share of the edit frame ``g`` ends up with. A frame with a key
        gets the falloff's share (its key is updated); a frame without one is
        given no key, so it follows the curve between the keys either side --
        f0's new one among them -- and that is what it shows here."""
        if self.loop is not None:
            return self._key_weight(g)       # a loop's frames are all keyed
        keyed = self.keyed()
        if g == self.f0 or g in keyed:
            return self._key_weight(g)
        prev = max((k for k in keyed | {self.f0} if k < g), default=None)
        nxt = min((k for k in keyed | {self.f0} if k > g), default=None)
        if prev is None and nxt is None:
            return 0.0
        if prev is None:                 # before the first key: it holds that key
            return self._key_weight(nxt)
        if nxt is None:
            return self._key_weight(prev)
        t = (g - prev) / float(nxt - prev)
        t = t * t * (3.0 - 2.0 * t)      # an ease, as the curve's handles give
        return self._key_weight(prev) * (1.0 - t) + self._key_weight(nxt) * t

    def keyed(self) -> set:
        """The frames that hold a key on the body's curves (the action's own)."""
        k = getattr(self, "_keyed", None)
        if k is None:
            from . import constraints_ui
            k = set()
            if self.action is not None:
                for fc in constraints_ui.iter_action_fcurves(self.action):
                    bone = fc.data_path.split('"')[1] if fc.data_path.startswith('pose.bones["') else None
                    if bone in self.edited:
                        k.update(int(round(p.co.x)) for p in fc.keyframe_points)
            self._keyed = k
        return k

    def span(self) -> tuple:
        """``(lo, hi)``: the frames the edit can change, f0's reach and, on a
        sparse take, out to the keys either side of it."""
        if self.loop is not None:
            return self.loop
        keyed = self.keyed()
        lo, hi = self.f0 - self.radius - 1, self.f0 + self.radius + 1
        before = [k for k in keyed if k <= lo]
        after = [k for k in keyed if k >= hi]
        lo = max(before) if before else lo - 120
        hi = min(after) if after else hi + 120
        return lo, hi

    def frames(self) -> list:
        """The frames whose keys the edit updates, f0 left out: only those
        that hold a key already -- an edit never adds one."""
        keyed = self.keyed()
        if self.loop is not None:
            from . import key_poses
            lo, hi = self.loop
            near = {key_poses.loop_wrap(g, self.loop) for g in range(self.f0 - self.radius, self.f0 + self.radius + 1)}
            if lo in near or self.f0 == lo:
                near.add(hi)                 # the last frame is the first again: kept so
            return sorted(g for g in near if g != self.f0 and g in keyed and self._key_weight(g) > 1e-3)
        return [g for g in range(self.f0 - self.radius, self.f0 + self.radius + 1)
                if g != self.f0 and g in keyed and self._key_weight(g) > 1e-3]

    def original(self, g) -> dict:
        b = self._orig.get(g)
        if b is None:
            b = self._orig[g] = bases_from_action(self.arm, self.action, g, self.names, self.before)
        return b

    def pose_at(self, g) -> dict:
        """Frame ``g``'s bases with its share of the edit (all of it at f0).
        A frame with no key gets none, and follows the curves through the new
        keys: that is read off the curves themselves (see _probed)."""
        if g != self.f0 and g not in self.keyed():
            return self._probed(g)
        w = self.weight(g)
        # on a repeat of a loop the pose on show carries the cycles' travel:
        # the loop's own frame is what is keyed
        orig = self.before if (g == self.f0 and not self._wrapped) else self.original(g)
        if w <= 1e-3 or not self.delta:
            return orig
        out = dict(orig)
        ident = Quaternion()
        for n, (dl, dq) in self.delta.items():
            loc, q, s = _split(orig[n])
            out[n] = _compose(loc + dl * w, ident.slerp(dq, w) @ q, s)
        # the ends that must be exactly somewhere: held ones where they were
        # on this frame, the dragged one along its bent path
        if self.held or self.dragged:
            mats0 = self.rig.fk(orig)
            for b in self.held:
                reach(self.rig, out, b, mats0[b].translation)
            if self.dragged:
                reach(self.rig, out, self.dragged, mats0[self.dragged].translation + self._dragged_move * w)
        return out

    def _probed(self, g) -> dict:
        """Frame ``g`` (no key there) as the curves will give it once the edit
        is keyed. Blender's own interpolation decides that -- its handles bend
        with the new keys -- so it is asked: the keys are written, the frames
        read, and every curve put back exactly as it was, in one go."""
        import time
        # the same solve and reach as last time: the same answer. A new solve is
        # read again at most every PROBE_EVERY -- it writes and reads every
        # curve, and on a wide reach that lagged the drag; refresh() before the
        # final ghosts. Held by reference: an id() can come back for a new dict.
        now = time.monotonic()
        changed = getattr(self, "_probe_after", None) is not self.after or getattr(self, "_probe_r", None) != self.radius
        if changed and (getattr(self, "_probe", None) is None or now - getattr(self, "_probe_at", 0.0) >= PROBE_EVERY):
            self._probe_after, self._probe_r, self._probe, self._probe_at = self.after, self.radius, None, now
        if self._probe is None:
            lo, hi = self.span()
            keyed = self.keyed()
            want = [f for f in range(lo, hi + 1) if f != self.f0 and f not in keyed]
            self._probe = self._read_through_keys(want) if self.delta else {}
        return self._probe.get(g) or self.original(g)

    def _read_through_keys(self, frames) -> dict:
        from . import constraints_ui, pose_edit
        if self.action is None or not frames:
            return {}
        curves = action_curves(self.action)
        writes = [(self.f0, self.channels(self.pose_at(self.f0)))]
        writes += [(k, self.channels(self.pose_at(k))) for k in self.frames()]
        undo = []          # (fc, keyframe, None) for an added key; (fc, keyframe, (y, hl, hr)) for a changed one
        touched = set()
        try:
            for frame, channels in writes:
                for path, index, value in pose_edit._continuous_quaternions(self.action, frame, channels):
                    fc = curves.get((path, index))
                    if fc is None:
                        continue
                    kp = next((k for k in fc.keyframe_points if int(round(k.co.x)) == frame), None)
                    if kp is None:
                        if frame != self.f0:
                            continue                 # an edit adds a key on its own frame only
                        kp = fc.keyframe_points.insert(frame, value)
                        undo.append((fc, frame, None))
                    else:
                        undo.append((fc, frame, (kp.co.y, kp.handle_left.y, kp.handle_right.y)))
                        kp.co.y = value
                        kp.handle_left.y = value
                        kp.handle_right.y = value
                    touched.add(fc)
            for fc in touched:
                fc.update()
            return {f: bases_from_action(self.arm, self.action, f, self.names, self.before, curves) for f in frames}
        finally:
            for fc, frame, was in reversed(undo):
                kp = next((k for k in fc.keyframe_points if int(round(k.co.x)) == frame), None)
                if kp is None:
                    continue
                if was is None:
                    fc.keyframe_points.remove(kp)
                else:
                    kp.co.y, kp.handle_left.y, kp.handle_right.y = was
            for fc in touched:
                fc.update()

    def refresh(self) -> None:
        """Read the curves again on the next look (before the final ghosts)."""
        self._probe = None
        self._probe_after = None

    def shown_at_f0(self) -> dict:
        """f0 as the rig shows it. On a repeat of a travelling loop the rig
        stands strides on from the cycle's own frame: the edit is keyed there
        but shown here (it jumped back by the strides while dragging)."""
        bases = self.pose_at(self.f0)
        if not self._wrapped:
            return bases
        cycle = self.original(self.f0)
        out = {}
        for n, M in bases.items():
            loc, q, sc = _split(M)
            on = _split(self.before[n])[0] - _split(cycle[n])[0]
            out[n] = _compose(loc + on, q, sc)
        return out

    # -- keying ------------------------------------------------------------

    def write(self) -> int:
        """Key f0 (into the take's motion where there is one, else as a key
        pose) and update the keys already in reach with their share. No new
        key is added anywhere else: a frame without one follows the curve.
        Nothing is stepped. The frames updated, f0 left out."""
        from . import pose_edit
        arm = self.arm
        if arm.animation_data is None:
            arm.animation_data_create()
        if self.action is None:
            self.action = bpy.data.actions.new(f"{arm.name}Action")
            arm.animation_data.action = self.action
        # capture everything first: a key written changes what the curves give
        reach_frames = self.frames()
        poses = {g: self.pose_at(g) for g in reach_frames}
        self.key_type = pose_edit.edit_key_type(arm, self.f0)    # before anything is written
        pose_edit.write_channels(self.action, self.f0, self.channels(self.pose_at(self.f0)), self.key_type,
                                 finish=False)
        for g, bases in sorted(poses.items()):
            pose_edit.write_channels(self.action, g, self.channels(bases), 'GENERATED', finish=False,
                                     existing_only=True)
        pose_edit.finish_channels(self.action)
        return len(reach_frames)

    def channels(self, bases) -> list:
        """``(data_path, index, value)`` for the edited bones in ``bases``, as
        pose_edit.pose_channels gives the rig's own pose."""
        from . import pose_edit
        arm = self.arm
        root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
        hips = pose_edit.hips_bone(arm)
        out = []
        for pb in pose_edit._edited_bones(arm):
            loc, q, _s = _split(bases[pb.name])
            path = pose_edit._rotation_path(pb)
            if path == "rotation_quaternion":
                values = [q.w, q.x, q.y, q.z]
            elif path == "rotation_euler":
                values = list(q.to_euler(pb.rotation_mode, pb.rotation_euler))
            else:
                axis, angle = q.to_axis_angle()
                values = [angle, axis.x, axis.y, axis.z]
            out += [(f'pose.bones["{pb.name}"].{path}', i, v) for i, v in enumerate(values)]
            if pb is root or pb.parent is None or pb.name == hips:
                out += [(f'pose.bones["{pb.name}"].location', i, v) for i, v in enumerate(loc)]
        return out


def ENDS_OF(arm) -> dict:
    """``{bone name: canonical}`` of the rig's hands and feet."""
    from .autoposer import poser
    out = {}
    for j in ENDS:
        b = poser.joint_bone(arm, j)
        if b is not None:
            out[b.name] = j
    return out


def held_ends(arm, hs, dragged_item=None) -> list:
    """The hands and feet the handles hold (switched on, not the one dragged),
    by bone name."""
    from .autoposer import poser
    out = []
    for h in hs:
        if not h.ap_enabled or h.ap_ety == 2 or (dragged_item is not None and h.name == dragged_item.name):
            continue
        if h.ap_joint in ENDS:
            b = poser.joint_bone(arm, h.ap_joint)
            if b is not None:
                out.append(b.name)
    return out


# ---------------------------------------------------------------------------
# Live ghosts: the frames on show within reach, re-captured as the edit moves
# ---------------------------------------------------------------------------

def show_live(context, carry, frames) -> int:
    """Pose the rig as each of ``frames`` will be, capture its ghost into the
    onion skin's live layer, and put the rig back as it was. The frames
    captured."""
    from . import key_poses
    arm = carry.arm
    keep = {pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones}
    live = {}
    n = 0
    try:
        for g in frames[:LIVE_MAX]:
            bases = carry.pose_at(g)
            for name in carry.edited:      # the body; a control rig's bones follow by themselves
                arm.pose.bones[name].matrix_basis = bases[name]
            entry = key_poses.capture_onion_entry(context, arm)
            if entry is not None:
                live[g] = entry
                n += 1
    finally:
        for name, M in keep.items():
            arm.pose.bones[name].matrix_basis = M
        context.view_layer.update()
    key_poses.set_onion_live(live)
    return n


def clear_live() -> None:
    from . import key_poses
    key_poses.set_onion_live({})
