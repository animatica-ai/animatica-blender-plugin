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

# The capability vocabulary. Strings rather than an enum so a host can be
# registered from a plugin that predates a new entry without an import error.
TAKES = "takes"                    # named, switchable animation containers
ZOOM_WINDOW = "zoom_window"        # a transport zoom bar distinct from the range
CHARACTER_SYSTEM = "character_system"   # HumanIK or equivalent
CONTROL_RIG = "control_rig"        # a bakeable control rig on top of a character
STORY = "story"                    # a Story/sequencer timeline
LIVE_DRIVE = "live_drive"          # streaming apply implemented for this host
ANIM_LAYERS = "anim_layers"        # native additive animation layers

_APP_FOLDER = "animatica"

_key: str | None = None
_product_name: str | None = None
_capabilities: frozenset = frozenset()

_UNREGISTERED = (
    "no host is registered — animatica_core.host.register() must run at plugin "
    "startup, alongside animatica_core.bridge.register()."
)


def register(*, key: str, product_name: str, capabilities=()) -> None:
    """Declare the host. Called once, at plugin startup.

    *key* is the short, stable, filesystem-safe identifier (``"mobu"``,
    ``"max"``, ``"maya"``, ``"blender"``) — it names the per-host settings
    folder, so changing it later strands a user's preferences.
    """
    global _key, _product_name, _capabilities
    if not key or not str(key).isidentifier():
        raise ValueError(
            f"host key must be a plain identifier (it names a folder); got {key!r}")
    _key = str(key)
    _product_name = str(product_name)
    _capabilities = frozenset(capabilities or ())


def unregister() -> None:
    """For tests and teardown."""
    global _key, _product_name, _capabilities
    _key = _product_name = None
    _capabilities = frozenset()


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


def has(capability: str) -> bool:
    """Does this host support *capability*? Absent ⇒ False, always."""
    return capability in _capabilities


def capabilities() -> frozenset:
    return _capabilities


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
