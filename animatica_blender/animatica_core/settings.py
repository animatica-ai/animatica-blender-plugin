"""JSON settings persistence for Animatica to MotionBuilder.

DCC-agnostic — no pyfbsdk imports.  Stored at:
  %APPDATA%\\animatica_core\\settings.json  (Windows)
  ~/animatica_core/settings.json           (fallback)

Pre-rename installs stored settings under ``pantomim_to_mobu``; ``load()``
carries those over once (see ``_migrate_legacy_settings``).
"""

import json
import os
import shutil
import tempfile


# Keys that belong to the account rather than to one DCC. Deliberately a short,
# explicit list: anything not named here stays per-host, so the default for a
# new setting is the safe one.
SHARED_KEYS = frozenset({"server_url", "use_cloud"})

_SHARED_FILE = "shared.json"
_SETTINGS_FILE = "settings.json"


def settings_dir() -> str:
    """This host's own settings folder."""
    from . import host
    return host.data_dir()


def settings_path() -> str:
    return os.path.join(settings_dir(), _SETTINGS_FILE)


def shared_dir() -> str:
    """The folder shared by every host on this machine."""
    from . import host
    return host.shared_dir()


def shared_path() -> str:
    return os.path.join(shared_dir(), _SHARED_FILE)


def legacy_settings_paths() -> list:
    """Pre-split settings files for THIS host only, newest convention first."""
    from . import host
    return [os.path.join(d, _SETTINGS_FILE) for d in host.legacy_dirs()]


def legacy_shared_paths() -> list:
    """Pre-split settings files any host may have written the account into."""
    from . import host
    return [os.path.join(d, _SETTINGS_FILE) for d in host.legacy_shared_dirs()]


def _migrate_legacy_file(legacy_path: str, new_path: str, new_dir: str) -> None:
    """One-time carry-over of a pre-rename (Pantomim) file into the new location.

    No-ops when *new_path* already exists (so it never clobbers newer data with
    stale data) or when *legacy_path* is absent (fresh install). The copy is
    atomic — written to a temp file in *new_dir* and ``os.replace``-d — so an
    interrupted copy can't leave a corrupt destination that would then block
    re-migration. Any failure is swallowed: a migration problem must not break
    startup (the caller falls back to defaults). Shared by the settings and
    auth carry-overs (both live under the same renamed APPDATA folder).
    """
    if os.path.exists(new_path) or not os.path.exists(legacy_path):
        return
    try:
        os.makedirs(new_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=new_dir, suffix=".tmp")
        os.close(fd)
        shutil.copyfile(legacy_path, tmp)
        os.replace(tmp, new_path)
    except OSError:
        try:
            os.unlink(tmp)
        except (OSError, NameError):
            pass


def _migrate_legacy_settings() -> None:
    """Carry a pre-split settings file forward once, and split it.

    Runs at most once per host: it no-ops as soon as the new file exists. The
    shared keys are lifted out into ``shared.json`` only if that file does not
    already hold them, so the first DCC to migrate seeds the account settings
    and the second does not clobber them.
    """
    target = settings_path()
    if os.path.exists(target):
        return
    _migrate_shared_only()
    for legacy in legacy_settings_paths():
        if not os.path.exists(legacy):
            continue
        _migrate_legacy_file(legacy, target, settings_dir())
        try:
            with open(target, "r", encoding="utf-8") as fh:
                carried = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        _seed_shared_from(carried)
        return


def _seed_shared_from(carried: dict) -> None:
    """Lift the account keys out of a carried-over file, once.

    Never overwrites an existing shared file: the first host to migrate seeds
    the account, and the second must not replace it with its own older copy.
    """
    shared = {k: v for k, v in (carried or {}).items() if k in SHARED_KEYS}
    if shared and not os.path.exists(shared_path()):
        _write_json(shared_path(), shared_dir(), shared)


def _migrate_shared_only() -> None:
    """Seed the account from ANY host's pre-split file.

    Runs when this host has no legacy settings of its own but another host
    does -- a user who has only ever run MotionBuilder should not have to type
    the server URL again in 3ds Max. Only the account keys travel; UI state
    stays where it was written.
    """
    if os.path.exists(shared_path()):
        return
    for legacy in legacy_shared_paths():
        if os.path.exists(legacy):
            _seed_shared_from(_read_json(legacy))
            return


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: str, directory: str, data: dict) -> None:
    """Atomic write — a half-written settings file is worse than a missing one."""
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict:
    """Per-host settings with the shared account keys overlaid.

    Shared wins for its own keys: a server URL changed in one DCC is the one
    every DCC should use, which is the point of sharing it.
    """
    _migrate_legacy_settings()
    data = _read_json(settings_path())
    for key, value in _read_json(shared_path()).items():
        if key in SHARED_KEYS:
            data[key] = value
    return data


def save(data: dict) -> None:
    """Split *data* across the two files and write both atomically."""
    shared = {k: v for k, v in data.items() if k in SHARED_KEYS}
    private = {k: v for k, v in data.items() if k not in SHARED_KEYS}
    _write_json(settings_path(), settings_dir(), private)
    if shared:
        merged = _read_json(shared_path())
        merged.update(shared)
        _write_json(shared_path(), shared_dir(), merged)


def get(key: str, default=None):
    """Return a single setting value, or *default* if absent."""
    return load().get(key, default)


def set(key: str, value) -> None:
    """Load current settings, update *key*, and save."""
    data = load()
    data[key] = value
    save(data)
