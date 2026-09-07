"""Runtime dependency bootstrap for animatica_core.

Runs *inside* MotionBuilder at plugin startup. Detects whether the
plugin's hard dependency (NumPy 1.x) is importable in MoBu's bundled
Python and, when missing, drives the pip install via the same
``sys.executable`` that imported this module -- so it works for any
MoBu install location without path heuristics.

DCC-agnostic: MUST NOT import pyfbsdk. The pyfbsdk-facing dialog logic
lives in ``_startup.py`` and calls into the functions here.

Public surface:
    NUMPY_SPEC          str          -- pip spec for THIS interpreter (version-aware)
    cache_path()        -> str       -- %LOCALAPPDATA%\\animatica_core\\numpy_ok.json
    mobu_year()         -> str|None  -- parsed from sys.executable / Documents\\MB
    cached_ok()         -> bool      -- fast-path cache hit for this MoBu+sys.executable
    numpy_ok()          -> bool      -- try import + version check; writes cache on success
    install_numpy()     -> bool      -- subprocess pip install, then re-check
"""

import datetime
import json
import os
import re
import subprocess
import sys

from . import constants

# Resolved for the interpreter that imported this module (inside MoBu this
# is the MoBu Python). "numpy<2" on Python <=3.12, "numpy>=2" on >=3.13.
NUMPY_SPEC = constants.numpy_spec()


# Regenerable dependency-probe cache (not user data): rename only, no
# carry-over — a miss after the rename costs one cheap ``import numpy`` probe.
_CACHE_DIR_NAME = "animatica_core"   # fallback only; see cache_path()
_CACHE_FILE_NAME = "numpy_ok.json"
_PIP_TIMEOUT_SECS = 180


def cache_path() -> str:
    """Where the dependency probe caches its result.

    Follows ``animatica_core.host.cache_root()`` so each DCC gets its own —
    the answer is per-interpreter, and one host's "numpy is fine" says nothing
    about another's. Falls back to a package-named folder when no host is
    registered, which happens in tests and headless tooling.
    """
    try:
        from . import host
        base = host.cache_root()
    except Exception:
        base = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            _CACHE_DIR_NAME)
    return os.path.join(base, "numpy_ok.json")




def mobu_year():
    """Best-effort parse of the 4-digit MoBu year from ``sys.executable``.

    ``C:\\Program Files\\Autodesk\\MotionBuilder 2027\\bin\\x64\\...``
    -> ``"2027"``. Returns ``None`` if no year-like token is present
    (e.g. running under a plain CPython); callers should treat that as
    "no cache key available".
    """
    match = re.search(r"MotionBuilder[ _-]?(\d{4})", sys.executable or "")
    if match:
        return match.group(1)
    return None


def _read_cache():
    path = cache_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data):
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError as exc:
        print(f"[animatica] cache write failed ({path}): {exc}")


def cached_ok():
    """Return True if the cache says this MoBu+sys.executable combo passed.

    Requires the stored ``mobu_python`` to equal the current
    ``sys.executable`` -- protects against the user upgrading MoBu in
    place to a build that no longer has numpy 1.x.
    """
    year = mobu_year()
    if not year:
        return False
    entry = _read_cache().get(year)
    if not isinstance(entry, dict):
        return False
    return (
        entry.get("mobu_python") == sys.executable
        and isinstance(entry.get("numpy_version"), str)
        and constants.numpy_version_ok(entry["numpy_version"])
    )


def _write_ok_entry(numpy_version):
    year = mobu_year()
    if not year:
        return
    data = _read_cache()
    data[year] = {
        "numpy_version": numpy_version,
        "mobu_python": sys.executable,
        "checked": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _write_cache(data)


def numpy_ok():
    """Import numpy and check the version is acceptable for this Python.

    NumPy 1.x on Python <=3.12, NumPy >=2 on Python >=3.13. Updates the
    cache on success.
    """
    try:
        import numpy as np  # noqa: F401  (probe only)
    except ImportError:
        return False
    version = getattr(np, "__version__", "")
    if not constants.numpy_version_ok(version):
        return False
    _write_ok_entry(version)
    return True


def _is_under_program_files(path):
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    norm = os.path.normcase(os.path.abspath(path))
    return any(
        norm.startswith(os.path.normcase(os.path.abspath(root)) + os.sep)
        for root in (pf, pfx86)
        if root
    )


def _run_pip(args):
    """Invoke pip via the MoBu Python; return (returncode, stderr_text).

    ``--only-binary=:all:`` forbids source builds: if no compatible wheel
    exists, pip fails fast with a clear message instead of dropping into a
    meson/MSVC compile that needs build tools MoBu users don't have.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--quiet", "--no-warn-script-location",
             "--only-binary=:all:",
             "--force-reinstall", *args, NUMPY_SPEC],
            capture_output=True, timeout=_PIP_TIMEOUT_SECS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, f"{type(exc).__name__}: {exc}"
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    return result.returncode, stderr


def install_numpy():
    """Run pip install for NUMPY_SPEC; retry with --user under Program Files.

    Returns True iff a numpy acceptable for this Python is importable
    afterwards (1.x on Python <=3.12, >=2 on Python >=3.13).
    """
    print(f"[animatica] installing {NUMPY_SPEC} into {sys.executable} ...")
    rc, stderr = _run_pip([])
    if rc != 0:
        if _is_under_program_files(sys.prefix):
            print(f"[animatica] system install failed (rc={rc}); retrying with --user ...")
            rc, stderr = _run_pip(["--user"])
        if rc != 0:
            print(
                f"[animatica] pip install failed (rc={rc}).\n"
                f"  {stderr or '(no stderr output)'}\n"
                f"  No compiler is needed -- this means no matching wheel was "
                f"found (check internet/proxy).\n"
                f'  Manual fix:  "{sys.executable}" -m pip install "{NUMPY_SPEC}"'
            )
            return False
    # Force a re-import in case numpy was already partially loaded.
    sys.modules.pop("numpy", None)
    if numpy_ok():
        print("[animatica] numpy ready.")
        return True
    print("[animatica] pip reported success but numpy still does not import "
          "at the expected version. Restart MotionBuilder and try again.")
    return False
