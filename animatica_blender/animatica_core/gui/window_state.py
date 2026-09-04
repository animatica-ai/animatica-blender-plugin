"""AppState plumbing every host window repeats — persistence, auth, log buffer.

A tool window in any host owns one :class:`~animatica_core.core.prompt_model.AppState`
and has to do the same three things with it on every session: seed it from the
settings file on construction, write the persisted subset back when the user
changes something, and keep ``state.logs`` from growing without bound.

None of that is host-specific and none of it is Qt, so it lives here as plain
functions over an ``AppState``. The window keeps the parts that genuinely are
its own — *when* to save (its debounce timer) and *where* to echo a log line
(its console section).

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b; the field
list below is that window's ``_PERSISTED_FIELDS`` verbatim, so a host adopting
it inherits the same settings file layout.
"""

from __future__ import annotations

from datetime import datetime

from animatica_core import settings as user_settings
from animatica_core.core.prompt_model import LogEntry


# How many log entries ``state.logs`` keeps. The Console lives in its own
# window and replays this list when it opens, so the list is the buffer for
# everything logged while it was closed — bounded here so a long session
# can't grow it without limit.
LOG_BUFFER_MAX = 2000

# Fields persisted to the host's settings.json. Password is never stored
# here — that's the auth.json file's job.
PERSISTED_FIELDS = (
    "backend", "server_url", "auth_email", "story_path",
    "story_passthrough", "story_overwrite_fbx",
    "fps", "namespace",
    "animation_mode", "take_name", "auto_take_name", "take_name_length",
    "steps", "steps_preset", "model", "random_seed", "seed", "advanced_on",
    "num_samples", "cfg_type", "cfg_text_weight", "cfg_constraint_weight",
    "transition_frames", "heading_deg", "post_processing", "root_margin",
    # Skeleton + constraint toggles (previously session-only; now remembered).
    "constraint_type", "keep_keyframes", "show_path_length", "show_marker_label",
    "auto_create_skeleton",
    # Same-frame reorder gate (Extra options; absent key -> dataclass default,
    # currently OFF -- the fabricated root wins the same-frame dedup).
    "reorder_same_frame_waypoints",
    # Seam waypoint duplication gate (Extra options; absent key -> dataclass
    # default, currently ON).
    "duplicate_seam_waypoints",
    "use_local_skeleton", "skip_root_joint",
    "compensate_group_scale", "group_scale", "match_scene_fps",
    # Output target toggles.
    "use_hip_pos", "preserve_height", "ground_offset_enabled",
    "ground_correction_enabled",
    # Capture grounding (default ON, unlike the generation one above).
    "capture_ground_correction",
    "base_layer_only", "bake_to_control_rig",
    "bake_whole_range",
    "auto_constraint", "key_pose", "sync_transport",
    # Generate-Selected dialog (per-block regen) boundary-pin defaults.
    "pin_start_pose", "pin_end_pose",
    # Single-pose prompt text + placement split (XZ+facing / height).
    "pose_prompt", "pose_use_xz", "pose_keep_height",
    # First-run onboarding marker (fires the Settings auto-open exactly once).
    "first_run_done",
    # Panel-visibility toggle (Motion Import hidden by default).
    "show_motion_import",
    "show_live_drive",
    # Last folder used by the Save/Load Prompts dialogs (example dir on first use).
    "last_prompt_dir",
    # Auto-open the tool window at host startup (opt-in, default off).
    "auto_open_on_startup",
    # Last native transport zoom window (absolute frames) — restored on load
    # and re-applied after a new-take generation.
    "zoom_window",
    # Debug lever: send effector `rotations` on the wire (inert on the hosted
    # server; a local kimodo_server consumes it). Persisted so a local-server
    # A/B survives a host restart; the other debug_* toggles stay session-only.
    "debug_send_effector_rotations",
)


def touches_persisted(patch: dict) -> bool:
    """True when *patch* writes at least one persisted field.

    The window uses this to decide whether a section patch should arm its
    debounced save, so a purely transient change (a status pill, a busy flag)
    costs no disk write.
    """
    return any(k in PERSISTED_FIELDS for k in patch)


def load_persisted_into(state) -> None:
    """Seed *state* from the settings file. Soft-fails whole and per field.

    A settings file written by a newer build may carry a value this build's
    dataclass rejects; one bad key must not cost the user every other
    remembered setting, so each assignment is guarded on its own.
    """
    try:
        data = user_settings.load()
    except Exception:
        return
    for key in PERSISTED_FIELDS:
        if key in data and hasattr(state, key):
            try:
                setattr(state, key, data[key])
            except Exception:
                pass


def persist_state(state) -> str | None:
    """Write *state*'s persisted fields back, preserving unknown keys.

    Returns ``None`` on success or the failure message, so the caller decides
    how to surface it (the window logs a warning). The read-modify-write keeps
    keys this build does not know about — another host, or a newer version,
    sharing the same file.
    """
    try:
        existing = user_settings.load()
    except Exception:
        existing = {}
    for key in PERSISTED_FIELDS:
        if hasattr(state, key):
            existing[key] = getattr(state, key)
    try:
        user_settings.save(existing)
    except Exception as exc:                      # noqa: BLE001
        return str(exc)
    return None


def hydrate_auth_state(state) -> None:
    """Mirror the stored Animatica credentials onto *state*'s auth fields."""
    from animatica_core import animatica_auth
    auth = animatica_auth.get_auth()
    state.auth_logged_in = auth.is_authenticated()
    if auth.user_email:
        state.auth_email = auth.user_email
    if auth.tier:
        state.auth_tier = auth.tier


def jwt_sub(token: str | None) -> str | None:
    """Extract the ``sub`` claim from a JWT access token, or return None.

    Deliberately does not verify the signature: the claim is used to key
    per-account local caches, never to authorise anything.
    """
    if not token:
        return None
    try:
        import base64 as _b64, json as _j
        p = token.split('.')[1]
        p += '=' * (-len(p) % 4)
        return _j.loads(_b64.urlsafe_b64decode(p)).get('sub')
    except Exception:
        return None


def append_log(state, level: str, msg: str) -> LogEntry:
    """Append one entry to ``state.logs``, capped at :data:`LOG_BUFFER_MAX`.

    Returns the entry so the caller can also push it at a live console widget.
    ``state.logs`` is the console's buffer as much as its model — it is what a
    console window replays when it is first opened — so the cap lives here
    rather than in whichever widget happens to be showing.
    """
    entry = LogEntry(t=datetime.now().strftime("%H:%M"), level=level, msg=msg)
    state.logs.append(entry)
    if len(state.logs) > LOG_BUFFER_MAX:
        del state.logs[:-LOG_BUFFER_MAX]
    return entry
