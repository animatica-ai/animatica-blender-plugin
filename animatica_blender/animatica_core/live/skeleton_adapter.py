"""Handshake skeleton block -> builder inputs. Pure Python, unit-tested.

The ``/stream/ardy/start`` handshake carries the model's canonical skeleton
in the same MMCP ``Skeleton`` shape as ``/capabilities``::

    {"joints": [{"name": ..., "parent": <name|None>,
                 "rest_translation": [x, y, z],       # parent-local, meters
                 "rest_rotation": [x, y, z, w]}, ...]}

The server derives rest_translation by subtracting global rest positions
and emits identity rest rotations (kimodo/mmcp/capabilities.py:80), so
world positions are the simple prefix sum down the hierarchy --
``builder.build_neutral_skeleton`` wants exactly those (world, meters).
"""

from __future__ import annotations

_IDENTITY_TOL = 1e-4


def skeleton_block_to_hierarchy(skeleton_block):
    """MMCP skeleton dict -> ``(hierarchy, rest_positions, joint_names)``.

    * ``hierarchy``      -- ``[(joint_name, parent_name_or_None), ...]`` in
      the block's order (the server emits parents before children);
    * ``rest_positions`` -- ``{joint_name: (x, y, z)}`` world-space meters;
    * ``joint_names``    -- names in block order == the order of the
      per-frame ``rotations`` arrays on the wire.

    Raises ``ValueError`` on a malformed block (unknown parent, duplicate
    name, non-identity rest rotation -- F1 supports the canonical skeleton
    only; see plan D10).
    """
    joints = skeleton_block.get("joints") or []
    if not joints:
        raise ValueError("skeleton block has no joints")

    hierarchy = []
    rest_positions = {}
    joint_names = []
    for j in joints:
        name = j["name"]
        parent = j.get("parent")
        if name in rest_positions:
            raise ValueError(f"duplicate joint name {name!r}")

        rot = j.get("rest_rotation") or (0.0, 0.0, 0.0, 1.0)
        qx, qy, qz, qw = (float(v) for v in rot)
        if (abs(qx) > _IDENTITY_TOL or abs(qy) > _IDENTITY_TOL
                or abs(qz) > _IDENTITY_TOL or abs(abs(qw) - 1.0) > _IDENTITY_TOL):
            raise ValueError(
                f"joint {name!r} has a non-identity rest rotation; the live "
                "client supports the canonical (identity-rest) skeleton only")

        off = j.get("rest_translation") or (0.0, 0.0, 0.0)
        ox, oy, oz = (float(v) for v in off)
        if parent is None:
            world = (ox, oy, oz)
        else:
            if parent not in rest_positions:
                raise ValueError(
                    f"joint {name!r} references parent {parent!r} before "
                    "its definition (block must be parent-before-child)")
            px, py, pz = rest_positions[parent]
            world = (px + ox, py + oy, pz + oz)

        hierarchy.append((name, parent))
        rest_positions[name] = world
        joint_names.append(name)

    roots = [n for n, p in hierarchy if p is None]
    if len(roots) != 1:
        raise ValueError(f"expected exactly one root joint, got {roots!r}")
    if hierarchy[0][1] is not None:
        raise ValueError("first joint in the block must be the root")

    return hierarchy, rest_positions, joint_names


def hip_height_from_positions(rest_positions, hierarchy):
    """Root rest height in meters -- feeds builder's ``hip_height``."""
    root_name = next(n for n, p in hierarchy if p is None)
    return float(rest_positions[root_name][1])
