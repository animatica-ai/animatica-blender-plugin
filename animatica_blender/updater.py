# SPDX-License-Identifier: GPL-3.0-or-later
"""Keeping the addon current, without making it the artist's errand.

A preview moves fast, and the version someone installed in week one answers
bug reports from week three. So the addon looks at the releases page itself,
says when there is a newer build, and installs it on one click.

Four things this deliberately does NOT do:

* **Install without being asked.** Checking is automatic; replacing the code
  running the artist's session is not. The check is a GET; the update is a
  decision.
* **Touch a development checkout.** ``make install`` symlinks the source tree
  into Blender's addons directory. Installing a zip over that link replaces
  the link with a copy — or, if the link is further up (``scripts/`` or
  ``addons/`` itself), writes the zip straight into the working tree. If any
  part of the path is a link, or the addon lives inside a git checkout,
  updating refuses and says why.
* **Leave the artist with nothing.** A download that is not a build — an
  HTML page, half a zip, the wrong folder, Python that does not compile — is
  refused before anything installed is touched. What cannot be caught that
  way (a register() that raises) is caught by the swap: the old install is
  moved aside, not deleted, until the new one has loaded, and goes back if it
  does not.
* **Pretend a hot swap is a restart.** The swap — disable, install, enable —
  is genuine and usually clean, because unregister() removes the handlers and
  timers this addon owns. But Python modules already imported stay imported,
  and anything holding a reference to the old code keeps it. So the answer is
  always the same sentence, whether the swap went well or badly: restart
  Blender. Hedging it into "if anything looks odd" asks the artist to judge
  something they have no way to judge, and the half of the addon still running
  the old code will not announce itself.

Threads: the check and the download run on worker threads and touch no
Blender API at all — not even ``bpy.app.timers``. They post their results to
a queue, and a timer on the main thread applies them.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import re
import shutil
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile

import bpy

#: Where releases come from. The repository is public, so the API needs no
#: token — and no token is what we want: an update check that depends on the
#: artist being signed into anything is an update check that does not run.
REPO = "animatica-ai/animatica-blender-plugin"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"

#: How long a check is good for. Startup checks are the common case and the
#: releases page does not change hourly. Kept on disk, so "once a day" means
#: once a day and not once per Blender session.
CHECK_INTERVAL = 24 * 60 * 60.0

#: After a failed check, try again sooner: one bad network at startup should
#: not hide a release for a day.
RETRY_INTERVAL = 60 * 60.0

#: Long enough for a slow network, short enough that a hung request does not
#: sit on a worker thread for the rest of the session.
TIMEOUT = 15.0

#: A build is a few MB, a build with the model a few hundred. Anything past
#: this is not one of ours, and not worth filling someone's disk to find out.
MAX_DOWNLOAD = 1024 * 1024 * 1024

#: The build asset on a release, as ``make zip`` names it. A release may carry
#: other zips (a model bundle, say); only this one is the addon.
_ASSET = re.compile(r"^animatica-blender-.*\.zip$", re.IGNORECASE)

#: The newest release we have heard about, and what we are doing about it.
_state: dict = {
    "checking": False,
    "at": 0.0,            # when the last check finished (wall clock, persisted)
    "ok": True,           # whether that check worked
    "channel": None,      # whether it included previews
    "error": "",
    "tag": "",            # e.g. "v0.6.1-preview2"
    "version": None,      # (0, 6, 1) — comparable
    "url": "",            # the .zip asset
    "size": 0,
    "notes": "",
    "page": "",           # the release's own page, for "what changed"
    "prerelease": False,
    "downloading": False,
    "waiting": False,     # downloaded, holding off until a generation ends
    "installed": "",      # the version this session swapped in, if any
}

#: Results from the worker threads, for the main thread to apply.
_results: "queue.Queue" = queue.Queue()


def state() -> dict:
    return dict(_state)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

#: ``v0.6.1-preview2`` -> ((0, 6, 1), rank of "preview", 2). Between the word
#: and its number there may be nothing, a dot or a dash.
_TAG = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.]?([A-Za-z]+)[-.]?(\d+)?)?$",
                  re.IGNORECASE)

#: How a suffix ranks against the others on the same version number. A plain
#: release is above all of them; a word this does not know ranks at the
#: bottom — it is still a pre-release.
FINAL = 4
_SUFFIX_RANK = {"rc": 3, "beta": 2, "b": 2, "preview": 1, "pre": 1}


def parse_tag(tag: str):
    """``(version tuple, rank, build)`` for a release tag, or None.

    ``rank`` orders a final release above its release candidates, those above
    betas, and those above previews. ``build`` separates preview1 from
    preview2; a suffix with no number counts as 0. Anything this cannot read
    is skipped rather than guessed at — a tag nobody can parse is not worth
    offering as an upgrade.
    """
    m = _TAG.match(tag.strip()) if isinstance(tag, str) else None
    if m is None:
        return None
    major, minor, patch, word, build = m.groups()
    version = (int(major), int(minor), int(patch or 0))
    rank = FINAL if not word else _SUFFIX_RANK.get(word.lower(), 0)
    return version, rank, int(build or 0)


def is_prerelease_tag(tag: str) -> bool:
    """Does the tag itself say it is not a final release?

    GitHub's ``prerelease`` flag is a checkbox someone has to remember; the
    tag is what the build calls itself. Either one is enough.
    """
    parsed = parse_tag(tag)
    return parsed is not None and parsed[1] != FINAL


def current_version() -> tuple:
    from . import bl_info

    return tuple(bl_info["version"])


def current_tag() -> str:
    """The release tag this build was cut from, e.g. ``v0.6.0-preview1``.

    Stamped into ``__init__`` by the zip target. Without it every preview of
    0.6.0 looks like every other one, and a tester on preview1 is never told
    about preview2 — which is most of the point of having an updater during a
    preview.
    """
    from . import VERSION_TAG

    tag = (VERSION_TAG or "").strip()
    return tag or "v" + ".".join(str(v) for v in current_version())


def _sort_key(tag: str):
    parsed = parse_tag(tag)
    return parsed if parsed is not None else ((0, 0, 0), 0, 0)


def is_newer(tag: str) -> bool:
    """Is ``tag`` a build the artist does not have?

    Both sides are read the same way, so preview2 is newer than preview1 and
    the final release is newer than either. A build whose own tag cannot be
    parsed falls back to "the final release of its version number", which
    offers nothing older and nothing sideways.
    """
    parsed = parse_tag(tag)
    if parsed is None:
        return False
    mine = parse_tag(current_tag()) or (current_version(), FINAL, 0)
    return parsed > mine


# ---------------------------------------------------------------------------
# What we remember between sessions
# ---------------------------------------------------------------------------
#
# One small JSON file in Blender's config directory: when we last checked,
# what we found, and how the last update went. The last matters because the
# swap reloads this module — the copy that did the work is not the copy that
# draws the banner afterwards — so the outcome has to be written where the
# new copy will read it.

def _config_path():
    try:
        base = bpy.utils.user_resource('CONFIG', path="animatica", create=True)
    except Exception:                       # noqa: BLE001
        return None
    return pathlib.Path(base) / "updater.json" if base else None


def _load_config() -> dict:
    """Whatever is on disk, or nothing. A torn or hand-edited file is no file."""
    path = _config_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001 — missing, unreadable, not JSON
        return {}
    return data if isinstance(data, dict) else {}


def _save_config(**changes) -> None:
    """Merge ``changes`` into the file (a value of None removes the key)."""
    path = _config_path()
    if path is None:
        return
    data = _load_config()
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass                                # remembering is a courtesy, not a requirement


def _num(value, default=0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _restore() -> None:
    """Pick up where the last session — or the last copy of this module — left off."""
    data = _load_config()
    _state["at"] = _num(data.get("at"))
    _state["ok"] = data.get("ok") is not False
    channel = data.get("channel")
    _state["channel"] = channel if isinstance(channel, bool) else None

    offer = data.get("offer")
    if isinstance(offer, dict):
        tag, url = _text(offer.get("tag")), _text(offer.get("url"))
        if url and is_newer(tag):
            parsed = parse_tag(tag)
            _state.update({
                "tag": tag,
                "version": parsed[0] if parsed else None,
                "url": url,
                "size": int(_num(offer.get("size"))),
                "notes": _text(offer.get("notes")),
                "page": _text(offer.get("page")),
                "prerelease": bool(offer.get("prerelease")) or is_prerelease_tag(tag),
            })

    # How the last update went — shown once, by the copy it concerns.
    outcome = data.get("outcome")
    if isinstance(outcome, dict):
        tag = _text(outcome.get("tag"))
        fresh = abs(time.time() - _num(outcome.get("time"))) < 15 * 60
        if outcome.get("ok") is True and tag and tag == current_tag():
            _state["installed"] = tag
        elif outcome.get("ok") is False and fresh:
            _state["error"] = _text(outcome.get("message")) or "the update failed"
        _save_config(outcome=None)


# ---------------------------------------------------------------------------
# Main-thread plumbing
# ---------------------------------------------------------------------------

def _redraw():
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


def _pump():
    """Apply what the workers posted. A main-thread timer; never a worker's."""
    handled = False
    while True:
        try:
            kind, payload = _results.get_nowait()
        except queue.Empty:
            break
        handled = True
        try:
            if kind == "check":
                _apply_check(payload)
            elif kind == "download_failed":
                _state.update({"downloading": False, "error": payload})
            elif kind == "downloaded":
                _state["downloading"] = False
                _schedule_swap(*payload)
        except Exception:                   # noqa: BLE001 — one bad result must not stop the pump
            traceback.print_exc()
    if handled:
        _redraw()
    busy = _state["checking"] or _state["downloading"] or not _results.empty()
    return 0.25 if busy else None


