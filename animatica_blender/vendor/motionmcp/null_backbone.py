# SPDX-License-Identifier: Apache-2.0
"""A reference :class:`Backbone` that returns the rest pose for every request.

Useful for:

* Plugin development — build a real client against a real server with zero
  ML or GPU dependencies.
* End-to-end transport tests.
* The ``motionmcp-null`` CLI entry point.
"""

from __future__ import annotations

import numpy as np

from .backbone import Backbone, ModelSpec, MotionResult
from .protocol import SUPPORTED_CONSTRAINTS
from .schemas import GenerateRequest, Joint, Skeleton


def _default_skeleton() -> Skeleton:
    """A tiny standing humanoid for default use. Six joints — Hips, Spine, Head,
    LeftFoot, RightFoot, RootFloor — enough for IK/effector smoke tests.
    """
    return Skeleton(
        joints=[
            Joint(name="Hips",      parent=None,    rest_translation=(0.0,  0.95, 0.0), rest_rotation=(0.0, 0.0, 0.0, 1.0)),
            Joint(name="Spine",     parent="Hips",  rest_translation=(0.0,  0.50, 0.0), rest_rotation=(0.0, 0.0, 0.0, 1.0)),
            Joint(name="Head",      parent="Spine", rest_translation=(0.0,  0.30, 0.0), rest_rotation=(0.0, 0.0, 0.0, 1.0)),
            Joint(name="LeftFoot",  parent="Hips",  rest_translation=(0.10, -0.95, 0.0), rest_rotation=(0.0, 0.0, 0.0, 1.0)),
            Joint(name="RightFoot", parent="Hips",  rest_translation=(-0.10, -0.95, 0.0), rest_rotation=(0.0, 0.0, 0.0, 1.0)),
        ]
    )


class NullBackbone(Backbone):
    """Returns the rest pose for every request.

    Parameters
    ----------
    model_id
        The id this backbone advertises in ``/capabilities.models[].id``.
        Use this to expose multiple "fake" models from one server during
        plugin development.
    fps
        Frame rate the model claims natively.
    canonical_skeleton
        The skeleton served as the canonical. Defaults to a tiny standing
        humanoid; pass your own when testing against a real DCC armature.
    """

    def __init__(
        self,
        *,
        model_id: str = "null",
        fps: float = 30.0,
        canonical_skeleton: Skeleton | None = None,
    ) -> None:
        self.model_id = model_id
        self.fps_value = fps
        self.skeleton_value = canonical_skeleton or _default_skeleton()

    def capabilities(self) -> ModelSpec:
        return ModelSpec(
            id=self.model_id,
            fps=self.fps_value,
            canonical_skeleton=self.skeleton_value,
            supports_retargeting=False,
            supported_constraints=list(SUPPORTED_CONSTRAINTS),
            predicted_contact_joints=[],
        )

    async def generate(self, request: GenerateRequest) -> MotionResult:
        num_samples = request.options.num_samples if request.options else 1
        T = request.total_frames
        joints = request.skeleton.joints
        J = len(joints)

        # Identity quaternion (x, y, z, w) per joint per frame per sample.
        rotations = np.zeros((num_samples, T, J, 4), dtype=np.float32)
        rotations[..., 3] = 1.0

        # Root translation = root rest position, held constant.
        root = joints[0]
        root_pos = np.asarray(root.rest_translation, dtype=np.float32)
        root_translations = np.broadcast_to(
            root_pos, (num_samples, T, 3)
        ).copy()

        return MotionResult(
            rotations=rotations,
            root_translations=root_translations,
        )


# ---- CLI: ``python -m motionmcp.null_backbone`` ---------------------------

def main() -> None:  # pragma: no cover - exercised manually
    import argparse

    from .server import serve

    parser = argparse.ArgumentParser(
        prog="motionmcp-null",
        description="Run a reference MMCP server backed by NullBackbone.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model-id", default="null")
    parser.add_argument("--fps", type=float, default=30.0)
    args = parser.parse_args()

    serve(NullBackbone(model_id=args.model_id, fps=args.fps),
          host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
