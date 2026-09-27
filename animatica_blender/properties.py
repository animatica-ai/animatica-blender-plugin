"""Blender property definitions for Animatica addon state."""

import json

import sys

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import AddonPreferences, PropertyGroup

from . import autoposer
from .autoposer import prefs as autoposer_prefs
from .hand_pose import STYLES as _hand_pose_items

# Whether this build is a preview (the zip stamps VERSION_TAG, e.g. v0.6.0-preview7).
_PREVIEW_BUILD = "-preview" in getattr(sys.modules.get(__package__), "VERSION_TAG", "")


def _on_hand_pose(settings, context):
    """Show a changed hand pose at once: on the preview if there is one, else on the rig."""
    from . import hand_pose

    arm = _live_armature(settings.target_armature)
    if arm is None:
        return
    ad = arm.animation_data
    action = ad.action if ad is not None else None
    if settings.is_previewing and action is not None:
        from . import preview_session
        lo, hi = (int(v) for v in action.frame_range)
        with preview_session.keeping_edits(action):     # the take's keys, not edits
            hand_pose.apply(arm, action, settings, (lo, hi))
    else:
        hand_pose.show(arm, settings)


# ---------------------------------------------------------------------------
# Per-armature prompt-block persistence
# ---------------------------------------------------------------------------

_BLOCKS_KEY = "animatica_prompt_blocks"
_ACTIVE_KEY = "animatica_active_block_index"


def _serialize_blocks(blocks) -> str:
    return json.dumps([
        {
            "prompt": b.prompt,
            "frame_start": b.frame_start,
            "frame_end": b.frame_end,
            "enabled": b.enabled,
            "color": list(b.color),
            "seed": int(getattr(b, "seed", 0)),
            "last_used_seed": int(getattr(b, "last_used_seed", 0)),
        }
        for b in blocks
    ])


def save_blocks_to_armature(arm_obj, settings):
    """Pickle prompt_blocks + active_block_index onto the armature's ID props."""
    if arm_obj is None:
        return
    try:
        _ = arm_obj.name
    except ReferenceError:
        return
    arm_obj[_BLOCKS_KEY] = _serialize_blocks(settings.prompt_blocks)
    arm_obj[_ACTIVE_KEY] = int(settings.active_block_index)


def load_blocks_from_armature(arm_obj, settings):
    """Replace settings.prompt_blocks with the serialized list on *arm_obj*.
    If the armature has no stored blocks, creates a single default block
    spanning the scene frame range."""
    settings.prompt_blocks.clear()
    if arm_obj is None:
        settings.active_block_index = 0
        return

    raw = arm_obj.get(_BLOCKS_KEY)
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = []
        for item in data:
            b = settings.prompt_blocks.add()
            b.prompt = item.get("prompt", "")
            b.frame_start = int(item.get("frame_start", 1))
            b.frame_end = int(item.get("frame_end", 250))
            b.enabled = bool(item.get("enabled", True))
            color = item.get("color") or [0, 0, 0, 0]
            b.color = color[:4] + [0] * (4 - len(color))
            # ``seed`` was added with per-block regenerate; older scenes serialised
            # without it default to 0 (= "server picks a random seed").
            b.seed = int(item.get("seed", 0))
            # ``last_used_seed`` records the concrete seed of the last generation
            # (added with client-side seed recording); older scenes default to 0.
            b.last_used_seed = int(item.get("last_used_seed", 0))
        settings.active_block_index = int(arm_obj.get(_ACTIVE_KEY, 0))
        return

    # No stored data — seed with one default block: four seconds, or the scene
    # range if that is shorter. Covering a default scene's 250 frames, the
    # first thing anyone generated was a ten-second wave.
    scene = bpy.context.scene
    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    b = settings.prompt_blocks.add()
    b.prompt = ""
    b.frame_start = scene.frame_start
    b.frame_end = min(scene.frame_end, scene.frame_start + int(round(4 * fps)) - 1)
    b.enabled = True
    settings.active_block_index = 0


def _preview_path_snap_update(self, context):
    """When the user flips the "Snap to Path" toggle on, immediately sync
    the current curve's control points into root-bone location keyframes.

    Without this the depsgraph handler only fires on the NEXT curve edit,
    which makes the checkbox feel dead when you first turn it on.
    Turning the toggle off is a no-op: keyframes stay where they are.
    """
    if not self.preview_path_snap:
        return
    if getattr(self, "is_generating", False) or getattr(self, "is_previewing", False):
        # Do not replace dense preview-bake root keys with sparse curve keys;
        # see ``path_follow._snap_skips_target_during_generation_preview``.
        return
    arm = self.target_armature
    if arm is None or arm.type != 'ARMATURE':
        return
    # Local import — properties.py would otherwise drag path_follow into
    # registration order and we'd hit a circular load when the addon
    # loads properties first.
    from . import path_follow
    curve = path_follow._find_root_path_curve(context.scene)
    if curve is not None:
        path_follow.sync_path_to_armature(arm, context.scene, curve)


# ---------------------------------------------------------------------------
# Target armature lifecycle
# ---------------------------------------------------------------------------

