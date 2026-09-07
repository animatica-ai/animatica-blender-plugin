"""Pack pose snapshots into Kimodo's constraint JSON format.

Copied verbatim from maya_kimodo/maya_kimodo/constraints.py — this is the
server contract consumed by the MMCP /generate endpoint. Pure-Python; no
DCC imports so it stays callable from headless tests.

Snapshots use Euler XYZ in degrees and root worldspace in meters; this
module converts to Kimodo's local axis-angle (radians) and packs the
trajectory.
"""

import json

import numpy as np


def _euler_xyz_deg_to_rotmat(rx_deg, ry_deg, rz_deg):
    """R = Rz(rz) * Ry(ry) * Rx(rx), matching rotateOrder=XYZ."""
    rx, ry, rz = np.radians([rx_deg, ry_deg, rz_deg])
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


def _rotmat_to_axis_angle(R):
    """Rodrigues inverse. Returns a length-3 axis*angle vector (radians)."""
    cos_theta = np.clip((np.trace(R) - 1.0) * 0.5, -1.0, 1.0)
    theta = np.arccos(cos_theta)
    if theta < 1e-6:
        return np.zeros(3, dtype=np.float32)
    if np.pi - theta < 1e-6:
        diag = np.array([R[0, 0], R[1, 1], R[2, 2]])
        k = int(np.argmax(diag))
        axis = np.zeros(3)
        axis[k] = np.sqrt(max(0.0, (diag[k] + 1.0) * 0.5))
        other = [i for i in range(3) if i != k]
        axis[other[0]] = R[k, other[0]] / (2.0 * axis[k]) if axis[k] > 1e-8 else 0.0
        axis[other[1]] = R[k, other[1]] / (2.0 * axis[k]) if axis[k] > 1e-8 else 0.0
        return (axis * theta).astype(np.float32)
    axis = np.array([
        R[2, 1] - R[1, 2],
        R[0, 2] - R[2, 0],
        R[1, 0] - R[0, 1],
    ]) / (2.0 * np.sin(theta))
    return (axis * theta).astype(np.float32)


def pack_fullbody(snapshots, joint_order):
    """Build a ``fullbody`` constraint dict from pose snapshots.

    snapshots: list of ``{"frame": int,
                          "local_eulers_deg": {joint_name: (rx, ry, rz)},
                          "root_pos_m": (x, y, z)}``.
    joint_order: ordered list of joint names (from ``skeleton.get_joint_hierarchy``).
    """
    ordered = sorted(snapshots, key=lambda s: s["frame"])
    frames = [int(s["frame"]) for s in ordered]
    T = len(ordered)
    J = len(joint_order)
    rots = np.zeros((T, J, 3), dtype=np.float32)
    roots = np.zeros((T, 3), dtype=np.float32)
    for t, snap in enumerate(ordered):
        eulers = snap.get("local_eulers_deg", {})
        for j, name in enumerate(joint_order):
            rx, ry, rz = eulers.get(name, (0.0, 0.0, 0.0))
            R = _euler_xyz_deg_to_rotmat(rx, ry, rz)
            rots[t, j] = _rotmat_to_axis_angle(R)
        px, py, pz = snap["root_pos_m"]
        roots[t] = (px, py, pz)
    return {
        "type": "fullbody",
        "frame_indices": frames,
        "local_joints_rot": rots.tolist(),
        "root_positions": roots.tolist(),
    }


END_EFFECTOR_TYPES = ("left-foot", "right-foot", "left-hand", "right-hand")


def pack_end_effector(snapshots, joint_order, kimodo_type):
    """Re-use the ``fullbody`` payload for an end-effector constraint.

    Kimodo's EndEffectorConstraintSet consumes the same schema as
    FullBodyConstraintSet and filters to the named joint internally. Only
    the ``type`` string differs.
    """
    if kimodo_type not in END_EFFECTOR_TYPES:
        raise ValueError(
            f"kimodo_type must be one of {END_EFFECTOR_TYPES}, got {kimodo_type!r}"
        )
    out = pack_fullbody(snapshots, joint_order)
    out["type"] = kimodo_type
    return out


def pack_root2d(snapshots):
    """Build a ``root2d`` constraint from root XZ of the snapshots."""
    ordered = sorted(snapshots, key=lambda s: s["frame"])
    frames = [int(s["frame"]) for s in ordered]
    xz = [[float(s["root_pos_m"][0]), float(s["root_pos_m"][2])] for s in ordered]
    return {
        "type": "root2d",
        "frame_indices": frames,
        "smooth_root_2d": xz,
    }


def save_constraints(path, constraint_lst):
    with open(path, "w") as f:
        json.dump(constraint_lst, f)
