# SPDX-License-Identifier: GPL-3.0-or-later
"""
Animatica for Blender — AI Motion Generation Addon
====================================================

Select an armature with a few keyframes, click Generate, and the server
fills in the motion using a backend MMCP-compatible motion model.

The addon is ML-free: all generation, retargeting, and keyframe
optimisation runs on the backend server.
"""

bl_info = {
    "name": "Animatica — AI Motion Generation",
    "author": "Animatica",
    "version": (0, 4, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Animatica",
    "description": "AI motion generation — select armature, set keyframes, generate",
    "category": "Animation",
}

import os
import sys

_ADDON_DIR = os.path.dirname(os.path.abspath(__file__))


def _ensure_on_sys_path() -> None:
    """Put the addon directory on ``sys.path`` so the core vendored beside this
    package is importable as top-level ``animatica_core`` — the name its own
    absolute imports use.

    Runs at module scope, not from ``register()``: the submodules imported
    below import core at module level, so by the time ``register()`` ran it
    would already be too late. ``register()`` calls it again, because
    ``unregister()`` takes the entry back out.
    """
    if _ADDON_DIR not in sys.path:
        sys.path.insert(0, _ADDON_DIR)


_ensure_on_sys_path()

import bpy
from bpy.app.handlers import persistent

from . import properties
from . import migrate
from . import operators
from . import canonical_skeleton
from . import constraints_ui
from . import panels
from . import path_follow
from . import timeline_overlay
from . import timeline_operators


# ---------------------------------------------------------------------------
# Persistent handlers — save/load prompt blocks onto the target armature
# ---------------------------------------------------------------------------

@persistent
def _animatica_save_pre(dummy):
    """Before saving the .blend, persist the current prompt_blocks onto the
    target armature's custom properties so they survive file reloads and
    per-armature switching."""
    for scene in bpy.data.scenes:
        settings = getattr(scene, "animatica", None)
        if settings is None:
            continue
        arm = settings.target_armature
        if arm is None:
            continue
        try:
            properties.save_blocks_to_armature(arm, settings)
        except Exception:
            pass


@persistent
def _animatica_load_post(dummy):
    """After loading a .blend, hydrate settings.prompt_blocks from the
    target armature's custom properties (or wipe state if the saved target
    no longer points at a live armature)."""
    # Files saved before the Proscenium -> Animatica rename store everything
    # under the old keys. Migrate first: the hydration below reads the new
    # ones, so it would find nothing on an unmigrated file.
    migrate.run()

    for scene in bpy.data.scenes:
        settings = getattr(scene, "animatica", None)
        if settings is None:
            continue
        arm = properties._live_armature(settings.target_armature)
        if arm is None:
            properties.reset_target_armature_state(settings)
            continue
        try:
            properties.load_blocks_from_armature(arm, settings)
            settings.previous_target_armature = arm
        except Exception:
            pass


def _reset_runtime_flags() -> None:
    """Clear the ``is_generating`` / ``cancel_requested`` flags on every scene.

    Reloading the addon kills any in-flight modal operator without giving it
    a chance to run ``_cleanup``, which leaves the flags stuck at True and
    the UI showing a "Generating…" state that can never clear. Reset them on
    every register() so reloading is a clean slate.

    ``bpy.data`` can be a restricted ``_RestrictData`` proxy (no ``scenes``
    attribute) when register() runs during startup or through the script
    context of the MCP bridge. In that case, defer the reset until the next
    app tick via a one-shot timer.
    """
    try:
        scenes = bpy.data.scenes
    except AttributeError:
        bpy.app.timers.register(_reset_runtime_flags, first_interval=0.0)
        return
    for scene in scenes:
        s = getattr(scene, "animatica", None)
        if s is None:
            continue
        try:
            s.is_generating = False
            s.cancel_requested = False
            if hasattr(s, "generation_progress"):
                s.generation_progress = 0.0
        except (AttributeError, ReferenceError):
            pass


def _purge_stale_handlers(handler_list, fn_name: str) -> None:
    """Drop every previously-registered copy of ``fn_name`` before a
    fresh append. Addon reload creates a new function object each time,
    so the usual ``if fn not in handler_list`` guard never matches and
    stale copies accumulate — each still firing with whatever behavior
    it had at the time of registration. Match by name, not identity."""
    for h in list(handler_list):
        if getattr(h, "__name__", None) == fn_name:
            handler_list.remove(h)


def _core_version(root: str) -> str:
    """The SDK commit the core under *root* was vendored from, if it says."""
    try:
        with open(os.path.join(root, "CORE-VERSION")) as fh:
            return fh.read().strip() or "(empty)"
    except OSError:
        return "(no CORE-VERSION)"


def _warn_on_foreign_core() -> None:
    """Warn if some other addon already owns the ``animatica_core`` name.

    Two addons vendoring core into one Blender share a single top-level module:
    whichever registered first wins, and the other silently runs against a core
    it was not pinned to. Nothing here can fix that — say which two copies are
    involved and carry on.
    """
    mod = sys.modules.get("animatica_core")
    if mod is None:
        return
    path = getattr(mod, "__file__", None)
    root = os.path.dirname(os.path.dirname(os.path.abspath(path))) if path else None
    if root is not None and os.path.normcase(root) == os.path.normcase(_ADDON_DIR):
        return
    print(f"[animatica] animatica_core is already imported from another addon: "
          f"{path or '(unknown location)'} "
          f"[{_core_version(root) if root else '(no CORE-VERSION)'}]; "
          f"this addon's own copy at {os.path.join(_ADDON_DIR, 'animatica_core')} "
          f"[{_core_version(_ADDON_DIR)}] will not be used.")


def _register_core() -> None:
    """Declare this host to core. Identity only — no bridge: the half of core
    this addon shares does not ask the host to touch the scene."""
    _ensure_on_sys_path()
    _warn_on_foreign_core()

    from animatica_core import host
    host.register(key="blender", product_name="Animatica for Blender")


def _unregister_core() -> None:
    """Drop the host registration, the loaded core modules and the path entry.

    A live DCC otherwise keeps the old ``animatica_core.*`` modules across an
    addon reload, so a re-vendored core would not take effect until Blender
    restarts (motionmcp-client-sdk docs/VENDORING.md, "the trap: a live DCC
    caches the old modules")."""
    try:
        from animatica_core import host
    except ImportError:
        pass
    else:
        if host.is_registered():
            host.unregister()

    for name in [m for m in sys.modules
                 if m == "animatica_core" or m.startswith("animatica_core.")]:
        del sys.modules[name]

    while _ADDON_DIR in sys.path:
        sys.path.remove(_ADDON_DIR)


def register():
    _register_core()

    properties.register()
    operators.register()
    bpy.utils.register_class(canonical_skeleton.ANIMATICA_OT_import_canonical_skeleton)
    constraints_ui.register()
    panels.register()
    path_follow.register()
    timeline_operators.register()
    timeline_overlay.register_draw_handler()

    _reset_runtime_flags()

    # Install persistent handlers, purging any stale copies from prior loads.
    _purge_stale_handlers(bpy.app.handlers.save_pre, "_animatica_save_pre")
    bpy.app.handlers.save_pre.append(_animatica_save_pre)
    _purge_stale_handlers(bpy.app.handlers.load_post, "_animatica_load_post")
    bpy.app.handlers.load_post.append(_animatica_load_post)


def unregister():
    _purge_stale_handlers(bpy.app.handlers.save_pre, "_animatica_save_pre")
    _purge_stale_handlers(bpy.app.handlers.load_post, "_animatica_load_post")

    timeline_overlay.unregister_draw_handler()
    timeline_operators.unregister()
    path_follow.unregister()
    panels.unregister()
    constraints_ui.unregister()
    bpy.utils.unregister_class(canonical_skeleton.ANIMATICA_OT_import_canonical_skeleton)
    operators.unregister()
    properties.unregister()

    # Last: the classes above may still reach into core while unregistering.
    _unregister_core()
