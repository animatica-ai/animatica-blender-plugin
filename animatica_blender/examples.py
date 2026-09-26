# SPDX-License-Identifier: GPL-3.0-or-later
"""Example scenes: pick one, and a finished set opens, ready to generate.

A prompt on its own teaches what to type. A scene teaches what the addon is
for: a character on a set, the prompt block sized to the idea, the sidebar
open on the button that remains to be pressed. So "Try an example" opens a
.blend rather than filling in a text box, and the examples climb from one
gesture to a sequence with three beats in it — a short course, taken by
pressing Generate seven times.

Three decisions shape this module.

* **Downloaded, not bundled.** A scene with the hero is twelve megabytes; the
  addon is under one. The files are release assets, fetched the first time
  each is picked and cached beside the poser model, and the manifest that
  lists them is fetched too — so an example can be added or fixed without
  shipping a new build. A copy of the manifest ships inside the addon, so
  the menu is populated before the first network round trip and offline.
* **Opened untitled.** ``wm.read_homefile`` loads the example's contents
  without adopting its path, the way Blender's own demo files behave: Ctrl+S
  asks where to save instead of writing over the cached copy.
* **Verified.** Each file's sha256 is in the manifest. A download that does
  not match is discarded rather than opened, and a cached file is re-checked
  by size before use — cheap enough for a menu, sufficient to catch a
  truncated download.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import threading
import urllib.request

import bpy

REPO = "animatica-ai/animatica-blender-plugin"
RELEASE_TAG = "examples-v1"
BASE_URL = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}"
MANIFEST_URL = f"{BASE_URL}/examples.json"

#: The copy that ships with the addon — the menu's contents before the first
#: fetch, and whenever the network is not there.
_BUNDLED = pathlib.Path(__file__).with_name("examples.json")

_state: dict = {
    "manifest": None,          # the parsed manifest in use
    "fetching": False,         # manifest refresh in flight
    "downloading": "",         # id of the example being fetched, if any
    "done": 0, "total": 0,     # its progress, in bytes
    "error": "",
    "pending_open": "",        # id to open when its download lands
}


def state() -> dict:
    return dict(_state)


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

def cache_dir() -> pathlib.Path:
    path = pathlib.Path(bpy.utils.user_resource("DATAFILES", path="animatica_examples",
                                                create=True))
    return path


def _cached_manifest_path() -> pathlib.Path:
    return cache_dir() / "examples.json"


def cached_path(entry: dict) -> pathlib.Path:
    return cache_dir() / entry["file"]


def is_cached(entry: dict) -> bool:
    path = cached_path(entry)
    return path.is_file() and path.stat().st_size == int(entry.get("size") or -1)


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

def manifest() -> dict:
    """The examples, newest source first: fetched, then cached, then bundled."""
    if _state["manifest"] is not None:
        return _state["manifest"]
    for path in (_cached_manifest_path(), _BUNDLED):
        try:
            _state["manifest"] = json.loads(path.read_text())
            return _state["manifest"]
        except (OSError, ValueError):
            continue
    _state["manifest"] = {"examples": []}
    return _state["manifest"]


def entries() -> list[dict]:
    return list(manifest().get("examples", []))


def find(example_id: str) -> dict | None:
    return next((e for e in entries() if e.get("id") == example_id), None)


def _fetch_manifest_worker():
    try:
        req = urllib.request.Request(MANIFEST_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and isinstance(data.get("examples"), list):
            _cached_manifest_path().write_text(json.dumps(data, indent=2))
            _state["manifest"] = data
    except Exception:                       # noqa: BLE001 — the bundled copy stands in
        pass
    finally:
        _state["fetching"] = False


def refresh_manifest_async() -> None:
    """Ask the release for the current list, once a session, off the main thread."""
    if _state["fetching"] or _state.get("refreshed"):
        return
    _state["fetching"] = True
    _state["refreshed"] = True
    threading.Thread(target=_fetch_manifest_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Downloading one
# ---------------------------------------------------------------------------

def _url(entry: dict) -> str:
    return entry.get("url") or f"{BASE_URL}/{entry['file']}"


def _download_worker(entry: dict):
    dest = cached_path(entry)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".part-", suffix=".blend")
    os.close(fd)
    try:
        req = urllib.request.Request(_url(entry), headers={"Accept": "application/octet-stream"})
        digest = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60.0) as resp, open(tmp, "wb") as out:
            _state["total"] = int(resp.headers.get("Content-Length") or entry.get("size") or 0)
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                _state["done"] += len(chunk)
        want = (entry.get("sha256") or "").lower()
        if want and digest.hexdigest() != want:
            raise ValueError("the download did not match its checksum — try again")
        os.replace(tmp, dest)               # atomic: a half-written file is never "cached"
    except Exception as exc:                # noqa: BLE001
        _state["error"] = f"could not download {entry['title']}: {exc}"
        _state["pending_open"] = ""
        try:
            os.unlink(tmp)
        except OSError:
            pass
    finally:
        _state["downloading"] = ""
        bpy.app.timers.register(_after_download, first_interval=0.0)


def _after_download():
    _redraw()
    pending = _state["pending_open"]
    if pending:
        _state["pending_open"] = ""
        entry = find(pending)
        if entry is not None and is_cached(entry):
            open_now(entry)
    return None


def download_async(entry: dict, *, then_open: bool) -> bool:
    if _state["downloading"]:
        return False
    _state.update({"downloading": entry["id"], "done": 0,
                   "total": int(entry.get("size") or 0), "error": "",
                   "pending_open": entry["id"] if then_open else ""})
    threading.Thread(target=_download_worker, args=(entry,), daemon=True).start()
    return True


def download_percent() -> float:
    return (100.0 * _state["done"] / _state["total"]) if _state["total"] else 0.0


# ---------------------------------------------------------------------------
# Opening one
# ---------------------------------------------------------------------------

def open_now(entry: dict) -> None:
    """Load the example as an untitled session, then put the sidebar on it."""
    path = cached_path(entry)
    bpy.ops.wm.read_homefile(filepath=str(path), load_ui=True)
    # The Animatica tab cannot be chosen when the file is built (there is no
    # window in a background build), so it is chosen here, once the UI exists.
    bpy.app.timers.register(_show_sidebar, first_interval=0.1)


def _show_sidebar():
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            area.spaces[0].show_region_ui = True
            for region in area.regions:
                if region.type == 'UI':
                    try:
                        region.active_panel_category = "Animatica"
                    except (AttributeError, TypeError):
                        pass
            area.tag_redraw()
    return None


def _redraw():
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()


# ---------------------------------------------------------------------------
# Operator and menu
# ---------------------------------------------------------------------------

class ANIMATICA_OT_open_example(bpy.types.Operator):
    bl_idname = "animatica.open_example"
    bl_label = "Open Example"
    bl_description = ("Open a finished example scene — character, set and prompt — "
                      "ready to generate. Downloads it the first time")
    bl_options = {'REGISTER'}

    example: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        entry = find(properties.example)
        if entry is None:
            return cls.bl_description
        where = "" if is_cached(entry) else f"  ({entry['size'] / 1048576:.1f} MB download)"
        return f"{entry.get('lesson', '')}\n\n“{entry['prompt']}”{where}"

    def invoke(self, context, event):
        entry = find(self.example)
        if entry is None:
            self.report({'ERROR'}, "that example is not in the list")
            return {'CANCELLED'}
        # Opening replaces the session. Unsaved work is the one thing worth a
        # confirmation; a clean file just opens.
        if bpy.data.is_dirty:
            return context.window_manager.invoke_confirm(
                self, event,
                title=f"Open “{entry['title']}”?",
                message="Unsaved changes in this file will be lost.",
                confirm_text="Open Example",
            )
        return self.execute(context)

    def execute(self, context):
        settings = getattr(context.scene, "animatica", None)
        if settings is not None and getattr(settings, "is_generating", False):
            self.report({'ERROR'}, "a generation is running — let it finish first")
            return {'CANCELLED'}
        entry = find(self.example)
        if entry is None:
            return {'CANCELLED'}
        if is_cached(entry):
            open_now(entry)
            return {'FINISHED'}
        if not download_async(entry, then_open=True):
            self.report({'WARNING'}, "another example is still downloading")
            return {'CANCELLED'}
        self.report({'INFO'}, f"downloading {entry['title']}…")
        return {'FINISHED'}


class ANIMATICA_MT_examples(bpy.types.Menu):
    bl_idname = "ANIMATICA_MT_examples"
    bl_label = "Try an Example"

    def draw(self, context):
        refresh_manifest_async()
        layout = self.layout
        last_tier = None
        for entry in entries():
            tier = entry.get("tier")
            if last_tier is not None and tier != last_tier:
                layout.separator()
            last_tier = tier
            text = f"{entry['title']}  ·  {entry['seconds']:g}s  ·  {entry['character']}"
            icon = 'FILE_BLEND' if is_cached(entry) else 'IMPORT'
            layout.operator("animatica.open_example", text=text, icon=icon).example = entry["id"]


def draw_status(layout) -> None:
    """A line under the menu while an example is on its way, or went wrong."""
    if _state["downloading"]:
        entry = find(_state["downloading"])
        name = entry["title"] if entry else "example"
        sub = layout.row()
        sub.active = False
        sub.label(text=f"Downloading {name}…  {download_percent():.0f}%", icon='SORTTIME')
    elif _state["error"]:
        row = layout.row()
        row.alert = True
        row.label(text=_state["error"][:70], icon='ERROR')


def draw_credit(layout, scene) -> None:
    """Attribution for an example's character, while its scene is open.

    CC-BY asks for the credit to be visible where the work is used; the panel
    of the file that uses it is that place.
    """
    credit = scene.get("animatica_example_credit")
    if not credit:
        return
    row = layout.row()
    row.active = False
    row.label(text=credit.split(" — ")[0], icon='INFO')


_classes = (ANIMATICA_OT_open_example, ANIMATICA_MT_examples)


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
