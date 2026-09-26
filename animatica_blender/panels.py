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
        # Examples and help are a click away in the corner, not rows of their
        # own above the button the panel is for.
        row = self.layout.row(align=True)
        row.menu("ANIMATICA_MT_examples", text="", icon='FILE_BLEND')
        row.operator("animatica.open_discord_help", text="", icon='HELP', emboss=False)

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

        # Connected. Nothing to animate yet: setting up is the whole panel.
        from . import canonical_skeleton
        arm_live = properties._live_armature(settings.target_armature)
        if arm_live is None or canonical_skeleton.download_state()["active"]:
            _draw_character(layout, context, settings)
            return

        # Warn only if the clip exceeds the connected model's duration limits.
        _draw_duration_hint(layout, context, settings)

        if settings.is_generating:
            # No server-side progress signal in MMCP v1, so show a live
            # elapsed-time counter rather than a bar frozen at 0%.
            col = layout.column(align=True)
            elapsed = int(getattr(settings, "generation_elapsed", 0))
            row = col.row()
            row.scale_y = 1.5
            row.label(text=f"Working… {elapsed}s", icon='SORTTIME')
            col.operator("animatica.cancel", icon='X', text="Cancel")
            # How long to expect, from the start: the cloud scales to zero
            # between generations, and the first after an idle spell waits
            # for the model to boot.
            note = layout.row()
            note.active = False
            note.label(text="Usually under 30 s · the first run can take a minute")
            return

        # The dedicated preview flag, not ``source_action_name``: that is
        # empty for a free-form generation, which still needs Accept / Reject.
        in_preview = bool(getattr(settings, "is_previewing", False))
        model = mmcp_client.cached_model(settings.model_id)

        has_prompt = any((b.prompt or "").strip() for b in settings.prompt_blocks)

        # Why this cannot be sent, asked of the same function the send asks --
        # so the button greys out for exactly the reasons a click would have
        # been refused, and says which one.
        blockers = []
        if not in_preview:
            from . import key_poses, request_builder

            try:
                blockers = request_builder.generation_blockers(
                    scene=context.scene,
                    prompt_blocks=settings.prompt_blocks,
                    constraint_objects=constraints_ui.walk_scene_constraints(context.scene),
                    model_caps=model,
                    pose_frames=key_poses.plan(context.scene)["frames"],
                    armature_obj=arm_live,
                )
            except Exception:               # noqa: BLE001 — never break a draw
                blockers = []

        # A take in front of you: keep it or not comes first.
        if in_preview:
            _draw_preview(layout, context, settings, arm_live)
        elif not has_prompt:
            # First run: say in one line what this is.
            note = layout.row()
            note.active = False
            note.label(text="Make character motion from text and poses")

        # Top to bottom as the work goes: say what happens, direct it, make it.
        _draw_prompt(layout, settings, has_prompt, in_preview)
        _draw_direction(layout, context, settings, in_preview)

        layout.separator()
        gen = layout.column(align=True)
        gen.enabled = not blockers
        row = gen.row()
        row.scale_y = 1.5
        # Every run is a new take (seed 0), so "again" is what it does.
        row.operator("animatica.generate",
                     text="Generate Again" if in_preview else "Generate Motion")
        # With no prompt, the field above already says what is missing.
        if blockers and has_prompt:
            note = gen.row()
            note.enabled = True         # readable while the button above is not
            note.active = False
            note.label(text=blockers[0], icon='INFO')
        if not in_preview:
            _draw_next_take(layout, context)
            _draw_kept(layout, arm_live)
        examples.draw_status(layout)

        # An example's character may need attribution; this is where the file
        # that uses it gives it.
        examples.draw_credit(layout, context.scene)


