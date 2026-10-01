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
  * frames - Blender is Z-up, the poser Y-up: poser = (x, z, -y). M = R_x(-90 deg). On any other
             rig M is extended by that rig's up axis, facing and unit (`_frame`), and each bone's
             rest by its turn onto the canonical rest pose (`_rest_fix`) — both identity here.
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
import numpy as np

from . import engine, joint_map

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

#: Called after a successful solve, with the context. Animatica sets this to
#: key the solved pose at the current frame — which is what makes detaching
#: the action unnecessary: the pose lives in the action, so the next animation
#: evaluation reproduces it instead of overwriting it.
AFTER_SOLVE = None

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
    """The rig being posed — the one Animatica generates for.

    This carried its own picker when it was an addon of its own, which made it
    possible to pose one character and generate another, and to wonder why
    editing a key pose did nothing. There is one character in this app, chosen
    once: the target armature. ``scene.ap_armature`` is kept as a mirror of it
    so the rest of this module reads unchanged, and as the fallback for a
    scene that has no Animatica settings at all.
    """
    settings = getattr(context.scene, "animatica", None)
    target = getattr(settings, "target_armature", None) if settings else None
    try:
        if target is not None and target.type == "ARMATURE":
            return target
    except ReferenceError:
        pass                                # deleted out from under the pointer
    ob = bpy.data.objects.get(context.scene.ap_armature)
    return ob if (ob is not None and ob.type == "ARMATURE") else None


def has_controls(arm) -> bool:
    """Whether this rig has been given the control bones the poser drives."""
    return arm is not None and any(_is_ctrl(b.bone) for b in arm.pose.bones)


def _is_ctrl(b):
    return bool(getattr(b, "ap_joint", ""))


def joint_pose_bone(arm, joint: str):
    """The PoseBone driving a canonical joint, or None."""
    b = joint_bone(arm, joint)
    return arm.pose.bones.get(b.name) if b is not None else None


def joint_bone(arm, joint: str):
    """The armature bone driving a canonical joint, or None — through the rig's joint map, so
    ``LeftShin`` is ``mixamorig:LeftLeg`` on a Mixamo rig and ``calf_l`` on an Unreal one."""
    name = joint_map_of(arm).get(joint)
    return arm.data.bones.get(name) if name else None


def canonical_joint(arm, bone_name: str) -> str:
    """The canonical joint a bone of this rig plays, or the bone's own bare name."""
    for j, bn in joint_map_of(arm).items():
        if bn == bone_name:
            return j
    return bone_name.rsplit(":", 1)[-1]


def joint_names(arm):
    """The canonical joints this rig has a bone for, in SOMA order — the poser's own order,
    which is what the descriptor and the returned pose are indexed by. A rig with MORE bones
    (fingers, toe ends, a head end) is fine: the extra ones are simply not driven."""
    names = engine.meta().get("joint_names") or joint_map.CANON
    jm = joint_map_of(arm)
    return [(n, jm[n]) for n in names if n in jm and jm[n] in arm.data.bones]


# --------------------------------------------------------------------------- any skeleton
#
# Three things differ between the canonical rig and whatever the artist brought, and each is
# settled once per rig and cached:
#   * WHICH BONE is which joint — ``joint_map`` finds it from the skeleton's structure, and the
#     artist can correct it (``Object.ap_joints``);
#   * WHICH WAY IS UP, how big a unit is, and which way the rig faces — an FBX import is often
#     Y-up in centimetres, with the conversion held in an object or parent transform the poser
#     never sees. ``_frame`` folds all three into the armature -> poser matrix;
#   * the REST POSE — the poser's rotations are relative to its own T-pose, and an A-pose arm
#     given those rotations points 45 degrees off. ``_rest_fix`` is, per joint, the rotation
#     that turns the rig's rest bone onto the canonical one.
# On the canonical rig all three are the identity, so nothing it was verified on moves.

#: detected maps and frames, keyed by armature — detection walks every bone, a solve must not
_MAP_CACHE = {}
_STORING = False
#: A rest bone within this of the canonical direction is taken as the same: no correction, so a
#: canonical rig is posed exactly as it always was.
REST_FIX_MIN_DEG = 1.0
#: The main child each joint points at, where it is not simply its first canonical child. The
#: head has no child along it; a hand's only children are SOMA's finger markers, which a real
#: rig either lacks or has as the ends of whole finger chains. Both take the correction of the
#: joint above: an A-pose hand is turned exactly as far as its forearm.
_PRIMARY_CHILD = {"Head": None, "LeftHand": None, "RightHand": None}


def _cache_key(arm):
    try:
        return (arm.as_pointer(), arm.data.name, len(arm.data.bones))
    except ReferenceError:
        return None


def forget_map(arm=None) -> None:
    """Drop what was worked out about a rig (all rigs, with no argument) — after the map is
    edited, or bones are added or removed."""
    if arm is None:
        _MAP_CACHE.clear()
        return
    key = _cache_key(arm)
    for k in [k for k in _MAP_CACHE if k[1:4] == key]:
        del _MAP_CACHE[k]
    try:
        if "ap_driven" in arm:
            del arm["ap_driven"]
    except (ReferenceError, TypeError):
        pass


def _detected(arm):
    """``(map, up)`` as detection finds them, cached."""
    key = _cache_key(arm)
    hit = _MAP_CACHE.get(("detect",) + key)
    if hit is None:
        hit = joint_map.detect_frame(joint_map.from_armature(arm))
        _MAP_CACHE[("detect",) + key] = hit
    return hit


def joint_map_of(arm) -> dict:
    """``{canonical joint: bone name}`` for this rig — the stored map if the rig has one (Build
    Rig stores it, and the artist may have corrected it), else what detection finds."""
    if arm is None:
        return {}
    stored = getattr(arm, "ap_joints", None)
    if stored is not None and len(stored):
        return {it.joint: it.bone for it in stored if it.bone and it.bone in arm.data.bones}
    return _detected(arm)[0]


def store_map(arm, mapping=None) -> None:
    """Write a map onto the rig, where the artist can see and correct it."""
    global _STORING
    mapping = _detected(arm)[0] if mapping is None else mapping
    up = _detected(arm)[1]
    _STORING = True                 # one invalidation for the whole map, not one per row
    try:
        arm.ap_joints.clear()
        for j in joint_map.CANON:          # markers too: kept, just not shown
            it = arm.ap_joints.add()
            it.joint = j
            it.bone = mapping.get(j, "")
    finally:
        _STORING = False
    arm["ap_up"] = joint_map.UPS.index(up) if up in joint_map.UPS else 0
    forget_map(arm)


