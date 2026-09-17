# SPDX-License-Identifier: Apache-2.0
"""The control rig: add/removable controls on an armature, solved on this machine.

No sidecar and no round trip — `engine.py` runs the poser and its Gauss-Newton IK through
onnxruntime in Blender's own process, so a drag re-solves the body in a few milliseconds
without leaving the machine. Ported from the sidecar client (`projects/autoposer/blender/`),
which this replaces; the geometry and the rig behaviour below are unchanged from it, and were
verified against the poser one property at a time.

WHAT MAKES IT A CONTROL RIG rather than a set of handles:
  * controls are non-deforming BONES in the same armature, so Pose Mode selects them natively;
  * the taxonomy comes from `rig.json`, generated from `atelier.rig.control_rig` when the addon
    is built — studio shape language (double ring = global, cube = COG, square = IK, circle =
    FK mass, triangle = guide, diamond = pivot, arrow = aim), side colour (LEFT red, RIGHT
    blue, centre yellow, world green), size hierarchy global > cog > IK > FK;
  * every control carries the poser's own channels: effector TYPE (position / rotation /
    look-at) and TOLERANCE — metres of slack, the model's influence dial, which also sets the
    IK weight (1/tol) so poser and solver never contradict each other;
  * controls are ADDED and REMOVED per pose. A control that is off contributes no effector,
    which is the whole point: the poser was trained on 3-20 effectors and fills in the rest
    from its prior. Enabling a control means "put this exactly here"; loosening its tolerance
    hands the decision back to the model.

KEY POSE is how a pose PERSISTS. This addon writes `matrix_basis` directly, which is transient:
with an action bound, any animation evaluation — frame change, mode switch, undo — re-applies
the keys over it. Measured on a rig carrying an action: 746 cm of drift on a frame change
unkeyed, 0.165 cm once keyed. `Take Over Rig` detaches the action instead, for free posing.

Geometry, both verified exactly against the poser on the canonical SOMA armature:
  * frames - Blender is Z-up, the poser Y-up: poser = (x, z, -y). M = R_x(-90 deg).
  * pose   - the poser returns rotations in ARMATURE space, which is what PoseBone.matrix is:
                 M_bone = T(p) @ (M^T . R_global . M) @ bone.matrix_local.to_3x3()
             At rest the poser emits identity rotations, so this collapses to matrix_local.
Applying goes through matrix_basis, derived from the desired PARENT matrix, so the whole pose
lands in ONE depsgraph update instead of one per bone (verified identical to the per-bone path:
1e-6 cm). That, plus a bpy.app.timers loop, is what makes live dragging affordable.
"""
import json
import math
import os
import time

import bpy
import mathutils

from . import engine

# poser = M @ blender
M = mathutils.Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0)))
MT = M.transposed()

SHAPE_COLL = "AP_Shapes"
CTRL_COLL = "Autoposer"          # the bone collection the controls live in
DEFORM_COLL = "Deform"           # where this addon parks a rig's own bones, to hide them
AIM_OFFSET = 0.45          # metres in front of the joint where an aim target is seeded
ROT_LATCH_DEG = 2.0        # rotating a control this far switches its rotation channel on
#: size hierarchy — an animator reads importance from scale (global > cog > IK > FK > guide)
SHAPE_SCALE = {"double_ring": 2.6, "cube": 1.9, "square": 1.25, "circle": 1.1,
               "diamond": 0.8, "triangle": 0.8, "arrow": 1.2}

_BUSY = False
_BUILDING = False
_LAST_KEY = None
_TIMER_ON = False
#: recent solve times, for the panel's meter. A MEDIAN of the last few, not a running average:
#: the first solve of a session loads two graphs and self-tests (~1.5 s), and an exponential
#: average seeded with that reads "2 Hz" for twenty solves afterwards — the addon telling the
#: animator it is slow while it is in fact taking 10 ms.
_STATS = {"ms": 0.0, "n": 0, "recent": []}
_RIG_CACHE = None


# --------------------------------------------------------------------------- the engine
def rig_def(refresh=False):
    """The control taxonomy, read from `rig.json` next to this module.

    It is generated at build time from `atelier.rig.control_rig`, the single source of truth,
    so the addon cannot drift from the rig the research side designs — the same reason the
    sidecar served it from `/rig` rather than letting the client invent one.
    """
    global _RIG_CACHE
    if _RIG_CACHE is None or refresh:
        path = os.path.join(os.path.dirname(__file__), "rig.json")
        with open(path, encoding="utf-8") as fh:
            _RIG_CACHE = json.load(fh)["controls"]
    return _RIG_CACHE


# --------------------------------------------------------------------------- shapes
def _shape_mesh(kind):
    """Wireframe custom-shape objects, one per silhouette in the studio taxonomy."""
    name = f"AP_shape_{kind}"
    ob = bpy.data.objects.get(name)
    if ob is not None:
        return ob
    import math
    verts, edges = [], []

    def ring(r, z=0.0, n=24, base=0):
        for i in range(n):
            a = 2 * math.pi * i / n
            verts.append((r * math.cos(a), r * math.sin(a), z))
        edges.extend([(base + i, base + (i + 1) % n) for i in range(n)])

    if kind == "circle":
        ring(0.5)
    elif kind == "double_ring":
        ring(0.5); ring(0.66, base=24)
    elif kind == "square":
        verts += [(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0)]
        edges += [(0, 1), (1, 2), (2, 3), (3, 0)]
    elif kind == "cube":
        for z in (-0.5, 0.5):
            verts += [(-0.5, -0.5, z), (0.5, -0.5, z), (0.5, 0.5, z), (-0.5, 0.5, z)]
        edges += [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4),
                  (0, 4), (1, 5), (2, 6), (3, 7)]
    elif kind == "triangle":
        verts += [(0.0, 0.6, 0), (-0.52, -0.3, 0), (0.52, -0.3, 0)]
        edges += [(0, 1), (1, 2), (2, 0)]
    elif kind == "diamond":
        verts += [(0, 0, 0.6), (0.5, 0, 0), (0, 0.5, 0), (-0.5, 0, 0), (0, -0.5, 0), (0, 0, -0.6)]
        edges += [(0, 1), (0, 2), (0, 3), (0, 4), (1, 2), (2, 3), (3, 4), (4, 1),
                  (5, 1), (5, 2), (5, 3), (5, 4)]
    elif kind == "arrow":
        verts += [(0, 0, 0), (0, 0.9, 0), (-0.18, 0.6, 0), (0.18, 0.6, 0),
                  (0, 0.6, -0.18), (0, 0.6, 0.18)]
        edges += [(0, 1), (1, 2), (1, 3), (1, 4), (1, 5)]
    else:
        ring(0.5)

    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, edges, [])
    me.update()
    ob = bpy.data.objects.new(name, me)
    coll = bpy.data.collections.get(SHAPE_COLL)
    if coll is None:
        coll = bpy.data.collections.new(SHAPE_COLL)
        bpy.context.scene.collection.children.link(coll)
        coll.hide_viewport = coll.hide_render = True
    coll.objects.link(ob)
    return ob


