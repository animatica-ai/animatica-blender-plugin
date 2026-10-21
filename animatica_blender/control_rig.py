# SPDX-License-Identifier: GPL-3.0-or-later
"""Control rigs: a pose put on a rig through its own controls.

A control rig's deform bones ignore anything written to them -- constraints drive them from the
controls an animator keys. So a take (or a pasted clip, or a generated pose) is put on the rig's
FK CONTROLS: each one set where its skeleton bone must be, parents first (each level evaluated
before the next, so constraints in between are honoured), then the skeleton read back and the
controls corrected for what is left.

A limb in IK (a generated Rigify rig starts with every limb in IK) is switched to FK the first
time it is posed, its FK matched to what it shows first so the switch moves nothing, and the
switch keyed at the next key (`to_fk`): an IK limb is the rig's solver's to bend (its stretch,
its pole's plane), so the pose would land only roughly through it. At each key the limb's OTHER
mode is matched to the same pose and keyed too (``match``), so switching back to IK at a key
never jumps. Writing IK (``Profile.IK``: its target, foot roll, toe, the chain's turn or the pole)
stays for that matching, and for a limb left in IK because its IK carries animation of its own.

The same engine as Marionette's (its ``control_rig``, copied from Marionette 1.0.4), so a take
lands on a rig the way Marionette poses it. Changed here: the limbs' other mode matched in one
solve (`match_all`), a correction pass that makes things worse undone (`_write_fk`), IK targets
set at unit scale (`_unscaled`), and an IK description for Auto-Rig Pro. One profile per rig
family:

* **Rigify** (a generated rig: ``rig_id``, ``ORG-`` bones). Its skeleton is the ORG chain, which
  is the metarig exactly; the DEF bones hang off it and its tweak bones.
* **Auto-Rig Pro** (``c_traj``, ``c_root_master.x``). Its skeleton is the bones the IK and FK
  chains are blended into -- ``root.x``, ``spine_01.x``, ``arm.l``, ``forearm.l``, ``hand.l``,
  ``thigh.l``, ``leg.l``, ``foot.l`` -- which the stretch and twist deform bones follow.
* **CloudRig** (``is_generated_cloudrig``).
* any other rig Marionette has connected (its Rig Connection, ``rig_connect``): the map it
  stored on the rig, or one from its library that fits.

``profile(arm)`` says which, or None for a rig with no profile (``control_bake`` says what
happens to one).
"""

from __future__ import annotations

import bpy
from mathutils import Matrix

SIDES = (("Left", "L"), ("Right", "R"))


class Profile:
    """What it takes to pose one family of control rigs."""

    name = ""

    def joints(self, arm) -> dict:
        """``{canonical joint: skeleton bone}``."""
        raise NotImplementedError

    def controls(self, arm) -> list:
        """``[(control, skeleton bone)]``: each control set so that bone lands where asked,
        in the rig's hierarchy order."""
        raise NotImplementedError

    #: controls set in place as well as turned (besides the hips'): see `write`
    PLACED = ()
    #: fit the body's control to every joint it carries between passes (`_fit_mover`): for a rig
    #: whose body control sits away from the hips (a lever the hips' own turn would swing)
    FIT_MOVER = False

    def keyed(self, arm) -> list:
        """The control bones an edit keys."""
        return [c for c, _b in self.controls(arm)]

    #: (bone holding the switch, its property, the value meaning FK, limb) per limb, "{s}" the side
    SWITCHES = ()
    #: the skeleton bones of each limb kind, "{s}" the side
    LIMB_BONES = {}
    SIDE = {"Left": "L", "Right": "R"}

    def ik_limbs(self, arm) -> list:
        """The limbs in IK mode (\"L arm\", \"spine\", ...): their FK pose is set but not what
        shows. A switch's bone and property may name the side (``{s}``: the rig's suffix,
        ``{side}``: left / right)."""
        out = []
        for bone, prop, fk, limb in self.SWITCHES:
            sided = "{s}" in bone + prop or "{side}" in bone + prop
            for side in (("Left", "Right") if sided else ("",)):
                s = self.SIDE.get(side, "")
                pb = arm.pose.bones.get(bone.format(s=s, side=side.lower()))
                key = prop.format(s=s, side=side.lower())
                if pb is not None and key in pb and abs(float(pb[key]) - fk) > 0.5:
                    out.append(f"{side[:1]} {limb}" if side else limb)
        return out

    def ik_bones(self, arm) -> set:
        """Skeleton bones nothing this profile sets drives right now: their limb is in IK and
        the profile has no IK description for it (`IK`), so only its FK is set."""
        out = set()
        for lim in self.ik_limbs(arm):
            side, _sp, limb = lim.rpartition(" ")
            side = {"L": "Left", "R": "Right"}.get(side, "")
            if self.ik_spec(arm, limb, side) is not None:
                continue                    # posed through its IK
            s = self.SIDE[side] if side else ""
            out.update(b.format(s=s) for b in self.LIMB_BONES.get(limb, ()))
        return out

    #: how a limb in IK is posed, by limb kind ("{s}" the side): ``target`` (the IK control and
    #: the skeleton bone it carries), ``twist`` (the control that turns the chain about its own
    #: length: the elbow's or knee's plane when the pole is off), ``pole`` and ``pole_on`` (the
    #: pole control, and the (bone, property) that turns it on), the skeleton's ``root``,
    #: ``mid`` and ``end`` of the chain, ``reset`` (controls put back at rest first: a foot's
    #: roll) and ``extra`` ((control, bone) placed after the target: a toe). A family without one
    #: poses its IK limbs' FK only.
    IK = {}

    def limbs(self, arm) -> list:
        """``[(limb, side)]``: each limb with an IK/FK switch on this rig ("" for one not sided)."""
        out = []
        for _bone, _prop, _fk, limb in self.SWITCHES:
            for side in ("Left", "Right"):
                if self._switch(arm, limb, side) is not None and (limb, side) not in out:
                    out.append((limb, side))
            if self._switch(arm, limb, "") is not None and (limb, "") not in out:
                out.append((limb, ""))
        return out

    def _switch(self, arm, limb, side):
        """``(pose bone, property, the value meaning FK)`` of a limb's switch, or None."""
        for bone, prop, fk, lim in self.SWITCHES:
            if lim != limb or (("{s}" in bone + prop or "{side}" in bone + prop) != bool(side)):
                continue
            s = self.SIDE.get(side, "")
            pb = arm.pose.bones.get(bone.format(s=s, side=side.lower()))
            key = prop.format(s=s, side=side.lower())
            if pb is not None and key in pb:
                return pb, key, fk
        return None

    def is_ik(self, arm, limb, side) -> bool:
        sw = self._switch(arm, limb, side)
        return sw is not None and abs(float(sw[0][sw[1]]) - sw[2]) > 0.5

    def set_ik(self, arm, limb, side, ik: bool) -> None:
        """Flip a limb's switch (its drivers re-evaluate at the next update)."""
        sw = self._switch(arm, limb, side)
        if sw is None:
            return
        pb, key, fk = sw
        value = (1.0 - fk) if ik else fk
        pb[key] = int(value) if isinstance(pb[key], int) else float(value)
        arm.update_tag()

    def limb_bones(self, limb, side) -> list:
        s = self.SIDE.get(side, "")
        return [b.format(s=s) for b in self.LIMB_BONES.get(limb, ())]

    def ik_spec(self, arm, limb, side):
        """The limb's IK description with this rig's names, or None (no description, or a part
        of it missing on this rig)."""
        raw = self.IK.get(limb)
        if raw is None:
            return None
        s = self.SIDE.get(side, "")
        f = lambda n: n.format(s=s)                         # noqa: E731
        pbs = arm.pose.bones
        spec = {"target": tuple(map(f, raw["target"])), "root": f(raw["root"]), "mid": f(raw["mid"]),
                "end": f(raw["end"]), "twist": f(raw["twist"]) if raw.get("twist") else None,
                "pole": f(raw["pole"]) if raw.get("pole") else None,
                "pole_on": (f(raw["pole_on"][0]), raw["pole_on"][1]) if raw.get("pole_on") else None,
                "reset": [f(n) for n in raw.get("reset", ()) if f(n) in pbs],
                "extra": [(f(c), f(b)) for c, b in raw.get("extra", ()) if f(c) in pbs and f(b) in pbs]}
        need = [spec["target"][0], spec["target"][1], spec["root"], spec["mid"], spec["end"]]
        if any(n not in pbs for n in need) or (spec["twist"] and spec["twist"] not in pbs):
            return None
        return spec

    def pole_on(self, arm, spec) -> bool:
        if not spec["pole"] or spec["pole"] not in arm.pose.bones:
            return False
        if not spec.get("pole_on"):
            return True                         # no switch for it: the pole is always on
        pb = arm.pose.bones.get(spec["pole_on"][0])
        return pb is not None and float(pb.get(spec["pole_on"][1], 0.0)) > 0.5

    def ik_controls(self, arm, limb, side) -> list:
        """The controls an IK limb is posed and keyed through: target, chain turn or pole, toe,
        and the foot roll (keyed at rest)."""
        spec = self.ik_spec(arm, limb, side)
        if spec is None:
            return []
        out = [spec["target"][0]]
        if self.pole_on(arm, spec):
            out.append(spec["pole"])
        elif spec["twist"]:
            out.append(spec["twist"])
        out += [c for c, _b in spec["extra"]] + spec["reset"] + pins(arm, self, limb, side, spec)
        return out

    def fk_controls(self, arm, limb, side) -> list:
        bones = set(self.limb_bones(limb, side))
        return [c for c, b in self.controls(arm) if b in bones]

    def finger_chains(self, arm, side) -> list:
        """``[[skeleton bone, ...], ...]`` thumb to pinky for ``side``, by the rig's names."""
        s = self.SIDE[side]
        out = []
        for chain in self.FINGERS:
            bones = [n.format(s=s) for n in chain if n.format(s=s) in arm.data.bones]
            if len(bones) >= 2:
                out.append(bones)
        return out

    def finger_bone(self, arm, skeleton_bone) -> str | None:
        """The control a finger's skeleton bone is posed through."""
        return skeleton_bone if skeleton_bone in arm.pose.bones else None

    FINGERS = ()


