"""Coordinate conversion between MMCP wire format (right-handed Y-up) and
Blender (right-handed Z-up).

The whole protocol is in MMCP frame; the addon converts at the boundary —
once outbound (when building requests) and once inbound (when baking
responses or importing the canonical skeleton).

Conversions (verified against docs/concepts.html#coordinates):

    MMCP → Blender (inbound):
        position    (x, y, z)        -> (x, -z,  y)
        quaternion  (qx, qy, qz, qw) -> (qx, -qz, qy, qw)

    Blender → MMCP (outbound):
        position    (x, y, z)        -> (x,  z, -y)
        quaternion  (qx, qy, qz, qw) -> (qx,  qz, -qy, qw)
"""

from __future__ import annotations

from typing import Sequence, Tuple


Vec3 = Tuple[float, float, float]
Quat = Tuple[float, float, float, float]   # always (x, y, z, w) — MMCP order


# ---------------------------------------------------------------------------
# MMCP → Blender (inbound)
# ---------------------------------------------------------------------------

def mmcp_pos_to_blender(p: Sequence[float]) -> Vec3:
    x, y, z = p[0], p[1], p[2]
    return (x, -z, y)


def mmcp_quat_to_blender(q: Sequence[float]) -> Quat:
    qx, qy, qz, qw = q[0], q[1], q[2], q[3]
    return (qx, -qz, qy, qw)


# ---------------------------------------------------------------------------
# Blender → MMCP (outbound)
# ---------------------------------------------------------------------------

def blender_pos_to_mmcp(p: Sequence[float]) -> Vec3:
    x, y, z = p[0], p[1], p[2]
    return (x, z, -y)


def blender_quat_to_mmcp(q: Sequence[float]) -> Quat:
    """Blender quaternions on pose bones are stored as (w, x, y, z); callers
    must reorder to (x, y, z, w) *before* calling this. See request_builder.py
    for the conversion helper.
    """
    qx, qy, qz, qw = q[0], q[1], q[2], q[3]
    return (qx, qz, -qy, qw)


# ---------------------------------------------------------------------------
# The armature object's rotation
# ---------------------------------------------------------------------------

def split_world_yaw(matrix_world):
    """Split an object's world rotation into ``(yaw, tilt)``: 3×3 matrices
    with ``rotation == yaw @ tilt``, where ``yaw`` is the turn about world Z
    and ``tilt`` is what remains (a Mixamo import's 90° about X, say).

    The server does not see which way the rig was turned in object mode: it
    generates in world space with its own heading, and the joint rotations
    it returns are relative to the rig's rest pose as it stands at yaw 0.
    So the bake conjugates rotations by ``tilt`` alone and undoes the yaw
    once, on the root, and the outbound pose keyframes do the reverse. The
    two must stay in sync (``gltf_to_blender._RotationBaker``,
    ``constraints_ui._joint_rotation_to_mmcp``).
    """
    from mathutils import Quaternion  # noqa: PLC0415 — importable outside Blender

    q = matrix_world.to_quaternion()
    twist = Quaternion((q.w, 0.0, 0.0, q.z))
    if twist.magnitude < 1e-9:
        # Upside down (180° about a horizontal axis): no yaw to speak of.
        twist = Quaternion((1.0, 0.0, 0.0, 0.0))
    twist.normalize()
    yaw = twist.to_matrix()
    tilt = yaw.transposed() @ q.to_matrix()
    return yaw, tilt
