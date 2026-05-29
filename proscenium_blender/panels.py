"""Blender UI panels for the Proscenium addon.

Sidebar panels in View3D > Sidebar > Proscenium:
  - Main: connect, model picker, armature, seed, generate buttons, preview
  - Constraints: root path / effector / pose-keyframe controls
  - Settings: quality / CFG / post-processing

Server URL + auth live in Edit > Preferences > Add-ons > Proscenium.
"""

import bpy
from bpy.types import Panel

from . import constraints_ui, mmcp_client, properties


class ProsceniumPanelBase:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Proscenium"


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
    box.operator("proscenium.signin", icon='IMPORT', text="Sign in")
    return True


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

class PROSCENIUM_PT_main(ProsceniumPanelBase, Panel):
    bl_label = "Proscenium"
    bl_idname = "PROSCENIUM_PT_main"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.proscenium

        layout.operator(
            "proscenium.open_discord_help",
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
                row.operator("proscenium.open_upgrade", icon='URL', text="Upgrade")
            row.operator("proscenium.dismiss_quota", icon='X', text="Dismiss")

        # Cloud sign-in nudge — auth lives in prefs, so surface it here too.
        _draw_signin_hint(layout, context)

        # Soft prompt to connect first. Server URL + auth live in addon prefs.
        if mmcp_client.cached_capabilities() is None:
            box = layout.box()
            err = mmcp_client.last_connection_error()
            if err:
                box.label(text="Connection failed", icon='ERROR')
                for line in err.split("\n")[:3]:
                    box.label(text=line)
            else:
                box.label(text="Connect to a server first", icon='INFO')
            box.operator("proscenium.connect", icon='URL', text="Connect")
            return

        # Connected — show model picker.
        layout.prop(settings, "model_id", text="Model")

        # Armature
        layout.prop(settings, "target_armature", text="Armature")

        # Import the canonical skeleton as a Blender armature (the
        # supported path when supports_retargeting=false). Always
        # available post-connect; prompt-style only when nothing's set.
        if settings.target_armature is None:
            box = layout.box()
            box.label(text="No armature — import the canonical one", icon='INFO')
            box.operator(
                "proscenium.import_canonical_skeleton",
                icon='ARMATURE_DATA',
                text=f"Import {settings.model_id or 'model'} skeleton",
            )
        else:
            row = layout.row()
            row.operator(
                "proscenium.import_canonical_skeleton",
                icon='ARMATURE_DATA',
                text=f"Re-import {settings.model_id or 'model'} skeleton",
            )

        # Seed (frequently tweaked when regenerating — kept next to Generate)
        row = layout.row(align=True)
        row.prop(settings, "seed")
        row.operator("proscenium.randomize_seed", text="", icon='FILE_REFRESH')

        # Offer to lock the concrete seed the last run used — only meaningful
        # when Seed was left on auto (0) and the recorded value differs.
        last_seed = int(getattr(settings, "last_used_seed", 0) or 0)
        if last_seed > 0 and last_seed != int(settings.seed):
            row = layout.row(align=True)
            row.label(text=f"Last run used seed {last_seed}")
            row.operator("proscenium.lock_global_seed", text="Lock", icon='LOCKED')

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
            col.operator("proscenium.cancel", icon='X', text="Cancel")
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
            row.operator("proscenium.generate", icon='PLAY', text=gen_text)

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
                row.operator("proscenium.generate_pose", icon='ARMATURE_DATA', text=pose_text)

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
                row.operator("proscenium.accept", icon='CHECKMARK', text="Accept")
                row.operator("proscenium.reject", icon='X')

                # Re-roll just the active block (keeping its neighbours) —
                # otherwise only reachable by right-clicking a timeline strip.
                # With a single block this is equivalent to Regenerate Motion,
                # so only surface it when there are blocks to keep.
                if len(settings.prompt_blocks) >= 2:
                    op = box.operator(
                        "proscenium.regenerate_block",
                        icon='FILE_REFRESH',
                        text="Regenerate Active Block",
                    )
                    op.block_index = -1


# ═══════════════════════════════════════════════════════════════════════════
# Constraints panel
# ═══════════════════════════════════════════════════════════════════════════

class PROSCENIUM_PT_constraints(ProsceniumPanelBase, Panel):
    bl_label = "Constraints"
    bl_idname = "PROSCENIUM_PT_constraints"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.proscenium

        # Add row
        row = layout.row(align=True)
        row.operator("proscenium.add_root_path",       icon='OUTLINER_OB_CURVE',  text="Root path")
        row.operator("proscenium.add_effector_target", icon='EMPTY_SINGLE_ARROW', text="Effector pin")

        found = constraints_ui.walk_scene_constraints(scene)
        root_paths = found["root_paths"]
        effectors  = found["effector_targets"]

        # Root paths
        layout.separator()
        header = layout.row(align=True)
        header.label(text=f"Root paths ({len(root_paths)})")
        header.prop(settings, "preview_path_snap", text="Snap armature", toggle=True)
        if not root_paths:
            layout.label(text="    (none — add a curve to define a trajectory)", icon='INFO')
        for obj in root_paths:
            row = layout.row(align=True)
            label = obj.name
            if obj.get("proscenium_match_direction"):
                label += "  ↗"
            row.label(text=label, icon='OUTLINER_OB_CURVE')
            op = row.operator("proscenium.focus_constraint_object", text="", icon='RESTRICT_SELECT_OFF')
            op.name = obj.name
            op = row.operator("proscenium.remove_constraint_object", text="", icon='X')
            op.name = obj.name

        # Effector pins
        layout.separator()
        layout.label(text=f"Effector pins ({len(effectors)})")
        if not effectors:
            layout.label(text="    (none — add an empty to pin a joint)", icon='INFO')
        for obj in effectors:
            row = layout.row(align=True)
            joint = obj.get("proscenium_target_joint", "?")
            keys  = _count_location_keyframes(obj)
            row.label(text=f"{joint} → {obj.name} ({keys} keys)", icon='EMPTY_SINGLE_ARROW')
            op = row.operator("proscenium.focus_constraint_object", text="", icon='RESTRICT_SELECT_OFF')
            op.name = obj.name
            op = row.operator("proscenium.remove_constraint_object", text="", icon='X')
            op.name = obj.name

        # Pose keyframes derived from the source action.
        layout.separator()
        arm = settings.target_armature
        if arm is not None and arm.animation_data and arm.animation_data.action:
            ac = arm.animation_data.action
            n = sum(
                1 for fc in constraints_ui.iter_action_fcurves(ac)
                if "rotation" in fc.data_path
                for _ in fc.keyframe_points
            )
            layout.label(text=f"Pose keyframes: {n}  (sampled from {ac.name})", icon='KEYFRAME_HLT')
        else:
            layout.label(text="Pose keyframes: 0  (set a target armature)", icon='KEYFRAME')


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

class PROSCENIUM_PT_settings(ProsceniumPanelBase, Panel):
    bl_label = "Settings"
    bl_idname = "PROSCENIUM_PT_settings"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.proscenium

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
        # Note: the In-place toggle lives in the Preview box on the main
        # panel — it's only meaningful while reviewing a generation.


# ═══════════════════════════════════════════════════════════════════════════
# Registration
# ═══════════════════════════════════════════════════════════════════════════

_classes = (
    PROSCENIUM_PT_main,
    PROSCENIUM_PT_constraints,
    PROSCENIUM_PT_settings,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
