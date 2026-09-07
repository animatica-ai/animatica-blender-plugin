"""Motion-file loader dispatch (DCC-agnostic).

Reads ``.npz`` / ``.bvh`` / ``.gltf`` / ``.glb`` files and returns a uniform
``motion_data`` dict consumed by :mod:`bridge.builder` and
:mod:`bridge.animator`.
"""

import json
import os


def load_motion_file(filepath):
    """Dispatch to the correct parser by file extension.

    Returns a ``motion_data`` dict with at least:
        ``posed_joints``   [T, J, 3] world-space positions (meters)
        ``local_rot_mats`` [T, J, 3, 3] local rotation matrices (or None)
        ``fps``            float
        ``num_frames``     int
        ``num_joints``     int

    BVH and glTF variants additionally include ``joint_names``.
    NPZ files do not embed joint names; callers should inject them from
    the skeleton registry when needed.
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".npz":
        from .core.loader import load_npz
        return load_npz(filepath)

    elif ext == ".bvh":
        from .bvh_loader import parse_bvh
        return parse_bvh(filepath)

    elif ext in (".gltf", ".glb"):
        from .gltf_parser import parse_gltf
        with open(filepath, "r", encoding="utf-8") as fh:
            gltf_doc = json.load(fh)
        return parse_gltf(gltf_doc)

    else:
        raise ValueError(
            f"Unsupported format: {ext!r}. Expected .npz, .bvh, .gltf, or .glb"
        )
