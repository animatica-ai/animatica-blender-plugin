"""The identity seam — who the host is, and what it can do.

Separate from :mod:`animatica_core.bridge` on purpose. The bridge answers "do
this to the scene"; this module answers "what am I running in, and is this
feature even possible here". Mixing them would put capability branches inside
scene code.

**Ask, never infer.** The 3ds Max port found three capabilities the MotionBuilder
code assumed: no HumanIK, no transport zoom bar, no takes. Blender will answer
differently again. A branch that reads ``if host.key() == "max"`` encodes today's
list; ``if host.has(TAKES)`` encodes the actual question, and a new host answers
it by declaring its own capabilities rather than by editing core.

Capabilities default to **absent**. A host that forgets to declare one gets the
conservative path, not a crash in the middle of a generation.
"""

from __future__ import annotations

import os
import uuid

# The capability vocabulary. Strings rather than an enum so a host can be
# registered from a plugin that predates a new entry without an import error.
TAKES = "takes"                    # named, switchable animation containers
ZOOM_WINDOW = "zoom_window"        # a transport zoom bar distinct from the range
CHARACTER_SYSTEM = "character_system"   # HumanIK or equivalent
CONTROL_RIG = "control_rig"        # a bakeable control rig on top of a character
STORY = "story"                    # a Story/sequencer timeline
LIVE_DRIVE = "live_drive"          # streaming apply implemented for this host
ANIM_LAYERS = "anim_layers"        # native additive animation layers
VIDEO_CAPTURE = "video_capture"    # the host builds the Video to Motion window

_APP_FOLDER = "animatica"

_key: str | None = None
_product_name: str | None = None
_capabilities: frozenset = frozenset()
_plugin_version: str = ""
_app_version: str = ""
_session_id: str = ""

_UNREGISTERED = (
    "no host is registered — animatica_core.host.register() must run at plugin "
    "startup, alongside animatica_core.bridge.register()."
)

# The host application's own name, keyed the way the plugin registers itself.
# Kept here rather than passed to register() so the three shipped plugins need
# no coordinated change; an unknown key falls back to a neutral phrase.
_APP_NAMES = {
    "mobu": "MotionBuilder",
    "max": "3ds Max",
    "blender": "Blender",
    "maya": "Maya",
}

# What the server is told this client is. Spelled out rather than reusing the
# folder key: the key is ours and short, this one goes over the wire and into
# server-side metrics, so it stays readable and stable independently. An
# unmapped key travels as itself — a new host reports something rather than
# nothing.
_CLIENT_IDS = {
    "mobu": "motionbuilder",
    "max": "3dsmax",
    "blender": "blender",
    "maya": "maya",
}


def register(*, key: str, product_name: str, capabilities=(),
             plugin_version: str = "", app_version: str = "") -> None:
    """Declare the host. Called once, at plugin startup.

    *key* is the short, stable, filesystem-safe identifier (``"mobu"``,
    ``"max"``, ``"maya"``, ``"blender"``) — it names the per-host settings
    folder, so changing it later strands a user's preferences.

    *plugin_version* and *app_version* are optional: they only decorate the
    request headers, so a plugin that predates them must keep working.
    """
    global _key, _product_name, _capabilities, _plugin_version, _app_version
    if not key or not str(key).isidentifier():
        raise ValueError(
            f"host key must be a plain identifier (it names a folder); got {key!r}")
    _key = str(key)
    _product_name = str(product_name)
    _capabilities = frozenset(capabilities or ())
    _plugin_version = str(plugin_version or "").strip()
    _app_version = str(app_version or "").strip()


def unregister() -> None:
    """For tests and teardown."""
    global _key, _product_name, _capabilities, _plugin_version, _app_version
    global _session_id
    _key = _product_name = None
    _capabilities = frozenset()
    _plugin_version = _app_version = _session_id = ""


def is_registered() -> bool:
    return _key is not None


def key() -> str:
    if _key is None:
        raise RuntimeError(_UNREGISTERED)
    return _key


def product_name() -> str:
    if _product_name is None:
        raise RuntimeError(_UNREGISTERED)
    return _product_name


def app_name() -> str:
    """Name of the host *application*, for UI text that talks about the program.

    ``product_name()`` is the plugin ("Animatica for MotionBuilder");
    this is the program it runs inside ("MotionBuilder"). Shared UI needs
    the latter -- "Open Animatica on MotionBuilder startup" reads as a
    ported label in 3ds Max, and interpolating the product name there
    would read "on Animatica for MotionBuilder startup".

    Never raises: a label is not worth a crash, and the standalone widget
    preview builds sections with no host registered.
    """
    return _APP_NAMES.get(_key or "", "your host application")


