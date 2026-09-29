"""motionmcp — Python SDK for the Motion Model Context Protocol (MMCP).

Build a vendor-neutral motion-generation HTTP server in ~30 lines:

    from motionmcp import Backbone, ModelSpec, GenerateRequest, MotionResult, serve

    class MyBackbone(Backbone):
        def capabilities(self) -> ModelSpec:
            return ModelSpec(
                id="my-model",
                fps=30.0,
                supports_retargeting=False,
                supported_constraints=["pose_keyframe"],
                canonical_skeleton=load_skeleton(),
            )

        async def generate(self, req: GenerateRequest) -> MotionResult:
            ...
            return MotionResult(rotations=..., root_translations=...)

    if __name__ == "__main__":
        serve(MyBackbone())

The HTTP client lives in ``motionmcp.client``. The server side (``Backbone``,
the request schemas, ``serve``) needs ``pip install "motionmcp-sdk[server]"``.
"""

import importlib

from .errors import ProtocolError
from .protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_CONSTRAINTS,
    SUPPORTED_GUIDANCE_TYPES,
    SUPPORTED_SEGMENTS,
    DEFAULT_LIMITS,
)

# Server-side names need the [server] extras (pydantic, fastapi, uvicorn), so
# they are imported on first access instead of with the package.
_SERVER_EXPORTS: dict[str, str] = {
    "Backbone": ".backbone",
    "ModelSpec": ".backbone",
    "MotionResult": ".backbone",
    "Constraint": ".schemas",
    "EffectorTargetConstraint": ".schemas",
    "GenerateRequest": ".schemas",
    "Guidance": ".schemas",
    "Joint": ".schemas",
    "Options": ".schemas",
    "PoseKeyframeConstraint": ".schemas",
    "PoseSegment": ".schemas",
    "RootPathConstraint": ".schemas",
    "Segment": ".schemas",
    "Skeleton": ".schemas",
    "TextSegment": ".schemas",
    "Timing": ".schemas",
    "UnconditionedSegment": ".schemas",
    "build_app": ".server",
    "serve": ".server",
}

__all__ = [
    "Backbone",
    "Constraint",
    "DEFAULT_LIMITS",
    "EffectorTargetConstraint",
    "GenerateRequest",
    "Guidance",
    "Joint",
    "ModelSpec",
    "MotionResult",
    "Options",
    "PROTOCOL_VERSION",
    "PoseKeyframeConstraint",
    "PoseSegment",
    "ProtocolError",
    "RootPathConstraint",
    "SUPPORTED_CONSTRAINTS",
    "SUPPORTED_GUIDANCE_TYPES",
    "SUPPORTED_SEGMENTS",
    "Segment",
    "Skeleton",
    "TextSegment",
    "Timing",
    "UnconditionedSegment",
    "build_app",
    "serve",
]

__version__ = "0.9.0"


def __getattr__(name):
    if name in _SERVER_EXPORTS:
        try:
            module = importlib.import_module(_SERVER_EXPORTS[name], __name__)
        except ImportError as exc:
            raise ImportError(
                f"motionmcp.{name} needs the server extras (missing module {exc.name!r}): "
                "pip install 'motionmcp-sdk[server]'"
            ) from exc
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