# --------------------------------------------------------------------------- rig helpers
def _anim_conflict(arm):
    """What, if anything, will overwrite a solved pose on the next animation evaluation.

    The autoposer writes `matrix_basis` directly, which is TRANSIENT: whenever the animation
    system runs — a mode switch, a frame change, an undo — it re-applies the action over our
    result and the solve appears to be "destroyed" and an old pose restored. Measured on a rig
    carrying an action: 740 cm of drift through one Edit-mode round-trip, 0.0 cm once detached.
    """
    ad = getattr(arm, "animation_data", None)
    if ad is None:
        return None
    if ad.action is not None:
        return f"action '{ad.action.name}'"
    live = [t.name for t in ad.nla_tracks if not t.mute]
    return f"NLA track '{live[0]}'" if live else None


def _armature(context):
    ob = bpy.data.objects.get(context.scene.ap_armature)
    return ob if (ob is not None and ob.type == "ARMATURE") else None


def _is_ctrl(b):
    return bool(getattr(b, "ap_joint", ""))


def joint_prefix(arm) -> str:
    """The prefix this rig puts in front of the canonical joint names, or "".

    Real characters rarely use bare SOMA names: the Animatica rig calls its hips
    `animatica:Hips`, a Mixamo export calls it `mixamorig:Hips`, and both are the SAME
    skeleton with a namespace in front. Detecting it costs one scan and makes the addon work
    on the rig the animator already has instead of demanding a renamed copy. Stamped on the
    object at Build Rig time so later lookups are stable even if bones are added.
    """
    got = arm.get("ap_prefix")
    if got is not None:
        return got
    names = {b.name for b in arm.data.bones}
    if "Hips" in names:
        return ""
    for n in names:
        if n.endswith(":Hips") or n.endswith("_Hips"):
            return n[:-len("Hips")]
    return ""


def joint_pose_bone(arm, joint: str):
    """The PoseBone driving a canonical joint, or None."""
    b = joint_bone(arm, joint)
    return arm.pose.bones.get(b.name) if b is not None else None


def joint_bone(arm, joint: str):
    """The armature bone driving a canonical joint, or None. Accepts a namespaced rig."""
    b = arm.data.bones.get(joint)
    if b is not None:
        return b
    return arm.data.bones.get(joint_prefix(arm) + joint)


def joint_names(arm):
    """The canonical joints this rig has a bone for, in SOMA order — the poser's own order,
    which is what the descriptor and the returned pose are indexed by. A rig with MORE bones
    (fingers, toe ends, a head end) is fine: the extra ones are simply not driven."""
    names = engine.meta().get("joint_names") or []
    pre = joint_prefix(arm)
    return [(n, pre + n if (pre + n) in arm.data.bones else n) for n in names
            if joint_bone(arm, n) is not None]


#: SOMA carries four MARKERS in the hand (thumb tip, middle-finger tip) that exist to give the
#: wrist an orientation — they are a short offset from `*Hand` and nothing is skinned to them.
#: A real character rig has bones of the SAME NAMES at the end of four-bone finger chains,
#: which is an entirely different joint. Driving those tore the mesh: measured on the Animatica
#: rig, `LeftHandMiddleEnd` was written 76.6 cm away from where its own chain put it, and the
#: skin came with it. The poser still SOLVES them; they are simply never written back.
MARKER_JOINTS = frozenset({"LeftHandThumbEnd", "LeftHandMiddleEnd",
                           "RightHandThumbEnd", "RightHandMiddleEnd"})

#: How far a rig's rest geometry may disagree with the poser's before a name is not to be
#: trusted. Bone LENGTHS vary by character, which is what the body descriptor is for — a
#: factor of two is not a different build, it is a different joint.
MAX_REST_RATIO = 2.0
MAX_REST_ANGLE = 50.0        # degrees


def driven_joints(arm):
    """The joints this rig lets us WRITE, in joint order: mapped, anatomically the same joint,
    and hanging directly off the joint above them.

    A name match is not enough. Three things disqualify a joint, and each of them showed up on
    a real rig before this existed: it is one of SOMA's hand markers (see `MARKER_JOINTS`); its
    bone does not hang off the bone driving its canonical parent, so bones we do not control
    sit in between and the chain visibly tears at them; or its rest offset disagrees with the
    poser's by more than a build difference could explain.
    """
    cached = arm.get("ap_driven")
    if cached:
        return [(j, bn) for j, bn in joint_names(arm) if j in set(cached)]
    meta = engine.meta()
    names, parents = meta.get("joint_names") or [], meta.get("parents") or []
    rest = engine.skeleton().rest_joints(_bone_lengths(arm))
    out, index = [], {n: i for i, n in enumerate(names)}
    for j, bn in joint_names(arm):
        i = index[j]
        p = parents[i] if i < len(parents) else -1
        if p < 0 or p == i:
            out.append((j, bn))                       # the root is always ours to place
            continue
        if j in MARKER_JOINTS:
            continue
        pb = joint_bone(arm, names[p])
        b = arm.data.bones[bn]
        if pb is None or b.parent is None or b.parent.name != pb.name:
            continue                                  # something undriven sits in between
        rig = b.matrix_local.translation - pb.matrix_local.translation
        rig = mathutils.Vector((rig.x, rig.z, -rig.y))          # blender -> poser frame
        canon = mathutils.Vector((float(rest[i][0] - rest[p][0]),
                                  float(rest[i][1] - rest[p][1]),
                                  float(rest[i][2] - rest[p][2])))
        if rig.length < 1e-6 or canon.length < 1e-6:
            out.append((j, bn))
            continue
        ratio = max(rig.length, canon.length) / max(min(rig.length, canon.length), 1e-6)
        if ratio > MAX_REST_RATIO or math.degrees(rig.angle(canon)) > MAX_REST_ANGLE:
            continue
        out.append((j, bn))
    return out


def _deform_bones(arm):
    """The bones the poser drives, in joint order — NOT every non-control bone. On a rig with
    fingers that distinction is the difference between a 29-length body descriptor and a
    76-length one."""
    return [arm.data.bones[bn] for _j, bn in driven_joints(arm)]


def _controls(arm):
    """Every control bone present, in rig order."""
    return [b for b in arm.data.bones if _is_ctrl(b)]


def _bone_lengths(arm):
    """The body descriptor: each non-root joint's distance to its PARENT JOINT, in joint order.

    Taken over the canonical parents, not over the rig's own bone parents — on a rig with
    extra bones the two differ, and a descriptor in the wrong order poses a different body.
    Raw metres: the runtime sanitises them (a rig differs from the training population on the
    near-constant dims by millimetres, which raw z-scoring turns into a collapsed pose).
    """
    meta = engine.meta()
    parents = meta.get("parents") or []
    canon = {j: bn for j, bn in joint_names(arm)}
    order = meta.get("joint_names") or []
    heads = {j: arm.data.bones[bn].matrix_local.translation for j, bn in canon.items()}
    out = []
    for i, j in enumerate(order):
        p = parents[i] if i < len(parents) else -1
        if p < 0 or p == i:
            continue
        pj = order[p]
        if j in heads and pj in heads:
            out.append((heads[j] - heads[pj]).length)
        else:
            out.append(0.0)
    return out


