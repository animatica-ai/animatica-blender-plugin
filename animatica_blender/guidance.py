# SPDX-License-Identifier: GPL-3.0-or-later
"""The next step, and why it makes the result better.

A line above the floating bar that names the one thing most worth doing now,
in words that say what it buys: not "add a waypoint" but "it goes somewhere,
and without a waypoint the model picks where". The model does what it is told
and guesses the rest; every hint here is a way of telling it something it
would otherwise guess.

One hint at a time, the first that applies, in the order a first take is
quickest: something to happen, Generate, then the moments that matter keyed
and where it goes. A failed take and a key the take would leave out come
before all of them. A hint the artist dismisses stays away for the session.
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

    A first take comes quickest from a prompt alone, so the prompt and
    Generate come first and key poses are offered as the way to steer it
    (posing first meant a 225 MB download before anything moved)."""
    from . import joint_lock, key_poses, mmcp_client, pose_edit, properties, waypoints
    step = joint_lock.step_text()
    if step:
        # a lock being made: the step it is on, where the eye is (the header
        # line alone was missed, and the click looked like it did nothing)
        return dict(id="lock_step", text=step, why="Lock in Place holds a hand or foot on one spot "
                    "for the frames you choose, so a planted foot stops sliding. Esc cancels",
                    op=None, props={})
    from . import retarget
    copied = retarget.hint(context)
    if copied is not None and copied["id"] not in dismissed:
        # just copied: what, and what next (the click itself showed nothing)
        return copied
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

    failed = mmcp_client.last_failure()
    if failed:
        out.append(dict(
            id="failed",
            text=f"The take failed: {failed}. Click to try again",
            why="Nothing in your scene changed. Click the hint or Generate to try again. "
                "The full error is in Blender's system console",
            op="animatica.toolbar_generate", props={}))
    dropped = key_poses.dropped_frames(scene) if not s.is_previewing else []
    if dropped:
        lo, hi = _window(context)
        where = f"frame {dropped[0]}" if len(dropped) == 1 else f"{len(dropped)} key poses"
        out.append(dict(
            id="dropped",
            text=f"The take ({lo}–{hi}) leaves out {where}. Click to stretch the block over it",
            why="Only the key poses inside the blocks steer the take. The ones outside them are "
                "not sent. Stretch the block (click), or drag its edge in the Timeline",
            op="animatica.blocks_over_keys", props={}))
    from . import posing
    pro = posing.present()          # the handles are Animatica Autoposer Pro's (a separate add-on)
    if s.is_previewing:
        out.append(dict(
            id="review",
            text=("Fine-tune the take with the handles \u00b7 Redo tries again \u00b7 Discard throws it away" if pro
                  else "Redo tries again \u00b7 Discard throws it away \u00b7 Key This Frame keeps a pose you like"),
            why="There is nothing to accept, because the take is already in your scene. Your first "
                "edit on it keeps it and locks its blocks, so a later Generate leaves them alone. "
                "Key This Frame turns a frame you like into a key pose, and a Redo then passes through it",
            op=None, props={}))
    elif in_take and pro:
        # an accepted take under the playhead: fine-tune it
        out.append(dict(
            id="finetune",
            text="Fine-tune the take: drag a handle and the motion around it changes, with no keys added",
            why="Edits on a take change its motion where it is keyed and add no keyframes. The "
                "frames around it follow by Reach and Intensity (the curve on the bar). The onion "
                "skins and the trail show them move as you drag",
            op="animatica.hint_finetune", props={}))
    elif in_take:
        # without the handles: Blender's own posing, a key pose, and Redo around it
        out.append(dict(
            id="finetune",
            text="Change a pose in the take: pose the character, Set Key Pose, then Redo",
            why=f"Poses are changed with Blender's own tools here ({posing.PRO_NAME} adds handles that "
                "carry an edit through the frames around it). A key pose set on the take stays, and "
                "Redo makes the motion around it again",
            op=None, props={}))
    busy = s.is_previewing or in_take
    if not busy and (not blocks or unprompted or not text_all.strip()):
        out.append(dict(
            id="motion_prompt",
            text="Type what happens, like \u201cwaves hello\u201d, then Generate",
            why="The model makes the motion the prompt describes. Start with one action, like "
                "\u201cwaves hello\u201d or \u201cjumps over a puddle\u201d. Key poses can come "
                "later, to steer it",
            op="animatica.toolbar_prompt_here", props={}))
    if not busy:
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
        if blocks and not unprompted:
            if not keys:
                text = "Ready: Generate. Key a pose first if you want to steer it"
                why = ("Every block has a prompt, so Generate makes the motion. Without key poses "
                       "the model decides every pose itself. To steer it, pose the character "
                       + ("with Autopose " if pro else "")
                       + "(or describe a pose with the button in the field) and key "
                       "the moments that matter, like a foot contact or a landing")
            elif len(keys) == 1:
                text = "Ready: Generate. A second key where the action changes steers it more"
                why = ("The take passes through your key pose. The model still guesses the motion "
                       "on either side, and a second key, like a push-off or a catch, sets what "
                       "happens between them")
            else:
                text = "Ready: Generate. The take will pass through your keys"
                why = ("Every block has a prompt and the key poses are set, and the take is made "
                       "from both. Add more keys where the action changes to control it more")
            out.append(dict(id="ready", text=text, why=why, op="animatica.toolbar_generate", props={}))
    for h in out:
        if h["id"] not in dismissed:
            return h
    return None


