# SPDX-License-Identifier: GPL-3.0-or-later
"""The next step, and why it makes the result better.

A line above the floating bar that names the one thing most worth doing now,
in words that say what it buys: not "add a waypoint" but "it goes somewhere,
and without a waypoint the model picks where". The model does what it is told
and guesses the rest; every hint here is a way of telling it something it
would otherwise guess.

One hint at a time, the first that applies, in the order a take is built:
something to happen, the moments that matter keyed, where it goes, then the
take itself. A hint the artist dismisses stays away for the session.
"""

from __future__ import annotations

import re

import bpy

#: hints dismissed this session, by id
dismissed: set = set()

#: words that say the character goes somewhere (a waypoint says where)
_PLACES = re.compile(r"\b(to|towards?|into|onto|across|over to|up to|through|along)\b", re.I)


def _seconds(scene, frames: int) -> float:
    return frames / max(1.0, scene.render.fps / max(scene.render.fps_base, 1e-6))


def next_step(context) -> dict | None:
    """``{id, text, why, op, props}`` for the hint to show now, or None.

    The work goes: key the moments, say what happens, Generate, judge the
    take and Accept it, then fine-tune it by hand. Each hint is the next of
    those still to do here."""
    from . import key_poses, pose_edit, properties, waypoints
    scene = context.scene
    s = scene.animatica
    arm = properties._live_armature(s.target_armature)
    if arm is None or s.is_generating:
        return None
    keys = key_poses.take_keys(scene)
    blocks = [b for b in s.prompt_blocks if b.enabled]
    text_all = " ".join((b.prompt or "") for b in blocks)
    unprompted = [b for b in blocks if not (b.prompt or "").strip()]
    out = []
    in_take = False
    try:
        in_take = pose_edit.edit_key_type(arm, int(scene.frame_current)) == 'GENERATED'
    except Exception:                                    # noqa: BLE001
        pass

    if s.is_previewing:
        out.append(dict(
            id="review",
            text="Fine-tune the take with the handles \u00b7 Redo tries again \u00b7 Discard throws it away",
            why="There is nothing to accept, because the take is already in your scene. Your first "
                "edit on it keeps it and locks its blocks, so a later Generate leaves them alone. "
                "Key This Frame turns a frame you like into a key pose, and a Redo then passes through it",
            op=None, props={}))
    elif in_take:
        # an accepted take under the playhead: fine-tune it
        out.append(dict(
            id="finetune",
            text="Fine-tune the take: drag a handle and the motion around it changes, with no keys added",
            why="Edits on a take change its motion where it is keyed and add no keyframes. The "
                "frames around it follow by Reach and Intensity (the curve on the bar). The onion "
                "skins and the trail show them move as you drag",
            op="animatica.hint_finetune", props={}))
    elif not keys:
        out.append(dict(
            id="pose_first_key",
            text="Key a moment that matters (I), and the take will pass through it",
            why="The take passes through each key pose at its frame. Without any, the model "
                "decides every pose itself. Two or three at the moments that matter, like a "
                "foot contact, a peak or a landing, give you control. Pose with the handles "
                "(Autopose), or describe the pose with the button in the field",
            op="animatica.toolbar_autoposer", props={}))
    elif len(keys) == 1:
        out.append(dict(
            id="pose_second_key",
            text="Key where the action changes, so the motion between keys goes your way",
            why="One key pose sets one moment, and the model still guesses the motion on either "
                "side. A second key where the action changes, like a push-off or a catch, sets "
                "what happens between them",
            op=None, props={}))
    if not out and (not blocks or unprompted or not text_all.strip()):
        out.append(dict(
            id="motion_prompt",
            text="Type what happens between your keys in the field",
            why="The model makes what the prompt describes. It follows one action per block "
                "most closely, like \u201cwalks to the door\u201d, then \u201csits down\u201d",
            op="animatica.toolbar_prompt_here", props={}))
    if not s.is_previewing and not in_take:
        if _PLACES.search(text_all) and not list(waypoints.waypoints(scene)):
            out.append(dict(
                id="motion_waypoint",
                text="Add a Waypoint where the character should end up, or the model guesses",
                why="A prompt can say \u201cto the door\u201d but not where the door is. With a "
                    "waypoint the character ends up there. Without one, the model picks the "
                    "direction and the distance",
                op="animatica.add_waypoint", props={}))
        long = [b for b in blocks if _seconds(scene, b.frame_end - b.frame_start) > 6.0]
        if long:
            secs = _seconds(scene, long[0].frame_end - long[0].frame_start)
            out.append(dict(
                id="motion_split",
                text=f"Split this {secs:.0f} s block, since one action per block is followed more closely",
                why="The model follows one instruction per block most closely. In a long block "
                    "with several actions they blur together. Split it in two to keep each one clear",
                op=None, props={}))
        if keys and blocks and not unprompted:
            out.append(dict(
                id="ready",
                text="Ready to Generate. The take will pass through your keys",
                why="Every block has a prompt and the key poses are set, and the take is made from "
                    "both. Add more keys where the action changes to control it more",
                op="animatica.toolbar_generate", props={}))
    for h in out:
        if h["id"] not in dismissed:
            return h
    return None


class ANIMATICA_OT_hint_finetune(bpy.types.Operator):
    """Fine-tune the take. Turns on the Autopose tool and the onion skins, so you
    can drag a handle and see the frames around it follow"""
    bl_idname = "animatica.hint_finetune"
    bl_label = "Fine-tune"
    bl_options = {'INTERNAL', 'UNDO'}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        from . import handles
        s = context.scene.animatica
        s.key_pose_overlay = True
        s.key_pose_ghosts = True
        s.onion_mode = 'FRAMES'
        if not handles.tool_active(context):
            why = handles.activate(context)
            if why:
                self.report({'WARNING'}, why)
        return {'FINISHED'}


class ANIMATICA_OT_hint_why(bpy.types.Operator):
    """Show why this is the next step"""
    bl_idname = "animatica.hint_why"
    bl_label = "Why"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        h = next_step(context)
        if h:
            self.report({'INFO'}, h["why"])
        return {'FINISHED'}


class ANIMATICA_OT_hint_dismiss(bpy.types.Operator):
    """Hide this hint for the rest of the session"""
    bl_idname = "animatica.hint_dismiss"
    bl_label = "Dismiss Hint"
    bl_options = {'INTERNAL'}

    key: bpy.props.StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        dismissed.add(self.key)
        for a in context.screen.areas:
            if a.type == 'VIEW_3D':
                a.tag_redraw()
        return {'FINISHED'}


def register():
    bpy.utils.register_class(ANIMATICA_OT_hint_finetune)
    bpy.utils.register_class(ANIMATICA_OT_hint_why)
    bpy.utils.register_class(ANIMATICA_OT_hint_dismiss)


def unregister():
    bpy.utils.unregister_class(ANIMATICA_OT_hint_dismiss)
    bpy.utils.unregister_class(ANIMATICA_OT_hint_why)
    bpy.utils.unregister_class(ANIMATICA_OT_hint_finetune)