def _state_key(arm):
    """Fingerprint of what would change the solve: every ENABLED control's transform + tolerance."""
    out = []
    for b in _controls(arm):
        if not b.ap_enabled:
            continue
        pb = arm.pose.bones.get(b.name)
        if pb is None:
            continue
        m = pb.matrix
        out.extend(round(v, 5) for v in m.translation)
        # ORIENTATION IS ALWAYS PART OF THE KEY. Rotating a control has to wake the live timer —
        # that is what auto-latches its rotation channel on. Keying orientation only for non-position
        # controls (as this did) makes rotation work on demand but never live, which reads as
        # "rotation is broken" even though every other layer is correct.
        out.extend(round(v, 4) for row in m.to_3x3() for v in row)
        out.append(round(float(b.ap_tol_m), 5))
        out.append(bool(b.ap_rot))
        out.append(round(float(b.ap_rot_tol_m), 5))
    return tuple(out)


def _rot_effector(arm, joint, b, tol):
    """Control rotation -> the poser's rotation effector (poser frame, first two matrix columns).

    desired_joint_orientation = animator_delta . joint_orientation_at_seat_time, then the joint's
    own REST orientation comes back out before converting frames — the same relation `_apply` uses
    in the other direction. An unrotated control gives delta = I, so the effector asks for exactly
    the orientation the joint already had: adding it changes nothing.
    """
    jb = joint_pose_bone(arm, joint)
    rest3 = jb.bone.matrix_local.to_3x3() if jb else mathutils.Matrix.Identity(3)
    ref = b.get("ap_rot_ref")
    if ref is not None and len(ref) == 9:
        R_ref = mathutils.Matrix(((ref[0], ref[1], ref[2]),
                                  (ref[3], ref[4], ref[5]),
                                  (ref[6], ref[7], ref[8])))
    else:
        R_ref = jb.matrix.to_3x3() if jb else mathutils.Matrix.Identity(3)
    desired = _ctrl_delta(arm, b) @ R_ref
    # the robot wants the LINK's world rotation, and its frame is Blender's: no
    # rest removal, no y-up conversion (the SOMA poser needs both)
    R3 = M @ (desired @ rest3.inverted()) @ MT
    return {"joint": joint, "type": "rot", "tol": tol,
            "rot": [R3[0][0], R3[1][0], R3[2][0], R3[0][1], R3[1][1], R3[2][1]]}


def _ctrl_delta(arm, b):
    """The rotation the ANIMATOR applied to this control, in armature space.

    Identity while the control is unrotated, because `_place` seats it at its own rest orientation.
    """
    pb = arm.pose.bones.get(b.name)
    if pb is None:
        return mathutils.Matrix.Identity(3)
    return pb.matrix.to_3x3() @ pb.bone.matrix_local.to_3x3().inverted()


def _ctrl_rot_offset_deg(arm, b):
    """Magnitude of that rotation, in degrees — what the auto-latch triggers on."""
    d = _ctrl_delta(arm, b)
    tr = max(-1.0, min(1.0, (d[0][0] + d[1][1] + d[2][2] - 1.0) / 2.0))
    return math.degrees(math.acos(tr))


def _latch_rotations(arm):
    """Rotating a control turns its rotation channel ON, by itself.

    Needed because a control that is not contributing rotation is re-seated onto its joint's
    orientation, which would quietly undo the animator's rotation. Latching is one-way on purpose:
    once on, the control owns the orientation, so the residual between request and result cannot
    make the flag flicker. Uncheck `rot` to hand it back to the poser.
    """
    global _BUILDING
    changed = False
    for b in _controls(arm):
        if not b.ap_enabled or b.ap_rot:
            continue
        if _ctrl_rot_offset_deg(arm, b) > ROT_LATCH_DEG:
            _BUILDING = True                      # setting the prop must not re-enter solve()
            try:
                b.ap_rot = True
            finally:
                _BUILDING = False
            changed = True
    return changed


def _effectors(arm):
    """Enabled controls -> the poser's effector list, deduped per (joint, type)."""
    picked = {}
    for b in _controls(arm):
        if not b.ap_enabled:
            continue
        pb = arm.pose.bones.get(b.name)
        if pb is None:
            continue
        joint = b.get("ap_eff") or b.ap_joint
        ety = int(b.get("ap_ety", 0))
        tol = float(b.ap_tol_m)
        key = (joint, ety)
        # two controls can target one joint (root and cog both drive Hips). Keep the tighter.
        if key in picked and picked[key][0] <= tol:
            continue
        m = pb.matrix
        p = M @ m.translation
        if ety == 2:                                    # look-at: control position IS the target
            e = {"joint": joint, "type": "lookat", "pos": [p.x, p.y, p.z]}
        elif ety == 1:
            # rotation effector = the joint's GLOBAL rotation in the poser frame, as the first two
            # matrix columns. The control carries the joint's pose matrix, so the joint's own rest
            # orientation has to come back out before converting frames — same relation as _apply.
            jb = joint_pose_bone(arm, b.ap_joint)
            rest3 = jb.bone.matrix_local.to_3x3() if jb else mathutils.Matrix.Identity(3)
            R3 = M @ (m.to_3x3() @ rest3.inverted()) @ MT
            e = {"joint": joint, "type": "rot", "tol": tol,
                 "rot": [R3[0][0], R3[1][0], R3[2][0], R3[0][1], R3[1][1], R3[2][1]]}
        else:
            e = {"joint": joint, "type": "pos", "pos": [p.x, p.y, p.z], "tol": tol}
        picked[key] = (tol, e)
        # a control can carry BOTH channels for its joint — the trainer samples position and
        # rotation effectors independently, so (joint, pos) + (joint, rot) is in-distribution
        if ety == 0 and b.ap_rot:
            rk = (joint, 1)
            rtol = float(b.ap_rot_tol_m)
            if rk not in picked or picked[rk][0] > rtol:
                picked[rk] = (rtol, _rot_effector(arm, joint, b, rtol))
    return [e for _t, e in picked.values()]


# --------------------------------------------------------------------------- solve + apply
def toe_tips(arm):
    """{ball joint: offset to the rig's toe tip}, in the poser's frame, from the REST pose.

    SOMA-30 ends at the ball of the foot, so the poser has no idea a toe continues past it.
    A character rig does — `animatica:LeftToeEnd` and its like — and without telling the
    runtime about that bone the floor fix puts the BALL on the ground and leaves the toes
    pointing into it, which reads as bent toes rather than flat ones.

    Measured at rest, where the poser's own rotations are identity, so a rest offset in the
    armature's frame IS the offset in the ball's local frame.
    """
    out = {}
    for _ankle, ball in engine.skeleton().FOOT_CHAINS:
        b = joint_bone(arm, ball)
        if b is None:
            continue
        kids = [c for c in b.children if not _is_ctrl(c)]
        if not kids:
            continue
        tip = min(kids, key=lambda c: (c.matrix_local.translation
                                       - b.matrix_local.translation).length)
        d = tip.matrix_local.translation - b.matrix_local.translation
        out[ball] = [d.x, d.z, -d.y]                        # blender -> poser
    return out


