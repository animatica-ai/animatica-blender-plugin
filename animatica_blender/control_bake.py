# SPDX-License-Identifier: GPL-3.0-or-later
"""A take on a control rig: the motion put on the rig's own controls.

The server answers on the skeleton the request sent: a control rig's deform bones, joined into
one tree (`request_builder.request_tree`). Keys on those bones do nothing on a control rig, whose
constraints drive them from its controls. So the answer, baked onto the deform bones as usual, is
read back off its keys as one pose per frame -- each joint chained down the tree sent, as the
server chained it -- and each of the rig's skeleton bones (`control_rig`: Rigify's ORG chain,
Auto-Rig Pro's IK/FK blend chain) is given the pose of the deform bone that sits on it. The
controls are set to that frame by frame and keyed (`control_rig.write`, `control_rig.key`): FK,
and where the profile describes a limb's IK (Rigify's) the IK matched to the same pose and keyed
too, so switching the limb back to IK at any frame shows the same pose. The deform keys are taken
off again, and the take's travel goes into the rig's top control, as a plain rig carries it in
its root bone, for In place, a loop and a path to move the take through (`top`, `_carry_on_top`).

A limb the bake switched from IK to FK keeps its switch's old value either side of the take
where the action has animation (keyed there, constant: `hold_outside`), so frames outside it
play as before; the IK controls are matched on every frame, so the switch moves nothing at the
edges.

Rigs with no profile (`control_rig.profile` None) are not baked here: a Mixamo Rig has its own
baker (``mixamo_bake``), and a rig Marionette has not connected keeps the deform keys.
"""

from __future__ import annotations

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import _bake_common, control_rig, request_builder


def riders(arm, prof, tree) -> dict:
    """``{skeleton bone: (deform bone, offset)}``: for each bone the profile poses, the bone of the
    tree sent that rides on it -- itself when it is sent, Rigify's ``DEF-`` twin of an ``ORG-``
    bone, or the sent bone that starts where it starts, turned most like it (Auto-Rig Pro's
    ``thigh_twist.l`` on ``thigh.l``, ``c_arm_twist_offset.l`` on ``arm.l``) -- and the rest
    relation between the two, so ``pose(deform) @ offset`` is the skeleton bone's pose. A
    skeleton bone with no rider is left out (its controls are not set)."""
    bones = arm.data.bones
    heads = [bones[n].head_local.z for n in tree if n in bones]
    near = 0.002 * max((max(heads) - min(heads)) if heads else 1.0, 0.1)
    out = {}
    names = [b for _c, b in prof.controls(arm)] + list(prof.joints(arm).values())
    for s in dict.fromkeys(names):
        if s not in bones:
            continue
        if s in tree:
            d = s
        elif s.startswith("ORG-") and "DEF-" + s[4:] in tree:
            d = "DEF-" + s[4:]
        else:
            q = bones[s].matrix_local.to_quaternion()
            at = [n for n in tree if n in bones and (bones[n].head_local - bones[s].head_local).length < near]
            d = min(at, key=lambda n: q.rotation_difference(bones[n].matrix_local.to_quaternion()).angle,
                    default=None)
        if d is not None:
            out[s] = (d, bones[d].matrix_local.inverted() @ bones[s].matrix_local)
    return out


def top(arm, prof) -> str | None:
    """The control everything hangs from: above the body's control (the profile's first), up
    its parents and what the constraints between follow, the first with neither (Rigify's
    ``root``, Auto-Rig Pro's ``c_pos``), a control shape preferred. None when there is none."""
    pairs = prof.controls(arm)
    if not pairs:
        return None
    pbs = arm.pose.bones
    mover = pairs[0][0]
    seen, todo, bare = set(), [mover], []
    while todo:
        n = todo.pop(0)
        if n in seen or n not in pbs:
            continue
        seen.add(n)
        pb = pbs[n]
        deps = [pb.parent.name] if pb.parent is not None else []
        for c in pb.constraints:
            if c.mute:
                continue
            if c.type == 'ARMATURE':
                deps += [x.subtarget for x in c.targets if x.target is arm and x.subtarget]
            elif getattr(c, "target", None) is arm and getattr(c, "subtarget", ""):
                deps.append(c.subtarget)
        if not deps and n != mover:
            if pb.custom_shape is not None:
                return n
            bare.append(n)
        todo += deps
    return bare[0] if bare else None


