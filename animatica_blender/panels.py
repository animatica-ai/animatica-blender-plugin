"""Blender UI panels for the Animatica addon.

Sidebar panels in View3D > Sidebar > Animatica, named for the job:
  - Animatica: the shot — model, character, generate, accept or reject
  - Pose: the character — handles, what the overlay draws, what will be sent
      - Paths & Pins: the other two constraint kinds, collapsed
  - Settings: everything set once and left alone

Server URL + auth live in Edit > Preferences > Add-ons > Animatica.
"""

import bpy
from bpy.types import Panel

from . import constraints_ui, examples, mmcp_client, properties


class AnimaticaPanelBase:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animatica"


# ═══════════════════════════════════════════════════════════════════════════
# Shared draw helpers
# ═══════════════════════════════════════════════════════════════════════════

def _addon_prefs(context):
    """Return the addon's preferences, or ``None`` if unavailable."""
    try:
        return context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def _draw_signin_hint(layout, context) -> bool:
    """Surface a sign-in prompt when pointed at Animatica Cloud without a
    session token. Returns ``True`` if it drew anything.

    On Cloud, ``/capabilities`` is reachable without auth, so a user can
    Connect and pick a model yet still hit a 401 at Generate. Sign-in
    otherwise lives only in addon preferences; nudging here avoids that dead
    end.
    """
    prefs = _addon_prefs(context)
    if prefs is None:
        return False
    if getattr(prefs, "self_hosted", False):
        return False
    if getattr(prefs, "access_token", ""):
        return False
    from . import mmcp_client

    box = layout.box()
    # A session that ended by itself needs a reason, or signing in again looks
    # like the addon forgetting things at random.
    expired = mmcp_client.session_expired()
    if expired:
        row = box.row()
        row.alert = True
        row.label(text=expired.capitalize(), icon='ERROR')
    else:
        box.label(text="Sign in to Animatica to generate", icon='USER')
    box.operator("animatica.signin", icon='IMPORT', text="Sign in")
    return True


def _draw_rig_held(layout, context, settings) -> None:
    """Say when the Autoposer is holding the rig, and offer it back.

    While it holds, the action is detached: the pose on screen is the solve,
    frame changes do not move the character, and nothing is playing. That is a
    state the artist has to be able to see and leave — without it, the addon
    reads as stuck in editing with no way out.
    """
    from . import pose_edit

    arm = properties._live_armature(settings.target_armature)
    if arm is None or not pose_edit.autoposer_holds(arm):
        return
    box = layout.box()
    box.label(text="Autoposer is holding this rig", icon='INFO')
    note = box.row()
    note.active = False
    if pose_edit.stash_is_stale(arm):
        note.label(text="its action has changed since — giving back keeps yours")
    else:
        note.label(text="its animation is detached while it poses")
    box.operator("animatica.give_back_rig", icon='LOOP_BACK', text="Give Back Rig")


def _draw_set_keyframe(layout, context, settings) -> None:
    """The one button for committing a pose you posed by hand.

    Sits in the Pose panel, under the handles it commits: posing is local
    work that needs no server, and the Pose panel draws without one.
    """
    arm = properties._live_armature(settings.target_armature)
    if arm is None:
        return
    row = layout.row(align=True)
    row.scale_y = 1.2
    row.operator("animatica.set_key_pose", icon='KEYFRAME_HLT', text="Set Keyframe")
    # Record, next to the button it automates: on, posing with the handles keys
    # itself; off, Set Keyframe is the only way a pose is written.
    row.prop(settings, "auto_key_pose", text="", icon='REC', toggle=True)
    _draw_rig_held(layout, context, settings)


