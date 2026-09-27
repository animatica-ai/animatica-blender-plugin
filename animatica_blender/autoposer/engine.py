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


#: Rough download sizes, for the button that fetches them: said up front, because
#: nothing is fetched until someone presses it (unless they opted in to automatic).
RUNTIME_MB = 75
MODEL_MB = 150


def online() -> bool:
    """Blender's "Allow Online Access". Nothing here touches the network without it."""
    from ..mmcp_client import online_access
    return online_access()


def offline_message() -> str:
    from ..mmcp_client import OFFLINE_MESSAGE
    return OFFLINE_MESSAGE


def runtime_needs_network() -> bool:
    """False when this build ships the onnxruntime wheels: installing is then an unzip."""
    return apr.ortsetup.bundled_wheels() is None


def auto_setup() -> bool:
    """Whether the artist opted in to fetching the runtime and model unasked."""
    try:
        return bool(prefs().auto_install_runtime)
    except (AttributeError, KeyError):
        return False


def download_mb() -> int:
    """What pressing Download Autoposer would fetch right now, in MB."""
    st = status()
    return (0 if st["runtime"] else RUNTIME_MB) + (0 if st["model"] else MODEL_MB)


def download_label() -> str:
    return f"Download Autoposer (≈{download_mb() or RUNTIME_MB + MODEL_MB} MB)"


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
        # Empty means the revision this build is pinned to (and has the hashes of).
        pinned = apr.bundle.HF_REVISION
        rev = p.hf_revision.strip()
        if rev in _UNPINNED_REVISIONS:
            rev = pinned            # an older build saved "main"; see migrate_prefs()
        rev = rev or pinned
        src = f"hf://{repo}" + (f"/{sub}" if sub else "") + (f"@{rev}" if rev != pinned else "")
        return src, (p.hf_token.strip() or None)
    return None, None


#: Revisions that meant "whatever the repo has now". Earlier previews saved "main" as the
#: default, which left those installs unpinned — and, once the pinned commit moved on, stuck
#: with a model the build no longer vouches for. They mean the pinned commit now.
_UNPINNED_REVISIONS = ("main", "master", "HEAD")


def migrate_prefs() -> None:
    """Clear a saved "main" revision, so the preference shows what is really used. Main thread."""
    try:
        p = prefs()
        if p.hf_revision.strip() in _UNPINNED_REVISIONS:
            p.hf_revision = ""
    except (AttributeError, KeyError, TypeError):
        pass


# ---------------------------------------------------------------------------
# Is the cached model the one this build wants?
#
# Hashing the model is ~150 MB through sha256, about a second and a half. It is
# done once per set of files — keyed by their sizes and modification times —
# and never from a panel: `status()` only does the cheap checks (is it there,
# does its meta.json name the pinned hashes, are the sizes right) and hands the
# hashing to a worker. A cache that fails any of it counts as not downloaded,
# so the Download button is offered instead of every solve failing.
# ---------------------------------------------------------------------------

#: {(folder, ((name, size, mtime_ns), ...)): verified?}
_VERIFIED: dict = {}
_VERIFY_LOCK = threading.Lock()
_VERIFYING = {"running": False}

#: What the panels say about a cached model that is there but will not do.
MODEL_NOTES = {
    "stale": "a newer Autoposer model is available",
    "bad": "the downloaded Autoposer model is damaged",
}


def _signature(path):
    sig = []
    for name in apr.bundle.FILES:
        try:
            st = os.stat(os.path.join(str(path), name))
        except OSError:
            return None
        sig.append((name, st.st_size, st.st_mtime_ns))
    return (str(path), tuple(sig))


def _mark_verified(bundle) -> None:
    sig = _signature(bundle.path)
    if sig is not None:
        _VERIFIED[sig] = True


def _uses_cache(src) -> bool:
    """Is `src` served from the download cache (rather than a folder named directly)?"""
    if src and not str(src).startswith("hf://"):
        return False
    return not (src is None and os.environ.get(apr.bundle.ENV_BUNDLE))


