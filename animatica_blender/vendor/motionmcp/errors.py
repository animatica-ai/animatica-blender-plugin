# SPDX-License-Identifier: Apache-2.0
"""MMCP error envelope and protocol-level exceptions.

The wire format is a single shape::

    {"error": {"code": "...", "message": "...", "details": {...}}}

Backbone code raises :class:`ProtocolError` (or one of the typed subclasses)
to signal a failure; the SDK's FastAPI layer maps it to the right HTTP status
via :data:`STATUS_FOR` and serialises the envelope.
"""

from __future__ import annotations

from typing import Any


# Code → HTTP status. The set is closed at v1.0; new codes go in minor versions.
STATUS_FOR: dict[str, int] = {
    "schema_validation":           422,
    "unknown_model":               400,
    "retargeting_unsupported":     400,
    "unsupported_constraint":      400,
    "unsupported_segment":         400,
    "unknown_joint":               400,
    "invalid_skeleton":            400,
    "frame_out_of_range":          400,
    "invalid_options":             400,
    "version_unsupported":         400,
    "unauthorized":                401,
    "forbidden":                   403,
    "payload_too_large":           413,
    "rate_limited":                429,
    "internal_error":              500,
    "resource_exhausted":          503,
    "model_unavailable":           503,
    "timeout":                     504,
}


class ProtocolError(Exception):
    """Raised by handlers to signal a protocol-level failure.

    The FastAPI app catches this and serialises it to the MMCP error envelope.
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ):
        if code not in STATUS_FOR:
            raise ValueError(f"unknown protocol error code: {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def status_code(self) -> int:
        return STATUS_FOR[self.code]

    def to_envelope(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


# ---- Convenience constructors for common cases ---------------------------

def unknown_model(model_id: str, available: list[str]) -> ProtocolError:
    return ProtocolError(
        "unknown_model",
        f"model {model_id!r} is not registered with this server",
        details={"available_models": available},
    )


def unsupported_constraint(
    constraint_type: str, supported: list[str]
) -> ProtocolError:
    return ProtocolError(
        "unsupported_constraint",
        f"constraint type {constraint_type!r} is not supported by this model",
        details={"supported_constraints": supported},
    )


def unsupported_segment(
    segment_type: str, supported: list[str]
) -> ProtocolError:
    return ProtocolError(
        "unsupported_segment",
        f"segment type {segment_type!r} is not supported by this model",
        details={"supported_segments": supported},
    )


def unknown_joint(joint: str, skeleton_joints: list[str]) -> ProtocolError:
    return ProtocolError(
        "unknown_joint",
        f"joint {joint!r} is not in the request skeleton",
        details={"skeleton_joints": skeleton_joints},
    )


def frame_out_of_range(frame: int, total_frames: int) -> ProtocolError:
    return ProtocolError(
        "frame_out_of_range",
        f"frame {frame} is outside [0, {total_frames - 1}]",
        details={"frame": frame, "total_frames": total_frames},
    )


def retargeting_unsupported() -> ProtocolError:
    return ProtocolError(
        "retargeting_unsupported",
        "this model does not support retargeting; send the canonical "
        "skeleton verbatim from /capabilities",
    )


def version_unsupported(requested: str, supported_majors: list[str]) -> ProtocolError:
    return ProtocolError(
        "version_unsupported",
        f"protocol_version {requested!r} is not supported by this server",
        details={"supported_majors": supported_majors},
    )
