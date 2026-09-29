# SPDX-License-Identifier: Apache-2.0
"""Which bone of an arbitrary humanoid rig plays each of the poser's joints.

The poser speaks SOMA-30: ``Hips``, ``Spine1``, ``LeftLeg`` (the thigh), ``LeftShin``… Real
rigs spell those joints a dozen ways, and some of the spellings collide: Mixamo's ``LeftLeg``
is the SHIN, not the thigh, and a Mixamo ``Spine1`` is SOMA's ``Spine2``. Matching by name
alone therefore maps the wrong bones onto the right names, which is worse than mapping none.

So the map is found from the rig's STRUCTURE and names only break ties:

  * the hips are where the two legs and the spine meet — the common ancestor of both feet
    and the head;
  * the chest is where the two arms meet — the common ancestor of both hands;
  * each limb, the spine and the neck are the chains between those, with twist, roll and
    bendy-segment bones taken out, and are read off in order.

Everything here works on plain tuples (:class:`Bone`), not on ``bpy`` data, so it can be run
and checked without Blender. ``from_armature`` is the one adapter.

A rig whose bones already carry the canonical names (optionally namespaced, like the Animatica
character's ``animatica:Hips``) is taken exactly as it is named, provided the names agree with
the hierarchy — the rig this was all verified on keeps its verified mapping.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: The poser's joints, in its own order (``meta.json`` carries the same list).
CANON = (
    "Hips", "Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head", "Jaw", "LeftEye", "RightEye",
    "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand", "LeftHandThumbEnd", "LeftHandMiddleEnd",
    "RightShoulder", "RightArm", "RightForeArm", "RightHand", "RightHandThumbEnd",
    "RightHandMiddleEnd",
    "LeftLeg", "LeftShin", "LeftFoot", "LeftToeBase", "RightLeg", "RightShin", "RightFoot",
    "RightToeBase",
)

#: The joints a usable map must have. Without these there is no body to pose.
CORE = ("Hips", "Chest", "Head", "LeftArm", "LeftForeArm", "LeftHand", "RightArm",
        "RightForeArm", "RightHand", "LeftLeg", "LeftShin", "LeftFoot", "RightLeg",
        "RightShin", "RightFoot")

#: What each joint's canonical parent is, by name (mirrors ``meta.json`` ``parents``).
PARENT = {
    "Spine1": "Hips", "Spine2": "Spine1", "Chest": "Spine2", "Neck1": "Chest", "Neck2": "Neck1",
    "Head": "Neck2", "Jaw": "Head", "LeftEye": "Head", "RightEye": "Head",
    "LeftShoulder": "Chest", "LeftArm": "LeftShoulder", "LeftForeArm": "LeftArm",
    "LeftHand": "LeftForeArm", "LeftHandThumbEnd": "LeftHand", "LeftHandMiddleEnd": "LeftHand",
    "RightShoulder": "Chest", "RightArm": "RightShoulder", "RightForeArm": "RightArm",
    "RightHand": "RightForeArm", "RightHandThumbEnd": "RightHand",
    "RightHandMiddleEnd": "RightHand",
    "LeftLeg": "Hips", "LeftShin": "LeftLeg", "LeftFoot": "LeftShin", "LeftToeBase": "LeftFoot",
    "RightLeg": "Hips", "RightShin": "RightLeg", "RightFoot": "RightShin",
    "RightToeBase": "RightFoot",
}

#: Bones inside a limb chain that are not joints: twist/roll helpers, correctives, and the
#: second halves of Rigify-style bendy segments (``DEF-upper_arm.L.001``).
_HELPER = re.compile(r"twist|roll|tws|share|helper|corrective|correct|heel|bend_?fix|"
                     r"(^|[_\-.\s])(tw|ik|fk|mch|pole|target|ctrl|con|fwd|bck|in|out)"
                     r"([_\-.\s\d]|$)", re.I)
_SEGMENT = re.compile(r"\.(00[1-9])(\.[lr])?$|\.(00[1-9])$", re.I)
#: Leaf markers exported by FBX/Maya/Mixamo that carry no joint of their own.
_END = re.compile(r"(_end|end|_nub|nub|_tip|tip|_top|top_end)$", re.I)


@dataclass(frozen=True)
class Bone:
    """What detection needs of a bone. Positions are armature space, Blender axes (Z up)."""
    name: str
    parent: str | None
    head: tuple
    tail: tuple
    deform: bool = True


# --------------------------------------------------------------------------- small helpers
def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _norm(v):
    return (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5


def _bare(name: str) -> str:
    """The name without a namespace (``mixamorig:``, ``Armature|``)."""
    return re.split(r"[:|]", name)[-1]


class _Rig:
    def __init__(self, bones):
        self.all = {b.name: b for b in bones}
        # Deform bones are the skeleton; a rig that marks none (a bare import) is all skeleton.
        self.children = {n: [] for n in self.all}
        for b in bones:
            if b.parent in self.children:
                self.children[b.parent].append(b.name)
        deform = {b.name for b in bones if b.deform}
        use = deform if len(deform) >= 10 else set(self.all)
        # Only the body: the biggest tree. Export roots for IK targets, centre of mass or
        # interaction points hang beside it, and one of those would pass for a foot.
        roots = [n for n, b in self.all.items() if b.parent not in self.all]
        best = max(roots, key=lambda r: sum(1 for d in [r] + self.descendants(r) if d in use),
                   default=None)
        self.use = {d for d in ([best] + self.descendants(best) if best else []) if d in use}

    def head(self, n):
        return self.all[n].head

    def parent(self, n):
        return self.all[n].parent

    def ancestors(self, n):
        out = []
        p = self.parent(n)
        while p is not None and p in self.all:
            out.append(p)
            p = self.parent(p)
        return out

    def path(self, top, bottom):
        """Bones from just below *top* down to *bottom*, inclusive, or None if not a chain."""
        chain = [bottom]
        for a in self.ancestors(bottom):
            if a == top:
                return list(reversed(chain))
            chain.append(a)
        return None

    def lca(self, names):
        sets = [[n] + self.ancestors(n) for n in names]
        common = set(sets[0]).intersection(*sets[1:])
        for n in sets[0]:
            if n in common:
                return n
        return None

    def descendants(self, n):
        out, stack = [], list(self.children[n])
        while stack:
            c = stack.pop()
            out.append(c)
            stack.extend(self.children[c])
        return out


def _is_end(rig, n) -> bool:
    """A leaf marker (``LeftToe_End``, ``HeadTop_End``), not a joint."""
    b = rig.all[n]
    return not rig.children[n] and (bool(_END.search(_bare(n))) or not b.deform)


def _joints_only(rig, chain):
    """A limb chain with its helper, segment and end bones removed."""
    return [n for n in chain
            if n in rig.use and not _HELPER.search(_bare(n)) and not _SEGMENT.search(n)
            and not _is_end(rig, n)]


# --------------------------------------------------------------------------- by name
def _named(rig, prefix: str):
    """The canonical map a rig gives by its names alone: exact, or behind one namespace."""
    out = {}
    for j in CANON:
        for cand in (j, prefix + j):
            if cand in rig.all:
                out[j] = cand
                break
    return out


def _prefix(rig) -> str:
    names = set(rig.all)
    if "Hips" in names:
        return ""
    for n in names:
        if n.endswith(":Hips") or n.endswith("_Hips"):
            return n[:-len("Hips")]
    return ""


def consistent(rig_or_bones, mapping) -> bool:
    """Whether every mapped joint hangs below the bone mapped to its canonical parent."""
    rig = rig_or_bones if isinstance(rig_or_bones, _Rig) else _Rig(rig_or_bones)
    for j, bn in mapping.items():
        p = PARENT.get(j)
        while p is not None and p not in mapping:
            p = PARENT.get(p)
        if p is None:
            continue
        if mapping[p] not in rig.ancestors(bn):
            return False
    return True


# --------------------------------------------------------------------------- by structure
def _side_of(rig, n, centre_x) -> str:
    """'L' or 'R' by the rig's naming, else by which side of the body the bone is on.

    Blender's convention is a character facing -Y with its LEFT on +X — the poser's frame
    assumes the same, so position decides when the name does not say.
    """
    named = name_side(_bare(n))
    if named:
        return named
    return "L" if rig.head(n)[0] >= centre_x else "R"


def name_side(s: str) -> str:
    """'L', 'R' or '' from a bone name's side marker, in any of the common conventions:
    ``LeftArm``, ``upperarm_l``, ``DEF-hand.L``, ``Bip01 L Thigh``, ``CC_Base_L_Hand``,
    ``lShldrBend``."""
    for side, word in (("L", "left"), ("R", "right")):
        ch = side.lower()
        if s.lower().startswith(word) or s.lower().endswith(word):
            return side
        if re.search(rf"(^|[._\-\s]){ch}([._\-\s]|$)", s, re.I):
            return side
        if re.match(rf"^{ch}(?=[A-Z])", s):          # Daz: lShldrBend, rThighBend
            return side
    return ""


def _extremity(rig, candidates, key):
    """The joint that ends a limb: the candidate best by *key*, skipping end markers."""
    best = None
    for n in candidates:
        if n not in rig.use or _is_end(rig, n):
            continue
        if best is None or key(n) > key(best):
            best = n
    return best


def _hand_of(rig, arm_chain):
    """Where the fingers branch — or, on a rig without fingers, the last real joint."""
    for n in arm_chain:
        kids = [c for c in rig.children[n] if c in rig.use and not _is_end(rig, c)
                and not _HELPER.search(_bare(c))]
        if len(kids) >= 3:
            return n
    return arm_chain[-1] if arm_chain else None


def structural(bones):
    """The canonical map a humanoid rig gives by its shape. Missing joints are left out."""
    rig = _Rig(bones)
    use = [n for n in rig.all if n in rig.use]
    if len(use) < 10:
        return {}
    zs = [rig.head(n)[2] for n in use]
    xs = [rig.head(n)[0] for n in use]
    zmin, zmax = min(zs), max(zs)
    height = max(zmax - zmin, 1e-6)
    # the body's midline: its root, not the middle of the bounding box, which one hand with
    # fingers and one without pulls off to the side
    root = next((n for n in use if rig.parent(n) not in rig.use), None)
    centre_x = rig.head(root)[0] if root else (min(xs) + max(xs)) / 2.0

    # --- feet: the lowest real joint on each side, at the far end of a chain
    low = [n for n in use if rig.head(n)[2] < zmin + 0.25 * height and not _is_end(rig, n)
           and not _HELPER.search(_bare(n))]
    sides = {"L": [], "R": []}
    for n in low:
        if name_side(_bare(n)) or abs(rig.head(n)[0] - centre_x) > 0.02 * height:
            sides[_side_of(rig, n, centre_x)].append(n)
    if not sides["L"] or not sides["R"]:
        return {}

    def deepest(ns):
        # the end of the foot chain: the ball or toe is the lowest real joint (the ankle and its
        # correctives sit above it); among equals, the one further down the hierarchy
        return min(ns, key=lambda n: (round(rig.head(n)[2] / (0.01 * height)),
                                      -len(rig.ancestors(n))))

    foot_end = {s: deepest(ns) for s, ns in sides.items()}

    # --- hands: the joints furthest out to each side, above the waist
    upper = [n for n in use if rig.head(n)[2] > zmin + 0.5 * height]
    hand_end = {}
    for s, sign in (("L", 1.0), ("R", -1.0)):
        cand = [n for n in upper if _side_of(rig, n, centre_x) == s]
        n = _extremity(rig, cand, key=lambda n: sign * (rig.head(n)[0] - centre_x))
        if n is None:
            return {}
        hand_end[s] = n

    # --- head: the highest central joint
    central = [n for n in upper if abs(rig.head(n)[0] - centre_x) < 0.05 * height]
    head_top = _extremity(rig, central, key=lambda n: rig.head(n)[2])
    if head_top is None:
        return {}

    chest = rig.lca([hand_end["L"], hand_end["R"]])
    hips = rig.lca([foot_end["L"], foot_end["R"], head_top])
    if chest is None or hips is None or chest == hips or hips not in rig.ancestors(chest):
        return {}
    out = {"Hips": hips, "Chest": chest}

    # --- spine: the joints between hips and chest
    spine = [n for n in (rig.path(hips, chest) or [])[:-1] if n in rig.use]
    if spine:
        out["Spine1"] = spine[0]
    if len(spine) >= 2:
        # SOMA's Spine2 sits a little under half-way up; pick the rig's closest
        z0, z1 = rig.head(hips)[2], rig.head(chest)[2]
        frac = lambda n: (rig.head(n)[2] - z0) / max(z1 - z0, 1e-6)
        out["Spine2"] = min(spine[1:], key=lambda n: abs(frac(n) - 0.55))

    # --- neck and head: the chain from the chest up to the top of the head
    up = [n for n in (rig.path(chest, head_top) or []) if n in rig.use]
    head = next((n for n in up if re.search(r"head", _bare(n), re.I)), None)
    if head is None and up:
        # no name to go by: the head is the last joint with the face hanging off it, else the top
        head = max(up, key=lambda n: (len(rig.descendants(n)) >= 2, up.index(n))) \
            if len(up) > 1 else up[0]
    if head is None:
        return {}
    out["Head"] = head
    neck = [n for n in up[:up.index(head)] if not _HELPER.search(_bare(n))]
    if neck:
        out["Neck1"] = neck[0]
    if len(neck) >= 2:
        out["Neck2"] = neck[-1]
    for n in rig.descendants(head):
        s = _bare(n).lower()
        if "jaw" in s and "Jaw" not in out and n in rig.use:
            out["Jaw"] = n
        elif "eye" in s and "lid" not in s and "brow" not in s and n in rig.use:
            key = "LeftEye" if _side_of(rig, n, centre_x) == "L" else "RightEye"
            out.setdefault(key, n)

    # --- arms: chest -> shoulder, upper arm, forearm, hand
    for s, side in (("L", "Left"), ("R", "Right")):
        chain = _joints_only(rig, rig.path(chest, hand_end[s]) or [])
        hand = _hand_of(rig, chain)
        if hand is None:
            return {}
        before = chain[:chain.index(hand)]
        if len(before) < 2:
            return {}
        out[side + "Hand"] = hand
        out[side + "ForeArm"] = before[-1]
        out[side + "Arm"] = before[-2]
        if len(before) >= 3:
            out[side + "Shoulder"] = before[-3]

    # --- legs: hips -> thigh, shin, foot, toe
    for s, side in (("L", "Left"), ("R", "Right")):
        chain = _joints_only(rig, rig.path(hips, foot_end[s]) or [])
        # a pelvis bone some rigs hang each thigh from sits level with the hips: not a joint
        while len(chain) > 3 and rig.head(chain[0])[2] >= rig.head(hips)[2] - 0.02 * height \
                and abs(rig.head(chain[1])[2] - rig.head(chain[0])[2]) < 0.03 * height:
            chain = chain[1:]
        if len(chain) < 3:
            return {}
        out[side + "Leg"], out[side + "Shin"], out[side + "Foot"] = chain[0], chain[1], chain[2]
        if len(chain) >= 4:
            out[side + "ToeBase"] = chain[3]
    return out


# --------------------------------------------------------------------------- entry points
#: Which way is up, for rigs authored in other conventions. Each maps the rig's own axes onto
#: Blender's (Z up, facing -Y): identity first, then Y-up (every FBX/Maya/Mixamo import whose
#: armature carries its conversion in an object or parent rotation), then the rest.
UPS = (
    ((1, 0, 0), (0, 1, 0), (0, 0, 1)),        # Z up
    ((1, 0, 0), (0, 0, -1), (0, 1, 0)),       # Y up   (R_x +90)
    ((1, 0, 0), (0, 0, 1), (0, -1, 0)),       # -Y up  (R_x -90)
    ((0, 0, -1), (0, 1, 0), (1, 0, 0)),       # X up   (R_y -90)
    ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),       # -X up  (R_y +90)
    ((1, 0, 0), (0, -1, 0), (0, 0, -1)),      # -Z up  (R_x 180)
)


def _apply(m, v):
    return tuple(m[r][0] * v[0] + m[r][1] * v[1] + m[r][2] * v[2] for r in range(3))


def _rotated(bones, up):
    return [Bone(b.name, b.parent, _apply(up, b.head), _apply(up, b.tail), b.deform)
            for b in bones]


def upright(bones, mapping) -> bool:
    """Head over chest over hips over both feet: the map describes a standing body."""
    z = {b.name: b.head[2] for b in bones}
    try:
        feet = max(z[mapping["LeftFoot"]], z[mapping["RightFoot"]])
        return z[mapping["Head"]] > z[mapping["Chest"]] > z[mapping["Hips"]] > feet
    except KeyError:
        return False


def detect_frame(bones):
    """``({canonical joint: bone name}, up)`` for a rig: the map, as complete as its skeleton
    allows, and the rotation (rows) that stands the rig up Z-up. ``({}, identity)`` when no
    humanoid is found."""
    bones = list(bones)
    rig = _Rig(bones)
    named = _named(rig, _prefix(rig))
    if all(j in named for j in CORE) and consistent(rig, named):
        for up in UPS:                   # canonically named: take it as it is named
            if upright(_rotated(bones, up), named):
                return named, up
    for up in UPS:
        turned = _rotated(bones, up)
        found = structural(turned)
        if found and not missing_core(found) and consistent(rig, found) \
                and upright(turned, found):
            return found, up
    partial = named if consistent(rig, named) else {}
    return partial, UPS[0]


def detect(bones) -> dict:
    """``{canonical joint: bone name}`` for a rig, as complete as its skeleton allows."""
    return detect_frame(bones)[0]


def missing_core(mapping) -> list:
    return [j for j in CORE if j not in mapping]


def from_armature(arm):
    """The rig's bones as :class:`Bone` tuples (rest pose, armature space)."""
    out = []
    for b in arm.data.bones:
        if b.get("ap_eff") or getattr(b, "ap_joint", ""):
            continue                      # the Autoposer's own control bones
        h, t = b.head_local, b.tail_local
        out.append(Bone(b.name, b.parent.name if b.parent else None,
                        (h.x, h.y, h.z), (t.x, t.y, t.z), bool(b.use_deform)))
    return out