def _start_pump() -> None:
    try:
        if not bpy.app.timers.is_registered(_pump):
            bpy.app.timers.register(_pump, first_interval=0.25, persistent=True)
    except Exception:                       # noqa: BLE001
        traceback.print_exc()


def _friendly(exc: Exception, doing: str) -> str:
    """Something an artist can act on, rather than an exception's repr.

    The repr still goes to the console, for whoever reads the bug report.
    """
    print(f"Animatica updater: {doing} failed: {exc!r}")
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return "GitHub is limiting update checks right now — try again later"
        if exc.code == 404:
            return "not found on GitHub — try again later"
        return f"GitHub answered with an error ({exc.code}) — try again later"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        return "could not reach GitHub — check your internet connection"
    return f"{doing} failed — try again later"


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def _prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


def _allow_previews() -> bool:
    prefs = _prefs()
    return bool(getattr(prefs, "update_previews", True)) if prefs is not None else True


def _asset_of(rel: dict):
    """The addon build on a release: the zip named like one, with a URL."""
    for asset in rel.get("assets") or ():
        if not isinstance(asset, dict):
            continue
        name, url = asset.get("name"), asset.get("browser_download_url")
        if isinstance(name, str) and _ASSET.match(name) and isinstance(url, str) and url:
            return asset
    return None


