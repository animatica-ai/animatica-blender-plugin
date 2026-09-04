"""Authentication client for the Animatica Cloud API.

Uses ``urllib.request`` only — no third-party dependencies.

The cloud server (https://api.animatica.ai) requires a Supabase Bearer
token on every MMCP call.  This module handles the sign-in / refresh /
logout lifecycle and persists tokens to disk so the session survives
MotionBuilder restarts.

Token file: %APPDATA%\\animatica_core\\auth.json
Shape:      { access_token, refresh_token, email, tier }

Typical usage::

    from animatica_core import animatica_auth

    auth = animatica_auth.get_auth()
    auth.login("user@example.com", "s3cret")          # once
    caps = mmcp_client.get_capabilities(
        ANIMATICA_API_URL, access_token=auth.access_token
    )


"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.request
from typing import Any

from .settings import (
    shared_dir,
    _migrate_legacy_file,
)

# ---------------------------------------------------------------------------
# Token persistence path
# ---------------------------------------------------------------------------

def auth_path() -> str:
    """Tokens live in the SHARED folder, not the per-host one.

    Signing in is an account action: doing it once should cover MotionBuilder,
    3ds Max and Blender. A path function rather than a constant because
    ``animatica_core.host`` is registered at plugin startup, after import.
    """
    return os.path.join(shared_dir(), "auth.json")


def legacy_auth_paths() -> list:
    """Any host's pre-split token file.

    Tokens are account-level, so one written by MotionBuilder is a perfectly
    good token for 3ds Max -- unlike UI state, which uses the narrower
    ``host.legacy_dirs()``. This host's own folder is tried first.
    """
    from . import host
    return [os.path.join(d, "auth.json") for d in host.legacy_shared_dirs()]


_API_URL = "https://api.animatica.ai"
_TIMEOUT = (5, 15)   # (connect, read) — mimics requests tuple convention


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class AuthError(RuntimeError):
    """Raised on any authentication failure (network or server-side)."""


# ---------------------------------------------------------------------------
# Auth client
# ---------------------------------------------------------------------------

class AnimaticaAuth:
    """Manages Animatica Cloud sign-in, token refresh, and disk persistence."""

    def __init__(self) -> None:
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._email: str | None = None
        self._tier: str = "free"
        self._migrate_legacy_tokens()
        self._load_tokens()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    def is_authenticated(self) -> bool:
        """True when an access token is held in memory (not validated server-side)."""
        return self.access_token is not None

    def auth_headers(self) -> dict[str, str]:
        """Return Authorization header dict, or empty dict if not authenticated."""
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {}

    @property
    def user_email(self) -> str | None:
        return self._email

    @property
    def tier(self) -> str:
        return self._tier

    def health_check(self, timeout=8.0) -> dict[str, Any]:
        """Call ``GET /health`` and return the JSON response.

        Does not require authentication. Raises :class:`AuthError` on failure.
        """
        url = _API_URL + "/health"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise AuthError(
                f"Cannot reach Animatica Cloud: {exc.reason}"
            )
        except Exception as exc:
            raise AuthError(f"Health check failed: {exc}")

    # ------------------------------------------------------------------
    # Auth lifecycle
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> dict[str, Any]:
        """Authenticate with email + password.

        Returns the full JSON response on success.
        Raises :class:`AuthError` on failure.
        """
        data = self._request(
            "POST", "/auth/login",
            body={"email": email, "password": password, "client": "desktop"},
            skip_auth=True,
        )
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self._email = email
        self._tier = data.get("tier", "free")
        self._save_tokens()
        return data

    def logout(self) -> None:
        """Clear the in-memory session and delete the token file."""
        self.access_token = None
        self.refresh_token = None
        self._email = None
        self._tier = "free"
        try:
            os.unlink(auth_path())
        except OSError:
            pass

    def refresh(self) -> bool:
        """Attempt to refresh the access token using the stored refresh token.

        Returns True on success, False otherwise.
        """
        if not self.refresh_token:
            return False
        try:
            data = self._request(
                "POST", "/auth/refresh",
                body={"refresh_token": self.refresh_token},
                skip_auth=True,
            )
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            self._tier = data.get("tier", self._tier)
            self._save_tokens()
            return True
        except (AuthError, Exception):
            return False

    # ------------------------------------------------------------------
    # Internal networking (urllib.request only)
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        skip_auth: bool = False,
        _retried: bool = False,
    ) -> dict[str, Any]:
        """Send a JSON request to the Animatica API.

        Injects Bearer auth header automatically and retries once with a
        refreshed token on 401.
        """
        url = _API_URL + path
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if not skip_auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)

        # urllib doesn't support a (connect, read) tuple; use total timeout.
        total_timeout = _TIMEOUT[0] + _TIMEOUT[1]

        try:
            with urllib.request.urlopen(req, timeout=total_timeout) as resp:
                resp_body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not skip_auth and not _retried:
                if self.refresh():
                    return self._request(method, path, body=body, skip_auth=skip_auth, _retried=True)
                raise AuthError("Session expired. Please log in again.")
            # Try to extract error detail from JSON body.
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", exc.reason)
            except Exception:
                detail = exc.reason
            raise AuthError(detail or f"Request failed ({exc.code})")
        except urllib.error.URLError as exc:
            raise AuthError(
                f"Unable to reach Animatica Cloud. Check your internet connection. ({exc.reason})"
            )
        except Exception as exc:
            raise AuthError(f"Auth request failed: {exc}")

        try:
            return json.loads(resp_body)
        except Exception as exc:
            raise AuthError(f"Unexpected response from server: {exc}")

    # ------------------------------------------------------------------
    # Token persistence
    # ------------------------------------------------------------------

    def _save_tokens(self) -> None:
        os.makedirs(shared_dir(), exist_ok=True)
        payload = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "email": self._email,
            "tier": self._tier,
        }
        tmp = auth_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, auth_path())
            try:
                os.chmod(auth_path(), stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _migrate_legacy_tokens(self) -> None:
        """Carry a pre-rename ``auth.json`` forward once, then tighten perms.

        Runs before :meth:`_load_tokens` so an upgrading user stays signed in
        instead of being forced to re-authenticate. No-ops when the new token
        file already exists or no legacy file is present (see
        ``settings._migrate_legacy_file``).
        """
        for _legacy in legacy_auth_paths():
            _migrate_legacy_file(_legacy, auth_path(), shared_dir())
        try:
            os.chmod(auth_path(), stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _load_tokens(self) -> None:
        try:
            with open(auth_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            self._email = data.get("email")
            self._tier = data.get("tier", "free")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_auth: AnimaticaAuth | None = None


def get_auth() -> AnimaticaAuth:
    """Return the global :class:`AnimaticaAuth` instance (created on first call)."""
    global _auth
    if _auth is None:
        _auth = AnimaticaAuth()
    return _auth