def _is_live_armature(obj) -> bool:
    """``True`` iff ``obj`` is an armature linked into the scene tree.

    Three failure modes to catch:

    1. **Dangling wrapper** — any RNA access raises ``ReferenceError``
       (``getattr`` does not catch it).
    2. **Not in ``bpy.data``** — the ID was fully removed but Blender did
       not auto-clear the ``PointerProperty``.
    3. **Orphan datablock** — the object is still in ``bpy.data`` but
       every collection has dropped it. A multi-object delete leaves the
       deleted rig in this state until Blender's garbage pass purges it;
       the single-object delete path either purges immediately or fires
       the RNA update callback, which is why this code never tripped
       before.
    """
    if obj is None:
        return False
    try:
        if obj.type != 'ARMATURE':
            return False
        name = obj.name
        if bpy.data.objects.get(name) is not obj:
            return False
        return bool(obj.users_collection)
    except (ReferenceError, AttributeError):
        return False


def _live_armature(obj):
    """Return ``obj`` if it is a live armature, else ``None``."""
    return obj if _is_live_armature(obj) else None


def _redraw_animatica_editors() -> None:
    """Tag every editor so the picker and timeline reflect the new state.

    Tagging only ``VIEW_3D`` / ``DOPESHEET_EDITOR`` is enough in theory, but
    the sidebar panel can sit in a region that does not redraw until a
    broader tag, and properties-editor mirrors can hold the stale name
    in their cached UI. Tag everything — it is cheap.
    """
    wm = bpy.context.window_manager
    if wm is None:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            area.tag_redraw()


_pending_reset_scene_names: set[str] = set()


def schedule_target_reset(scene_name: str) -> None:
    """Queue a one-shot timer to reset stale target state on ``scene_name``.

    Draw callbacks (panels, gpu overlays) must never mutate scene
    properties directly. When such a callback notices that
    ``target_armature`` is dangling but the depsgraph handler has not
    fired yet, it calls this to defer the cleanup to the next app tick.
    """
    if not scene_name or scene_name in _pending_reset_scene_names:
        return
    _pending_reset_scene_names.add(scene_name)

    def _flush():
        try:
            sc = bpy.data.scenes.get(scene_name)
            if sc is None:
                return None
            settings = getattr(sc, "animatica", None)
            if settings is None:
                return None
            if _scene_needs_target_reset(settings):
                reset_target_armature_state(settings)
        finally:
            _pending_reset_scene_names.discard(scene_name)
        return None

    bpy.app.timers.register(_flush, first_interval=0.0)


_in_target_reset = False


def reset_target_armature_state(settings) -> None:
    """Wipe every piece of scene state bound to the target armature.

    Called when the rig was deleted, when the user clears the picker, or when
    the file loads with no target set. Idempotent and re-entrancy-safe: the
    ``settings.target_armature = None`` assignment fires the update callback,
    which re-enters this helper; the ``_in_target_reset`` flag makes the
    nested call a no-op.
    """
    global _in_target_reset
    if _in_target_reset:
        return
    _in_target_reset = True
    try:
        if settings.target_armature is not None:
            settings.target_armature = None
        if settings.previous_target_armature is not None:
            settings.previous_target_armature = None
        settings.is_previewing = False
        settings.source_action_name = ""
        settings.prompt_blocks.clear()
        settings.active_block_index = 0
    finally:
        _in_target_reset = False
    _redraw_animatica_editors()


def mirror_autoposer_rig(settings) -> None:
    """Point the Autoposer at the armature Animatica generates for.

    One character, chosen once. The Autoposer's own ``ap_armature`` stays as
    the mirror the ported module reads, rather than being torn out of it.
    """
    scene = getattr(settings, "id_data", None)
    if scene is None or not hasattr(scene, "ap_armature"):
        return
    arm = _live_armature(settings.target_armature)
    name = arm.name if arm is not None else ""
    if scene.ap_armature != name:
        scene.ap_armature = name


def _target_armature_update(self, context):
    """Sync per-armature state when the picker changes.

    Three cases:
      * picker cleared (or rig deleted, since Blender auto-clears the
        pointer) → wipe scene state via ``reset_target_armature_state``;
      * picker switched to a different rig → persist the previous rig's
        prompt blocks, then load the new rig's;
      * idempotent re-assignment (same id) → no-op.
    """
    if _in_target_reset:
        return

    # The settings whose picker changed, not the active scene's: a rig set in
    # one scene (an import run for another scene, a script) cleared the
    # preview of whichever scene happened to be on screen.
    settings = self
    new_arm = _live_armature(settings.target_armature)
    old_arm = _live_armature(settings.previous_target_armature)

    # Whatever else happens below, the Autoposer follows the same character.
    mirror_autoposer_rig(settings)

    if new_arm is None:
        reset_target_armature_state(settings)
        return

    if new_arm is old_arm:
        return

    if old_arm is not None:
        try:
            save_blocks_to_armature(old_arm, settings)
        except ReferenceError:
            pass

    # Preview / Accept-Reject bookkeeping is tied to the rig that was baked.
    # A take left waiting on the old rig stays there (its session keeps the
    # user's action alive and knows how to restore it) and comes back for
    # review when that rig is picked again. Clearing it stranded the take:
    # no Reject, and the user's action lost on the next save.
    from . import preview_session
    waiting = preview_session.get(new_arm)
    if waiting is not None and not new_arm.get("animatica_batch_take"):
        settings.is_previewing = True
        settings.source_action_name = waiting.get("source", "")
    else:
        settings.is_previewing = False
        settings.source_action_name = ""

    load_blocks_from_armature(new_arm, settings)
    settings.previous_target_armature = new_arm
    _redraw_animatica_editors()


