# SPDX-License-Identifier: Apache-2.0
"""The poser, held open for the session — one place that knows whether it can solve.

Loading is lazy and never happens implicitly on a background thread: a solve either has the
engine or raises `NotReady` with a sentence the panel prints verbatim. The preferences page
does the provisioning (install the runtime, fetch the model) with a progress bar and its own
error handling; everything else just calls `get()`.
"""
from __future__ import annotations

import os
import threading
import time

import bpy

from .vendor import autoposer_runtime as apr

_ENGINE = None
_META = {}
_SKEL = None
_LOCK = threading.Lock()


class NotReady(RuntimeError):
    """The engine cannot solve yet, and the message says what to do about it."""


#: The addon this package now lives inside. There is one AddonPreferences per
#: addon, and this is no longer an addon of its own — its settings are fields
#: on Animatica's preferences (see ``prefs.PROPERTIES``).
_HOST_ADDON = __package__.split(".")[0]


def prefs():
    return bpy.context.preferences.addons[_HOST_ADDON].preferences


def data_dir() -> str:
    """Where the model and the runtime live. The preference wins; otherwise Blender's own user
    data directory, under the addon's name, so uninstalling can take it with it."""
    p = prefs()
    if p.cache_dir:
        return bpy.path.abspath(p.cache_dir)
    return bpy.utils.user_resource("DATAFILES", path="animatica_autoposer", create=True)


def bundled_model():
    """A model directory shipped inside this build, or ``None``.

    A test build can carry the weights so a machine needs no token and no
    download; a release does not, and this returns ``None`` there, so the code
    path is the same either way.
    """
    import pathlib

    d = pathlib.Path(__file__).resolve().parent / "model"
    return str(d) if (d / "meta.json").is_file() else None


def source():
    """What `resolve()` should be pointed at, from the preferences.

    Returned as (source, token). `None` means "work it out": the cache, then whatever the
    environment names — which is how a studio can deploy a prepared cache and have every
    workstation just use it.
    """
    p = prefs()
    if p.model_source == "LOCAL":
        return (bpy.path.abspath(p.local_path) or None), None
    # A model inside the addon wins over fetching one: it is the build's own,
    # it needs no account, and a pointed-at folder is the only thing that
    # should override it — which is the branch above.
    shipped = bundled_model()
    if shipped:
        return shipped, None
    if p.model_source == "HF":
        repo = p.hf_repo.strip() or "Animatica-ai/autoposer"
        sub = p.hf_subfolder.strip()
        rev = p.hf_revision.strip() or "main"
        src = f"hf://{repo}" + (f"/{sub}" if sub else "") + (f"@{rev}" if rev != "main" else "")
        return src, (p.hf_token.strip() or None)
    return None, None


#: `status()` is called from panel `draw()`, which Blender runs on every redraw — so it must
#: cost nothing. It reads a little json; this keeps even that off the redraw path.
_STATUS_TTL = 1.0
_STATUS_CACHE = (0.0, None)


def status(refresh: bool = False) -> dict:
    """Everything the panels show. Cheap by construction, and cached for a second.

    NOTHING here may hash, download or load: it runs inside `draw()`. It used to call
    `cached_bundle()`, which verifies — 150 MB through sha256 on every redraw, one core pinned,
    Blender's main loop down to ~1 Hz. Integrity is checked when a model is downloaded and when
    the engine loads it; a redraw only needs to know whether a file is there.
    """
    global _STATUS_CACHE
    now = time.monotonic()
    if not refresh and _STATUS_CACHE[1] is not None and now - _STATUS_CACHE[0] < _STATUS_TTL:
        return _STATUS_CACHE[1]
    p = prefs()
    d = data_dir()
    runtime_ok = apr.ortsetup.activate(cache_dir=d)
    try:
        cached = apr.bundle.cached_bundle(d, verify=False)
    except Exception:                                          # noqa: BLE001
        cached = None
    local = None
    if p.model_source == "LOCAL" and p.local_path:
        try:
            local = apr.bundle.Bundle(bpy.path.abspath(p.local_path))
        except Exception:                                      # noqa: BLE001
            local = None
    shipped = None
    if local is None:
        path = bundled_model()
        if path:
            try:
                shipped = apr.bundle.Bundle(path)
            except Exception:                                  # noqa: BLE001
                shipped = None
    b = local or shipped or cached
    out = {
        "runtime": runtime_ok,
        "runtime_dir": str(apr.ortsetup.default_target(d)),
        "model": b is not None,
        "model_dir": str(b.path) if b else "",
        # Where this model came from, most specific first: a build that ships
        # one says so, since that is what a tester needs to know.
        "model_source": (("shipped with the addon" if shipped else "")
                         or ("folder" if local else "")
                         or (b.meta.get("source") if b else "")),
        "step": (b.meta.get("step") if b else None),
        "loaded": _ENGINE is not None,
        "data_dir": d,
    }
    _STATUS_CACHE = (now, out)
    return out


