# SPDX-License-Identifier: Apache-2.0
"""FastAPI server factory and run helper.

Two entry points:

* :func:`build_app` — returns the FastAPI ``app`` for one or more backbones.
  Use this if you want to mount additional routes, middleware, or run the
  app under your own ASGI server.
* :func:`serve` — convenience that builds the app and runs it under uvicorn.
  Use this for the simple case.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Iterable, Mapping

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .backbone import Backbone, ModelSpec, MotionResult, is_async_generate
from .errors import (
    ProtocolError,
    frame_out_of_range,
    retargeting_unsupported,
    unknown_joint,
    unknown_model,
    unsupported_constraint,
    unsupported_segment,
    version_unsupported,
)
from .gltf import build_gltf
from .protocol import (
    COORDINATE_SYSTEM,
    DEFAULT_LIMITS,
    PROTOCOL_MAJOR,
    PROTOCOL_VERSION,
    RESPONSE_FORMATS,
    ROTATION_FORMAT,
    UNITS,
)
from .schemas import GenerateRequest


# ---- Backbone registry ----------------------------------------------------

def _to_registry(
    backbone: Backbone | Mapping[str, Backbone] | Iterable[Backbone],
) -> dict[str, Backbone]:
    """Normalise the backbone argument to ``{model_id: backbone}``.

    Accepts:
      * a single :class:`Backbone` (id read from its ``model_id`` attribute,
        falling back to ``capabilities().id``),
      * a dict of ``{model_id: backbone}``,
      * an iterable of backbones (ids resolved as for the single case).

    The ``model_id`` attribute is preferred so backbones can register their
    id without forcing model weights to load. ``capabilities()`` may need
    the model loaded — it gets called per-request once the server is up,
    after :meth:`Backbone.setup` has run.
    """
    if isinstance(backbone, Backbone):
        return {_backbone_id(backbone): backbone}
    if isinstance(backbone, Mapping):
        return dict(backbone)
    backbones = list(backbone)
    if not backbones:
        raise ValueError("at least one backbone is required")
    out: dict[str, Backbone] = {}
    for b in backbones:
        bid = _backbone_id(b)
        if bid in out:
            raise ValueError(f"duplicate model id: {bid!r}")
        out[bid] = b
    return out


def _backbone_id(b: Backbone) -> str:
    """Resolve a backbone's model id without forcing setup() to have run.

    Prefers a lightweight ``model_id`` attribute; falls back to
    ``capabilities().id`` for backbones that don't expose one (and can build
    a spec without their model loaded).
    """
    mid = getattr(b, "model_id", None)
    if isinstance(mid, str) and mid:
        return mid
    return b.capabilities().id


# ---- App factory ----------------------------------------------------------

def build_app(
    backbone: Backbone | Mapping[str, Backbone] | Iterable[Backbone],
    *,
    title: str = "MMCP",
    description: str | None = None,
) -> FastAPI:
    """Build a FastAPI app that serves the MMCP protocol for ``backbone``.

    ``backbone`` may be a single :class:`Backbone`, a dict of
    ``{model_id: Backbone}``, or an iterable of backbones.
    """
    registry = _to_registry(backbone)

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        for b in registry.values():
            b.setup()
        try:
            yield
        finally:
            for b in registry.values():
                b.teardown()

    app = FastAPI(
        title=title,
        version=PROTOCOL_VERSION,
        description=description or (
            "MMCP — Motion Model Context Protocol. "
            "See https://animatica.ai/mmcp for the protocol docs."
        ),
        lifespan=_lifespan,
    )

    # ----- /capabilities ------------------------------------------------------

    @app.get("/capabilities")
    async def get_capabilities() -> dict:
        return {
            "protocol_version":  PROTOCOL_VERSION,
            "rotation_format":   ROTATION_FORMAT,
            "coordinate_system": COORDINATE_SYSTEM,
            "units":             UNITS,
            "response_formats":  RESPONSE_FORMATS,
            # Every model served here takes a batch body at POST /generate:
            # the SDK runs one item by item when the backbone cannot batch.
            "models":            [b.capabilities().model_copy(update={"supports_batch": True,
                                                                     "supports_trajectory": True})
                                  .model_dump(mode="json")
                                  for b in registry.values()],
        }

    # ----- /generate ---------------------------------------------------------

    def _parse(payload) -> GenerateRequest:
        """One generate request, schema- and version-checked."""
        try:
            req = GenerateRequest.model_validate(payload)
        except ValidationError as exc:
            raise ProtocolError(
                "schema_validation",
                "request fails schema validation",
                details={"errors": exc.errors(include_url=False)},
            ) from exc
        _check_version(req.protocol_version)
        return req

    def _backbone_for(req: GenerateRequest) -> tuple[Backbone, ModelSpec]:
        """The backbone a request names, with the request checked against it."""
        if req.model not in registry:
            raise unknown_model(req.model, sorted(registry.keys()))
        backbone = registry[req.model]
        spec = backbone.capabilities()
        # Per-model semantic validation that the SDK can do generically.
        _validate_against_spec(req, spec)
        return backbone, spec

    def _encode(req: GenerateRequest, spec: ModelSpec, result) -> dict:
        """A backbone's MotionResult as the glTF the protocol answers with."""
        if not isinstance(result, MotionResult):
            raise ProtocolError(
                "internal_error",
                "backbone.generate must return a MotionResult; "
                f"got {type(result).__name__}",
            )
        skeleton_dict = req.skeleton.model_dump(mode="json")
        joint_names = (
            list(result.joint_names)
            if result.joint_names is not None
            else [j["name"] for j in skeleton_dict["joints"]]
        )
        fps = req.fps(spec.fps)
        trajectories = result.trajectories
        if trajectories is None:
            trajectories = _trajectories(req, skeleton_dict, joint_names, result, fps)
        return build_gltf(
            skeleton=skeleton_dict,
            joint_names=joint_names,
            rotations_quat=result.rotations,
            root_translations=result.root_translations,
            fps=fps,
            model_id=spec.id,
            foot_contacts=result.foot_contacts or None,
            chunk_boundaries=result.chunk_boundaries,
            canonical_to_request=result.canonical_to_request,
            trajectories=trajectories,
        )

    @app.post("/generate")
    async def post_generate(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception as exc:
            raise ProtocolError(
                "schema_validation",
                f"request body is not valid JSON: {exc}",
            )

        # A batch: several generate requests in one body (see _generate_batch).
        if isinstance(payload, dict) and "requests" in payload:
            return JSONResponse(content=await _generate_batch(payload),
                                media_type="application/json")

        req = _parse(payload)
        backbone, spec = _backbone_for(req)
        result = await _call_generate(backbone, req)
        return JSONResponse(
            content=_encode(req, spec, result),
            media_type="model/gltf+json",
        )

    async def _generate_batch(payload: dict) -> dict:
        """Several generate requests in one body, one result each, in order.

        ``{"protocol_version": "1.0", "requests": [<generate request>, ...]}``
        answers ``{"results": [{"gltf": {...}} | {"error": {...}}, ...]}``. An
        item that fails — malformed, an unknown model, a failed generation —
        gets its own error envelope and does not fail the others; only a batch
        that is not a batch at all (no list, too many items, a protocol major
        the server does not speak) is refused whole. An item may leave out
        ``protocol_version``; it inherits the batch's.

        Items are grouped by model and each group is handed to that backbone's
        ``generate_batch`` when it has one (one pass over several characters),
        or run item by item through ``generate``.
        """
        version = payload.get("protocol_version")
        if not isinstance(version, str):
            raise ProtocolError("schema_validation", "a batch needs a protocol_version")
        _check_version(version)
        items = payload.get("requests")
        if not isinstance(items, list) or not items:
            raise ProtocolError("schema_validation", "requests must be a non-empty list")
        limit = min((b.capabilities().limits.max_batch_size for b in registry.values()),
                    default=int(DEFAULT_LIMITS["max_batch_size"]))
        if len(items) > limit:
            raise ProtocolError(
                "invalid_options",
                f"a batch holds at most {limit} requests; got {len(items)}",
                details={"max_batch_size": limit, "requests": len(items)},
            )

        results: list[dict | None] = [None] * len(items)
        groups: dict[str, list[tuple[int, GenerateRequest, ModelSpec]]] = {}
        for i, raw in enumerate(items):
            try:
                if not isinstance(raw, dict):
                    raise ProtocolError("schema_validation", "each request must be an object")
                req = _parse({"protocol_version": version, **raw})
                _, spec = _backbone_for(req)
            except ProtocolError as exc:
                results[i] = exc.to_envelope()
                continue
            groups.setdefault(req.model, []).append((i, req, spec))

        for model_id, group in groups.items():
            outcomes = await _call_generate_batch(registry[model_id], [r for _, r, _ in group])
            for (i, req, spec), outcome in zip(group, outcomes):
                try:
                    if isinstance(outcome, BaseException):
                        raise outcome
                    results[i] = {"gltf": _encode(req, spec, outcome)}
                except ProtocolError as exc:
                    results[i] = exc.to_envelope()
                except Exception as exc:  # noqa: BLE001 — one item's failure
                    results[i] = ProtocolError("internal_error", f"{type(exc).__name__}: {exc}").to_envelope()
        return {"results": results}

    # ----- error envelope ---------------------------------------------------

    @app.exception_handler(ProtocolError)
    async def _protocol_error_handler(_: Request, exc: ProtocolError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_envelope())

    @app.exception_handler(HTTPException)
    async def _http_handler(_: Request, exc: HTTPException) -> JSONResponse:
        # Map untyped HTTPExceptions to the MMCP envelope where possible.
        code = "internal_error" if exc.status_code >= 500 else "schema_validation"
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail), "details": {}}},
        )

    return app


