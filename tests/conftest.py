"""Headless rig for ``animatica_blender.mmcp_client`` — no Blender, no network
beyond 127.0.0.1.

``animatica_blender/__init__.py`` imports ``bpy`` and registers Blender
classes; none of that is under test. So the parent package is synthesised
here: an empty module object whose ``__path__`` points at the REAL directory,
so ``import animatica_blender.mmcp_client`` and its ``from . import http_gate``
load the real files without running that ``__init__``. ``http_gate`` is
stdlib-only, so ``mmcp_client._opener()`` builds the real gate — asserted
below, because a stand-in there would make the offline tests prove nothing.

``bpy`` is a narrow, explicit stub: only the attributes ``mmcp_client`` reads
on the paths these tests exercise. Anything else raises ``AttributeError``
instead of pretending to be a value:

* ``bpy.app.version`` — read at import by ``_host_version()`` for
  ``X-Animatica-Host-Version`` (part of ``client_headers()``).
* ``bpy.app.online_access`` — read by ``online_access()`` on every request
  (``_require_online`` and the opener's gate); tests flip it.

``bpy.context`` is deliberately absent: the token getters read preferences
through it, and the tests monkeypatch ``get_access_token`` /
``get_refresh_token`` instead.

``animatica_blender.properties`` is replaced by a namespace carrying only
``CLOUD_API_URL`` (the real module is Blender ``PropertyGroup``s).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
ADDON_DIR = REPO_ROOT / "animatica_blender"

# ``import fake_server`` from the test module, whatever pytest's import mode.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def _synthetic_package(name: str, directory: Path) -> None:
    assert directory.is_dir(), directory
    mod = types.ModuleType(name)
    mod.__path__ = [str(directory)]
    mod.__package__ = name
    sys.modules[name] = mod


_synthetic_package("animatica_blender", ADDON_DIR)

_bpy = types.ModuleType("bpy")
_bpy.app = types.SimpleNamespace(online_access=True, version=(5, 0, 0))
sys.modules["bpy"] = _bpy

_properties = types.ModuleType("animatica_blender.properties")
_properties.CLOUD_API_URL = "https://api.animatica.ai"
sys.modules["animatica_blender.properties"] = _properties

from animatica_blender import http_gate as _http_gate  # noqa: E402

assert Path(_http_gate.__file__).resolve() == ADDON_DIR / "http_gate.py", (
    f"the gate must be the addon's real http_gate.py, got {_http_gate.__file__}")


@pytest.fixture
def bpy_app():
    """The stub's ``bpy.app``; flip ``online_access`` via ``monkeypatch.setattr``."""
    return _bpy.app


@pytest.fixture
def fake_server():
    from fake_server import FakeMmcpServer

    server = FakeMmcpServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()