def _up(arm):
    idx = arm.get("ap_up")
    if idx is not None and 0 <= int(idx) < len(joint_map.UPS):
        return joint_map.UPS[int(idx)]
    return _detected(arm)[1]


#: A character outside this height (metres) is taken to be in other units: an FBX in
#: centimetres at scale 1 reads as a 170 m giant, and is measured as a 1.7 m person instead.
PLAUSIBLE_HEIGHT = (0.4, 3.0)
DEFAULT_HEIGHT = 1.7


def _frame(arm):
    """``(A, A_inv, R)``: armature space -> the poser's frame, for positions (A, which carries
    the unit scale) and for rotations (R, orthonormal). ``M`` on the canonical rig."""
    try:
        sw = sum(arm.matrix_world.to_scale()) / 3.0
    except (AttributeError, ReferenceError):
        sw = 1.0
    key = ("frame",) + (_cache_key(arm) or ()) + (round(sw, 6), arm.get("ap_up"))
    hit = _MAP_CACHE.get(key)
    if hit is not None:
        return hit
    U = mathutils.Matrix(_up(arm))
    heads = [U @ b.head_local for b in arm.data.bones if not _is_ctrl(b)]
    # facing: the rig's left must be on +X once it is stood up, or the poser poses it backwards
    yaw = mathutils.Matrix.Identity(3)
    jm = joint_map_of(arm)
    for left, right in (("LeftArm", "RightArm"), ("LeftLeg", "RightLeg")):
        lb, rb = arm.data.bones.get(jm.get(left, "")), arm.data.bones.get(jm.get(right, ""))
        if lb is None or rb is None:
            continue
        lat = U @ (lb.head_local - rb.head_local)
        ang = math.atan2(lat.y, lat.x)
        if abs(math.degrees(ang)) > REST_FIX_MIN_DEG:
            yaw = mathutils.Matrix.Rotation(-ang, 3, "Z")
        break
    F = yaw @ U
    s = sw
    if heads:
        tall = max(h.z for h in heads) - min(h.z for h in heads)
        if tall > 1e-6 and not (PLAUSIBLE_HEIGHT[0] <= tall * sw <= PLAUSIBLE_HEIGHT[1]):
            s = DEFAULT_HEIGHT / tall
    R = M @ F
    A = R * s
    hit = (A, A.inverted(), R)
    _MAP_CACHE[key] = hit
    return hit


def unit_scale(arm) -> float:
    """Metres per armature unit, as the poser reads this rig."""
    A = _frame(arm)[0]
    return A.col[0].length


def to_poser(arm, v):
    """An armature-space point (or offset) in the poser's frame."""
    return _frame(arm)[0] @ mathutils.Vector(v)


def from_poser(arm, p):
    """A poser-frame point in armature space."""
    return _frame(arm)[1] @ mathutils.Vector(tuple(float(x) for x in p))


def rot_to_poser(arm, R3):
    """An armature-space rotation in the poser's frame."""
    R = _frame(arm)[2]
    return R @ R3 @ R.transposed()


def rot_from_poser(arm, R3):
    R = _frame(arm)[2]
    return R.transposed() @ R3 @ R


def _rest_fix(arm):
    """``{joint: 3x3}``: the rotation (armature space) that turns each mapped bone's REST onto
    the poser's canonical rest. Identity where they already agree."""
    key = ("restfix",) + (_cache_key(arm) or ())
    hit = _MAP_CACHE.get(key)
    if hit is not None:
        return hit
    meta = engine.meta()
    names = meta.get("joint_names") or list(joint_map.CANON)
    parents = meta.get("parents") or []
    neutral = meta.get("neutral_joints") or []
    jm = joint_map_of(arm)
    R = _frame(arm)[2]
    RT = R.transposed()
    kids = {n: [] for n in names}
    for i, n in enumerate(names):
        p = parents[i] if i < len(parents) else -1
        if 0 <= p != i:
            kids[names[p]].append(n)
    heads = {j: arm.data.bones[bn].head_local for j, bn in jm.items() if bn in arm.data.bones}
    out = {}
    I3 = mathutils.Matrix.Identity(3)
    for i, j in enumerate(names):
        p = parents[i] if i < len(parents) else -1
        inherit = out.get(names[p], I3) if 0 <= p != i else I3
        if j not in heads or not neutral:
            out[j] = inherit
            continue
        c = _PRIMARY_CHILD.get(j, kids[j][0] if kids[j] else None)
        rig_dir = heads[c] - heads[j] if c is not None and c in heads else None
        if rig_dir is None or rig_dir.length < 1e-8:
            out[j] = inherit
            continue
        ci = names.index(c)
        canon = RT @ mathutils.Vector([neutral[ci][k] - neutral[i][k] for k in range(3)])
        if canon.length < 1e-8:
            out[j] = inherit
            continue
        q = rig_dir.rotation_difference(canon)
        out[j] = q.to_matrix() if math.degrees(q.angle) > REST_FIX_MIN_DEG else I3
    _MAP_CACHE[key] = out
    return out


def _rest3(arm, joint, bone=None):
    """The joint's rest orientation as the poser means it: the bone's own rest, turned onto the
    canonical rest pose. What every rotation to or from the poser is taken relative to."""
    b = bone if bone is not None else joint_bone(arm, joint)
    rest = b.matrix_local.to_3x3() if b is not None else mathutils.Matrix.Identity(3)
    return _rest_fix(arm).get(joint, mathutils.Matrix.Identity(3)) @ rest


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


