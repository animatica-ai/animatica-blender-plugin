# SPDX-License-Identifier: Apache-2.0
"""Backbone abstraction — what users implement to plug their model into MMCP.

Two pieces:

* :class:`ModelSpec` — the per-model metadata served at ``GET /capabilities``.
* :class:`Backbone` — the ABC users subclass; defines :meth:`Backbone.capabilities`
  and :meth:`Backbone.generate`.

A :class:`MotionResult` is what :meth:`Backbone.generate` returns: per-sample
quaternion rotations, root translations, and optional foot-contact masks. The
SDK encodes this to glTF for you.
"""

from __future__ import annotations

import abc
import inspect
from dataclasses import dataclass, field
from typing import Awaitable, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .protocol import (
    DEFAULT_LIMITS,
    SUPPORTED_GUIDANCE_TYPES,
    SUPPORTED_SEGMENTS,
)
from .schemas import GenerateRequest, Skeleton


# ---- ModelSpec — published at /capabilities --------------------------------

class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_duration_seconds: float = float(DEFAULT_LIMITS["max_duration_seconds"])
    max_num_samples: int = int(DEFAULT_LIMITS["max_num_samples"])
    max_constraints_per_request: int = int(DEFAULT_LIMITS["max_constraints_per_request"])
    max_prompt_length: int = int(DEFAULT_LIMITS["max_prompt_length"])
    max_request_bytes: int = int(DEFAULT_LIMITS["max_request_bytes"])
    max_batch_size: int = int(DEFAULT_LIMITS["max_batch_size"])


class ModelSpec(BaseModel):
    """Per-model entry under ``/capabilities.models[]``.

    All fields except ``id``, ``fps``, and ``canonical_skeleton`` have spec
    defaults — you only have to fill in what differs from the conservative
    baseline.
    """
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    fps: float = Field(..., gt=0)
    canonical_skeleton: Skeleton

    supports_retargeting: bool = False
    supports_async: bool = False
    # True when the backbone honours a per-segment ``seed`` (see
    # ``TextSegment.seed``). Clients should only attach per-segment seeds when
    # this is advertised — older servers reject unknown fields (extra=forbid).
    supports_segment_seed: bool = False
    # True when the backbone samples ``options.loop`` as a cycle. Clients
    # should only set ``loop`` when this is advertised; the SDK rejects it
    # otherwise.
    supports_loop: bool = False
    # True when POST /generate accepts a batch body, ``{"requests": [...]}``,
    # of up to ``limits.max_batch_size`` generate requests. Advertised by the
    # SDK server for every model it serves (it can always run a batch item by
    # item); a backbone that batches on the GPU overrides ``generate_batch``.
    supports_batch: bool = False
    # True when each response sample carries its travel trajectory
    # (``MMCP_motion.samples[b].trajectory``): the path the motion moves along,
    # so a client can play it in place by removing exactly that. Advertised by
    # the SDK server for every model (it computes it, see motionmcp.trajectory).
    supports_trajectory: bool = False

    supported_constraints: list[str] = Field(default_factory=list)
    # Wire-format segment types this model accepts. Defaults to "text" and
    # "unconditioned" (every conforming server supports those). Backbones
    # with a specialized text-to-pose model add "pose"; the SDK rejects
    # any segment whose type isn't listed here.
    supported_segments: list[str] = Field(
        default_factory=lambda: list(SUPPORTED_SEGMENTS)
    )
    supported_guidance_types: list[str] = Field(
        default_factory=lambda: list(SUPPORTED_GUIDANCE_TYPES)
    )
    predicted_contact_joints: list[str] = Field(default_factory=list)

    native_clip_seconds: float = 10.0
    chunking: str = Field("none", pattern=r"^(none|stitched)$")
    recommended_max_duration_seconds: float = 12.0

    limits: Limits = Field(default_factory=Limits)


# ---- MotionResult — what generate() returns --------------------------------