def _draw_duration_hint(layout, context, settings) -> None:
    """Warn when the would-be clip exceeds the model's duration limits.

    The capabilities payload carries both a recommended and a hard maximum
    duration; comparing the authored frame range (converted to seconds at the
    model's fps) up front saves the user a full cold-start round-trip just to
    be told the clip is too long. Draws nothing while the clip is within
    limits. Read-only — safe to call from ``draw``.
    """
    arm = properties._live_armature(settings.target_armature)
    if arm is None:
        return
    model = mmcp_client.cached_model(settings.model_id)
    if not model:
        return
    try:
        fps = float(model.get("fps"))
    except (TypeError, ValueError):
        return
    if fps <= 0:
        return

    from . import request_builder
    try:
        gen_start, gen_end = request_builder.compute_frame_range(
            settings.prompt_blocks, arm, context.scene,
        )
    except Exception:                                    # noqa: BLE001 — never break draw
        return
    total_frames = gen_end - gen_start + 1
    if total_frames <= 0:
        return

    seconds = total_frames / fps
    rec = model.get("recommended_max_duration_seconds")
    hard = (model.get("limits") or {}).get("max_duration_seconds")

    if hard is not None and seconds > float(hard):
        row = layout.row()
        row.alert = True
        row.label(
            text=f"Clip {seconds:.1f}s exceeds model max {float(hard):g}s",
            icon='ERROR',
        )
    elif rec is not None and seconds > float(rec):
        row = layout.row()
        row.alert = True
        row.label(
            text=f"Clip {seconds:.1f}s over recommended {float(rec):g}s",
            icon='ERROR',
        )


# ═══════════════════════════════════════════════════════════════════════════
# Main panel
# ═══════════════════════════════════════════════════════════════════════════