def _loop_update(self, context):
    """Loop turns In place on: a travelling cycle walks off the viewport, and
    one on the spot is what a game controller wants. Turned off again, Loop
    takes In place with it -- only if it was Loop that turned it on."""
    if self.loop:
        if not self.inplace:
            self.loop_set_inplace = True
            self.inplace = True
    elif self.loop_set_inplace:
        self.loop_set_inplace = False
        self.inplace = False


def _inplace_update(self, context):
    """Live toggle for In-place mode: the take's travel path is taken out of
    the preview, or put back (see inplace.py). Only the path the character
    moves along comes out; the body's own sway, surge and height stay.

    Non-destructive: the original keys are kept on the action, so flipping the
    toggle off restores the travel without re-generating. Accept keeps
    whichever the preview shows.

    Imports operators lazily to dodge the circular ``properties ->
    operators -> properties`` chain at module load.
    """
    if not self.inplace:
        self.loop_set_inplace = False       # the artist's choice now, not Loop's
    if self.batch_pending:
        # Takes waiting in the batch review: live on each of them.
        from . import batch  # noqa: PLC0415
        batch.apply_inplace(self)
        if self.target_armature is not None and self.target_armature.name in batch.pending(self):
            return
    arm = self.target_armature
    if arm is None or arm.type != 'ARMATURE':
        return
    from . import operators  # noqa: PLC0415 — lazy to avoid circular import
    # Before a generation this is a choice for the bake to act on: the artist's
    # own keys are never moved.
    operators._apply_inplace_constraint(arm, enabled=bool(self.inplace and self.is_previewing))

    # Tag the depsgraph so the viewport reflects the constraint change.
    arm.update_tag()
    # The trail and ghosts are baked from the evaluated rig, constraint and
    # all, and cached: without a rebake they went on drawing the travel the
    # constraint had just taken away.
    if self.is_previewing:
        from . import key_poses  # noqa: PLC0415
        key_poses.request_rebuild()


# ---------------------------------------------------------------------------
# Key-pose overlay update callbacks
# ---------------------------------------------------------------------------
#
# Split by what each setting invalidates: the toggle owns the baked geometry,
# the display mode changes what gets captured and needs a re-bake, and the
# rest only change how the plan is drawn and need nothing but a redraw.
# Baking from an update callback would be unsafe (it moves the playhead) —
# ``key_poses`` defers onto a timer.

def _key_poses_toggle_update(self, context):
    from . import key_poses  # noqa: PLC0415 — lazy to avoid circular import

    key_poses.on_toggle(self)


def _key_poses_rebake_update(self, context):
    from . import key_poses  # noqa: PLC0415 — lazy to avoid circular import

    key_poses.on_rebake_setting(self)


def _tightness_update(self, context):
    """Push one number onto every control's own tolerance.

    The poser reads a tolerance per control — metres of slack, and the IK
    weight. Seven identical fields reading 0.005 is not seven decisions; it is
    one, asked seven times. This is that one, and the per-control values stay
    underneath for anyone who wants them from the bone properties.
    """
    from . import properties  # noqa: PLC0415 — self, for _live_armature
    from .autoposer import poser  # noqa: PLC0415 — lazy to avoid circular import

    arm = properties._live_armature(self.target_armature)
    if arm is None:
        return
    for b in poser._controls(arm):
        b.ap_tol_m = float(self.pose_tightness)
        b.ap_rot_tol_m = float(self.pose_tightness)


def _key_poses_redraw_update(self, context):
    from . import key_poses  # noqa: PLC0415 — lazy to avoid circular import

    key_poses.on_redraw_setting(self)


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------


class PromptBlock(PropertyGroup):
    """One frame-range window with a text prompt, drawn on the timeline."""

    prompt: StringProperty(
        name="Prompt",
        description="Text prompt driving generation for this time window",
        default="",
    )
    frame_start: IntProperty(
        name="Start Frame",
        description="First frame of this range (inclusive)",
        default=1, min=1,
    )
    frame_end: IntProperty(
        name="End Frame",
        description="Last frame of this range (inclusive)",
        default=250, min=1,
    )
    enabled: BoolProperty(
        name="Enabled",
        description="Include this block when generating",
        default=True,
    )
    color: FloatVectorProperty(
        name="Color",
        description="Display color for this strip (0,0,0,0 = auto palette)",
        subtype="COLOR",
        size=4,
        min=0.0, max=1.0,
        default=(0.0, 0.0, 0.0, 0.0),
    )
    seed: IntProperty(
        name="Seed",
        description=(
            "Per-block seed override. 0 = inherit the global Seed, so setting "
            "the global Seed reproduces the whole clip. Set a positive value to "
            "give this block its own seed — when the connected model advertises "
            "per-segment seeds, full-timeline Generate honours it so this block "
            "re-rolls independently of the others"
        ),
        default=0, min=0, max=999999,
    )
    last_used_seed: IntProperty(
        name="Last Used Seed",
        description=(
            "The concrete seed this block was last generated with (0 = not "
            "generated yet). When Seed is 0, the addon rolls a real seed at "
            "generation time and records it here so the result is reproducible "
            "— right-click the strip and choose 'Reuse seed' to lock it in"
        ),
        default=0, min=0, max=999999,
    )


# Animatica Cloud's MMCP endpoint. Surfaced as a non-editable label in the
# prefs UI; users opt in to a self-hosted override via the `self_hosted`
# checkbox. Resolved at request time by mmcp_client.get_server_url().
CLOUD_API_URL = "https://api.animatica.ai"


