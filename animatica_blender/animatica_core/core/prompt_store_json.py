"""JSON sidecar persistence for ``AppState``.

Default sidecar path is ``<scene>.animatica.json`` next to the .fbx. The
file format is a direct JSON dump of ``prompt_model.to_dict(state)``.

Pure-Python; no pyfbsdk. The caller (the MoBu UI layer) is responsible
for resolving the scene path -- this module only takes/returns paths.
"""

import json
import os
from pathlib import Path

from .prompt_model import AppState, from_dict, to_dict


SIDECAR_SUFFIX = ".animatica.json"


def sidecar_path_for(scene_path: str) -> str:
    """Return the sidecar path that pairs with ``scene_path``.

    For ``foo/bar.fbx`` -> ``foo/bar.animatica.json``. If ``scene_path``
    is empty or unsaved (no extension), returns an empty string so the
    caller can fall back to a user-picked path.
    """
    if not scene_path:
        return ""
    p = Path(scene_path)
    if not p.suffix:
        return ""
    return str(p.with_suffix(SIDECAR_SUFFIX))


def save(state: AppState, path: str) -> None:
    data = to_dict(state)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load(path: str) -> AppState:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return from_dict(data)


def load_or_default(path: str) -> AppState:
    """Load if the file exists; otherwise return a fresh ``AppState``."""
    if path and os.path.exists(path):
        return load(path)
    return AppState()