class ANIMATICA_PT_main(AnimaticaPanelBase, Panel):
    bl_label = "Animatica"
    bl_idname = "ANIMATICA_PT_main"

    def draw_header_preset(self, context):
        # Help is always a click away, in the corner, not the panel's first row.
        self.layout.operator("animatica.open_discord_help", text="", icon='HELP', emboss=False)

    def draw(self, context):
        from . import updater

        layout = self.layout
        settings = context.scene.animatica

        # A newer build, if there is one. Drawn first and only when it exists:
        # on a preview, the version someone is running is half of every bug
        # report, and the sidebar is where they already are.
        updater.draw_banner(layout, context)

        # Safety net: if the picker still holds a dangling armature (e.g.
        # the user just deleted it as part of a multi-object delete that
        # bypassed Blender's auto-remap), schedule a one-shot cleanup. The
        # actual reset cannot run from a draw callback, so we defer to a
        # timer and continue rendering this frame — the panel will redraw
        # with the cleared state on the next tick.
        try:
            target = settings.target_armature
        except ReferenceError:
            target = object()  # treat as dangling
        if target is not None and not properties._is_live_armature(target):
            properties.schedule_target_reset(context.scene.name)

        # Quota banner. Sticks around after a 429 from the cloud until the
        # user upgrades, dismisses, or makes a successful generation. Shown
        # above the connect prompt so it survives even when capabilities
        # haven't loaded yet (the user might re-Connect mid-error).
        if settings.quota_exceeded_message:
            box = layout.box()
            box.label(text="Generation limit reached", icon='ERROR')
            for line in settings.quota_exceeded_message.split("\n")[:3]:
                box.label(text=line)
            row = box.row(align=True)
            if settings.quota_upgrade_url:
                row.operator("animatica.open_upgrade", icon='URL', text="Upgrade")
            row.operator("animatica.dismiss_quota", icon='X', text="Dismiss")

        # Cloud sign-in nudge — auth lives in prefs, so surface it here too.
        _draw_signin_hint(layout, context)

        # Soft prompt to connect first. Server URL + auth live in addon prefs.
        if mmcp_client.cached_capabilities() is None:
            # Connecting happens by itself, so this says what is happening
            # rather than asking for a click — and asks again on its own
            # schedule, for a laptop that woke up or a VPN that came back.
            mmcp_client.connect_async()
            box = layout.box()
            err = mmcp_client.last_connection_error()
            if mmcp_client.connecting() or not err:
                box.label(text="Connecting…", icon='SORTTIME')
            else:
                box.label(text="Cannot reach the server", icon='ERROR')
                for line in err.split("\n")[:2]:
                    box.label(text=line)
                box.operator("animatica.connect", icon='FILE_REFRESH', text="Try again")
            return

        # Connected — show model picker.
        layout.prop(settings, "model_id", text="Model")

        # Import the canonical skeleton as a Blender armature (the
        # supported path when supports_retargeting=false). Always
        # available post-connect; prompt-style only when nothing's set.
        from . import canonical_skeleton, remote_asset
        fetching = canonical_skeleton.download_state()
        if fetching["active"]:
            layout.prop(settings, "target_armature", text="Armature")
            # First run: the character is being downloaded on a worker thread.
            # Drawn where the Import button sits, so the click the user just
            # made visibly turned into something, and carrying the three
            # numbers that answer "is this moving, and how long do I wait".
            box = layout.box()
            box.label(text="Fetching the character (first run only)", icon='IMPORT')
            row = box.row()
            row.enabled = False
            row.progress(
                factor=fetching["percent"] / 100.0,
                type='BAR',
                text=(f"{remote_asset.format_bytes(fetching['got'])}"
                      f" / {remote_asset.format_bytes(fetching['total'])}"
                      f"  ({fetching['percent']}%)"),
            )
            sub = box.row()
            sub.active = False
            sub.label(text=remote_asset.format_rate(fetching["speed"]))
            sub.label(text=remote_asset.format_eta(fetching["eta"]))
        elif settings.target_armature is None:
            layout.prop(settings, "target_armature", text="Armature")
            box = layout.box()
            box.label(text="No armature — import a rig to animate on", icon='INFO')
            box.operator(
                "animatica.import_canonical_skeleton",
                icon='ARMATURE_DATA',
                text="Import Animatic character",
            )
        else:
            # Re-importing is rare; it rides on the picker rather than taking
            # a full-width row of its own.
            row = layout.row(align=True)
            row.prop(settings, "target_armature", text="Armature")
            row.operator("animatica.import_canonical_skeleton", text="", icon='IMPORT')

        # Warn only if the clip exceeds the connected model's duration limits.
        _draw_duration_hint(layout, context, settings)

        layout.separator()

        # Generate buttons — state-aware
        if settings.is_generating:
            # Cold-start advisory. The cloud scales to zero between
            # generations during early access, so the first request after
            # an idle period waits ~30–60s for the GPU container + model
            # to boot. Without this hint people think the plugin hung.
            # No server-side progress signal in MMCP v1, so show a live
            # elapsed-time counter rather than a bar frozen at 0%.
            col = layout.column(align=True)
            elapsed = int(getattr(settings, "generation_elapsed", 0))
            col.label(text=f"Working… {elapsed}s", icon='SORTTIME')
            col.operator("animatica.cancel", icon='X', text="Cancel")
            # Said once the wait is long enough to wonder about, not on every
            # generation: the first after an idle spell waits for the model.
            if elapsed >= 10:
                note = layout.row()
                note.active = False
                note.label(text="First run can take 60 s while the model warms up")
        else:
            # Use the dedicated preview flag — ``source_action_name`` is
            # empty for free-form generations (no prior action to restore
            # to), so gating on it would hide the Accept / Reject
            # buttons after a successful free-form bake.
            arm_live = properties._live_armature(settings.target_armature)
            in_preview = (
                arm_live is not None
                and bool(getattr(settings, "is_previewing", False))
            )

            # The examples stay. They are the quickest way to a second block
            # as much as the first — "what else does it do well" is a question
            # someone asks all week, not only in the first five minutes — and
            # a menu that vanishes the moment you use it teaches people not to
            # rely on it. Only the first-run explanation goes away.
            if arm_live is not None:
                has_prompt = any(
                    (b.prompt or "").strip() for b in settings.prompt_blocks
                )
                # In preview too: Regenerate Motion is offered there, so the
                # prompts are still live, and "try another one" is exactly
                # what someone does while looking at a take they are not sure
                # about.
                if has_prompt or in_preview:
                    row = layout.row(align=True)
                    row.menu("ANIMATICA_MT_examples",
                             text="Try an example", icon='PRESET')
                    examples.draw_status(layout)
                else:
                    box = layout.box()
                    box.label(text="Add a prompt to generate", icon='INFO')
                    # The example is the short path: one click writes a prompt
                    # across the scene and the next one generates. Finding the
                    # Timeline, drawing a block and typing into it is three
                    # discoveries before anything moves.
                    box.menu("ANIMATICA_MT_examples",
                             text="Try an example", icon='PRESET')
                    examples.draw_status(box)
                    sub = box.column(align=True)
                    sub.active = False
                    sub.label(text="or double-click the Timeline to add a block,")
                    sub.label(text="then double-click it to type your own.")
                # An example's character may need attribution; this is where
                # the file that uses it gives it.
                examples.draw_credit(layout, context.scene)

            # Why this cannot be sent, asked of the same function the send
            # asks — so the button greys out for exactly the reasons a click
            # would have been refused, and says which one.
            blockers = []
            if arm_live is not None and not in_preview:
                from . import key_poses, request_builder

                try:
                    blockers = request_builder.generation_blockers(
                        scene=context.scene,
                        prompt_blocks=settings.prompt_blocks,
                        constraint_objects=constraints_ui.walk_scene_constraints(context.scene),
                        model_caps=mmcp_client.cached_model(settings.model_id),
                        pose_frames=key_poses.plan(context.scene)["frames"],
                        armature_obj=arm_live,
                    )
                except Exception:               # noqa: BLE001 — never break a draw
                    blockers = []

            model = mmcp_client.cached_model(settings.model_id)
            col = layout.column()
            col.enabled = arm_live is not None

            # Only the clip generation is gated. Generating a single pose
            # carries its own prompt in its own dialog — greying it out
            # because the timeline has no prompt on it would be nonsense.
            gen_text = "Regenerate Motion" if in_preview else "Generate Motion"
            gen = col.column(align=True)
            gen.enabled = not blockers
            row = gen.row()
            row.scale_y = 1.5
            row.operator("animatica.generate", text=gen_text)
            if blockers:
                note = gen.row()
                note.enabled = True         # readable while the button above is not
                note.active = False
                note.label(text=blockers[0], icon='INFO')

            _draw_take_options(col, settings, model, in_preview)
            if arm_live is not None and not in_preview:
                _draw_next_take(col, context)

            # Pose-segment generation is a cloud-only capability — only
            # surface the button when the connected model advertises it.
            if model and "pose" in (model.get("supported_segments") or []):
                pose_text = (
                    f"Regenerate Pose at Frame {context.scene.frame_current}"
                    if in_preview else
                    f"Generate Pose at Frame {context.scene.frame_current}"
                )
                col.separator()
                row = col.row()
                row.scale_y = 1.2
                row.operator("animatica.generate_pose", icon='ARMATURE_DATA', text=pose_text)

            if in_preview:
                _draw_preview(layout, context, settings, arm_live, model)


