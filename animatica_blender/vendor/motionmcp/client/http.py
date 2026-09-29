# SPDX-License-Identifier: Apache-2.0
"""A client for an MMCP server.

Uses ``urllib.request`` only — interpreters embedded in other applications
don't always ship with ``requests`` or ``httpx``, and we want zero runtime
dependencies outside the standard library.

The wire contract is the MMCP specification; this module implements the
client side of it and nothing else.

Three endpoints are exercised:

* ``GET /capabilities`` — model + canonical skeleton metadata. Cached
  per ``(server_url, access_token)`` for the life of the process (the
  skeleton can't change without a server restart).
* ``GET /health`` — read by :func:`retarget_state` to tell whether the
  server can actually retarget right now.
* ``POST /generate`` — the main generation call. Returns a glTF 2.0 JSON
  document. A server may answer with ``model/gltf-binary`` (GLB) instead
  of JSON, on the synchronous answer or after a ``202`` and polling;
  :func:`generate` unpacks either into the same glTF JSON document via
  :func:`motionmcp.client.glb.glb_to_gltf`.
  Parsing of the document itself is done in :mod:`gltf_parser`.

``generate()`` takes a ``headers`` mapping and merges it into the request
after the standard ones. That is how an embedding application attaches its
own identity headers -- who is calling, which version, which session --
without this module having to know anything about it. They ride on the
request that STARTS a generation and on nothing else: never on
``/capabilities``, ``/health`` or job polling. Caller identity is the
caller's question to answer; this module answers for the protocol alone.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal, Optional

from .glb import glb_to_gltf

__all__ = [
    "MmcpError",
    "get_capabilities",
    "cached_capabilities",
    "clear_capabilities_cache",
    "model_supported_segments",
    "retarget_state",
    "pick_model",
    "ProbeResult",
    "probe_server",
    "generate",
    "poll_job",
]

_DEFAULT_TIMEOUT = 600.0  # cloud cold-start + inference can take up to ~5 min

ProbeStatus = Literal["online", "unreachable", "auth_required", "http_error", "bad_response"]

# MmcpError.code -> ProbeResult.status. A code not listed here but carrying an
# HTTP status in ``details`` (the server's own envelope code, e.g. ``forbidden``)
# is an HTTP failure; anything else (transport, timeout) falls back to
# "unreachable" so probe_server never raises.
_PROBE_STATUS_BY_CODE: dict[str, ProbeStatus] = {
    "connection_failed": "unreachable",
    "auth_required":     "auth_required",
    "http_error":        "http_error",
    "bad_response":      "bad_response",
}


class MmcpError(RuntimeError):
    """Raised on any MMCP transport or server-side failure.

    ``code`` mirrors the server's error envelope when available
    (e.g. ``"retargeting_unsupported"``); otherwise ``"transport"``.
    """

    def __init__(self, message, code="transport", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


_capabilities_cache: dict = {}


def _cache_key(server_url, access_token):
    """The one key ``_capabilities_cache`` is read and written under."""
    return (server_url.rstrip("/") + "/capabilities", access_token or None)


def _error_from_http(exc, method, url) -> MmcpError:
    """Map an ``HTTPError`` to :class:`MmcpError` — the one place that does.

    A JSON body with an ``error`` object (the MMCP envelope) supplies
    ``code``, ``message`` and ``details``; any other body gives
    ``code="http_error"``. HTTP 401 is always ``"auth_required"`` whatever
    the envelope says, because consumers branch on that code. ``details``
    always carries ``status`` (the HTTP status, unless the envelope's own
    ``details`` already name one), plus ``retry_after`` in seconds when
    ``Retry-After`` is a number and
    ``request_id`` from ``X-Request-ID`` or, failing that, ``X-Job-Id``.
    """
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    envelope = None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else None
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            envelope = parsed["error"]
    except Exception:
        envelope = None

    fallback = f"{method} {url} returned HTTP {exc.code}: {exc.reason}"
    if envelope is not None:
        message = envelope.get("message") or fallback
        code = envelope.get("code") or "http_error"
        env_details = envelope.get("details")
        details = dict(env_details) if isinstance(env_details, dict) else {}
    else:
        message, code, details = fallback, "http_error", {}
    details.setdefault("status", exc.code)
    if exc.code == 401:
        code = "auth_required"
        if envelope is None or not envelope.get("message"):
            message = "Authentication required (HTTP 401)."

    headers = exc.headers
    if headers is not None:
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                details["retry_after"] = float(retry_after)
            except ValueError:
                pass
        request_id = headers.get("X-Request-ID") or headers.get("X-Job-Id")
        if request_id:
            details["request_id"] = request_id
    return MmcpError(message, code=code, details=details)


def get_capabilities(server_url, timeout=10.0, use_cache=True, access_token=None):
    """Fetch ``/capabilities`` and return the parsed JSON dict.

    Cached per ``(server_url, access_token)`` so local and cloud entries,
    and two cloud accounts, don't collide.  Pass ``use_cache=False`` to bypass.

    ``access_token`` — Bearer token, when the deployment requires one.
    """
    url = server_url.rstrip("/") + "/capabilities"
    cache_key = _cache_key(server_url, access_token)
    if use_cache and cache_key in _capabilities_cache:
        return _capabilities_cache[cache_key]
    headers = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise _error_from_http(exc, "GET", url) from exc
    except urllib.error.URLError as exc:
        raise MmcpError(
            f"Could not reach server at {server_url}: {exc.reason}",
            code="connection_failed",
        ) from exc
    except Exception as exc:
        raise MmcpError(
            f"Capabilities request failed: {exc}", code="transport",
        ) from exc

    if "json" not in ctype:
        preview = body[:200].decode("utf-8", errors="replace") if body else "<empty>"
        raise MmcpError(
            f"Unexpected Content-Type from /capabilities: {ctype!r} "
            f"(status={status}, body[:200]={preview!r})",
            code="bad_response",
        )

    if not body:
        raise MmcpError(
            f"Server returned an empty {status} response from /capabilities "
            f"(Content-Type={ctype!r}). The server likely failed silently — "
            f"check server logs.",
            code="bad_response",
        )

    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as exc:
        preview = body[:200].decode("utf-8", errors="replace")
        raise MmcpError(
            f"Capabilities response was not valid JSON: {exc} "
            f"(status={status}, Content-Type={ctype!r}, body[:200]={preview!r})",
            code="bad_response",
        ) from exc
    _capabilities_cache[cache_key] = data
    return data


def cached_capabilities(server_url, access_token=None):
    """The cached ``/capabilities`` for *server_url*, or ``None``.

    Never touches the network, so a UI that must not block the main thread
    can consult it on a repaint or a settings change. A probe fills the
    cache, and everything after reads the full model list from here
    instead of depending on catching that one probe result.
    """
    # Same _cache_key get_capabilities writes under, cached per
    # (server_url, access_token): the cloud gateway filters models by the
    # account's ACL, so one account must never read another's list.
    return _capabilities_cache.get(_cache_key(server_url, access_token))


def clear_capabilities_cache():
    """Forget every cached ``/capabilities`` response. Call after the
    user changes the server URL or restarts the server."""
    _capabilities_cache.clear()


def model_supported_segments(model_info):
    """Return one model entry's advertised ``supported_segments``.

    ``model_info`` is a single entry of ``GET /capabilities .models[]``.
    Missing/null key -> ``[]``. Values are lower-cased and de-duplicated
    (order preserved) so callers can test membership directly. Used to gate
    the official MMCP ``PoseSegment`` path versus a 2-frame ``text``
    fallback.
    """
    raw = (model_info or {}).get("supported_segments") or []
    out = []
    for s in raw:
        if not s:
            continue
        v = str(s).strip().lower()
        if v and v not in out:
            out.append(v)
    return out


def retarget_state(server_url, timeout=5.0, access_token=None):
    """Can this server actually retarget? ``ready`` / ``unavailable`` / ``unknown``.

    Distinct from ``supports_retargeting`` in ``/capabilities``, which
    describes the MODEL and stays true on an install whose retarget
    weights are missing — the request then fails with a 500 several
    seconds in. The ``retargeting`` field in ``/health`` was emitted only
    by a server that has since been retired. No server contemporary with
    this release emits the field; the result is then ``"unknown"``, which
    callers must treat as *ask the server* — never a reason to refuse.
    Send the request and let the server answer.
    """
    req = urllib.request.Request(
        server_url.rstrip("/") + "/health",
        headers={"Authorization": f"Bearer {access_token}"} if access_token else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            value = json.loads(resp.read() or b"{}").get("retargeting")
    except Exception:
        return "unknown"
    return value if value in ("ready", "unavailable") else "unknown"


def pick_model(caps, wanted=None):
    """Return the ``models[]`` entry the user asked for, or the first one.

    Every generation path used to hard-code ``caps["models"][0]``, so a
    caller-selected model name went unread and every request went to
    whichever model the server happened to list first. This is the one
    place that resolves the choice.

    *wanted* is matched case-insensitively against the entry ``id``. An
    unknown or empty name falls back to the first entry rather than
    failing: the same setting travels between servers, and a model that
    exists on one may not exist on another. Returns ``None`` only when
    the server advertises no models at all.
    """
    models = (caps or {}).get("models") or []
    if not models:
        return None
    name = str(wanted or "").strip().lower()
    if name:
        for entry in models:
            if str((entry or {}).get("id", "")).strip().lower() == name:
                return entry
    return models[0]


@dataclass
class ProbeResult:
    """Structured verdict from :func:`probe_server`.

    ``status`` lets the UI branch without string-matching error text:
    ``"online"`` (reachable + valid capabilities), ``"unreachable"``
    (connection refused / DNS / timeout / unknown), ``"auth_required"``
    (reachable, needs a token), ``"http_error"`` (non-401 HTTP failure),
    ``"bad_response"`` (reachable but empty/HTML/garbage body).
    """

    ok:           bool
    status:       ProbeStatus
    message:      str
    capabilities: Optional[dict] = None


def probe_server(server_url, *, timeout=5.0, access_token=None) -> ProbeResult:
    """Probe ``/capabilities`` and return a structured :class:`ProbeResult`.

    Meant for both a caller-triggered check and an automatic one, so
    neither has to inspect error strings. Always bypasses the capabilities
    cache for a true liveness check. Never raises :class:`MmcpError` —
    every failure maps to a status.

    ``access_token`` — Bearer token, when the deployment requires one.
    """
    try:
        caps = get_capabilities(
            server_url, timeout=timeout, use_cache=False, access_token=access_token,
        )
    except MmcpError as exc:
        status: ProbeStatus = _PROBE_STATUS_BY_CODE.get(
            exc.code,
            "http_error" if isinstance(exc.details.get("status"), int) else "unreachable",
        )
        return ProbeResult(ok=False, status=status, message=str(exc))
    return ProbeResult(ok=True, status="online", message="Server is reachable.", capabilities=caps)


def _with_phase(err, phase, location=None):
    """Tag *err* with the phase it came from (and the job ``location``).

    ``setdefault``, so the innermost phase wins: an error already tagged
    ``"poll"`` stays ``"poll"`` when it passes up through :func:`generate`.
    """
    err.details.setdefault("phase", phase)
    if location is not None:
        err.details.setdefault("location", location)
    return err


def poll_job(server_url, location, *, retry_after=2.0, timeout=_DEFAULT_TIMEOUT,
             access_token=None, on_progress=None):
    """Poll an async job (the ``202`` pattern) until it returns ``200`` or times out.

    Returns the job's glTF 2.0 JSON document; a GLB answer is unpacked the
    same way :func:`generate` unpacks it. ``location`` is the job path from
    the ``202``'s ``Location`` header, relative to ``server_url`` (an
    absolute URL is cut down to its path). Polls no faster than every
    0.5 s, honouring ``Retry-After``; ``timeout`` counts from the start of
    polling.

    Every :class:`MmcpError` raised while polling carries
    ``details["phase"] == "poll"`` and ``details["location"]`` -- the path
    that was polled. That is how a caller resumes: after a
    ``"auth_required"`` it refreshes the token and calls ``poll_job`` again
    with the same location, instead of re-running :func:`generate` and
    starting a second job.

    ``on_progress(msg)`` is called with a status string on each poll cycle so
    the caller can surface elapsed time in the UI.
    """
    if not location:
        raise MmcpError(
            "Server returned 202 Accepted with no Location header.",
            code="bad_response",
        )
    if not location.startswith("/"):
        location = "/" + location.split("/", 3)[-1]
    try:
        return _poll(server_url.rstrip("/") + location, retry_after, timeout,
                     access_token, on_progress)
    except MmcpError as err:
        raise _with_phase(err, "poll", location)
    except Exception as exc:
        raise _with_phase(
            MmcpError(f"Generate request failed: {exc}", code="transport"),
            "poll", location,
        ) from exc


def _poll(url, retry_after, timeout, access_token, on_progress):
    """The polling loop behind :func:`poll_job`, on a full job URL."""
    start = time.time()
    deadline = start + timeout
    headers = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    while time.time() < deadline:
        time.sleep(max(retry_after, 0.5))
        elapsed = time.time() - start
        if on_progress:
            on_progress(f"Generating… {elapsed:.0f}s")
        poll_req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(poll_req, timeout=timeout) as resp:
                if resp.status == 200:
                    body = resp.read()
                    if not body:
                        raise MmcpError(
                            "Job completed but server returned an empty response.",
                            code="bad_response",
                        )
                    # GLB is unpacked into the same glTF JSON document the
                    # JSON path returns; anything else is parsed as JSON.
                    if "model/gltf-binary" in resp.headers.get("Content-Type", ""):
                        try:
                            return glb_to_gltf(body)
                        except ValueError as exc:
                            raise MmcpError(
                                f"Job result was not a valid GLB: {exc}",
                                code="bad_response",
                            ) from exc
                    return json.loads(body.decode("utf-8"))
                if resp.status == 202:
                    retry_after = float(resp.headers.get("Retry-After") or retry_after)
                    continue
                raise MmcpError(
                    f"Poll returned HTTP {resp.status}",
                    code="http_error",
                    details={"status": resp.status},
                )
        except MmcpError:
            raise
        except urllib.error.HTTPError as exc:
            raise _error_from_http(exc, "GET", url) from exc
        except urllib.error.URLError as exc:
            raise MmcpError(
                f"Poll connection failed: {exc.reason}",
                code="connection_failed",
            ) from exc
    raise MmcpError(
        f"Async job did not complete within {timeout}s",
        code="timeout",
    )


def generate(server_url, request_body, timeout=_DEFAULT_TIMEOUT, access_token=None,
             on_progress=None, headers=None):
    """POST ``/generate`` with the given MMCP request body.

    Returns the parsed glTF 2.0 JSON document on success. Handles both the
    synchronous ``200 OK`` response and the async ``202 Accepted`` plus
    polling pattern. Raises :class:`MmcpError` on any transport or
    server-side failure.

    ``access_token`` — Bearer token, when the deployment requires one.

    ``headers`` — extra request headers (a mapping, or ``None``), merged in
    after the standard ones. The caller uses it to identify itself; the
    ``Authorization`` header is derived from ``access_token`` and wins over
    any same-named entry here.

    Every :class:`MmcpError` carries ``details["phase"]``: ``"generate"``
    when the ``POST`` itself failed, ``"poll"`` (plus ``details["location"]``)
    when the job was accepted and polling it failed -- see :func:`poll_job`
    for resuming that job instead of starting another.
    """
    try:
        return _generate(server_url, request_body, timeout, access_token,
                         on_progress, headers)
    except MmcpError as err:
        raise _with_phase(err, "generate")


def _generate(server_url, request_body, timeout, access_token, on_progress, headers):
    """The body of :func:`generate`, before its errors are tagged with a phase."""
    url = server_url.rstrip("/") + "/generate"
    payload = json.dumps(request_body).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "model/gltf+json, application/json, model/gltf-binary;q=0.9",
    }
    # whatever the caller wants the server to know about it; None when the
    # caller has nothing to say
    if headers:
        request_headers.update(headers)
    if access_token:
        request_headers["Authorization"] = f"Bearer {access_token}"
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers=request_headers)
    _t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            if status == 202:
                location = resp.headers.get("Location") or ""
                retry_after = float(resp.headers.get("Retry-After") or "2")
                return poll_job(
                    server_url, location, retry_after=retry_after, timeout=timeout,
                    access_token=access_token, on_progress=on_progress,
                )
            if status == 200:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "")
            else:
                raise MmcpError(
                    f"Unexpected status {status} from /generate",
                    code="bad_response",
                    details={"status": status},
                )
    except MmcpError:
        raise
    except urllib.error.HTTPError as exc:
        raise _error_from_http(exc, "POST", url) from exc
    except urllib.error.URLError as exc:
        raise MmcpError(
            f"Could not reach server at {server_url}: {exc.reason}",
            code="connection_failed",
        ) from exc
    except Exception as exc:
        raise MmcpError(
            f"Generate request failed: {exc}", code="transport",
        ) from exc

    # GLB is unpacked into the same glTF JSON document the JSON path returns.
    if "model/gltf-binary" in ctype:
        try:
            return glb_to_gltf(body)
        except ValueError as exc:
            raise MmcpError(
                f"Generate response was not a valid GLB: {exc} "
                f"(status={status}, Content-Type={ctype!r}, {len(body)} bytes)",
                code="bad_response",
            ) from exc

    if "json" not in ctype:
        preview = body[:200].decode("utf-8", errors="replace") if body else "<empty>"
        raise MmcpError(
            f"Unexpected Content-Type from /generate: {ctype!r} "
            f"(status={status}, body[:200]={preview!r})",
            code="bad_response",
        )

    if not body:
        raise MmcpError(
            f"Server returned an empty 200 response from /generate "
            f"(Content-Type={ctype!r}). The server likely failed silently — "
            f"check server logs.",
            code="bad_response",
        )

    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        preview = body[:200].decode("utf-8", errors="replace")
        raise MmcpError(
            f"Generate response was not valid JSON: {exc} "
            f"(status={status}, Content-Type={ctype!r}, body[:200]={preview!r})",
            code="bad_response",
        ) from exc
