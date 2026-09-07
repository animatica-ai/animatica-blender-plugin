"""Vendor this SDK into a host plugin, and keep every copy honest.

One implementation, parameterised by host — the pattern that
``gates/surface.py`` set: the MotionBuilder and 3ds Max plugins each grew a
``scripts/sync_core.py`` of their own, and the two disagreed in exactly the
way the audit predicted. The Max copy read the SDK's **working tree** and
stamped ``CORE-VERSION`` from HEAD, so an uncommitted SDK edit leaked into
the vendor under a stamp naming a commit that never contained it; and a
vendored tree that was merely *behind* was reported as "LOCALLY EDITED",
which points the operator at the wrong repo. This module is the MoBu
mechanism (git archive at a named commit) promoted to core, plus the
classification the Max gate had and the sync did not.

A host's ``scripts/sync_core.py`` becomes a shim::

    import os, sys
    from animatica_core.vendoring import main
    sys.exit(main(plugin_root=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))

Yes, the shim runs the VENDORED copy of this module to judge the vendored
tree it is part of. That is sound: the judgement compares bytes against a
named commit of the SDK, so a stale checker still compares correctly — and
a checker whose own file drifted is reported by its own comparison.

Hard rules, enforced here rather than promised:

* ``--write`` extracts from ``git archive <commit>`` — the SDK's working
  tree is never a write source and never earns a ``CORE-VERSION`` stamp.
  (``--ref worktree`` stays available for CHECK mode, where "how far is my
  copy from what's on disk over there" is a fair question.)
* A vendored file that differs from the pin is classified, not lumped:
  content found in the SDK's history is **STALE** (says which commit it
  matches — re-vendor); content found nowhere is **LOCALLY EDITED** (the
  fix belongs in the SDK).

Line endings are normalised before hashing: both repos run with
``core.autocrlf=true``, so bytes on disk depend on how a checkout was made;
the commit's bytes are the truth and they are LF.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

#: Directories never vendored and never compared.
SKIP_DIRS = {"__pycache__"}

DEFAULT_CORE_REPO = os.environ.get(
    "ANIMATICA_CORE", r"C:\_CODE\motionmcp-client-sdk")


class VendorConfig:
    """Where one host keeps the things this module reads and writes."""

    def __init__(self, plugin_root, core_repo=None):
        self.plugin_root = os.path.abspath(plugin_root)
        self.core_repo = os.path.abspath(core_repo or DEFAULT_CORE_REPO)
        self.dst = os.path.join(self.plugin_root, "animatica_core")
        self.version_file = os.path.join(self.plugin_root, "CORE-VERSION")

    # -- pin ----------------------------------------------------------------

    def pinned_ref(self) -> str:
        """The commit ``CORE-VERSION`` names, or ``HEAD`` with no pin yet."""
        try:
            with open(self.version_file, encoding="utf-8") as fh:
                return fh.read().strip() or "HEAD"
        except OSError:
            return "HEAD"

    # -- git ----------------------------------------------------------------

    def _git(self, *args, binary=False, data=None):
        out = subprocess.run(["git", "-C", self.core_repo] + list(args),
                             capture_output=True, timeout=120, input=data)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", "replace").strip())
        return out.stdout if binary else out.stdout.decode("utf-8").strip()

    def resolve(self, ref: str) -> str:
        """Full hash for *ref*; the literal ``worktree`` passes through."""
        if ref == "worktree":
            return "worktree"
        return self._git("rev-parse", ref)

    def commit_holding(self, vendored_path: str, rel: str) -> str | None:
        """A commit whose tree holds this exact content at ``rel``.

        This is what tells STALE from LOCALLY EDITED: the file's normalised
        bytes are hashed as a git blob and searched for in history. A hit
        means the copy is an honest older (or newer) SDK file — re-vendor;
        a miss means someone edited the generated tree.

        ``--find-object`` reports every commit where the blob CHANGED —
        including the one that removed it, whose tree does not contain the
        content — so each hit is verified against its tree at ``rel``
        before it may be named in a report.
        """
        with open(vendored_path, "rb") as fh:
            raw = fh.read()
        # SDK blobs are LF (autocrlf normalises on check-in), so the
        # normalised bytes are the expected hit; the raw fallback covers a
        # repo whose blobs were committed with CRLF anyway.
        for data in dict.fromkeys((raw.replace(b"\r\n", b"\n"), raw)):
            try:
                blob = self._git("hash-object", "-w", "--stdin", data=data)
                full = self._git("rev-parse", blob)
                hits = self._git("log", "--all", "--format=%h",
                                 f"--find-object={blob}")
            except RuntimeError:
                return None
            for hit in hits.splitlines():
                try:
                    at = self._git("rev-parse",
                                   f"{hit}:animatica_core/{rel}")
                except RuntimeError:
                    continue                      # removed it, or renamed
                if at == full:
                    return hit
        return None


def _iter(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, fn)
            yield full, os.path.relpath(full, root).replace(os.sep, "/")


def _sha(path) -> str:
    with open(path, "rb") as fh:
        data = fh.read()
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:16]


class source_tree:
    """Context manager yielding a directory holding core at ``ref``."""

    def __init__(self, cfg: VendorConfig, ref: str):
        self.cfg, self.ref = cfg, ref
        self._tmp = None

    def __enter__(self):
        if self.ref == "worktree":
            return os.path.join(self.cfg.core_repo, "animatica_core")
        self._tmp = tempfile.mkdtemp(prefix="animatica_core_")
        blob = self.cfg._git("archive", self.ref, "animatica_core",
                             binary=True)
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                parts = member.name.split("/")
                if "__pycache__" in parts \
                        or member.name.endswith((".pyc", ".pyo")):
                    continue
                target = os.path.join(self._tmp, *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with tar.extractfile(member) as src, \
                        open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh)
        return os.path.join(self._tmp, "animatica_core")

    def __exit__(self, *exc):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
        return False


def compare(cfg: VendorConfig, ref=None):
    """``(mismatched, missing, extra)`` between core at *ref* and the copy.

    ``mismatched`` carries relpaths only; :func:`classify` splits them into
    stale vs edited (a second, slower question — CI asks both, an in-DCC
    gate may settle for this one). Raises ``FileNotFoundError`` when the SDK
    is not checked out — "cannot tell" must never read as "no drift".
    """
    if not os.path.isdir(cfg.core_repo):
        raise FileNotFoundError(cfg.core_repo)
    with source_tree(cfg, ref or cfg.pinned_ref()) as src:
        if not os.path.isdir(src):
            raise FileNotFoundError(src)
        source = dict((rel, full) for full, rel in _iter(src))
        vendored = (dict((rel, full) for full, rel in _iter(cfg.dst))
                    if os.path.isdir(cfg.dst) else {})
        mismatched, missing = [], []
        for rel, full in sorted(source.items()):
            dst = os.path.join(cfg.dst, *rel.split("/"))
            if not os.path.exists(dst):
                missing.append(rel)
            elif _sha(full) != _sha(dst):
                mismatched.append(rel)
        extra = sorted(rel for rel in vendored if rel not in source)
        return mismatched, missing, extra


def classify(cfg: VendorConfig, mismatched):
    """Split *mismatched* relpaths into ``(stale, edited)``.

    ``stale`` is ``[(rel, commit)]`` — the vendored content exists in SDK
    history at *commit*; ``edited`` is ``[rel]`` — it exists nowhere, so a
    person changed the generated tree.
    """
    stale, edited = [], []
    for rel in mismatched:
        commit = cfg.commit_holding(
            os.path.join(cfg.dst, *rel.split("/")), rel)
        (stale.append((rel, commit)) if commit else edited.append(rel))
    return stale, edited


def main(plugin_root=None, core_repo=None, argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=None,
                    help="SDK commit to compare against (default: the one "
                         "CORE-VERSION pins; 'worktree' for the SDK's "
                         "working tree — CHECK mode only)")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="also delete vendored files core no longer has")
    args = ap.parse_args(argv)

    cfg = VendorConfig(plugin_root or os.getcwd(), core_repo)

    if not os.path.isdir(cfg.core_repo):
        print(f"error: motionmcp-client-sdk not found at {cfg.core_repo}")
        print("set ANIMATICA_CORE to its checkout")
        return 2

    ref = args.ref or cfg.pinned_ref()
    if args.write and ref == "worktree":
        # The Max lesson, made structural: an uncommitted edit must not be
        # able to enter a vendor, and nothing unresolvable earns a stamp.
        print("error: --write vendors a COMMIT; 'worktree' cannot be "
              "stamped into CORE-VERSION. Commit the SDK change first.")
        return 2
    try:
        commit = cfg.resolve(ref)
    except RuntimeError as exc:
        print(f"error: cannot resolve {ref!r} in {cfg.core_repo}: {exc}")
        return 2

    print(f"core @ {commit}")

    with source_tree(cfg, ref) as src:
        source = dict((rel, full) for full, rel in _iter(src))
        mismatched, missing, extra = compare(cfg, ref)
        copied = 0
        for rel, full in sorted(source.items()):
            dst = os.path.join(cfg.dst, *rel.split("/"))
            if args.write and (not os.path.exists(dst)
                               or _sha(full) != _sha(dst)):
                # --write always makes the tree match core: refusing to
                # overwrite an edited file here would mean a legitimate core
                # update silently did not land. The edit was CHECK mode's
                # news to break.
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(full, dst)
                copied += 1

    if args.write and args.force:
        for rel in extra:
            os.remove(os.path.join(cfg.dst, *rel.split("/")))

    if args.write:
        with open(cfg.version_file, "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(f"{commit}\n")
        print(f"vendored {len(source)} file(s); {copied} updated")
        print(f"CORE-VERSION -> {commit}")

    print(f"{len(source)} core file(s) vendored into "
          f"{os.path.relpath(cfg.dst, cfg.plugin_root)}")

    if not args.write:
        problems = False
        if missing:
            problems = True
            print(f"\nMISSING from the vendored tree ({len(missing)}):")
            for rel in missing[:10]:
                print(f"  - {rel}")
        if mismatched:
            problems = True
            stale, edited = classify(cfg, mismatched)
            if stale:
                print(f"\nSTALE -- honest SDK content, wrong vintage "
                      f"({len(stale)}); re-vendor (--write):")
                for rel, at in stale[:10]:
                    print(f"  - {rel} (matches core @ {at})")
            if edited:
                print(f"\nLOCALLY EDITED -- content in no SDK commit "
                      f"({len(edited)}):")
                for rel in edited[:10]:
                    print(f"  - {rel}")
                print("\nThe vendored tree is generated. Make the fix in "
                      "motionmcp-client-sdk and re-run with --write; an "
                      "edit here is lost on the next sync and leaves the "
                      "plugins disagreeing.")
        if extra:
            problems = True
            print(f"\nNOT IN CORE -- stale leftovers ({len(extra)}):")
            for rel in extra[:10]:
                print(f"  - {rel}")
        if problems:
            return 1
        print("vendored tree matches core exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main(plugin_root=os.getcwd()))
