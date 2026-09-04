"""Resolve versioned FBX paths for Story-clip export.

Pure filesystem logic — no pyfbsdk — so it lives under ``core/`` per the
DCC-isolation rule and is unit-testable in plain CPython.

Convention::

    <story_path>/YYYY-MM-DD/<stem>_v001.fbx

**The filesystem is the existence oracle, not the scene.** A scene-derived
counter (``unique_take_name``-style) restarts at 1 in a fresh session, so
regenerating yesterday's prompt in a new scene would silently clobber the
existing file. Every resolve globs the day folder instead.

Comparison is **casefolded**: ``_Anim_v001.fbx`` and ``_anim_v001.fbx`` are
the same file on NTFS but two distinct strings in a Python set.

*today* is injectable throughout so tests don't depend on the wall clock,
and the day folder is computed **per export** — a session running across
midnight must roll over rather than pin the folder it started in.
"""

from __future__ import annotations

import os
import re
from datetime import date


# Trailing ``_v<digits>``, with or without an extension. Anchored at the end
# so a stem that itself contains "_v12_" doesn't parse as a version.
_VERSION_RE = re.compile(r"_v(\d+)$", re.IGNORECASE)

# Mirrors ``story_builder._sanitize``; duplicated rather than imported
# because that module imports pyfbsdk (DCC isolation).
_UNSAFE_RE = re.compile(r"[^\w\-]")

_STEM_FALLBACK = "animatica"
_VERSION_PAD   = 3


def sanitize_stem(stem: str | None) -> str:
    """Return *stem* reduced to filename-safe characters.

    Falls back to ``"animatica"`` when nothing survives — an empty or
    symbol-only label must not resolve to a bare ``_v001.fbx``.
    """
    safe = _UNSAFE_RE.sub("_", stem or "").strip("_")
    return safe or _STEM_FALLBACK


def date_folder(story_path: str, *, today: date | None = None) -> str:
    """Return ``<story_path>/YYYY-MM-DD`` for *today* (default: the real today).

    Call per export — never cache. The folder is not created here; see
    ``story_builder._export_clip_fbx``, which makedirs after resolving.
    """
    day = today or date.today()
    return os.path.join(story_path, day.strftime("%Y-%m-%d"))


def parse_version(filename: str) -> int | None:
    """Return the ``_vNNN`` version encoded in *filename*, or None.

    Accepts a bare stem or a full filename (the extension is stripped
    first). Non-versioned names return None rather than raising.
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    match = _VERSION_RE.search(stem)
    return int(match.group(1)) if match else None


def format_version(stem: str, version: int) -> str:
    """Return the versioned filename for *stem* at *version* (``name_v001.fbx``)."""
    return f"{stem}_v{version:0{_VERSION_PAD}d}.fbx"


def existing_versions(story_path: str, stem: str, *, today: date | None = None) -> list[int]:
    """Return the sorted versions of *stem* already present in the day folder.

    Empty when the folder doesn't exist yet — the first export of the day
    is the common case, not an error.
    """
    folder = date_folder(story_path, today=today)
    safe   = sanitize_stem(stem)
    needle = safe.casefold()
    found: list[int] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return found
    for name in names:
        base, ext = os.path.splitext(name)
        if ext.casefold() != ".fbx":
            continue
        version = parse_version(base)
        if version is None:
            continue
        # Strip the parsed suffix and compare the stem casefolded — NTFS
        # treats the two casings as one file, a Python set would not.
        if _VERSION_RE.sub("", base).casefold() == needle:
            found.append(version)
    return sorted(found)


def resolve_versioned_path(
    story_path: str,
    stem: str,
    *,
    overwrite: bool,
    today: date | None = None,
) -> tuple[str, int]:
    """Return ``(absolute_path, version)`` for the next Story-clip export.

    *overwrite* False -> the next free version, i.e. ``max(existing) + 1``
    (``_v001`` on an empty folder). Deliberately **not** the lowest unused
    number: filling a gap left by a deleted ``_v002`` would mint a version
    that sorts *older* than content generated before it, and the whole
    "newest version wins playback" model ranks by that number.

    *overwrite* True -> the **newest existing** version's path, so a replace
    rewrites the current file in place (``_v001`` when there is nothing to
    replace). Older versions are never touched — they are the history.

    The returned path's parent directory may not exist yet; the caller
    creates it.
    """
    safe     = sanitize_stem(stem)
    folder   = date_folder(story_path, today=today)
    versions = existing_versions(story_path, safe, today=today)

    if overwrite:
        version = versions[-1] if versions else 1
    else:
        version = (versions[-1] + 1) if versions else 1

    return os.path.join(folder, format_version(safe, version)), version
