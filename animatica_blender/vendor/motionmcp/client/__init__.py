# SPDX-License-Identifier: Apache-2.0
"""A client for the MMCP protocol: request a motion, read back a glTF.

:mod:`motionmcp.client.http` speaks the protocol over the standard library
alone -- no ``requests``, no ``httpx`` -- because the interpreters this runs
in are embedded in other applications and we do not own their site-packages.
:mod:`motionmcp.client.glb` unpacks a binary glTF (GLB) response into the
same plain glTF JSON document the JSON path returns.
:mod:`motionmcp.client.gltf_parser` turns that glTF 2.0 document into plain
arrays; numpy is its only dependency.
"""

from .glb import glb_to_gltf
from .gltf_parser import parse_gltf, parse_gltf_samples, xyzw_to_rotmat
from .http import (
    MmcpError,
    ProbeResult,
    cached_capabilities,
    clear_capabilities_cache,
    generate,
    get_capabilities,
    model_supported_segments,
    pick_model,
    poll_job,
    probe_server,
    retarget_state,
)

__all__ = [
    "MmcpError",
    "ProbeResult",
    "cached_capabilities",
    "clear_capabilities_cache",
    "generate",
    "get_capabilities",
    "glb_to_gltf",
    "model_supported_segments",
    "parse_gltf",
    "parse_gltf_samples",
    "pick_model",
    "poll_job",
    "probe_server",
    "retarget_state",
    "xyzw_to_rotmat",
]
