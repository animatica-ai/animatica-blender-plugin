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
            text="Judge it: Accept keeps it \u00b7 Key This Frame saves a good moment \u00b7 Redo retries",
            why="Accept keeps the take and locks its blocks, so a later Generate leaves it alone. "
                "Key This Frame turns a frame you like into a key pose, so the next Redo goes "
                "through it and keeps what was good. Redo makes the block again",
            op=None, props={}))
    elif in_take:
        # an accepted take under the playhead: fine-tune it
        out.append(dict(
            id="finetune",
            text="Fine-tune it: drag a handle and the take reshapes around it, no keys added",
            why="Edits on a take update its motion where it is keyed and add no keyframes. The "
                "frames around follow by Reach and Intensity (the curve on the bar); the onion "
                "skins and the trail show them move as you drag",
            op="animatica.hint_finetune", props={}))
    elif not keys:
        out.append(dict(
            id="pose_first_key",
            text="Key a moment that matters (I): the take will go through it",
            why="A key pose steers the take through that pose at that frame. Without any, "
                "the model decides every pose itself; two or three at the moments that "
                "carry the action (a contact, a peak, a landing) make it yours. Pose with "
                "the handles (Autopose), or describe the pose with the button in the field",
            op="animatica.toolbar_autoposer", props={}))
    elif len(keys) == 1:
        out.append(dict(
            id="pose_second_key",
            text="Key where the action turns: the motion between keys follows your plan",
            why="One key pose fixes one instant; the motion either side is still the model's "
                "guess. A second, where the action turns (the push-off, the catch), pins "
                "down what happens between them",
            op=None, props={}))
    if not out and (not blocks or unprompted or not text_all.strip()):
        out.append(dict(
            id="motion_prompt",
            text="Say what happens between your keys: type it in the field",
            why="The prompt is what the model makes. One action per block is followed most "
                "closely: \u201cwalks to the door\u201d, then \u201csits down\u201d",
            op="animatica.toolbar_prompt_here", props={}))
    if not s.is_previewing and not in_take:
        if _PLACES.search(text_all) and not list(waypoints.waypoints(scene)):
            out.append(dict(
                id="motion_waypoint",
                text="Add a Waypoint where it ends, or the model guesses where \u201cthere\u201d is",
                why="A prompt can say \u201cto the door\u201d but not where the door is. With a "
                    "waypoint the character ends up there; without one the model picks the "
                    "direction and the distance",
                op="animatica.add_waypoint", props={}))
        long = [b for b in blocks if _seconds(scene, b.frame_end - b.frame_start) > 6.0]
        if long:
            secs = _seconds(scene, long[0].frame_end - long[0].frame_start)
            out.append(dict(
                id="motion_split",
                text=f"Split this {secs:.0f} s block: one action per block is followed more closely",
                why="The model follows one instruction per block most closely. A long block "
                    "with several actions blurs them; two blocks keep each one sharp",
                op=None, props={}))
        if keys and blocks and not unprompted:
            out.append(dict(
                id="ready",
                text="Ready: Generate, and the take goes through your keys",
                why="Every block has a prompt and the key poses are set: the take is made from "
                    "both. Keep adding keys where the action changes to steer it more",
                op="animatica.toolbar_generate", props={}))
    for h in out:
        if h["id"] not in dismissed:
            return h
    return None


class ANIMATICA_OT_hint_finetune(bpy.types.Operator):
    """Fine-tune the take: the Autopose tool and the onion skins on, so a drag
    reshapes the take and you see the frames around it follow"""
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
    """Why this is the next step"""
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