def _draw_take_options(layout, settings, model, in_preview) -> None:
    """How the next take comes back: as a loop, and on the spot.

    Checkboxes, not toggle buttons: they are options of the take, not modes of
    the tool. Loop is the model's to make, so it is only here when the model
    can; it needs one prompt block, and says so rather than going quiet. While
    a take is previewing, In place moves into the Preview box, where it acts
    on the take in front of you.
    """
    can_loop = bool(model and model.get("supports_loop"))
    if not can_loop and in_preview:
        return
    col = layout.column(heading="Next take" if in_preview else "Options", align=True)
    col.use_property_split = True
    col.use_property_decorate = False
    one_block = len(settings.prompt_blocks) == 1
    if can_loop:
        row = col.row()
        row.active = one_block
        row.prop(settings, "loop")
    if not in_preview:
        col.prop(settings, "inplace")
    if can_loop and settings.loop and not one_block:
        note = layout.row()
        note.active = False
        note.label(text="Loop needs a single prompt block", icon='INFO')


def _draw_next_take(layout, context) -> None:
    """What the next take covers and what it is asked to hit."""
    from . import key_poses

    plan = key_poses.plan(context.scene)
    frames, entries = plan["frames"], plan["entries"]
    dropped = [f for f in frames if not entries[f]["in_range"]]
    sent = len(frames) - len(dropped)
    poses = "no key poses" if not sent else f"{sent} key pose{'' if sent == 1 else 's'}"
    row = layout.row()
    row.active = False
    row.label(text=f"Frames {plan['range'][0]}–{plan['range'][1]} · {poses}")
    if dropped:
        shown = ", ".join(str(f) for f in dropped[:_MAX_NAMED_DROPPED])
        if len(dropped) > _MAX_NAMED_DROPPED:
            shown += f" +{len(dropped) - _MAX_NAMED_DROPPED}"
        warn = layout.row()
        warn.alert = True
        warn.label(text=f"Outside the range, not sent: {shown}", icon='ERROR')


