# SPDX-License-Identifier: GPL-3.0-or-later
"""An Autoposer edit carried through time.

A drag on a handle -- on the live character or on a wormhole slice -- is
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
LIVE_MAX = 24


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
    by its two parents, the elbow or knee bending the way it bends, the end
    keeping its orientation. ``bases`` is changed in place."""
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


def bases_from_action(arm, action, frame, names, fallback) -> dict:
    """The bases ``names`` have at ``frame`` as the action plays them --
    read off the curves, no frame stepped. A channel with no curve keeps
    ``fallback``'s value."""
    from . import constraints_ui
    curves = {}
    if action is not None:
        for fc in constraints_ui.iter_action_fcurves(action):
            curves[(fc.data_path, fc.array_index)] = fc
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
        self.after = self.before
        self.base = self.before        # what the edit is measured from (see set_base)
        self.delta = {}
        self.dragged = dragged if dragged in ENDS_OF(arm) else ""
        self.held = [b for b in held if b in ENDS_OF(arm) and b != self.dragged]
        self._orig = {}
        self._dragged_move = Vector()

    # -- the edit --------------------------------------------------------

    def set_base(self, base) -> None:
        """The solve of the pose with nothing moved. The poser never gives a
        pose back exactly (a few degrees here and there): measured from
        ``before``, that difference jumped in on the first move. Measured from
        this, the edit is only what the drag changed."""
        self.base = {n: base[n].copy() for n in self.names}

    def set_after(self, after) -> None:
        """The pose the solve gave at f0: each bone's change from the base."""
        self.after = {n: after[n].copy() for n in self.names}
        self.delta = {}
        for n in self.edited:
            l0, q0, _s0 = _split(self.base[n])
            l1, q1, _s1 = _split(self.after[n])
            dq = q1 @ q0.inverted()
            if dq.w < 0.0:
                dq.negate()
            dl = l1 - l0
            if dl.length > 1e-7 or dq.angle > 1e-6:
                self.delta[n] = (dl, dq)
        if self.dragged:
            m0 = self.rig.fk(self.base)[self.dragged].translation
            m1 = self.rig.fk(self.after)[self.dragged].translation
            self._dragged_move = m1 - m0
        else:
            self._dragged_move = Vector()

    def changed(self) -> bool:
        return bool(self.delta)

    def weight(self, g) -> float:
        from .curve_edit import falloff
        if g == self.f0:
            return 1.0
        return falloff(g - self.f0, self.radius) if self.radius > 0 else 0.0

    def frames(self) -> list:
        """The frames the edit reaches, f0 left out."""
        return [g for g in range(self.f0 - self.radius, self.f0 + self.radius + 1)
                if g != self.f0 and self.weight(g) > 1e-3]

    def original(self, g) -> dict:
        b = self._orig.get(g)
        if b is None:
            b = self._orig[g] = bases_from_action(self.arm, self.action, g, self.names, self.before)
        return b

    def pose_at(self, g) -> dict:
        """Frame ``g``'s bases with its share of the edit (all of it at f0)."""
        w = self.weight(g)
        orig = self.before if g == self.f0 else self.original(g)
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

    # -- keying ------------------------------------------------------------

    def write(self) -> int:
        """Key f0 (into the take's motion where there is one, else as a key
        pose) and the frames in reach as motion; the two
        frames past the reach are fenced as they were, so the edit stays
        inside it. Nothing is stepped. The frames keyed, f0 left out."""
        from . import pose_edit
        arm = self.arm
        if arm.animation_data is None:
            arm.animation_data_create()
        if self.action is None:
            self.action = bpy.data.actions.new(f"{arm.name}Action")
            arm.animation_data.action = self.action
        # capture everything first: a key written changes what the curves give
        reach_frames = self.frames()
        lo, hi = self.f0 - self.radius, self.f0 + self.radius
        fence = [g for g in (lo - 2, lo - 1, hi + 1, hi + 2)] if self.radius > 0 else []
        fence += [g for g in range(lo, hi + 1) if g != self.f0 and g not in reach_frames]
        poses = {g: self.pose_at(g) for g in reach_frames}
        poses.update({g: self.original(g) for g in fence})
        self.key_type = pose_edit.edit_key_type(arm, self.f0)    # before anything is written
        pose_edit.write_channels(self.action, self.f0, self.channels(self.pose_at(self.f0)), self.key_type,
                                 finish=False)
        for g, bases in sorted(poses.items()):
            pose_edit.write_channels(self.action, g, self.channels(bases), 'GENERATED', finish=False)
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