def _window(context) -> tuple:
    from . import properties, request_builder
    s = context.scene.animatica
    arm = properties._live_armature(s.target_armature)
    return request_builder.compute_frame_range(s.prompt_blocks, arm, context.scene)


class ANIMATICA_OT_blocks_over_keys(bpy.types.Operator):
    """Stretch the first and last blocks so every key pose is inside the take"""
    bl_idname = "animatica.blocks_over_keys"
    bl_label = "Stretch Blocks Over the Keys"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import key_poses
        s = context.scene.animatica
        blocks = [b for b in s.prompt_blocks if b.enabled]
        dropped = key_poses.dropped_frames(context.scene)
        if not blocks or not dropped:
            return {'CANCELLED'}
        first = min(blocks, key=lambda b: b.frame_start)
        last = max(blocks, key=lambda b: b.frame_end)
        lo, hi = min(dropped), max(dropped)
        if lo < first.frame_start:
            first.frame_start = max(1, lo)
        if hi > last.frame_end:
            last.frame_end = hi
            context.scene.frame_end = max(context.scene.frame_end, hi)     # so it plays too
        key_poses.invalidate_plan()
        key_poses.request_rebuild()
        for a in context.screen.areas:
            if a.type in {'VIEW_3D', 'DOPESHEET_EDITOR', 'TIMELINE'}:
                a.tag_redraw()
        self.report({'INFO'}, f"The take now runs {first.frame_start}\u2013{last.frame_end}")
        return {'FINISHED'}


class ANIMATICA_OT_hint_finetune(bpy.types.Operator):
    """Fine-tune the take. Turns on the Autopose tool and the onion skins, so you
    can drag a handle and see the frames around it follow"""
    bl_idname = "animatica.hint_finetune"
    bl_label = "Fine-tune"
    bl_options = {'INTERNAL', 'UNDO'}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        from . import posing
        s = context.scene.animatica
        s.key_pose_overlay = True
        s.key_pose_ghosts = True
        s.onion_mode = 'FRAMES'
        handles = posing.handles()          # posing it is the Autoposer's (when installed)
        if handles is not None and not handles.tool_active(context):
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
    bpy.utils.register_class(ANIMATICA_OT_blocks_over_keys)
    bpy.utils.register_class(ANIMATICA_OT_hint_finetune)
    bpy.utils.register_class(ANIMATICA_OT_hint_why)
    bpy.utils.register_class(ANIMATICA_OT_hint_dismiss)


def unregister():
    bpy.utils.unregister_class(ANIMATICA_OT_hint_dismiss)
    bpy.utils.unregister_class(ANIMATICA_OT_hint_why)
    bpy.utils.unregister_class(ANIMATICA_OT_hint_finetune)
    bpy.utils.unregister_class(ANIMATICA_OT_blocks_over_keys)
