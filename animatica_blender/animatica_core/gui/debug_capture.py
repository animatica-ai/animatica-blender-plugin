"""The debug-capture folder a generate run leaves behind, and the crash breadcrumb.

When "Debug capture" is on, every generation writes the exact request it sent
plus the settings that shaped it into a timestamped folder, and arms a
fixed-path breadcrumb that survives the host dying mid-run. Both exist to answer
one question after the fact — *what did we actually send, and how far did it
get* — which is why the metadata is deliberately explicit about things a reader
would otherwise have to infer.

``skeleton_block_source`` is the sharpest example. It records the branch the
caller's skeleton selection actually took, passed in rather than re-derived
here: the version that guessed it from "is there a rig in the scene" mislabelled
every canonical-block request made from a scene that merely *has* one, and every
capture taken to diagnose that was misleading.

None of this touches a scene, so it is shared. The window keeps only the
decision of where to show the messages.

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

import os


#: Where captures go when the user has not chosen a folder.
def default_debug_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents", "MB",
                        "Animatica_Debug")


def open_session(state, tag: str):
    """Open a fresh debug-capture folder for a generate run.

    Returns ``(session_dir_or_None, (level, message)_or_None)``. ``None`` for
    the directory means capture is off (no message) or the folder could not be
    created (a warning the caller should show — a silent failure here is how a
    user ends up with no evidence after the run they enabled capture for).
    """
    from animatica_core.core import debug_io
    if not state.debug_capture:
        return None, None
    debug_dir = state.debug_dir or default_debug_dir()
    session_dir = debug_io.open_session(debug_dir=debug_dir, tag=tag)
    if session_dir:
        return session_dir, ("info", f"Debug capture: {session_dir}")
    return None, ("warn",
                  "Debug capture enabled but the debug folder is unset or "
                  "couldn't be created — skipping capture for this run.")


def write_request_files(session_dir: str, *, state, token: str | None,
                        url: str, mode: str, model_id: str,
                        supports_retargeting: bool, request: dict,
                        skeleton_source: str, user_agent: str,
                        extra_meta: dict | None = None) -> None:
    """Write ``request.json`` + ``meta.json`` into *session_dir*.

    *skeleton_source* is the branch the caller's skeleton selection actually
    took (``"canonical"`` / ``"wire"``), NOT a fact re-derived here. Keyword-only
    on purpose: the parameter it replaced was a positional host model object, so
    a call site missed in a rename fails loudly instead of stringifying a scene
    node into meta.json.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    from animatica_core import animatica_auth as _auth_mod
    from animatica_core.core import debug_io
    from .window_state import jwt_sub

    auth = _auth_mod.get_auth()
    debug_io.write_json(session_dir, "request.json", {
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "user_id":        jwt_sub(token),
        "user_email":     auth.user_email,
        "user_agent":     user_agent,
        "generation_ids": [str(_uuid.uuid4())],
        "body":           request,
    })
    meta = {
        "url":                   url,
        "mode":                  mode,
        "model_id":              model_id,
        "supports_retargeting":  supports_retargeting,
        "skeleton_block_source": skeleton_source,
        "skip_root_joint":       state.skip_root_joint,
        "compensate_group_scale": state.compensate_group_scale,
        "group_scale":           state.group_scale,
        "namespace":             state.namespace,
        # Without this a gate-off capture is indistinguishable from one whose
        # pins simply carried no context -- both show a bare effector wire.
        "send_effector_root_context": state.send_effector_root_context,
    }
    if extra_meta:
        meta.update(extra_meta)
    debug_io.write_json(session_dir, "meta.json", meta)


def arm_crash_breadcrumb(*, enabled: bool, rescue: bool = True) -> None:
    """Arm the fixed-path crash breadcrumb for this run, gated by Debug Capture.

    Localizes an intermittent host hard-crash (the process simply vanishing) to
    a phase/frame in ``settings_dir()/last_run.json``; a lingering
    ``in_progress`` status after a restart marks the run that crashed. The OS
    crash dump captures the native faulting module independently.

    *rescue* is forwarded to :func:`debug_io.arm_breadcrumb`; pass
    ``rescue=False`` for an intra-run re-arm (such as a fallback relaunch) so
    the still-live ``in_progress`` isn't mistaken for a crash.
    """
    if not enabled:
        return
    from animatica_core.core import debug_io
    from animatica_core.settings import settings_dir
    debug_io.arm_breadcrumb(os.path.join(settings_dir(), "last_run.json"),
                            rescue=rescue)