def _pick(releases, *, allow_prerelease: bool):
    """The newest release worth offering, with a build to install."""
    best = None
    for rel in releases if isinstance(releases, list) else ():
        try:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue                    # not published; nobody should see it
            tag = rel.get("tag_name") or ""
            if parse_tag(tag) is None or not is_newer(tag):
                continue
            if (rel.get("prerelease") or is_prerelease_tag(tag)) and not allow_prerelease:
                continue
            asset = _asset_of(rel)
            if asset is None:
                continue                    # a release with no build is not an update
            if best is None or _sort_key(tag) > _sort_key(best[0].get("tag_name", "")):
                best = (rel, asset)
        except Exception:                   # noqa: BLE001 — one odd release is not the whole list
            continue
    return best


def _check_worker(allow_prerelease: bool):
    """Runs on a worker thread: fetch, pick, post. No Blender API in here."""
    result = {"allow": allow_prerelease}
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": f"animatica-blender/{'.'.join(map(str, current_version()))}"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(8 * 1024 * 1024)
        try:
            releases = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            releases = None
        if not isinstance(releases, list):
            print(f"Animatica updater: the releases page sent {body[:200]!r}")
            result["error"] = "GitHub sent something other than a release list — try again later"
        else:
            found = _pick(releases, allow_prerelease=allow_prerelease)
            if found is None:
                result["offer"] = None
            else:
                rel, asset = found
                tag = rel["tag_name"]
                result["offer"] = {
                    "tag": tag,
                    "url": asset["browser_download_url"],
                    "size": int(_num(asset.get("size"))),
                    "notes": _text(rel.get("body")).strip(),
                    "page": _text(rel.get("html_url")) or RELEASES_PAGE,
                    "prerelease": bool(rel.get("prerelease")) or is_prerelease_tag(tag),
                }
    except Exception as exc:                # noqa: BLE001 — a failed check is not an event
        result["error"] = _friendly(exc, "checking for updates")
    _results.put(("check", result))


