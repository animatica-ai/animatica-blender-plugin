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
    """``{id, text, why, op, props}`` for the hint to show now, or None."""
    from . import key_poses, properties, toolbar, waypoints
    scene = context.scene
    s = scene.animatica
    arm = properties._live_armature(s.target_armature)
    if arm is None or s.is_generating:
        return None
    mode = toolbar.mode_of(context)
    keys = key_poses.take_keys(scene)
    blocks = [b for b in s.prompt_blocks if b.enabled]
    out = []

    if s.is_previewing:
        out.append(dict(
            id="review",
            text="Judge it: Accept keeps it · Key This Frame saves a good moment · Redo retries",
            why="Accept keeps the take and locks its blocks, so a later Generate leaves it alone. "
                "Key This Frame turns a frame you like into a key pose, so the next Redo goes "
                "through it and keeps what was good. Redo makes the block again",
            op=None, props={}))
    elif mode == 'POSE':
        unprompted = [b for b in blocks if not (b.prompt or "").strip()]
        if not keys:
            out.append(dict(
                id="pose_first_key",
                text="Key a moment that matters (I): the take will go through it",
                why="A key pose steers the take through that pose at that frame. Without any, "
                    "the model decides every pose itself; two or three at the moments that "
                    "carry the action (a contact, a peak, a landing) make it yours. Pose with "
                    "the handles (Autopose) or describe the pose in the field",
                op="animatica.toolbar_autoposer", props={}))
        elif len(keys) == 1:
            out.append(dict(
                id="pose_second_key",
                text="Key where the action turns: the motion between keys follows your plan",
                why="One key pose fixes one instant; the motion either side is still the model's "
                    "guess. A second, where the action turns (the push-off, the catch), pins "
                    "down what happens between them",
                op=None, props={}))
        elif not blocks or unprompted:
            out.append(dict(
                id="pose_to_motion",
                text="Say what happens between your keys in Motion (Alt M)",
                why="Key poses fix the moments; a prompt says how to get from one to the next "
                    "(the walk, the jump, the stumble). A block with no prompt leaves that to "
                    "the model",
                op="animatica.bar_mode", props={"mode": 'MOTION'}))
        else:
            out.append(dict(
                id="pose_ready",
                text="Ready for a take: switch to Motion (Alt M) and Generate",
                why="Every block has a prompt and the key poses are set: the take is made from "
                    "both. Keep adding keys where the action changes to steer it more",
                op="animatica.bar_mode", props={"mode": 'MOTION'}))
    else:
        text_all = " ".join((b.prompt or "") for b in blocks)
        if not blocks or not text_all.strip():
            out.append(dict(
                id="motion_prompt",
                text="Type what happens here: the model moves to your words",
                why="The prompt is what the model makes. One action per block is followed most "
                    "closely: \u201cwalks to the door\u201d, then \u201csits down\u201d",
                op="animatica.toolbar_prompt_here", props={}))
        elif not keys:
            out.append(dict(
                id="motion_keys",
                text="Key 2\u20133 moments in Pose (Alt M), or the model picks every pose",
                why="Without key poses the model invents every pose. A key pose at the contact, "
                    "the peak or the landing steers the take through it, so it does what you "
                    "meant there",
                op="animatica.bar_mode", props={"mode": 'POSE'}))
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
    for h in out:
        if h["id"] not in dismissed:
            return h
    return None


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
    bpy.utils.register_class(ANIMATICA_OT_hint_why)
    bpy.utils.register_class(ANIMATICA_OT_hint_dismiss)


def unregister():
    bpy.utils.unregister_class(ANIMATICA_OT_hint_dismiss)
    bpy.utils.unregister_class(ANIMATICA_OT_hint_why)