def _draw_model_details(layout, caps) -> None:
    """What the connected server says it can do — one model per box.

    Kept out of the way rather than deleted: when a generation is refused for
    a reason that makes no sense, this is the page that explains it.
    """
    layout.label(text=f"MMCP {caps.get('protocol_version', '?')}"
                      f"  ·  {caps.get('coordinate_system', '?')}"
                      f"  ·  {caps.get('units', '?')}")
    for m in caps.get("models", []):
        box = layout.box()
        box.label(text=m.get("id", "?"), icon='OUTLINER_OB_ARMATURE')
        joints = len(m.get("canonical_skeleton", {}).get("joints", []))
        retarget = "yes" if m.get("supports_retargeting") else "no"
        col = box.column(align=True)
        col.active = False
        col.label(text=f"{joints} joints @ {m.get('fps', '?')} fps  ·  retargeting: {retarget}")
        col.label(text="segments: " + (", ".join(m.get("supported_segments") or []) or "—"))
        col.label(text="constraints: " + (", ".join(m.get("supported_constraints") or []) or "—"))
        limits = m.get("limits") or {}
        parts = []
        if m.get("recommended_max_duration_seconds") is not None:
            parts.append(f"recommended ≤ {m['recommended_max_duration_seconds']:g}s")
        if limits.get("max_duration_seconds") is not None:
            parts.append(f"max {limits['max_duration_seconds']:g}s")
        if parts:
            col.label(text="  ·  ".join(parts))


class AnimaticaAddonPreferences(AddonPreferences):
    """Addon-level preferences — edit in ``Edit > Preferences > Add-ons > Animatica``."""

    bl_idname = __package__  # "animatica_blender"

    self_hosted: BoolProperty(
        name="Self-hosted",
        default=False,
        description=(
            "Tick when running an MMCP server on your own machine or LAN "
            "(e.g. motionmcp-kimodo on localhost). Untick to use Animatica "
            "Cloud at api.animatica.ai (default; requires sign-in)."
        ),
    )
    server_url: StringProperty(
        name="Server URL",
        default="http://localhost:8000",
        description="Base URL of your self-hosted MMCP server",
    )

    # --- Updates ----------------------------------------------------------
    # A preview moves faster than anyone will reinstall by hand, so the addon
    # looks at its own releases page. Checking is automatic; installing is not.
    check_updates: BoolProperty(
        name="Check for updates",
        default=True,
        description=(
            "Ask GitHub once a day whether a newer build has been released. "
            "Nothing is downloaded or installed until you press Update"
        ),
    )
    update_previews: BoolProperty(
        name="Include previews",
        # On by default only for a preview build: someone on a release is
        # not asked to move to the next preview.
        default=_PREVIEW_BUILD,
        description="Offer pre-release builds as well as final ones",
    )

    # --- Animatica Cloud session (populated by /auth/login) ----------------
    # Auth is NOT part of the MMCP protocol — it lives at the cloud's proxy
    # in front of /generate. Self-hosted servers ignore the Authorization
    # header entirely.
    access_token: StringProperty(
        name="Access Token",
        default="",
        description="Animatica session token; valid for ~1 hour, then auto-refreshed",
        subtype='PASSWORD',
    )
    refresh_token: StringProperty(
        name="Refresh Token",
        default="",
        description="Long-lived refresh token used to renew the session",
        subtype='PASSWORD',
    )
    email: StringProperty(
        name="Email",
        default="",
        description="Email of the signed-in Animatica user",
    )
    tier: StringProperty(
        name="Tier",
        default="",
        description="Animatica plan tier (free / pro / team / admin)",
    )

    # The Autoposer's own settings — model source, token, cache, threads —
    # merged in below the class body. Blender allows one AddonPreferences per
    # addon, and the Autoposer is part of this one now; its property names and
    # defaults are unchanged, so a machine that already fetched the model
    # through the standalone addon keeps using what it downloaded.

    def draw(self, context):
        """Three questions, in the order anyone opening this page has them.

        Who am I, what am I talking to, and is the poser on this machine
        ready. Everything else — protocol details, model sources, caches,
        thread counts — is recovery equipment, and it is folded away: needed
        on the day something breaks, noise on every other day.
        """
        from . import mmcp_client, updater

        layout = self.layout

        # --- This build -------------------------------------------------------
        updater.check_async()
        updater.draw_preferences(layout, context)

        # --- Account ----------------------------------------------------------
        layout.separator()
        layout.label(text="Account", icon='USER')
        box = layout.box()
        if self.self_hosted:
            box.label(text="Self-hosted — no sign-in needed", icon='INFO')
        elif self.access_token:
            # Split rather than a plain row: an even share would give signing
            # out half the width, and it is not half the point of the row.
            row = box.split(factor=0.75)
            who = self.email or "signed in"
            row.label(text=who + (f"  ·  {self.tier}" if self.tier else ""), icon='CHECKMARK')
            row.operator("animatica.signout", icon='X', text="Sign out")
        else:
            expired = mmcp_client.session_expired()
            if expired:
                row = box.row()
                row.alert = True
                row.label(text=expired.capitalize(), icon='ERROR')
            else:
                box.label(text="Sign in to generate motion", icon='USER')
            box.operator("animatica.signin", icon='IMPORT', text="Sign in")

        # --- Server -----------------------------------------------------------
        layout.separator()
        layout.label(text="Server", icon='WORLD_DATA')
        box = layout.box()
        caps = mmcp_client.cached_capabilities()
        row = box.split(factor=0.75)
        if caps is None:
            mmcp_client.connect_async()
            err = mmcp_client.last_connection_error()
            if mmcp_client.connecting() or not err:
                row.label(text="Connecting…", icon='SORTTIME')
                row.label(text="")              # keep the split's second column filled
            else:
                row.alert = True
                row.label(text="Cannot reach the server", icon='ERROR')
                row.operator("animatica.connect", icon='FILE_REFRESH', text="Try again")
                for line in err.split("\n")[:2]:
                    sub = box.row()
                    sub.active = False
                    sub.label(text=line[:70])
        else:
            models = caps.get("models", [])
            row.label(text=f"Connected  ·  {len(models)} model"
                           + ("" if len(models) == 1 else "s"), icon='LINKED')
            row.operator("animatica.connect", icon='FILE_REFRESH', text="Reconnect")

        sub = box.row()
        sub.active = False
        sub.label(text=("your own server" if self.self_hosted
                        else f"Animatica Cloud · {CLOUD_API_URL}"))
        box.prop(self, "self_hosted")
        if self.self_hosted:
            box.prop(self, "server_url", text="URL")

        # What each model can do: true, occasionally needed, and nobody's first
        # question. Folded.
        if caps is not None and caps.get("models"):
            header, body = layout.panel("animatica_prefs_models", default_closed=True)
            header.label(text="Models and protocol")
            if body is not None:
                _draw_model_details(body, caps)

        # --- The poser --------------------------------------------------------
        layout.separator()
        layout.label(text="Poser", icon='ARMATURE_DATA')
        clash = autoposer.superseded_addons()
        if clash:
            warn = layout.row()
            warn.alert = True
            warn.label(text=f"disable the standalone {', '.join(clash)} addon",
                       icon='ERROR')
        autoposer_prefs.draw(layout, self, context)