def model_check(d, src, *, hash_now: bool = False):
    """The cached model, as `(state, bundle)`.

    state is "ok" (verified), "unverified" (looks right, not hashed yet),
    "missing", "stale" (a model from another commit — an older build's) or
    "bad" (damaged). With `hash_now`, an unverified model is hashed here —
    once; the answer is remembered for as long as the files do not change.
    Never call that from a draw.
    """
    try:
        b = apr.bundle.cached_bundle(d, verify=False)
    except Exception:                                          # noqa: BLE001
        b = None
    if b is None:
        return "missing", None
    pinned = need_source = None
    if src and str(src).startswith("hf://"):
        try:
            repo, sub, rev = apr.bundle.parse_hf(src)
        except apr.BundleError:
            return "stale", b
        pinned = apr.bundle.pinned_hashes(repo, sub, rev)
        need_source = None if pinned else str(src)
    files = b.meta.get("files")
    if not isinstance(files, dict):
        return "bad", b
    if need_source and b.meta.get("source") != need_source:
        return "stale", b
    if pinned:
        for name in ("poser.onnx", "ik.onnx"):
            if (files.get(name) or {}).get("sha256") != pinned[name]:
                return "stale", b
    for name, info in files.items():
        want = (info or {}).get("bytes") if isinstance(info, dict) else None
        if isinstance(want, int):
            try:
                if os.path.getsize(os.path.join(str(b.path), name)) != want:
                    return "bad", b
            except OSError:
                return "bad", b
    sig = _signature(b.path)
    if sig is None:
        return "bad", b
    known = _VERIFIED.get(sig)
    if known is None and hash_now:
        with _VERIFY_LOCK:          # a second caller waits for the first one's answer
            known = _VERIFIED.get(sig)
            if known is None:
                try:
                    known = bool(b.verify(pinned))
                except OSError:
                    known = False
                _VERIFIED[sig] = known
    if known is None:
        return "unverified", b
    return ("ok" if known else "bad"), b


def _verify_async(d, src) -> None:
    """Hash an unverified cache on a worker, so the panel can say what it found."""
    if _VERIFYING["running"]:
        return
    _VERIFYING["running"] = True

    def _work():
        try:
            model_check(d, src, hash_now=True)
        finally:
            _post(_after_verify)
            _VERIFYING["running"] = False

    threading.Thread(target=_work, daemon=True).start()
    _keep_pumping()


def _after_verify():
    status(refresh=True)
    _redraw()
    return None


def model_problem(state: str) -> str:
    """The sentence for a model that cannot be used, and what to do about it."""
    if state == "stale":
        return f"{MODEL_NOTES['stale']} — press {download_label()}"
    if state == "bad":
        return f"{MODEL_NOTES['bad']} — press {download_label()} to fetch it again"
    return f"the Autoposer is not downloaded yet — press {download_label()}"


def _offline_problem(state: str) -> str:
    if state == "stale":
        return f"{MODEL_NOTES['stale']}, but it needs a download. {offline_message()}"
    if state == "bad":
        return f"{MODEL_NOTES['bad']} and needs downloading again. {offline_message()}"
    return f"the Autoposer needs a one-off download. {offline_message()}"


# ---------------------------------------------------------------------------
# Handing results to the main thread (see mmcp_client.post_to_main)
# ---------------------------------------------------------------------------

def _post(fn) -> None:
    from ..mmcp_client import post_to_main
    post_to_main(fn)


def _busy() -> bool:
    return bool(_INSTALL["running"] or _FETCH["running"] or _VERIFYING["running"])


def _keep_pumping() -> None:
    """Main thread: keep collecting the workers' results while any is running."""
    from ..mmcp_client import pump_while
    pump_while(_busy)


