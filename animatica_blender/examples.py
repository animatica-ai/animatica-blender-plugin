# SPDX-License-Identifier: GPL-3.0-or-later
"""Example scenes: pick one, and a finished set opens, ready to generate.

A prompt on its own teaches what to type. A scene teaches what the addon is
for: a character on a set, the prompt block sized to the idea, the sidebar
open on the button that remains to be pressed. So "Try an example" opens a
.blend rather than filling in a text box, and the examples climb from one
gesture to a sequence with three beats in it — a short course, taken by
pressing Generate seven times.

Three decisions shape this module.

* **Listed live from the asset repository.** The examples are the ``.blend``
  files in ``examples/`` of ``animatica-ai/animatica-assets-public``, the
  repository the addon already takes the Hero from. The menu is built from
  whatever is there: add a file and it appears, delete one and it goes, with
  no manifest to keep in step and no addon build. A ``<name>.json`` beside a
  file gives the menu its title, place and lesson; without one, the file name
  stands in. The list is read at one resolved commit, so every file it names
  and every download it leads to come from the same state of the repository.
  A CDN cannot pair an old list with a new file. The last list is cached for
  offline use.
* **Opened untitled.** ``wm.read_homefile`` loads the example's contents
  without adopting its path, the way Blender's own demo files behave: Ctrl+S
  asks where to save instead of writing over the cached copy.
* **Verified.** The repository keeps the files in Git LFS, and an LFS
  pointer's ``oid`` is the file's sha256. A download that does not match is
  discarded rather than opened, and the hash is part of the cached file's
  name, so an updated example is a new download and never a stale hit.
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

REPO = "animatica-ai/animatica-assets-public"
#: The branch the menu follows. A commit here changes what every installed
#: addon offers, which is the point: examples move without a release.
REF = "main"
FOLDER = "examples"
API = f"https://api.github.com/repos/{REPO}"


def _raw_url(commit: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{commit}/{path}"


def _media_url(commit: str, path: str) -> str:
    # LFS files: raw.githubusercontent.com serves the pointer, the media host
    # serves the content (the same split remote_asset relies on).
    return f"https://media.githubusercontent.com/media/{REPO}/{commit}/{path}"


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
    """The examples as last listed: this session's fetch, else the cached one."""
    if _state["manifest"] is not None:
        return _state["manifest"]
    try:
        _state["manifest"] = json.loads(_cached_manifest_path().read_text())
    except (OSError, ValueError):
        _state["manifest"] = {"examples": []}
    return _state["manifest"]


def entries() -> list[dict]:
    return list(manifest().get("examples", []))


def find(example_id: str) -> dict | None:
    return next((e for e in entries() if e.get("id") == example_id), None)


def _get(url: str, accept: str = "application/json", limit: int | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "animatica-blender"})
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return resp.read(limit) if limit else resp.read()


def _lfs_pointer(text: str) -> tuple[str, int] | None:
    """``(sha256, size)`` from a Git LFS pointer, or None if *text* is not one."""
    if not text.startswith("version https://git-lfs.github.com/spec/"):
        return None
    oid = size = None
    for line in text.splitlines():
        if line.startswith("oid sha256:"):
            oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            size = int(line.split()[1])
    return (oid, size) if oid and size else None


def _entry(commit: str, item: dict, sidecars: dict) -> dict:
    """One example from its listing item, its LFS pointer and its sidecar."""
    stem = item["name"][:-len(".blend")]
    meta = {}
    if stem in sidecars:
        try:
            meta = json.loads(_get(_raw_url(commit, sidecars[stem])).decode("utf-8"))
        except (OSError, ValueError):
            meta = {}
    pointer = _lfs_pointer(_get(_raw_url(commit, item["path"]), "*/*", 512).decode("utf-8", "replace"))
    if pointer is not None:
        sha, size, url = pointer[0], pointer[1], _media_url(commit, item["path"])
    else:                               # committed without LFS: no hash to check against
        sha, size, url = "", int(item.get("size") or 0), _raw_url(commit, item["path"])
    return {
        "id": stem,
        "title": meta.get("title") or stem.replace("-", " ").capitalize(),
        "tier": meta.get("tier", 99),
        "order": meta.get("order", 999),
        "seconds": meta.get("seconds"),
        "prompt": meta.get("prompt", ""),
        "lesson": meta.get("lesson", ""),
        "character": meta.get("character", ""),
        "credit": meta.get("credit", ""),
        "file": f"{stem}.{sha[:8]}.blend" if sha else item["name"],
        "url": url,
        "size": size,
        "sha256": sha,
    }


