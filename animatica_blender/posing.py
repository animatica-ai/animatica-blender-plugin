# SPDX-License-Identifier: GPL-3.0-or-later
"""Animatica Autoposer Pro, when it is installed alongside: the one place this add-on reaches it.

The two are separate products: this one makes motion, the Autoposer poses. Installed together,
the Autoposer's tool is on the bar, an edit with it carries through the frames around it, the
ghosts and the zoetrope can be posed, and a pose keys into the take. Without it, everything
here that poses (the Autopose tool, dragging a ghost or the motion trail) is not offered, and
what needs to know which bone is a hand or a foot reads it off the bone names.
"""

from __future__ import annotations

import importlib
import sys

import bpy

#: the Autoposer's package name, as a legacy add-on or an extension (``bl_ext.<repo>.<id>``)
PRO_ID = "animatica_autoposer"
#: where to get it, said where a posing feature would be
PRO_NAME = "Animatica Autoposer Pro"
#: the bridge with the Autoposer (posing.py here, host.py there): the two combine only when both
#: say the same number, so an older or newer Autoposer leaves this add-on working on its own
BRIDGE = 1


def package():
    """The Autoposer's package if it is installed, enabled and speaks this bridge, else None."""
    try:
        enabled = {a.module for a in bpy.context.preferences.addons}
    except AttributeError:
        return None
    for name in enabled:
        if name == PRO_ID or name.endswith("." + PRO_ID):
            mod = sys.modules.get(name)
            if mod is not None and getattr(mod, "BRIDGE", None) == BRIDGE:
                return mod
    return None


def present() -> bool:
    return package() is not None


def module(name: str):
    """A module of the Autoposer (``handles``, ``autoposer.poser``...), or None without it."""
    pkg = package()
    if pkg is None:
        return None
    try:
        return importlib.import_module(f"{pkg.__name__}.{name}")
    except ImportError:
        return None


def handles():
    return module("handles")


def poser():
    return module("autoposer.poser")


def engine():
    return module("autoposer.engine")


def tool_active(context) -> bool:
    h = handles()
    return bool(h is not None and h.tool_active(context))


def dragging() -> bool:
    """A handle drag under way (the ghosts are being re-posed by it)."""
    h = handles()
    return bool(h is not None and h._drag.get("active"))


def draw_controls(layout, context) -> bool:
    """The Autoposer's block (the tool, the handles), drawn here; False without it."""
    panels = module("panels")
    if panels is None:
        return False
    panels.draw_controls(layout, context)
    return True


def draw_settings(layout, context) -> bool:
    """The Autoposer's own settings (the floor, how the body follows), drawn here."""
    panels = module("panels")
    if panels is None:
        return False
    panels.draw_settings(layout, context)
    return True


# ---------------------------------------------------------------------------
# Which bone is a hand, a foot, the hips
# ---------------------------------------------------------------------------

#: names a joint goes by on the rigs this add-on generates for, without the Autoposer's map
_NAMES = {
    "Hips": ("Hips", "hips", "pelvis", "Pelvis", "DEF-spine", "ORG-spine", "CC_Base_Hip", "root_x"),
    "LeftHand": ("LeftHand", "hand_l", "Hand_L", "hand.L", "DEF-hand.L", "ORG-hand.L", "CC_Base_L_Hand"),
    "RightHand": ("RightHand", "hand_r", "Hand_R", "hand.R", "DEF-hand.R", "ORG-hand.R", "CC_Base_R_Hand"),
    "LeftFoot": ("LeftFoot", "foot_l", "Foot_L", "foot.L", "DEF-foot.L", "ORG-foot.L", "CC_Base_L_Foot"),
    "RightFoot": ("RightFoot", "foot_r", "Foot_R", "foot.R", "DEF-foot.R", "ORG-foot.R", "CC_Base_R_Foot"),
    "Head": ("Head", "head", "DEF-spine.006", "ORG-spine.006", "CC_Base_Head"),
}


def _by_name(arm, joint):
    bones = arm.data.bones
    for n in _NAMES.get(joint, (joint,)):
        if n in bones:
            return bones[n]
    # a prefixed export (mixamorig:LeftHand, Armature|LeftHand)
    for b in bones:
        tail = b.name.replace("|", ":").rsplit(":", 1)[-1]
        if tail in _NAMES.get(joint, (joint,)):
            return b
    return None


def joint_bone(arm, joint):
    """The bone that plays ``joint`` on ``arm``: the Autoposer's map when it is here (any rig),
    else the bone names (the rigs Animatica generates for)."""
    p = poser()
    if p is not None:
        return p.joint_bone(arm, joint)
    return _by_name(arm, joint)


def joint_pose_bone(arm, joint):
    p = poser()
    if p is not None:
        return p.joint_pose_bone(arm, joint)
    b = _by_name(arm, joint)
    return arm.pose.bones.get(b.name) if b is not None else None


def canonical_joint(arm, bone_name: str) -> str:
    p = poser()
    if p is not None:
        return p.canonical_joint(arm, bone_name)
    for joint in _NAMES:
        b = _by_name(arm, joint)
        if b is not None and b.name == bone_name:
            return joint
    return bone_name


def control_label(joint: str, kind: str = "") -> str:
    p = poser()
    if p is not None:
        return p.control_label(joint, kind)
    for side, short in (("Left", "L "), ("Right", "R ")):
        if joint.startswith(side):
            return short + joint[len(side):].lower()
    return joint


def has_controls(arm) -> bool:
    """Whether the rig carries the old control bones (the Autoposer's own business)."""
    p = poser()
    return bool(p is not None and arm is not None and p.has_controls(arm))
