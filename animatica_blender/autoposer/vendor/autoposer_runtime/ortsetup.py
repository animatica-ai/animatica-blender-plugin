"""Getting onnxruntime onto the machine — once, on first use.

Blender ships numpy and not much else, so the runtime has to arrive from somewhere. By default
it is fetched once, on first use, with pip — the package stays small and the one download that
has to happen anyway (the model) gains a companion.

An addon that cannot rely on a network ships the wheels instead, in `wheels/` beside this
package (~21 MB compressed per platform), and they are UNZIPPED directly: no pip, no
subprocess, nothing spawned inside the host. Same code path otherwise.

It installs with `--no-deps` on purpose. onnxruntime's only import-time dependency is numpy,
which Blender already has — verified by importing it and running a session in an environment
holding nothing else. Installing the declared dependency set would drop a second numpy into
the path, and a second numpy next to `bpy` is a bad trade for nothing.

The install directory is keyed by python version and platform, so upgrading Blender (which
changes the ABI) fetches a fresh runtime instead of importing an incompatible one. It is
APPENDED to `sys.path`, never prepended: anything Blender already provides keeps winning.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import zipfile
from pathlib import Path

#: What to ask pip for: one exact release, so what lands on a machine is what was tested.
#: 1.23.2 is the newest onnxruntime with wheels for every platform Blender 5.x runs on —
#: macOS arm64 AND x86_64 (1.24 dropped Intel Macs), Windows x64, Linux x86_64/aarch64 — for
#: cp311 (Blender 5.0) and cp313 (Blender 5.1). Bump it together with ORT_WHEEL_SHA256.
ORT_VERSION = "1.23.2"
ORT_REQUIREMENT = f"onnxruntime=={ORT_VERSION}"

#: sha256 of every onnxruntime 1.23.2 wheel pip may pick for cp311-cp313, from PyPI.
#: pip is run with --require-hashes, so a wheel that is not one of these is refused.
ORT_WHEEL_SHA256 = (
    "6f91d2c9b0965e86827a5ba01531d5b669770b01775b23199565d6c1f136616c",  # cp311-cp311-macosx_13_0_arm64
    "87d8b6eaf0fbeb6835a60a4265fde7a3b60157cf1b2764773ac47237b4d48612",  # cp311-cp311-macosx_13_0_x86_64
    "bbfd2fca76c855317568c1b36a885ddea2272c13cb0e395002c402f2360429a6",  # cp311-cp311-manylinux_2_27_aarch64.manylinux_2_28_aarch64
    "da44b99206e77734c5819aa2142c69e64f3b46edc3bd314f6a45a932defc0b3e",  # cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64
    "902c756d8b633ce0dedd889b7c08459433fbcf35e9c38d1c03ddc020f0648c6e",  # cp311-cp311-win_amd64
    "b8f029a6b98d3cf5be564d52802bb50a8489ab73409fa9db0bf583eabb7c2321",  # cp312-cp312-macosx_13_0_arm64
    "218295a8acae83905f6f1aed8cacb8e3eb3bd7513a13fe4ba3b2664a19fc4a6b",  # cp312-cp312-macosx_13_0_x86_64
    "76ff670550dc23e58ea9bc53b5149b99a44e63b34b524f7b8547469aaa0dcb8c",  # cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64
    "0f9b4ae77f8e3c9bee50c27bc1beede83f786fe1d52e99ac85aa8d65a01e9b77",  # cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64
    "25de5214923ce941a3523739d34a520aac30f21e631de53bba9174dc9c004435",  # cp312-cp312-win_amd64
    "2ff531ad8496281b4297f32b83b01cdd719617e2351ffe0dba5684fb283afa1f",  # cp313-cp313-macosx_13_0_arm64
    "162f4ca894ec3de1a6fd53589e511e06ecdc3ff646849b62a9da7489dee9ce95",  # cp313-cp313-macosx_13_0_x86_64
    "45d127d6e1e9b99d1ebeae9bcd8f98617a812f53f46699eafeb976275744826b",  # cp313-cp313-manylinux_2_27_aarch64.manylinux_2_28_aarch64
    "8bace4e0d46480fbeeb7bbe1ffe1f080e6663a42d1086ff95c1551f2d39e7872",  # cp313-cp313-manylinux_2_27_x86_64.manylinux_2_28_x86_64
    "1f9cc0a55349c584f083c1c076e611a7c35d5b867d5d6e6d6c823bf821978088",  # cp313-cp313-win_amd64
)


def platform_tag() -> str:
    mach = {"AMD64": "x86_64", "x86_64": "x86_64", "arm64": "arm64",
            "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    return f"{sys.platform}-{mach}-py{sys.version_info.major}{sys.version_info.minor}"


def default_target(cache_dir=None) -> Path:
    from .bundle import default_cache_dir
    return Path(cache_dir or default_cache_dir()) / "runtime" / platform_tag()


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except Exception:       # noqa: BLE001 — a broken install must read as "not available"
        return False
    return True


def activate(target=None, cache_dir=None) -> bool:
    """Make an already-installed runtime importable. True if onnxruntime can now be imported."""
    if available():
        return True
    d = Path(target or default_target(cache_dir))
    if d.exists() and str(d) not in sys.path:
        sys.path.append(str(d))          # append: Blender's own packages keep priority
    return available()


def python_executable() -> str:
    """The interpreter to run pip with.

    NOT `sys.executable` — inside Blender that is the Blender binary, and running `-m pip`
    with it launches Blender. The bundled interpreter sits under `sys.prefix`.
    """
    prefix = Path(sys.prefix)
    v = f"python{sys.version_info.major}.{sys.version_info.minor}"
    for c in (prefix / "bin" / v, prefix / "bin" / "python3", prefix / "bin" / "python",
              prefix / "bin" / "python.exe", prefix / "python.exe"):
        if c.exists():
            return str(c)
    return sys.executable


def bundled_wheels() -> Path | None:
    """A `wheels/` directory shipped next to this package, if there is one.

    An addon that must work with no network at all — a locked-down studio, an artist on a
    plane, a render farm with no route out — drops the onnxruntime wheels for its platforms
    there. Nothing else changes.
    """
    d = Path(__file__).resolve().parent / "wheels"
    return d if d.is_dir() and any(d.glob("*.whl")) else None


def wheel_for_this_machine(wheel_dir) -> Path | None:
    """Pick the wheel in `wheel_dir` built for this interpreter and this machine.

    A deliberately small subset of PEP 425 — enough to choose between the handful of wheels an
    addon would ship, and honest about what it is: it matches the python tag (`cp313`, or a
    `py3` pure wheel) and the platform tag, and returns None rather than guess.
    """
    mach = {"AMD64": "x86_64", "x86_64": "x86_64", "arm64": "arm64",
            "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    py = f"cp{sys.version_info.major}{sys.version_info.minor}"
    for whl in sorted(Path(wheel_dir).glob("*.whl")):
        parts = whl.stem.split("-")
        if len(parts) < 5:
            continue
        pytag, _abi, plat = parts[-3], parts[-2], parts[-1]
        if py not in pytag.split(".") and not any(t.startswith("py3") for t in pytag.split(".")):
            continue
        for p in plat.split("."):
            if p == "any":
                return whl
            if sys.platform == "darwin" and p.startswith("macosx") and p.endswith(mach):
                return whl
            if sys.platform.startswith("linux") and mach in p and (
                    p.startswith("manylinux") or p.startswith("musllinux")):
                return whl
            if sys.platform == "win32" and p in ("win_amd64", "win32"):
                return whl
    return None


def install_wheel(wheel: Path, target: Path) -> Path:
    """Unpack a wheel into `target`. No pip, no subprocess, no network — a wheel is a zip.

    That matters more than it sounds: `pip` inside a DCC means spawning the host's bundled
    interpreter, which is the part of "install a dependency into Blender" that goes wrong —
    a missing `ensurepip`, a sandbox that forbids subprocesses, an antivirus that objects to
    the host launching a child process. Unzipping needs none of it. Data-directory entries
    (`*.data/`) are not handled; onnxruntime's wheels have none, and a wheel that does should
    go through pip.
    """
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel) as z:
        bad = [n for n in z.namelist() if ".data/" in n]
        if bad:
            raise RuntimeError(f"{wheel.name} has a .data directory; install it with pip")
        z.extractall(target)
    return target


def install(target=None, cache_dir=None, *, requirement: str = ORT_REQUIREMENT,
            find_links=None, timeout: float = 900.0) -> Path:
    """Put onnxruntime in `target`. Returns the directory.

    A shipped wheel (`find_links`, or `wheels/` beside this package) is unzipped directly —
    no pip, no subprocess, no network. Otherwise pip fetches one from PyPI, which is the
    once-per-machine case.
    """
    d = Path(target or default_target(cache_dir))
    d.mkdir(parents=True, exist_ok=True)
    links = find_links or bundled_wheels()
    if links:
        whl = wheel_for_this_machine(links)
        if whl is None:
            raise RuntimeError(
                f"none of the wheels in {links} is built for {platform_tag()}")
        return install_wheel(whl, d)
    py = python_executable()
    # No PIP_* variables and no pip.conf (--isolated): a machine's own index or proxy settings
    # must not decide where the runtime comes from.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PIP_")}
    subprocess.run([py, "-m", "ensurepip", "--default-pip"], check=False,
                   capture_output=True, env=env, timeout=timeout)
    cmd = [py, "-m", "pip", "--isolated", "install", "--no-input", "--no-deps",
           "--only-binary=:all:", "--disable-pip-version-check", "--no-cache-dir",
           "--index-url", "https://pypi.org/simple",
           "--target", str(d)]
    req_file = None
    if requirement == ORT_REQUIREMENT:
        # Hash-pinned: pip refuses any file whose sha256 is not listed.
        import tempfile
        fd, req_file = tempfile.mkstemp(prefix="ort-", suffix=".txt")
        with os.fdopen(fd, "w") as fh:
            fh.write(requirement + " " + " ".join(f"--hash=sha256:{h}"
                                                  for h in ORT_WHEEL_SHA256) + "\n")
        cmd += ["--require-hashes", "-r", req_file]
    else:
        cmd.append(requirement)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    finally:
        if req_file:
            try:
                os.unlink(req_file)
            except OSError:
                pass
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        raise RuntimeError("could not install onnxruntime: " + " / ".join(tail))
    return d


def ensure(target=None, cache_dir=None, *, allow_install: bool = True, **kw) -> bool:
    """Import onnxruntime, installing it first if it is not there yet.

    Returns True when `import onnxruntime` works afterwards. Raises only if the install
    itself fails — a caller that wants to degrade quietly passes `allow_install=False`.
    """
    if activate(target, cache_dir):
        return True
    if not allow_install:
        return False
    install(target, cache_dir, **kw)
    return activate(target, cache_dir)