def _carry_on_top(arm, head, mover, off) -> None:
    """The take's travel into the top control: put under the body's control on the ground (its
    world X and Y less ``off``, where it sits at rest), the body's control then put back where
    it was, so nothing shows a change. The travel is then where a plain rig keeps it, in the
    bone everything hangs from: what In place takes out, what a loop repeats with offset and a
    path moves (on the body's control, a loop's travel snapped back every cycle)."""
    pbs = arm.pose.bones
    mpb, tpb = pbs[mover], pbs[head]
    mw = arm.matrix_world
    M = mpb.matrix.copy()
    body = mw @ M.translation
    T = mw @ tpb.matrix
    T.translation.x, T.translation.y = body.x - off.x, body.y - off.y
    tpb.matrix = mw.inverted() @ T
    bpy.context.view_layer.update()
    control_rig._set_to(arm, mpb, M)
    bpy.context.view_layer.update()


def _key_top(arm, name, frame) -> None:
    """Key the top control as it stands: In place, a loop and a path move the take through it."""
    pb = arm.pose.bones[name]
    path = ("rotation_quaternion" if pb.rotation_mode == 'QUATERNION' else
            "rotation_axis_angle" if pb.rotation_mode == 'AXIS_ANGLE' else "rotation_euler")
    pb.keyframe_insert(path, frame=frame)
    pb.keyframe_insert("location", frame=frame)


def moved_by(arm, prof, controls) -> set:
    """The bones of the tree sent that ``controls`` (names) move: each FK control its skeleton
    bone's rider, each IK control of a limb in IK (its target, pole, chain turn, foot roll, toe,
    pins) the limb's chain, and a control that is a sent bone itself (Auto-Rig Pro's fingers).
    What a key pose pins of an animator's edit on a control rig, and what a pose generated
    for some bones only rewrites."""
    controls = set(controls)
    tree = request_builder.request_tree(arm)
    ride = riders(arm, prof, tree)
    skel = {s for c, s in prof.controls(arm) if c in controls}
    for limb, side in prof.limbs(arm):
        spec = prof.ik_spec(arm, limb, side)
        if spec is None or not prof.is_ik(arm, limb, side):
            continue
        movers = {spec["target"][0], spec.get("pole") or "", spec.get("twist") or ""} \
            | set(spec["reset"]) | {c for c, _b in spec["extra"]} \
            | set(control_rig.pins(arm, prof, limb, side, spec))
        if movers & controls:
            skel.update([spec["root"], spec["mid"], spec["end"]] + [b for _c, b in spec["extra"]])
    return {ride[s][0] for s in skel if s in ride} | (controls & set(tree))


def _channels(action, arm, names) -> dict:
    """``{(bone, prop): {index: fcurve}}`` of ``action``'s rotation and location curves on ``names``."""
    out: dict = {}
    for fc in _bake_common.action_fcurves(action, arm) or ():
        dp = fc.data_path
        if not dp.startswith('pose.bones["'):
            continue
        bone = dp.split('"')[1]
        prop = dp.rsplit(".", 1)[-1]
        if bone in names and prop in ("rotation_quaternion", "location"):
            out.setdefault((bone, prop), {})[fc.array_index] = fc
    return out


def read_pose(arm, prof, tree) -> dict:
    """``{sent bone: armature-space pose matrix}`` as the controls pose it: each sent bone read
    off the skeleton bone it rides on (`riders`), the bone the controls were set by, the rest as
    they evaluate. A twist bone shows part of its limb's turn (Rigify's upper arm, Auto-Rig Pro's
    ``c_arm_twist_offset``: 13 to 38 degrees short of it); a key pose read off it went out with
    the twist the rig spreads, not the one it was given."""
    bpy.context.view_layer.update()
    pbs = arm.pose.bones
    out = {n: pbs[n].matrix.copy() for n in tree if n in pbs}
    for s, (d, off) in riders(arm, prof, tree).items():
        out[d] = pbs[s].matrix @ off.inverted()
    return out


def shown(arm, tree) -> dict:
    """``{bone: armature-space pose matrix}`` of the tree sent as the rig shows it now."""
    bpy.context.view_layer.update()
    pbs = arm.pose.bones
    return {n: pbs[n].matrix.copy() for n in tree if n in pbs}