def load(*, allow_install=True, allow_download=True, on_progress=None):
    """Bring the engine up. Raises `NotReady` with a message written for a user."""
    global _ENGINE, _META
    with _LOCK:
        if _ENGINE is not None:
            return _ENGINE
        d = data_dir()
        src, token = source()
        if not apr.ortsetup.activate(cache_dir=d):
            if not allow_install:
                raise NotReady("the inference runtime is not installed — open the addon "
                               "preferences and press Install Runtime")
            try:
                apr.ortsetup.install(cache_dir=d)
            except Exception as e:                             # noqa: BLE001
                raise NotReady(f"could not install the inference runtime: {e}") from e
            if not apr.ortsetup.activate(cache_dir=d):
                raise NotReady("the inference runtime installed but will not import")
        try:
            bundle = apr.resolve(src, cache_dir=d, token=token,
                                 allow_download=allow_download, on_progress=on_progress)
        except apr.BundleError as e:
            raise NotReady(str(e)) from e
        try:
            _ENGINE = apr.Engine(bundle, threads=prefs().threads)
            ok, err = _ENGINE.selftest()
        except Exception as e:                                 # noqa: BLE001
            _ENGINE = None
            raise NotReady(f"the model would not load: {e}") from e
        if not ok:
            _ENGINE = None
            raise NotReady(
                f"the IK graph does not reproduce its own answer on this machine ({err:.2f} cm "
                "out) — this is an onnxruntime problem, and the poses would be wrong")
        _META = dict(bundle.meta)
        return _ENGINE


#: The one-off runtime fetch, tracked so the UI can say it is happening. It runs
#: on a worker thread: it is tens of megabytes, and Blender's main thread is
#: where the artist is.
_INSTALL = {"running": False, "done": False, "error": ""}


def install_state() -> dict:
    return dict(_INSTALL)


def _install_worker(cache_dir):
    try:
        apr.ortsetup.install(cache_dir=cache_dir)
        _INSTALL["done"] = True
    except Exception as exc:                                   # noqa: BLE001
        _INSTALL["error"] = str(exc)
    finally:
        _INSTALL["running"] = False
        # sys.path is touched on the main thread, where everything else that
        # imports is, and a redraw picks the new state up.
        bpy.app.timers.register(_after_install, first_interval=0.0)


def _after_install():
    apr.ortsetup.activate(cache_dir=data_dir())
    status(refresh=True)
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


def ensure_runtime(*, force: bool = False) -> bool:
    """Start the one-off runtime install if it is missing. Returns True if it
    is already usable.

    Nothing about this needs a decision from the artist: the runtime is a
    dependency of the addon, not a choice within it, and asking someone to
    press Install Runtime before the first pose can be solved is a step that
    exists only because the download has to happen somewhere. It happens
    here, once, in the background.
    """
    d = data_dir()
    if apr.ortsetup.activate(cache_dir=d):
        return True
    if _INSTALL["running"]:
        return False
    if _INSTALL["error"] and not force:
        return False        # said its piece; the preferences button can retry
    _INSTALL.update({"running": True, "done": False, "error": ""})
    threading.Thread(target=_install_worker, args=(d,), daemon=True).start()
    return False


def get():
    """The engine, loading it if it is not up yet.

    The runtime installs itself on first need; the model still does not, since
    it is account-gated and only the artist has the token.
    """
    if _ENGINE is not None:
        return _ENGINE
    if not ensure_runtime():
        if _INSTALL["error"]:
            raise NotReady(f"the inference runtime would not install: {_INSTALL['error']}")
        raise NotReady("fetching the inference runtime — one-off, about 75 MB")
    return load(allow_install=False, allow_download=False)


def unload():
    global _ENGINE, _META, _SKEL, _STATUS_CACHE
    with _LOCK:
        _ENGINE, _META, _SKEL = None, {}, None
        _STATUS_CACHE = (0.0, None)


def meta() -> dict:
    """The bundle's metadata — skeleton, conditioning stats, checkpoint step.

    Available WITHOUT loading the graphs: it is a json file next to them, and the rig code
    needs joint names and parents long before anything is solved. Reading it lazily is not a
    nicety — returning an empty dict here makes the rig silently see a skeleton with no joints.
    """
    global _META
    if _META:
        return dict(_META)
    src = source()[0]
    try:
        # no hashing here either: this is reached from rig code that runs while the UI is up
        b = (apr.bundle.Bundle(src) if src and not str(src).startswith("hf://")
             else apr.bundle.cached_bundle(data_dir(), verify=False))
    except Exception:                                      # noqa: BLE001
        return {}
    if b is None:
        return {}
    _META = dict(b.meta)
    return dict(_META)


def skeleton():
    """The skeleton, as numpy geometry — no onnxruntime, no session, no model load."""
    global _SKEL
    if _SKEL is None:
        m = meta()
        if not m:
            raise NotReady("no model on this machine yet — open the addon preferences")
        _SKEL = apr.Skeleton(m)
    return _SKEL


def joint_names():
    e = _ENGINE
    return list(e.skel.names) if e is not None else []


def env_hint() -> str:
    """Named so a support conversation can start from what the machine actually has."""
    bits = []
    for var in (apr.bundle.ENV_BUNDLE, apr.bundle.ENV_HF, "HF_TOKEN"):
        if os.environ.get(var):
            bits.append(var)
    return ", ".join(bits)