class Rigify(Profile):
    name = "Rigify"

    SPINE = {"Hips": "ORG-spine", "Spine1": "ORG-spine.001", "Spine2": "ORG-spine.002",
             "Chest": "ORG-spine.003", "Neck1": "ORG-spine.004", "Neck2": "ORG-spine.005",
             "Head": "ORG-spine.006"}
    LIMB = {"Shoulder": "ORG-shoulder.{s}", "Arm": "ORG-upper_arm.{s}", "ForeArm": "ORG-forearm.{s}",
            "Hand": "ORG-hand.{s}", "Leg": "ORG-thigh.{s}", "Shin": "ORG-shin.{s}",
            "Foot": "ORG-foot.{s}", "ToeBase": "ORG-toe.{s}"}
    #: (control, skeleton bone), in the order they are set: parents before children
    ORDER = (
        ("torso", "ORG-spine"),
        ("spine_fk", "ORG-spine"), ("spine_fk.001", "ORG-spine.001"),
        ("spine_fk.002", "ORG-spine.002"), ("spine_fk.003", "ORG-spine.003"),
        ("neck", "ORG-spine.004"), ("head", "ORG-spine.006"),
        ("shoulder.{s}", "ORG-shoulder.{s}"),
        ("upper_arm_fk.{s}", "ORG-upper_arm.{s}"), ("forearm_fk.{s}", "ORG-forearm.{s}"),
        ("hand_fk.{s}", "ORG-hand.{s}"),
        ("thigh_fk.{s}", "ORG-thigh.{s}"), ("shin_fk.{s}", "ORG-shin.{s}"),
        ("foot_fk.{s}", "ORG-foot.{s}"), ("toe_fk.{s}", "ORG-toe.{s}"),
    )
    #: its FK spine pivots from its middle, the joints held by tweak bones: turned alone it put
    #: the chest 6 cm off, so these are placed as well as turned
    PLACED = ("spine_fk", "spine_fk.001", "spine_fk.002", "spine_fk.003")
    #: the IK/FK switch of each limb: the bone that holds it, and the value that means FK
    SWITCHES = (("upper_arm_parent.{s}", "IK_FK", 1.0, "arm"), ("thigh_parent.{s}", "IK_FK", 1.0, "leg"))
    LIMB_BONES = {"arm": ("ORG-upper_arm.{s}", "ORG-forearm.{s}", "ORG-hand.{s}"),
                  "leg": ("ORG-thigh.{s}", "ORG-shin.{s}", "ORG-foot.{s}", "ORG-toe.{s}")}
    #: its limbs in IK (super_limb): the hand or foot IK control carries the ORG hand or foot; the
    #: IK chain's first control turns only about its own length, which sets the elbow's or
    #: knee's plane while the pole is off (the default); with Pole Vector on, the pole does
    IK = {
        "arm": {"target": ("hand_ik.{s}", "ORG-hand.{s}"), "twist": "upper_arm_ik.{s}",
                "pole": "upper_arm_ik_target.{s}", "pole_on": ("upper_arm_parent.{s}", "pole_vector"),
                "root": "ORG-upper_arm.{s}", "mid": "ORG-forearm.{s}", "end": "ORG-hand.{s}"},
        "leg": {"target": ("foot_ik.{s}", "ORG-foot.{s}"), "twist": "thigh_ik.{s}",
                "pole": "thigh_ik_target.{s}", "pole_on": ("thigh_parent.{s}", "pole_vector"),
                "root": "ORG-thigh.{s}", "mid": "ORG-shin.{s}", "end": "ORG-foot.{s}",
                "reset": ("foot_spin_ik.{s}", "foot_heel_ik.{s}"), "extra": (("toe_ik.{s}", "ORG-toe.{s}"),)},
    }

    @staticmethod
    def detect(arm) -> bool:
        b = arm.data.bones
        return bool(arm.data.get("rig_id")) and "ORG-spine" in b and "torso" in b \
            and "ORG-upper_arm.L" in b

    def joints(self, arm) -> dict:
        out = {k: v for k, v in self.SPINE.items() if v in arm.data.bones}
        for side, s in SIDES:
            for j, bone in self.LIMB.items():
                name = bone.format(s=s)
                if name in arm.data.bones:
                    out[side + j] = name
        return out

    def controls(self, arm) -> list:
        out = []
        for c, b in self.ORDER:
            for s in (("L", "R") if "{s}" in c else ("",)):
                cn, bn = c.format(s=s), b.format(s=s)
                if cn in arm.pose.bones and bn in arm.pose.bones:
                    out.append((cn, bn))
        # the fingers: each ORG finger bone has an FK control of its own name
        for b in arm.data.bones:
            n = b.name
            if n.startswith("ORG-") and (n[4:].startswith("f_") or n[4:].startswith("thumb")
                                         or n[4:].startswith("palm")):
                if n[4:] in arm.pose.bones:
                    out.append((n[4:], n))
        return out

    #: the fingers, thumb to pinky, knuckle outward: Rigify's palm bones hang off the forearm's
    #: tweak, not the hand, so they are named, not found
    FINGERS = tuple(tuple(f"ORG-{n}.{{s}}" for n in chain) for chain in (
        ("thumb.01", "thumb.02", "thumb.03"), ("f_index.01", "f_index.02", "f_index.03"),
        ("f_middle.01", "f_middle.02", "f_middle.03"), ("f_ring.01", "f_ring.02", "f_ring.03"),
        ("f_pinky.01", "f_pinky.02", "f_pinky.03")))

    def finger_bone(self, arm, skeleton_bone) -> str | None:
        """The control a finger's skeleton bone is posed through."""
        c = skeleton_bone[4:] if skeleton_bone.startswith("ORG-") else None
        return c if c and c in arm.pose.bones else None


