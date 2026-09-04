"""glTF 2.0 → motion_data parser for the MMCP server response.

``mmcp_client.generate()`` returns a parsed glTF 2.0 JSON document (Python
dict).  ``parse_gltf`` extracts the skeleton animation tracks and returns the
standard ``motion_data`` dict consumed by
:mod:`animatica_core.bridge.animator`.

glTF animation conventions used by the MMCP server:
  - Each ``animations[]`` entry is one sample (variant): with ``num_samples > 1``
    the server returns N entries, all sharing the same node/channel structure.
    ``parse_gltf`` returns the first; ``parse_gltf_samples`` returns all N.
  - ``channels[*].target.path == "rotation"``   → XYZW quaternions, local space.
  - ``channels[*].target.path == "translation"`` → XYZ meters, world space
    (root joint only).
  - Buffer data is embedded as base64 data URIs.
  - Node names are the SOMA-77 joint names.
  - The ``MMCP_motion`` extension pairs ``samples[i]`` with ``animations[i]``;
    ``samples[i].foot_contacts`` is a ``dict[str, list[bool]]`` keyed by joint
    name (verified live; absent from some older captures — never required).
  - ``nodes[].translation`` is the LOCAL rest offset in meters; world-space
    rest positions are accumulated down ``nodes[].children``, applying
    ``nodes[].rotation`` to child offsets when non-identity.

No external dependencies beyond numpy.
"""

import base64

import numpy as np


# glTF component-type enum → numpy dtype
_COMPONENT_DTYPE = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}

# glTF accessor type → number of components per element
_TYPE_N = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_gltf(gltf_doc):
    """Parse a glTF 2.0 document and return the first sample's ``motion_data`` dict.

    *gltf_doc* is the Python dict returned by ``mmcp_client.generate()``.
    When the server returns multiple samples (``num_samples > 1``) this returns
    only the first; use :func:`parse_gltf_samples` to get every sample.

    Returns:
        dict with keys:
            ``local_rot_mats`` – np.ndarray [T, J, 3, 3]
            ``posed_joints``   – np.ndarray [T, J, 3]  world positions, meters
            ``fps``            – float
            ``num_frames``     – int
            ``num_joints``     – int
            ``joint_names``    – list[str]  SOMA-77 names in joint-index order
            ``foot_contacts``  – np.ndarray [T, J] bool, aligned to
                                 ``joint_names``; all-``False`` columns for
                                 joints the server did not report
            ``hierarchy``      – list[(joint, parent_or_None)]  joint *names*,
                                 in ``joint_names`` order
            ``rest_positions`` – dict[name → (x, y, z)]  world-space rest,
                                 meters (accumulated, not the local offsets)
    """
    return parse_gltf_samples(gltf_doc)[0]


def parse_gltf_samples(gltf_doc):
    """Parse a glTF 2.0 document into one ``motion_data`` dict per sample.

    The MMCP server encodes ``num_samples`` motion variants as N entries in the
    ``animations[]`` array, all sharing the same node/channel structure and
    buffers. Returns one ``motion_data`` dict (same shape as :func:`parse_gltf`)
    per entry, in ``animations[]`` order. Ports the per-sample loop from
    ``maya_kimodo/gltf_parser.py:414-452``.

    Foot contacts come from the ``MMCP_motion`` extension, whose ``samples[i]``
    pairs with ``animations[i]``. Degrades to no contacts (never raises) when
    the block is absent or the lengths disagree — the 2026-05-21 captures in
    ``doc/debug/`` have samples without a ``foot_contacts`` key.

    Returns:
        list[dict] – one motion_data dict per ``animations[]`` entry.

    Raises:
        ValueError – if the document has no animations, or an animation has no
            rotation channels.
    """
    if not gltf_doc.get("animations"):
        raise ValueError("glTF document contains no animations")

    raw_buffers = _decode_buffers(gltf_doc.get("buffers", []))
    nodes       = gltf_doc.get("nodes", [])
    anims       = gltf_doc["animations"]

    ext_samples = (gltf_doc.get("extensions", {})
                           .get("MMCP_motion", {})
                           .get("samples", []))
    if not isinstance(ext_samples, list) or len(ext_samples) != len(anims):
        ext_samples = [{}] * len(anims)   # absent / mismatched → no contacts

    def read_acc(idx):
        return _read_accessor(gltf_doc, raw_buffers, idx)

    return [
        _parse_animation(
            anim, nodes, read_acc,
            sample.get("foot_contacts") if isinstance(sample, dict) else None,
        )
        for anim, sample in zip(anims, ext_samples)
    ]


