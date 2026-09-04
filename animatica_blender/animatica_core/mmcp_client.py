"""HTTP client for the Kimodo MMCP server.

Uses ``urllib.request`` only — Maya's embedded Python doesn't ship with
``requests`` or ``httpx`` and we want zero new runtime dependencies.

The server contract is documented in
``D:/projects/Maya/kimodo_server`` (start_server.bat / test_connection.py)
and in this project's ``.claude/mmcp-server-integration.md``.

Two endpoints are exercised:

* ``GET /capabilities`` — model + canonical skeleton metadata. Cached
  per-URL per-session (the skeleton can't change without a server
  restart).
* ``POST /generate`` — the main generation call. Returns a glTF 2.0 JSON
  document; parsing is done in :mod:`gltf_parser`.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal, Optional


_DEFAULT_TIMEOUT = 600.0  # cloud cold-start + inference can take up to ~5 min

# MmcpError.code -> ProbeResult.status. Codes not listed (transport, timeout,
# anything unmapped) fall back to "unreachable" so probe_server never raises.
_PROBE_STATUS_BY_CODE = {
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


_capabilities_cache = {}


def get_capabilities(server_url, timeout=10.0, use_cache=True, access_token=None):
    """Fetch ``/capabilities`` and return the parsed JSON dict.

    Cached per ``(server_url, has_token)`` pair so local and cloud entries
    don't collide.  Pass ``use_cache=False`` to bypass.

    ``access_token`` — Animatica Cloud Bearer token; omit for local servers.
    """
    url = server_url.rstrip("/") + "/capabilities"
    cache_key = (url, bool(access_token))
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
        if exc.code == 401:
            raise MmcpError(
                "Authentication required. Please log in to Animatica Cloud.",
                code="auth_required",
                details={"status": 401},
            ) from exc
        raise MmcpError(
            f"GET {url} returned HTTP {exc.code}: {exc.reason}",
            code="http_error",
            details={"status": exc.code},
        ) from exc
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

    Never touches the network, so the UI can consult it on a repaint or a
    settings change without risking a 5 s stall on the main thread. The
    Model dropdown uses it: a probe fills the cache, and everything after
    reads the full model list from here instead of depending on catching
    that one probe result.
    """
    # Same key get_capabilities writes under — the full endpoint URL and a
    # BOOLEAN for the token, not the token itself. Building it differently
    # here would make this function always answer None.
    cache_key = (server_url.rstrip("/") + "/capabilities", bool(access_token))
    return _capabilities_cache.get(cache_key)


def clear_capabilities_cache():
    """Forget every cached ``/capabilities`` response. Call after the
    user changes the server URL or restarts the server."""
    _capabilities_cache.clear()


def model_supported_segments(model_info):
    """Return one model entry's advertised ``supported_segments``.

    ``model_info`` is a single entry of ``GET /capabilities .models[]``.
    Missing/null key -> ``[]``. Values are lower-cased and de-duplicated
    (order preserved) so callers can test membership directly. Used to gate
    the official MMCP ``PoseSegment`` path versus the 2-frame ``text``
    fallback in :func:`core.request_builder.build_pose_request`.
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
    seconds in. ``unknown`` covers servers predating the field, and is
    never a reason to refuse: keep today's behaviour and let the server
    answer.
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

    Every generation path used to hard-code ``caps["models"][0]``, so the
    tool's Model dropdown selected a name that nothing ever read and every
    request went to whichever model the server happened to list first.
    This is the one place that resolves the choice.

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
    status:       Literal["online", "unreachable", "auth_required", "http_error", "bad_response"]
    message:      str
    capabilities: Optional[dict] = None


def probe_server(server_url, *, timeout=5.0, access_token=None) -> ProbeResult:
    """Probe ``/capabilities`` and return a structured :class:`ProbeResult`.

    Shared by the Test button and the auto-probe so neither has to inspect
    error strings. Always bypasses the capabilities cache for a true liveness
    check. Never raises :class:`MmcpError` — every failure maps to a status.

    ``access_token`` — Animatica Cloud Bearer token; omit for local servers.
    """
    try:
        caps = get_capabilities(
            server_url, timeout=timeout, use_cache=False, access_token=access_token,
        )
    except MmcpError as exc:
        status = _PROBE_STATUS_BY_CODE.get(exc.code, "unreachable")
        return ProbeResult(ok=False, status=status, message=str(exc))
    return ProbeResult(ok=True, status="online", message="Server is reachable.", capabilities=caps)


def _poll_job(server_url, location, retry_after, timeout, access_token, on_progress=None):
    """Poll an async job URL (202 pattern) until it returns 200 or timeout.

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
    url = server_url.rstrip("/") + location
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
            if exc.code == 401:
                raise MmcpError(
                    "Authentication required. Please log in to Animatica Cloud.",
                    code="auth_required",
                    details={"status": 401},
                ) from exc
            raise MmcpError(
                f"Poll failed: HTTP {exc.code}",
                code="http_error",
                details={"status": exc.code},
            ) from exc
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
             on_progress=None):
    """POST ``/generate`` with the given MMCP request body.

    Returns the parsed glTF 2.0 JSON document on success. Handles both the
    synchronous ``200 OK`` response and the async ``202 Accepted`` + polling
    pattern used by the Animatica Cloud. Raises :class:`MmcpError` on any
    transport or server-side failure.

    ``access_token`` — Animatica Cloud Bearer token; omit for local servers.
    """
    url = server_url.rstrip("/") + "/generate"
    payload = json.dumps(request_body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "model/gltf+json, application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    _t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            if status == 202:
                location = resp.headers.get("Location") or ""
                retry_after = float(resp.headers.get("Retry-After") or "2")
                return _poll_job(
                    server_url, location, retry_after, timeout, access_token,
                    on_progress=on_progress,
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
        if exc.code == 401:
            raise MmcpError(
                "Authentication required. Please log in to Animatica Cloud.",
                code="auth_required",
                details={"status": 401},
            ) from exc
        try:
            err_body = exc.read().decode("utf-8")
            err = json.loads(err_body).get("error", {})
            raise MmcpError(
                err.get("message", f"HTTP {exc.code}"),
                code=err.get("code", "http_error"),
                details=err.get("details", {"status": exc.code}),
            ) from exc
        except MmcpError:
            raise
        except Exception:
            raise MmcpError(
                f"POST {url} returned HTTP {exc.code}: {exc.reason}",
                code="http_error",
                details={"status": exc.code},
            ) from exc
    except urllib.error.URLError as exc:
        raise MmcpError(
            f"Could not reach server at {server_url}: {exc.reason}",
            code="connection_failed",
        ) from exc
    except Exception as exc:
        raise MmcpError(
            f"Generate request failed: {exc}", code="transport",
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