class AutoRigPro(Profile):
    name = "Auto-Rig Pro"
    SIDE = {"Left": "l", "Right": "r"}
    LIMB = {"Shoulder": "shoulder.{s}", "Arm": "arm.{s}", "ForeArm": "forearm.{s}", "Hand": "hand.{s}",
            "Leg": "thigh.{s}", "Shin": "leg.{s}", "Foot": "foot.{s}", "ToeBase": "toes_01.{s}"}
    #: (control, skeleton bone), parents first. The hips through the root master, which carries
    #: the spine with it (``c_root.x`` under it moves the pelvis alone)
    ORDER = (
        ("c_root_master.x", "root.x"),
        ("c_spine_01.x", "spine_01.x"), ("c_spine_02.x", "spine_02.x"), ("c_spine_03.x", "spine_03.x"),
        ("c_neck.x", "neck.x"), ("c_head.x", "head.x"),
        ("c_shoulder.{s}", "shoulder.{s}"),
        ("c_arm_fk.{s}", "arm.{s}"), ("c_forearm_fk.{s}", "forearm.{s}"), ("c_hand_fk.{s}", "hand.{s}"),
        ("c_thigh_fk.{s}", "thigh.{s}"), ("c_leg_fk.{s}", "leg.{s}"),
        ("c_foot_fk.{s}", "foot.{s}"), ("c_toes_fk.{s}", "toes_01.{s}"),
    )
    SWITCHES = (("c_hand_ik.{s}", "ik_fk_switch", 1.0, "arm"), ("c_foot_ik.{s}", "ik_fk_switch", 1.0, "leg"))
    LIMB_BONES = {"arm": ("arm.{s}", "forearm.{s}", "hand.{s}"),
                  "leg": ("thigh.{s}", "leg.{s}", "foot.{s}", "toes_01.{s}")}
    #: its limbs in IK (not in Marionette's copy): the hand or foot IK control carries the hand
    #: or foot, the pole is always on (no switch for it), the foot rolls from its roll cursor and
    #: its ball control (put back at rest), the toe has an IK control of its own. Without it, a
    #: take's IK controls stayed where the take began, and a limb switched to IK flew back there
    IK = {
        "arm": {"target": ("c_hand_ik.{s}", "hand.{s}"), "pole": "c_arms_pole.{s}",
                "root": "arm.{s}", "mid": "forearm.{s}", "end": "hand.{s}"},
        "leg": {"target": ("c_foot_ik.{s}", "foot.{s}"), "pole": "c_leg_pole.{s}",
                "root": "thigh.{s}", "mid": "leg.{s}", "end": "foot.{s}",
                "reset": ("c_foot_roll_cursor.{s}", "c_foot_01.{s}"),
                "extra": (("c_toes_ik.{s}", "toes_01.{s}"),)},
    }
    #: its finger controls are the fingers (the first phalanx's deform bone hangs under its own
    #: control); a finger has its metacarpal, the thumb starts at its first phalanx
    FINGERS = (("c_thumb1.{s}", "c_thumb2.{s}", "c_thumb3.{s}"),) + tuple(
        (f"c_{n}1_base.{{s}}", f"c_{n}1.{{s}}", f"c_{n}2.{{s}}", f"c_{n}3.{{s}}")
        for n in ("index", "middle", "ring", "pinky"))

    @staticmethod
    def detect(arm) -> bool:
        b = arm.data.bones
        return "c_traj" in b and "c_root_master.x" in b and "root.x" in b \
            and ("c_arm_fk.l" in b or "c_thigh_fk.l" in b)

    def joints(self, arm) -> dict:
        b = arm.data.bones
        spine = [n for n in ("spine_01.x", "spine_02.x", "spine_03.x") if n in b]
        out = {"Hips": "root.x"}
        if spine:
            out["Spine1"] = spine[0]
            if len(spine) >= 3:
                out["Spine2"] = spine[1]
            if len(spine) >= 2:
                out["Chest"] = spine[-1]
        for j, n in (("Neck1", "neck.x"), ("Head", "head.x")):
            if n in b:
                out[j] = n
        for side, s in self.SIDE.items():
            for j, bone in self.LIMB.items():
                name = bone.format(s=s)
                if name in b:
                    out[side + j] = name
        return {j: n for j, n in out.items() if n in b}

    def controls(self, arm) -> list:
        out = []
        for c, b in self.ORDER:
            for s in (("l", "r") if "{s}" in c else ("",)):
                cn, bn = c.format(s=s), b.format(s=s)
                if cn in arm.pose.bones and bn in arm.pose.bones:
                    out.append((cn, bn))
        for side in ("Left", "Right"):
            for chain in self.finger_chains(arm, side):
                out.extend((n, n) for n in chain)
        return out