def _redraw() -> None:
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()


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
    src, _token = source()
    state = "external"
    if _uses_cache(src):
        state, cached = model_check(d, src)
        if state == "unverified":
            _verify_async(d, src)       # hashed off the main thread, once
        elif state not in ("ok",):
            cached = None               # there, but not a model this build will run
    else:
        try:
            cached = apr.bundle.cached_bundle(d, verify=False)
        except Exception:                                      # noqa: BLE001
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
        # Why a model that is on disk does not count: a newer one is pinned, or it is damaged.
        "model_state": state if b is None or b is cached else "external",
        "model_note": MODEL_NOTES.get(state, "") if b is None else "",
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
                if runtime_needs_network() and not online():
                    raise NotReady("the inference runtime is not installed, and it needs a "
                                   f"download. {offline_message()}")
                raise NotReady("the inference runtime is not installed — press "
                               f"{download_label()}")
            if runtime_needs_network() and not online():
                raise NotReady(f"the inference runtime is not installed. {offline_message()}")
            try:
                apr.ortsetup.install(cache_dir=d)
            except Exception as e:                             # noqa: BLE001
                raise NotReady(f"could not install the inference runtime: {e}") from e
            if not apr.ortsetup.activate(cache_dir=d):
                raise NotReady("the inference runtime installed but will not import")
        if _uses_cache(src):
            state, bundle = model_check(d, src, hash_now=True)    # once per set of files
            if state != "ok":
                _reset_status()
                if not allow_download:
                    raise NotReady(model_problem(state) if online() else _offline_problem(state))
                if not online():
                    raise NotReady(_offline_problem(state))
                try:
                    bundle = _download_model(d, src, token, on_progress, force=True)
                except apr.BundleError as e:
                    raise NotReady(str(e)) from e
        else:
            try:
                bundle = apr.resolve(src, cache_dir=d, token=token, allow_download=False)
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
        # sys.path is touched on the main thread, where everything else that
        # imports is, and a redraw picks the new state up. Posted before
        # saying "done", so the main thread is still listening for it.
        _post(_after_install)
        _INSTALL["running"] = False


def _after_install():
    apr.ortsetup.activate(cache_dir=data_dir())
    status(refresh=True)
    ensure_model()          # the runtime is only half of what a solve needs
    preload_async()
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


def ensure_runtime(*, force: bool = False) -> bool:
    """Start the one-off runtime install if it is missing. Returns True if it
    is already usable.

    Called from the Download Autoposer button, and unasked only for someone
    who opted in to automatic setup. Never without Blender's online access,
    unless this build ships the wheels (then it is an unzip).
    """
    d = data_dir()
    if apr.ortsetup.activate(cache_dir=d):
        return True
    if _INSTALL["running"]:
        return False
    if _INSTALL["error"] and not force:
        return False        # said its piece; the preferences button can retry
    if runtime_needs_network() and not online():
        return False        # not an error: it starts once online access is allowed
    _INSTALL.update({"running": True, "done": False, "error": ""})
    threading.Thread(target=_install_worker, args=(d,), daemon=True).start()
    _keep_pumping()
    return False


#: The one-off model fetch, tracked the same way and for the same reason: it
#: is ~150 MB and the artist should be able to see that it is happening.
_FETCH = {"running": False, "error": "", "done": 0, "total": 0}


def fetch_state() -> dict:
    return dict(_FETCH)


def fetch_percent() -> float:
    total = _FETCH["total"]
    return (100.0 * _FETCH["done"] / total) if total else 0.0


def _download_model(cache_dir, src, token, on_progress=None, *, force=False):
    """Fetch the model into the cache and return it. Worker thread (or a caller that insists).

    `force` downloads even over a cached copy — the case for one that is from another commit
    or damaged, and for Re-download. The fresh copy was hashed as it arrived, so it is
    remembered as verified.
    """
    if src and str(src).startswith("hf://"):
        if not force:
            state, b = model_check(cache_dir, src, hash_now=True)
            if state == "ok":
                return b
        repo, sub, rev = apr.bundle.parse_hf(src)
        b = apr.bundle.download_hf(repo, cache_dir, revision=rev, subfolder=sub, token=token,
                                   on_progress=on_progress)
        apr.bundle._stamp_source(b, str(src))
    else:
        b = apr.resolve(src, cache_dir=cache_dir, token=token, allow_download=True,
                        on_progress=on_progress)
    _mark_verified(b)
    return b


def _fetch_worker(cache_dir, src, token, force=False):
    def on_progress(_name, done, total):
        _FETCH["done"], _FETCH["total"] = done, total

    try:
        _download_model(cache_dir, src, token, on_progress, force=force)
        _FETCH["fetched"] = True
    except Exception as exc:                                   # noqa: BLE001
        _FETCH["error"] = str(exc)
    finally:
        _post(_after_fetch)          # before "done", so the main thread is still listening
        _FETCH["running"] = False