def driven_joints(arm, *, fresh: bool = False):
    """The joints this rig lets us WRITE, in joint order: mapped, anatomically the same joint,
    and hanging below the joint above them.

    A mapped bone is not enough. Three things disqualify a joint, and each of them showed up on
    a real rig before this existed: it is one of SOMA's hand markers (see `MARKER_JOINTS`); its
    bone does not hang below the bone driving its canonical parent, so writing it detaches it
    from the chain; or its rest offset, once the rest pose is corrected, disagrees with the
    poser's by more than a build difference could explain — a joint mapped to the wrong bone.

    Bones BETWEEN two driven joints are fine: a rig with four spine bones, or a twist bone
    inside the chain, has them held at rest while the joints either side are posed (see
    `chain_bones`).
    """
    # only trusted alongside a stored map: a rig built before maps were stored cached the
    # joints its bone NAMES allowed, which on a Mixamo rig was seven of them
    cached = None if fresh or not len(arm.ap_joints) else arm.get("ap_driven")
    if cached:
        return [(j, bn) for j, bn in joint_names(arm) if j in set(cached)]
    meta = engine.meta()
    names, parents = meta.get("joint_names") or [], meta.get("parents") or []
    rest = engine.skeleton().rest_joints(_bone_lengths(arm))
    fix = _rest_fix(arm)
    out, index = [], {n: i for i, n in enumerate(names)}
    for j, bn in joint_names(arm):
        i = index[j]
        p = parents[i] if i < len(parents) else -1
        if p < 0 or p == i:
            out.append((j, bn))                       # the root is always ours to place
            continue
        if j in MARKER_JOINTS:
            continue
        # the nearest canonical ancestor this rig has: a rig without Spine2 hangs its chest
        # off Spine1, and the chest is still a joint to drive
        while p >= 0 and joint_bone(arm, names[p]) is None:
            p = parents[p] if p < len(parents) else -1
        if p < 0:
            continue
        pb = joint_bone(arm, names[p])
        b = arm.data.bones[bn]
        if pb.name not in {a.name for a in b.parent_recursive}:
            continue                                  # not below its parent joint at all
        off = fix.get(names[p], mathutils.Matrix.Identity(3)) @ (
            b.matrix_local.translation - pb.matrix_local.translation)
        rig = to_poser(arm, off)
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


#: what the artist is told when the Autoposer cannot work on a rig, instead of a traceback
NO_MODEL = "The Autoposer model isn't downloaded yet — see Preferences > Add-ons > Animatica"
UNSUPPORTED = "The Autoposer couldn't find a humanoid body on this rig"
CONTROL_RIG = ("This rig's bones are driven by a control rig, which the Autoposer can't pose "
               "yet — pose its deform skeleton instead")
LINKED = ("This rig is linked from another file, so its controls can't be added or removed "
          "here (make it local first)")


def edit_problem(arm):
    """Why control bones cannot be added to or removed from *arm* here, or None: they need
    Edit Mode, which a rig linked from another file (or a library override of one) does not
    allow -- Build Rig stopped with a raw mode_set error half way through."""
    if arm is None:
        return None
    for idb in (arm, arm.data):
        if idb is None:
            continue
        if idb.library is not None or getattr(idb, "override_library", None) is not None:
            return LINKED
    return None


def rig_problem(arm):
    """Why the Autoposer cannot pose *arm*, in a sentence for the artist, or None.

    Asked before anything is changed on the rig: a Build Rig that failed half way used to hide
    the whole skeleton, and leave it hidden."""
    if arm is None:
        return "Pick the character to pose first"
    if not engine.meta():
        return NO_MODEL
    missing = joint_map.missing_core(joint_map_of(arm))
    if len(missing) == len(joint_map.CORE):
        return UNSUPPORTED
    if missing:
        names = ", ".join(control_label(j) for j in missing[:4])
        more = f" and {len(missing) - 4} more" if len(missing) > 4 else ""
        it = "it" if len(missing) == 1 else "them"
        return f"Couldn't find the {names}{more} on this rig — set {it} under Skeleton"
    if _constrained(arm):
        return CONTROL_RIG
    try:
        engine.skeleton()
    except engine.NotReady:
        return NO_MODEL
    return None