class CloudRig(Profile):
    name = "CloudRig"
    SIDE = {"Left": "L", "Right": "R"}
    SPINE = {"Hips": "Hips", "Spine1": "Spine", "Chest": "Chest", "Neck1": "Neck", "Head": "Head"}
    LIMB = {"Shoulder": "Shoulder.{s}", "Arm": "UpperArm.{s}", "ForeArm": "Forearm.{s}", "Hand": "Hand.{s}",
            "Leg": "Thigh.{s}", "Shin": "Knee.{s}", "Foot": "Foot.{s}", "ToeBase": "Toes.{s}"}
    #: (control, skeleton bone), parents first: the hips through the torso, which carries the
    #: spine and the legs' FK with it
    ORDER = (
        ("TORSO-Spine", "Hips"),
        ("FK-Spine", "Spine"), ("FK-Chest", "Chest"), ("FK-Neck", "Neck"), ("FK-Head", "Head"),
        ("FK-Shoulder.{s}", "Shoulder.{s}"),
        ("FK-UpperArm.{s}", "UpperArm.{s}"), ("FK-Forearm.{s}", "Forearm.{s}"), ("FK-Hand.{s}", "Hand.{s}"),
        ("FK-Thigh.{s}", "Thigh.{s}"), ("FK-Knee.{s}", "Knee.{s}"),
        ("FK-Foot.{s}", "Foot.{s}"), ("FK-Toes.{s}", "Toes.{s}"),
    )
    #: its FK spine turns about the middle of each spine bone, not the joint: turned alone it
    #: put the chest 3 cm off, so these are placed as well as turned (their location is free)
    PLACED = ("FK-Spine", "FK-Chest")
    #: the switches on its Properties bone: 1 is IK
    SWITCHES = (("Properties", "ik_{side}_arm", 0.0, "arm"), ("Properties", "ik_{side}_leg", 0.0, "leg"),
                ("Properties", "Spine_ik", 0.0, "spine"))
    LIMB_BONES = {"arm": ("UpperArm.{s}", "Forearm.{s}", "Hand.{s}"),
                  "leg": ("Thigh.{s}", "Knee.{s}", "Foot.{s}", "Toes.{s}"),
                  "spine": ("Hips", "Spine", "Chest")}
    FINGERS = (("Finger_Thumb1.{s}", "Finger_Thumb2.{s}", "Finger_Thumb3.{s}"),) + tuple(
        (f"Finger_{n}_Carpal.{{s}}", f"Finger_{n}1.{{s}}", f"Finger_{n}2.{{s}}", f"Finger_{n}3.{{s}}")
        for n in ("Index", "Middle", "Ring", "Pinky"))

    @staticmethod
    def detect(arm) -> bool:
        b = arm.data.bones
        return bool(arm.data.get("is_generated_cloudrig")) and "TORSO-Spine" in b \
            and ("FK-UpperArm.L" in b or "FK-Thigh.L" in b)

    def joints(self, arm) -> dict:
        b = arm.data.bones
        out = {j: n for j, n in self.SPINE.items() if n in b}
        for side, s in self.SIDE.items():
            for j, bone in self.LIMB.items():
                if bone.format(s=s) in b:
                    out[side + j] = bone.format(s=s)
        return out

    def controls(self, arm) -> list:
        out = []
        for c, b in self.ORDER:
            for s in (("L", "R") if "{s}" in c else ("",)):
                cn, bn = c.format(s=s), b.format(s=s)
                if cn in arm.pose.bones and bn in arm.pose.bones:
                    out.append((cn, bn))
        for side in ("Left", "Right"):
            for chain in self.finger_chains(arm, side):
                out.extend((self.finger_bone(arm, n) or n, n) for n in chain)
        return out

    def finger_bone(self, arm, skeleton_bone) -> str | None:
        c = "FK-" + skeleton_bone
        return c if c in arm.pose.bones else None


class Mapped(Profile):
    """A rig Marionette connected by asking it (its ``rig_connect``): its map says which control
    moves which skeleton bone, each limb's IK and its switch -- the same answers a family's
    profile gives by name, for a rig of no family."""

    FIT_MOVER = True

    def __init__(self, m):
        self.map = m
        self.name = m.get("name") or "Control"
        self.PLACED = tuple(m.get("placed", ()))
        self._limbs = {(e["limb"], e["side"]): e for e in m.get("limbs", ())}

    def joints(self, arm) -> dict:
        return {j: b for j, b in self.map.get("joints", {}).items() if b in arm.data.bones}

    def controls(self, arm) -> list:
        pbs = arm.pose.bones
        return [(c, b) for c, b in self.map.get("pairs", ()) if c in pbs and b in pbs]

    def limbs(self, arm) -> list:
        return [k for k, e in self._limbs.items() if e.get("ik") or self._switch(arm, *k) is not None]

    def _switch(self, arm, limb, side):
        e = self._limbs.get((limb, side))
        sw = e and e.get("switch")
        if not sw:
            return None
        h = (arm.pose.bones.get(sw["bone"]) if sw["on"] == "bone" else
             arm if sw["on"] == "object" else arm.data)
        if h is None or sw["prop"] not in h:
            return None
        return h, sw["prop"], float(sw["fk"])

    def is_ik(self, arm, limb, side) -> bool:
        if self._switch(arm, limb, side) is not None:
            return super().is_ik(arm, limb, side)
        e = self._limbs.get((limb, side))
        return bool(e and e.get("ik"))          # a limb with IK and no switch is always in IK

    def ik_limbs(self, arm) -> list:
        return [f"{side[:1]} {limb}" if side else limb for limb, side in self.limbs(arm)
                if self.is_ik(arm, limb, side)]

    def limb_bones(self, limb, side) -> list:
        e = self._limbs.get((limb, side))
        return list(e["bones"]) if e else []

    def ik_spec(self, arm, limb, side):
        e = self._limbs.get((limb, side))
        raw = e and e.get("ik")
        if not raw:
            return None
        pbs = arm.pose.bones
        spec = {"target": tuple(raw["target"]), "root": raw["root"], "mid": raw["mid"], "end": raw["end"],
                "twist": raw.get("twist") if (raw.get("twist") or "") in pbs else None,
                "twist_axis": raw.get("twist_axis") or "Y",
                "pole": raw.get("pole") if (raw.get("pole") or "") in pbs else None, "pole_on": None,
                "reset": [n for n in raw.get("reset", ()) if n in pbs],
                "extra": [(c, b) for c, b in raw.get("extra", ()) if c in pbs and b in pbs]}
        if any(n not in pbs for n in (spec["target"][0], spec["target"][1], spec["root"], spec["mid"], spec["end"])):
            return None
        return spec

    def finger_chains(self, arm, side) -> list:
        bones = arm.data.bones
        return [ch for ch in self.map.get("fingers", {}).get(side, ()) if all(b in bones for b in ch)]

    def finger_bone(self, arm, skeleton_bone) -> str | None:
        c = self.map.get("finger_controls", {}).get(skeleton_bone)
        return c if c and c in arm.pose.bones else None


