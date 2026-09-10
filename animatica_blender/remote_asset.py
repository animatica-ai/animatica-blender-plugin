# SPDX-License-Identifier: GPL-3.0-or-later
"""The Animatic character, fetched on demand instead of shipped in the addon.

**Why it is not in the repo.** The character is ~12 MB. Vendoring it put that
in every clone and every release zip, and because the file was rewritten a few
times while it was being got right, several near-copies of it sit in git
history. It also pinned the addon to whatever build happened to be committed.
Fetching it moves the asset to the repository that owns it,
``animatica-assets-public``, and makes the version a one-line change here.

**What is fetched.** ``assets/animatica-hero/source/animatica-hero.fbx``: one
file, textures embedded, no control rig, no animation. The sibling
``blender/`` variant is deliberately NOT used. It is a default FBX import
saved as-is, so every one of its bones points +Y regardless of where its child
sits — the defect described in ``docs/rebuilding-the-character.md``. Importing
the FBX here with ``automatic_bone_orientation`` is what produces a usable
armature, and it costs a tenth of a second.

**Integrity and version are the same number.** The asset repository stores the
file in Git LFS, and an LFS pointer's ``oid`` *is* the sha256 of the content,
so pinning the hash pins the build: :data:`ASSET_SHA256` answers both "which
version" and "did it arrive intact". The hash is part of the cache filename,
so bumping the pin can never reuse an older download.

**Where it lands.** Blender's own per-user data directory, not the addon
folder: an addon directory is not reliably writable, and anything written
there is lost when the addon is updated or reinstalled. The cache therefore
survives addon upgrades, and deleting it is safe — the next import refetches.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import bpy

#: Asset repository coordinates. Pinned to a commit rather than ``main`` so a
#: change published upstream cannot alter what an installed addon downloads;
#: the pin and :data:`ASSET_SHA256` are bumped together.
ASSET_REPO = "animatica-ai/animatica-assets-public"
ASSET_REF = "df2d87cd76c9fb3d12b22d6c4541ddca4fdf1e1c"
ASSET_PATH = "assets/animatica-hero/source/animatica-hero.fbx"

#: Human-facing version, for messages. From the asset's own README.
ASSET_VERSION = "v003"

#: sha256 of the file contents == the Git LFS oid recorded in the repository.
ASSET_SHA256 = "631a8e3ec4c570d6a19c81710747b61c3a94044725cb09ae9f697cb5847bcdca"
ASSET_BYTES = 12610444

#: LFS-backed files must be fetched through the media host: ``raw.github
#: usercontent.com`` serves the ~130-byte pointer text instead of the content.
ASSET_URL = f"https://media.githubusercontent.com/media/{ASSET_REPO}/{ASSET_REF}/{ASSET_PATH}"

_CACHE_DIR_NAME = os.path.join("animatica", "assets")
_CHUNK = 256 * 1024


def cache_dir() -> Path:
    """The per-user directory downloads live in, created on demand."""
    return Path(bpy.utils.user_resource('DATAFILES', path=_CACHE_DIR_NAME, create=True))


def cache_path() -> Path:
    """Where this pinned build of the character is cached.

    The hash is in the name, so a bumped pin is a different file and an old
    download is never mistaken for the new one.
    """
    return cache_dir() / f"animatica-hero-{ASSET_SHA256[:12]}.fbx"


def is_cached() -> bool:
    """True when this exact build is already downloaded.

    Size only — the sha256 is verified once, at download time, and rehashing
    12 MB on every panel redraw to re-answer a question already answered would
    be absurd. A truncated or corrupted file fails the import with a readable
    error, and deleting the cache recovers it.
    """
    p = cache_path()
    try:
        return p.is_file() and p.stat().st_size == ASSET_BYTES
    except OSError:
        return False


def human_size(num_bytes: int = ASSET_BYTES) -> str:
    return f"{num_bytes / 1048576:.0f} MB"


class DownloadError(RuntimeError):
    """Fetching the character failed. Message is fit to show a user."""


def download(progress=None, is_cancelled=None) -> Path:
    """Fetch the character into the cache and return its path.

    *progress* is called with ``(bytes_so_far, total_bytes)``; *is_cancelled*
    is polled between chunks and aborts the download when it returns True.
    Both are optional, and both are called from whatever thread this runs on —
    keep them free of ``bpy``.

    Written to a temporary file and moved into place only after the hash
    matches, so an interrupted or corrupted download can never leave something
    behind that :func:`is_cached` would accept.
    """
    target = cache_path()
    if is_cached():
        return target

    tmp = target.with_suffix(".part")
    digest = hashlib.sha256()
    received = 0
    try:
        req = Request(ASSET_URL, headers={"Accept": "application/octet-stream"})
        with urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
            declared = int(resp.headers.get("Content-Length") or ASSET_BYTES)
            while True:
                if is_cancelled is not None and is_cancelled():
                    raise DownloadError("Download cancelled")
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fh.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, declared)
    except HTTPError as exc:
        _discard(tmp)
        raise DownloadError(
            f"Cannot download the Animatic character (HTTP {exc.code} from "
            f"{ASSET_REPO}). Check the connection and try again."
        ) from exc
    except URLError as exc:
        _discard(tmp)
        raise DownloadError(
            f"Cannot reach {ASSET_REPO} to download the Animatic character "
            f"({exc.reason})."
        ) from exc
    except DownloadError:
        _discard(tmp)
        raise
    except OSError as exc:
        _discard(tmp)
        raise DownloadError(f"Cannot write the character to {cache_dir()}: {exc}") from exc

    if digest.hexdigest() != ASSET_SHA256:
        _discard(tmp)
        raise DownloadError(
            "The downloaded character does not match its expected checksum; "
            "it arrived corrupted or the published asset changed. Nothing was "
            "installed."
        )

    try:
        shutil.move(str(tmp), str(target))
    except OSError as exc:
        _discard(tmp)
        raise DownloadError(f"Cannot move the character into {cache_dir()}: {exc}") from exc
    return target


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