def _constrained(arm) -> bool:
    """Whether the mapped bones are driven by constraints — a control rig, whose deform bones
    ignore anything written to them. Posing it means posing its controls, which this does not
    do; the question is asked so the answer is a sentence rather than a rig that will not move."""
    for _j, bn in joint_names(arm):
        pb = arm.pose.bones.get(bn)
        if pb is None:
            continue
        for c in pb.constraints:
            if c.mute or c.influence <= 0.0:
                continue
            if c.type in {"COPY_TRANSFORMS", "COPY_ROTATION", "CHILD_OF", "ARMATURE",
                          "DAMPED_TRACK", "IK", "STRETCH_TO"}:
                return True
    return False


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

    A rig that lacks a joint still has the body around it. Where joints are missing between
    two it has (one neck bone for SOMA's two, two spine bones for three), the measured span is
    shared out in the proportions of the average body; a joint with nothing below it (a jaw,
    the finger markers, a toe) gets the average body's length, scaled to this one. Zeros, as
    this sent before, describe a body with no neck.

    A length is measured ALONG the canonical bone: the poser can only lay a bone out in its
    own direction, and a rig whose shoulders start up by the neck, 30 cm from the chest but
    only 13 cm out to the side, described by the full 30 cm gets a body with its shoulders
    30 cm out — and a spine bent double to bring the hands back in. On the canonical rig the
    two directions agree and this is the plain distance.
    """
    meta = engine.meta()
    parents = meta.get("parents") or []
    order = meta.get("joint_names") or []
    mean = list(meta.get("blen_mean") or [])
    canon = {j: bn for j, bn in joint_names(arm)}
    s = unit_scale(arm)
    heads = {j: arm.data.bones[bn].matrix_local.translation for j, bn in canon.items()}
    neutral = meta.get("neutral_joints") or []
    fix = _rest_fix(arm)
    slot = {}                                   # joint index -> position in the descriptor
    for i in range(len(order)):
        p = parents[i] if i < len(parents) else -1
        if 0 <= p != i:
            slot[i] = len(slot)
    out = [None] * len(slot)

    def avg(i):
        k = slot[i]
        return float(mean[k]) if k < len(mean) else 0.0

    for i, j in enumerate(order):
        if i not in slot or j not in heads:
            continue
        span, p = [i], parents[i]
        while p >= 0 and order[p] not in heads:
            if p in slot:
                span.append(p)
            p = parents[p] if p < len(parents) else -1
        if p < 0:
            continue
        off = heads[j] - heads[order[p]]
        d = off.length * s
        if len(span) == 1 and neutral and d > 0:
            rig_dir = to_poser(arm, fix.get(order[p], mathutils.Matrix.Identity(3)) @ off)
            canon_dir = mathutils.Vector([neutral[i][k] - neutral[p][k] for k in range(3)])
            if canon_dir.length > 1e-8 and rig_dir.length > 1e-8 and \
                    math.degrees(rig_dir.angle(canon_dir)) > REST_FIX_MIN_DEG:
                # never below a third: a joint mapped far off its bone is not a stub
                d = max(rig_dir.dot(canon_dir.normalized()), d / 3.0)
        total = sum(avg(k) for k in span)
        for k in span:
            out[slot[k]] = d * (avg(k) / total if total > 0 else 1.0 / len(span))
    known = [(out[slot[i]], avg(i)) for i in slot if out[slot[i]] is not None and avg(i) > 0]
    ratios = sorted(v / m for v, m in known if v > 0)
    ratio = ratios[len(ratios) // 2] if ratios else 1.0
    joint_at = {k: i for i, k in slot.items()}
    out = [v if v is not None else avg(joint_at[k]) * ratio for k, v in enumerate(out)]
    if _canonical_build(arm):
        return out
    return _in_distribution(out, {order[i]: k for i, k in slot.items()}, meta)


#: Spans whose TOTAL a rig gives reliably but whose split between joints is a rig convention:
#: Mixamo starts its spine 10 cm above the hips where SOMA starts it 5 cm up, and hangs its
#: arms off a clavicle half as long. Measured joint by joint these read to the poser as a body
#: 23 standard deviations from anything it was trained on, and its answer jumps about; the
#: total, shared out as the average body shares it, is the same torso in the poser's terms.
_CONVENTION_SPANS = (("Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head"),
                     ("LeftShoulder", "LeftArm"), ("RightShoulder", "RightArm"))
#: ...and no dimension further out than this, in standard deviations of the training bodies
BODY_Z_MAX = 2.5


def _canonical_build(arm) -> bool:
    """Whether the rig is laid out like the canonical skeleton: every bone at the canonical rest
    direction. Such a rig's descriptor is measured, not reinterpreted."""
    return all(m == mathutils.Matrix.Identity(3) for m in _rest_fix(arm).values())


def _in_distribution(lengths, slot_of, meta):
    """The descriptor of a rig built to another convention, restated in the poser's terms: its
    convention-dependent spans re-split like the average body's, then every dimension held
    within `BODY_Z_MAX` of the training population."""
    mean = [float(v) for v in (meta.get("blen_mean") or [])]
    std = [float(v) for v in (meta.get("blen_std") or [])]
    if len(mean) != len(lengths) or len(std) != len(lengths):
        return lengths
    out = list(lengths)
    for span in _CONVENTION_SPANS:
        ks = [slot_of[j] for j in span if j in slot_of]
        total, share = sum(out[k] for k in ks), sum(mean[k] for k in ks)
        if total > 0 and share > 0:
            for k in ks:
                out[k] = total * mean[k] / share
    deg = float(meta.get("cond_degenerate_std", 1e-3))
    for k, v in enumerate(out):
        if std[k] >= deg:
            out[k] = min(max(v, mean[k] - BODY_Z_MAX * std[k]), mean[k] + BODY_Z_MAX * std[k])
    return out


def chain_bones(arm):
    """Bones that sit BETWEEN two driven joints — a third spine bone, a twist bone inside an
    arm chain. Held at rest (identity basis) while the joints either side are posed, so the
    chain reads through them; `pose_bases` writes them."""
    driven = {bn for _j, bn in driven_joints(arm)}
    out = []
    for bn in driven:
        b = arm.data.bones[bn].parent
        between = []
        while b is not None and b.name not in driven:
            between.append(b.name)
            b = b.parent
        if b is not None:
            out.extend(n for n in between if n not in out)
    return out


def _hands(arm):
    """``{control: transform}`` for every ENABLED control — where the handles are.

    ORIENTATION IS ALWAYS PART OF THIS. Rotating a control has to wake the live timer — that is
    what auto-latches its rotation channel on. Tracking orientation only for non-position controls
    (as this did) makes rotation work on demand but never live, which reads as "rotation is broken"
    even though every other layer is correct.
    """
    out = {}
    for b in _controls(arm):
        if not b.ap_enabled:
            continue
        pb = arm.pose.bones.get(b.name)
        if pb is None:
            continue
        m = pb.matrix
        out[b.name] = (tuple(round(v, 5) for v in m.translation),
                       tuple(round(v, 4) for row in m.to_3x3() for v in row))
    return out


def _dials(arm):
    """The rest of what changes a solve: which controls are on, and how tight."""
    return tuple((b.name, bool(b.ap_enabled), round(float(b.ap_tol_m), 5),
                  bool(b.ap_rot), round(float(b.ap_rot_tol_m), 5))
                 for b in _controls(arm))


def _state_key(arm):
    """Fingerprint of everything that would change the solve, in two halves.

    Split because they mean different things to the artist: the first half is where the handles
    are — something they moved — and the second is how the solve is configured. Both should
    re-solve; only the first is an edit worth keying.
    """
    return (tuple(sorted(_hands(arm).items())), _dials(arm))


def _handle_moved(before, after) -> bool:
    """Did the artist actually drag a handle between these two states?

    Only controls present in BOTH states count. Adding a control, or switching one on, makes an
    entry appear in the fingerprint without anything having moved — and keying that wrote a pose
    the artist never posed.
    """
    if not before:
        return False           # nothing to compare against: the first tick is not a drag
    was = dict(before[0])
    return any(name in was and was[name] != now for name, now in dict(after[0]).items())


def _rot_effector(arm, joint, b, tol):
    """Control rotation -> the poser's rotation effector (poser frame, first two matrix columns).

    desired_joint_orientation = animator_delta . joint_orientation_at_seat_time, then the joint's
    own REST orientation comes back out before converting frames — the same relation `_apply` uses
    in the other direction. An unrotated control gives delta = I, so the effector asks for exactly
    the orientation the joint already had: adding it changes nothing.
    """
    jb = joint_pose_bone(arm, joint)
    rest3 = _rest3(arm, joint)
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
    R3 = rot_to_poser(arm, desired @ rest3.inverted())
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
        p = to_poser(arm, m.translation)
        if ety == 2:                                    # look-at: control position IS the target
            e = {"joint": joint, "type": "lookat", "pos": [p.x, p.y, p.z]}
        elif ety == 1:
            # rotation effector = the joint's GLOBAL rotation in the poser frame, as the first two
            # matrix columns. The control carries the joint's pose matrix, so the joint's own rest
            # orientation has to come back out before converting frames — same relation as _apply.
            R3 = rot_to_poser(arm, m.to_3x3() @ _rest3(arm, b.ap_joint).inverted())
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
        d = to_poser(arm, _rest_fix(arm).get(ball, mathutils.Matrix.Identity(3))
                     @ (tip.matrix_local.translation - b.matrix_local.translation))
        out[ball] = [d.x, d.y, d.z]
    return out


def pose_bases(arm, names, pos, r6, pins=None):
    """The solve as {bone name: matrix_basis}, without writing anything.

    Split out of `_apply` so a solve can also be keyed at a frame the rig is
    not standing on — the whole computation reads rest data (`matrix_local`)
    and the solve, never the current pose, so it holds at any frame. Animatica
    drives it from the motion-curve drag, which solves for one frame while the
    playhead stays where the artist left it.

    Only `driven_joints` are written: a bone whose name matches a canonical joint but whose
    chain says otherwise is left alone, because forcing it detaches it from the bones above it
    and the skin tears at exactly that seam.

    Each joint is given its solved ORIENTATION; only the root is given its solved position.
    Every other joint lands where its parent chain puts it — which is where the poser put it
    on a rig of the canonical build, and on any other rig is the only position a key of its
    rotations can reproduce. Writing solved positions onto disconnected bones (every FBX
    import) would show one pose live and key another.

    ``pins`` — ``{hand or foot joint: armature-space position}`` — are the handles the artist
    pinned. On a rig built differently from the canonical one, the chain above a hand does not
    land exactly where the poser's did (its shoulders branch off the chest at another angle),
    so the limb is finished with a two-bone fit onto the pin. The canonical rig skips this and
    is posed exactly as it always was.

    Bones between two driven joints (`chain_bones`) are held at identity, so the joints below
    them can be placed through them.
    """
    drivable = {j for j, _bn in driven_joints(arm)}
    root_joint = (engine.meta().get("joint_names") or ["Hips"])[0]
    rots, root_pos = {}, None
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
        rots[pb.name] = rot_from_poser(arm, R_ps) @ _rest3(arm, nm, pb.bone)
        if nm == root_joint:
            root_pos = (pb.name, from_poser(arm, pos[i]))
    posed, bases = _chain_pose(arm, rots, root_pos)
    if pins and needs_fit(arm):
        for joint, target in pins.items():
            _fit_limb(arm, joint, mathutils.Vector(target), rots, posed)
        posed, bases = _chain_pose(arm, rots, root_pos)
    return bases


def _chain_pose(arm, rots, root_pos):
    """World orientations -> ({bone: armature-space pose}, {bone: matrix_basis}), parents first:
    a joint is placed through the pose of the joint above it."""
    I4 = mathutils.Matrix.Identity(4)
    bases, posed = {}, {}
    for nm in sorted(rots, key=lambda n: len(arm.data.bones[n].parent_recursive)):
        b = arm.data.bones[nm]
        anc, between = b.parent, []
        while anc is not None and anc.name not in rots:
            between.append(anc.name)
            anc = anc.parent
        if anc is not None:
            base = posed[anc.name] @ (anc.matrix_local.inverted() @ b.matrix_local)
            for n in between:
                bases[n] = I4.copy()
        else:
            base = b.matrix_local.copy()
        target = rots[nm].to_4x4()
        target.translation = (root_pos[1] if root_pos and root_pos[0] == nm
                              else base.translation)
        posed[nm] = target
        bases[nm] = base.inverted() @ target
    return posed, bases


#: the two-bone limbs a pinned hand or foot is fitted through: (upper, lower, end)
LIMBS = {"LeftHand": ("LeftArm", "LeftForeArm"), "RightHand": ("RightArm", "RightForeArm"),
         "LeftFoot": ("LeftLeg", "LeftShin"), "RightFoot": ("RightLeg", "RightShin")}


def needs_fit(arm) -> bool:
    """Whether this rig is built differently enough from the canonical one that a pinned limb
    has to be finished on the rig itself (see `pose_bases`)."""
    return bool(chain_bones(arm)) or any(
        m != mathutils.Matrix.Identity(3) for m in _rest_fix(arm).values())


def _fit_limb(arm, joint, target, rots, posed):
    """Turn a limb's upper and lower bones so its end reaches *target*, keeping the plane it
    bends in and the end's own orientation. Changes ``rots`` in place."""
    if joint not in LIMBS:
        return
    ua, la = (joint_bone(arm, j) for j in LIMBS[joint])
    end = joint_bone(arm, joint)
    if ua is None or la is None or end is None or not {ua.name, la.name, end.name} <= set(posed):
        return
    A = posed[ua.name].translation
    B = posed[la.name].translation
    C = posed[end.name].translation
    l1, l2 = (B - A).length, (C - B).length
    to_t = target - A
    d = to_t.length
    if l1 < 1e-8 or l2 < 1e-8 or d < 1e-8 or (target - C).length < 1e-6:
        return
    d = max(abs(l1 - l2) + 1e-5, min(l1 + l2 - 1e-5, d))
    u = to_t.normalized()
    # the bend: where the elbow or knee points now, off the line from the shoulder to the hand
    pole = (B - A) - (B - A).dot(u) * u
    if pole.length < 1e-6:
        pole = (C - A).cross(u).cross(u) if (C - A).cross(u).length > 1e-6 else u.orthogonal()
    v = pole.normalized()
    cos_a = max(-1.0, min(1.0, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d)))
    B2 = A + l1 * (cos_a * u + math.sqrt(max(0.0, 1 - cos_a * cos_a)) * v)
    Ra = (B - A).rotation_difference(B2 - A).to_matrix()
    C_turned = B2 + Ra @ (C - B)
    Rb = (C_turned - B2).rotation_difference(A + u * d - B2).to_matrix()
    # world orientations: the end keeps the one the poser gave it
    for n, r in ((ua.name, Ra), (la.name, Rb @ Ra)):
        rots[n] = r @ rots[n]


