# SPDX-License-Identifier: GPL-3.0-or-later
"""Several versions of one take, and flipping between them.

With Variations above 1 a Generate asks for that many samples of the same
request (``options.num_samples``): same prompts, poses and constraints,
performed differently, in one generation. The server answers with one glTF
holding one animation per sample. The first is baked as usual; the answer is
kept here, with what the bake needs, so another one can be baked in its place
without asking the server again. Accept keeps the one showing.

Kept in memory only, for the take being previewed: a take that waits across a
reload of the file keeps the version it shows, and loses the switcher.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import bpy
from bpy.props import IntProperty
from bpy.types import Operator

from . import properties


@dataclass
class Take:
    result: dict
    count: int
    index: int
    action_name: str
    bake: dict = field(default_factory=dict)


#: armature name -> the take on it that has variations
_takes: dict[str, Take] = {}


def sample_count(result) -> int:
    return len((result or {}).get("animations") or ()) or 1


def remember(arm, result, action, **bake) -> None:
    """Keep *result* for *arm* if it holds more than one version."""
    count = sample_count(result)
    if arm is None or count < 2:
        forget(arm)
        return
    _takes[arm.name] = Take(result=result, count=count, index=0,
                            action_name=action.name, bake=bake)


def forget(arm) -> None:
    if arm is not None:
        _takes.pop(arm.name, None)


def take_of(arm) -> Take | None:
    """The take with variations showing on *arm*, if it still is the one showing."""
    if arm is None:
        return None
    take = _takes.get(arm.name)
    ad = arm.animation_data
    if take is None or ad is None or ad.action is None or ad.action.name != take.action_name:
        return None
    return take


def show(context, arm, index: int) -> None:
    """Bake version *index* of the take on *arm* in place of the one showing."""
    from . import operators

    take = take_of(arm)
    if take is None:
        raise RuntimeError("no variations to switch between")
    index %= take.count
    settings = context.scene.animatica
    old = arm.animation_data.action
    b = take.bake
    action, _ = operators.bake_take(
        context, settings, arm, take.result, sample_index=index,
        prompt_blocks=b["prompt_blocks"], gen_start=b["gen_start"], gen_end=b["gen_end"],
        anchor_frames=b.get("anchor_frames"),
        splice_target=bpy.data.actions.get(b.get("splice_target_name") or ""),
        server_looped=b.get("server_looped", False),
        source_action_name=b.get("source_action_name", ""),
    )
    if action is not old and old is not None and operators._is_motion_bake_action(old):
        # The version it replaces goes, and the new one takes its name.
        name = old.name
        bpy.data.actions.remove(old)
        action.name = name
    take.index = index
    take.action_name = action.name


class ANIMATICA_OT_show_variation(Operator):
    bl_idname = "animatica.show_variation"
    bl_label = "Show Variation"
    bl_description = "Show another version of this take. No new generation"
    bl_options = {'REGISTER', 'UNDO'}

    step: IntProperty(default=1)

    @classmethod
    def poll(cls, context):
        s = context.scene.animatica
        return (not s.is_generating and s.is_previewing
                and take_of(properties._live_armature(s.target_armature)) is not None)

    def execute(self, context):
        arm = properties._live_armature(context.scene.animatica.target_armature)
        take = take_of(arm)
        try:
            show(context, arm, take.index + self.step)
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI
            self.report({'ERROR'}, f"Could not switch: {exc}")
            return {'CANCELLED'}
        return {'FINISHED'}


def draw(layout, arm) -> None:
    """Variation k of N, with a way to the neighbours."""
    take = take_of(arm)
    if take is None:
        return
    row = layout.row(align=True)
    row.operator("animatica.show_variation", text="", icon='TRIA_LEFT').step = -1
    mid = row.row(align=True)
    mid.alignment = 'CENTER'
    mid.label(text=f"Variation {take.index + 1} of {take.count}")
    row.operator("animatica.show_variation", text="", icon='TRIA_RIGHT').step = 1


def register():
    bpy.utils.register_class(ANIMATICA_OT_show_variation)


def unregister():
    _takes.clear()
    bpy.utils.unregister_class(ANIMATICA_OT_show_variation)
