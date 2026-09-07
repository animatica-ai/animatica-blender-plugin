"""One way for a gate to reach an MMCP server — local or the cloud.

The acceptance gates were written against the local server, where a bare
``urlopen(f"{server}/generate", ...)`` is the whole story. Against
``https://api.animatica.ai/mmcp`` it is not: measured 2026-09-05,

* ``/generate`` answers **401** without a Bearer token, so every gate that
  generates fails there for a reason that has nothing to do with what it
  tests;
* ``/capabilities`` answers 200 to an anonymous caller but lists only the
  lowest tier's models (one instead of two — the registry gates visibility
  by tier), so even the *reachable* answer is a different answer;
* the access token lives about an hour, and a gate suite outlives it.

So this module signs every call with the PRODUCT's own session — the token
file ``animatica_auth`` writes, never an environment variable, never
printed — and, on a 401, refreshes once and retries exactly as the product's
GUI workers do (``gui/generation_worker.py``, ``gui/connection_worker.py``).
A local server ignores the header, so the same code serves both and a gate
does not need to know which server it is talking to.

One caveat that cost a session today: Supabase **rotates** the refresh token
on every refresh and answers a reused one with ``Invalid Refresh Token:
Already Used``. Two processes holding one session (a sandboxed MotionBuilder
and the shell, say) therefore cannot both refresh — the second one to try is
logged out. Sign in once and hand the session over; do not refresh twice.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


def _headers(extra=None):
    """The product session's headers, plus *extra*. Empty when signed out —
    a local server neither needs nor reads them."""
    headers = dict(extra or {})
    try:
        from ..animatica_auth import get_auth
        headers.update(get_auth().auth_headers())
    except Exception:                                        # noqa: BLE001
        pass
    return headers


def _refreshed() -> bool:
    try:
        from ..animatica_auth import get_auth
        return bool(get_auth().refresh())
    except Exception:                                        # noqa: BLE001
        return False


def _call(url, data=None, timeout=60, content_type=None):
    extra = {"Content-Type": content_type} if content_type else None
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=data, headers=_headers(extra))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0 and _refreshed():
                continue
            raise


def capabilities(server, timeout=60):
    return _call(f"{server}/capabilities", timeout=timeout)


def health(server, timeout=15):
    """``GET /health``. The shape differs by server and a gate must not
    assume: a local MMCP server names its encoder mode, its models and their
    devices; the cloud API answers ``{"status": "ok"}`` and nothing else
    (measured 2026-09-05). Read it with ``.get``, print what is there."""
    return _call(f"{server}/health", timeout=timeout)


def generate(server, body, timeout=600):
    return _call(f"{server}/generate", data=json.dumps(body).encode(),
                 timeout=timeout, content_type="application/json")


def health_line(health_doc):
    """A one-line summary of whatever ``/health`` returned — the fields the
    local server has, silently skipped where the cloud has none."""
    bits = [f"status {health_doc.get('status', '?')}"]
    for key in ("retargeting", "text_encoder_mode", "stream"):
        value = health_doc.get(key)
        if value is not None:
            bits.append(f"{key} {value}")
    return " | ".join(bits)