PROFILES = (Rigify, AutoRigPro, CloudRig)
_CACHE: dict = {}


def profile(arm):
    """The control-rig profile for ``arm``, or None: the map Marionette connected on it first
    -- a rig corrected by hand wins over its family's profile -- then its family's, then a map
    from Marionette's library that fits it."""
    if arm is None or arm.type != 'ARMATURE':
        return None
    try:
        rm = getattr(arm, "autoposer_rig_map", None)
        key = (arm.as_pointer(), arm.data.name, len(arm.data.bones), rm.rev if rm is not None else 0)
    except ReferenceError:
        return None
    if key not in _CACHE:
        # the maps are Marionette's (its Rig Connection): read through it when it is on
        from . import posing
        rig_connect = posing.module("rig_connect")
        m = _ask(rig_connect, "as_dict", arm)
        found = Mapped(m) if m else next((P() for P in PROFILES if P.detect(arm)), None)
        if found is None:
            m = _ask(rig_connect, "library_match", arm)
            found = Mapped(m) if m else None
        _CACHE[key] = found
    return _CACHE[key]


def _ask(rig_connect, name, arm):
    """A map from Marionette's ``rig_connect``, or None (no Marionette, an older one, or a map
    it cannot read)."""
    fn = getattr(rig_connect, name, None)
    if fn is None:
        return None
    try:
        m = fn(arm)
    except Exception:                                   # noqa: BLE001
        return None
    return m if isinstance(m, dict) and m.get("pairs") else None


# ---------------------------------------------------------------------------- posing it
def _depth(arm, name) -> int:
    d, b = 0, arm.data.bones[name].parent
    while b is not None:
        d, b = d + 1, b.parent
    return d


def _after(arm, name, memo, _busy=None) -> int:
    """How many bones ``name`` waits on: 1 + the most any of its parent or its constraints'
    targets waits on. Memoised in ``memo``; a cycle counts as nothing more."""
    if name in memo:
        return memo[name]
    busy = _busy if _busy is not None else set()
    if name in busy:
        return 0
    busy.add(name)
    pb = arm.pose.bones.get(name)
    d = 0
    if pb is not None:
        deps = [pb.parent.name] if pb.parent is not None else []
        for con in pb.constraints:
            if con.mute or getattr(con, "target", None) is not arm and con.type != 'ARMATURE':
                continue
            if con.type == 'ARMATURE':
                deps += [t.subtarget for t in con.targets if t.target is arm and t.subtarget]
            elif getattr(con, "subtarget", ""):
                deps.append(con.subtarget)
        for dep in deps:
            d = max(d, 1 + _after(arm, dep, memo, busy))
    busy.discard(name)
    memo[name] = d
    return d


#: limbs switched from IK to FK to pose them, by rig: their switch is keyed at the next
#: key (`key`), so the animation keeps the mode they are posed in
SWITCHED: dict = {}


def _keyed_frames(arm, names) -> set:
    """The frames the rig's action keys any of ``names`` (pose bones) at."""
    ad = arm.animation_data
    act = ad.action if ad else None
    if act is None or not names:
        return set()
    try:
        from bpy_extras import anim_utils
        bag = anim_utils.action_get_channelbag_for_slot(act, ad.action_slot)
        curves = list(bag.fcurves) if bag is not None else []
    except (ImportError, AttributeError):
        curves = list(getattr(act, "fcurves", ()))
    want = {f'pose.bones["{n}"]' for n in names}
    out = set()
    for fc in curves:
        head = fc.data_path.split("]", 1)[0] + "]" if fc.data_path.startswith("pose.bones[") else ""
        if head in want:
            out.update(round(k.co.x) for k in fc.keyframe_points)
    return out


def _ik_only_animation(arm, prof, limb, side) -> bool:
    """Whether the limb's IK controls are keyed where its FK controls are not: an animation made
    in IK, which switching the limb to FK would change on every other frame."""
    ik = _keyed_frames(arm, prof.ik_controls(arm, limb, side))
    return bool(ik - _keyed_frames(arm, prof.fk_controls(arm, limb, side)))


def to_fk(arm, prof, want) -> list:
    """Switch to FK each limb in IK that ``want`` poses, its FK matched first so the switch moves
    nothing: a pose is put on through FK, where it lands as it was asked (an IK limb is the
    rig's solver's to bend: stretch, a pole's plane). Left in IK: a limb with no FK
    controls, and one whose IK carries animation its FK does not (switching would change the
    other frames). The limbs switched, ``[(limb, side)]``; their switch is keyed at the next key."""
    out = []
    for limb, side in prof.limbs(arm):
        if not prof.is_ik(arm, limb, side) or prof._switch(arm, limb, side) is None:
            continue
        bones = prof.limb_bones(limb, side)
        if not any(b in want for b in bones) or not prof.fk_controls(arm, limb, side):
            continue
        if _ik_only_animation(arm, prof, limb, side):
            continue
        match(arm, prof, limb, side)
        prof.set_ik(arm, limb, side, False)
        out.append((limb, side))
    if out:
        bpy.context.view_layer.update()
        SWITCHED.setdefault(arm.name, set()).update(out)
    return out


def write(arm, prof, want: dict, *, passes: int = 3, place_first: bool = True, switch: bool = True,
          stats: dict | None = None) -> float:
    """Pose the rig so its skeleton bones reach ``want`` -- ``{skeleton bone: armature-space
    pose matrix}`` -- by setting its FK controls: a limb in IK is switched to FK first (`to_fk`,
    ``switch``), matched so nothing moves. One that stays in IK (its IK animated on its own, or
    ``switch`` False) is posed through its IK controls (`write_ik`), when the profile describes
    its IK. Returns the worst miss left (metres). ``place_first`` False: the first control is
    turned only, like the rest (a limb on its own, `match`, whose first control is not the hips').
    ``stats``, if given, gets ``"pass"``: the pass the FK controls were left at (0: as first set,
    the corrections after it no help)."""
    if switch:
        to_fk(arm, prof, want)
    ik_now = [(limb, side) for limb, side in prof.limbs(arm)
              if prof.is_ik(arm, limb, side) and prof.ik_spec(arm, limb, side) is not None
              and any(b in want for b in prof.limb_bones(limb, side))]
    by_ik = {b for limb, side in ik_now for b in prof.limb_bones(limb, side)}
    pairs = [(c, b) for c, b in prof.controls(arm) if b in want and b not in by_ik]
    worst = 0.0
    if pairs:
        worst = _write_fk(arm, prof, want, pairs, passes, place_first, stats=stats)
    if ik_now:
        worst = max(worst, write_ik(arm, prof, ik_now, want))
    return worst