def pose_bases(arm, names, pos, r6):
    """The solve as {bone name: matrix_basis}, without writing anything.

    Split out of `_apply` so a solve can also be keyed at a frame the rig is
    not standing on — the whole computation reads rest data (`matrix_local`)
    and the solve, never the current pose, so it holds at any frame. Animatica
    drives it from the motion-curve drag, which solves for one frame while the
    playhead stays where the artist left it.

    Only `driven_joints` are written: a bone whose name matches a canonical joint but whose
    chain says otherwise is left alone, because forcing it detaches it from the bones above it
    and the skin tears at exactly that seam.
    """
    drivable = {j for j, _bn in driven_joints(arm)}
    want = {}
    for i, nm in enumerate(names):
        if nm not in drivable:
            continue
        pb = joint_pose_bone(arm, nm)
        if pb is None or _is_ctrl(pb.bone):
            continue
        c0 = mathutils.Vector(r6[i][0:3]).normalized()
        c1 = mathutils.Vector(r6[i][3:6])
        c1 = (c1 - c1.dot(c0) * c0).normalized()
        c2 = c0.cross(c1)
        R_ps = mathutils.Matrix(((c0.x, c1.x, c2.x), (c0.y, c1.y, c2.y), (c0.z, c1.z, c2.z)))
        m = ((MT @ R_ps @ M) @ pb.bone.matrix_local.to_3x3()).to_4x4()
        m.translation = MT @ mathutils.Vector(pos[i])
        want[pb.name] = m
    bases = {}
    for nm, target in want.items():
        b = arm.pose.bones[nm].bone
        if b.parent is not None and b.parent.name in want:
            base = want[b.parent.name] @ (b.parent.matrix_local.inverted() @ b.matrix_local)
        else:
            base = b.matrix_local.copy()
        bases[nm] = base.inverted() @ target
    return bases


def _apply(arm, names, pos, r6):
    """Write the solve onto the rig, one depsgraph update for the whole body."""
    for nm, basis in pose_bases(arm, names, pos, r6).items():
        arm.pose.bones[nm].matrix_basis = basis


