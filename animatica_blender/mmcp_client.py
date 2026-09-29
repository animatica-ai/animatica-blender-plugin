"""Minimal MMCP HTTP client.

Stdlib-only — Blender ships its own Python interpreter and adding a third-party
``requests`` dependency makes installation finicky. ``urllib`` is enough. The
wire format (Accept, GLB, the ``202`` poll loop, the error envelope) is the
vendored ``motionmcp.client``'s; this module owns the gate, the cloud session
and the error codes the UI branches on.

Public surface:
  * ``get_server_url()`` — read the configured base URL from addon prefs.
  * ``MmcpError`` — typed exception carrying the MMCP error envelope.
  * ``MmcpClient.capabilities()`` — cached ``GET /capabilities``.
  * ``MmcpClient.generate(req)`` — synchronous ``POST /generate`` returning a
    parsed glTF JSON document. Handles the optional ``202 Accepted`` async
    poll loop transparently when the model declares ``supports_async``.
  * ``client_headers()`` — the ``X-Animatica-*`` attribution headers the API
    buckets generations by.

Threading: the addon calls these from a worker thread; nothing here touches
``bpy`` so it's safe.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import traceback
import types
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

import bpy

from .vendor.motionmcp.client import http as _http
from .vendor.motionmcp.client.http import MmcpError as SdkError
# Bound at import on purpose: the helper is private to the SDK, so a bump
# that renames it must fail here, at load, not inside sign_in later.
_sdk_error_from_http = _http._error_from_http


# ---------------------------------------------------------------------------
# Client attribution
#
# The API records which DCC a generation came from, so that DCC mix and
# failure rate per host version are answerable and a new addon build can be
# watched against the old one. Sent on the request that STARTS a generation
# and nowhere else: the ``202`` poll follow-ups and ``GET /capabilities`` are
# not generations, and labelling them would skew whatever the buckets are
# counted against.
#
# Attribution only, never auth: a wrong or missing value costs a row in a
# dashboard, never a request, so every value below degrades to an omitted
# header rather than raising.
# ---------------------------------------------------------------------------

#: Must be exactly one of the API's accepted values (``blender``, ``maya``,
#: ``3dsmax``, ``motionbuilder``, ``desktop``, ``web``, ``cli``). Anything else
#: is recorded as ``other``, which is indistinguishable from not reporting.
CLIENT_NAME = "blender"

#: The API drops any value longer than this.
_MAX_HEADER_VALUE = 64

#: One id per addon launch, shared by every generation fired from it. Module
#: scope, so enabling the addon — or reloading it — starts a fresh session.
SESSION_ID = str(uuid.uuid4())


def _addon_version() -> str:
    """The addon build, from ``bl_info``.

    Read off the already-imported package rather than imported from it:
    ``__init__`` imports this module, so a top-level import would be circular.
    ``bl_info`` is assigned above those imports, so it is always in place by
    the time this runs.
    """
    mod = sys.modules.get(__package__ or "")
    version = (getattr(mod, "bl_info", None) or {}).get("version")
    return ".".join(str(part) for part in version) if version else ""


def _host_version() -> str:
    """The Blender this is running in, as a bare numeric triple.

    ``bpy.app.version_string`` carries build noise ("4.2.0 Alpha") that would
    fragment the per-host-version view this attribution exists to feed, so the
    numeric tuple is sent instead.
    """
    version = getattr(bpy.app, "version", None)
    return ".".join(str(part) for part in version) if version else ""


def _build_client_headers() -> dict[str, str]:
    hdrs = {"X-Animatica-Client": CLIENT_NAME}
    addon = _addon_version()
    if addon:
        hdrs["X-Animatica-Client-Version"] = addon[:_MAX_HEADER_VALUE]
    host = _host_version()
    if host:
        hdrs["X-Animatica-Host-Version"] = host[:_MAX_HEADER_VALUE]
    hdrs["X-Animatica-Session-Id"] = SESSION_ID
    return hdrs


# Computed once, at import, on the main thread: ``generate`` posts from a
# worker thread and must not read ``bpy`` there.
_CLIENT_HEADERS: dict[str, str] = _build_client_headers()


def client_headers() -> dict[str, str]:
    """The attribution headers for a generation request (a fresh copy)."""
    return dict(_CLIENT_HEADERS)


# ---------------------------------------------------------------------------
# Online access
#
# Blender's "Allow Online Access" (Preferences > System) is the artist's say
# on whether anything may reach the network. Every request the addon makes
# asks here first. A self-hosted server on this machine (localhost) is not
# "online", so it keeps working with the switch off; anything else waits.
# ---------------------------------------------------------------------------

#: What to show wherever a feature needs the network and is not allowed it.
OFFLINE_MESSAGE = "Online access is disabled in Preferences > System"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def online_access() -> bool:
    """True when Blender allows online access (always, on a Blender without the switch)."""
    return bool(getattr(bpy.app, "online_access", True))


def _host_of(url: str) -> str | None:
    """The host a request to *url* would connect to — or None if that is in doubt.

    Parsed the way urllib will parse it, and refused outright where urllib and
    ``urlsplit`` could disagree: a backslash (``http://example.com\\@127.0.0.1/``
    is example.com to one and 127.0.0.1 to the other), userinfo (``user@host``),
    and whitespace or control characters anywhere in the URL.
    """
    if not isinstance(url, str) or "\\" in url:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
        if "@" in parts.netloc:
            return None
        return (parts.hostname or "").lower() or None
    except ValueError:
        return None


def is_loopback(url: str) -> bool:
    host = _host_of(url)
    return host is not None and (host in _LOOPBACK_HOSTS or host.endswith(".localhost"))


def may_connect(url: str) -> bool:
    """True if a request to *url* is allowed right now."""
    return online_access() or is_loopback(url)


def popup(title: str, lines, icon: str = 'INFO') -> bool:
    """Show a small popup from anywhere on the main thread — a timer included.

    ``popup_menu`` needs a window in the context, and a timer has none: called
    bare there (or in a background Blender) it crashes Blender outright. So it
    is given the first window explicitly, and skipped when there is none.
    """
    if bpy.app.background:
        return False
    wm = getattr(bpy.context, "window_manager", None)
    windows = list(getattr(wm, "windows", ()) or ())
    if not windows:
        return False
    window = bpy.context.window or windows[0]

    def _draw(menu, _context):
        col = menu.layout.column(align=True)
        for line in lines:
            col.label(text=line)
    try:
        with bpy.context.temp_override(window=window, screen=window.screen):
            wm.popup_menu(_draw, title=title, icon=icon)
        return True
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()
        return False


def draw_offline(layout, text: str = OFFLINE_MESSAGE) -> None:
    """The offline line, with the button that goes where the switch is."""
    col = layout.column(align=True)
    col.label(text=text, icon='INTERNET_OFFLINE')
    # A row of its own: sharing one with a sentence this long elides the sentence.
    col.operator("animatica.open_online_prefs", text="Open Preferences", icon='PREFERENCES')


def _require_online(url: str) -> None:
    if not may_connect(url):
        raise MmcpError(code="offline", message=OFFLINE_MESSAGE)


# ---------------------------------------------------------------------------
# Where the session token may go
#
# The Animatica session belongs to Animatica Cloud and to nothing else: not a
# self-hosted server (which may be anyone's, and is often plain http), not a
# host a redirect points at. Toggling self-hosted keeps the session — it just
# stays home.
# ---------------------------------------------------------------------------

def _origin(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return ("", "")
    return (parts.scheme.lower(), parts.netloc.lower())


def is_cloud_url(url: str) -> bool:
    """True if *url* is on Animatica Cloud, over https — the only place a token goes."""
    from .properties import CLOUD_API_URL
    scheme, netloc = _origin(url)
    return scheme == "https" and (scheme, netloc) == _origin(CLOUD_API_URL)


_OPENER = None


def _opener():
    """The one opener every request of this addon goes through.

    It asks :func:`may_connect` about every request it opens — redirect hops
    included, so a server on this machine cannot bounce a request off it
    with online access switched off — and drops ``Authorization`` when a
    redirect leaves the host.
    """
    global _OPENER
    if _OPENER is None:
        from .autoposer.vendor.autoposer_runtime import bundle
        bundle.GATE = may_connect
        _OPENER = bundle.build_opener()
    return _OPENER


def urlopen(req, timeout: float):
    """``urllib.request.urlopen``, through the gated opener. Any thread."""
    return _opener().open(req, timeout=timeout)


_open = urlopen


def refused(exc: BaseException) -> bool:
    """Was *exc* the gate refusing a request (online access is off)?"""
    from .autoposer.vendor.autoposer_runtime.bundle import RequestRefused
    return isinstance(exc, RequestRefused)


# ---------------------------------------------------------------------------
# Handing results to the main thread
#
# Workers never call ``bpy`` — not even ``bpy.app.timers.register``, which is
# not safe off the main thread. They post a callable here, and a timer that
# the main thread started runs it. The timer stops itself when there is
# nothing left to do, and whatever next runs on the main thread (a connect, a
# panel asking whether the session expired) starts it again.
# ---------------------------------------------------------------------------

_MAIN_QUEUE: "queue.Queue" = queue.Queue()


def post_to_main(fn) -> None:
    """Run *fn* on the main thread, soon. Safe from any thread."""
    _MAIN_QUEUE.put(fn)
    if threading.current_thread() is threading.main_thread():
        _start_pump()


def _pump():
    # Ask what is still in flight BEFORE draining: a worker posts its result
    # and only then says it is done, so anything that has said so has
    # already posted, and the drain below picks it up.
    busy = bool(_CONNECT["running"])
    for pred in list(_BUSY):
        try:
            if pred():
                busy = True
            else:
                _BUSY.discard(pred)
        except Exception:                                   # noqa: BLE001
            _BUSY.discard(pred)
    while True:
        try:
            fn = _MAIN_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            fn()
        except Exception:                                   # noqa: BLE001
            traceback.print_exc()
    return 0.2 if (busy or not _MAIN_QUEUE.empty()) else None


#: Work in flight elsewhere (a download on a worker) that will post a result:
#: the pump keeps going while any of these says True.
_BUSY: set = set()


def pump_while(pred) -> None:
    """Keep handing results over while ``pred()`` is True. Main thread only."""
    _BUSY.add(pred)
    _start_pump()


def _start_pump() -> None:
    """Main thread only."""
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        if not bpy.app.timers.is_registered(_pump):
            bpy.app.timers.register(_pump, first_interval=0.0)
    except Exception:                                       # noqa: BLE001
        traceback.print_exc()


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
    *is* the user's server, so there's no path prefix. Either way the
    plugin's ``MmcpClient`` should be constructed with this URL — it
    appends ``/capabilities`` etc. directly.
    """
    from .properties import CLOUD_API_URL
    if not _MAIN_QUEUE.empty():
        _start_pump()
    addon = bpy.context.preferences.addons.get(__package__)
    if addon is None:
        return f"{CLOUD_API_URL.rstrip('/')}/mmcp"
    prefs = addon.preferences
    if getattr(prefs, "self_hosted", False):
        url = (prefs.server_url or "").strip()
        return (url or "http://localhost:8000").rstrip("/")
    return f"{CLOUD_API_URL.rstrip('/')}/mmcp"