def _apply_check(result: dict) -> None:
    now = time.time()
    _state.update({"checking": False, "at": now, "channel": result["allow"]})
    if "error" in result:
        # Keep whatever we knew before: a failed check is not news that the
        # update went away.
        _state.update({"ok": False, "error": result["error"]})
        _save_config(at=now, ok=False, channel=result["allow"])
        return
    offer = result.get("offer")
    _state["ok"] = True
    if offer is None:
        _state.update({"tag": "", "version": None, "url": "", "size": 0, "notes": "",
                       "page": "", "prerelease": False})
    else:
        parsed = parse_tag(offer["tag"])
        _state.update(dict(offer, version=parsed[0] if parsed else None))
    _save_config(at=now, ok=True, channel=result["allow"], offer=offer or {})


def check_async(*, force: bool = False) -> bool:
    """Ask the releases page what exists. True if a check started.

    Runs on a worker thread and touches no Blender data: it is a GET, and the
    main thread is where the artist is. Switching the previews setting counts
    as a reason to look again.
    """
    if _state["checking"]:
        return False
    # Nothing goes online unless Blender allows it (Preferences > System).
    from .mmcp_client import online_access
    if not online_access():
        return False
    prefs = _prefs()
    if prefs is None:
        return False
    if not force and not getattr(prefs, "check_updates", True):
        return False
    allow = bool(getattr(prefs, "update_previews", True))
    if not force and _state["channel"] == allow:
        age = time.time() - _state["at"]
        wait = CHECK_INTERVAL if _state["ok"] else RETRY_INTERVAL
        if 0 <= age < wait:                 # a clock that went backwards is a stale check
            return False
    _state.update({"checking": True, "error": ""})
    threading.Thread(target=_check_worker, args=(allow,), daemon=True).start()
    _start_pump()
    return True


def update_available() -> bool:
    if not _state["tag"] or _state["installed"]:
        return False
    if _state["prerelease"] and not _allow_previews():
        return False                        # found on the other channel; not on offer here
    return is_newer(_state["tag"])


# ---------------------------------------------------------------------------
# May we replace this install?
# ---------------------------------------------------------------------------

def addon_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def _inside_git(path: str) -> bool:
    p = pathlib.Path(path)
    return any((folder / ".git").exists() for folder in (p, *p.parents))


def is_development_checkout() -> bool:
    """Is this addon linked in from a working tree rather than installed?

    ``make install`` links the source tree in; so does anyone who points
    ``scripts/`` or ``addons/`` at a folder of their own. Installing over any
    of those writes into the working tree, or replaces the link with a copy
    so every later edit silently does nothing. So: refuse if any part of the
    path is a link, or if the addon really lives inside a git checkout.
    """
    candidates = {os.path.dirname(os.path.abspath(__file__))}
    try:
        for base in bpy.utils.script_paths(subdir="addons"):
            candidates.add(os.path.abspath(os.path.join(base, __package__)))
        candidates.add(os.path.abspath(os.path.join(
            bpy.utils.user_resource('SCRIPTS', path="addons"), __package__)))
    except Exception:                       # noqa: BLE001
        pass
    for path in candidates:
        if not os.path.lexists(path):
            continue
        real = os.path.realpath(path)
        if os.path.normcase(real) != os.path.normcase(path):
            return True                     # a link somewhere along the way
        if _inside_git(real):
            return True
    return False