def poses(arm, action, tree, frames, *, joints=None, before=None) -> dict:
    """``{frame: {deform bone: armature-space pose matrix}}`` from ``action``'s keys on the tree
    sent: each bone's key against its rest, chained down from its parent in the tree (the
    server's parent, not Blender's: on a control rig the two differ).

    ``joints``: the bones the keys are for (None: all of them). Any other bone keeps the pose
    it had against its parent in ``before`` (``{frame: shown(...)}``, read before the keys were
    written), or its rest without it."""
    bones = arm.data.bones
    names = [n for n in tree if n in bones]
    curves = _channels(action, arm, set(names))
    rest = {n: bones[n].matrix_local for n in names}
    rel = {n: rest[tree[n]].inverted() @ rest[n] for n in names if tree[n] in rest}
    out = {}
    for f in frames:
        old = (before or {}).get(f)
        pose = {}
        for n in names:                                     # parents before children
            p = tree[n]
            if joints is not None and n not in joints and old is not None and n in old:
                pose[n] = pose[p] @ (old[p].inverted() @ old[n]) if p in pose and p in old else old[n]
                continue
            rot = curves.get((n, "rotation_quaternion"))
            loc = curves.get((n, "location"))
            q = Quaternion([rot[i].evaluate(f) if i in rot else (1.0 if i == 0 else 0.0) for i in range(4)]) \
                if rot else Quaternion()
            basis = q.normalized().to_matrix().to_4x4()
            if loc:
                basis.translation = Vector([loc[i].evaluate(f) if i in loc else 0.0 for i in range(3)])
            pose[n] = (pose[p] @ rel[n] if p in pose else rest[n]) @ basis
        out[f] = pose
    return out


def _seat(arm, pose: dict, floor: float) -> None:
    """Move ``pose`` (armature space) up or down so its lowest point is on ``floor`` (world
    height): a heel, a toe, a knee for a kneel."""
    mw = arm.matrix_world
    bones = arm.data.bones
    low = min(min((mw @ M.translation).z, (mw @ (M @ Vector((0.0, bones[n].length, 0.0)))).z)
              for n, M in pose.items())
    dz = floor - low
    if abs(dz) > 1e-4:
        shift = Matrix.Translation(mw.inverted().to_3x3() @ Vector((0.0, 0.0, dz)))
        for n in pose:
            pose[n] = shift @ pose[n]


def _drop_keys(action, arm, names, f0: int, f1: int) -> None:
    """Take ``names``' keys from ``f0`` to ``f1`` off ``action``, and a curve left with none:
    keys on a control rig's deform bones do nothing, or move a bone its controls do not
    (Auto-Rig Pro's spine bones carry no constraint)."""
    fcurves = _bake_common.action_fcurves(action, arm)
    if fcurves is None:
        return
    pbs = arm.pose.bones
    for fc in [fc for fc in fcurves if fc.data_path.startswith('pose.bones["')
               and fc.data_path.split('"')[1] in names]:
        kps = fc.keyframe_points
        for i in reversed(range(len(kps))):
            if f0 - 0.5 <= kps[i].co.x <= f1 + 0.5:
                kps.remove(kps[i], fast=True)
        if len(kps):
            fc.update()
            continue
        # and the channel back at rest: the bone still holds the last value the bake wrote
        # (Auto-Rig Pro's root.x kept the take's last step, 5 m on, and carried the body there)
        pb = pbs.get(fc.data_path.split('"')[1])
        prop = fc.data_path.rsplit(".", 1)[-1]
        if pb is not None and prop in ("location", "rotation_quaternion", "rotation_euler", "scale"):
            getattr(pb, prop)[fc.array_index] = _REST[prop][fc.array_index]
        fcurves.remove(fc)


_REST = {"location": (0.0, 0.0, 0.0), "rotation_quaternion": (1.0, 0.0, 0.0, 0.0),
         "rotation_euler": (0.0, 0.0, 0.0), "scale": (1.0, 1.0, 1.0)}


def switches(arm, prof) -> dict:
    """``{(limb, side): value}`` of each limb's IK/FK switch as it is now."""
    out = {}
    for limb, side in prof.limbs(arm):
        sw = prof._switch(arm, limb, side)
        if sw is not None:
            out[(limb, side)] = float(sw[0][sw[1]])
    return out