# ---------------------------------------------------------------------------
# Auth — Animatica Cloud only. The token is attached only to requests for
# the cloud host over https (see ``is_cloud_url``); a self-hosted server never
# receives it. Auth is NOT part of the MMCP protocol; the cloud's proxy in
# front of /generate is what consumes the token. The plugin treats it as
# "attach if present, prompt to sign in on the cloud's 401".
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
    than the MMCP base. Self-hosted setups don't sign in at all — and the
    password goes to Animatica Cloud whatever the server setting says.
    """
    from .properties import CLOUD_API_URL
    base_url = CLOUD_API_URL.rstrip("/")
    _require_online(base_url)
    body = json.dumps({"email": email, "password": password, "client": "blender"}).encode()
    req = Request(
        f"{base_url}/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with _open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as exc:
        # The SDK's envelope parser, so a login error reads like any other.
        raise MmcpError.from_sdk(_sdk_error_from_http(exc, "POST", req.full_url)) from exc
    except URLError as exc:
        if refused(exc):
            raise MmcpError(code="offline", message=OFFLINE_MESSAGE) from exc
        raise MmcpError(
            code="model_unavailable",
            message=f"cannot reach {base_url}: {exc.reason}",
        ) from exc

    p = _addon_prefs()
    if p is not None:
        p.access_token = data.get("access_token", "")
        p.refresh_token = data.get("refresh_token", "")
        p.email = data.get("email", email)
        p.tier = data.get("tier", "")
    clear_session_expired()
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


#: Why the session ended, when it ended by itself rather than by a click. Read
#: by the panels so the artist is told what happened where they would look.
_EXPIRED: dict = {"reason": ""}


def session_expired() -> str:
    if _EXPIRED.get("pending"):
        _start_pump()               # a worker expired it; the panels asking is our cue
    return _EXPIRED["reason"]


def clear_session_expired() -> None:
    _EXPIRED["reason"] = ""


def expire_session(reason: str = "your session has expired") -> None:
    """The server says this token is no good, and refreshing it did not help.

    Keeping a token the server rejects signs the artist in on paper only: the
    panel says "Signed in", every generation fails the same way, and the one
    action that would fix it — sign in again — is not offered because we still
    look signed in. So the token goes, and the sign-in prompt comes back with
    a sentence saying why it is there.

    Called from worker threads, so the actual write is handed to the main
    thread: preferences are Blender data like anything else.
    """
    if not (get_access_token() or get_refresh_token()):
        return                      # nothing to expire; a self-hosted server, most likely
    _EXPIRED["pending"] = reason
    post_to_main(_apply_expiry)


def _apply_expiry():
    """The main-thread half of ``expire_session``."""
    reason = _EXPIRED.pop("pending", "")
    if not reason:
        return None
    sign_out()
    _EXPIRED["reason"] = reason
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


def refresh_access_token() -> bool:
    """POST /auth/refresh. Returns True on success and updates prefs.

    Always hits the cloud auth host (``CLOUD_API_URL``) — never the
    self-hosted URL — and ``/auth/refresh`` lives outside ``/mmcp/``.
    """
    from .properties import CLOUD_API_URL
    rt = get_refresh_token()
    if not rt:
        return False
    base_url = CLOUD_API_URL.rstrip("/")
    if not is_cloud_url(base_url) or not may_connect(base_url):
        return False
    req = Request(
        f"{base_url}/auth/refresh",
        data=json.dumps({"refresh_token": rt}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with _open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
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
# The Connect operator writes here once on success; the EnumProperty items
# callback in ``properties.py`` reads from here. Strings in the items list
# must stay alive for as long as Blender holds references to them, hence the
# module-level list.
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
# Connecting, without being asked
#
# Fetching /capabilities is not a decision — it is how the addon finds out
# which models exist so it can show them. Making the artist press Connect
# first put a step in front of everything else that could only ever be
# answered one way. It happens on its own now: at startup, after a file load,
# after signing in, and again on its own schedule if the network was not there
# the first time.
# ---------------------------------------------------------------------------

#: Seconds before a failed attempt is worth repeating. Long enough not to
#: hammer a server that is down, short enough that coming back from a dropped
#: network or a VPN does not need a click.
CONNECT_RETRY_SECONDS = 20.0

_CONNECT = {"running": False, "at": 0.0}


def connecting() -> bool:
    return bool(_CONNECT["running"])


def connect_async(*, force: bool = False) -> bool:
    """Fetch capabilities on a worker thread. True if an attempt started.

    Safe to call from anywhere, including a draw callback: it starts a thread
    and touches no Blender data. The result is applied on the main thread.
    """
    import threading
    import time as _time

    if _CONNECT["running"] or (_CAPABILITIES is not None and not force):
        return False
    if not force and _time.monotonic() - _CONNECT["at"] < CONNECT_RETRY_SECONDS:
        return False        # tried recently and it did not work
    url = get_mmcp_url()
    if not may_connect(url):
        # Said, not attempted: the panels show this line instead of "Connecting…".
        clear_capabilities(error=OFFLINE_MESSAGE)
        return False
    _CONNECT["running"] = True
    _CONNECT["at"] = _time.monotonic()

    def _work():
        caps, error = None, ""
        try:
            caps = MmcpClient(url, timeout=30).capabilities(refresh=True)
        except Exception as exc:                            # noqa: BLE001
            error = str(exc)
        # Posted before saying "done", so the pump cannot stop in between.
        post_to_main(lambda: _apply_connection(caps, error, url))
        _CONNECT["running"] = False

    threading.Thread(target=_work, daemon=True).start()
    _start_pump()
    return True


def _apply_connection(caps, error: str, url: str):
    """Land the result on the main thread, where Blender data may be touched."""
    if caps is None:
        clear_capabilities(error=error or f"no answer from {url}")
    else:
        store_capabilities(caps)
        models = [m.get("id") for m in caps.get("models", []) if m.get("id")]
        for scene in getattr(bpy.data, "scenes", ()):
            settings = getattr(scene, "animatica", None)
            if settings is None or not models or settings.model_id in models:
                continue
            try:
                settings.model_id = models[0]
            except TypeError:
                pass
    wm = getattr(bpy.context, "window_manager", None)
    for window in getattr(wm, "windows", ()):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MmcpError(Exception):
    """Wraps the MMCP error envelope.

    ``code`` is the protocol code (``unknown_model``, ``constraint_conflict``,
    …); ``status`` is the HTTP status; ``details`` is the optional dict from
    the envelope. The string form is ``"<code>: <message>"`` so it's safe to
    surface in a Blender ``self.report({'ERROR'}, str(exc))`` call.
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    @classmethod
    def from_sdk(cls, exc: SdkError) -> "MmcpError":
        """The SDK's error, in the codes the addon's UI branches on.

        Envelope codes (``quota_exceeded``, ``unknown_model``, …) pass through.
        The SDK's ``connection_failed`` is split: the gate refusing is
        ``offline`` (the artist can fix that in Preferences), anything else is
        ``model_unavailable``.
        """
        details = dict(exc.details or {})
        code, message = exc.code, str(exc)
        if code == "connection_failed":
            if refused(exc.__cause__):
                code, message = "offline", OFFLINE_MESSAGE
            else:
                code = "model_unavailable"
        return cls(code=code, message=message, status=details.get("status"), details=details)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MmcpClient:
    """Single-purpose MMCP client.

    Construct with a base URL (typically from ``get_server_url()``). The
    capabilities response is cached on first read and can be refreshed by
    calling ``capabilities(refresh=True)``.
    """

    DEFAULT_TIMEOUT_SECONDS = 600  # generation can take minutes

    def __init__(self, base_url: str | None = None, *, timeout: float | None = None):
        self.base_url = (base_url or get_server_url()).rstrip("/")
        self.timeout = timeout or self.DEFAULT_TIMEOUT_SECONDS
        self._caps: dict[str, Any] | None = None

    # --- Capabilities ------------------------------------------------------

    def capabilities(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._caps is None or refresh:
            _require_online(f"{self.base_url}/capabilities")
            self._caps = self._call(lambda: _http.get_capabilities(
                self.base_url, timeout=self.timeout, use_cache=not refresh,
                access_token=self._token()))
        return self._caps

    def model(self, model_id: str) -> dict[str, Any]:
        for m in self.capabilities().get("models", []):
            if m.get("id") == model_id:
                return m
        available = [m.get("id") for m in self.capabilities().get("models", [])]
        raise MmcpError(
            code="unknown_model",
            message=f"model {model_id!r} is not in /capabilities",
            details={"available_models": available},
        )

    # --- Generation --------------------------------------------------------

    def generate(self, request_body: dict[str, Any]) -> dict[str, Any]:
        """POST a GenerateRequest. Returns the parsed glTF JSON document.

        The SDK handles sync (200), async (202 + Location) and GLB answers;
        every request it opens — the poll GETs included — passes the gate.
        On the cloud's 401 we attempt one silent token refresh + retry.
        """
        _require_online(f"{self.base_url}/generate")
        # ``client_headers()``: this is the request that starts a generation —
        # the one place the API wants attributed. The SDK sends them on the
        # POST only, never on the polls.
        return self._call(lambda: _http.generate(
            self.base_url, request_body, timeout=self.timeout,
            access_token=self._token(), headers=client_headers()))

    def generate_batch(self, requests: list[dict[str, Any]]) -> list:
        """Several requests in one POST /generate (an MMCP batch, for a model
        that advertises ``supports_batch``). One entry per request, in order:
        the glTF document, or the :class:`MmcpError` that request came back
        with. A failure of the whole call raises, as ``generate`` does."""
        body = {
            "protocol_version": requests[0].get("protocol_version", "1.0"),
            "requests": [{k: v for k, v in r.items() if k != "protocol_version"}
                         for r in requests],
        }
        results = (self.generate(body) or {}).get("results") or []
        if len(results) != len(requests):
            raise MmcpError(code="internal_error",
                            message=f"{len(results)} results for {len(requests)} requests")
        out: list = []
        for item in results:
            if "gltf" in item:
                out.append(item["gltf"])
            else:
                err = item.get("error") or {}
                out.append(MmcpError(code=err.get("code", "internal_error"),
                                     message=err.get("message", "failed"),
                                     details={k: v for k, v in err.items()
                                              if k not in ("code", "message")}))
        return out

    # --- Internal HTTP -----------------------------------------------------

    def _token(self) -> str | None:
        """The Bearer for this server: the session on the cloud, nothing elsewhere.

        Asked on every attempt, so the retry after a refresh carries the new one.
        """
        if not is_cloud_url(self.base_url):
            return None
        return get_access_token() or None

    def _call(self, fn):
        """Run one SDK call; map its errors, and own the cloud session on 401.

        Only the cloud's 401 is about the cloud session: a self-hosted server
        saying no must not sign anyone out. One refresh, one retry — a token
        the server still rejects after that is expired, not retried forever.
        A 401 met while polling resumes the same job instead of retrying.
        """
        try:
            return fn()
        except SdkError as exc:
            if exc.code != "auth_required" or not is_cloud_url(self.base_url):
                raise MmcpError.from_sdk(exc) from exc
            if not refresh_access_token():
                expire_session("your session expired — sign in again")
                raise MmcpError.from_sdk(exc) from exc
            # A token that expired while a job was polling: the job is still
            # running on the server, so resume polling it (``poll_job``) rather
            # than re-run ``fn`` — a second POST would start, and be billed as,
            # a second generation.
            location = exc.details.get("location") if exc.details.get("phase") == "poll" else None
        try:
            if location:
                return _http.poll_job(self.base_url, location, timeout=self.timeout,
                                      access_token=self._token())
            return fn()
        except SdkError as exc:
            if exc.code == "auth_required":
                expire_session("your session expired — sign in again")
            raise MmcpError.from_sdk(exc) from exc


# ---------------------------------------------------------------------------
# The gate, inside the SDK
#
# ``motionmcp.client.http`` opens every request through its module attribute
# ``urllib.request.urlopen``. Pointing that attribute — in the vendored copy
# only, not the process-wide ``urllib`` — at our ``urlopen`` puts the SDK's
# POST, poll GETs and ``/capabilities`` behind the same gate and the same
# redirect handling as every other request of this addon. ``Request``,
# ``HTTPError`` and ``URLError`` are the only other names it reads. Pinned to
# the vendored 0.9.0; the tests check it is installed and that it works.
# ---------------------------------------------------------------------------

def _install_gate() -> None:
    _http.urllib = types.SimpleNamespace(
        request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen),
        error=urllib.error,
    )


_install_gate()
