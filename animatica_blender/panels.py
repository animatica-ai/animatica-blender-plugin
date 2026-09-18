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

from . import constraints_ui, mmcp_client, properties


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
    box = layout.box()
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

    Sits with the pose-generation buttons because it answers the same
    question — what is the pose at this frame — from the other direction: the
    model proposes one, this one states one. It needs no server, so it is
    drawn while disconnected too; everything else in that part of the panel
    does, which is why this is a helper with two call sites rather than a line
    in one place.
    """
    arm = properties._live_armature(settings.target_armature)
    if arm is None:
        return
    row = layout.row()
    row.scale_y = 1.2
    row.operator("animatica.set_key_pose", icon='KEYFRAME_HLT', text="Set Keyframe")
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

    def draw(self, context):
        layout = self.layout
        settings = context.scene.animatica

        layout.operator(
            "animatica.open_discord_help",
            icon='URL',
            text="Need help?",
        )

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
            # Keying a pose is local work — no reason to make it wait on a
            # server the artist has not connected to yet.
            _draw_set_keyframe(layout, context, settings)
            return

        # Connected — show model picker.
        layout.prop(settings, "model_id", text="Model")

        # Armature
        layout.prop(settings, "target_armature", text="Armature")

        # Import the canonical skeleton as a Blender armature (the
        # supported path when supports_retargeting=false). Always
        # available post-connect; prompt-style only when nothing's set.
        from . import canonical_skeleton, remote_asset
        fetching = canonical_skeleton.download_state()
        if fetching["active"]:
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
            box = layout.box()
            box.label(text="No armature — import a rig to animate on", icon='INFO')
            box.operator(
                "animatica.import_canonical_skeleton",
                icon='ARMATURE_DATA',
                text="Import Animatic character",
            )
        else:
            row = layout.row()
            row.operator(
                "animatica.import_canonical_skeleton",
                icon='ARMATURE_DATA',
                text="Re-import Animatic character",
            )

        # Warn only if the clip exceeds the connected model's duration limits.
        _draw_duration_hint(layout, context, settings)

        layout.separator()

        # Generate buttons — state-aware
        if settings.is_generating:
            # Cold-start advisory. The cloud scales to zero between
            # generations during early access, so the first request after
            # an idle period waits ~30–60s for the GPU container + model
            # to boot. Without this hint people think the plugin hung.
            box = layout.box()
            box.label(text="Early access — heads up", icon='SORTTIME')
            box.label(text="First generation can take 60s+")
            box.label(text="while the model warms up.")
            # No server-side progress signal in MMCP v1, so show a live
            # elapsed-time counter rather than a bar frozen at 0%.
            col = layout.column(align=True)
            elapsed = int(getattr(settings, "generation_elapsed", 0))
            col.label(text=f"Working… {elapsed}s", icon='SORTTIME')
            col.operator("animatica.cancel", icon='X', text="Cancel")
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

            # First-run nudge: prompts live on the Timeline, which isn't
            # obvious. Shown until the user authors prompt text, so it
            # disappears on its own once they're going.
            if arm_live is not None and not in_preview:
                has_prompt = any(
                    (b.prompt or "").strip() for b in settings.prompt_blocks
                )
                if not has_prompt:
                    box = layout.box()
                    box.label(text="Add a prompt to generate", icon='INFO')
                    box.label(text="Double-click the Timeline to add a block,")
                    box.label(text="then double-click it to type a prompt.")

            col = layout.column(align=True)
            col.enabled = arm_live is not None

            gen_text = "Regenerate Motion" if in_preview else "Generate Motion"
            row = col.row()
            row.scale_y = 1.5
            row.operator("animatica.generate", icon='PLAY', text=gen_text)

            # Pose-segment generation is a cloud-only capability — only
            # surface the button when the connected model advertises it.
            model = mmcp_client.cached_model(settings.model_id)
            if model and "pose" in (model.get("supported_segments") or []):
                pose_text = (
                    f"Regenerate Pose @ Frame {context.scene.frame_current}"
                    if in_preview else
                    f"Generate Pose @ Frame {context.scene.frame_current}"
                )
                row = col.row()
                row.scale_y = 1.2
                row.operator("animatica.generate_pose", icon='ARMATURE_DATA', text=pose_text)

            _draw_set_keyframe(col, context, settings)

            if in_preview:
                layout.separator()
                box = layout.box()
                box.label(text="Preview", icon='INFO')
                # In-place toggle lives here so it's only surfaced when
                # there's a preview to apply it to. Non-destructive: live-
                # toggle adds / removes a Limit Location constraint on the
                # root bone; Accept bakes the result into the final
                # per-block actions.
                box.prop(settings, "inplace", icon='LOCKED' if settings.inplace else 'UNLOCKED')
                row = box.row(align=True)
                row.scale_y = 1.3
                row.operator("animatica.accept", icon='CHECKMARK', text="Accept")
                row.operator("animatica.reject", icon='X')

                # Re-roll just the active block (keeping its neighbours) —
                # otherwise only reachable by right-clicking a timeline strip.
                # With a single block this is equivalent to Regenerate Motion,
                # so only surface it when there are blocks to keep.
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
        from . import key_poses, pose_edit
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
            box.label(text="The poser is still setting itself up", icon='SORTTIME')
            box.label(text="Preferences → Animatica for progress")
        elif not poser.has_controls(arm):
            layout.operator("autoposer.build_rig", icon='OUTLINER_OB_ARMATURE',
                            text="Add Pose Controls")
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

        # --- what is drawn --------------------------------------------------
        layout.separator()
        col = layout.column(align=True)
        col.prop(settings, "key_pose_overlay", text="Show Plan", toggle=True,
                 icon='HIDE_OFF' if settings.key_pose_overlay else 'HIDE_ON')
        # What the plan is made of. Greyed rather than hidden when the master
        # is off, so the way back is where the artist left it.
        parts = col.column(align=True)
        parts.active = settings.key_pose_overlay
        row = parts.row(align=True)
        row.prop(settings, "key_pose_ghosts", text="Ghosts", toggle=True)
        row.prop(settings, "key_pose_trail", text="Trail", toggle=True)
        row = parts.row(align=True)
        row.active = key_poses.overlay_on(settings)
        row.prop(settings, "key_pose_labels", text="Numbers", toggle=True)
        row.prop(settings, "key_pose_xray", text="X-Ray", toggle=True)
        if key_poses.ghosts_on(settings):
            sub = col.row()
            sub.active = False
            sub.label(text="drag a curve · shift moves the pose")

        # --- what will be sent ----------------------------------------------
        plan = key_poses.plan(context.scene)
        frames, entries = plan["frames"], plan["entries"]
        dropped = [f for f in frames if not entries[f]["in_range"]]
        layout.separator()
        info = layout.column(align=True)
        sent = len(frames) - len(dropped)
        info.label(text=f"{sent} key pose{'' if sent == 1 else 's'} sent as constraints")
        sub = info.row()
        sub.active = False
        sub.label(text=f"Generating frames {plan['range'][0]}–{plan['range'][1]}")
        if dropped:
            shown = ", ".join(str(f) for f in dropped[:_MAX_NAMED_DROPPED])
            if len(dropped) > _MAX_NAMED_DROPPED:
                shown += f" +{len(dropped) - _MAX_NAMED_DROPPED}"
            warn = layout.row()
            warn.alert = True
            warn.label(text=f"Not sent: {shown}", icon='ERROR')
        held = key_poses.refresh_held_by() if key_poses.overlay_on(settings) else ""
        if held:
            note = layout.row()
            note.active = False
            note.label(text=f"refreshing after {held}")

        # A rig left detached by an older session; nothing here creates one.
        if pose_edit.autoposer_holds(arm):
            box = layout.box()
            box.label(text="Autoposer is holding this rig", icon='INFO')
            box.operator("animatica.give_back_rig", icon='LOOP_BACK', text="Give Back Rig")


class ANIMATICA_PT_paths(AnimaticaPanelBase, Panel):
    bl_label = "Paths & Pins"
    bl_idname = "ANIMATICA_PT_paths"
    bl_parent_id = "ANIMATICA_PT_pose"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.animatica

        row = layout.row(align=True)
        row.operator("animatica.add_root_path", icon='OUTLINER_OB_CURVE', text="Root path")
        row.operator("animatica.add_effector_target", icon='EMPTY_SINGLE_ARROW', text="Pin")

        found = constraints_ui.walk_scene_constraints(scene)
        root_paths, effectors = found["root_paths"], found["effector_targets"]
        if not root_paths and not effectors:
            note = layout.row()
            note.active = False
            note.label(text="a curve to travel along, an empty to pin a hand to")
            return

        if root_paths:
            layout.prop(settings, "preview_path_snap", text="Snap armature", toggle=True)
        for obj in root_paths + effectors:
            row = layout.row(align=True)
            if obj in effectors:
                joint = obj.get("animatica_target_joint", "?")
                keys = _count_location_keyframes(obj)
                row.label(text=f"{joint} ({keys} keys)", icon='EMPTY_SINGLE_ARROW')
            else:
                label = obj.name + ("  ↗" if obj.get("animatica_match_direction") else "")
                row.label(text=label, icon='OUTLINER_OB_CURVE')
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
        settings = context.scene.animatica
        scene = context.scene

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

        layout.separator()

        # Quality
        layout.prop(settings, "quality_preset")
        if settings.quality_preset == "CUSTOM":
            layout.prop(settings, "custom_steps")

        # CFG
        layout.separator()
        layout.prop(settings, "cfg_enabled")
        if settings.cfg_enabled:
            col = layout.column(align=True)
            col.prop(settings, "cfg_text", slider=True)
            col.prop(settings, "cfg_constraint", slider=True)

        layout.separator()
        layout.prop(settings, "num_transition_frames")

        # Motion cleanup — tightens keyframe pins and fixes foot skating.
        # Requires the server to have `motion_correction` installed.
        layout.prop(settings, "post_processing")

        # --- the poser and the overlay: set once, then left alone ----------
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Posing")
        col.prop(scene, "ap_floor", text="Floor is solid")
        col.prop(settings, "key_pose_display", text="Ghosts show")
        col.prop(settings, "key_pose_auto_refresh")
        row = col.row(align=True)
        row.operator("animatica.key_poses_refresh", text="Refresh Ghosts", icon='FILE_REFRESH')
        row.operator("autoposer.rest", text="Rest Pose", icon='LOOP_BACK')
        arm = properties._live_armature(settings.target_armature)
        if arm is not None:
            row = col.row(align=True)
            row.prop(arm, "show_in_front", text="In front", icon='XRAY')
            row.prop(scene, "ap_hide_deform", text="Hide skeleton", icon='HIDE_ON')
        # Note: the In-place toggle lives in the Preview box on the main
        # panel — it's only meaningful while reviewing a generation.


# ═══════════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════════

_classes = (
    ANIMATICA_PT_main,
    ANIMATICA_PT_pose,
    ANIMATICA_PT_paths,
    ANIMATICA_PT_settings,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