# Merged after the class body: annotations are read at registration, so adding
# them here gives the Autoposer's fields to Animatica's preferences without
# restating them in two places.
for _name, _prop in autoposer_prefs.PROPERTIES.items():
    AnimaticaAddonPreferences.__annotations__[_name] = _prop


def _model_id_items(self, context):
    """Dynamic EnumProperty items, populated by the Connect operator."""
    from . import mmcp_client
    return mmcp_client.cached_model_items()


class AnimaticaSettings(PropertyGroup):
    """Scene-level addon state."""

    # -- MMCP server connection --
    model_id: EnumProperty(
        name="Model",
        description="Motion-generation model exposed by the connected MMCP server",
        items=_model_id_items,
    )

    # -- Target armature --
    batch_direction: EnumProperty(
        name="Direction",
        items=[
            ('OWN', "Each Their Own",
             "Every character uses its own prompts, key poses, waypoints and pins"),
            ('SHARED', "Shared",
             "Every character uses the active character's prompts, each with its "
             "own seed, starting from where it stands: variations on one action, "
             "a crowd. Waypoints and pins are left out"),
        ],
        default='OWN',
    )
    # The characters whose batch take waits for Accept All / Reject All (JSON).
    batch_pending: StringProperty(default="", options={'HIDDEN'})
    # "name: reason" for the characters a batch could not make (JSON).
    batch_failed: StringProperty(default="", options={'HIDDEN'})
    batch_total: IntProperty(default=0, options={'HIDDEN', 'SKIP_SAVE'})
    batch_done: IntProperty(default=0, options={'HIDDEN', 'SKIP_SAVE'})
    follow_active: BoolProperty(
        name="Follow Selection",
        description=(
            "Animate whichever character you select: making an armature, or a "
            "mesh skinned to one, the active object makes it Animatica's "
            "character. Not while a take is being made or reviewed"
        ),
        default=True,
    )
    target_armature: PointerProperty(
        name="Target Armature",
        type=bpy.types.Object,
        description="Armature with keyframed animation to generate from",
        poll=lambda self, obj: obj.type == 'ARMATURE',
        update=_target_armature_update,
    )

    previous_target_armature: PointerProperty(
        name="Previous Target Armature",
        type=bpy.types.Object,
        options={"HIDDEN", "SKIP_SAVE"},
    )

    # -- Prompt blocks (one time-window with one prompt each) --
    prompt_blocks: CollectionProperty(
        type=PromptBlock,
        name="Prompt Blocks",
        description="Per-window prompts drawn as strips on the timeline",
    )
    active_block_index: IntProperty(
        name="Active Block",
        default=0, min=0,
    )

    # -- Generation settings --
    seed: IntProperty(
        name="Seed",
        description=(
            "Clip seed used for every block that doesn't pin its own. 0 (the "
            "default) makes every run a new take, and the seed it used is shown "
            "so you can lock a take you like. The same seed with the same inputs "
            "reproduces the same motion"
        ),
        default=0, min=0, max=999999,
    )
    last_used_seed: IntProperty(
        name="Last Used Seed",
        description=(
            "The concrete clip seed the last generation actually ran with "
            "(0 = none yet). Set when Seed is 0 and a fresh seed was rolled; "
            "click the lock to copy it into Seed and reproduce the run"
        ),
        default=0, min=0, max=999999,
    )

    quality_preset: EnumProperty(
        name="Quality",
        items=[
            ("STANDARD", "Best", "The finest motion (50 denoising steps)"),
            ("HALF", "Faster", "About twice as fast, a little rougher (25 steps)"),
            ("QUARTER", "Draft", "Quick drafts for blocking out (12 steps)"),
            ("CUSTOM", "Custom", "Custom step count"),
        ],
        default="STANDARD",
    )
    custom_steps: IntProperty(
        name="Steps", default=50, min=1, max=200,
    )

    cfg_enabled: BoolProperty(
        name="Guidance",
        description=(
            "Enable classifier-free guidance. When on, generation is pushed "
            "to follow your prompt and constraints more closely; turn it off "
            "for the model's looser, unguided output"
        ),
        default=True,
    )
    cfg_text: FloatProperty(
        name="Text Weight",
        description=(
            "How strongly the motion follows the text prompt. Higher is more "
            "literal to the words but can look stiff; lower is more natural "
            "but looser. Typical range 1–3"
        ),
        default=2.0, min=0.0, max=5.0, step=10,
    )
    cfg_constraint: FloatProperty(
        name="Constraint Weight",
        description=(
            "How strongly the motion honors your constraints — root paths, "
            "effector pins, and pose keyframes. Higher sticks tighter to the "
            "poses and paths you authored. Typical range 1–3"
        ),
        default=2.0, min=0.0, max=5.0, step=10,
    )

    hand_pose_left: EnumProperty(
        name="Left hand",
        description=(
            "The left hand's fingers, laid over every generation — the model "
            "has none of its own and leaves them dead straight"
        ),
        items=_hand_pose_items,
        default='RELAXED',
        update=lambda self, context: _on_hand_pose(self, context),
    )
    hand_pose_right: EnumProperty(
        name="Right hand",
        description=(
            "The right hand's fingers, laid over every generation — the model "
            "has none of its own and leaves them dead straight"
        ),
        items=_hand_pose_items,
        default='RELAXED',
        update=lambda self, context: _on_hand_pose(self, context),
    )

    post_processing: BoolProperty(
        name="Motion Cleanup",
        description=(
            "Clean the motion up after it is generated: feet stop sliding, "
            "and key poses and pins are hit more exactly. Adds a second or two"
        ),
        default=True,
    )
    inplace: BoolProperty(
        name="In place",
        description=(
            "Keep the character on the spot: the path it travels along is "
            "taken out (a straight line, an arc, or one of those eased), and "
            "everything else stays: the body's sway and bounce, jumps and "
            "crouches. Set it before generating, or switch it on a preview "
            "without regenerating -- switching back restores the travel. For "
            "game cycles, where a controller moves the character"
        ),
        default=False,
        update=_inplace_update,
    )
    loop: BoolProperty(
        name="Loop",
        description=(
            "Generate the block as a seamless cycle: the model samples it so "
            "its last frame runs straight into its first, and it repeats past "
            "its end. Turns In place on, so the cycle plays on the spot. "
            "Needs a single prompt block; walk and run cycles work best at "
            "two to four seconds. Offered only when the model supports it"
        ),
        default=False,
        update=_loop_update,
    )
    variations: IntProperty(
        name="Variations",
        description=(
            "How many versions of the take to make at once: the same prompt and "
            "poses, performed differently. Flip between them on the preview and "
            "Accept the one you want. One generation, whatever the number"
        ),
        default=1, min=1, max=8,
    )
    loop_set_inplace: BoolProperty(
        description="Loop turned In place on, and turns it off again with itself",
        default=False,
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    preview_path_snap: BoolProperty(
        name="Snap to Path",
        description=(
            "Bake each root_path curve control point into one keyframe on "
            "the target armature's root bone. Turning this on syncs "
            "immediately from the current curve; leaving it on keeps the "
            "keyframes in sync as you edit the curve in the viewport. "
            "While a generation is running or a preview bake is active, "
            "sync is paused so the returned root trajectory is not replaced "
            "by sparse path keys (which looks like snapping and foot sliding)"
        ),
        default=True,
        update=_preview_path_snap_update,
    )
    num_transition_frames: IntProperty(
        name="Transition Frames",
        description=(
            "Frames blended between adjacent prompt blocks so segments flow "
            "smoothly into one another instead of snapping at the boundary. "
            "0 = hard cut between blocks"
        ),
        default=5, min=0, max=30,
    )

    # -- Key-pose overlay --
    #
    # Each pose the artist keys becomes one full-body ``pose_keyframe``
    # constraint in the request. These settings control the viewport view of
    # that plan — which poses exist, where, and which the request will carry.
    # See ``key_poses.py``.
    # One switch for the whole overlay, so it can go away in a click instead
    # of three. It sits with the toggles it governs — the same switch in the
    # Pose panel's *header* read as switching posing off, which it never did.
    key_pose_overlay: BoolProperty(
        name="Show Plan",
        description=(
            "Draw the motion plan in the viewport at all. Off hides the "
            "ghosts, the trail and the frame numbers in one go, and remembers "
            "which of them were on for when you switch it back"
        ),
        default=True,
        update=_key_poses_toggle_update,
    )
    key_pose_ghosts: BoolProperty(
        name="Ghosts",
        description=(
            "Draw the body at each pose you keyed. Independent of the motion "
            "trail — either can be shown on its own"
        ),
        default=True,
        update=_key_poses_toggle_update,
    )
    key_pose_display: EnumProperty(
        name="Show As",
        description="What each key pose is drawn as",
        items=[
            ("AUTO", "Auto", "Skinned mesh if the rig has one, bones otherwise"),
            ("MESH", "Mesh", "Meshes deformed by the rig, as a translucent body"),
            ("BONES", "Bones", "The skeleton as sticks — clearer on a dense character"),
        ],
        default="AUTO",
        update=_key_poses_rebake_update,
    )
    key_pose_labels: BoolProperty(
        name="Frame Numbers",
        description="Label each key pose with the frame it sits on",
        default=True,
        update=_key_poses_redraw_update,
    )
    key_pose_trail: BoolProperty(
        name="Motion Trail",
        description=(
            "Trace the path the motion actually takes, frame by frame, "
            "coloured by the prompt block driving each stretch and marked at "
            "every pose you keyed. Follows the joints the model is steered "
            "by: the hands and feet, the root, and the head"
        ),
        default=True,
        update=_key_poses_toggle_update,
    )
    # Which joints the motion trail shows: one toggle each, so a click turns
    # that one on or off and leaves the others be.
    key_pose_trail_hips: BoolProperty(
        name="Hips", description="Trail the hips: where the body goes, sway and all",
        default=True, update=_key_poses_redraw_update,
    )
    key_pose_trail_head: BoolProperty(
        name="Head", description="Trail the head", default=True, update=_key_poses_redraw_update,
    )
    key_pose_trail_hands: BoolProperty(
        name="Hands", description="Trail both hands", default=True, update=_key_poses_redraw_update,
    )
    key_pose_trail_feet: BoolProperty(
        name="Feet", description="Trail both feet", default=True, update=_key_poses_redraw_update,
    )
    key_pose_root_path: BoolProperty(
        name="Root Trajectory",
        description=(
            "Draw the take's root trajectory on the floor, in amber: the path "
            "the character travels along without the sway of its steps -- a "
            "line, an arc, or one of those eased. It is what In place takes "
            "out, so with In place on it shows what was removed. Coloured by "
            "speed, green (slow) to red (fast), and labelled with what it is "
            "(\"line · 1.05 m/s\"). Edit it with Edit Root Trajectory"
        ),
        default=False,
        update=_key_poses_toggle_update,
    )
    key_pose_xray: BoolProperty(
        name="X-Ray",
        description="Draw the key poses through the character instead of behind it",
        default=False,
        update=_key_poses_redraw_update,
    )
    editing_key_pose_frame: IntProperty(
        name="Editing Key Pose",
        description=(
            "Frame of the key pose currently being edited, or -1. Set by "
            "clicking a ghost; cleared by Apply or Cancel"
        ),
        default=-1,
        options={"SKIP_SAVE"},
    )
    pose_tightness: FloatProperty(
        name="Slack",
        description=(
            "How far a joint may stray from its handle, in metres. Low puts "
            "the joint where you put the handle; high makes it a hint the "
            "poser may overrule to keep the body natural"
        ),
        default=0.005, min=0.001, max=0.2, precision=3, step=1,
        update=_tightness_update,
    )
    auto_key_pose: BoolProperty(
        name="Auto Key",
        description=(
            "Write a keyframe whenever you pose with the Autoposer handles. "
            "Off: posing still works and still shows, but nothing is recorded "
            "until you press Set Keyframe — the way to try a pose out without "
            "it landing in the action"
        ),
        default=True,
    )
    waypoint_heading: BoolProperty(
        name="Face along the path",
        description=(
            "Also tell the model which way to face at each waypoint — toward "
            "the next one. Off by default: pinning a facing at every waypoint "
            "over-constrains turns, and the model faces the way it walks"
        ),
        default=False,
    )
    pose_details: BoolProperty(
        name="Per-Handle Settings",
        description=(
            "Show each handle's own tightness and whether it sends its "
            "rotation, instead of the compact row of on/off toggles"
        ),
        default=False,
    )
    key_pose_auto_refresh: BoolProperty(
        name="Auto Refresh",
        description=(
            "Re-bake the ghosts when you key a pose or move the rig. Turn off "
            "on a heavy character and refresh by hand instead"
        ),
        default=True,
    )

    default_prompt: StringProperty(
        name="Prompt",
        default="a person moves naturally",
        description="Text prompt for motion generation",
    )

    last_pose_prompt: StringProperty(
        name="Last pose prompt",
        default="",
        description=(
            "Most-recent prompt used in the Generate Pose dialog. "
            "Pre-fills the dialog the next time it opens so the user "
            "can iterate on a phrasing without retyping"
        ),
    )

    # -- Runtime state (not saved) --
    is_generating: BoolProperty(name="Generating", default=False, options={"SKIP_SAVE"})
    generation_progress: FloatProperty(
        name="Progress", default=0.0, min=0.0, max=1.0, subtype='FACTOR',
    )
    # Seconds elapsed since the in-flight generation started. There is no
    # server-side progress signal in MMCP v1, so the panel shows this as a
    # live "Working… Ns" counter instead of a progress bar that would sit
    # frozen at 0%. Updated from the generate / pose / regenerate modals.
    generation_elapsed: IntProperty(
        name="Elapsed Seconds",
        default=0,
        options={"SKIP_SAVE"},
    )
    cancel_requested: BoolProperty(
        name="Cancel Requested",
        default=False,
        description="Flipped by the Cancel button; the running modal op picks it up and exits",
        options={"SKIP_SAVE"},
    )

    # -- Quota / upgrade state. Set when the cloud returns 429
    #    quota_exceeded; cleared on the next successful generation or
    #    when the user dismisses the banner. ``upgrade_url`` is sent in
    #    the error envelope so the plugin doesn't hardcode a billing URL.
    quota_exceeded_message: StringProperty(
        name="Quota Message",
        default="",
        description="Human-readable quota error message from the cloud",
    )
    quota_upgrade_url: StringProperty(
        name="Upgrade URL",
        default="",
        description="URL to open in the user's browser to upgrade the plan",
    )

    # -- Preview state: name of the user's source action while a
    #    Animatica_Motion (or legacy Animatica_Generated) action is
    #    being previewed.  Empty when free-form generation runs (no
    #    source to fall back to) — the ``is_previewing`` flag tracks
    #    preview state independently for that case.
    source_action_name: StringProperty(
        name="Source Action",
        default="",
        description="Original action name preserved while previewing a generated motion",
    )
    is_previewing: BoolProperty(
        name="Previewing",
        default=False,
        description=(
            "Set true while a Animatica-generated motion is being "
            "reviewed (Push to NLA / Reject visible). Independent of "
            "source_action_name so free-form generations — where "
            "there's no prior action to restore — also surface the "
            "preview UI"
        ),
    )


# ---------------------------------------------------------------------------
# Depsgraph — catch armature deletions that bypass the RNA update callback
# ---------------------------------------------------------------------------

def _purge_stale_depsgraph_handlers(handler_list, fn_name: str) -> None:
    for h in list(handler_list):
        if getattr(h, "__name__", None) == fn_name:
            handler_list.remove(h)


def _scene_needs_target_reset(s) -> bool:
    """True if scene state is inconsistent with a live target armature.

    Triggers cleanup when:
      * ``target_armature`` is a dangling wrapper (single-object delete on
        rigs that did not survive Blender's auto-remap);
      * ``target_armature`` is ``None`` but per-target state lingers — the
        case that breaks multi-object delete, where Blender silently
        clears the pointer to ``None`` without firing the RNA update
        callback, leaving ``previous_target_armature`` / ``prompt_blocks``
        / preview flags untouched.
    """
    try:
        target = s.target_armature
    except ReferenceError:
        return True
    if target is not None:
        return not _is_live_armature(target)

    try:
        if s.previous_target_armature is not None:
            return True
    except ReferenceError:
        return True
    if len(s.prompt_blocks) > 0:
        return True
    if s.is_previewing or s.source_action_name:
        return True
    return False


def armature_of(obj):
    """The character ``obj`` belongs to: an armature itself, or the rig a mesh
    is skinned to or parented under -- clicking a character means clicking its
    body far more often than its bones."""
    if obj is None:
        return None
    try:
        if obj.type == 'ARMATURE':
            return obj
        for mod in getattr(obj, "modifiers", ()):
            if mod.type == 'ARMATURE' and mod.object is not None and mod.object.type == 'ARMATURE':
                return mod.object
        parent = obj.parent
        if parent is not None and parent.type == 'ARMATURE':
            return parent
    except ReferenceError:
        return None
    return None


# scene name -> the active object last seen, so following reacts to the active
# object *changing*, not to it merely differing: a character picked in the
# Armature field while another object is active must not be switched back.
_last_active: dict[str, str] = {}


def _follow_active_character(scene, depsgraph) -> None:
    """Make the focused character the one Animatica animates (Follow Selection)."""
    s = getattr(scene, "animatica", None)
    if s is None:
        return
    try:
        view_layer = scene.view_layers.get(depsgraph.view_layer.name)
        obj = view_layer.objects.active if view_layer else None
        name = obj.name if obj is not None else ""
    except (AttributeError, ReferenceError):
        return
    if _last_active.get(scene.name) == name:
        return
    _last_active[scene.name] = name
    # A take being reviewed or made belongs to the current character: switching
    # would lose its Accept / Reject (the Preview box says so instead).
    if not s.follow_active or s.is_generating or s.is_previewing:
        return
    arm = armature_of(obj)
    if arm is None or not _is_live_armature(arm):
        return
    try:
        current = s.target_armature
    except ReferenceError:
        current = None
    if arm != current:
        s.target_armature = arm


@persistent
def _validate_target_armature_on_depsgraph(_scene, _depsgraph) -> None:
    """Drop dangling ``target_armature`` pointers and any per-target state
    that survives them, and follow the focused character. Runs on every
    depsgraph tick; the reset is idempotent and following compares one name,
    so the cost when nothing changed is a few attribute reads per scene."""
    for scene in bpy.data.scenes:
        s = getattr(scene, "animatica", None)
        if s is None:
            continue
        if _scene_needs_target_reset(s):
            reset_target_armature_state(s)
    if _scene is not None:
        _follow_active_character(_scene, _depsgraph)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    AnimaticaAddonPreferences,
    PromptBlock,
    AnimaticaSettings,
)

_DEPSGRAPH_HANDLER_NAME = "_validate_target_armature_on_depsgraph"


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.animatica = PointerProperty(type=AnimaticaSettings)
    _purge_stale_depsgraph_handlers(
        bpy.app.handlers.depsgraph_update_post, _DEPSGRAPH_HANDLER_NAME,
    )
    bpy.app.handlers.depsgraph_update_post.append(
        _validate_target_armature_on_depsgraph,
    )


def unregister():
    _purge_stale_depsgraph_handlers(
        bpy.app.handlers.depsgraph_update_post, _DEPSGRAPH_HANDLER_NAME,
    )
    del bpy.types.Scene.animatica
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
