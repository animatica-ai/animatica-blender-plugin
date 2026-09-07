"""Animatica Cloud sign-in for a tool window — worker, slots, and the mixin.

Logging in is one blocking ``urllib`` call, so it runs on a thread and comes
back through two queued signals. Nothing about that is host-specific, and the
state it writes (``auth_logged_in`` / ``auth_email`` / ``auth_tier``) is the
same ``AppState`` in every host.

A window mixes :class:`AuthMixin` in, calls
:meth:`~AuthMixin.init_auth` from its ``__init__``, and wires the Settings
card's ``login_requested`` / ``logout_requested`` signals straight at
:meth:`~AuthMixin._on_login` / :meth:`~AuthMixin._on_logout`.

What the host window still owns, and what this mixin calls on it:

``self.state``                  the ``AppState`` whose auth fields are written
``self.sec_settings`` / ``sec_generate``   the cards whose pills follow sign-in
``self._log(level, msg)``       the console
``self._save_timer``            the debounced settings save
``self._retain_worker(worker)`` from :class:`~animatica_core.gui.worker_lifetime.WorkerRetentionMixin`

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

from .qt_compat import QtCore, QtWidgets, Signal, Slot


class LoginWorker(QtCore.QThread):
    """Async Animatica login so urllib.request doesn't block the UI."""

    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, email: str, password: str, parent=None):
        super().__init__(parent)
        self._email = email
        self._password = password

    def run(self):
        from animatica_core import animatica_auth
        try:
            auth = animatica_auth.get_auth()
            data = auth.login(self._email, self._password)
            self.succeeded.emit(data)
        except animatica_auth.AuthError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(f"Login failed: {exc}")


class AuthMixin:
    """Sign-in / sign-out slots over :class:`LoginWorker`."""

    def init_auth(self) -> None:
        """Reset the in-flight login handle. Call from ``__init__``."""
        # Identity/busy bookkeeping only — the worker's LIFETIME is the
        # retention set's job (see worker_lifetime).
        self._login_worker: LoginWorker | None = None

    def _on_login(self, email: str, password: str) -> None:
        if self._login_worker is not None and self._login_worker.isRunning():
            self._log("warn", "Login already in progress.")
            return
        self._log("info", f"Logging in as {email}…")
        worker = LoginWorker(email, password)
        worker.succeeded.connect(self._on_login_succeeded, QtCore.Qt.QueuedConnection)
        worker.failed.connect(self._on_login_failed, QtCore.Qt.QueuedConnection)
        self._retain_worker(worker)
        self._login_worker = worker
        worker.start()

    @Slot(dict)
    def _on_login_succeeded(self, data: dict) -> None:
        from animatica_core import animatica_auth
        auth = animatica_auth.get_auth()
        self.state.auth_logged_in = True
        self.state.auth_email = auth.user_email or self.state.auth_email
        self.state.auth_tier = auth.tier or data.get("tier", "")
        self.sec_settings.refresh()
        self._log("ok", f"Signed in ({self.state.auth_tier or 'ok'}).")
        self._login_worker = None
        self._save_timer.start()
        who = self.state.auth_email or "Animatica Cloud"
        tier = f" ({self.state.auth_tier})" if self.state.auth_tier else ""
        QtWidgets.QMessageBox.information(self, "Signed in", f"Signed in as {who}{tier}.")

    @Slot(str)
    def _on_login_failed(self, message: str) -> None:
        self._log("error", message)
        self._login_worker = None
        QtWidgets.QMessageBox.warning(self, "Sign-in failed", message)

    def _on_logout(self) -> None:
        from animatica_core import animatica_auth
        animatica_auth.get_auth().logout()
        self.state.auth_logged_in = False
        self.state.auth_tier = ""
        self.state.connected = False
        self.sec_settings.refresh()
        self.sec_generate.refresh()
        self._log("ok", "Signed out.")