@dataclass
class MotionResult:
    """The output of :meth:`Backbone.generate`.

    Shape conventions (B = num_samples, T = num_frames, J = num_joints):

    * ``rotations``: ``(B, T, J, 4)`` quaternions ``(x, y, z, w)``, local-to-parent.
    * ``root_translations``: ``(B, T, 3)`` world-space root positions.
    * ``joint_names``: optional ``(J,)`` list. Defaults to the request skeleton's
      joint order. Provide this only if your model retargeted internally and
      you need to declare a different output order — the SDK will validate
      that ``joint_names`` matches the request skeleton (or your declared
      ``canonical_to_request`` map).
    * ``foot_contacts``: optional ``{joint_name: (B, T) bool array}``. Joint
      names must exist in the request skeleton.
    * ``chunk_boundaries``: optional per-sample list of frame indices where
      the server stitched internal chunks. Empty / None means no stitching.
    * ``canonical_to_request``: optional ``{canonical_joint: request_joint}``
      map echoed in the response extension. Only populate when the server
      actually retargeted.
    * ``trajectories``: optional per-sample travel trajectory (the wire dict of
      :mod:`motionmcp.trajectory`). Leave it None and the SDK computes it; set
      it when the backbone knows better.
    """

    rotations: np.ndarray
    root_translations: np.ndarray
    joint_names: Sequence[str] | None = None
    foot_contacts: dict[str, np.ndarray] = field(default_factory=dict)
    chunk_boundaries: Sequence[Sequence[int]] | None = None
    canonical_to_request: dict[str, str] | None = None
    trajectories: Sequence[dict | None] | None = None

    def __post_init__(self) -> None:
        r = self.rotations
        if r.ndim != 4 or r.shape[-1] != 4:
            raise ValueError(
                f"rotations must be (B, T, J, 4); got {r.shape}"
            )
        B, T, J, _ = r.shape
        t = self.root_translations
        if t.shape != (B, T, 3):
            raise ValueError(
                f"root_translations must be ({B}, {T}, 3); got {t.shape}"
            )
        for joint, mask in self.foot_contacts.items():
            if mask.shape != (B, T):
                raise ValueError(
                    f"foot_contacts[{joint!r}] must be ({B}, {T}); "
                    f"got {mask.shape}"
                )

    @property
    def num_samples(self) -> int:
        return int(self.rotations.shape[0])

    @property
    def num_frames(self) -> int:
        return int(self.rotations.shape[1])

    @property
    def num_joints(self) -> int:
        return int(self.rotations.shape[2])


# ---- Backbone ABC ----------------------------------------------------------

class Backbone(abc.ABC):
    """The contract a user implements to plug their motion model into MMCP.

    Subclasses MUST implement:

    * :meth:`capabilities` — returns a :class:`ModelSpec` describing the model.
    * :meth:`generate` — runs the model on a validated :class:`GenerateRequest`
      and returns a :class:`MotionResult`.

    :meth:`generate` may be ``async def`` or a regular function — the SDK
    handles both. Async is preferred when the underlying model has any I/O or
    benefits from yielding (queueing on a GPU worker, awaiting a remote call).

    Raise :class:`~motionmcp.errors.ProtocolError` (or one of its convenience
    constructors in :mod:`motionmcp.errors`) for protocol-level failures —
    the SDK converts them to the standard error envelope. Any other exception
    becomes a generic ``500 internal_error``.
    """

    @abc.abstractmethod
    def capabilities(self) -> ModelSpec:
        """Return the per-model entry that goes under ``/capabilities.models[]``."""

    @abc.abstractmethod
    def generate(self, request: GenerateRequest) -> MotionResult | Awaitable[MotionResult]:
        """Run motion generation for one validated request.

        May be implemented as either ``def`` or ``async def``.
        """

    def generate_batch(
        self, requests: list[GenerateRequest],
    ) -> list[MotionResult | BaseException] | Awaitable[list[MotionResult | BaseException]]:
        """Optional: run several validated requests at once.

        Return one entry per request, in order: a :class:`MotionResult`, or
        the exception that item raised (a :class:`ProtocolError` for a clean
        envelope). Override it when the model can batch — one GPU pass over
        several characters instead of several passes. Not overriding it is
        fine: the SDK then runs :meth:`generate` on each item in turn.

        May be implemented as either ``def`` or ``async def``.
        """
        raise NotImplementedError

    # Optional hook: called once at server startup. Use it to load model
    # weights, allocate GPU buffers, warm caches.
    def setup(self) -> None:  # pragma: no cover - default no-op
        return

    # Optional hook: called once at server shutdown.
    def teardown(self) -> None:  # pragma: no cover - default no-op
        return


def is_async_generate(backbone: Backbone) -> bool:
    """Return True if the backbone's ``generate`` is an async coroutine function."""
    return inspect.iscoroutinefunction(backbone.generate)