def _write_fk(arm, prof, want, pairs, passes, place_first, *, stats=None) -> float:
    # set level by level, each control after everything it hangs from -- its parents, and what
    # its parents' constraints follow (CloudRig's FK arm hangs off a hinge bone with no parent,
    # carried by an Armature constraint from the chest; Rigify's FK hips hang under the FK spine)
    memo: dict = {}
    levels: dict = {}
    for c, b in pairs:
        levels.setdefault(_after(arm, c, memo), []).append((c, b))
    mover = pairs[0][0] if place_first else None    # the hips' control: placed, not just turned
    bones = arm.data.bones
    free = set(getattr(prof, "PLACED", ()))
    # a limb in IK with no IK description: its FK controls are set (the pose is there when the
    # animator switches) but do not move its bones, so they are never corrected from them
    deaf = prof.ik_bones(arm) if hasattr(prof, "ik_bones") else set()
    fit = mover is not None and getattr(prof, "FIT_MOVER", False)
    if fit:
        passes = max(passes, 5)
    worst = 0.0
    last = None
    best = None                     # (miss, pass, the controls as that pass left them)
    for p in range(passes):
        for d in sorted(levels):
            for c, b in levels[d]:
                pb = arm.pose.bones[c]
                if fit and c == mover and p > 0:
                    continue                # set by the fit from here on (`_fit_mover`)
                if p == 0 or b in deaf or c == b:
                    # the control where it must be for its bone to land on ``want``: the rest
                    # relation between the two carried over
                    C = want[b] @ (bones[b].matrix_local.inverted() @ bones[c].matrix_local)
                else:
                    # then corrected by what the bone read back: whatever lies between the
                    # control and its bone (a pelvis control posed under the root, a
                    # constraint's offset) is taken out
                    C = want[b] @ arm.pose.bones[b].matrix.inverted() @ pb.matrix
                # ``C`` is where the control must END UP; what it is set to is that less what
                # its own constraints do (CloudRig's toes ride an Armature constraint)
                if pb.constraints:
                    M_set = arm.convert_space(pose_bone=pb, matrix=pb.matrix_basis,
                                              from_space='LOCAL', to_space='POSE')
                    C = M_set @ pb.matrix.inverted() @ C
                # the hips' control is placed, and the few a profile names (`PLACED`); every other
                # one turns, its place following from the bones above it -- no stray location
                # keys on an arm
                if c != mover and not (c in free and not any(pb.lock_location)):
                    C = C.copy()
                    C.translation = pb.matrix.translation if not pb.constraints else M_set.translation
                pb.matrix = C
            bpy.context.view_layer.update()
        if fit and p < passes - 1:
            _fit_mover(arm, arm.pose.bones[mover], want, pairs)
            bpy.context.view_layer.update()
        live = [b for _c, b in pairs if b not in deaf]
        worst = max(((want[b].translation - arm.pose.bones[b].matrix.translation).length for b in live),
                    default=0.0)
        if best is not None and worst >= best[0]:
            # a correction that made it worse (Rigify's FK spine, which pivots off its joints, reads
            # back further off each pass): the best pass put back
            for c, basis in best[2].items():
                arm.pose.bones[c].matrix_basis = basis
            bpy.context.view_layer.update()
            worst = best[0]
            break
        best = (worst, p, {c: arm.pose.bones[c].matrix_basis.copy() for c, _b in pairs})
        if worst < 1e-4 and (p > 0 or not fit):
            break                   # landed (read back after the last level: nothing to correct)
        # hardly closer than the pass before: another will not help (Rigify's FK spine pivots off
        # its joints, and keeps a few mm whatever is done)
        if last is not None and worst > last * 0.8:
            break
        last = worst
    if stats is not None and best is not None:
        stats["pass"] = best[1]
    return worst


def _fit_mover(arm, pb, want, pairs) -> None:
    """Set the body's control by what it carries rigidly: the heads of the controls hung right
    under it (each turns about its own head, so its head goes only where the body takes it), to
    where their bones need them. A rigid fit (least squares), pulled lightly toward the turn it
    has (two heads leave a roll about the line between them open). Rigs whose body control sits
    away from the hips -- a pelvis turned by its own control under it -- swing the hips with any
    turn of the body that only one bone was set by."""
    import numpy as np
    pbs, bones = arm.pose.bones, arm.data.bones
    have, need = [], []
    for c, b in pairs:
        cp = pbs[c]
        if c == pb.name or cp.parent is None or cp.parent.name != pb.name:
            continue
        rel = bones[b].matrix_local.inverted() @ bones[c].matrix_local
        have.append(tuple(cp.matrix.translation))
        need.append(tuple((want[b] @ rel).translation))
    if len(have) < 2:
        return
    w = [1.0] * len(have)
    # the pull toward the turn it has: its own axes, a body's length out, staying put
    size = max(np.linalg.norm(np.subtract(have[0], have[1])), 1e-3) * 4.0
    M = pb.matrix
    for k in range(3):
        q = tuple(M.translation + M.col[k].xyz.normalized() * size)
        have.append(q)
        need.append(q)
        w.append(1e-3)
    have, need, w = np.array(have), np.array(need), np.array(w)[:, None]
    hc, nc = (have * w).sum(0) / w.sum(), (need * w).sum(0) / w.sum()
    u, _s, vt = np.linalg.svd(((have - hc) * w).T @ (need - nc))
    d = np.sign(np.linalg.det(vt.T @ u.T))
    R = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    t = nc - R @ hc
    D = Matrix(((R[0, 0], R[0, 1], R[0, 2], t[0]), (R[1, 0], R[1, 1], R[1, 2], t[1]),
                (R[2, 0], R[2, 1], R[2, 2], t[2]), (0.0, 0.0, 0.0, 1.0)))
    _set_to(arm, pb, D @ pb.matrix)


def _set_to(arm, pb, C) -> None:
    """Put a control where it must END UP, less what its own constraints do."""
    if pb.constraints:
        M_set = arm.convert_space(pose_bone=pb, matrix=pb.matrix_basis, from_space='LOCAL', to_space='POSE')
        C = M_set @ pb.matrix.inverted() @ C
    pb.matrix = C


def _unscaled(M) -> Matrix:
    """``M`` placed and turned, at unit scale: an IK target is only ever moved and turned. Set
    with the scale its bone reads back relative to it, the scale compounded frame after frame
    (Auto-Rig Pro's foot copies its IK control's scale and stretches: a foot degenerated by
    frame 19 of a take)."""
    return Matrix.LocRotScale(M.translation, M.to_quaternion(), None)


