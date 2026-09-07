"""Backend availability, off the UI thread — the whole subsystem as a mixin.

"Is the server there?" is asked from four places (first show, the Test button, a
backend switch, and a debounced re-check while the user types a URL), it must
never block the UI, and late answers from superseded probes must not be applied
under a backend the user has since left. That is more machinery than it sounds
like, and none of it is host-specific: it is an HTTP call, a sequence guard, and
three dialogs.

A window mixes :class:`ConnectionProbeMixin` in, calls
:meth:`~ConnectionProbeMixin.init_connection_probe` from its ``__init__``, and
gets the Settings card's Test button, the status pill, model-list population and
the local-server recovery flow for free.

What the host window still owns, and what this mixin calls on it:

``self.state``                  the ``AppState`` being probed
``self.sec_settings`` / ``sec_generate`` / ``sec_skeleton``
                                the sections whose pills follow the verdict
``self._log(level, msg)``       the console
``self._apply_patch(patch)``    used by the "Switch to Cloud" recovery action
``self._refresh_model_choices(caps=None)``
                                repopulate the model dropdown from /capabilities

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

from .qt_compat import QtCore, QtWidgets, Slot


#: Debounce for a backend/URL change, in ms. The URL field emits per keystroke.
PROBE_DEBOUNCE_MS = 500


class ConnectionProbeMixin:
    """Async backend probing with a sequence guard and recovery dialogs."""

    def init_connection_probe(self) -> None:
        """Set up the probe's timer and bookkeeping. Call from ``__init__``.

        Probes legitimately overlap (first-show / Test / debounced backend|URL
        change), so every in-flight worker is held in a set and discarded only
        on its native ``QThread.finished`` — this keeps the C++ thread
        referenced until it truly ends (no "Destroyed while still running"
        crash). ``_probe_seq`` is a monotonic guard so a late result from a
        superseded probe is dropped rather than applied under a backend the
        user has since left.
        """
        self._probe_workers: set = set()
        self._probe_seq = 0
        self._recovery_dialog_open = False
        # True for first-show / backend-switch / Test (offer the recovery
        # dialog); False for URL-typing (debounced probe updates the pill only,
        # no modal spam). Consumed + reset by _on_probe_timer.
        self._pending_probe_recovery = False
        self._probe_timer = QtCore.QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(PROBE_DEBOUNCE_MS)
        self._probe_timer.timeout.connect(self._on_probe_timer)

    def arm_connection_probe(self, *, offer_recovery: bool = False) -> None:
        """Queue a debounced probe. ``offer_recovery`` arms the recovery dialog.

        The one entry point for "something about the connection target
        changed": the patch router calls it on a ``backend`` or ``server_url``
        write. Once armed for recovery, the flag survives until the timer
        fires, so a backend switch followed by keystrokes still offers the
        dialog.
        """
        if offer_recovery:
            self._pending_probe_recovery = True
        self._probe_timer.start()

    def _on_test_connection(self) -> None:
        """Probe the selected backend on demand (Settings → Test).

        Delegates to the shared async probe so Test, first-show, and the
        backend/URL-change trigger all run identical logic off the UI thread.
        """
        self._log("info", "Testing connection…")
        # Cancel any queued URL-change debounce first: otherwise a later probe
        # (higher _probe_seq) supersedes this one and the seq guard silently
        # drops our result dialog. Mirrors _show_local_unavailable_dialog's
        # "Switch to Cloud" branch.
        self._probe_timer.stop()
        self._start_connection_probe(offer_recovery=True, user_initiated=True)

    def _on_probe_timer(self) -> None:
        """Fire the debounced connection probe after a backend/URL change."""
        offer = self._pending_probe_recovery
        self._pending_probe_recovery = False
        self._start_connection_probe(offer_recovery=offer)

    def _start_connection_probe(
        self, *, offer_recovery: bool = True, user_initiated: bool = False
    ) -> None:
        """Probe the selected backend's availability off the UI thread.

        Shared by the Test button, the first-show auto-probe, and the debounced
        backend/URL-change trigger. Each start bumps ``_probe_seq``; the result
        handler ignores any worker whose ``seq`` no longer matches, so a slow
        probe finishing after the user switched backends can't apply stale state.
        Early returns (cloud-not-authed / empty URL) still bump the counter to
        invalidate any in-flight result.

        ``user_initiated`` is True only for the Test button (``_on_test_connection``)
        so ``_on_probe_result`` can pop a result dialog for a deliberate test while
        the background first-show / URL-change probes stay silent. It is a distinct
        flag from ``offer_recovery`` (which is also armed on URL-change debounces),
        so the two intents don't conflate.
        """
        from animatica_core import animatica_auth
        from animatica_core.gui.connection_worker import ConnectionProbeWorker

        self._probe_seq += 1
        seq = self._probe_seq

        s = self.state
        if s.backend == "cloud":
            auth = animatica_auth.get_auth()
            if not auth.is_authenticated():
                s.connected = False
                self.sec_settings.refresh()
                self.sec_generate.refresh()
                self._log("warn", "Log in to Animatica Cloud first.")
                return
            mode, url, token = "cloud", "", auth.access_token
        else:
            url = s.server_url.strip()
            if not url:
                s.connected = False
                self.sec_settings.refresh()
                self.sec_generate.refresh()
                self._log("warn", "Server URL is empty.")
                return
            mode, token = "local", None

        worker = ConnectionProbeWorker(url, mode=mode, access_token=token)
        worker.seq = seq
        worker.offer_recovery = offer_recovery
        worker.user_initiated = user_initiated
        worker.result.connect(
            lambda res, w=worker: self._on_probe_result(res, w),
            QtCore.Qt.QueuedConnection)
        worker.failed.connect(self._on_probe_failed, QtCore.Qt.QueuedConnection)
        # Hold every running thread until it truly ends (native QThread.finished)
        # so a superseded probe is never GC'd mid-run.
        self._probe_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._probe_workers.discard(w))
        worker.start()

    @Slot(object)
    def _on_probe_result(self, result, worker=None) -> None:
        """Apply a connection-probe verdict on the UI thread (success or failure).

        *worker* is bound at connect time. It used to be read from
        ``self.sender()``, which is not reliable for a queued connection
        from a QThread: when it came back None the guard below dropped the
        result outright, and the Model dropdown — populated only here —
        silently kept the single entry seeded from persisted state.
        """
        from animatica_core import mmcp_client

        if worker is None:
            worker = self.sender()
        if worker is None or getattr(worker, "seq", -1) != self._probe_seq:
            return  # superseded by a newer probe
        s = self.state
        is_cloud = s.backend == "cloud"
        # Only a deliberate Test-button probe pops a result dialog; background
        # first-show / URL-change probes stay silent. This is a separate flag
        # from offer_recovery, which is also armed on URL-change debounces —
        # reusing that one would spam dialogs on background probes.
        user_test = getattr(worker, "user_initiated", False)
        s.connected = result.ok
        if result.ok:
            caps = result.capabilities or {}
            models = caps.get("models") or caps.get("model_names") or []
            # Probe the active model's supports_retargeting flag so the
            # "Send my hierarchy" checkbox enables/greys in real time.
            supports_retargeting = False
            if caps.get("models"):
                # Retargeting is a per-model capability, so read it off the
                # model the user actually selected, not the first one listed.
                probe_model = mmcp_client.pick_model(caps, self.state.model)
                supports_retargeting = bool(
                    (probe_model or {}).get("supports_retargeting", False)
                )
            self.sec_skeleton.set_retargeting_capability(supports_retargeting)
            # Populate the Model dropdown from the server's advertised models.
            # Each entry is either a {"id": ...} dict (/capabilities models[]) or
            # a bare string (model_names fallback).
            self._refresh_model_choices(caps)
            retarget_suffix = " — retargeting: yes" if supports_retargeting else ""
            self._log(
                "ok",
                f"Server online ({len(models)} models){retarget_suffix}"
                if models else "Server online.",
            )
            if user_test:
                QtWidgets.QMessageBox.information(
                    self,
                    "Connection OK",
                    f"Server online — {len(models)} model(s) available."
                    if models else "Server online.",
                )
        else:
            if is_cloud and result.status == "auth_required":
                # Server rejected the bearer token — clear the logged-in flag so
                # the Settings UI prompts for re-login instead of a stale pill.
                try:
                    from animatica_core import animatica_auth
                    animatica_auth.get_auth().logout()
                except Exception:
                    pass
                s.auth_logged_in = False
                s.auth_tier = ""
                self._log("error", "Session expired — log in to Animatica Cloud again.")
            else:
                self._log("error", f"Server unreachable: {result.message}")
            # Local server unreachable → offer recovery actions, but only when
            # this probe was armed for it (not while typing a URL).
            recovery_shown = False
            if (not is_cloud and getattr(worker, "offer_recovery", False)
                    and result.status in {"unreachable", "bad_response", "http_error"}):
                self._show_local_unavailable_dialog(result.message)
                recovery_shown = True
            # For a user-triggered Test, surface the failure — but skip when the
            # richer local-unavailable recovery dialog already popped, so we never
            # stack two dialogs. Cloud auth_required has no recovery dialog, so it
            # falls through to this warning with its own message.
            if user_test and not recovery_shown:
                if is_cloud and result.status == "auth_required":
                    fail_msg = "Session expired — log in to Animatica Cloud again."
                else:
                    fail_msg = result.message or "Server unreachable."
                QtWidgets.QMessageBox.warning(self, "Connection failed", fail_msg)
        self.sec_settings.refresh()
        self.sec_generate.refresh()

    @Slot(str)
    def _on_probe_failed(self, message: str) -> None:
        worker = self.sender()
        if worker is None or getattr(worker, "seq", -1) != self._probe_seq:
            return
        self.state.connected = False
        self.sec_settings.refresh()
        self.sec_generate.refresh()
        self._log("error", f"Connection probe failed: {message}")

    def _show_local_unavailable_dialog(self, message: str) -> None:
        """Offer recovery actions when the selected local server is unreachable.

        "How to start the server" only shows guidance — Animatica never launches
        a process (generation is server-only). Re-entrancy-guarded so a result
        arriving during the nested exec_ loop can't stack dialogs.
        """
        if self._recovery_dialog_open:
            return
        self._recovery_dialog_open = True
        try:
            box = QtWidgets.QMessageBox(self)
            box.setIcon(QtWidgets.QMessageBox.Warning)
            box.setWindowTitle("Local server unavailable")
            box.setText(
                f"Could not reach the local MMCP server at "
                f"{self.state.server_url}.\n\n{message}"
            )
            btn_cloud = box.addButton("Switch to Cloud", QtWidgets.QMessageBox.AcceptRole)
            btn_howto = box.addButton(
                "How to start the server", QtWidgets.QMessageBox.ActionRole)
            btn_dismiss = box.addButton("Dismiss", QtWidgets.QMessageBox.RejectRole)
            box.setDefaultButton(btn_dismiss)
            box.exec_() if hasattr(box, "exec_") else box.exec()
            clicked = box.clickedButton()
        finally:
            self._recovery_dialog_open = False
        if clicked is btn_cloud:
            self._apply_patch({"backend": "cloud"})
            # _apply_patch already arms a debounced re-probe; fire one now too so
            # the switch verifies immediately. The debounced one supersedes it.
            self._probe_timer.stop()
            self._start_connection_probe(offer_recovery=False)
        elif clicked is btn_howto:
            self._show_server_howto()

    def _show_server_howto(self) -> None:
        """Guidance-only dialog: how to bring up a local MMCP server."""
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Information)
        box.setWindowTitle("Start a local MMCP server")
        box.setText(
            "Animatica generates motion through an MMCP server; it never launches "
            "one for you (generation is server-only).\n\n"
            "To use a local server:\n"
            "  1. Start your Kimodo / MMCP server.\n"
            f"  2. Confirm it listens at {self.state.server_url}.\n"
            "  3. Click Test in Settings to re-check.\n\n"
            "Docs: https://www.animatica.ai/mmcp/docs/get-started/introduction"
        )
        box.exec_() if hasattr(box, "exec_") else box.exec()