def solve(context, report=None):
    global _BUSY
    if _BUSY:
        return False
    arm = _armature(context)
    if arm is None:
        if report:
            report({"ERROR"}, "Pick a SOMA-30 armature in the Autoposer panel")
        return False
    _latch_rotations(arm)
    eff = _effectors(arm)
    n_pos = sum(1 for e in eff if e["type"] == "pos")
    if n_pos < 3:
        if report:
            report({"WARNING"}, f"{n_pos} position control(s) enabled — the poser was trained on 3+")
        if n_pos == 0:
            return False
    try:
        eng = engine.get()          # outside the timing: the first call loads the model
    except engine.NotReady as e:
        context.scene.ap_live = False
        if report:
            report({"ERROR"}, str(e))
        context.scene.ap_status = str(e)
        return False
    t0 = time.perf_counter()
    try:
        floor = bool(context.scene.ap_floor)
        out = eng.pose(eff, bone_lengths=_bone_lengths(arm),
                       ik_refine=context.scene.ap_use_ik,
                       # one switch: the floor is solid, or it is not there. The solver's own
                       # floor term stays on the feet — the set the checkpoint was trained
                       # alongside, and the cheap one — and the geometric pass makes it a
                       # guarantee for everything else. Pointing the term at the whole body
                       # as well costs ~5 ms a solve and changes only how far the geometric
                       # pass then has to move things.
                       floor=floor, floor_joints="feet", hard_floor=floor,
                       toe_roll=floor, toe_tips=toe_tips(arm) if floor else None)
    except engine.NotReady as e:
        context.scene.ap_live = False
        if report:
            report({"ERROR"}, str(e))
        context.scene.ap_status = str(e)
        return False
    except Exception as e:                                    # noqa: BLE001
        if report:
            report({"ERROR"}, f"solve failed: {e}")
        return False

    _BUSY = True
    try:
        _apply(arm, out["names"], out["joints"], out["rotations_6d"])
        context.view_layer.update()          # joints must be evaluated before controls read them
        _follow(arm)          # every DISABLED control tracks the joint the poser just placed
        _reseat_rot_refs(arm)  # ...and every non-rotating control re-anchors its rotation frame
        context.view_layer.update()          # ...and the controls' own matrices re-evaluated
    finally:
        _BUSY = False

    ms = (time.perf_counter() - t0) * 1000
    _STATS["recent"].append(ms)
    del _STATS["recent"][:-20]
    _STATS["ms"] = sorted(_STATS["recent"])[len(_STATS["recent"]) // 2]
    _STATS["n"] += 1
    ik = out.get("ik") or {}
    err = (f"IK {ik.get('err_before_cm', 0):.1f}->{ik.get('err_after_cm', 0):.2f} cm"
           if ik else f"err {out.get('err_cm', 0):.2f} cm")
    if ik and ik.get("toe_roll_cm", 0.0) > 0.05:
        err += f" | toe +{ik['toe_roll_cm']:.1f}"
    if ik and ik.get("below_floor_cm", 0.0) < -0.1:
        err += f" | {abs(ik['below_floor_cm']):.1f} under"
    context.scene.ap_status = (
        f"{len(eff)} eff | {_STATS['ms']:.0f} ms (~{1000 / max(_STATS['ms'], 1):.0f} Hz) | {err}")
    return True


# --------------------------------------------------------------------------- property callbacks
def _owner_armature(bone):
    data = bone.id_data                                  # the Armature datablock
    for ob in bpy.data.objects:
        if ob.type == "ARMATURE" and ob.data is data:
            return ob
    return None


def _on_enabled(self, context):
    """Toggling a control is asymmetric, and deliberately so.

    Turning ON is INERT: seat the control on its joint and do not re-solve. The animator has not
    asked for anything to move, so nothing should — the control simply starts participating, and
    the next drag includes it. (Re-solving here would jerk the body several centimetres even though
    the new effector asks for the pose that already exists: adding an effector changes the
    network's input AND the centroid every position target is referenced to, so the poser is not
    idempotent under a redundant constraint. Measured 4.9 cm on the hand, 13 cm on a head aim.)

    Turning OFF re-solves: the joint has genuinely been freed, so the poser SHOULD re-decide it,
    and the now-disabled control follows wherever it lands.
    """
    if _BUILDING or _BUSY:
        return
    arm = _owner_armature(self)
    if arm is None:
        return
    if self.ap_enabled:
        _place(arm, self)
        context.view_layer.update()
    else:
        solve(context)


def _on_tol(self, context):
    if _BUILDING or _BUSY:
        return
    if _owner_armature(self) is not None:
        solve(context)


def _on_rot(self, context):
    """Same asymmetry as ap_enabled: switching rotation ON is inert (the control already carries
    the joint's orientation), switching it OFF re-solves because the orientation is handed back."""
    if _BUILDING or _BUSY:
        return
    arm = _owner_armature(self)
    if arm is None:
        return
    if not self.ap_rot:
        _place(arm, self)                 # drop the animator's rotation, resume following
        context.view_layer.update()
        solve(context)


# --------------------------------------------------------------------------- live timer
def _tick():
    global _LAST_KEY
    ctx = bpy.context
    scene = getattr(ctx, "scene", None)
    if scene is None or not getattr(scene, "ap_live", False):
        return None
    arm = _armature(ctx)
    if arm is None:
        return 0.2
    key = _state_key(arm)
    if key != _LAST_KEY:
        _LAST_KEY = key
        solve(ctx)
    return 1.0 / max(scene.ap_rate, 1)


def _set_live(self, context):
    global _TIMER_ON, _LAST_KEY
    if self.ap_live and not _TIMER_ON:
        _LAST_KEY = None
        bpy.app.timers.register(_tick, persistent=True)
        _TIMER_ON = True
    elif not self.ap_live and _TIMER_ON:
        if bpy.app.timers.is_registered(_tick):
            bpy.app.timers.unregister(_tick)
        _TIMER_ON = False


# --------------------------------------------------------------------------- control creation
def _clear_transform_flag(context):
    """Adding or removing a control needs Edit Mode, and mode_set refuses while Blender thinks a
    transform is in progress. That flag can be left set when pose data is written from a script
    while a modal grab is live (`window.modal_operators` reads EMPTY, yet mode_set fails with
    "Unable to change object mode while transforming"). A no-op non-modal transform resets it.
    Harmless when nothing is stuck; interactive users never hit this, script drivers do."""
    try:
        bpy.ops.object.mode_set(mode=context.object.mode)   # cheap probe
        return True
    except Exception:
        pass
    try:
        win = context.window
        area = next(a for a in win.screen.areas if a.type == "VIEW_3D")
        region = next(r for r in area.regions if r.type == "WINDOW")
        with context.temp_override(window=win, area=area, region=region):
            bpy.ops.transform.translate(value=(0.0, 0.0, 0.0))
        return True
    except Exception:
        return False



def _seed_position(arm, spec):
    """Where a freshly added control sits: on its joint, or in front of it for an aim."""
    src = joint_pose_bone(arm, spec["joint"])
    rest = joint_bone(arm, spec["joint"])
    if src is None or rest is None:
        return None
    p = src.matrix.translation.copy()
    if spec["ety"] == 2:
        p = p + mathutils.Vector((0.0, -AIM_OFFSET, 0.0))     # -Y is 'forward' in this rig's frame
    return p


def _preserve_pose(arm):
    """Snapshot the deform bones' matrix_basis before a STRUCTURAL edit.

    Adding or deleting a control needs Edit Mode, and that round-trip can perturb the evaluated
    pose (measured: a joint moving 18 cm while nothing was solved). Restoring the basis afterwards
    reproduces the pose exactly — verified 0.0001 cm — so a structural edit is guaranteed not to
    move the character, which is the whole contract of add/remove.
    """
    return {b.name: arm.pose.bones[b.name].matrix_basis.copy()
            for b in _deform_bones(arm) if arm.pose.bones.get(b.name)}


def _restore_pose(arm, snap, context):
    for n, m in snap.items():
        pb = arm.pose.bones.get(n)
        if pb is not None:
            pb.matrix_basis = m
    context.view_layer.update()


def _control_collection(arm):
    """The bone collection the controls live in, created once.

    Their own collection because that is what makes them a RIG rather than loose bones: the
    animator can hide the deform chain and keep the handles, which is how every character rig
    in a studio is worked with.
    """
    coll = arm.data.collections.get(CTRL_COLL)
    if coll is None:
        coll = arm.data.collections.new(CTRL_COLL)
    return coll


def _bone_collections(arm):
    return getattr(arm.data, "collections_all", None) or arm.data.collections


def _hide_deform_bones(arm, hide=True):
    """Show the controls and the character, not the skeleton between them.

    `show_in_front` is an OBJECT flag, so bringing the handles forward brings every bone
    forward — a body covered in octahedra with the controls lost among them. Every character
    rig answers this the same way: hide the deform chain once there is something to grab it by.

    Through bone COLLECTIONS, not `Bone.hide`. On Blender 5.1 that flag sets, reads back as
    set, survives an Edit-Mode round trip, appears set on the EVALUATED armature — and changes
    nothing on screen. Collections are what 4.x+ actually draws by, so a rig's own bones are
    parked in one and its visibility is the switch. Display only: the solver reads bone
    matrices and does not care what is visible.
    """
    ctrl = _control_collection(arm)
    deform = arm.data.collections.get(DEFORM_COLL) or arm.data.collections.new(DEFORM_COLL)
    for b in arm.data.bones:
        if not _is_ctrl(b) and DEFORM_COLL not in {c.name for c in b.collections}:
            deform.assign(b)
        if b.hide:
            b.hide = False          # never leave a flag set that does nothing on some versions
    others = [c for c in _bone_collections(arm) if c.name != ctrl.name]
    if hide:
        # remember what the rig had visible, so giving it back gives back what was there
        if "ap_shown_colls" not in arm:
            arm["ap_shown_colls"] = [c.name for c in others if c.is_visible]
        for c in others:
            c.is_visible = False
    else:
        was = list(arm.get("ap_shown_colls") or [])
        for c in others:
            c.is_visible = (c.name in was) if was else True
        if "ap_shown_colls" in arm:
            del arm["ap_shown_colls"]
    ctrl.is_visible = True


def _on_hide_deform(self, context):
    arm = _armature(context)
    if arm is not None:
        _hide_deform_bones(arm, self.ap_hide_deform)


def _show_controls_in_front(arm, on=True):
    """Draw the armature over the mesh.

    A control you cannot see is a control you cannot grab, and a handle on the hips or the chest
    is INSIDE the character. Blender has no per-bone depth setting — `show_in_front` is an
    object-level flag — so the whole armature comes forward, which is the normal state for a
    rig being animated.
    """
    arm.show_in_front = bool(on)


def _add_control(arm, spec, context):
    """Create one control bone from a /rig spec. Returns the bone name."""
    _clear_transform_flag(context)
    name = spec["name"]
    bone_name = spec["joint"]
    jb = joint_bone(arm, bone_name)
    rest_head = jb.matrix_local.translation.copy()
    if spec["ety"] == 2:
        rest_head = rest_head + mathutils.Vector((0.0, -AIM_OFFSET, 0.0))
    prev = arm.mode
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        eb = arm.data.edit_bones
        b = eb.get(name) or eb.new(name)
        b.head = rest_head
        b.tail = rest_head + mathutils.Vector((0.0, 0.0, 0.08))
        b.parent = None
        b.use_deform = False
        b.use_connect = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    bone = arm.data.bones[name]
    try:
        _control_collection(arm).assign(bone)
    except Exception:                                   # noqa: BLE001
        pass
    # wireframe, so the shape reads as an outline over the mesh instead of a solid blob
    bone.show_wire = True
    bone["ap_ety"] = int(spec["ety"])
    bone["ap_kind"] = spec["kind"]
    bone["ap_shape"] = spec["shape"]
    bone["ap_side"] = spec["side"]
    bone.ap_joint = bone_name                  # what the control FOLLOWS (a bone)
    bone["ap_eff"] = spec["joint"]             # what the SOLVER calls it
    bone.ap_tol_m = float(spec["tol"])
    bone.ap_rot_tol_m = float(spec.get("rot_tol", spec["tol"]))
    bone.ap_rot = int(spec["ety"]) == 1        # a dedicated rotation control starts rotational
    bone.ap_enabled = True
    bone.use_deform = False
    pb = arm.pose.bones[name]
    pb.custom_shape = _shape_mesh(spec["shape"])
    pb.use_custom_shape_bone_size = False
    s = SHAPE_SCALE.get(spec["shape"], 1.0) * 0.10
    try:
        pb.custom_shape_scale_xyz = (s, s, s)
    except Exception:
        pass
    try:
        pb.color.palette = "CUSTOM"
        c = tuple(spec["colour"])
        pb.color.custom.normal = c
        pb.color.custom.select = tuple(min(1.0, v + 0.25) for v in c)
        pb.color.custom.active = (1.0, 1.0, 1.0)
    except Exception:
        pass
    bpy.ops.object.mode_set(mode="POSE" if prev == "POSE" else "OBJECT")
    return name


def _aim_dir(joint_pb):
    """World (Blender-frame) direction of the poser's LOCAL aim axis for this joint.

    The poser aims its local +z (poser frame). Pushing that through the apply relation
    M_bone = MT . R_ps . M . rest  gives  dir_blender = (pose3 . rest3^-1) . (MT . +z_poser),
    and MT @ (0,0,1) is (0,-1,0). Using the bone's own local +z instead would aim the wrong axis.
    """
    pose3 = joint_pb.matrix.to_3x3()
    rest3 = joint_pb.bone.matrix_local.to_3x3()
    return (pose3 @ rest3.inverted()) @ mathutils.Vector((0.0, -1.0, 0.0))


def _place(arm, b):
    """Seat one control on its joint's CURRENT pose — the identity placement.

    This is what makes a disabled control track its joint and an enabled one start without a jump:
    the control always describes exactly what the joint is already doing, so turning it on adds a
    constraint the pose already satisfies.
    """
    pb = arm.pose.bones.get(b.name)
    src = joint_pose_bone(arm, b.ap_joint)
    if pb is None or src is None:
        return
    rest = pb.bone.matrix_local
    # Keep the control's ROTATION CHANNEL at identity and record the joint's orientation separately.
    # The animator's rotation is then a clean DELTA on top of it, which is how a rig control behaves:
    # an unrotated control (and Alt+R) means "leave the orientation to the poser", and Blender's
    # loc/rot/scale channels stay readable instead of carrying a baked-in joint frame.
    want = rest.copy()
    ety = int(b.get("ap_ety", 0))
    if ety == 2:
        want.translation = src.matrix.translation + AIM_OFFSET * _aim_dir(src).normalized()
    else:
        want.translation = src.matrix.translation
    pb.matrix_basis = rest.inverted() @ want
    r = src.matrix.to_3x3()          # the JOINT's orientation at seat time = the delta's origin
    b["ap_rot_ref"] = [r[i][j] for i in range(3) for j in range(3)]


def _reseat_rot_refs(arm):
    """Re-anchor the rotation reference of every control that is NOT driving rotation.

    The animator's rotation is a delta on top of `ap_rot_ref`, so that reference has to be the
    joint's CURRENT orientation — otherwise the delta is measured from a stale frame and a small
    twist of the control asks for a huge reorientation (measured: a 20 deg control rotation
    requesting a 76 deg change, because the reference still held the rest pose). Controls that ARE
    driving rotation keep their anchor: it is the frame their delta was applied to.
    """
    for b in _controls(arm):
        if b.ap_rot:
            continue
        jb = joint_pose_bone(arm, b.ap_joint)
        if jb is None:
            continue
        r = jb.matrix.to_3x3()
        b["ap_rot_ref"] = [r[i][j] for i in range(3) for j in range(3)]


def _follow(arm, only_disabled=True, names=None):
    """Slave controls to their joints. Disabled controls follow the solve; enabled ones lead it."""
    for b in _controls(arm):
        if names is not None and b.name not in names:
            continue
        if only_disabled and b.ap_enabled:
            continue
        _place(arm, b)


def _snap(arm, context, names=None):
    _follow(arm, only_disabled=False, names=names)
    context.view_layer.update()


# --------------------------------------------------------------------------- operators
class AP_OT_build_rig(bpy.types.Operator):
    bl_idname = "autoposer.build_rig"
    bl_label = "Build Rig"
    bl_description = "Create the control rig (default-on controls) from the sidecar's taxonomy"
    bl_options = {"REGISTER", "UNDO"}

    all_controls: bpy.props.BoolProperty(
        name="All controls", default=False,
        description="Create every control in the taxonomy, not just the default set")

    def execute(self, context):
        arm = _armature(context)
        if arm is None:
            self.report({"ERROR"}, "Pick a SOMA-30 armature first")
            return {"CANCELLED"}
        try:
            rig = rig_def(refresh=True)
        except Exception as e:
            self.report({"ERROR"}, f"cannot read /rig from the sidecar: {e}")
            return {"CANCELLED"}
        _show_controls_in_front(arm)           # handles inside a body are not handles
        _hide_deform_bones(arm, context.scene.ap_hide_deform)
        arm["ap_prefix"] = joint_prefix(arm)    # settle the namespace once, at build time
        arm["ap_driven"] = []
        have = joint_names(arm)
        drivable = driven_joints(arm)
        arm["ap_driven"] = [j for j, _bn in drivable]
        if len(have) < 3:
            self.report({"ERROR"},
                        "that armature has no SOMA-30 joints (looked for Hips, LeftHand, …)")
            return {"CANCELLED"}
        global _BUILDING
        _BUILDING = True                    # creating controls must not fire the toggle callbacks
        keep = _preserve_pose(arm)
        made = []
        try:
            for spec in rig:
                if not (self.all_controls or spec["default_on"]):
                    continue
                if joint_bone(arm, spec["joint"]) is None:
                    continue
                made.append(_add_control(arm, spec, context))
        finally:
            _BUILDING = False
        _restore_pose(arm, keep, context)
        _snap(arm, context)
        pre = arm.get("ap_prefix") or ""
        skipped = len(have) - len(drivable)
        self.report({"INFO"}, f"{len(made)} controls, driving {len(drivable)} joints"
                              + (f" (rig prefix {pre!r})" if pre else "")
                              + (f", {skipped} name-matched bones left alone" if skipped else "")
                              + " — grab them in Pose Mode")
        return {"FINISHED"}


class AP_OT_add_control(bpy.types.Operator):
    bl_idname = "autoposer.add_control"
    bl_label = "Add Control"
    bl_description = "Add one control from the rig taxonomy"
    bl_options = {"REGISTER", "UNDO"}

    def _items(self, context):
        arm = _armature(context)
        try:
            rig = rig_def()
        except Exception:
            return [("NONE", "sidecar unreachable", "")]
        have = {b.name for b in _controls(arm)} if arm else set()
        out = [(s["name"], f"{s['name']}  ({s['joint']}, {s['kind']})", s["kind"])
               for s in rig if s["name"] not in have]
        return out or [("NONE", "all controls already present", "")]

    control: bpy.props.EnumProperty(name="Control", items=_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        arm = _armature(context)
        if arm is None or self.control == "NONE":
            return {"CANCELLED"}
        spec = next((s for s in rig_def() if s["name"] == self.control), None)
        if spec is None or joint_bone(arm, spec["joint"]) is None:
            self.report({"ERROR"}, "control not in this armature's taxonomy")
            return {"CANCELLED"}
        global _BUILDING
        _BUILDING = True
        keep = _preserve_pose(arm)
        try:
            name = _add_control(arm, spec, context)
        finally:
            _BUILDING = False
        _restore_pose(arm, keep, context)
        # Seat the new control on the joint's CURRENT pose and do NOT re-solve — adding a control
        # should not move the character (see _on_enabled). It joins in on the next drag.
        _snap(arm, context, names={name})
        self.report({"INFO"}, f"added {name}")
        return {"FINISHED"}


class AP_OT_remove_control(bpy.types.Operator):
    bl_idname = "autoposer.remove_control"
    bl_label = "Remove Control"
    bl_description = "Delete this control bone (the poser fills that joint from its prior)"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty()

    def execute(self, context):
        arm = _armature(context)
        if arm is None:
            return {"CANCELLED"}
        target = self.name or (arm.data.bones.active.name if arm.data.bones.active else "")
        if not target or target not in arm.data.bones or not _is_ctrl(arm.data.bones[target]):
            self.report({"ERROR"}, "not a control bone")
            return {"CANCELLED"}
        prev = arm.mode
        context.view_layer.objects.active = arm
        _clear_transform_flag(context)
        keep = _preserve_pose(arm)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            eb = arm.data.edit_bones
            b = eb.get(target)
            if b is not None:
                eb.remove(b)
        finally:
            bpy.ops.object.mode_set(mode="POSE" if prev == "POSE" else "OBJECT")
        _restore_pose(arm, keep, context)     # deleting a control must not move the character
        self.report({"INFO"}, f"removed {target}")
        return {"FINISHED"}


class AP_OT_key_pose(bpy.types.Operator):
    bl_idname = "autoposer.key_pose"
    bl_label = "Key Pose"
    bl_description = ("Commit the solved pose as keyframes on the current frame: a rotation key on "
                      "every deform bone and a location key on the root. This is how the pose "
                      "SURVIVES frame and mode changes, and it is the form Proscenium reads for a "
                      "blockout pose")
    bl_options = {"REGISTER", "UNDO"}

    key_controls: bpy.props.BoolProperty(
        name="Also key controls", default=False,
        description="Key the CTRL_* bones too, so you can return to this frame and keep editing "
                    "from the same control layout")

    def execute(self, context):
        arm = _armature(context)
        if arm is None:
            return {"CANCELLED"}
        f = context.scene.frame_current
        if arm.animation_data is None:
            arm.animation_data_create()
        if arm.animation_data.action is None:
            arm.animation_data.action = bpy.data.actions.new(f"{arm.name}_Autoposer")
        n = 0
        bones = list(_deform_bones(arm))
        if self.key_controls:
            bones += list(_controls(arm))
        for b in bones:
            pb = arm.pose.bones.get(b.name)
            if pb is None:
                continue
            path = ("rotation_quaternion" if pb.rotation_mode == "QUATERNION"
                    else "rotation_euler" if pb.rotation_mode != "AXIS_ANGLE"
                    else "rotation_axis_angle")
            pb.keyframe_insert(data_path=path, frame=f)
            n += 1
        # the root also needs its LOCATION keyed — rotations alone leave the body's placement to
        # whatever else is driving it, which is the whole failure this operator exists to prevent
        root = joint_pose_bone(arm, (engine.meta().get("joint_names") or ["Hips"])[0])
        if root is not None:
            root.keyframe_insert(data_path="location", frame=f)
        self.report({"INFO"}, f"keyed {n} bones + root location at frame {f}")
        return {"FINISHED"}


class AP_OT_take_over(bpy.types.Operator):
    bl_idname = "autoposer.take_over"
    bl_label = "Take Over Rig"
    bl_description = ("Detach the action driving this rig so the solved pose survives mode "
                      "switches and frame changes. The action is kept (fake user) and can be "
                      "put back")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm = _armature(context)
        if arm is None or arm.animation_data is None:
            return {"CANCELLED"}
        ad = arm.animation_data
        if ad.action is not None:
            ad.action.use_fake_user = True          # unlinking drops a user; never lose their data
            arm["ap_stashed_action"] = ad.action.name
            # Blender 4.4+ actions are SLOTTED: the slot binding is what actually drives this
            # object, and re-assigning the action alone leaves it unbound (silently inert). Stash
            # the binding too, or "Give Back" hands back an action that animates nothing.
            arm["ap_stashed_slot"] = int(getattr(ad, "action_slot_handle", 0) or 0)
            arm["ap_stashed_slot_id"] = str(getattr(ad, "last_slot_identifier", "") or "")
            ad.action = None
        muted = []
        for t in ad.nla_tracks:
            if not t.mute:
                t.mute = True
                muted.append(t.name)
        arm["ap_muted_nla"] = muted
        solve(context, self.report)
        self.report({"INFO"}, "rig detached from its animation — the autoposer owns the pose")
        return {"FINISHED"}


class AP_OT_release(bpy.types.Operator):
    bl_idname = "autoposer.release"
    bl_label = "Give Back"
    bl_description = "Re-attach the action (and un-mute NLA) the autoposer took over"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm = _armature(context)
        if arm is None:
            return {"CANCELLED"}
        ad = arm.animation_data or arm.animation_data_create()
        name = arm.get("ap_stashed_action")
        if name and name in bpy.data.actions:
            act = bpy.data.actions[name]
            ad.action = act
            handle = int(arm.get("ap_stashed_slot", 0) or 0)
            slot_id = str(arm.get("ap_stashed_slot_id", "") or "")
            try:                                     # rebind by handle, else by identifier
                if handle and getattr(ad, "action_slot_handle", None) != handle:
                    ad.action_slot_handle = handle
                if not getattr(ad, "action_slot_handle", 0) and slot_id:
                    for sl in getattr(act, "slots", []):
                        if getattr(sl, "identifier", None) == slot_id:
                            ad.action_slot = sl
                            break
            except Exception as e:
                self.report({"WARNING"}, f"action re-linked but its slot did not rebind: {e}")
        for t in ad.nla_tracks:
            if t.name in list(arm.get("ap_muted_nla", [])):
                t.mute = False
        for k in ("ap_stashed_action", "ap_muted_nla", "ap_stashed_slot", "ap_stashed_slot_id"):
            if k in arm:
                del arm[k]
        self.report({"INFO"}, "animation re-attached — it will drive the pose again")
        return {"FINISHED"}


class AP_OT_solve(bpy.types.Operator):
    bl_idname = "autoposer.solve"
    bl_label = "Solve Pose"
    bl_description = "Pose the body from the enabled controls"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        return {"FINISHED"} if solve(context, self.report) else {"CANCELLED"}


class AP_OT_snap_controls(bpy.types.Operator):
    bl_idname = "autoposer.snap_controls"
    bl_label = "Snap Controls to Pose"
    bl_description = "Move every control back onto its joint's current position"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        global _LAST_KEY
        arm = _armature(context)
        if arm is None:
            return {"CANCELLED"}
        _snap(arm, context)
        _LAST_KEY = _state_key(arm)
        return {"FINISHED"}


class AP_OT_rest(bpy.types.Operator):
    bl_idname = "autoposer.rest"
    bl_label = "Reset to Rest"
    bl_description = "Clear the pose on the deform bones and re-seat the controls"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm = _armature(context)
        if arm is None:
            return {"CANCELLED"}
        for b in _deform_bones(arm):
            arm.pose.bones[b.name].matrix_basis = mathutils.Matrix()
        context.view_layer.update()
        _snap(arm, context)
        return {"FINISHED"}


# --------------------------------------------------------------------------- UI
class AP_PT_panel(bpy.types.Panel):
    bl_label = "Autoposer"
    bl_idname = "AP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Animatica"     # ported: one addon, one tab

    def draw(self, context):
        s = context.scene
        lay = self.layout
        st = engine.status()
        if not (st["runtime"] and st["model"]):
            # first run: say what is missing and where to fix it, rather than failing at the
            # first drag with an error in the status bar
            box = lay.box().column(align=True)
            box.label(text="Set up the model", icon="ERROR")
            if not st["runtime"]:
                box.label(text="the inference runtime is not installed")
            if not st["model"]:
                box.label(text="no model on this machine yet")
            box.operator("screen.userpref_show", text="Open Preferences",
                         icon="PREFERENCES").section = "ADDONS"
            return
        arm = _armature(context)
        conflict = _anim_conflict(arm) if arm else None
        if conflict:
            box = lay.box().column(align=True)
            box.label(text="Pose will be overwritten", icon="ERROR")
            box.label(text=f"driven by {conflict}")
            box.label(text="a mode or frame change re-applies it")
            box.operator("autoposer.key_pose", icon="KEYINGSET")
            box.operator("autoposer.take_over", text="or detach it", icon="UNLOCKED")
        elif arm is not None and arm.get("ap_stashed_action"):
            row = lay.box().row(align=True)
            row.label(text="animation detached", icon="CHECKMARK")
            row.operator("autoposer.release", text="Give Back", icon="LOCKED")
        col = lay.column(align=True)
        col.prop_search(s, "ap_armature", bpy.data, "objects", text="Rig")
        col.separator()
        col.operator("autoposer.build_rig", icon="OUTLINER_OB_ARMATURE")
        if arm is not None:
            row = col.row(align=True)
            row.prop(arm, "show_in_front", text="In front", icon="XRAY")
            row.prop(s, "ap_hide_deform", text="Hide skeleton", icon="HIDE_ON")
        row = col.row(align=True)
        row.operator("autoposer.solve", icon="ARMATURE_DATA")
        row.prop(s, "ap_live", text="Live", toggle=True, icon="PLAY")
        col.prop(s, "ap_rate")
        col.separator()
        col.operator("autoposer.key_pose", icon="KEYINGSET")
        row = col.row(align=True)
        row.operator("autoposer.snap_controls", text="Snap", icon="SNAP_ON")
        row.operator("autoposer.rest", text="Rest", icon="LOOP_BACK")
        row = col.row(align=True)
        row.prop(s, "ap_use_ik")
        row.prop(s, "ap_floor")
        if s.ap_status:
            col.label(text=s.ap_status, icon="INFO")
        col.label(text="solved on this machine", icon="LOCKED")


class AP_PT_controls(bpy.types.Panel):
    bl_label = "Controls"
    bl_idname = "AP_PT_controls"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Animatica"     # ported: one addon, one tab
    bl_parent_id = "AP_PT_panel"

    def draw(self, context):
        arm = _armature(context)
        lay = self.layout
        if arm is None:
            lay.label(text="no armature")
            return
        ctrls = _controls(arm)
        lay.operator("autoposer.add_control", icon="ADD")
        if not ctrls:
            lay.label(text="no controls — press Build Rig")
            return
        ETY = {0: "pos", 1: "rot", 2: "aim"}
        col = lay.column(align=True)
        for b in ctrls:
            box = col.box().column(align=True)
            row = box.row(align=True)
            row.prop(b, "ap_enabled", text="")
            row.label(text=f"{b.name}", icon="BONE_DATA")
            row.label(text=ETY.get(int(b.get('ap_ety', 0)), "?"))
            if int(b.get("ap_ety", 0)) == 0:
                row.prop(b, "ap_rot", text="", icon="ORIENTATION_GIMBAL", toggle=True)
            op = row.operator("autoposer.remove_control", text="", icon="X", emboss=False)
            op.name = b.name
            sub = box.row(align=True)
            sub.active = b.ap_enabled
            sub.prop(b, "ap_tol_m", text="tol (m)")
            if b.ap_rot:
                r2 = box.row(align=True)
                r2.active = b.ap_enabled
                r2.prop(b, "ap_rot_tol_m", text="rot tol")


CLASSES = (AP_OT_build_rig, AP_OT_add_control, AP_OT_remove_control, AP_OT_solve,
           AP_OT_snap_controls, AP_OT_rest, AP_OT_key_pose, AP_OT_take_over, AP_OT_release,
           AP_PT_panel, AP_PT_controls)


def register():
    B = bpy.types.Bone
    B.ap_joint = bpy.props.StringProperty(name="Joint", default="",
                                          description="SOMA joint this control drives")
    B.ap_enabled = bpy.props.BoolProperty(
        name="On", default=True, update=_on_enabled,
        description="Off: the control follows its joint and contributes no effector, so the poser "
                    "places that joint from its prior. On: the joint goes where the control is")
    B.ap_rot = bpy.props.BoolProperty(
        name="Rotation", default=False, update=_on_rot,
        description="Also send this joint's ORIENTATION to the poser. Switches on by itself when "
                    "you rotate the control; uncheck to hand the orientation back to the model")
    B.ap_rot_tol_m = bpy.props.FloatProperty(
        name="Rotation tolerance", default=0.005, min=0.001, max=0.5, step=0.1, precision=3,
        update=_on_tol,
        description="Slack on the orientation. The Rust IK has no rotation term, so this is the "
                    "poser's dial alone. If a rotation lands short, loosen this control's POSITION "
                    "tolerance — pinning the position fights the rotation on the same joint "
                    "(measured: 30 deg request turned 8.9 deg pinned, 15.3 deg at pos tol 0.20)")
    B.ap_tol_m = bpy.props.FloatProperty(
        name="Tolerance", default=0.005, min=0.001, max=0.5, step=0.1, precision=3,
        unit="LENGTH", update=_on_tol,
        description="Metres of slack. Tight = obey; loose = a hint the poser may overrule. Also "
                    "sets the IK weight (1/tol), so poser and solver read the same dial")
    S = bpy.types.Scene
    S.ap_armature = bpy.props.StringProperty(name="Rig", default="")
    S.ap_live = bpy.props.BoolProperty(name="Live", default=False, update=_set_live)
    S.ap_rate = bpy.props.IntProperty(name="Max Hz", default=60, min=5, max=120)
    S.ap_use_ik = bpy.props.BoolProperty(name="IK refine", default=True)
    S.ap_floor = bpy.props.BoolProperty(
        name="Floor", default=True,
        description="The floor is solid: no joint ends up below it. Whatever the solver leaves "
                    "underground is lifted out by turning bones about joints that stay put — a "
                    "knee swings about the line through its hip and ankle, anything else rolls "
                    "about the joint above it — so pinned controls are not spent doing it. A "
                    "control you pin BELOW the floor is overruled")
    S.ap_hide_deform = bpy.props.BoolProperty(
        name="Hide skeleton", default=True, update=_on_hide_deform,
        description="Hide the deform bones so only the controls and the character are visible. "
                    "Display only — the poser drives them either way")
    S.ap_status = bpy.props.StringProperty(name="Status", default="")
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    global _TIMER_ON
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    _TIMER_ON = False
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    for p in ("ap_armature", "ap_live", "ap_rate", "ap_use_ik", "ap_status",
              "ap_hide_deform", "ap_floor"):
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)
    for p in ("ap_joint", "ap_enabled", "ap_tol_m", "ap_rot", "ap_rot_tol_m"):
        if hasattr(bpy.types.Bone, p):
            delattr(bpy.types.Bone, p)