def _plane_turn(arm, spec, want) -> float:
    """The signed angle (radians) about the root-to-end line that brings the middle joint (elbow,
    knee) round to where ``want`` has it; 0 for a limb straight enough to have no plane."""
    pbs = arm.pose.bones
    r = pbs[spec["root"]].matrix.translation
    axis = pbs[spec["end"]].matrix.translation - r
    if axis.length < 1e-6:
        return 0.0
    axis = axis.normalized()

    def across(v):
        v = v - r
        return v - axis * v.dot(axis)
    have, need = across(pbs[spec["mid"]].matrix.translation), across(want[spec["mid"]].translation)
    if have.length < 1e-4 or need.length < 1e-4:
        return 0.0
    a = have.angle(need)
    return a if axis.dot(have.cross(need)) >= 0.0 else -a


def write_ik(arm, prof, limbs, want, passes: int = 4, tol: float = 1e-4) -> float:
    """Pose limbs in IK (``[(limb, side)]``) through their IK controls: the foot roll back at
    rest, the target where the hand or foot must be (with its turn), the toe, then the chain
    turned about its own length (or the pole moved round, with the rig's pole on) until the
    elbow or knee is where ``want`` has it (within ``tol``, metres). All limbs a step at a time,
    one update a step. Returns the worst miss left (metres): the hand, foot, toe and elbow or
    knee."""
    pbs = arm.pose.bones
    specs = []
    for limb, side in limbs:
        spec = prof.ik_spec(arm, limb, side)
        if spec is not None and spec["end"] in want:
            specs.append((spec, prof.pole_on(arm, spec)))
    if not specs:
        return 0.0
    for spec, _pole in specs:
        for name in spec["reset"]:
            pbs[name].matrix_basis = Matrix.Identity(4)
    update = bpy.context.view_layer.update
    update()
    # a pole goes with its elbow or knee, as a rig's own IK/FK snap puts it: out from it in the
    # limb's plane, as far as it rests from it (only turned round, it stayed where it was)
    placed = False
    for spec, pole in specs:
        if pole and all(b in want for b in (spec["root"], spec["mid"], spec["end"])):
            placed = _place_pole(arm, spec, want) or placed
    if placed:
        update()

    def worst():
        out = 0.0
        for spec, _pole in specs:
            for b in [spec["end"], spec["mid"]] + [b for _c, b in spec["extra"]]:
                if b in want:
                    out = max(out, (want[b].translation - pbs[b].matrix.translation).length)
        return out
    miss = 0.0
    for _p in range(passes):
        for spec, _pole in specs:
            ctrl, bone = spec["target"]
            _set_to(arm, pbs[ctrl], _unscaled(want[bone] @ pbs[bone].matrix.inverted() @ pbs[ctrl].matrix))
        update()
        for spec, _pole in specs:
            for ctrl, bone in spec["extra"]:
                if bone in want:
                    _set_to(arm, pbs[ctrl], _unscaled(want[bone] @ pbs[bone].matrix.inverted() @ pbs[ctrl].matrix))
        if any(spec["extra"] for spec, _pole in specs):
            update()
        # the elbow's or knee's plane: turned by its own amount, then the turn checked once and
        # turned back the other way if it went the wrong way round (a control's axis may run
        # either way along the limb)
        for _t in range(2):
            turns = []
            for spec, pole in specs:
                if spec["mid"] not in want or (not pole and not spec["twist"]):
                    continue
                a = _plane_turn(arm, spec, want)
                if abs(a) > 1e-3:
                    turns.append((spec, pole, a, _turn(arm, spec, pole, a)))
            if not turns:
                break
            update()
            redo = False
            for spec, pole, a, undo in turns:
                after = _plane_turn(arm, spec, want)
                if abs(after) > abs(a) * 0.9:           # the wrong way round: back, and the other way
                    undo()
                    _turn(arm, spec, pole, -a)
                    redo = True
            if redo:
                update()
        last, miss = miss, worst()
        if miss < tol or (_p > 0 and miss > last * 0.8):
            break                       # there, or no pass bringing it much closer
    if _place_pins(arm, prof, limbs, want):
        update()
        miss = worst()
    return miss


_PINS: dict = {}


def pins(arm, prof, limb, side, spec) -> list:
    """A limb's pins: controls resting on its elbow or knee that nothing in the limb carries and
    that follow nothing (Auto-Rig Pro's ``c_stretch_arm_pin``, ``c_stretch_leg_pin``). They are
    set back on the joint with the IK; left alone they float where the limb was."""
    bones = arm.data.bones
    key = (arm.data.as_pointer(), limb, side, len(bones))
    if key in _PINS:
        return [p for p in _PINS[key] if p in bones]
    mid = bones.get(spec["mid"])
    out = []
    if mid is not None:
        movers = {spec["root"], spec["mid"], spec["end"], spec["target"][0], spec.get("pole") or "",
                  spec.get("twist") or ""} | {c for c, _b in spec["extra"]} | set(spec["reset"])
        movers |= set(prof.fk_controls(arm, limb, side)) | set(prof.limb_bones(limb, side))
        for pb in arm.pose.bones:
            b = pb.bone
            if pb.custom_shape is None or b.name in movers or pb.constraints \
                    or (b.head_local - mid.head_local).length > 0.03:
                continue
            p, carried = b.parent, False
            while p is not None and not carried:
                carried, p = p.name in movers, p.parent
            if not carried:
                out.append(b.name)
    _PINS[key] = out
    return out


def _place_pins(arm, prof, limbs, want) -> bool:
    """Each limb's pins back on its elbow or knee where ``want`` has it, as they rest on it."""
    pbs, bones = arm.pose.bones, arm.data.bones
    placed = False
    for limb, side in limbs:
        spec = prof.ik_spec(arm, limb, side)
        if spec is None or spec["mid"] not in want:
            continue
        rest = bones[spec["mid"]].matrix_local.inverted()
        for p in pins(arm, prof, limb, side, spec):
            _set_to(arm, pbs[p], want[spec["mid"]] @ rest @ bones[p].matrix_local)
            placed = True
    return placed


def _place_pole(arm, spec, want) -> bool:
    """Put a limb's pole out from where ``want`` has its elbow or knee, in the limb's plane (the
    way it bends), at the distance the pole rests from it. False for a limb too straight to say."""
    pbs, bones = arm.pose.bones, arm.data.bones
    r, m, e = (want[spec[k]].translation for k in ("root", "mid", "end"))
    axis = e - r
    if axis.length < 1e-6:
        return False
    axis = axis.normalized()
    out = (m - r) - axis * (m - r).dot(axis)
    if out.length < 0.01:                       # straight: no plane to put it in
        return False
    dist = (bones[spec["pole"]].head_local - bones[spec["mid"]].head_local).length
    pb = pbs[spec["pole"]]
    C = pb.matrix.copy()
    C.translation = m + out.normalized() * dist
    _set_to(arm, pb, C)
    return True