def _draw_preview(layout, context, settings, arm, model) -> None:
    """The take in front of you: what it is, how it plays, keep it or not."""
    layout.separator()
    box = layout.box()
    box.label(text="Previewing take")
    ad = arm.animation_data if arm is not None else None
    looped = ad.action.get("animatica_loop") if ad and ad.action else None
    if looped:
        fps = context.scene.render.fps / (context.scene.render.fps_base or 1.0)
        frames = int(looped["frames"])
        sub = box.row()
        sub.active = False
        sub.label(text=f"Seamless loop · {frames} frames ({frames / fps:.2f} s)", icon='LOOP_FORWARDS')
    # Live on the take: the travel is muted, not removed, so it comes back.
    col = box.column()
    col.use_property_split = True
    col.use_property_decorate = False
    col.prop(settings, "inplace")
    row = box.row(align=True)
    row.scale_y = 1.3
    row.operator("animatica.accept", icon='CHECKMARK', text="Accept")
    row.operator("animatica.reject", icon='X')

    # Re-roll just the active block (keeping its neighbours) — otherwise only
    # reachable by right-clicking a timeline strip. With a single block this
    # is Regenerate Motion, so only surface it when there are blocks to keep.
    if len(settings.prompt_blocks) >= 2:
        op = box.operator(
            "animatica.regenerate_block",
            icon='FILE_REFRESH',
            text="Regenerate Active Block",
        )
        op.block_index = -1


# ═══════════════════════════════════════════════════════════════════════════
# Pose panel — the character: handles, what is drawn, and what will be sent
#
# This was three panels: Constraints (paths and pins), Ghosts (what the
# overlay draws) and Posing (the control rig). They are one job — direct the
# motion — and were separate only because they were built separately. The
# rarely-touched half, paths and pins, is a collapsed child rather than a
# fourth header.
# ═══════════════════════════════════════════════════════════════════════════

_MAX_NAMED_DROPPED = 3


class ANIMATICA_PT_pose(AnimaticaPanelBase, Panel):
    bl_label = "Pose"
    bl_idname = "ANIMATICA_PT_pose"

    def draw(self, context):
        from .autoposer import engine, poser

        layout = self.layout
        settings = context.scene.animatica
        arm = properties._live_armature(settings.target_armature)
        if arm is None:
            layout.label(text="Set a target armature first", icon='INFO')
            return

        # --- the handles ---------------------------------------------------
        status = engine.status()
        if not (status["runtime"] and status["model"]):
            box = layout.box()
            fetching = engine.fetch_state()
            if fetching["running"]:
                box.label(text=f"Downloading the poser… {engine.fetch_percent():.0f}%",
                          icon='SORTTIME')
            else:
                box.label(text="The poser is setting itself up", icon='SORTTIME')
            box.label(text="Preferences → Animatica for detail")
        elif not poser.has_controls(arm):
            layout.operator("autoposer.build_rig", icon='OUTLINER_OB_ARMATURE',
                            text="Add Pose Handles")
        else:
            # The handles, in the artist's words rather than the rig's. Adding
            # one belongs in the same block as picking one, so the + sits with
            # them either way round.
            controls = poser._controls(arm)
            if settings.pose_details:
                col = layout.column(align=True)
                for b in controls:
                    row = col.row(align=True)
                    row.prop(b, "ap_enabled", text=poser.joint_label(b), toggle=True)
                    sub = row.row(align=True)
                    sub.active = b.ap_enabled
                    sub.prop(b, "ap_rot", text="Rot", toggle=True)
                    sub.prop(b, "ap_tol_m", text="")
                col.operator("autoposer.add_control", text="Add Handle", icon='ADD')
            else:
                grid = layout.grid_flow(row_major=True, columns=4, align=True)
                for b in controls:
                    grid.prop(b, "ap_enabled", text=poser.joint_label(b), toggle=True)
                grid.operator("autoposer.add_control", text="", icon='ADD')
            row = layout.row(align=True)
            row.prop(settings, "pose_tightness", slider=True)
            row.prop(settings, "pose_details", text="", icon='OPTIONS')

        _draw_set_keyframe(layout, context, settings)

        # --- the hands: the model has no fingers, so they are chosen here ---
        layout.separator()
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(settings, "hand_pose_left", text="Left Hand")
        col.prop(settings, "hand_pose_right", text="Right Hand")