def _draw_prompt(layout, settings, has_prompt, in_preview) -> None:
    """What the character should do: the selected Timeline block's prompt.

    Always here, not only before the first take -- it is what Generate Again
    reads, and it went out of sight the moment a take came back. The Timeline
    is where blocks are added and timed, and the line under the field says so.
    """
    blocks = settings.prompt_blocks
    if not len(blocks):
        return
    i = min(max(settings.active_block_index, 0), len(blocks) - 1)
    col = layout.column(align=True)
    label = "Prompt" if len(blocks) == 1 else f"Prompt · block {i + 1} of {len(blocks)}"
    col.label(text=label)
    col.prop(blocks[i], "prompt", text="", placeholder="e.g. a person waves hello")
    sub = col.row()
    sub.active = False
    if has_prompt:
        sub.label(text="Add more actions on the Timeline")
    else:
        sub.label(text="Also editable on the Timeline block")
    if not has_prompt and not in_preview:
        layout.menu("ANIMATICA_MT_examples", text="Or open an example scene", icon='FILE_BLEND')


def _draw_direction(layout, context, settings, in_preview) -> None:
    """The ways to steer a take besides the prompt, each with its count.

    These are what make the result yours -- poses the motion is made to pass
    through, places to stand when, a hand or foot held -- and they were a
    closed panel or two away, so a newcomer took the tool for a prompt box.
    """
    from . import key_poses

    plan = key_poses.plan(context.scene)
    frames, entries = plan["frames"], plan["entries"]
    dropped = [f for f in frames if not entries[f]["in_range"]]
    n_poses = len(frames) - len(dropped)
    found = constraints_ui.walk_scene_constraints(context.scene)
    n_marks, n_pins = len(found["waypoints"]), len(found["effector_targets"])

    col = layout.column(align=True)
    col.label(text="Direct it")
    rows = (
        ("Key poses", n_poses, "animatica.set_key_pose", "Set Keyframe", 'KEYFRAME_HLT'),
        ("Waypoints", n_marks, "animatica.add_waypoint", "Add Waypoint", 'MESH_CIRCLE'),
        ("Pins", n_pins, "animatica.add_effector_target", "Add Pin", 'EMPTY_SINGLE_ARROW'),
    )
    for label, n, op, text, icon in rows:
        split = col.split(factor=0.45, align=True)
        left = split.row()
        left.active = n > 0
        left.label(text=f"{label}  {n}")
        split.operator(op, text=text, icon=icon)
    note = col.row()
    note.active = False
    if not (n_poses or n_marks or n_pins):
        note.label(text="Pose the rig, then Set Keyframe")
    elif in_preview:
        # Blender keeps a key's type when you key over it, so I on a take
        # writes a key the addon reads as the model's own and leaves out.
        note.label(text="On a take, use Set Keyframe, not I")
    if dropped:
        shown = ", ".join(str(f) for f in dropped[:_MAX_NAMED_DROPPED])
        if len(dropped) > _MAX_NAMED_DROPPED:
            shown += f" +{len(dropped) - _MAX_NAMED_DROPPED}"
        warn = layout.row()
        warn.alert = True
        warn.label(text=f"Outside the range, not sent: {shown}", icon='ERROR')


def _draw_character(layout, context, settings) -> None:
    """Which model animates which rig. Set once per scene.

    In the main panel while nothing is set up, since then it is the only thing
    to do; afterwards in the collapsed Character sub-panel.
    """
    from . import canonical_skeleton, remote_asset

    # A choice only when there is one to make.
    caps = mmcp_client.cached_capabilities() or {}
    if len(caps.get("models") or []) > 1:
        layout.prop(settings, "model_id", text="Model")
    fetching = canonical_skeleton.download_state()
    if fetching["active"]:
        layout.prop(settings, "target_armature", text="Armature")
        # First run: the character is being downloaded on a worker thread.
        # Drawn where the Import button sits, so the click just made visibly
        # turned into something, with the three numbers that answer "is this
        # moving, and how long do I wait".
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
    elif properties._live_armature(settings.target_armature) is None:
        box = layout.box()
        box.label(text="No character yet")
        row = box.row()
        row.scale_y = 1.4
        row.operator(
            "animatica.import_canonical_skeleton",
            icon='ARMATURE_DATA',
            text="Add Ready-Made Character",
        )
        sub = box.row()
        sub.active = False
        sub.label(text="or animate your own rig:")
        box.prop(settings, "target_armature", text="")
    else:
        # Re-importing is rare; it rides on the picker rather than taking a
        # full-width row of its own.
        row = layout.row(align=True)
        row.prop(settings, "target_armature", text="Armature")
        row.operator("animatica.import_canonical_skeleton", text="", icon='IMPORT')


