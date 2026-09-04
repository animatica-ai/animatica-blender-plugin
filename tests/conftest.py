"""Headless rig for the addon's tests — no Blender, no network.

``animatica_blender/__init__.py`` imports ``bpy`` and registers Blender
classes, so ``import animatica_blender.core_adapter`` cannot work outside
Blender. Two ways around that were on the table:

1. build a synthetic ``animatica_blender`` package in ``sys.modules`` with
   ``bpy``/``mathutils`` stubs (what ``scratchpad/b2/parity_check.py`` does
   for the *other* addon module it needs), or
2. load ``core_adapter.py`` straight off disk under a name that pulls in no
   package at all.

(2) is used here, and it is the simpler one *for this module*: everything
above ``core_adapter``'s "Blender-facing" banner is pure, and its only
module-level imports are absolute (``from animatica_core...``) — every
``from . import ...`` sits inside a function body. So no parent package and
no ``bpy`` stub is needed; the pure half loads with nothing but the addon
directory on ``sys.path``, and the ``bpy`` half simply is never called. A
stub package would additionally risk a stub silently standing in for a real
value and turning a red test green.

That same ``sys.path`` entry is what makes the VENDORED ``animatica_core``
(``animatica_blender/animatica_core/``) importable under the top-level name
it uses for itself. The tests assert on and print ``animatica_core.__file__``
so it stays visible that the copy under test ships with the addon and is not
some SDK checkout that happens to be on the path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = REPO_ROOT / "animatica_blender"

# Front of the path on purpose: the addon's own vendored core must win over any
# ``animatica_core`` a stray checkout or site-packages entry might provide.
if str(ADDON_DIR) not in sys.path:
    sys.path.insert(0, str(ADDON_DIR))


# ---------------------------------------------------------------------------
# The Blender surfaces the pure adapter reads, as plain ducks
# ---------------------------------------------------------------------------

class Block:
    """The ``PromptBlock`` surface ``prompt_boxes`` reads (properties.py)."""

    def __init__(self, prompt="", frame_start=0, frame_end=0, enabled=True,
                 seed=0):
        self.prompt = prompt
        self.frame_start = int(frame_start)
        self.frame_end = int(frame_end)
        self.enabled = bool(enabled)
        self.seed = int(seed)
        self.last_used_seed = 0


class Settings:
    """The settings surface ``state_from_settings`` reads.

    Defaults are the same INPUT numbers the A/B hosts used for the goldens
    (custom preset at 300 steps, separated CFG at [2.0, 2.0], 5 transition
    frames), so the parity test can use it unchanged.
    """

    def __init__(self, seed=0, **over):
        self.seed = int(seed)
        self.last_used_seed = 0
        self.quality_preset = "CUSTOM"      # unknown preset -> custom_steps
        self.custom_steps = 300
        self.post_processing = True
        self.num_transition_frames = 5
        self.cfg_enabled = True
        self.cfg_text = 2.0
        self.cfg_constraint = 2.0
        for key, value in over.items():
            setattr(self, key, value)


@pytest.fixture(scope="session")
def block_cls():
    return Block


@pytest.fixture(scope="session")
def settings_cls():
    return Settings


# ---------------------------------------------------------------------------
# The modules under test
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def adapter():
    """``animatica_blender/core_adapter.py``, loaded without its package."""
    path = ADDON_DIR / "core_adapter.py"
    if not path.is_file():
        pytest.fail(f"core_adapter.py not found at {path}")
    name = "animatica_blender_core_adapter_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def core(adapter):
    """The core the ADDON ships, reached under its own top-level name.

    ``adapter`` is depended on so the load order is the real one: the adapter
    is what pulls core in.
    """
    import animatica_core
    from animatica_core.core import prompt_model, request_builder
    from animatica_core.gates import ab_compare, ab_scenario

    pkg_dir = Path(animatica_core.__file__).resolve().parent
    assert pkg_dir.parent == ADDON_DIR, (
        f"animatica_core resolved to {animatica_core.__file__}, which is not "
        f"the copy vendored in {ADDON_DIR} -- a stray checkout is shadowing it"
    )
    return types.SimpleNamespace(
        animatica_core=animatica_core,
        prompt_model=prompt_model,
        request_builder=request_builder,
        ab_compare=ab_compare,
        ab_scenario=ab_scenario,
    )


# ---------------------------------------------------------------------------
# A/B goldens (optional — the parity test skips without them)
# ---------------------------------------------------------------------------

GOLDEN_TAIL = ("tools", "ab_suite", "golden", "blender", "local")

#: Checkouts of the SDK that are known to carry the frozen Blender goldens.
#: Not authoritative — ``ANIMATICA_AB_GOLDEN`` overrides, and the repo's
#: siblings are scanned too, so a differently named worktree still works.
DEFAULT_SDK_ROOTS = (
    Path("C:/_CODE/motionmcp-client-sdk-wt-k7"),
    Path("C:/_CODE/motionmcp-client-sdk"),
)


def _golden_candidates():
    """Every directory that might hold ``c1..c4.manifest.json``, best first."""
    env_dir = os.environ.get("ANIMATICA_AB_GOLDEN")
    if env_dir:
        yield Path(env_dir)

    env_core = os.environ.get("ANIMATICA_CORE")
    if env_core:
        base = Path(env_core)
        # Accept either the SDK checkout root or the animatica_core package dir.
        for root in (base, base.parent):
            yield root.joinpath(*GOLDEN_TAIL)

    for root in DEFAULT_SDK_ROOTS:
        yield root.joinpath(*GOLDEN_TAIL)

    for sibling in sorted(REPO_ROOT.parent.glob("motionmcp-client-sdk*")):
        yield sibling.joinpath(*GOLDEN_TAIL)


CHECKPOINTS = ("c1", "c2", "c3", "c4")


def _is_complete(directory: Path) -> bool:
    return all((directory / f"{cp}.manifest.json").is_file()
               for cp in CHECKPOINTS)


@pytest.fixture(scope="session")
def golden_dir():
    tried = []
    for candidate in _golden_candidates():
        if candidate in tried:
            continue
        tried.append(candidate)
        if _is_complete(candidate):
            return candidate
    listing = "\n".join(f"    {p}" for p in tried)
    pytest.skip(
        "A/B goldens not found -- the parity gate needs "
        f"{', '.join(cp + '.manifest.json' for cp in CHECKPOINTS)} in one "
        "directory. Looked in:\n" + listing + "\n"
        "  Set ANIMATICA_AB_GOLDEN to that directory (typically "
        "<sdk checkout>/tools/ab_suite/golden/blender/local), or set "
        "ANIMATICA_CORE to the SDK checkout root."
    )


@pytest.fixture(scope="session")
def golden_capabilities(golden_dir):
    path = (golden_dir / ".." / ".." / "capabilities.json").resolve()
    if not path.is_file():
        pytest.skip(f"goldens found in {golden_dir} but {path} is missing")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="session")
def golden_manifests(golden_dir):
    out = {}
    for cp in CHECKPOINTS:
        with open(golden_dir / f"{cp}.manifest.json", encoding="utf-8") as fh:
            out[cp] = json.load(fh)
    return out