class ANIMATICA_PT_overlay(AnimaticaPanelBase, Panel):
    """What the viewport draws of the plan: the keyed poses and the trail.

    The master switch is the header checkbox, Blender's own pattern for a
    section that can be off as a whole, and the parts are greyed rather than
    hidden when it is, so the way back is where the artist left it.
    """
    bl_label = "Plan Overlay"
    bl_idname = "ANIMATICA_PT_overlay"
    bl_parent_id = "ANIMATICA_PT_pose"

    @classmethod
    def poll(cls, context):
        return properties._live_armature(context.scene.animatica.target_armature) is not None

    def draw_header(self, context):
        self.layout.prop(context.scene.animatica, "key_pose_overlay", text="")

    def draw(self, context):
        from . import key_poses

        layout = self.layout
        settings = context.scene.animatica
        layout.active = settings.key_pose_overlay
        grid = layout.grid_flow(row_major=True, columns=2, even_columns=True)
        grid.prop(settings, "key_pose_ghosts", text="Ghosts")
        grid.prop(settings, "key_pose_trail", text="Trail")
        sub = grid.row()
        sub.active = key_poses.overlay_on(settings)
        sub.prop(settings, "key_pose_labels", text="Frame Numbers")
        sub = grid.row()
        sub.active = key_poses.overlay_on(settings)
        sub.prop(settings, "key_pose_xray", text="X-Ray")
        if key_poses.trail_on(settings):
            note = layout.row()
            note.active = False
            note.label(text="Drag the trail to repose · Shift: whole body")
        held = key_poses.refresh_held_by() if key_poses.overlay_on(settings) else ""
        if held:
            note = layout.row()
            note.active = False
            note.label(text=f"Refreshing after {held}")


class ANIMATICA_PT_paths(AnimaticaPanelBase, Panel):
    bl_label = "Paths & Pins"
    bl_idname = "ANIMATICA_PT_paths"
    bl_parent_id = "ANIMATICA_PT_pose"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        from . import waypoints

        layout = self.layout
        scene = context.scene
        settings = scene.animatica

        row = layout.row(align=True)
        row.operator("animatica.add_waypoint", icon='MESH_CIRCLE',
                     text=f"Waypoint at {scene.frame_current}")
        row.operator("animatica.add_effector_target", icon='EMPTY_SINGLE_ARROW', text="Pin")

        found = constraints_ui.walk_scene_constraints(scene)
        marks = found["waypoints"]
        root_paths, effectors = found["root_paths"], found["effector_targets"]
        if not marks and not root_paths and not effectors:
            note = layout.column(align=True)
            note.active = False
            note.label(text="Waypoint: where to stand, at this frame")
            note.label(text="Pin: hold a hand or foot somewhere")
            return

        # The route: one row per waypoint, frame editable in place — retiming
        # a waypoint is the most common edit, so it should not need a dialog.
        if marks:
            col = layout.column(align=True)
            for obj in marks:
                row = col.row(align=True)
                row.prop(obj, "animatica_waypoint_frame", text="", icon='MESH_CIRCLE')
                row.prop(obj, "animatica_waypoint_face", text="")
                if obj.animatica_waypoint_face == 'SET':
                    row.prop(obj, "animatica_waypoint_facing", text="")
                op = row.operator("animatica.go_to_waypoint", text="", icon='RESTRICT_SELECT_OFF')
                op.frame = obj.animatica_waypoint_frame
                op = row.operator("animatica.remove_waypoint", text="", icon='X')
                op.frame = obj.animatica_waypoint_frame
            layout.prop(settings, "waypoint_heading")

        # Curves from before waypoints existed still generate, but they hide
        # the timing — so each one offers the way out.
        for obj in root_paths:
            row = layout.row(align=True)
            row.label(text=obj.name, icon='OUTLINER_OB_CURVE')
            op = row.operator("animatica.curve_to_waypoints", text="To waypoints")
            op.name = obj.name
            op = row.operator("animatica.remove_constraint_object", text="", icon='X')
            op.name = obj.name

        for obj in effectors:
            row = layout.row(align=True)
            joint = obj.get("animatica_target_joint", "?")
            keys = _count_location_keyframes(obj)
            row.label(text=f"{joint} ({keys} keys)", icon='EMPTY_SINGLE_ARROW')
            op = row.operator("animatica.focus_constraint_object", text="", icon='RESTRICT_SELECT_OFF')
            op.name = obj.name
            op = row.operator("animatica.remove_constraint_object", text="", icon='X')
            op.name = obj.name


