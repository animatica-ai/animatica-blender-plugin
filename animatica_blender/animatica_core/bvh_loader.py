"""BVH file parser (DCC-agnostic).

Parses the BVH HIERARCHY and MOTION sections, converts cm to meters,
converts ZYX Euler angles to rotation matrices, and computes world-space
joint positions via forward kinematics.

Returns the same normalised dict format as :func:`loader.load_motion_file`,
plus ``hierarchy`` (list of ``(name, parent_name)`` tuples) and
``rest_positions``.

Ported from ``maya_kimodo/maya_kimodo/bvh_loader.py``.
"""

import os

import numpy as np

from .constants import CM_TO_M, DEFAULT_FPS

_CM_TO_M = CM_TO_M


def parse_bvh(filepath):
    """Parse a .bvh file into a motion_data dict.

    Returns:
        dict with keys:
            posed_joints    - np.ndarray [T, J, 3] world-space, meters
            local_rot_mats  - np.ndarray [T, J, 3, 3]
            global_rot_mats - None
            foot_contacts   - None
            fps             - float
            num_frames      - int
            num_joints      - int
            rest_positions  - np.ndarray [J, 3] world-space rest, meters
            hierarchy       - list of (name, parent_name) tuples
            joint_names     - list[str]

    Raises:
        FileNotFoundError: If *filepath* does not exist.
        OSError: If the file cannot be read.
        ValueError: If the file is not a valid BVH (missing MOTION section).
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"BVH file not found: {filepath}")

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise OSError(f"Could not read BVH file {filepath!r}: {exc}") from exc

    if "MOTION" not in text:
        raise ValueError(f"Invalid BVH file (no MOTION section): {filepath}")

    hierarchy_text, _, motion_text = text.partition("MOTION")

    joints = _parse_hierarchy(hierarchy_text)
    num_frames, frame_time, channel_data = _parse_motion(motion_text, joints)

    fps = 1.0 / frame_time if frame_time > 0 else DEFAULT_FPS

    # The Kimodo BVH wraps everything in a synthetic "Root" joint at index 0
    # whose channels are always zero. Skip it -- work with Hips onward.
    root_wrapper_idx = None
    for i, jt in enumerate(joints):
        if jt["name"] == "Root" and jt["parent_idx"] is None:
            root_wrapper_idx = i
            break

    if root_wrapper_idx is not None:
        real_joints = [j for j in joints if j["name"] != "Root"]
        old_to_new = {}
        new_idx = 0
        for i, jt in enumerate(joints):
            if jt["name"] == "Root":
                continue
            old_to_new[i] = new_idx
            new_idx += 1

        for jt in real_joints:
            old_parent = jt["parent_idx"]
            if old_parent is None or old_parent == root_wrapper_idx:
                jt["parent_idx"] = None
            else:
                jt["parent_idx"] = old_to_new[old_parent]

        joints = real_joints

    num_joints = len(joints)

    offsets = np.zeros((num_joints, 3), dtype=np.float64)
    parents = np.full(num_joints, -1, dtype=np.int32)
    root_idx = -1

    for i, jt in enumerate(joints):
        offsets[i] = np.array(jt["offset"], dtype=np.float64) * _CM_TO_M
        if jt["parent_idx"] is not None:
            parents[i] = jt["parent_idx"]
        else:
            root_idx = i

    local_rot_eulers = np.zeros((num_frames, num_joints, 3), dtype=np.float64)
    root_translation = np.zeros((num_frames, 3), dtype=np.float64)

    for i, jt in enumerate(joints):
        ch_start = jt["channel_offset"]
        ch_names = jt["channels"]

        for frame_idx in range(num_frames):
            vals = channel_data[frame_idx]
            ch_vals = vals[ch_start:ch_start + len(ch_names)]

            pos_vals = {}
            rot_vals = {}
            for ci, cname in enumerate(ch_names):
                cl = cname.lower()
                if "position" in cl:
                    pos_vals[cl[0]] = ch_vals[ci]
                elif "rotation" in cl:
                    rot_vals[cl[0]] = ch_vals[ci]

            if pos_vals and i == root_idx:
                tx = pos_vals.get("x", 0.0) * _CM_TO_M
                ty = pos_vals.get("y", 0.0) * _CM_TO_M
                tz = pos_vals.get("z", 0.0) * _CM_TO_M
                root_translation[frame_idx] = [tx, ty, tz]

            # BVH channel order is Zrotation Yrotation Xrotation
            rz = rot_vals.get("z", 0.0)
            ry = rot_vals.get("y", 0.0)
            rx = rot_vals.get("x", 0.0)
            local_rot_eulers[frame_idx, i] = [rz, ry, rx]

    local_rot_mats = _euler_zyx_to_matrices(local_rot_eulers)

    posed_joints = _forward_kinematics(
        local_rot_mats, offsets, parents, root_idx, root_translation
    )

    rest_positions = np.zeros((num_joints, 3), dtype=np.float64)
    for j in range(num_joints):
        if parents[j] < 0:
            rest_positions[j] = offsets[j]
        else:
            rest_positions[j] = rest_positions[parents[j]] + offsets[j]

    joint_names = [jt["name"] for jt in joints]
    hierarchy = [
        (jt["name"], joints[jt["parent_idx"]]["name"] if jt["parent_idx"] is not None else None)
        for jt in joints
    ]

    return {
        "posed_joints": posed_joints.astype(np.float32),
        "local_rot_mats": local_rot_mats.astype(np.float32),
        "global_rot_mats": None,
        "foot_contacts": None,
        "fps": fps,
        "num_frames": num_frames,
        "num_joints": num_joints,
        "rest_positions": rest_positions.astype(np.float32),
        "hierarchy": hierarchy,
        "joint_names": joint_names,
    }


def _parse_hierarchy(text):
    """Parse the HIERARCHY section into a list of joint dicts.

    Each dict: {name, parent_idx, offset, channels, channel_offset}
    """
    joints = []
    parent_stack = []
    channel_offset = 0

    lines = text.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        tokens = line.split()

        if not tokens:
            i += 1
            continue

        if tokens[0] in ("ROOT", "JOINT"):
            name = tokens[1]
            parent_idx = parent_stack[-1] if parent_stack else None
            joint = {
                "name": name,
                "parent_idx": parent_idx,
                "offset": [0.0, 0.0, 0.0],
                "channels": [],
                "channel_offset": 0,
            }
            joint_idx = len(joints)
            joints.append(joint)
            parent_stack.append(joint_idx)

        elif tokens[0] == "OFFSET":
            if joints and parent_stack:
                joints[parent_stack[-1]]["offset"] = [
                    float(tokens[1]), float(tokens[2]), float(tokens[3])
                ]

        elif tokens[0] == "CHANNELS":
            if joints and parent_stack:
                num_ch = int(tokens[1])
                ch_names = tokens[2:2 + num_ch]
                joints[parent_stack[-1]]["channels"] = ch_names
                joints[parent_stack[-1]]["channel_offset"] = channel_offset
                channel_offset += num_ch

        elif tokens[0] == "}":
            if parent_stack:
                parent_stack.pop()

        elif tokens[0] == "End" and len(tokens) > 1 and tokens[1] == "Site":
            depth = 0
            i += 1
            while i < len(lines):
                sl = lines[i].strip()
                if "{" in sl:
                    depth += 1
                if "}" in sl:
                    depth -= 1
                    if depth <= 0:
                        break
                i += 1

        i += 1

    return joints


def _parse_motion(text, joints):
    """Parse the MOTION section. Returns (num_frames, frame_time, channel_data)."""
    lines = text.strip().splitlines()

    num_frames = 0
    frame_time = 0.0
    data_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Frames:"):
            num_frames = int(stripped.split(":")[1].strip())
        elif stripped.startswith("Frame Time:"):
            frame_time = float(stripped.split(":", 1)[1].strip())
            data_start = i + 1
            break

    channel_data = []
    for i in range(data_start, len(lines)):
        line = lines[i].strip()
        if not line:
            continue
        vals = [float(v) for v in line.split()]
        channel_data.append(vals)

    return num_frames, frame_time, channel_data


def _euler_zyx_to_matrices(eulers_deg):
    """Convert [T, J, 3] ZYX Euler angles (degrees) to [T, J, 3, 3] rotation matrices.

    R = Rz(z) * Ry(y) * Rx(x)
    eulers_deg[:,:,0] = Z, [:,:,1] = Y, [:,:,2] = X
    """
    T, J = eulers_deg.shape[:2]
    rad = np.radians(eulers_deg)

    cz, sz = np.cos(rad[:, :, 0]), np.sin(rad[:, :, 0])
    cy, sy = np.cos(rad[:, :, 1]), np.sin(rad[:, :, 1])
    cx, sx = np.cos(rad[:, :, 2]), np.sin(rad[:, :, 2])

    R = np.zeros((T, J, 3, 3), dtype=np.float64)

    R[:, :, 0, 0] = cz * cy
    R[:, :, 0, 1] = cz * sy * sx - sz * cx
    R[:, :, 0, 2] = cz * sy * cx + sz * sx
    R[:, :, 1, 0] = sz * cy
    R[:, :, 1, 1] = sz * sy * sx + cz * cx
    R[:, :, 1, 2] = sz * sy * cx - cz * sx
    R[:, :, 2, 0] = -sy
    R[:, :, 2, 1] = cy * sx
    R[:, :, 2, 2] = cy * cx

    return R


def _forward_kinematics(local_rot_mats, offsets, parents, root_idx, root_translation):
    """Compute world-space joint positions from local rotations and bone offsets.

    Args:
        local_rot_mats: [T, J, 3, 3]
        offsets: [J, 3] parent-relative bone offsets (meters)
        parents: [J] parent index (-1 for root)
        root_idx: index of the root joint
        root_translation: [T, 3] world-space root position (meters)

    Returns:
        posed_joints: [T, J, 3] world-space positions
    """
    T, J = local_rot_mats.shape[:2]

    global_transforms = np.zeros((T, J, 4, 4), dtype=np.float64)

    for j in range(J):
        R = local_rot_mats[:, j]
        t = offsets[j]

        local_T = np.zeros((T, 4, 4), dtype=np.float64)
        local_T[:, :3, :3] = R
        local_T[:, :3, 3] = t
        local_T[:, 3, 3] = 1.0

        if parents[j] < 0:
            local_T[:, :3, 3] = root_translation
            global_transforms[:, j] = local_T
        else:
            parent_T = global_transforms[:, parents[j]]
            global_transforms[:, j] = np.einsum("tij,tjk->tik", parent_T, local_T)

    posed_joints = global_transforms[:, :, :3, 3]
    return posed_joints
