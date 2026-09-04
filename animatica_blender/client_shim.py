"""Blender-side glue in front of the shared MMCP transport.

The HTTP work — ``GET /capabilities``, ``POST /generate``, the ``202
Accepted`` poll loop, the error envelope — lives in
:mod:`animatica_core.mmcp_client` and is shared with the SDK. This module
keeps only what is ``bpy``-flavoured and therefore cannot be shared:

  * the server URL resolved from addon preferences (cloud vs self-hosted),
  * the Animatica Cloud session (``/auth/login``, ``/auth/refresh``, tokens
    stored on ``AddonPreferences``),
  * the process-wide capabilities cache that backs the Model
    ``EnumProperty`` and the "Connection failed" banners.

Threading — why ``generate()`` takes its token as an argument
-------------------------------------------------------------
The generate operators run the POST on a worker thread while a modal
timer keeps the UI alive. ``bpy.context.preferences`` must not be touched
from that thread, so both the server URL and the access token are read on
the main thread (in ``execute()``) and handed to the worker as arguments.

The consequence, decided deliberately: **``generate()`` does not retry
after an expired token.** The retry needs ``refresh_access_token()``,
which *writes* back to ``AddonPreferences`` — a main-thread-only
operation. The old in-addon transport did that silent refresh-and-retry
from inside the worker thread; here it survives only on the main-thread
path, :func:`fetch_capabilities` (Connect, and the canonical-skeleton
import). An ``auth_required`` raised on the worker thread propagates to
the modal, which reports it; the user reconnects or signs in and
generates again.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import bpy

import animatica_core.mmcp_client as _core

# Re-exported so ``except client_shim.MmcpError`` keeps catching what the
# shared transport raises. Same class object, not a subclass.
MmcpError = _core.MmcpError

# Generation can take minutes (cloud cold start + inference).
DEFAULT_TIMEOUT_SECONDS = 600


# ---------------------------------------------------------------------------
# Preferences plumbing
# ---------------------------------------------------------------------------

def get_server_url() -> str:
    """Return the bare configured server URL (no trailing slash, no path prefix).

    For Animatica Cloud this is ``https://api.animatica.ai`` — the host
    that owns ``/auth/*``, ``/account``, etc. For self-hosted users
    it's the override URL they typed.

    Use ``get_mmcp_url()`` if you need the base for MMCP requests
    specifically; on cloud those live one path-segment deeper, behind
    the auth proxy.
    """
    from .properties import CLOUD_API_URL
    addon = bpy.context.preferences.addons.get(__package__)
    if addon is None:
        return CLOUD_API_URL
    prefs = addon.preferences
    if getattr(prefs, "self_hosted", False):
        url = (prefs.server_url or "").strip()
        return (url or "http://localhost:8000").rstrip("/")
    return CLOUD_API_URL.rstrip("/")


def get_mmcp_url() -> str:
    """Return the base URL for MMCP requests (``/capabilities``, ``/generate``).

    On Animatica Cloud the MMCP server sits behind an auth/quota proxy
    at ``api.animatica.ai/mmcp``; on self-hosted setups the MMCP server
    *is* the user's server, so there's no path prefix.
    """
    from .properties import CLOUD_API_URL
    addon = bpy.context.preferences.addons.get(__package__)
    if addon is None:
        return f"{CLOUD_API_URL.rstrip('/')}/mmcp"
    prefs = addon.preferences
    if getattr(prefs, "self_hosted", False):
        url = (prefs.server_url or "").strip()
        return (url or "http://localhost:8000").rstrip("/")
    return f"{CLOUD_API_URL.rstrip('/')}/mmcp"


# ---------------------------------------------------------------------------
# Auth — Animatica Cloud only. Self-hosted servers ignore the Authorization
# header. Auth is NOT part of the MMCP protocol; the cloud's proxy in front
# of /generate is what consumes the token. The plugin treats it as
# "attach if present, prompt to sign in on 401".
# ---------------------------------------------------------------------------

def _addon_prefs():
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


def get_access_token() -> str:
    p = _addon_prefs()
    return ((getattr(p, "access_token", "") or "").strip()) if p else ""


def get_refresh_token() -> str:
    p = _addon_prefs()
    return ((getattr(p, "refresh_token", "") or "").strip()) if p else ""


def sign_in(email: str, password: str) -> dict[str, Any]:
    """POST /auth/login on the cloud auth proxy. Stores tokens on AddonPreferences.

    The /auth/* endpoints live at the bare host (``api.animatica.ai/auth/login``),
    not under ``/mmcp/``, so this always uses ``get_server_url()`` rather
    than the MMCP base. Self-hosted setups don't sign in at all. Auth is
    outside the MMCP protocol, hence outside the shared transport too.
    """
    base_url = get_server_url()
    body = json.dumps({"email": email, "password": password, "client": "blender"}).encode()
    req = Request(
        f"{base_url}/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as exc:
        try:
            err = (json.loads(exc.read()) or {}).get("error") or {}
        except Exception:                                # noqa: BLE001 — best effort
            err = {}
        raise MmcpError(
            err.get("message") or f"HTTP {exc.code}: {exc.reason}",
            code=err.get("code", "http_error"),
            details=err.get("details") or {"status": exc.code},
        ) from exc
    except URLError as exc:
        raise MmcpError(
            f"cannot reach {base_url}: {exc.reason}",
            code="connection_failed",
        ) from exc

    p = _addon_prefs()
    if p is not None:
        p.access_token = data.get("access_token", "")
        p.refresh_token = data.get("refresh_token", "")
        p.email = data.get("email", email)
        p.tier = data.get("tier", "")
    return data


def sign_out() -> None:
    """Forget cached tokens. The server-side session may still be valid;
    self-clear is enough for the plugin's purposes."""
    p = _addon_prefs()
    if p is not None:
        p.access_token = ""
        p.refresh_token = ""
        p.email = ""
        p.tier = ""


def refresh_access_token() -> bool:
    """POST /auth/refresh. Returns True on success and updates prefs.

    Always hits the bare cloud auth host (``get_server_url()``), not the
    MMCP base — ``/auth/refresh`` lives outside the ``/mmcp/`` namespace.

    Main thread only: it writes back to ``AddonPreferences``.
    """
    rt = get_refresh_token()
    if not rt:
        return False
    base_url = get_server_url()
    req = Request(
        f"{base_url}/auth/refresh",
        data=json.dumps({"refresh_token": rt}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception:                                    # noqa: BLE001 — defensive
        return False
    p = _addon_prefs()
    if p is None:
        return False
    new_access = data.get("access_token", "")
    if not new_access:
        return False
    p.access_token = new_access
    p.refresh_token = data.get("refresh_token", rt)
    if data.get("tier"):
        p.tier = data["tier"]
    return True


# ---------------------------------------------------------------------------
# Process-wide capabilities cache.
#
# Distinct from the shared transport's own per-URL cache: this one carries the
# EnumProperty items list, whose strings must stay alive for as long as Blender
# holds references to them — hence the module-level list. The Connect operator
# writes here once on success; the items callback in ``properties.py`` reads
# from here. That's why ``fetch_capabilities`` bypasses the core cache.
# ---------------------------------------------------------------------------

_CAPABILITIES: dict[str, Any] | None = None
_MODEL_ITEMS: list[tuple[str, str, str]] = []
_LAST_ERROR: str = ""


def cached_capabilities() -> dict[str, Any] | None:
    return _CAPABILITIES


def cached_model_items() -> list[tuple[str, str, str]]:
    """Return a static list of ``(id, label, description)`` tuples for use
    as ``EnumProperty(items=...)`` values.

    Returns at least one entry so Blender always has a valid default; when no
    capabilities have been fetched, returns a sentinel item that's clearly
    not a real model id.
    """
    if _MODEL_ITEMS:
        return _MODEL_ITEMS
    return [("", "(connect to discover models)", "")]


def cached_model(model_id: str) -> dict[str, Any] | None:
    if _CAPABILITIES is None:
        return None
    for m in _CAPABILITIES.get("models", []):
        if m.get("id") == model_id:
            return m
    return None


def store_capabilities(caps: dict[str, Any]) -> None:
    """Replace the cache and rebuild the EnumProperty items list."""
    global _CAPABILITIES, _MODEL_ITEMS, _LAST_ERROR
    _CAPABILITIES = caps
    _LAST_ERROR = ""
    items: list[tuple[str, str, str]] = []
    for m in caps.get("models", []):
        mid = m.get("id", "")
        if not mid:
            continue
        fps = m.get("fps", "?")
        joints = len(m.get("canonical_skeleton", {}).get("joints", []))
        items.append((mid, mid, f"{joints} joints @ {fps} fps"))
    _MODEL_ITEMS = items


def clear_capabilities(error: str = "") -> None:
    global _CAPABILITIES, _MODEL_ITEMS, _LAST_ERROR
    _CAPABILITIES = None
    _MODEL_ITEMS = []
    _LAST_ERROR = error


def last_connection_error() -> str:
    return _LAST_ERROR


# ---------------------------------------------------------------------------
# Transport — thin wrappers over animatica_core.mmcp_client
# ---------------------------------------------------------------------------

def describe_error(exc: BaseException) -> str:
    """Render an exception for ``self.report``, keeping the MMCP code visible.

    ``str(core.MmcpError)`` is the bare message; the addon's own transport
    used to render ``"<code>: <message>"`` and the panels' "Connection
    failed" box has always shown that code. This keeps it.
    """
    code = getattr(exc, "code", "")
    return f"{code}: {exc}" if code else str(exc)


def fetch_capabilities(timeout: float = 30) -> dict[str, Any]:
    """``GET /capabilities`` against the configured server. Main thread only.

    Bypasses the shared transport's cache — the addon keeps its own
    (see above), and Connect / the canonical-skeleton import both mean
    "ask the server again". Retries once after a silent token refresh
    when the proxy says the session expired.
    """
    url = get_mmcp_url()
    try:
        return _core.get_capabilities(
            url, timeout=timeout, use_cache=False, access_token=get_access_token(),
        )
    except MmcpError as exc:
        if exc.code != "auth_required" or not refresh_access_token():
            raise
    return _core.get_capabilities(
        url, timeout=timeout, use_cache=False, access_token=get_access_token(),
    )


def generate(
    request_body: dict[str, Any],
    *,
    access_token: str,
    server_url: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """``POST /generate``; returns the parsed glTF JSON document.

    Callable from a worker thread — and only safe there because nothing in
    this function reads ``bpy``. ``access_token`` and ``server_url`` must
    both be resolved on the main thread by the caller (``server_url``
    defaults to ``get_mmcp_url()`` for main-thread callers). See this
    module's docstring for why an expired token is not retried here.
    """
    return _core.generate(
        server_url or get_mmcp_url(),
        request_body,
        timeout=timeout,
        access_token=access_token,
    )
