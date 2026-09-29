# SPDX-License-Identifier: Apache-2.0
"""MotionResult → glTF 2.0 JSON document.

Builds a self-contained glTF (buffers embedded as ``data:`` URIs) with one
animation per generated sample. Each animation has dense per-frame ``LINEAR``
channels: a quaternion track per joint, plus a translation track on the root.

The MMCP-specific metadata (model id, fps, foot contacts, sample frame counts,
chunk boundaries, travel trajectories) lives in ``extensions.MMCP_motion``.
"""

from __future__ import annotations

import base64
from typing import Any, Sequence

import numpy as np

from .protocol import PROTOCOL_VERSION


GLTF_GENERATOR = "motionmcp"
GLTF_VERSION = "2.0"
MMCP_VERSION = PROTOCOL_VERSION

# glTF componentType
_FLOAT = 5126

# Output array dtype for buffers — must be little-endian for glTF.
_F32 = np.dtype("<f4")


# ---- Math helpers ---------------------------------------------------------

def matrices_to_quats(m: np.ndarray) -> np.ndarray:
    """Convert ``(..., 3, 3)`` rotation matrices to ``(..., 4)`` ``(x, y, z, w)``.

    Uses Shepperd's branched method, vectorised over the leading dims, with a
    final renormalisation to absorb small departures from orthonormality (FK
    chains can produce slightly off matrices).
    """
    if m.shape[-2:] != (3, 3):
        raise ValueError(f"expected (..., 3, 3), got {m.shape}")

    m00 = m[..., 0, 0]; m11 = m[..., 1, 1]; m22 = m[..., 2, 2]
    m01 = m[..., 0, 1]; m02 = m[..., 0, 2]; m12 = m[..., 1, 2]
    m10 = m[..., 1, 0]; m20 = m[..., 2, 0]; m21 = m[..., 2, 1]
    trace = m00 + m11 + m22

    qx = np.empty(m.shape[:-2], dtype=np.float32)
    qy = np.empty_like(qx)
    qz = np.empty_like(qx)
    qw = np.empty_like(qx)

    # Branch A: trace > 0
    a = trace > 0
    s = np.sqrt(np.where(a, trace + 1.0, 1.0)) * 2.0
    qw_a = 0.25 * s
    qx_a = (m21 - m12) / s
    qy_a = (m02 - m20) / s
    qz_a = (m10 - m01) / s

    # Branch B: m00 is largest diagonal
    b = (~a) & (m00 >= m11) & (m00 >= m22)
    s = np.sqrt(np.where(b, 1.0 + m00 - m11 - m22, 1.0)) * 2.0
    qw_b = (m21 - m12) / s
    qx_b = 0.25 * s
    qy_b = (m01 + m10) / s
    qz_b = (m02 + m20) / s

    # Branch C: m11 is largest diagonal
    c = (~a) & (~b) & (m11 >= m22)
    s = np.sqrt(np.where(c, 1.0 + m11 - m00 - m22, 1.0)) * 2.0
    qw_c = (m02 - m20) / s
    qx_c = (m01 + m10) / s
    qy_c = 0.25 * s
    qz_c = (m12 + m21) / s

    # Branch D: m22 is largest diagonal
    d = (~a) & (~b) & (~c)
    s = np.sqrt(np.where(d, 1.0 + m22 - m00 - m11, 1.0)) * 2.0
    qw_d = (m10 - m01) / s
    qx_d = (m02 + m20) / s
    qy_d = (m12 + m21) / s
    qz_d = 0.25 * s

    qx[:] = np.where(a, qx_a, np.where(b, qx_b, np.where(c, qx_c, qx_d)))
    qy[:] = np.where(a, qy_a, np.where(b, qy_b, np.where(c, qy_c, qy_d)))
    qz[:] = np.where(a, qz_a, np.where(b, qz_b, np.where(c, qz_c, qz_d)))
    qw[:] = np.where(a, qw_a, np.where(b, qw_b, np.where(c, qw_c, qw_d)))

    out = np.stack([qx, qy, qz, qw], axis=-1).astype(np.float32)
    out /= np.maximum(np.linalg.norm(out, axis=-1, keepdims=True), 1e-8)
    return out


# ---- Buffer assembly ------------------------------------------------------