def _draw_next_take(layout, context) -> None:
    """How long the next take is, and which frames it covers."""
    from . import key_poses

    a, b = key_poses.plan(context.scene)["range"]
    fps = context.scene.render.fps / (context.scene.render.fps_base or 1.0)
    row = layout.row()
    row.active = False
    row.label(text=f"{(b - a + 1) / fps:.1f} s · frames {a}–{b}")


def _draw_kept(layout, arm) -> None:
    """Where an accepted take went. Accept moves it off the rig onto the NLA,
    and without a word the keyframes vanishing reads as the take lost."""
    ad = arm.animation_data if arm is not None else None
    if ad is None or not any(t.name.startswith("Animatica: ") for t in ad.nla_tracks):
        return
    row = layout.row()
    row.active = False
    row.label(text="Kept take is on the NLA", icon='NLA')


def _draw_preview(layout, context, settings, arm) -> None:
    """The take in front of you: what made it, how it plays, keep it or not."""
    from . import key_poses

    box = layout.box()
    box.label(text="Previewing take")
    info = box.column(align=True)
    info.active = False
    ad = arm.animation_data if arm is not None else None
    looped = ad.action.get("animatica_loop") if ad and ad.action else None
    if looped:
        fps = context.scene.render.fps / (context.scene.render.fps_base or 1.0)
        frames = int(looped["frames"])
        info.label(text=f"Seamless loop · {frames} frames ({frames / fps:.2f} s)", icon='LOOP_FORWARDS')
    if key_poses.trail_on(settings):
        # The lines drawn through the body are a tool, not Blender's own
        # motion paths, and nothing else says so where they are seen.
        info.label(text="Blue trail: drag it to repose the body", icon='CURVE_PATH')
    # Which take this is, so one worth keeping can be had again.
    last = int(getattr(settings, "last_used_seed", 0) or 0)
    if last > 0:
        row = box.row(align=True)
        locked = int(settings.seed) == last
        sub = row.row()
        sub.active = False
        sub.label(text=f"Seed {last}")
        lock = row.row()
        lock.enabled = not locked
        lock.operator("animatica.lock_global_seed", text="Locked" if locked else "Lock",
                      icon='LOCKED' if locked else 'UNLOCKED')
    # Live on the take: the travel is muted, not removed, so it comes back.
    col = box.column()
    col.use_property_split = True
    col.use_property_decorate = False
    col.prop(settings, "inplace")
    # Accept replaces a take kept before; say so on the button, not only in
    # its tooltip.
    kept = ad is not None and any(t.name.startswith("Animatica: ") for t in ad.nla_tracks)
    row = box.row(align=True)
    row.scale_y = 1.3
    row.operator("animatica.accept", icon='CHECKMARK',
                 text="Replace Kept" if kept else "Accept")
    row.operator("animatica.reject", icon='X')

    # Re-roll just the active block (keeping its neighbours) — otherwise only
    # reachable by right-clicking a timeline strip. With a single block this
    # is Generate Again, so only surface it when there are blocks to keep.
    if len(settings.prompt_blocks) >= 2:
        op = box.operator(
            "animatica.regenerate_block",
            icon='FILE_REFRESH',
            text="Regenerate Active Block",
        )
        op.block_index = -1
    layout.separator()


def _setup_done(context) -> bool:
    from . import canonical_skeleton
    settings = context.scene.animatica
    return (mmcp_client.cached_capabilities() is not None
            and properties._live_armature(settings.target_armature) is not None
            and not canonical_skeleton.download_state()["active"])