def hold_outside(arm, prof, action, left: dict, right: dict, f0: int, f1: int) -> None:
    """A limb a bake from ``f0`` to ``f1`` switched to FK: on a side where ``action`` has keys,
    the mode it had there (``left`` / ``right``, by `switches`) keyed just outside the range, and
    the switch's keys made constant, so those frames play as before. A side with no keys gets
    none: it has nothing to play, and a key there would only make a frame nobody asked for."""
    curves = {}
    before = after = False
    for fc in _bake_common.action_fcurves(action, arm) or ():
        curves[fc.data_path] = fc
        if '"][' in fc.data_path:
            continue                                    # a switch's own keys are not animation
        xs = [kp.co.x for kp in fc.keyframe_points]
        before = before or any(x < f0 - 0.5 for x in xs)
        after = after or any(x > f1 + 0.5 for x in xs)
    for (limb, side) in switches(arm, prof):
        holder, prop, _fk = prof._switch(arm, limb, side)
        if not isinstance(holder, bpy.types.PoseBone):
            continue
        fc = curves.get(f'pose.bones["{holder.name}"]["{prop}"]')
        if fc is None:
            continue                                    # never keyed: it is the same everywhere
        inside = fc.evaluate(f0)
        for f, outside, keys in ((f0 - 1, left, before), (f1 + 1, right, after)):
            value = outside.get((limb, side))
            if keys and value is not None and abs(value - inside) > 1e-6 and abs(fc.evaluate(f) - value) > 1e-6:
                fc.keyframe_points.insert(f, value, options={'FAST'})
        for kp in fc.keyframe_points:
            kp.interpolation = 'CONSTANT'
        fc.update()


_SNAPSHOT = {"location": "loc", "rotation_quaternion": "quat", "rotation_euler": "euler",
             "rotation_axis_angle": "aa", "scale": "scale"}


def hold_outside_curves(arm, action, pose: dict, f0: int, f1: int, only=None) -> int:
    """A control's curve with keys only from ``f0`` to ``f1`` (a take's, a paste's) held just
    outside them at the value it had before (``pose``: `preview_session._pose_snapshot`), on a
    side where ``action`` has animation of its own -- or it carries its first or last value over
    those frames: the travel a take keys on the top control moved a kick before it by 1.7 cm.
    ``only``: the ``(data path, index)`` curves to look at (default all). The keys added."""
    curves = list(_bake_common.action_fcurves(action, arm) or ())
    before = after = False
    for fc in curves:
        xs = [kp.co.x for kp in fc.keyframe_points]
        before = before or any(x < f0 - 0.5 for x in xs)
        after = after or any(x > f1 + 0.5 for x in xs)
    added = 0
    for fc in curves:
        dp = fc.data_path
        if not dp.startswith('pose.bones["') or (only is not None and (dp, fc.array_index) not in only):
            continue
        bone, prop = dp.split('"')[1], dp.rsplit(".", 1)[-1]
        key = _SNAPSHOT.get(prop)
        was = (pose.get(bone) or {}).get(key) if key else None
        if was is None or fc.array_index >= len(was):
            continue
        xs = [kp.co.x for kp in fc.keyframe_points]
        for f, side, outside in ((f0 - 1, before, any(x < f0 - 0.5 for x in xs)),
                                 (f1 + 1, after, any(x > f1 + 0.5 for x in xs))):
            if side and not outside:
                fc.keyframe_points.insert(f, float(was[fc.array_index]), options={'FAST'})
                added += 1
        fc.update()
    return added


def switches_in(arm, prof, action, frame: int, fallback: dict) -> dict:
    """Each limb's switch as ``action`` keys it at ``frame``, or ``fallback``'s where it has no
    curve for it."""
    curves = {fc.data_path: fc for fc in _bake_common.action_fcurves(action, arm) or ()} if action else {}
    out = dict(fallback)
    for (limb, side) in fallback:
        holder, prop, _fk = prof._switch(arm, limb, side)
        if isinstance(holder, bpy.types.PoseBone):
            fc = curves.get(f'pose.bones["{holder.name}"]["{prop}"]')
            if fc is not None:
                out[(limb, side)] = fc.evaluate(frame)
    return out