class _BufferBuilder:
    """Accumulates float32 chunks into one binary buffer + accessors / bufferViews."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._offset = 0
        self.buffer_views: list[dict[str, Any]] = []
        self.accessors: list[dict[str, Any]] = []

    def add(
        self,
        data: np.ndarray,
        *,
        kind: str,                      # "SCALAR" | "VEC3" | "VEC4"
        with_min_max: bool = False,
    ) -> int:
        """Append a float32 array, return the new accessor index."""
        arr = np.ascontiguousarray(data, dtype=_F32)
        chunk = arr.tobytes()

        view_idx = len(self.buffer_views)
        self.buffer_views.append({
            "buffer":     0,
            "byteOffset": self._offset,
            "byteLength": len(chunk),
        })
        self._chunks.append(chunk)
        self._offset += len(chunk)
        # 4-byte align (float32 already aligned but keep this for future types).
        pad = (-self._offset) % 4
        if pad:
            self._chunks.append(b"\x00" * pad)
            self._offset += pad

        accessor: dict[str, Any] = {
            "bufferView":    view_idx,
            "componentType": _FLOAT,
            "count":         int(arr.shape[0]),
            "type":          kind,
        }
        if with_min_max and arr.size:
            if arr.ndim == 1:
                accessor["min"] = [float(arr.min())]
                accessor["max"] = [float(arr.max())]
            else:
                accessor["min"] = [float(v) for v in arr.min(axis=0)]
                accessor["max"] = [float(v) for v in arr.max(axis=0)]
        self.accessors.append(accessor)
        return len(self.accessors) - 1

    def to_data_uri(self) -> tuple[str, int]:
        blob = b"".join(self._chunks)
        uri = "data:application/octet-stream;base64," + base64.b64encode(blob).decode("ascii")
        return uri, len(blob)


# ---- Node hierarchy -------------------------------------------------------

def _build_nodes(
    joints: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Build glTF ``nodes[]`` from MMCP joint defs. Returns ``(nodes, root_index)``."""
    name_to_idx = {j["name"]: i for i, j in enumerate(joints)}
    children: list[list[int]] = [[] for _ in joints]
    root_idx: int | None = None

    for i, j in enumerate(joints):
        parent = j.get("parent")
        if parent is None:
            if root_idx is not None:
                raise ValueError("skeleton has multiple roots")
            root_idx = i
        else:
            if parent not in name_to_idx:
                raise ValueError(
                    f"joint {j['name']!r} references unknown parent {parent!r}"
                )
            children[name_to_idx[parent]].append(i)

    if root_idx is None:
        raise ValueError("skeleton has no root joint")

    nodes: list[dict[str, Any]] = []
    for i, j in enumerate(joints):
        node: dict[str, Any] = {
            "name":        j["name"],
            "translation": list(j["rest_translation"]),
            "rotation":    list(j["rest_rotation"]),
        }
        if children[i]:
            node["children"] = children[i]
        nodes.append(node)

    return nodes, root_idx


# ---- Public API -----------------------------------------------------------

