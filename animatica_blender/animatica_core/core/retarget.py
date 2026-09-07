"""Skeleton-to-skeleton motion retargeting. Pure Python — no DCC, no server.

STATUS (2026-09-01): the PRODUCT no longer calls this module — retargeting
moved server-side (Max plugin commit 07f8ea5; the plugins ship the rig or the
canonical block and the server decides). What remains is GATE machinery: the
shared gates (demo, m8, m9, m11, m17) use :func:`retarget_motion` to map the
server's canonical-skeleton glTF onto the rig the gate built — which, for the
canonical rigs the gates build, is an identity mapping. A green gate is
therefore NOT evidence about any product retarget path; the server-retarget
path has no gate yet (planned as the m3 successor).

This was originally the replacement for the MotionBuilder plugin's HumanIK
route (``mobu_bridge/hik.py``, 815 lines). 3ds Max has no HumanIK and no
equivalent, so "one rig, any model" needed the transfer to happen in the data
rather than in a character system.

Doing it here rather than in a bridge has three consequences worth stating:

* it is **testable headlessly** under the existing pytest suite, which the HIK
  route never was;
* it needs **no hidden source rig** in the scene, so the build/characterise/
  drive/plot/bake sequence and its ordering hazards disappear entirely;
* it is **DCC-agnostic**, so MoBu and Maya can adopt it and retire their own
  retarget paths.

Design
------

The input is motion on the *model's* canonical skeleton; the output is the same
motion on the *user's* skeleton, ready for ``animator.apply_animation``.

Both MMCP canonical skeletons carry identity ``rest_rotation`` — the rotation
convention is world-aligned parent frames — but their rest *geometry* differs:
SOMA rests in a T-pose while ARDY's Core27 rests with the arms hanging down.
So local rotations cannot simply be copied across; a walk transferred that way
arrives with the arms held straight out, which is exactly the bug MoBu's
``pose_arms_horizontal`` exists to prevent (commit 898216b).

The fix is to transfer what each joint *does relative to its own rest*, which
means building a rest frame per joint and conjugating:

    F_j          orthonormal rest frame of joint j, from the rest geometry
    W_s(f)       source joint's world rotation at frame f (FK over the source)
    W_t(f)  =  W_s(f) · F_s · F_tᵀ

At rest (``W_s = I``) the target adopts the source's rest orientation, which is
what retargeting means: the target does what the source does. Local rotations
then come back out of the target hierarchy, and ``posed_joints`` is re-solved
by forward kinematics on the target so downstream consumers
(``core.ground_measure``, the apply path's root transport) stay consistent.

Limitations, stated rather than hidden
--------------------------------------

* **Rotation retargeting only.** There is no IK pass, so a foot pinned by the
  source's geometry can slide when limb proportions differ. HIK's solver does
  better here; its own output was measured off by ~3 cm in Z on the same
  comparison (commit 8e4c4f4), so the difference is one of degree.
* **Twist is resolved against the character's forward axis.** A rest frame
  built from positions alone has no roll information, so the secondary axis is
  taken from world forward (+Z in the canonical Y-up frame), falling back to
  world up for bones that point along it. Both skeletons use the same rule, so
  the twist reference is at least consistent.
* **Unmapped target joints keep their rest pose** relative to their parent —
  SOMA's fingers stay put when driven by a 27-joint source, rather than being
  guessed at.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import math

import numpy as np

_EPS = 1e-9

# Canonical Y-up axes. Forward is the twist reference because a bone pointing
# along it is rare (toes), while bones along world up are common (spine, neck,
# and every limb of an arms-down rest pose).
_FORWARD = np.array([0.0, 0.0, 1.0])
_UP = np.array([0.0, 1.0, 0.0])

_HIK_XML = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "Animatica_HIK.xml")

# Joints whose absence makes a mapping useless. Same list the HIK path requires,
# so "can this pair be retargeted?" gets the same answer as "could HIK
# characterise it?".
REQUIRED_SLOTS = frozenset({
    "Hips", "Spine",
    "LeftArm", "LeftForeArm", "LeftHand",
    "RightArm", "RightForeArm", "RightHand",
    "LeftUpLeg", "LeftLeg", "LeftFoot",
    "RightUpLeg", "RightLeg", "RightFoot",
    "Head",
})

_TABLE_CACHE: dict | None = None


# ---------------------------------------------------------------------------
# joint mapping — one source of truth, shared with the HIK path
# ---------------------------------------------------------------------------

def load_hik_table(path: str | None = None) -> dict[str, str]:
    """``{hik_slot: joint_name}`` from ``config/Animatica_HIK.xml``.

    The bundled table is what translates SOMA's naming into HIK slots
    (``LeftUpLeg`` is SOMA's ``LeftLeg``, ``Spine`` is ``Spine1``, …). Blank
    values mean "this slot has no SOMA joint" and are dropped. The
    ``animatica_`` prefix is a MoBu artefact and is stripped.
    """
    global _TABLE_CACHE
    if path is None and _TABLE_CACHE is not None:
        return dict(_TABLE_CACHE)

    table: dict[str, str] = {}
    try:
        root = ET.parse(path or _HIK_XML).getroot()
    except (OSError, ET.ParseError):
        return table
    for item in root.iter("item"):
        slot = (item.get("key") or "").strip()
        value = (item.get("value") or "").strip()
        if not slot or not value:
            continue
        if value.startswith("animatica_"):
            value = value[len("animatica_"):]
        table[slot] = value
    if path is None:
        _TABLE_CACHE = dict(table)
    return table


def slot_map_for(joint_names, table: dict | None = None) -> dict[str, str]:
    """``{hik_slot: joint_name}`` for one skeleton's joint names.

    Two naming conventions are in play and both are already known:
    the bundled table describes SOMA, while ARDY's Core27 names **are** HIK
    slot names, so the identity fits it. The table is tried first because it is
    the documented one; whichever resolves more required slots wins, so a rig
    that is partly both cannot be misread (the failure that once reported a
    Core27 rig as missing ``LeftLeg``).
    """
    names = {str(n).split(":")[-1] for n in joint_names}
    table = load_hik_table() if table is None else table

    from_table = {slot: joint for slot, joint in table.items() if joint in names}
    identity = {n: n for n in names if n in REQUIRED_SLOTS or n in table}

    t_score = len(REQUIRED_SLOTS & set(from_table))
    i_score = len(REQUIRED_SLOTS & set(identity))
    return from_table if t_score >= i_score else identity


def build_mapping(source_names, target_names, table: dict | None = None
                  ) -> dict[str, str]:
    """``{target_joint: source_joint}``, composed through HIK slot names.

    Each skeleton is resolved to slots independently, then the two are joined on
    the slots they share — so the pairing never depends on the two skeletons
    using the same vocabulary.
    """
    src_slots = slot_map_for(source_names, table)
    dst_slots = slot_map_for(target_names, table)
    return {dst_slots[slot]: src_slots[slot]
            for slot in dst_slots.keys() & src_slots.keys()}


def missing_required(source_names, target_names, table: dict | None = None
                     ) -> list[str]:
    """Required slots that do not resolve on **both** skeletons.

    Empty means the pair can be retargeted. The caller reports the names, so a
    refusal says which joints are missing rather than just failing.
    """
    src_slots = slot_map_for(source_names, table)
    dst_slots = slot_map_for(target_names, table)
    both = dst_slots.keys() & src_slots.keys()
    return sorted(REQUIRED_SLOTS - both)


# ---------------------------------------------------------------------------
# rest frames
# ---------------------------------------------------------------------------

def _normalise(v):
    n = float(np.linalg.norm(v))
    return None if n < _EPS else np.asarray(v, float) / n


def _primary_child(joint, hierarchy, rest, shared=None):
    """The child that defines this joint's bone direction.

    Where a joint has several children (Hips has the spine and both legs) the
    longest bone is used, which picks the anatomically dominant one and is
    stable across skeletons rather than depending on child ordering.

    *shared* restricts the choice to children that are MAPPED between the two
    skeletons, and that restriction is what makes the frames comparable.

    "Mapped", not "present in both" -- the distinction is load-bearing and I
    got it wrong in prose first. soma77 and the 30-joint source BOTH carry
    Jaw, LeftEye and RightEye under Head, yet none of the three is in the
    mapping, so none can anchor a comparable frame. Chosen per skeleton,
    "longest" answers differently when the child sets differ: against a
    30-joint source, this target's Head takes HeadEnd (up the skull) while the
    source's takes LeftEye (forward), and LeftHand takes the thumb where the
    source takes the middle finger. Two different bone axes, so an identity
    input came out as a 64-degree head and 58-degree hands -- visible as
    "the hands are rotated oddly" long before anyone measured it.

    Falls back to every child when none is mapped: a frame off the wrong axis
    still beats no frame, and ``_shared_child`` will refuse to align that
    joint anyway, so the fallback frame is never used for a correction.
    """
    kids = [c for c, p in hierarchy if p == joint and c in rest]
    if not kids:
        return None
    if shared:
        common = [c for c in kids if c in shared]
        if common:
            kids = common
    here = np.asarray(rest[joint], float)
    return max(kids, key=lambda c: float(
        np.linalg.norm(np.asarray(rest[c], float) - here)))


def _shared_child(joint, hierarchy, rest, shared) -> bool:
    """Whether *joint* has a MAPPED child.

    The precondition for a comparable rest frame: without one, the two frames
    are built off different bones and their difference is noise, not anatomy.

    Measured on the real pair: neither Head nor LeftHand has a mapped child on
    both sides -- the target head continues into an unmapped HeadEnd, the
    source head into unmapped eyes, and the hands branch into fingers that do
    not pair -- so both are left unaligned, which is what turns a 64-degree
    head and 58-degree hands into 0.00.
    """
    return any(c in shared for c, p in hierarchy if p == joint and c in rest)

def rest_frames(hierarchy, rest, shared=None) -> dict:
    """``{joint: 3x3 orthonormal frame}`` built from rest positions alone.

    Column 0 is the bone direction; columns 1 and 2 are completed against the
    character's forward axis, falling back to world up when the bone points
    along forward. A leaf inherits its parent's frame, which keeps hands and
    toes from getting an arbitrary orientation.
    """
    parent_of = dict(hierarchy)
    frames: dict = {}
    for joint, _p in hierarchy:
        if joint not in rest:
            continue
        child = _primary_child(joint, hierarchy, rest, shared)
        x = None
        if child is not None:
            x = _normalise(np.asarray(rest[child], float)
                           - np.asarray(rest[joint], float))
        if x is None:
            parent = parent_of.get(joint)
            if parent in frames:
                frames[joint] = frames[parent].copy()
                continue
            x = _FORWARD.copy()

        ref = _FORWARD
        if abs(float(np.dot(x, ref))) > 0.99:
            ref = _UP
        y = _normalise(ref - float(np.dot(ref, x)) * x)
        if y is None:
            y = _normalise(_UP - float(np.dot(_UP, x)) * x) or np.array([0.0, 1.0, 0.0])
        z = np.cross(x, y)
        frames[joint] = np.column_stack((x, y, z))
    return frames


def hip_height(rest, hierarchy) -> float:
    """Root height above the lowest joint — the proportion scale reference."""
    if not rest:
        return 1.0
    root = hierarchy[0][0] if hierarchy else None
    if root not in rest:
        return 1.0
    lowest = min(float(p[1]) for p in rest.values())
    return max(float(rest[root][1]) - lowest, _EPS)


# ---------------------------------------------------------------------------
# the transfer
# ---------------------------------------------------------------------------

def _forward_kinematics(hierarchy, rest, local_rot, index, root_world=None):
    """World rotations and translations for one frame, Y-up metres."""
    world_r: dict = {}
    world_t: dict = {}
    for joint, parent in hierarchy:
        if joint not in rest:
            continue
        r_local = local_rot[index[joint]] if joint in index else np.eye(3)
        if parent is None or parent not in world_r:
            world_r[joint] = r_local
            world_t[joint] = (np.asarray(root_world, float)
                              if root_world is not None
                              else np.asarray(rest[joint], float))
        else:
            offset = np.asarray(rest[joint], float) - np.asarray(rest[parent], float)
            world_r[joint] = world_r[parent] @ r_local
            world_t[joint] = world_t[parent] + world_r[parent] @ offset
    return world_r, world_t


def retarget_motion(motion_data, source_hierarchy, source_rest,
                    target_hierarchy, target_rest, *, mapping=None,
                    scale_root=True):
    """Motion on the source skeleton → motion on the target skeleton.

    *motion_data* is the parsed response (``local_rot_mats``, ``posed_joints``,
    ``joint_names``, ``fps``); the returned dict has the same shape and keys,
    keyed on the target's joint names and ready for ``apply_animation``.

    *source_rest* / *target_rest* are ``{joint: (x, y, z)}`` in **Y-up metres**
    — the same convention as the MMCP skeleton block, so they come straight from
    ``build_canonical_skeleton_block`` or the registry.

    *scale_root* scales the root trajectory by the hip-height ratio, so stride
    length follows leg length. Turn it off to keep the source's absolute
    trajectory — which preserves authored pin positions exactly but makes a
    short character over-stride.
    """
    src_names = list(motion_data.get("joint_names") or [])
    if not src_names:
        raise ValueError("motion_data carries no joint_names")
    local_rot = np.asarray(motion_data["local_rot_mats"], dtype=np.float64)
    posed = np.asarray(motion_data["posed_joints"], dtype=np.float64)
    num_frames = local_rot.shape[0]

    tgt_names = [j for j, _p in target_hierarchy]
    if mapping is None:
        mapping = build_mapping(src_names, tgt_names)
    if not mapping:
        raise ValueError(
            "no joints could be paired between the two skeletons; "
            "retargeting needs a shared naming convention")

    src_index = {n: i for i, n in enumerate(src_names)}
    tgt_index = {n: i for i, n in enumerate(tgt_names)}
    # Both frames are built from the joints the two skeletons SHARE, so each
    # bone axis is measured the same way on both sides. mapping is
    # {target: source}, so each side gets its own half of those pairs.
    shared_tgt = set(mapping)
    shared_src = set(mapping.values())
    src_frames = rest_frames(source_hierarchy, source_rest, shared_src)
    tgt_frames = rest_frames(target_hierarchy, target_rest, shared_tgt)

    # A_j = F_s · F_tᵀ, precomputed per mapped joint. Frames are orthonormal, so
    # the transpose is the inverse.
    align: dict = {}
    for tgt_joint, src_joint in mapping.items():
        if src_joint not in src_frames or tgt_joint not in tgt_frames:
            continue
        # Only align where both frames measure the SAME bone. Where a joint
        # has no MAPPED child on both sides, each frame was built off whatever
        # that skeleton happened to have -- the target head continues into
        # HeadEnd, the 30-joint source head into an eye, neither of them
        # mapped -- and F_s . F_t^T then encodes the angle between two
        # unrelated axes. On a zero pose that came out as a 64-degree head.
        # Nothing measurable says how these two differ, so pass the source
        # rotation through rather than inventing a correction.
        if not (_shared_child(src_joint, source_hierarchy, source_rest,
                              shared_src)
                and _shared_child(tgt_joint, target_hierarchy, target_rest,
                                  shared_tgt)):
            continue
        align[tgt_joint] = src_frames[src_joint] @ tgt_frames[tgt_joint].T

    ratio = 1.0
    if scale_root:
        ratio = (hip_height(target_rest, target_hierarchy)
                 / hip_height(source_rest, source_hierarchy))

    out_local = np.zeros((num_frames, len(tgt_names), 3, 3), dtype=np.float64)
    out_posed = np.zeros((num_frames, len(tgt_names), 3), dtype=np.float64)
    tgt_parent = dict(target_hierarchy)
    tgt_root = tgt_names[0]

    for f in range(num_frames):
        src_world_r, _ = _forward_kinematics(
            source_hierarchy, source_rest, local_rot[f], src_index)

        # target world rotations, parents before children
        world_r: dict = {}
        for joint, parent in target_hierarchy:
            src_joint = mapping.get(joint)
            if src_joint is not None and src_joint in src_world_r:
                world_r[joint] = src_world_r[src_joint] @ align.get(
                    joint, np.eye(3))
            elif parent in world_r:
                # Unmapped: hold the rest pose relative to the parent.
                world_r[joint] = world_r[parent]
            else:
                world_r[joint] = np.eye(3)

        # back to local, and re-solve positions on the target's own geometry
        for joint, parent in target_hierarchy:
            if parent is None or parent not in world_r:
                out_local[f, tgt_index[joint]] = world_r[joint]
            else:
                out_local[f, tgt_index[joint]] = world_r[parent].T @ world_r[joint]

        src_root_world = posed[f][0] if posed.shape[1] else np.zeros(3)
        root_world = np.asarray(src_root_world, float) * ratio
        _, world_t = _forward_kinematics(
            target_hierarchy, target_rest, out_local[f], tgt_index,
            root_world=root_world)
        for joint in tgt_names:
            if joint in world_t:
                out_posed[f, tgt_index[joint]] = world_t[joint]

    result = dict(motion_data)
    result["local_rot_mats"] = out_local
    result["posed_joints"] = out_posed
    result["joint_names"] = tgt_names
    result["retargeted_from"] = src_names
    result["retarget_scale"] = ratio
    return result