def _reset_status() -> None:
    global _STATUS_CACHE
    _STATUS_CACHE = (0.0, None)


def _after_fetch():
    if _FETCH.pop("fetched", False):
        unload()                    # the next solve picks the new model up
    status(refresh=True)
    preload_async()
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


def ensure_model(*, force: bool = False, redownload: bool = False) -> bool:
    """Start the one-off model download if it is missing. True if it is here.

    Called after the runtime install and from the Download Autoposer button;
    unasked only for someone who opted in to automatic setup, and never
    without Blender's online access.

    A folder the artist pointed at is left alone: LOCAL means they have said
    where it is, and downloading over that would be presumptuous.
    """
    p = prefs()
    if getattr(p, "model_source", "HF") == "LOCAL":
        return bool(status()["model"])
    st = status(refresh=True)       # cheap; a stale or damaged cache is "not here"
    if st["model"] and not redownload:
        return True
    if _FETCH["running"]:
        return False
    if _FETCH["error"] and not force:
        return False        # said its piece; the preferences button can retry
    src, token = source()
    if not src and not redownload:
        return False        # "Already installed": nothing is fetched unless asked
    if not online():
        return False
    # Over a cached copy that is stale or damaged — or when asked to — fetch
    # regardless of what is there.
    force_fetch = redownload or st.get("model_state") in ("stale", "bad")
    _FETCH.update({"running": True, "error": "", "done": 0, "total": 0})
    threading.Thread(target=_fetch_worker, args=(data_dir(), src, token, force_fetch),
                     daemon=True).start()
    _keep_pumping()
    return False


_PRELOAD = {"running": False}


def preload_async() -> None:
    """Bring the engine up in the background, before anyone reaches for it.

    Loading is two onnx graphs and a self-test — about a second and a half. On
    the first drag that lands as a freeze in the middle of the gesture, which
    is the worst possible moment for it. Doing it on a worker thread when the
    pieces are already on the machine means the first drag is as fast as the
    tenth.
    """
    if _ENGINE is not None or _PRELOAD["running"]:
        return
    if not apr.ortsetup.activate(cache_dir=data_dir()):
        return                      # runtime not there yet; its install will call back
    _PRELOAD["running"] = True

    def _work():
        try:
            load(allow_install=False, allow_download=False)
        except Exception:                                      # noqa: BLE001
            pass            # the first real solve reports it properly
        finally:
            _PRELOAD["running"] = False

    threading.Thread(target=_work, daemon=True).start()


def download_all() -> None:
    """What the Download Autoposer button does: the runtime, then the model.

    The model follows the runtime on its own (``_after_install``), so this only
    has to start whichever half is missing, forgiving any earlier failure.
    """
    _INSTALL["error"] = ""
    _FETCH["error"] = ""
    if ensure_runtime(force=True):
        ensure_model(force=True)


def get():
    """The engine, loading it if it is not up yet.

    Nothing is downloaded unasked: the runtime and the model come from the
    Download Autoposer button, or by themselves only for someone who opted in
    to automatic setup. Until then the caller gets a sentence saying which.
    """
    if _ENGINE is not None:
        return _ENGINE
    st = status()
    busy = _INSTALL["running"] or _FETCH["running"]
    needs_network = (not st["runtime"] and runtime_needs_network()) or not st["model"]
    if needs_network and not busy:
        state = st.get("model_state", "missing") if not st["model"] else "missing"
        if not online():
            raise NotReady(_offline_problem(state))
        if not auto_setup() and not (_INSTALL["error"] or _FETCH["error"]):
            raise NotReady(model_problem(state))
    if not ensure_runtime():
        if _INSTALL["error"]:
            raise NotReady(f"the inference runtime would not install: {_INSTALL['error']}")
        raise NotReady("fetching the inference runtime — one-off, about 75 MB")
    if not ensure_model():
        if _FETCH["error"]:
            raise NotReady(f"the model would not download: {_FETCH['error']}")
        pct = fetch_percent()
        raise NotReady("fetching the poser model — one-off, about 150 MB"
                       + (f" ({pct:.0f}%)" if pct else ""))
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
