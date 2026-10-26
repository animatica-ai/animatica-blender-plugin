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
    from . import key_poses, mmcp_client, pose_edit, posing, properties, waypoints
    step = posing.hint(context)
    if step:
        # an edit under way (the Autoposer's Lock in Place): the step it is on, where the eye
        # is (the header line alone was missed, and the click looked like it did nothing)
        return step
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
    from . import batch, toolbar
    waiting, failures = batch.pending(s), batch.failures(s)
    if waiting or failures:
        # a batch's takes in front of you: what each button on the bar does with them
        n = len(waiting)
        if n > 1:
            text = (f"Judge the {n} takes: Keep All keeps them · Redo All tries again "
                    "· Discard All throws them away")
        elif n:
            text = "Judge the take: Keep keeps it · Redo tries again · Discard throws it away"
        else:
            text = "No take was made. Redo tries again, and the sidebar (N) says why"
        if n and failures:
            text += f" · {len(failures)} failed"
        out.append(dict(
            id="batch_review", text=text,
            why="Each selected character got a take of its own. Keep keeps them as their own actions "
                "and locks their blocks. The number buttons flip every character to another version. "
                "To keep, redo or discard one character alone, use its row in the sidebar (N)",
            op=None, props={}))
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
    pro = posing.present()          # the handles are Animatica Marionette's (a separate add-on)
    if pro and (s.is_previewing or in_take):
        # what the editing add-on has to say about the take showing (problems it found in it)
        said = posing.suggestion(context)
        if said:
            out.append(said)
    if s.is_previewing:
        out.append(dict(
            id="review",
            # in the batch's words: one way to judge a take, however it was made
            text=("Judge the take: Keep or fine-tune it \u00b7 Redo tries again \u00b7 Discard throws it away" if pro
                  else "Judge the take: Keep keeps it \u00b7 Redo tries again \u00b7 Discard throws it away"),
            why="Keep sets the take in stone: its motion stays and its blocks lock, so a later Generate "
                "or Redo leaves them alone (unlock a block on the Timeline to make it again). Until then "
                "Generate waits, and Redo makes it again. "
                + ("Fine-tuning it with the handles keeps it too. " if pro else "")
                + "Key This Frame turns a frame you like into a key pose, and a Redo then passes through it",
            op=None, props={}))
    elif waiting or failures:
        pass                        # the batch's hint, above
    elif in_take and pro:
        # an accepted take under the playhead: fine-tune it
        out.append(dict(
            id="finetune",
            text="Fine-tune the take: drag a handle and the motion around it changes, with no keys added",
            why="Edits on a take change its motion where it is keyed and add no keyframes. The "
                "frames around it follow by Reach and Intensity (the curve on the bar). The onion "
                "skins and the trail show them move as you drag",
            op="animatica.hint_finetune", props={}))
    elif in_take and _kept_here(s, scene):
        # a kept take: Redo leaves a locked block alone, so say how to make it again
        out.append(dict(
            id="finetune",
            text="Change a pose in the kept take: pose the character and key it (I)",
            why=f"Poses are changed with Blender's own tools here ({posing.PRO_NAME} adds handles that "
                "carry an edit through the frames around it). This block is kept, so Redo and Generate "
                "leave it alone. To make it again around a new key pose, unlock it on the Timeline, "
                "Set Key Pose, then Redo",
            op=None, props={}))
    elif in_take:
        # without the handles: Blender's own posing, a key pose, and Redo around it
        out.append(dict(
            id="finetune",
            text="Change a pose in the take: pose the character, Set Key Pose, then Redo",
            why=f"Poses are changed with Blender's own tools here ({posing.PRO_NAME} adds handles that "
                "carry an edit through the frames around it). A key pose set on the take stays, and "
                "Redo makes the motion around it again",
            op=None, props={}))
    busy = s.is_previewing or in_take or bool(waiting or failures)
    chars = [] if busy else batch.batch_selection(context)
    ready = toolbar.batch_readiness(context, chars) if chars else []
    bare = [name for name, why in ready if why == "no prompt"]
    if chars and bare and s.batch_direction == 'OWN' and text_all.strip():
        # several selected, and only some told what to do: the prompt typed can be everyone's
        out.append(dict(
            id="batch_shared",
            text=f"{toolbar._names(bare)} {'has' if len(bare) == 1 else 'have'} no prompt. "
                 f"Click to share {arm.name}'s with every selected character",
            why="With Shared on, every selected character does the active character's prompts, each with "
                "its own seed and from where it stands. Without it, a character with no prompt is left out. "
                "To give one its own, Shift-click it so it is the active one, and type its prompt",
            op="wm.context_toggle_enum",
            props={"data_path": "scene.animatica.batch_direction", "value_1": 'OWN', "value_2": 'SHARED'}))
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
        go = [name for name, why in ready if not why]
        if go:
            # Generate makes every selected character: the ready line says so, and from whose prompts
            count = f"{len(chars)} characters" if len(go) == len(chars) else f"{len(go)} of the {len(chars)}"
            if s.batch_direction == 'SHARED':
                text = f"Ready: Generate {count}, all doing {arm.name}'s prompts"
                why = ("Every selected character does the active character's prompts, each with its own "
                       "seed and from where it stands, so they differ like a crowd. Waypoints and pins are "
                       "left out. Turn off Shared to have each follow its own")
            else:
                text = f"Ready: Generate {count}, each from its own prompts"
                why = ("Each selected character gets a take of its own, from its own prompts, key poses "
                       "and waypoints, all in one go. Turn on Shared to give them all the active "
                       "character's prompts instead")
            out.append(dict(id="batch_ready", text=text, why=why, op="animatica.toolbar_generate", props={}))
        elif chars:
            pass                    # the button says what stops them; the single take's Ready would mislead
        elif blocks and not unprompted:
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
    """Fine-tune the take. Turns on the Autopose tool and Marionette's onion skin, so
    you can drag a handle and see the frames around it follow"""
    bl_idname = "animatica.hint_finetune"
    bl_label = "Fine-tune"
    bl_options = {'INTERNAL', 'UNDO'}

    def invoke(self, context, event):
        return self.execute(context)

    def execute(self, context):
        from . import posing
        s = context.scene.animatica
        s.key_pose_overlay = True
        posing.show_onion(context)          # Marionette's onion skin (when installed)
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


def _kept_here(settings, scene) -> bool:
    """Is the block under the playhead kept (locked)?"""
    from .toolbar import block_at
    i = block_at(settings, int(scene.frame_current))
    return 0 <= i < len(settings.prompt_blocks) and bool(settings.prompt_blocks[i].locked)