class ANIMATICA_PT_options(AnimaticaPanelBase, Panel):
    """How a take comes back: as a loop, on the spot, and the hands.

    One set of options among the ways to shape a take, not the headline: at
    the top level, Loop and In place made the tool read as a cycle generator.
    The header says what is on, so it can stay closed.
    """
    bl_label = "Options"
    bl_idname = "ANIMATICA_PT_options"
    bl_parent_id = "ANIMATICA_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _setup_done(context) and not context.scene.animatica.is_generating

    def draw_header_preset(self, context):
        settings = context.scene.animatica
        model = mmcp_client.cached_model(settings.model_id)
        on = []
        if settings.loop and model and model.get("supports_loop"):
            on.append("Loop")
        if settings.inplace:
            on.append("In place")
        row = self.layout.row()
        row.active = False
        row.label(text=" · ".join(on))

    def draw(self, context):
        layout = self.layout
        settings = context.scene.animatica
        model = mmcp_client.cached_model(settings.model_id)
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        # Loop is the model's to make: only here when the model can, and it
        # needs one prompt block, which it says rather than going quiet.
        can_loop = bool(model and model.get("supports_loop"))
        one_block = len(settings.prompt_blocks) == 1
        if can_loop:
            row = col.row()
            row.active = one_block
            row.prop(settings, "loop")
        col.prop(settings, "inplace")
        if can_loop and settings.loop and not one_block:
            note = layout.row()
            note.active = False
            note.label(text="Loop needs a single prompt block", icon='INFO')
        elif can_loop and settings.loop:
            # The block's length is the loop's length.
            b = settings.prompt_blocks[0]
            fps = context.scene.render.fps / (context.scene.render.fps_base or 1.0)
            if (b.frame_end - b.frame_start + 1) / fps > 4.5:
                note = layout.row()
                note.active = False
                note.label(text="Loops work best at 2–4 s", icon='INFO')
        # The fingers: the model has none, so each hand's pose is chosen here
        # and laid on every take.
        col = layout.column(align=True)
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(settings, "hand_pose_left", text="Left Hand")
        col.prop(settings, "hand_pose_right", text="Right Hand")


class ANIMATICA_PT_character(AnimaticaPanelBase, Panel):
    """Model and rig: set once, then out of the way.

    The header says which rig is targeted, so it can stay closed.
    """
    bl_label = "Character"
    bl_idname = "ANIMATICA_PT_character"
    bl_parent_id = "ANIMATICA_PT_main"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _setup_done(context)

    def draw_header_preset(self, context):
        arm = properties._live_armature(context.scene.animatica.target_armature)
        row = self.layout.row()
        row.active = False
        row.label(text=arm.name if arm is not None else "")

    def draw(self, context):
        layout = self.layout
        settings = context.scene.animatica
        _draw_character(layout, context, settings)


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
    bl_options = {'DEFAULT_CLOSED'}

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

        # --- the pose at this frame: proposed by the model, or stated -----
        layout.separator()
        model = mmcp_client.cached_model(settings.model_id)
        if model and "pose" in (model.get("supported_segments") or []):
            # Pose-segment generation is a cloud-only capability.
            text = f"Generate Pose at Frame {context.scene.frame_current}"
            if settings.is_previewing:
                text = "Re" + text[0].lower() + text[1:]
            row = layout.row()
            row.scale_y = 1.2
            row.enabled = not settings.is_generating
            row.operator("animatica.generate_pose", icon='ARMATURE_DATA', text=text)
        _draw_set_keyframe(layout, context, settings)


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

        # Motion cleanup — feet that do not slide, poses hit more exactly.
        layout.prop(settings, "post_processing")


class ANIMATICA_PT_settings_advanced(AnimaticaPanelBase, Panel):
    """How closely the model follows, and how blocks join. Rarely touched."""
    bl_label = "Advanced"
    bl_idname = "ANIMATICA_PT_settings_advanced"
    bl_parent_id = "ANIMATICA_PT_settings"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        settings = context.scene.animatica
        layout.prop(settings, "cfg_enabled")
        if settings.cfg_enabled:
            col = layout.column(align=True)
            col.prop(settings, "cfg_text", slider=True)
            col.prop(settings, "cfg_constraint", slider=True)
        layout.prop(settings, "num_transition_frames", text="Blend Between Blocks")


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
    ANIMATICA_PT_options,
    ANIMATICA_PT_character,
    ANIMATICA_PT_pose,
    ANIMATICA_PT_overlay,
    ANIMATICA_PT_paths,
    ANIMATICA_PT_settings,
    ANIMATICA_PT_settings_advanced,
    ANIMATICA_PT_settings_posing,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
