# SPDX-License-Identifier: GPL-3.0-or-later
"""Animatica Autoposer Pro, when it is installed alongside: the one place this add-on reaches it.

The two are separate products: this one makes motion and shows it (prompts, key poses, takes,
the ghosts), the Autoposer edits it by hand -- the Autopose handles, the motion trail, the
zoetrope, Lock in Place, the reach of an edit, a ghost clicked to edit, the root path bent.
Installed together, its buttons are on this add-on's bar and its rows in these panels, it samples
and draws in this add-on's overlay pass, and its edits key into the take. Without it nothing
here edits by hand, and what needs to know which bone is a hand or a foot reads the bone names.

Everything it adds is asked for here, through its ``editing`` module, each time it is needed:
nothing is registered into this add-on, so either can be enabled or disabled first.
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
#: say the same number, so an older or newer Autoposer leaves this add-on working on its own.
#: 2: editing moved to the Autoposer, asked through its ``editing`` module.
BRIDGE = 2


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


def editing():
    """The Autoposer's editing (its answers to this add-on), or None without it."""
    return module("editing")


def dragging() -> bool:
    """A drag under way that poses the rig -- a handle, the trail, a zoetrope slice: its pose is
    unkeyed until it ends, so nothing may step the playhead under it."""
    ed = editing()
    try:
        return bool(ed is not None and ed.dragging())
    except Exception:                                   # noqa: BLE001
        return False


def bar_items(context, arm, where: str) -> list:
    """The Autoposer's buttons for the bar (dicts the bar makes its items from): ``where`` is
    "pose" (first on the bar) or "edit" (after the onion skin)."""
    ed = editing()
    if ed is None:
        return []
    try:
        return list(ed.bar_items(context, arm, where))
    except Exception as exc:                            # noqa: BLE001 -- never break the bar
        print(f"[Animatica] the Autoposer's buttons: {exc}")
        return []


def samplers() -> list:
    """Who else samples the motion in the ghosts' bake pass and draws in the overlay (the
    Autoposer's motion trail)."""
    ed = editing()
    try:
        return list(ed.samplers()) if ed is not None else []
    except Exception:                                   # noqa: BLE001
        return []


def editing_frame(scene) -> int:
    """The ghost whose pose is being edited, or -1."""
    ed = editing()
    try:
        return int(ed.editing_frame(scene)) if ed is not None else -1
    except Exception:                                   # noqa: BLE001
        return -1


def end_edit(scene) -> None:
    """A pose was keyed: the ghost clicked to edit it is done."""
    ed = editing()
    if ed is not None:
        try:
            ed.end_edit(scene)
        except Exception:                               # noqa: BLE001
            pass


def hint(context):
    """The hint line while an edit is under way (Lock in Place's steps), or None."""
    ed = editing()
    try:
        return ed.hint(context) if ed is not None else None
    except Exception:                                   # noqa: BLE001
        return None


def after_take(arm, action, start, end) -> None:
    """A take was baked over these frames (the locks over them hold again)."""
    ed = editing()
    if ed is not None:
        try:
            ed.after_take(arm, action, start, end)
        except Exception as exc:                        # noqa: BLE001 -- never fail a take
            print(f"[Animatica] the Autoposer's locks over the take: {exc}")


def reseat(scene) -> None:
    """The bake walked the playhead with the frame handler muted: the controls are put back."""
    ed = editing()
    if ed is not None:
        try:
            ed.reseat(scene)
        except Exception:                               # noqa: BLE001 -- never fail a bake
            pass


def onion_layout():
    """How the Autoposer lays the onion skin out while editing (the frames within Reach, the
    zoetrope), or None: the onion skin is then this add-on's own Before/After."""
    ed = editing()
    return ed.onion_layout() if ed is not None else None


def path_overlay(action, spans, fps):
    """The fitted root path with the artist's bent one laid over it, where there is one."""
    ed = editing()
    return ed.path_overlay(action, spans, fps) if ed is not None else spans


def path_edited(action) -> bool:
    ed = editing()
    return bool(ed is not None and ed.path_edited(action))


def path_discard_all() -> None:
    ed = editing()
    if ed is not None:
        ed.path_discard_all()


def draw_panel(kind: str, layout, context) -> bool:
    """The Autoposer's rows in this add-on's panels; False without it."""
    ed = editing()
    if ed is None:
        return False
    try:
        return bool(ed.draw_panel(kind, layout, context))
    except Exception as exc:                            # noqa: BLE001 -- never break a panel
        print(f"[Animatica] the Autoposer's panel rows: {exc}")
        return False


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