def _turn(arm, spec, pole, a):
    """Turn a limb's plane by ``a`` (radians): its chain control about its own length, or its
    pole round the root-to-end line. Returns how to undo it."""
    pbs = arm.pose.bones
    if pole:
        pb = pbs[spec["pole"]]
        r = pbs[spec["root"]].matrix.translation
        axis = (pbs[spec["end"]].matrix.translation - r).normalized()
        before = pb.matrix.copy()
        C = before.copy()
        C.translation = r + Matrix.Rotation(a, 3, axis) @ (before.translation - r)
        _set_to(arm, pb, C)
        return lambda: _set_to(arm, pb, before)
    pb = pbs[spec["twist"]]
    axis = spec.get("twist_axis") or "Y"
    i = "XYZ".index(axis)
    before = pb.matrix_basis.copy()
    if pb.rotation_mode not in ('QUATERNION', 'AXIS_ANGLE') and not pb.lock_rotation[i]:
        e = pb.rotation_euler.copy()
        e[i] += a
        pb.rotation_euler = e
    else:
        pb.matrix_basis = before @ Matrix.Rotation(a, 4, axis)

    def undo():
        pb.matrix_basis = before
    return undo


def match(arm, prof, limb, side) -> float:
    """Set a limb's OTHER mode to the pose it shows now -- its FK controls from its IK, or its
    IK controls from its FK -- so that switching it shows the same pose. The switch is flipped
    to set them (each mode is only read back through its own) and put back. The worst miss
    left (metres). Setting FK from IK needs no IK description; setting IK from FK does."""
    ik = prof.is_ik(arm, limb, side)
    if not ik and prof.ik_spec(arm, limb, side) is None:
        return 0.0
    pbs = arm.pose.bones
    want = {b: pbs[b].matrix.copy() for b in prof.limb_bones(limb, side) if b in pbs}
    prof.set_ik(arm, limb, side, not ik)
    bpy.context.view_layer.update()
    try:
        if ik:
            pairs = [(c, b) for c, b in prof.controls(arm) if b in want]
            miss = _write_fk(arm, prof, want, pairs, 3, False) if pairs else 0.0
        else:
            miss = write_ik(arm, prof, [(limb, side)], want)
    finally:
        prof.set_ik(arm, limb, side, ik)
        bpy.context.view_layer.update()
    return miss


def match_all(arm, prof, limbs) -> float:
    """`match` for several limbs at once: their switches flipped together and their other mode
    set in one solve (one update a step for all of them, where one limb at a time took one
    each). The worst miss left (metres)."""
    pbs = arm.pose.bones
    was = {}
    for limb, side in limbs:
        ik = prof.is_ik(arm, limb, side)
        if ik or prof.ik_spec(arm, limb, side) is not None:
            was[(limb, side)] = ik
    if not was:
        return 0.0
    want = {b: pbs[b].matrix.copy() for limb, side in was for b in prof.limb_bones(limb, side) if b in pbs}
    for (limb, side), ik in was.items():
        prof.set_ik(arm, limb, side, not ik)
    bpy.context.view_layer.update()
    miss = 0.0
    try:
        fk_from = {b for (limb, side), ik in was.items() if ik for b in prof.limb_bones(limb, side)}
        pairs = [(c, b) for c, b in prof.controls(arm) if b in want and b in fk_from]
        if pairs:
            miss = _write_fk(arm, prof, want, pairs, 3, False)
        ik_from = [k for k, ik in was.items() if not ik]
        if ik_from:
            # a millimetre: it shows only when the animator switches the limb back to IK
            miss = max(miss, write_ik(arm, prof, ik_from, want, tol=1e-3))
    finally:
        for (limb, side), ik in was.items():
            prof.set_ik(arm, limb, side, ik)
        bpy.context.view_layer.update()
    return miss


def follow(arm, prof) -> float:
    """Each limb's other mode set to the pose it shows -- an FK limb's IK target and pole where
    its hand or foot and elbow or knee are -- without a key: the rig's own controls go with the
    limb whatever mode it is in. The worst miss left (metres)."""
    worst = 0.0
    for limb, side in prof.limbs(arm):
        worst = max(worst, match(arm, prof, limb, side))
    return worst


def matching(scene) -> bool:
    """Whether a key matches each limb's other mode too (Marionette's Settings > Keep IK and FK
    Matched; always, without Marionette)."""
    own = getattr(scene, "animatica_autoposer", None)
    return bool(getattr(own, "match_ik_fk", True)) if own is not None else True


def key(arm, prof, frame, *, match_modes=None) -> int:
    """Key the controls the pose is set through, at ``frame``: each limb's in the mode it is
    in, and -- with ``match_modes`` (by default the scene's Keep IK and FK Matched) -- its other
    mode matched to the same pose and keyed too. The bones keyed."""
    if match_modes is None:
        match_modes = matching(bpy.context.scene)
    names = list(dict.fromkeys(prof.keyed(arm)))
    limbs = [(limb, side) for limb, side in prof.limbs(arm) if prof.ik_spec(arm, limb, side) is not None]
    if match_modes and limbs:
        match_all(arm, prof, limbs)
    for limb, side in limbs:
        ik = prof.is_ik(arm, limb, side)
        fk_c, ik_c = prof.fk_controls(arm, limb, side), prof.ik_controls(arm, limb, side)
        if ik and not match_modes:
            names = [n for n in names if n not in fk_c]
        if ik or match_modes:
            names += [n for n in ik_c if n not in names]
    n = 0
    for c in names:
        pb = arm.pose.bones.get(c)
        if pb is None:
            continue
        path = ("rotation_quaternion" if pb.rotation_mode == 'QUATERNION' else
                "rotation_axis_angle" if pb.rotation_mode == 'AXIS_ANGLE' else "rotation_euler")
        pb.keyframe_insert(path, frame=frame)
        if not all(pb.lock_location):
            pb.keyframe_insert("location", frame=frame)
        n += 1
    # a limb switched to FK to pose it: its switch keyed here, so the take keeps it
    for limb, side in SWITCHED.pop(arm.name, ()):
        sw = prof._switch(arm, limb, side)
        if sw is not None:
            holder, prop, _fk = sw
            holder.keyframe_insert(f'["{prop}"]', frame=frame)
    return n


def all_fk(arm, prof) -> list:
    """Put every limb with an IK description in FK, for reading the rig's FK pose (Reset to
    T-Pose reads its controls at rest). The limbs that were in IK, for `restore`."""
    was = [(limb, side) for limb, side in prof.limbs(arm)
           if prof.ik_spec(arm, limb, side) is not None and prof.is_ik(arm, limb, side)]
    for limb, side in was:
        prof.set_ik(arm, limb, side, False)
    if was:
        bpy.context.view_layer.update()
    return was


def restore(arm, prof, was) -> None:
    for limb, side in was:
        prof.set_ik(arm, limb, side, True)
    if was:
        bpy.context.view_layer.update()