def _apply(arm, names, pos, r6, pins=None):
    """Write the solve onto the rig, one depsgraph update for the whole body."""
    for nm, basis in pose_bases(arm, names, pos, r6, pins).items():
        arm.pose.bones[nm].matrix_basis = basis


#: A handle looser than this is a hint to the poser, not a pin to be met on the rig.
PIN_TOL = 0.02                  # metres


def _pins(arm, eff, out, floor):
    """The hands and feet the artist pinned, in armature space, for `pose_bases` to finish.

    With the floor on, a pinned foot never goes lower than the poser left it: the floor pass
    may have lifted it, and pulling it back down would put the toes under the floor it keeps.
    """
    names = list(out.get("names") or [])
    pins = {}
    for e in eff:
        if e.get("type") != "pos" or e["joint"] not in LIMBS or float(e.get("tol", 1)) > PIN_TOL:
            continue
        p = list(e["pos"])
        if floor and e["joint"].endswith("Foot") and e["joint"] in names:
            p[1] = max(p[1], float(out["joints"][names.index(e["joint"])][1]))
        pins[e["joint"]] = from_poser(arm, p)
    return pins


def floor_height(arm, eff) -> float:
    """The floor under the pose, in the poser's frame (its Y).

    The poser's floor is Y=0 of its frame -- the armature's base plane. A
    character posed on a roof 0.6 m below that, or on a stair, had its feet
    held at the base plane in the air. The ground under it is read from the
    scene instead: a ray down at the hips (or the middle of the targets),
    from just above them, past the character's own meshes -- not from above
    the head, where a hat or a ledge being reached for is not the floor.
    """
    from .. import ground

    pos = [e for e in eff if e.get("type") == "pos"]
    if not pos or arm is None:
        return 0.0
    mw = arm.matrix_world
    world = {e["joint"]: mw @ from_poser(arm, e["pos"]) for e in pos}
    at = world.get("Hips")
    if at is None:
        at = sum(world.values(), mathutils.Vector()) / len(world)
        at.z = min(v.z for v in world.values()) + 1.0      # about hip height over the lowest target
    z = ground.surface_below_cached(bpy.context.scene, at.x, at.y, at.z + ground.RAY_HEADROOM, arm=arm)
    if z is None:
        return 0.0
    return float(to_poser(arm, mw.inverted() @ mathutils.Vector((at.x, at.y, z))).y)