def build_gltf(
    *,
    skeleton: dict[str, Any],
    joint_names: Sequence[str],
    rotations_quat: np.ndarray,                                 # (B, T, J, 4) (x,y,z,w)
    root_translations: np.ndarray,                              # (B, T, 3)
    fps: float,
    model_id: str,
    foot_contacts: dict[str, np.ndarray] | None = None,         # {joint_name: (B, T) bool}
    chunk_boundaries: Sequence[Sequence[int]] | None = None,    # per-sample int lists
    canonical_to_request: dict[str, str] | None = None,
    trajectories: Sequence[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Build a glTF 2.0 JSON document with one animation per sample.

    Parameters
    ----------
    skeleton
        The MMCP skeleton sent in the request — used for the node hierarchy
        and rest pose. Joint names from this skeleton are what the animation
        channels target.
    joint_names
        The model-side joint order matching ``rotations_quat``'s J axis.
        MUST match ``[j["name"] for j in skeleton["joints"]]`` 1:1 — server
        code is responsible for retargeting before calling this encoder.
    rotations_quat
        Per-frame local-to-parent rotation quaternions ``(x, y, z, w)``,
        shape ``(B, T, J, 4)``.
    root_translations
        Per-frame root world positions, shape ``(B, T, 3)``.
    fps
        Animation frame rate. Used to compute per-frame timestamps.
    model_id
        Echoed into the ``MMCP_motion`` extension.
    foot_contacts
        Optional per-joint per-frame contact booleans, shape ``(B, T)`` per joint.
    chunk_boundaries
        Optional per-sample list of stitch frame indices.
    canonical_to_request
        Optional retargeting map (canonical joint → request joint). Echoed in
        the response extension when the server retargets.
    trajectories
        Optional per-sample travel trajectory (see :mod:`motionmcp.trajectory`),
        written as ``samples[b].trajectory`` in the extension.
    """
    if rotations_quat.ndim != 4 or rotations_quat.shape[-1] != 4:
        raise ValueError(
            f"rotations_quat must be (B, T, J, 4); got {rotations_quat.shape}"
        )
    B, T, J, _ = rotations_quat.shape
    if root_translations.shape != (B, T, 3):
        raise ValueError(
            f"root_translations must be (B={B}, T={T}, 3); "
            f"got {root_translations.shape}"
        )
    if len(joint_names) != J:
        raise ValueError(
            f"joint_names has {len(joint_names)} entries but rotations_quat has J={J}"
        )

    request_joints = list(skeleton["joints"])
    request_names = [j["name"] for j in request_joints]
    if request_names != list(joint_names):
        raise ValueError(
            "request skeleton joint order does not match joint_names; the "
            "server must retarget so that the response is on the request's "
            "joint set before reaching the encoder. "
            f"first mismatch: request[{_first_diff_index(request_names, joint_names)}]"
        )

    nodes, root_idx = _build_nodes(request_joints)

    timestamps = (np.arange(T, dtype=_F32) / float(fps))

    bb = _BufferBuilder()
    animations: list[dict[str, Any]] = []

    for b in range(B):
        ts_acc = bb.add(timestamps, kind="SCALAR", with_min_max=True)

        samplers: list[dict[str, Any]] = []
        channels: list[dict[str, Any]] = []

        # One rotation channel per joint.
        for j in range(J):
            quat_acc = bb.add(rotations_quat[b, :, j, :], kind="VEC4")
            sampler_idx = len(samplers)
            samplers.append({
                "input":         ts_acc,
                "output":        quat_acc,
                "interpolation": "LINEAR",
            })
            channels.append({
                "sampler": sampler_idx,
                "target":  {"node": j, "path": "rotation"},
            })

        # One translation channel for the root.
        root_acc = bb.add(root_translations[b], kind="VEC3")
        sampler_idx = len(samplers)
        samplers.append({
            "input":         ts_acc,
            "output":        root_acc,
            "interpolation": "LINEAR",
        })
        channels.append({
            "sampler": sampler_idx,
            "target":  {"node": root_idx, "path": "translation"},
        })

        animations.append({
            "name":     f"sample_{b}",
            "channels": channels,
            "samplers": samplers,
        })

    data_uri, byte_length = bb.to_data_uri()

    extension = _build_extension(
        model_id=model_id,
        fps=float(fps),
        num_samples=B,
        num_frames=T,
        foot_contacts=foot_contacts,
        chunk_boundaries=chunk_boundaries,
        canonical_to_request=canonical_to_request,
        trajectories=trajectories,
    )

    return {
        "asset":          {"version": GLTF_VERSION, "generator": GLTF_GENERATOR},
        "nodes":          nodes,
        "scenes":         [{"nodes": [root_idx]}],
        "scene":          0,
        "skins":          [{"name": "skin", "joints": list(range(J))}],
        "animations":     animations,
        "accessors":      bb.accessors,
        "bufferViews":    bb.buffer_views,
        "buffers":        [{"byteLength": byte_length, "uri": data_uri}],
        "extensionsUsed": ["MMCP_motion"],
        "extensions":     {"MMCP_motion": extension},
    }


def _first_diff_index(a: Sequence[str], b: Sequence[str]) -> int | str:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return "lengths differ"


# ---- MMCP_motion extension block ------------------------------------------

def _build_extension(
    *,
    model_id: str,
    fps: float,
    num_samples: int,
    num_frames: int,
    foot_contacts: dict[str, np.ndarray] | None,
    chunk_boundaries: Sequence[Sequence[int]] | None,
    canonical_to_request: dict[str, str] | None,
    trajectories: Sequence[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for b in range(num_samples):
        sample: dict[str, Any] = {
            "name":             f"sample_{b}",
            "num_frames":       int(num_frames),
            "chunk_boundaries": list(chunk_boundaries[b]) if chunk_boundaries else [],
        }
        if foot_contacts:
            sample["foot_contacts"] = {
                joint_name: arr[b].astype(bool).tolist()
                for joint_name, arr in foot_contacts.items()
            }
        if trajectories and b < len(trajectories) and trajectories[b]:
            sample["trajectory"] = trajectories[b]
        samples.append(sample)

    block: dict[str, Any] = {
        "version": MMCP_VERSION,
        "model":   model_id,
        "fps":     fps,
        "samples": samples,
    }
    if canonical_to_request:
        block["canonical_to_request"] = dict(canonical_to_request)
    return block