def has(capability: str) -> bool:
    """Does this host support *capability*? Absent ⇒ False, always."""
    return capability in _capabilities


def capabilities() -> frozenset:
    return _capabilities


# ---------------------------------------------------------------------------
# who the server is talking to — headers on generation requests
# ---------------------------------------------------------------------------

def client_id() -> str | None:
    """The wire name of this client, or ``None`` when nothing is registered."""
    if _key is None:
        return None
    return _CLIENT_IDS.get(_key, _key)


def plugin_version() -> str:
    """Version of the Animatica plugin; ``""`` when it was not declared."""
    return _plugin_version


def app_version() -> str:
    """Version of the host application; ``""`` when it was not declared."""
    return _app_version


def session_id() -> str:
    """Stable id for this *plugin* session — one DCC process, one id.

    Minted on first use and kept until :func:`unregister`, so every request
    from one run of the plugin carries the same id. Unrelated to
    ``stream_client.session_id``, which the streaming server hands out per
    stream. ``""`` when no host is registered.
    """
    global _session_id
    if _key is None:
        return ""
    if not _session_id:
        _session_id = str(uuid.uuid4())
    return _session_id


def client_headers() -> dict:
    """Identity headers for the requests that start a generation.

    Empty when no host is registered — a caller outside a plugin sends
    nothing rather than half an identity. Never raises: telling the server
    who we are is not worth failing a generation over.
    """
    try:
        cid = client_id()
        if cid is None:
            return {}
        headers = {"X-Animatica-Client": cid,
                   "X-Animatica-Session-Id": session_id()}
        for name, value in (("X-Animatica-Client-Version", _plugin_version),
                            ("X-Animatica-Host-Version", _app_version)):
            # A newline in a header value makes http.client reject the whole
            # request; dropping one version is cheaper than losing the call.
            if value and "\r" not in value and "\n" not in value:
                headers[name] = value
        return headers
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# user data — shared auth, per-host settings
# ---------------------------------------------------------------------------

def _app_root() -> str:
    """``%APPDATA%\\animatica`` on Windows, ``~/animatica`` elsewhere."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, _APP_FOLDER)


def shared_dir() -> str:
    """Data shared across every host on this machine.

    The login and the server URL live here: signing in once should cover every
    DCC, because it is one account against one server.
    """
    return _app_root()


def data_dir() -> str:
    """This host's own data.

    Namespace, modes, window state and the prompt cache are per-host — sharing
    them would have two DCCs overwriting each other's UI state, which is the
    failure the split exists to prevent.
    """
    return os.path.join(_app_root(), key())


def cache_root() -> str:
    """``%LOCALAPPDATA%\\animatica\\<host>`` — regenerable data only.

    Kept apart from :func:`data_dir` so a user can delete it without losing
    settings, and so it does not roam.
    """
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            or os.path.expanduser("~"))
    return os.path.join(base, _APP_FOLDER, key())


def legacy_dirs() -> list[str]:
    """Pre-split folders **this host** may migrate its own settings from.

    Only this host's own history. An earlier version also listed the other
    hosts' folders as fallbacks, which sounded helpful and was not: running the
    3ds Max plugin then inherited MotionBuilder's UI state -- including
    ``animation_mode: "new_take"`` and a take name, for a host that has no takes
    at all. Caught by running the migration against real data, which is the only
    place it would ever have shown up.

    The account is a different matter -- see :func:`legacy_shared_dirs`.
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return [os.path.join(base, f"animatica_to_{key()}"),
            os.path.join(base, f"pantomim_to_{key()}")]


def legacy_shared_dirs() -> list[str]:
    """Pre-split folders the **account** may migrate from, any host.

    Signing in is account-level, so a token written by MotionBuilder is a
    perfectly good token for 3ds Max -- that is the whole point of sharing it.
    UI state is not, which is why the two lists differ.

    This host's own folder comes first so its answer wins when several exist.
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    here = key() if is_registered() else ""
    names, seen = [], set()
    for name in ([f"animatica_to_{here}"] if here else []) + [
            "animatica_to_mobu", "animatica_to_max", "animatica_to_maya",
            "pantomim_to_mobu"]:
        if name not in seen:
            seen.add(name)
            names.append(name)
    return [os.path.join(base, n) for n in names]