def generating_anywhere() -> bool:
    """Is a generation running in any scene — not just the one on screen?"""
    try:
        scenes = list(bpy.data.scenes)
    except AttributeError:                  # restricted data during registration
        return False
    return any(getattr(getattr(scene, "animatica", None), "is_generating", False)
               for scene in scenes)


# ---------------------------------------------------------------------------
# Downloading, and checking it is a build
# ---------------------------------------------------------------------------

class _Refused(Exception):
    """A failure whose message is already fit for the artist."""


def _download(url: str, dest: pathlib.Path, expected_size: int = 0) -> None:
    """Fetch ``url`` into ``dest``, or raise. A short read is a failure."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream",
                 "User-Agent": f"animatica-blender/{'.'.join(map(str, current_version()))}"},
    )
    got = 0
    with urllib.request.urlopen(req, timeout=60.0) as resp, open(dest, "wb") as out:
        length = resp.headers.get("Content-Length")
        length = int(length) if length and length.strip().isdigit() else None
        if length is not None and length > MAX_DOWNLOAD:
            raise ValueError(f"the download is {length} bytes, more than any build")
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            got += len(chunk)
            if got > MAX_DOWNLOAD:
                raise ValueError("the download is larger than any build")
            out.write(chunk)
    if length is not None and got != length:
        raise ValueError(f"the download stopped short ({got} of {length} bytes)")
    if expected_size and got != expected_size:
        raise ValueError(f"the download is {got} bytes, the release says {expected_size}")


def validate_zip(path) -> str:
    """Why this file is not a build we can install — or "" if it is one.

    A build is a zip with exactly one folder at the top, ``animatica_blender/``,
    an ``__init__.py`` in it, no path that climbs out, every entry intact, and
    Python that compiles. Anything else is refused before the installed
    version is touched.
    """
    if not zipfile.is_zipfile(path):
        return "the download is not a zip file"
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not infos:
                return "the zip is empty"
            tops, total = set(), 0
            for info in infos:
                name = info.filename
                parts = re.split(r"[\\/]", name)
                if (name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name)
                        or ".." in parts or "\0" in name):
                    return f"it has an unsafe path in it ({name!r})"
                tops.add(parts[0])
                total += info.file_size
            if total > 4 * MAX_DOWNLOAD:
                return "it unpacks to more than any build"
            if tops != {__package__}:
                return f"it holds {', '.join(sorted(tops))} rather than {__package__}/"
            if f"{__package__}/__init__.py" not in zf.namelist():
                return f"it has no {__package__}/__init__.py"
            bad = zf.testzip()
            if bad is not None:
                return f"it is damaged ({bad})"
            for info in infos:
                if info.filename.endswith(".py"):
                    try:
                        compile(zf.read(info), info.filename, "exec", dont_inherit=True)
                    except (SyntaxError, ValueError) as exc:
                        return f"{info.filename} does not compile ({exc})"
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
        return f"the zip could not be read ({exc})"
    return ""


def _install_worker(url: str, tag: str, size: int, addons: str):
    """Runs on a worker thread: download, validate, unpack beside the install.

    Unpacked into a hidden folder in the addons directory itself, so the swap
    on the main thread is two renames on one filesystem. Blender does not
    import a folder with a dot in its name, so it never sees the staging copy.
    No Blender API in here.
    """
    fd, tmp = tempfile.mkstemp(prefix="animatica-", suffix=".zip")
    os.close(fd)
    stage = None
    try:
        try:
            _download(url, pathlib.Path(tmp), size)
        except ValueError as exc:
            print(f"Animatica updater: {exc}")
            raise _Refused("the download did not complete — try again") from exc
        except Exception as exc:            # noqa: BLE001
            raise _Refused(_friendly(exc, "the download")) from exc
        why = validate_zip(tmp)
        if why:
            print(f"Animatica updater: refusing {url}: {why}")
            raise _Refused(f"{tag} is not a valid build — nothing was changed")
        addons_dir = pathlib.Path(addons)
        for old in addons_dir.glob(".animatica-update-*"):
            shutil.rmtree(old, ignore_errors=True)  # from an attempt Blender did not survive
        stage = pathlib.Path(tempfile.mkdtemp(prefix=".animatica-update-", dir=addons_dir))
        with zipfile.ZipFile(tmp) as zf:
            zf.extractall(stage / "new")
        _results.put(("downloaded", (str(stage), tag)))
        stage = None                        # the main thread owns it now
    except _Refused as exc:
        _results.put(("download_failed", str(exc)))
    except Exception as exc:                # noqa: BLE001
        _results.put(("download_failed", _friendly(exc, "the update")))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


# ---------------------------------------------------------------------------
# Swapping
# ---------------------------------------------------------------------------

def _schedule_swap(stage: str, tag: str) -> None:
    def _swap_timer():
        return _swap(stage, tag)
    bpy.app.timers.register(_swap_timer, first_interval=0.0, persistent=True)


def _purge_modules(module: str) -> None:
    import sys

    for name in [n for n in list(sys.modules) if n == module or n.startswith(module + ".")]:
        del sys.modules[name]


def _fail(message: str, stage) -> None:
    _state.update({"error": message, "waiting": False})
    if stage is not None:
        shutil.rmtree(stage, ignore_errors=True)
    _redraw()


def _swap(stage: str, tag: str):
    """Replace the installed addon with the unpacked build, and reload it.

    Runs from a timer rather than inside the operator that asked for it: the
    swap unregisters the operator's own class, and doing that while its
    execute() is still on the stack is asking for trouble.

    The old install is renamed aside, not deleted, until the new one has
    loaded. If the new one will not load, the old one goes back and is enabled
    again. Either way the artist is told through the config file, because the
    copy of this module that draws the banner afterwards is a fresh import.
    """
    import addon_utils

    module = __package__
    stage = pathlib.Path(stage)

    # A generation may have started while we were downloading — in this scene
    # or another. Swapping now would kill it mid-flight. Wait for it.
    if generating_anywhere():
        if not _state["waiting"]:
            _state["waiting"] = True
            _redraw()
        return 2.0
    _state["waiting"] = False

    if is_development_checkout():
        _fail("this is a linked development checkout — update it with git", stage)
        return None

    target = addon_dir()
    new = stage / "new" / module
    backup = stage / "old"
    if not new.is_dir() or not target.is_dir():
        _fail("the update could not be unpacked — nothing was changed", stage)
        return None

    try:
        addon_utils.disable(module, default_set=False)
    except Exception:                       # noqa: BLE001
        traceback.print_exc()
        _try_enable(module)
        _fail("could not unload the current version — nothing was changed", stage)
        return None

    try:
        os.rename(target, backup)
    except OSError:
        traceback.print_exc()
        _try_enable(module)
        _fail("could not move the current version aside — nothing was changed", stage)
        return None
    try:
        os.rename(new, target)
    except OSError:
        traceback.print_exc()
        os.rename(backup, target)
        _try_enable(module)
        _fail("could not put the new version in place — nothing was changed", stage)
        return None

    # Written before enabling, because the new copy reads it in its register().
    _save_config(outcome={"tag": tag, "ok": True, "time": time.time()})
    _purge_modules(module)
    if _try_enable(module):
        shutil.rmtree(stage, ignore_errors=True)
        return None

    # The new version did not load. Put the old one back as it was.
    print(f"Animatica updater: {tag} did not load; restoring the previous version")
    message = f"{tag} would not load, so your previous version is back — restart Blender"
    _save_config(outcome={"tag": tag, "ok": False, "time": time.time(), "message": message})
    try:
        addon_utils.disable(module, default_set=False)
    except Exception:                       # noqa: BLE001
        pass
    _purge_modules(module)
    try:
        shutil.rmtree(target)
        os.rename(backup, target)
    except OSError:
        traceback.print_exc()
        _fail(f"{tag} would not load and the previous version could not be put back "
              f"(it is in {backup}) — restart Blender", None)
        return None
    if not _try_enable(module):
        _fail("the previous version would not load again — restart Blender", stage)
        return None
    shutil.rmtree(stage, ignore_errors=True)
    return None


def _try_enable(module: str) -> bool:
    import addon_utils

    # default_set=False: the preferences entry — and with it the artist's
    # server, token and settings — survives a version that fails to load.
    try:
        addon_utils.enable(module, default_set=False, persistent=True)
    except Exception:                       # noqa: BLE001
        traceback.print_exc()
        return False
    loaded_default, loaded_state = addon_utils.check(module)
    return bool(loaded_state)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class ANIMATICA_OT_check_update(bpy.types.Operator):
    bl_idname = "animatica.check_update"
    bl_label = "Check for Updates"
    bl_description = "Ask GitHub whether a newer build of this addon has been released"

    def execute(self, context):
        from .mmcp_client import OFFLINE_MESSAGE, online_access
        if not online_access():
            self.report({'ERROR'}, OFFLINE_MESSAGE)
            return {'CANCELLED'}
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
        return update_available() and not _state["downloading"] and not _state["waiting"]

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
        from .mmcp_client import OFFLINE_MESSAGE, online_access
        if not online_access():
            self.report({'ERROR'}, OFFLINE_MESSAGE)
            return {'CANCELLED'}
        if generating_anywhere():
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
        addons = str(addon_dir().parent)
        _state.update({"downloading": True, "error": ""})
        threading.Thread(target=_install_worker,
                         args=(_state["url"], _state["tag"], _state["size"], addons),
                         daemon=True).start()
        _start_pump()
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

def _draw_error(layout) -> None:
    err = layout.row()
    err.alert = True
    err.label(text=_state["error"][:90], icon='ERROR')


def draw_banner(layout, context) -> None:
    """The update row for the sidebar: only when there is something to say."""
    if _state["installed"]:
        box = layout.box()
        col = box.column(align=True)
        col.label(text=f"Updated to {_state['installed']}", icon='CHECKMARK')
        col.label(text="Restart Blender to finish", icon='INFO')
        return
    if not update_available():
        # A failed update is worth saying even with nothing left on offer; a
        # failed background check is not — the preferences carry that one.
        if _state["error"] and not _state["checking"] and _state["ok"]:
            _draw_error(layout.box())
        return
    box = layout.box()
    col = box.column(align=True)
    # Two rows rather than one: the sidebar is narrow, and a label sharing a
    # row with a button loses its tail — "v0.6.1-previe…" told nobody which
    # build was on offer.
    col.label(text=f"New version: {_state['tag']}", icon='IMPORT')
    if _state["downloading"] or _state["waiting"]:
        sub = col.row()
        sub.active = False
        sub.label(text="downloading…" if _state["downloading"]
                  else "waiting for the generation to finish…")
    else:
        col.operator("animatica.update", text="Update")
    if _state["error"]:
        _draw_error(box)


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
        if _state["error"]:
            _draw_error(box)
        if _state["notes"]:
            note = box.column(align=True)
            note.active = False
            for line in _state["notes"].splitlines()[:4]:
                if line.strip():
                    note.label(text=line.strip()[:80])
    elif _state["error"]:
        _draw_error(box)
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
    try:
        _restore()
    except Exception:                       # noqa: BLE001 — never let memory stop the addon loading
        traceback.print_exc()


def unregister() -> None:
    try:
        if bpy.app.timers.is_registered(_pump):
            bpy.app.timers.unregister(_pump)
    except Exception:                       # noqa: BLE001
        pass
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
