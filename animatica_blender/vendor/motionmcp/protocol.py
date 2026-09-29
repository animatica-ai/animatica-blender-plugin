# SPDX-License-Identifier: Apache-2.0
"""Protocol-level constants. Versioned with the spec."""

from __future__ import annotations


PROTOCOL_VERSION = "1.0"
PROTOCOL_MAJOR = 1

ROTATION_FORMAT = "quaternion_xyzw"
COORDINATE_SYSTEM = "right_handed_y_up"
UNITS = "meters"

RESPONSE_FORMATS = ["gltf_2.0_json"]

SUPPORTED_CONSTRAINTS = ["root_path", "effector_target", "pose_keyframe"]

# The two segment types every conforming server supports. ``"pose"`` is an
# optional extension — backbones that have a specialized text-to-pose model
# include it in their ``ModelSpec.supported_segments``; backbones without
# it leave this default and the SDK rejects ``pose`` segments before they
# reach the backbone.
SUPPORTED_SEGMENTS = ["text", "unconditioned"]

SUPPORTED_GUIDANCE_TYPES = ["nocfg", "regular", "separated"]

DEFAULT_LIMITS = {
    "max_duration_seconds":         30.0,
    "max_num_samples":              16,
    "max_constraints_per_request":  64,
    "max_prompt_length":            1000,
    "max_request_bytes":            1_048_576,
    # Items in one batch body sent to POST /generate (see server.py).
    "max_batch_size":               16,
}
