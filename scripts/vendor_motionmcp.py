#!/usr/bin/env python3
"""Fetch the pinned motionmcp wheel into animatica_blender/vendor/motionmcp/.

    python scripts/vendor_motionmcp.py --write   # the build step: extract the wheel, then check
    python scripts/vendor_motionmcp.py           # check only: exit 0 in sync, 1 drift, 2 undecidable

The vendored tree is git-ignored: `make deps` (run by `make zip` and
`make install`) and CI put it there from the pin, the single
``motionmcp-sdk==X`` line in requirements-bundle.txt, the same way the
MotionBuilder and 3ds Max plugins bundle their dependencies. The wheel is
fetched with pip into wheels/ (git-ignored) unless already there. The check
compares every RECORD entry under ``motionmcp/`` with the extracted file by
sha256, and the LICENSE with the wheel's dist-info copy.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements-bundle.txt"
WHEELS = REPO_ROOT / "wheels"
VENDOR = REPO_ROOT / "animatica_blender" / "vendor" / "motionmcp"
DIST = "motionmcp-sdk"
PREFIX = "motionmcp/"


def read_pin() -> str:
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        name, sep, version = raw.split("#", 1)[0].strip().partition("==")
        if sep and name.strip() == DIST:
            return version.strip()
    raise SystemExit(f"{REQUIREMENTS}: no '{DIST}==X' line")


def get_wheel(version: str) -> Path | None:
    pattern = f"{DIST.replace('-', '_')}-{version}-*.whl"
    found = sorted(WHEELS.glob(pattern))
    if not found:
        subprocess.run(
            [sys.executable, "-m", "pip", "download", "--no-deps",
             "--only-binary=:all:", "--dest", str(WHEELS), f"{DIST}=={version}"],
            check=False,
        )
        found = sorted(WHEELS.glob(pattern))
    return found[0] if found else None


def sha256_b64(data: bytes) -> str:
    # RECORD format: urlsafe base64, no padding.
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def license_member(whl: zipfile.ZipFile) -> str | None:
    names = [n for n in whl.namelist() if ".dist-info/licenses/" in n and n.endswith("LICENSE")]
    return names[0] if names else None


def expected(whl: zipfile.ZipFile) -> dict:
    """``{path relative to VENDOR: sha256_b64}`` for everything the tree must hold."""
    record = next(n for n in whl.namelist() if n.endswith(".dist-info/RECORD"))
    rows = csv.reader(io.StringIO(whl.read(record).decode("utf-8")))
    want = {}
    for path, digest, _size in rows:
        if path.startswith(PREFIX):
            algo, _, value = digest.partition("=")
            assert algo == "sha256", f"{path}: unexpected hash {algo}"
            want[path[len(PREFIX):]] = value
    lic = license_member(whl)
    if lic:
        want["LICENSE"] = sha256_b64(whl.read(lic))
    return want


def write(whl: zipfile.ZipFile) -> None:
    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    for name in whl.namelist():
        if name.startswith(PREFIX) and not name.endswith("/"):
            dest = VENDOR / name[len(PREFIX):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(whl.read(name))
    lic = license_member(whl)
    if lic is None:
        # The plan allowed copying LICENSE from a local source checkout; a
        # committed script cannot point at one, so stop instead of guessing.
        raise SystemExit("wheel has no dist-info/licenses/LICENSE; add vendor/motionmcp/LICENSE by hand")
    (VENDOR / "LICENSE").write_bytes(whl.read(lic))
    print(f"wrote {VENDOR.relative_to(REPO_ROOT)} from {whl.filename}")


def check(whl: zipfile.ZipFile) -> int:
    want = expected(whl)
    have = {
        p.relative_to(VENDOR).as_posix()
        for p in VENDOR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    } if VENDOR.is_dir() else set()
    drift = False
    for rel in sorted(want.keys() | have):
        if rel not in have:
            print(f"MISSING {rel}")
        elif rel not in want:
            print(f"EXTRA   {rel}")
        elif sha256_b64((VENDOR / rel).read_bytes()) != want[rel]:
            print(f"CHANGED {rel}")
        else:
            continue
        drift = True
    if not drift:
        print(f"ok   {len(want)} files match {Path(whl.filename).name}")
    return 1 if drift else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help="replace the vendored tree with the wheel's contents, then check")
    args = parser.parse_args(argv)

    version = read_pin()
    wheel = get_wheel(version)
    if wheel is None:
        # Exit 2, not 0: CI must go red when it cannot tell, not green.
        print(f"ERROR no {DIST}=={version} wheel in {WHEELS} and pip download failed")
        return 2
    with zipfile.ZipFile(wheel) as whl:
        if args.write:
            write(whl)
        return check(whl)


if __name__ == "__main__":
    sys.exit(main())