def pose_on_ground(eng, arm, eff, **kw):
    """``eng.pose`` with its floor on the ground under the pose (see
    `floor_height`): the targets go down by it, the solve comes back up."""
    lift = floor_height(arm, eff) if kw.get("floor", True) else 0.0
    if abs(lift) < 1e-4:
        return eng.pose(eff, **kw)
    moved = []
    for e in eff:
        if "pos" in e:
            e = dict(e, pos=[e["pos"][0], e["pos"][1] - lift, e["pos"][2]])
        moved.append(e)
    out = dict(eng.pose(moved, **kw))
    up = np.array([0.0, lift, 0.0], dtype=np.float32)
    out["joints"] = np.asarray(out["joints"], dtype=np.float32) + up
    out["root"] = np.asarray(out["root"], dtype=np.float32) + up
    out["floor_m"] = lift
    return out


def solve(context, report=None, *, moved: bool = False):
    """Solve and apply one pose.

    ``moved`` says the solve answers a handle the artist dragged. It is what tells the auto-key
    apart from every other reason to solve — a tolerance nudged, a handle switched on, a control
    added, the rig rebuilt — none of which should write a keyframe on their own.
    """
    global _BUSY
    if _BUSY:
        return False
    arm = _armature(context)
    if arm is None:
        if report:
            report({"ERROR"}, "Pick the character to pose first")
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
        out = pose_on_ground(eng, arm, eff, bone_lengths=_bone_lengths(arm),
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
        _apply(arm, out["names"], out["joints"], out["rotations_6d"],
               _pins(arm, eff, out, floor))
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
    if AFTER_SOLVE is not None:
        try:
            AFTER_SOLVE(context, moved=moved)
        except Exception as exc:                              # noqa: BLE001
            print(f"[Animatica] after-solve hook failed: {exc}")
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
def editing_now(ctx, arm) -> bool:
    """Whether the artist is posing this rig right now.

    Live solving belongs to an edit and nothing else. Outside one it is a
    timer waiting to mistake something for a drag: the playhead moving
    re-seats the controls, a generation samples frame by frame, and either
    would read as "a control moved" and solve — and, now that a solve is
    keyed, write a pose nobody asked for.
    """
    screen = getattr(ctx, "screen", None)
    if screen is not None and getattr(screen, "is_animation_playing", False):
        return False
    settings = getattr(getattr(ctx, "scene", None), "animatica", None)
    if settings is not None and getattr(settings, "is_generating", False):
        return False
    if ctx.mode != "POSE":
        return False
    return ctx.view_layer.objects.active is arm


def sync_state(arm) -> None:
    """Take the controls' current positions as the baseline.

    Called after anything that moves the controls without the artist doing it
    — re-seating them on a frame change, above all. Without this the next tick
    sees a changed fingerprint and solves, which is the difference between
    scrubbing and posing.
    """
    global _LAST_KEY
    _LAST_KEY = _state_key(arm)


def _tick():
    global _LAST_KEY
    ctx = bpy.context
    scene = getattr(ctx, "scene", None)
    if scene is None or not getattr(scene, "ap_live", False):
        return None
    arm = _armature(ctx)
    if arm is None:
        return 0.2
    if not editing_now(ctx, arm):
        # Keep the baseline current while not editing, so returning to the rig
        # does not read every frame scrubbed through as one enormous drag.
        _LAST_KEY = _state_key(arm)
        return 1.0 / max(scene.ap_rate, 1)
    key = _state_key(arm)
    if key != _LAST_KEY:
        moved = _handle_moved(_LAST_KEY, key)
        solve(ctx, moved=moved)
        # The baseline is taken AFTER the solve, not before it: solving moves things itself —
        # disabled controls are put back on their joints, rotation references re-anchor — and
        # reading that as the artist's next drag made the timer answer its own last answer.
        _LAST_KEY = _state_key(arm)
    return 1.0 / max(scene.ap_rate, 1)


def ensure_timer(scene=None) -> bool:
    """Start the live timer if the scene says live and nothing is running.

    ``ap_live`` is a scene property and its update callback only fires when it
    *changes*. A file load or an addon reload therefore leaves it True with no
    timer behind it — live, and dead, with no way to tell from the UI. This is
    how it gets restarted.
    """
    global _TIMER_ON, _LAST_KEY
    scene = scene or getattr(bpy.context, "scene", None)
    if scene is None or not getattr(scene, "ap_live", False):
        return False
    if bpy.app.timers.is_registered(_tick):
        _TIMER_ON = True
        return True
    _LAST_KEY = None
    bpy.app.timers.register(_tick, persistent=True)
    _TIMER_ON = True
    return True


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
        p = p + _forward(arm) * AIM_OFFSET
    return p


def _forward(arm):
    """One metre straight ahead of the character at rest, in armature units — -Y on the
    canonical rig, whatever the rig's own frame on any other."""
    return from_poser(arm, (0.0, 0.0, 1.0))


def _preserve_pose(arm):
    """Snapshot the deform bones' matrix_basis before a STRUCTURAL edit.

    Adding or deleting a control needs Edit Mode, and that round-trip can perturb the evaluated
    pose (measured: a joint moving 18 cm while nothing was solved). Restoring the basis afterwards
    reproduces the pose exactly — verified 0.0001 cm — so a structural edit is guaranteed not to
    move the character, which is the whole contract of add/remove.
    """
    names = [b.name for b in _deform_bones(arm)] + chain_bones(arm)
    return {n: arm.pose.bones[n].matrix_basis.copy() for n in names if arm.pose.bones.get(n)}


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
    unit = 1.0 / unit_scale(arm)                # a metre, in this rig's units
    if spec["ety"] == 2:
        rest_head = rest_head + _forward(arm) * AIM_OFFSET
    prev = arm.mode
    context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        eb = arm.data.edit_bones
        b = eb.get(name) or eb.new(name)
        b.head = rest_head
        b.tail = rest_head + _frame(arm)[2].transposed() @ mathutils.Vector((0.0, 0.08 * unit, 0.0))
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
    s = SHAPE_SCALE.get(spec["shape"], 1.0) * 0.10 * unit
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


def _aim_dir(arm, joint):
    """Armature-space direction of the poser's LOCAL aim axis for this joint.

    The poser aims its local +z (poser frame). Pushing that through the apply relation
    M_bone = R^T . R_ps . R . rest  gives  dir = (pose3 . rest3^-1) . (R^T . +z_poser), which
    on the canonical rig (R = M) is (0,-1,0). Using the bone's own local +z instead would aim
    the wrong axis.
    """
    joint_pb = joint_pose_bone(arm, joint)
    pose3 = joint_pb.matrix.to_3x3()
    fwd = _frame(arm)[2].transposed() @ mathutils.Vector((0.0, 0.0, 1.0))
    return (pose3 @ _rest3(arm, joint).inverted()) @ fwd


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
        want.translation = (src.matrix.translation
                            + (AIM_OFFSET / unit_scale(arm)) * _aim_dir(arm, b.ap_joint).normalized())
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
            self.report({"ERROR"}, "Pick the character to pose first")
            return {"CANCELLED"}
        try:
            rig = rig_def(refresh=True)
        except Exception as e:
            self.report({"ERROR"}, f"cannot read /rig from the sidecar: {e}")
            return {"CANCELLED"}
        # Every check before the first change: a build that stops leaves the rig as it was.
        problem = edit_problem(arm) or rig_problem(arm)
        if problem is None:
            try:
                have = joint_names(arm)
                drivable = driven_joints(arm, fresh=True)
            except engine.NotReady:
                problem = NO_MODEL
        if problem is not None:
            self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        _show_controls_in_front(arm)           # handles inside a body are not handles
        _hide_deform_bones(arm, context.scene.ap_hide_deform)
        if not len(arm.ap_joints):
            store_map(arm, joint_map_of(arm))  # settle the map once, where it can be corrected
            drivable = driven_joints(arm, fresh=True)
        arm["ap_driven"] = [j for j, _bn in drivable]
        global _BUILDING
        _BUILDING = True                    # creating controls must not fire the toggle callbacks
        keep = _preserve_pose(arm)
        made = []
        try:
            for spec in rig:
                if not (self.all_controls or spec["name"] in DEFAULT_CONTROLS):
                    continue
                if joint_bone(arm, spec["joint"]) is None:
                    continue
                made.append(_add_control(arm, spec, context))
        finally:
            _BUILDING = False
        _restore_pose(arm, keep, context)
        _snap(arm, context)
        sync_state(arm)          # the controls moved because we built them, not because anyone posed
        skipped = len(have) - len(drivable)
        self.report({"INFO"}, f"{len(made)} controls, driving {len(drivable)} joints"
                              + (f", {skipped} mapped bones left alone" if skipped else "")
                              + " — grab them in Pose Mode")
        return {"FINISHED"}


# The handles an animator meets on a fresh rig. The taxonomy carries its own
# ``default_on``, but that is the research rig's opening set: it steers the
# torso by the chest and leaves the head out entirely. Ours goes the other
# way — the spine follows well enough on its own, while where the head sits
# and where it looks are among the first things anyone poses.
DEFAULT_CONTROLS = {
    "C_cog_CTRL",                               # hips
    "L_arm_IK_CTRL", "R_arm_IK_CTRL",           # hands
    "L_foot_IK_CTRL", "R_foot_IK_CTRL",         # feet
    "C_head_CTRL",                              # head
    "C_head_AIM_CTRL",                          # look at
}


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
        out = [(s["name"], control_label(s["joint"], s["kind"]),
                f"{s['name']} — {s['joint']}, {s['kind']}")
               for s in rig if s["name"] not in have]
        return out or [("NONE", "all controls already present", "")]

    control: bpy.props.EnumProperty(name="Control", items=_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        arm = _armature(context)
        if arm is None or self.control == "NONE":
            return {"CANCELLED"}
        problem = edit_problem(arm) or rig_problem(arm)
        if problem is not None:
            self.report({"ERROR"}, problem)
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
        sync_state(arm)          # ...and the live timer must not read the new handle as a drag
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
        problem = edit_problem(arm)
        if problem is not None:
            self.report({"ERROR"}, problem)
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
        problem = rig_problem(arm) if arm is not None else None
        if arm is None or problem is not None:
            if problem:
                self.report({"ERROR"}, problem)
            return {"CANCELLED"}
        try:
            bones = list(_deform_bones(arm))
        except engine.NotReady:
            self.report({"ERROR"}, NO_MODEL)
            return {"CANCELLED"}
        if not bones:
            self.report({"ERROR"}, UNSUPPORTED)
            return {"CANCELLED"}
        if self.key_controls:
            bones += list(_controls(arm))
        from .. import inplace
        why = inplace.read_only(arm.animation_data.action if arm.animation_data else None)
        if why:
            self.report({"ERROR"}, why)
            return {"CANCELLED"}
        f = context.scene.frame_current
        if arm.animation_data is None:
            arm.animation_data_create()
        if arm.animation_data.action is None:
            arm.animation_data.action = bpy.data.actions.new(f"{arm.name}_Autoposer")
        n = 0
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
        # Nothing is detached unless the pose can then be solved: a take-over that stops half
        # way left the rig with its animation unplugged and no poser driving it.
        problem = rig_problem(arm)
        if problem is None and not has_controls(arm):
            problem = "Build the Autoposer's controls first"
        if problem is None:
            try:
                engine.get()
            except engine.NotReady as e:
                problem = str(e)
        if problem is not None:
            self.report({"ERROR"}, problem)
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
        for n in [b.name for b in _deform_bones(arm)] + chain_bones(arm):
            arm.pose.bones[n].matrix_basis = mathutils.Matrix()
        context.view_layer.update()
        _snap(arm, context)
        sync_state(arm)          # rest is where the artist asked to be, not a pose to solve back out of
        return {"FINISHED"}


# --------------------------------------------------------------------------- UI
#
# What an animator calls a handle. The rig calls the same thing
# ``L_arm_IK_CTRL`` driving ``LeftHand``; nobody reaches for that. Where the
# joint name and the everyday word differ — a knee is driven through the shin,
# an elbow through the forearm — the everyday word wins.
_HUMAN_JOINT = {
    "Hips": "Hips",
    "Spine1": "Lower back",
    "Spine2": "Upper back",
    "Chest": "Chest",
    "Neck1": "Neck",
    "Head": "Head",
    "LeftShoulder": "L shoulder", "RightShoulder": "R shoulder",
    "LeftForeArm": "L elbow", "RightForeArm": "R elbow",
    "LeftHand": "L hand", "RightHand": "R hand",
    "LeftShin": "L knee", "RightShin": "R knee",
    "LeftFoot": "L foot", "RightFoot": "R foot",
    "LeftToeBase": "L toe", "RightToeBase": "R toe",
}


def control_label(joint: str, kind: str = "") -> str:
    """The panel name for a control on ``joint`` of ``kind``."""
    if kind == "aim":
        return "Look at"        # not the head's position — where it faces
    if kind == "root":
        return "Root"
    joint = (joint or "").rsplit(":", 1)[-1]
    if joint in _HUMAN_JOINT:
        return _HUMAN_JOINT[joint]
    for side, short in (("Left", "L "), ("Right", "R ")):
        if joint.startswith(side):
            return short + joint[len(side):].lower()
    return joint.capitalize() if joint.isupper() else joint


def joint_label(b) -> str:
    """What to call a control bone in a panel."""
    return control_label(b.get("ap_eff") or b.get("ap_joint") or b.name,
                         b.get("ap_kind") or "")


# --------------------------------------------------------------------------- the joint map
def _on_map_edit(self, context):
    """A joint was pointed at another bone: everything worked out from the old map is stale,
    and the controls re-seat on the joints they now follow."""
    if _STORING:
        return
    arm = self.id_data
    forget_map(arm)
    if has_controls(arm):
        try:
            _snap(arm, context)
            sync_state(arm)
        except Exception:                                   # noqa: BLE001
            pass


class AP_JointItem(bpy.types.PropertyGroup):
    """One row of a rig's joint map: which of its bones plays a canonical joint."""
    joint: bpy.props.StringProperty(name="Joint")
    bone: bpy.props.StringProperty(
        name="Bone", update=_on_map_edit,
        description="The bone of this rig that plays this joint. Empty: the rig has no such "
                    "joint, and the poser fills it in from its prior")


class AP_OT_detect_joints(bpy.types.Operator):
    bl_idname = "autoposer.detect_joints"
    bl_label = "Detect Joints"
    bl_description = ("Find the hips, spine, arms, legs and head on this rig again, from its "
                      "shape, replacing any joints set by hand")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        arm = _armature(context)
        if arm is None:
            return {"CANCELLED"}
        arm.ap_joints.clear()
        if "ap_up" in arm:
            del arm["ap_up"]
        forget_map(arm)
        store_map(arm)
        if has_controls(arm):
            _snap(arm, context)
            sync_state(arm)
        found = sum(1 for it in arm.ap_joints if it.bone)
        missing = joint_map.missing_core(joint_map_of(arm))
        if missing:
            self.report({"WARNING"}, f"found {found} joints; set "
                        + ", ".join(control_label(j) for j in missing) + " by hand")
        else:
            self.report({"INFO"}, f"found {found} joints")
        return {"FINISHED"}


def skeleton_summary(arm) -> str:
    """One line for the panel: how much of the body the rig was matched on."""
    jm = joint_map_of(arm)
    total = len([j for j in joint_map.CANON if j not in MARKER_JOINTS])
    got = len([j for j in jm if j not in MARKER_JOINTS])
    return f"{got} of {total} joints matched"


CLASSES = (AP_OT_build_rig, AP_OT_add_control, AP_OT_remove_control, AP_OT_solve,
           AP_OT_snap_controls, AP_OT_rest, AP_OT_key_pose, AP_OT_take_over, AP_OT_release,
           AP_OT_detect_joints,
)


def register():
    bpy.utils.register_class(AP_JointItem)
    bpy.types.Object.ap_joints = bpy.props.CollectionProperty(type=AP_JointItem)
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
    S.ap_live = bpy.props.BoolProperty(
        name="Live", default=True, update=_set_live,
        description="Solve as you drag a control. Off, a control moves nothing "
                    "until you run Solve from the search menu")
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
    S.ap_show_joints = bpy.props.BoolProperty(
        name="Skeleton", default=False,
        description="Show which bone of the rig plays each joint the Autoposer poses")
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
              "ap_hide_deform", "ap_floor", "ap_show_joints"):
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)
    for p in ("ap_joint", "ap_enabled", "ap_tol_m", "ap_rot", "ap_rot_tol_m"):
        if hasattr(bpy.types.Bone, p):
            delattr(bpy.types.Bone, p)
    if hasattr(bpy.types.Object, "ap_joints"):
        del bpy.types.Object.ap_joints
    bpy.utils.unregister_class(AP_JointItem)
    forget_map()