def _switches_at(arm, prof, frame: int) -> dict:
    bpy.context.scene.frame_set(frame)
    return switches(arm, prof)


#: how often a bake that dropped the correction passes tries them again (frames)
RECHECK = 15


def _at_rest(action, arm, tree, frames) -> set:
    """The bones of the tree sent whose rotation keys stay at rest over ``frames``."""
    curves = _channels(action, arm, set(tree))
    out = set()
    for n in tree:
        rot = curves.get((n, "rotation_quaternion"))
        if not rot:
            continue
        if all(abs(rot[i].evaluate(f) - (1.0 if i == 0 else 0.0)) < 1e-5
               for f in frames[::max(1, len(frames) // 20)] + frames[-1:] for i in rot):
            out.add(n)
    return out


def bake(arm, prof, action, frame_start: int, frame_end: int, *, joints=None, before=None,
         floor: float | None = None) -> dict:
    """Put ``action``'s keys on the deform skeleton from ``frame_start`` to ``frame_end`` onto
    ``arm``'s controls (see the module). ``action`` is the rig's active action and gets the
    control keys; the deform keys in the range are taken off. ``joints`` / ``before``: only those
    bones' keys are new, the rest keep the pose they showed (`poses`). ``floor``: each frame's
    pose stood on that world height first. Returns ``{"frames", "controls", "worst"}``: the
    frames keyed, how many skeleton bones were carried, and the worst miss left (metres)."""
    tree = request_builder.request_tree(arm)
    ride = riders(arm, prof, tree)
    if joints is not None:
        # only the bones a new key moves, and those below them in the tree
        moved = set(joints)
        for n, p in tree.items():                           # parents before children
            if p in moved:
                moved.add(n)
        ride = {s: v for s, v in ride.items() if v[0] in moved}
    frames = list(range(int(frame_start), int(frame_end) + 1))
    # the fingers the answer leaves at rest throughout -- the model has none, nor has a paste --
    # are not set: the hand pose the animator gave the finger controls stays
    still = _at_rest(action, arm, tree, frames)
    fingers = {b for side in ("Left", "Right") for chain in prof.finger_chains(arm, side) for b in chain}
    ride = {s: v for s, v in ride.items() if not (s in fingers and v[0] in still)}
    want_at = poses(arm, action, tree, frames, joints=joints, before=before)
    _drop_keys(action, arm, set(tree), frames[0], frames[-1])
    scene = bpy.context.scene
    saved = scene.frame_current
    # each limb's mode just outside the range, as the action keys it there (a block regenerated
    # inside a take that is in FK) or as the rig stands (a take on a rig with none)
    left = _switches_at(arm, prof, frames[0] - 1)
    right = _switches_at(arm, prof, frames[-1] + 1)
    worst = 0.0
    head = top(arm, prof) if joints is None else None
    mover = prof.controls(arm)[0][0] if head is not None else None
    bones = arm.data.bones
    if mover is not None and (mover == head or any(arm.pose.bones[head].lock_location[:2])):
        mover = None                                    # nothing to carry it in
    off = (arm.matrix_world @ bones[mover].head_local) - (arm.matrix_world @ bones[head].head_local) \
        if mover is not None else None
    # the correction passes, while they help this rig: checked again every RECHECK frames
    passes, stats = 3, {}
    control_rig.SWITCHED.pop(arm.name, None)
    try:
        for i, f in enumerate(frames):
            scene.frame_set(f)
            pose = want_at[f]
            if floor is not None:
                _seat(arm, pose, floor)
            want = {s: pose[d] @ off for s, (d, off) in ride.items() if d in pose}
            n = 3 if i % RECHECK == 0 else passes
            worst = max(worst, control_rig.write(arm, prof, want, passes=n, stats=stats))
            if n > 1:
                passes = 1 if stats.get("pass") == 0 else 3
            if mover is not None:
                _carry_on_top(arm, head, mover, off)
            control_rig.key(arm, prof, f, match_modes=True)
            if head is not None:
                _key_top(arm, head, f)
        hold_outside(arm, prof, action, left, right, frames[0], frames[-1])
    finally:
        control_rig.SWITCHED.pop(arm.name, None)
        scene.frame_set(saved)
    return {"frames": len(frames), "controls": len(ride), "worst": worst}