def _parse_animation(anim, nodes, read_acc, contacts=None):
    """Parse one ``animations[]`` entry into a ``motion_data`` dict.

    *contacts* is the sample's raw ``foot_contacts`` wire dict
    (``dict[str, list[bool]]`` keyed by joint name) or ``None``.
    """
    rot_tracks   = {}  # node_idx -> (times [T], xyzw [T, 4])
    trans_tracks = {}  # node_idx -> (times [T], xyz  [T, 3])

    for ch in anim["channels"]:
        s  = anim["samplers"][ch["sampler"]]
        ni = ch["target"]["node"]
        times  = read_acc(s["input"]).ravel().astype(np.float32)
        values = read_acc(s["output"])
        path   = ch["target"]["path"]
        if path == "rotation":
            rot_tracks[ni] = (times, values.astype(np.float32))
        elif path == "translation":
            trans_tracks[ni] = (times, values.astype(np.float32))

    if not rot_tracks:
        raise ValueError("glTF animation has no rotation channels")

    sorted_nodes = sorted(rot_tracks)
    first_times  = rot_tracks[sorted_nodes[0]][0]
    num_frames   = int(first_times.shape[0])
    num_joints   = len(sorted_nodes)

    joint_names = [
        nodes[ni]["name"] if ni < len(nodes) else str(ni)
        for ni in sorted_nodes
    ]

    local_rot_mats = np.zeros((num_frames, num_joints, 3, 3), dtype=np.float32)
    posed_joints   = np.zeros((num_frames, num_joints, 3),    dtype=np.float32)

    for j, ni in enumerate(sorted_nodes):
        _, xyzw = rot_tracks[ni]
        local_rot_mats[:, j] = _xyzw_to_rotmat(xyzw)
        if ni in trans_tracks:
            posed_joints[:, j] = trans_tracks[ni][1]

    # Infer FPS from uniform time steps; fall back to 30 when ambiguous.
    if num_frames > 1:
        dt  = float(first_times[-1] - first_times[0]) / (num_frames - 1)
        fps = 1.0 / dt if dt > 1e-9 else 30.0
    else:
        fps = 30.0

    # Foot contacts: mapped BY NAME from the wire dict into joint_names order,
    # never by position — the full-joint bool axis avoids inventing a second
    # column-ordering convention. Unknown names / length mismatches degrade to
    # an all-False column, never raise.
    foot_contacts = np.zeros((num_frames, num_joints), dtype=bool)
    if isinstance(contacts, dict):
        col = {name: j for j, name in enumerate(joint_names)}
        for name, flags in contacts.items():
            j = col.get(name)
            if j is None or not isinstance(flags, (list, tuple)) \
                    or len(flags) != num_frames:
                continue
            foot_contacts[:, j] = np.asarray(flags, dtype=bool)

    hierarchy, rest_positions = _node_rest_geometry(nodes, sorted_nodes, joint_names)

    return {
        "local_rot_mats": local_rot_mats,
        "posed_joints":   posed_joints,
        "fps":            fps,
        "num_frames":     num_frames,
        "num_joints":     num_joints,
        "joint_names":    joint_names,
        "foot_contacts":  foot_contacts,
        "hierarchy":      hierarchy,
        "rest_positions": rest_positions,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _node_rest_geometry(nodes, sorted_nodes, joint_names):
    """Joint hierarchy + world-space rest positions from the glTF ``nodes[]``.

    ``nodes[].translation`` is the *local* rest offset (meters), so world-space
    rest positions are accumulated down the ``children`` graph, applying each
    node's rest ``rotation`` to the child offset when it is non-identity (all
    nine in-repo captures carry identity rest rotations, but the accumulation
    must not assume it). ``children`` is in *node*-index space; joint index
    ``j`` maps to node ``sorted_nodes[j]``, and hierarchy parents are joint
    *names* — a parent node that is not itself animated is walked through to
    the nearest animated ancestor, the root mapping to ``None``.

    Returns:
        ``(hierarchy, rest_positions)`` per the CLAUDE.md ``motion_data``
        contract — ``list[tuple[str, str | None]]`` in ``joint_names`` order,
        and ``dict[str, tuple[float, float, float]]`` world-space meters.
    """
    parent_of = {}
    for pi, node in enumerate(nodes):
        for ci in node.get("children", []):
            if isinstance(ci, int) and 0 <= ci < len(nodes):
                parent_of[ci] = pi

    world_pos = {}   # node_idx -> np.ndarray [3]   world rest position
    world_rot = {}   # node_idx -> np.ndarray [3,3] world rest rotation

    def accumulate(ni):
        """Memoised root-to-leaf accumulation for node *ni* and its ancestors."""
        chain = []
        while ni is not None and ni not in world_pos:
            chain.append(ni)
            ni = parent_of.get(ni)
            if ni in chain:          # cycle guard: sever, treat as root
                ni = None
        for ci in reversed(chain):
            node = nodes[ci]
            t = np.asarray(node.get("translation", (0.0, 0.0, 0.0)), dtype=np.float64)
            q = np.asarray([node.get("rotation", (0.0, 0.0, 0.0, 1.0))], dtype=np.float64)
            R = _xyzw_to_rotmat(q)[0].astype(np.float64)
            pi = parent_of.get(ci)
            if pi is None or pi not in world_pos:
                world_pos[ci] = t
                world_rot[ci] = R
            else:
                world_pos[ci] = world_pos[pi] + world_rot[pi] @ t
                world_rot[ci] = world_rot[pi] @ R

    node_joint = {ni: joint_names[j] for j, ni in enumerate(sorted_nodes)}

    hierarchy      = []
    rest_positions = {}
    for j, ni in enumerate(sorted_nodes):
        name = joint_names[j]
        if not (0 <= ni < len(nodes)):
            hierarchy.append((name, None))
            rest_positions[name] = (0.0, 0.0, 0.0)
            continue
        accumulate(ni)
        pi   = parent_of.get(ni)
        seen = set()
        while pi is not None and pi not in node_joint and pi not in seen:
            seen.add(pi)
            pi = parent_of.get(pi)
        hierarchy.append((name, node_joint.get(pi)))
        rest_positions[name] = tuple(float(v) for v in world_pos[ni])
    return hierarchy, rest_positions


def _decode_buffers(buffers):
    """Return a list of ``bytes`` objects, one per glTF buffer."""
    raw = []
    for buf in buffers:
        uri = buf.get("uri", "")
        if uri.startswith("data:"):
            _, data_part = uri.split(",", 1)
            raw.append(base64.b64decode(data_part))
        else:
            raw.append(b"")  # external URIs not supported in this context
    return raw


def _read_accessor(gltf_doc, raw_buffers, idx):
    """Decode accessor *idx* and return a numpy array."""
    acc  = gltf_doc["accessors"][idx]
    bv   = gltf_doc["bufferViews"][acc["bufferView"]]
    buf  = raw_buffers[bv["buffer"]]

    byte_offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count       = acc["count"]
    dtype       = _COMPONENT_DTYPE[acc["componentType"]]
    n_comp      = _TYPE_N[acc["type"]]

    item_bytes = np.dtype(dtype).itemsize * n_comp
    raw        = buf[byte_offset: byte_offset + count * item_bytes]
    arr        = np.frombuffer(raw, dtype=dtype)
    if n_comp > 1:
        arr = arr.reshape(count, n_comp)
    return arr


def _xyzw_to_rotmat(xyzw):
    """Convert [N, 4] XYZW quaternions to [N, 3, 3] rotation matrices."""
    x = xyzw[:, 0].astype(np.float64)
    y = xyzw[:, 1].astype(np.float64)
    z = xyzw[:, 2].astype(np.float64)
    w = xyzw[:, 3].astype(np.float64)

    R = np.empty((len(xyzw), 3, 3), dtype=np.float32)
    R[:, 0, 0] = (1 - 2*(y*y + z*z)).astype(np.float32)
    R[:, 0, 1] = (2*(x*y - z*w)).astype(np.float32)
    R[:, 0, 2] = (2*(x*z + y*w)).astype(np.float32)
    R[:, 1, 0] = (2*(x*y + z*w)).astype(np.float32)
    R[:, 1, 1] = (1 - 2*(x*x + z*z)).astype(np.float32)
    R[:, 1, 2] = (2*(y*z - x*w)).astype(np.float32)
    R[:, 2, 0] = (2*(x*z - y*w)).astype(np.float32)
    R[:, 2, 1] = (2*(y*z + x*w)).astype(np.float32)
    R[:, 2, 2] = (1 - 2*(x*x + y*y)).astype(np.float32)
    return R
