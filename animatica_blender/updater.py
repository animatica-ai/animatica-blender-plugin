# SPDX-License-Identifier: GPL-3.0-or-later
"""Keeping the addon current, without making it the artist's errand.

A preview moves fast, and the version someone installed in week one answers
bug reports from week three. So the addon looks at the releases page itself,
says when there is a newer build, and installs it on one click.

Three things this deliberately does NOT do:

* **Install without being asked.** Checking is automatic; replacing the code
  running the artist's session is not. The check is a GET; the update is a
  decision.
* **Touch a development checkout.** ``make install`` symlinks the source tree
  into Blender's addons directory. Installing a zip over that link replaces
  the link with a copy, and the next edit in the working tree silently does
  nothing — which is a very confusing hour. If the addon directory is a
  symlink, updating refuses and says why.
* **Pretend a hot swap is a restart.** The swap — disable, install, enable —
  is genuine and usually clean, because unregister() removes the handlers and
  timers this addon owns. But Python modules already imported stay imported,
  and anything holding a reference to the old code keeps it. So the answer is
  always the same sentence, whether the swap went well or badly: restart
  Blender. Hedging it into "if anything looks odd" asks the artist to judge
  something they have no way to judge, and the half of the addon still running
  the old code will not announce itself.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import threading
import time
import urllib.request

import bpy

#: Where releases come from. The repository is public, so the API needs no
#: token — and no token is what we want: an update check that depends on the
#: artist being signed into anything is an update check that does not run.
REPO = "animatica-ai/animatica-blender-plugin"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

#: How long a check is good for. Startup checks are the common case and the
#: releases page does not change hourly.
CHECK_INTERVAL = 24 * 60 * 60.0

#: Long enough for a slow network, short enough that a hung request does not
#: sit on a worker thread for the rest of the session.
TIMEOUT = 15.0

#: The newest release we have heard about, and what we are doing about it.
_state: dict = {
    "checking": False,
    "at": 0.0,            # when the last check finished (monotonic)
    "error": "",
    "tag": "",            # e.g. "v0.6.1-preview2"
    "version": None,      # (0, 6, 1) — comparable
    "url": "",            # the .zip asset
    "size": 0,
    "notes": "",
    "page": "",           # the release's own page, for "what changed"
    "prerelease": False,
    "downloading": False,
    "installed": "",      # the version this session swapped in, if any
}


def state() -> dict:
    return dict(_state)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

#: ``v0.6.1-preview2`` -> ((0, 6, 1), "preview", 2). A tag with no suffix is a
#: real release, and sorts above every preview of the same number.
_TAG = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?([A-Za-z]+)\.?(\d+)?)?$")


def parse_tag(tag: str):
    """``(version tuple, rank, build)`` for a release tag, or None.

    ``rank`` orders a final release above its own previews: a plain ``v0.6.0``
    is 1, anything with a suffix is 0. ``build`` separates preview1 from
    preview2. Anything this cannot read is skipped rather than guessed at —
    a tag nobody can parse is not worth offering as an upgrade.
    """
    m = _TAG.match((tag or "").strip())
    if m is None:
        return None
    major, minor, patch, word, build = m.groups()
    version = (int(major), int(minor), int(patch or 0))
    return version, (0 if word else 1), int(build or 0)


def current_version() -> tuple:
    from . import bl_info

    return tuple(bl_info["version"])


def _sort_key(tag: str):
    parsed = parse_tag(tag)
    return parsed if parsed is not None else ((0, 0, 0), 0, 0)


def is_newer(tag: str) -> bool:
    """Is ``tag`` a build the artist does not have?

    The installed build has no suffix to compare — bl_info carries numbers
    only — so it is treated as a final release of its version. That is the
    conservative reading: it means a preview of the SAME number is not offered
    as an upgrade to someone already running that number.
    """
    parsed = parse_tag(tag)
    if parsed is None:
        return False
    return parsed > (current_version(), 1, 0)


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def _prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


def _pick(releases, *, allow_prerelease: bool):
    """The newest release worth offering, with a .zip to install."""
    best = None
    for rel in releases:
        if rel.get("draft"):
            continue                        # not published; nobody should see it
        if rel.get("prerelease") and not allow_prerelease:
            continue
        tag = rel.get("tag_name") or ""
        if parse_tag(tag) is None or not is_newer(tag):
            continue
        asset = next((a for a in rel.get("assets", [])
                      if (a.get("name") or "").endswith(".zip")), None)
        if asset is None:
            continue                        # a release with no build is not an update
        if best is None or _sort_key(tag) > _sort_key(best[0].get("tag_name", "")):
            best = (rel, asset)
    return best


def _check_worker(allow_prerelease: bool):
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"animatica-blender/{'.'.join(map(str, current_version()))}"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
        found = _pick(releases, allow_prerelease=allow_prerelease)
        if found is None:
            _state.update({"tag": "", "version": None, "url": "", "notes": "", "error": ""})
        else:
            rel, asset = found
            parsed = parse_tag(rel["tag_name"])
            _state.update({
                "tag": rel["tag_name"],
                "version": parsed[0] if parsed else None,
                "url": asset.get("browser_download_url", ""),
                "size": int(asset.get("size") or 0),
                "notes": (rel.get("body") or "").strip(),
                "page": rel.get("html_url") or RELEASES_PAGE,
                "prerelease": bool(rel.get("prerelease")),
                "error": "",
            })
    except Exception as exc:                # noqa: BLE001 — a failed check is not an event
        _state["error"] = str(exc)
    finally:
        _state["checking"] = False
        _state["at"] = time.monotonic()
        bpy.app.timers.register(_redraw, first_interval=0.0)


def _redraw():
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


def check_async(*, force: bool = False) -> bool:
    """Ask the releases page what exists. True if a check started.

    Runs on a worker thread and touches no Blender data: it is a GET, and the
    main thread is where the artist is.
    """
    if _state["checking"]:
        return False
    prefs = _prefs()
    if prefs is None:
        return False
    if not force and not getattr(prefs, "check_updates", True):
        return False
    if not force and _state["at"] and (time.monotonic() - _state["at"]) < CHECK_INTERVAL:
        return False
    _state.update({"checking": True, "error": ""})
    allow = bool(getattr(prefs, "update_previews", True))
    threading.Thread(target=_check_worker, args=(allow,), daemon=True).start()
    return True


def update_available() -> bool:
    return bool(_state["tag"]) and not _state["installed"]


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def addon_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def is_development_checkout() -> bool:
    """Is this addon a symlink into a working tree rather than an install?

    ``make install`` links the source tree in. Installing a zip over the link
    replaces it with a copy, and every later edit in the working tree silently
    does nothing.
    """
    for base in bpy.utils.script_paths(subdir="addons"):
        candidate = pathlib.Path(base) / __package__
        if candidate.is_symlink():
            return True
    return False


def _download(url: str, dest: pathlib.Path) -> None:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream",
                 "User-Agent": f"animatica-blender/{'.'.join(map(str, current_version()))}"},
    )
    with urllib.request.urlopen(req, timeout=60.0) as resp, open(dest, "wb") as out:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)


def _swap(path: str, tag: str):
    """Replace the installed addon with the downloaded zip, and reload it.

    Runs from a timer rather than inside the operator that asked for it: the
    swap unregisters the operator's own class, and doing that while its
    execute() is still on the stack is asking for trouble.
    """
    import addon_utils

    module = __package__
    try:
        addon_utils.disable(module, default_set=False)
    except Exception as exc:                # noqa: BLE001
        _state["error"] = f"could not unload the current version: {exc}"
        return None

    try:
        bpy.ops.preferences.addon_install(filepath=path, overwrite=True)
    except Exception as exc:                # noqa: BLE001
        _state["error"] = f"install failed: {exc}"
        _try_enable(module)                 # put the old one back rather than leave nothing
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    # The new files are on disk; drop the old modules so the enable below
    # imports them rather than finding the previous load in sys.modules.
    import sys

    for name in [n for n in list(sys.modules) if n == module or n.startswith(module + ".")]:
        del sys.modules[name]

    if _try_enable(module):
        _state.update({"installed": tag, "error": ""})
    else:
        _state["error"] = ("the new version is installed but would not load — "
                           "restart Blender to finish the update")
    _redraw()
    return None


def _try_enable(module: str) -> bool:
    import addon_utils

    try:
        addon_utils.enable(module, default_set=True, persistent=True)
    except Exception:                       # noqa: BLE001
        return False
    loaded_default, loaded_state = addon_utils.check(module)
    return bool(loaded_state)


def _install_worker(url: str, tag: str):
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix="animatica-", suffix=".zip")
    os.close(fd)
    try:
        _download(url, pathlib.Path(tmp))
    except Exception as exc:                # noqa: BLE001
        _state["error"] = f"download failed: {exc}"
        _state["downloading"] = False
        bpy.app.timers.register(_redraw, first_interval=0.0)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return
    _state["downloading"] = False
    bpy.app.timers.register(lambda: _swap(tmp, tag), first_interval=0.0)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class ANIMATICA_OT_check_update(bpy.types.Operator):
    bl_idname = "animatica.check_update"
    bl_label = "Check for Updates"
    bl_description = "Ask GitHub whether a newer build of this addon has been released"

    def execute(self, context):
        check_async(force=True)
        self.report({'INFO'}, "checking for updates…")
        return {'FINISHED'}


class ANIMATICA_OT_update(bpy.types.Operator):
    bl_idname = "animatica.update"
    bl_label = "Update Animatica"
    bl_description = ("Download the newer build and install it over this one, "
                      "then restart Blender to finish")
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return update_available() and not _state["downloading"]

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        mb = _state["size"] / (1024 * 1024) if _state["size"] else 0.0
        layout.label(text=f"Install {_state['tag']}"
                          + (f" ({mb:.1f} MB)" if mb else "") + "?", icon='IMPORT')
        col = layout.column(align=True)
        col.active = False
        col.label(text="Your scene is untouched — only the addon is replaced.")
        col.label(text="Restart Blender afterwards to finish the update.")

    def execute(self, context):
        settings = getattr(context.scene, "animatica", None)
        if settings is not None and getattr(settings, "is_generating", False):
            self.report({'ERROR'}, "a generation is running — let it finish first")
            return {'CANCELLED'}
        if is_development_checkout():
            self.report({'ERROR'},
                        "this is a linked development checkout — update it with git, "
                        "not from here")
            return {'CANCELLED'}
        if not _state["url"]:
            self.report({'ERROR'}, "no build to install — check for updates first")
            return {'CANCELLED'}
        _state.update({"downloading": True, "error": ""})
        threading.Thread(target=_install_worker,
                         args=(_state["url"], _state["tag"]), daemon=True).start()
        self.report({'INFO'}, f"downloading {_state['tag']}…")
        return {'FINISHED'}


class ANIMATICA_OT_open_releases(bpy.types.Operator):
    bl_idname = "animatica.open_releases"
    bl_label = "What Changed"
    bl_description = "Open the release notes in your browser"

    def execute(self, context):
        bpy.ops.wm.url_open(url=_state["page"] or RELEASES_PAGE)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Drawing — one row, in the two places someone would look
# ---------------------------------------------------------------------------

def draw_banner(layout, context) -> None:
    """The update row for the sidebar: only when there is something to say."""
    if _state["installed"]:
        box = layout.box()
        col = box.column(align=True)
        col.label(text=f"Updated to {_state['installed']}", icon='CHECKMARK')
        col.label(text="Restart Blender to finish", icon='INFO')
        return
    if not update_available():
        return
    box = layout.box()
    col = box.column(align=True)
    # Two rows rather than one: the sidebar is narrow, and a label sharing a
    # row with a button loses its tail — "v0.6.1-previe…" told nobody which
    # build was on offer.
    col.label(text=f"New version: {_state['tag']}", icon='IMPORT')
    if _state["downloading"]:
        sub = col.row()
        sub.active = False
        sub.label(text="downloading…")
    else:
        col.operator("animatica.update", text="Update")
    if _state["error"]:
        err = box.row()
        err.alert = True
        err.label(text=_state["error"][:70], icon='ERROR')


def draw_preferences(layout, context) -> None:
    """The fuller version, for the preferences: state, notes, and the switches."""
    box = layout.box()
    row = box.row(align=True)
    version = ".".join(str(v) for v in current_version())
    row.label(text=f"Version {version}", icon='FILE_BLEND')
    row.operator("animatica.check_update", text="", icon='FILE_REFRESH')

    if _state["checking"]:
        box.label(text="checking for updates…", icon='SORTTIME')
    elif _state["installed"]:
        col = box.column(align=True)
        col.label(text=f"Updated to {_state['installed']}", icon='CHECKMARK')
        col.label(text="Restart Blender to finish the update", icon='INFO')
    elif update_available():
        # The version gets a line of its own here too: sharing one with two
        # buttons is how "v0.6.1-preview2" became "v0.6.1…".
        col = box.column(align=True)
        # No size here: the panel is as narrow as the artist has made it, and
        # the confirm dialog states the size anyway. The version is the part
        # that must survive being elided.
        col.label(text=f"New version: {_state['tag']}", icon='IMPORT')
        row = col.row(align=True)
        row.operator("animatica.update", text="Update")
        row.operator("animatica.open_releases", text="", icon='URL')
        if _state["notes"]:
            note = box.column(align=True)
            note.active = False
            for line in _state["notes"].splitlines()[:4]:
                if line.strip():
                    note.label(text=line.strip()[:80])
    elif _state["error"]:
        err = box.row()
        err.alert = True
        err.label(text=_state["error"][:70], icon='ERROR')
    else:
        box.label(text="up to date", icon='CHECKMARK')

    col = box.column(align=True)
    prefs = _prefs()
    if prefs is not None:
        col.prop(prefs, "check_updates")
        sub = col.row()
        sub.active = bool(getattr(prefs, "check_updates", True))
        sub.prop(prefs, "update_previews")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (ANIMATICA_OT_check_update, ANIMATICA_OT_update, ANIMATICA_OT_open_releases)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
