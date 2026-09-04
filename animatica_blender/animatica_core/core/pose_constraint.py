"""Build full-body pose-constraint joint rotations from server ``motion_data``.

DCC-agnostic (no ``pyfbsdk``); lives under ``core/`` and is unit-testable.

Why this exists -- the Generate-Pose *auto-constraint* path cannot reliably
re-read the just-applied pose from the live MotionBuilder scene. Immediately
after ``animator.apply_single_pose`` ``KeyAdd``s the rotation curves (written
through a PreRotation conjugation ``REST.T @ R @ REST`` and a per-joint
rotation-order Euler keying), the local rotation matrix is not yet fully
evaluated, so a ``GetMatrix(kModelRotation, False)`` read attenuates deep joint
flexions (empirically: knees ~90 deg read back as ~10 deg). Root *translation*
reads fine -- it carries no PreRotation conjugation. The manual constraint path
avoids this only because the user reads the pose later, after evaluation has
settled.

So the auto path sources ``joint_rotations`` straight from the ``motion_data``
the server returned. This is provably convention-identical to the working manual
scene capture: ``local_rot_mats[0] -> quat[x,y,z,w]`` matched the manual
``FBMatrixToQuaternion`` capture with a sign-agnostic dot of 1.00000 across every
joint (debug captures 2026-06-28). ``root_position`` is left to the scene read,
which is already correct.

Quaternion order is ``[qx, qy, qz, qw]`` (identity ``[0, 0, 0, 1]``) -- the MMCP
wire order ``request_builder._marker_to_wire`` consumes. See CLAUDE.md.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _mat3_to_quat_xyzw(m) -> list[float]:
    """3x3 rotation matrix -> unit quaternion ``[qx, qy, qz, qw]``.

    Shepperd's method (branch on the largest diagonal term) for numerical
    stability. Mirrors ``bridge/animator._mat3_to_quat_wxyz`` but returns
    xyzw (wire order) and reads a numpy 3x3.
    """
    m00, m01, m02 = float(m[0][0]), float(m[0][1]), float(m[0][2])
    m10, m11, m12 = float(m[1][0]), float(m[1][1]), float(m[1][2])
    m20, m21, m22 = float(m[2][0]), float(m[2][1]), float(m[2][2])
    tr = m00 + m11 + m22
    if tr > 0.0:
        s = math.sqrt(tr + 1.0) * 2.0
        w = 0.25 * s
        x = (m21 - m12) / s
        y = (m02 - m20) / s
        z = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        w = (m21 - m12) / s
        x = 0.25 * s
        y = (m01 + m10) / s
        z = (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        w = (m02 - m20) / s
        x = (m01 + m10) / s
        y = 0.25 * s
        z = (m12 + m21) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        w = (m10 - m01) / s
        x = (m02 + m20) / s
        y = (m12 + m21) / s
        z = 0.25 * s
    return [x, y, z, w]


def joint_rotations_from_motion_data(motion_data: dict[str, Any]) -> dict[str, list[float]]:
    """``{joint_name: [qx, qy, qz, qw]}`` from frame 0 of ``local_rot_mats``.

    Names come from ``motion_data['joint_names']`` (the canonical order the server
    returned). Returns ``{}`` if ``local_rot_mats`` is absent/empty so the caller
    can fall back to the scene capture rather than crash.
    """
    lrm = motion_data.get("local_rot_mats")
    if lrm is None:
        return {}
    arr = np.asarray(lrm)
    if arr.ndim != 4 or arr.shape[0] < 1 or arr.shape[-2:] != (3, 3):
        return {}
    frame0 = arr[0]
    names = motion_data.get("joint_names") or []
    out: dict[str, list[float]] = {}
    for i, name in enumerate(names):
        if i < frame0.shape[0]:
            out[str(name)] = _mat3_to_quat_xyzw(frame0[i])
    return out