def _fetch_manifest() -> bool:
    """List the examples from the repository now. True if the list changed."""
    try:
        commit = _get(f"{API}/commits/{REF}", "application/vnd.github.sha").decode().strip()
        listing = json.loads(_get(f"{API}/contents/{FOLDER}?ref={commit}"))
    except Exception:                       # noqa: BLE001 — the cached list stands in
        return False
    if not isinstance(listing, list):
        return False
    blends = [i for i in listing if i.get("type") == "file" and i["name"].endswith(".blend")]
    sidecars = {i["name"][:-len(".json")]: i["path"] for i in listing
                if i.get("type") == "file" and i["name"].endswith(".json")}
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            found = list(pool.map(lambda item: _entry(commit, item, sidecars), blends))
    except Exception:                       # noqa: BLE001
        return False
    found.sort(key=lambda e: (e["tier"], e["order"], e["title"]))
    data = {"source": {"repo": REPO, "ref": REF, "commit": commit}, "examples": found}
    changed = data.get("examples") != manifest().get("examples")
    _cached_manifest_path().write_text(json.dumps(data, indent=2))
    _state["manifest"] = data
    _prune_cache(data)
    return changed


def _prune_cache(data: dict) -> None:
    """Drop cached example files the current list no longer names.

    File names carry their content hash, so every update to an example is a
    new file; without this the cache would keep every version ever fetched.
    """
    keep = {e.get("file") for e in data.get("examples", [])}
    for path in cache_dir().glob("*.blend"):
        # A download in flight is a ".part-" file; pruning it from under the
        # worker that is writing it is how a retry after an update used to
        # fail with "no such file".
        if path.name.startswith("."):
            continue
        if path.name not in keep:
            try:
                path.unlink()
            except OSError:
                pass


def _fetch_manifest_worker():
    try:
        _fetch_manifest()
    finally:
        _state["fetching"] = False
        bpy.app.timers.register(lambda: (_redraw(), None)[1], first_interval=0.0)


def refresh_manifest_async() -> None:
    """List the examples again, once a session, off the main thread."""
    if _state["fetching"] or _state.get("refreshed"):
        return
    _state["fetching"] = True
    _state["refreshed"] = True
    threading.Thread(target=_fetch_manifest_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Downloading one
# ---------------------------------------------------------------------------

def _url(entry: dict) -> str:
    return entry["url"]


def _download_worker(entry: dict, retried: bool = False):
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
            # Usually not corruption: the example was replaced in the
            # repository since this list was read (from the cache, say). List
            # again and, if this example changed, try once more against it;
            # otherwise a fixed example would be refused until the next
            # session, and "try again" would never help.
            if not retried and _fetch_manifest():
                fresh = find(entry["id"])
                if fresh is not None and fresh.get("sha256") != entry.get("sha256"):
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
                    _state["done"] = 0
                    return _download_worker(fresh, retried=True)
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
    bl_description = ("Open a finished example scene: character, set and prompt, "
                      "ready to generate. Downloads it the first time")
    bl_options = {'REGISTER'}

    example: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        entry = find(properties.example)
        if entry is None:
            return cls.bl_description
        where = "" if is_cached(entry) else f"  ({entry['size'] / 1048576:.1f} MB download)"
        prompt = f"\n\n“{entry['prompt']}”" if entry.get("prompt") else ""
        return f"{entry.get('lesson', '')}{prompt}{where}".strip()

    def invoke(self, context, event):
        refresh_manifest_async()
        entry = find(self.example)
        if entry is None:
            self.report({'ERROR'}, "that example is not in the list")
            return {'CANCELLED'}
        # Opening replaces the session, and nothing about an example menu
        # says so: always ask, and say what closes. Unsaved work is named.
        message = "It opens as a new file; your current scene closes."
        if bpy.data.is_dirty:
            message += " Unsaved changes will be lost."
        return context.window_manager.invoke_confirm(
            self, event,
            title=f"Open the “{entry['title']}” example?",
            message=message,
            confirm_text="Open Example",
        )

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
    bl_label = "Open an Example Scene"

    def draw(self, context):
        refresh_manifest_async()
        layout = self.layout
        found = entries()
        if not found:
            layout.label(text="Listing examples…" if _state["fetching"]
                         else "Examples need a connection the first time", icon='INFO')
            return
        last_tier = None
        for entry in found:
            tier = entry.get("tier")
            if last_tier is not None and tier != last_tier:
                layout.separator()
            last_tier = tier
            parts = [entry["title"]]
            if entry.get("seconds"):
                parts.append(f"{entry['seconds']:g}s")
            if entry.get("character"):
                parts.append(entry["character"])
            text = "  ·  ".join(parts)
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
