"""Low-level NPZ / motion-array IO.

Ported from ``maya_kimodo/maya_kimodo/loader.py``.  DCC-agnostic: no pyfbsdk,
no Maya imports.  Returns numpy arrays in the standard ``motion_data`` dict
consumed by :mod:`animatica_core.bridge.animator`.
"""

import io

import numpy as np

from ..constants import DEFAULT_FPS


def load_npz(path):
    """Load a Animatica/Kimodo NPZ motion file from *path*.

    Handles both legacy key names (``joints_pos`` / ``joints_rot``) and
    current key names (``posed_joints`` / ``local_rot_mats``).
    """
    data = np.load(path, allow_pickle=True)
    return _normalise_npz(data)


def load_npz_from_bytes(npz_bytes):
    """Load an NPZ motion file from in-memory *npz_bytes* (``bytes``)."""
    data = np.load(io.BytesIO(npz_bytes), allow_pickle=True)
    return _normalise_npz(data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _normalise_npz(data):
    """Shared normalisation for NPZ data loaded from file or bytes."""
    posed_joints = _get_array(data, "posed_joints", "joints_pos")
    if posed_joints is None:
        raise ValueError(
            f"NPZ has no 'posed_joints' or 'joints_pos' key. "
            f"Available: {list(data.files)}"
        )

    # Strip leading batch dimension if present (shape [1, T, J, 3])
    if posed_joints.ndim == 4:
        posed_joints = posed_joints[0]
    if posed_joints.ndim != 3 or posed_joints.shape[-1] != 3:
        raise ValueError(
            f"Expected posed_joints shape [T, J, 3], got {posed_joints.shape}"
        )

    local_rot_mats = _get_array(data, "local_rot_mats")
    if local_rot_mats is not None and local_rot_mats.ndim == 5:
        local_rot_mats = local_rot_mats[0]

    global_rot_mats = _get_array(data, "global_rot_mats", "joints_rot")
    if global_rot_mats is not None and global_rot_mats.ndim == 5:
        global_rot_mats = global_rot_mats[0]

    foot_contacts = _get_array(data, "foot_contacts")
    if foot_contacts is not None and foot_contacts.ndim == 3:
        foot_contacts = foot_contacts[0]

    fps = float(data["fps"]) if "fps" in data.files else DEFAULT_FPS
    num_frames, num_joints = posed_joints.shape[:2]

    return {
        "posed_joints":    posed_joints,
        "local_rot_mats":  local_rot_mats,
        "global_rot_mats": global_rot_mats,
        "foot_contacts":   foot_contacts,
        "fps":             fps,
        "num_frames":      num_frames,
        "num_joints":      num_joints,
        # joint_names not stored in NPZ; caller resolves via skeleton registry
    }


def _get_array(data, *keys):
    """Return the first matching array from *keys*, or ``None``."""
    for k in keys:
        if k in data.files:
            return data[k]
    return None