def _count_location_keyframes(obj: bpy.types.Object) -> int:
    if obj.animation_data is None or obj.animation_data.action is None:
        return 0
    return sum(
        len(fc.keyframe_points)
        for fc in constraints_ui.iter_action_fcurves(obj.animation_data.action)
        if fc.data_path == "location"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Settings panel (collapsed by default)
# ═══════════════════════════════════════════════════════════════════════════

class ANIMATICA_PT_settings(AnimaticaPanelBase, Panel):
    bl_label = "Settings"
    bl_idname = "ANIMATICA_PT_settings"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.animatica

        # Seed — set once for a shot, not reached for every generation, so it
        # sits here rather than above the button you press constantly.
        row = layout.row(align=True)
        row.prop(settings, "seed")
        row.operator("animatica.randomize_seed", text="", icon='FILE_REFRESH')
        last_seed = int(getattr(settings, "last_used_seed", 0) or 0)
        if last_seed > 0 and last_seed != int(settings.seed):
            row = layout.row(align=True)
            row.label(text=f"Last run used seed {last_seed}")
            row.operator("animatica.lock_global_seed", text="Lock", icon='LOCKED')

        layout.prop(settings, "quality_preset")
        if settings.quality_preset == "CUSTOM":
            layout.prop(settings, "custom_steps")

        layout.prop(settings, "cfg_enabled")
        if settings.cfg_enabled:
            col = layout.column(align=True)
            col.prop(settings, "cfg_text", slider=True)
            col.prop(settings, "cfg_constraint", slider=True)

        layout.prop(settings, "num_transition_frames")
        # Motion cleanup — tightens keyframe pins and fixes foot skating.
        layout.prop(settings, "post_processing")


class ANIMATICA_PT_settings_posing(AnimaticaPanelBase, Panel):
    """The poser and the overlay: set once, then left alone."""
    bl_label = "Posing"
    bl_idname = "ANIMATICA_PT_settings_posing"
    bl_parent_id = "ANIMATICA_PT_settings"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.animatica
        scene = context.scene
        layout.prop(scene, "ap_floor", text="Solid Floor")
        layout.prop(settings, "key_pose_display", text="Ghost Style")
        layout.prop(settings, "key_pose_auto_refresh")
        arm = properties._live_armature(settings.target_armature)
        if arm is not None:
            layout.prop(arm, "show_in_front", text="Rig In Front")
            layout.prop(scene, "ap_hide_deform", text="Hide Skeleton")
        row = layout.row(align=True)
        row.use_property_split = False
        row.operator("animatica.key_poses_refresh", text="Refresh Ghosts", icon='FILE_REFRESH')
        row.operator("autoposer.rest", text="Rest Pose", icon='LOOP_BACK')


# ═══════════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════════

_classes = (
    ANIMATICA_PT_main,
    ANIMATICA_PT_pose,
    ANIMATICA_PT_overlay,
    ANIMATICA_PT_paths,
    ANIMATICA_PT_settings,
    ANIMATICA_PT_settings_posing,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