# ---- Generic per-spec validation ------------------------------------------

def _check_version(version: str) -> None:
    try:
        major = int(version.split(".", 1)[0])
    except ValueError as exc:
        raise version_unsupported(version, [str(PROTOCOL_MAJOR)]) from exc
    if major != PROTOCOL_MAJOR:
        raise version_unsupported(version, [str(PROTOCOL_MAJOR)])


async def _call_generate(backbone: Backbone, req: GenerateRequest):
    """Run generate(), sync or async."""
    result = backbone.generate(req)
    if is_async_generate(backbone) or inspect.isawaitable(result):
        result = await result
    return result


async def _call_generate_batch(backbone: Backbone, reqs: list[GenerateRequest]) -> list:
    """One outcome per request: a MotionResult or the exception it raised.

    The backbone's own ``generate_batch`` when it has one, else ``generate``
    on each request in turn.
    """
    if type(backbone).generate_batch is not Backbone.generate_batch:
        out = backbone.generate_batch(reqs)
        if inspect.isawaitable(out):
            out = await out
        out = list(out)
        # An item may itself be awaitable (a backbone that fans out to its own
        # async generate); resolve each, keeping a failure to its item.
        for i, item in enumerate(out):
            if inspect.isawaitable(item):
                try:
                    out[i] = await item
                except Exception as exc:  # noqa: BLE001 — recorded per item
                    out[i] = exc
        if len(out) != len(reqs):
            err = ProtocolError(
                "internal_error",
                f"generate_batch returned {len(out)} results for {len(reqs)} requests",
            )
            return [err] * len(reqs)
        return out
    outcomes: list = []
    for req in reqs:
        try:
            outcomes.append(await _call_generate(backbone, req))
        except Exception as exc:  # noqa: BLE001 — recorded per item
            outcomes.append(exc)
    return outcomes


def _trajectories(req: GenerateRequest, skeleton: dict, joint_names, result, fps: float):
    """The travel trajectory of each sample, for clients that play it in place.
    Best effort: a motion whose body cannot be read (no hips found) gets none,
    and a failure here never fails the generation."""
    from .trajectory import compute_trajectories
    try:
        return compute_trajectories(
            skeleton=skeleton, joint_names=joint_names, rotations_quat=result.rotations,
            root_translations=result.root_translations, fps=fps,
            loop=bool(req.options is not None and getattr(req.options, "loop", False)),
            canonical_to_request=result.canonical_to_request,
        )
    except Exception:                  # never fail a generation over it
        import logging
        logging.getLogger("motionmcp").exception("trajectory: could not compute")
        return None


def _validate_against_spec(req: GenerateRequest, spec) -> None:
    """Run generic checks the SDK can do without consulting the backbone.

    Raises :class:`ProtocolError` on the first failure.
    """
    # Retargeting policy.
    if not spec.supports_retargeting:
        canonical = spec.canonical_skeleton
        sent = req.skeleton
        canonical_names = [j.name for j in canonical.joints]
        sent_names = [j.name for j in sent.joints]
        if canonical_names != sent_names:
            raise retargeting_unsupported()

    # Segment type support. Reject before any further work — for many
    # backbones a request with a "pose" segment routes through an entirely
    # different code path; validating the type up front lets the SDK send
    # a clean error envelope without the backbone having to handle it.
    for s in req.segments:
        if s.type not in spec.supported_segments:
            raise unsupported_segment(s.type, list(spec.supported_segments))

    # Looping: only where the backbone says it can, and over one segment.
    if req.options is not None and req.options.loop:
        if not getattr(spec, "supports_loop", False):
            raise ProtocolError(
                "invalid_options",
                "this model does not support options.loop",
                details={"model": spec.id},
            )
        if len(req.segments) != 1:
            raise ProtocolError(
                "invalid_options",
                "options.loop needs exactly one segment",
                details={"segments": len(req.segments)},
            )

    # Constraint type support.
    skeleton_joint_names = {j.name for j in req.skeleton.joints}
    total_frames = req.total_frames
    for c in req.constraints:
        if c.type not in spec.supported_constraints:
            raise unsupported_constraint(c.type, list(spec.supported_constraints))

        # Joint references must exist on the request skeleton.
        if c.type == "effector_target":
            if c.joint not in skeleton_joint_names:
                raise unknown_joint(c.joint, sorted(skeleton_joint_names))
        elif c.type == "pose_keyframe":
            for j in c.joint_rotations:
                if j not in skeleton_joint_names:
                    raise unknown_joint(j, sorted(skeleton_joint_names))

        # Frame range check.
        frames = [c.frame] if c.type == "pose_keyframe" else c.frames
        for f in frames:
            if not (0 <= f < total_frames):
                raise frame_out_of_range(f, total_frames)

    # Constraint count limit.
    if len(req.constraints) > spec.limits.max_constraints_per_request:
        raise ProtocolError(
            "invalid_options",
            f"too many constraints: {len(req.constraints)} > "
            f"{spec.limits.max_constraints_per_request}",
        )

    # Sample count limit.
    if req.options is not None:
        if req.options.num_samples > spec.limits.max_num_samples:
            raise ProtocolError(
                "invalid_options",
                f"num_samples {req.options.num_samples} exceeds "
                f"max_num_samples {spec.limits.max_num_samples}",
            )

    # Prompt length limit. Both text and pose segments carry prompts.
    for s in req.segments:
        if s.type in ("text", "pose"):
            if len(s.prompt) > spec.limits.max_prompt_length:
                raise ProtocolError(
                    "invalid_options",
                    f"prompt length {len(s.prompt)} exceeds "
                    f"max_prompt_length {spec.limits.max_prompt_length}",
                )

    # Total duration limit.
    fps = req.fps(spec.fps)
    duration_s = total_frames / fps
    if duration_s > spec.limits.max_duration_seconds:
        raise ProtocolError(
            "invalid_options",
            f"total duration {duration_s:.2f}s exceeds "
            f"max_duration_seconds {spec.limits.max_duration_seconds}",
        )


# ---- Convenience runner ---------------------------------------------------

def serve(
    backbone: Backbone | Mapping[str, Backbone] | Iterable[Backbone],
    *,
    host: str = "0.0.0.0",
    port: int = 8000,
    log_level: str = "info",
    title: str = "MMCP",
) -> None:
    """Build the app and run it under uvicorn. Blocks until the server stops.

    For more control (custom middleware, mounting, lifespan, multi-worker
    deployment), use :func:`build_app` and run uvicorn yourself.
    """
    import uvicorn

    app = build_app(backbone, title=title)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
