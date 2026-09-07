"""The busy state and elapsed clock a generation run shows, in two places at once.

A run can be launched from the Generate card or from the floating timeline, and
either way both have to show the same phase text and the same running seconds.
The rule that makes that work is one anchor per run: an intra-run re-arm — the
pose two-frame fallback relaunch, or the next group of a gap fan-out — must not
reset the clock, or a three-minute run reports as thirty seconds.

A window mixes :class:`GenerationStatusMixin` in and calls
:meth:`~GenerationStatusMixin.init_generation_status` from its ``__init__``.

What the host window still owns:

``sec_generate``            the card carrying the busy state and progress row
``_timeline_container``     the floating timeline's status row (may not exist yet)
``_log(level, msg)``        the console
``_worker``                 the generation QThread, or None (the watchdog asks
                            it whether the silence means never-started or died)

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

import time

from .qt_compat import QtCore, Slot


class GenerationStatusMixin:
    """Busy gating, progress text, and the one-anchor-per-run elapsed clock."""

    def init_generation_status(self) -> None:
        """Create the elapsed-clock timer. Call from ``__init__``.

        ``_gen_t0`` doubles as the already-running guard, which is what makes
        one anchor span a whole multi-request run.
        """
        self._gen_t0: float | None = None
        # Watchdog state. The worker announces itself on its first progress
        # line, so ten seconds of silence after ARMING means the thread never
        # got there. One report per worker.
        self._gen_saw_progress = False
        self._gen_stall_reported = False
        self._gen_armed_at: float | None = None
        self._gen_elapsed_timer = QtCore.QTimer(self)
        self._gen_elapsed_timer.setInterval(1000)
        self._gen_elapsed_timer.timeout.connect(self._on_gen_elapsed_tick)

    def _set_generate_busy(self, busy: bool) -> None:
        # The generate button lives inside sec_generate; set_busy disables the
        # interactive section body (so prompt edits during generation can't
        # corrupt the in-flight request) and drives the spinner/status row —
        # which sits OUTSIDE the disabled subtree so it keeps animating.
        self.sec_generate.set_busy(busy)
        # Mirror onto the floating timeline so a run launched from there shows
        # the same phase status. Guarded — the container always exists once the
        # panel is built, but stay defensive on any early call.
        tc = getattr(self, "_timeline_container", None)
        if tc is not None:
            tc.set_status("", busy)
        # Elapsed clock: one anchor per run. busy(True) while already ticking
        # is a no-op (intra-run re-arms — pose fallback relaunch, gap groups —
        # must not reset the clock); busy(False) runs in the finally of both
        # success and failure paths, so the total is logged for either outcome.
        if busy:
            if self._gen_t0 is None:
                self._gen_t0 = time.monotonic()
                self._arm_stall_watchdog()
                self._gen_elapsed_timer.start()
            self._on_gen_elapsed_tick()   # populate immediately (labels were just cleared)
        else:
            self._gen_elapsed_timer.stop()
            if self._gen_t0 is not None:
                self._log("info", f"Generation took {time.monotonic() - self._gen_t0:.1f}s")
                self._gen_t0 = None

    def _arm_stall_watchdog(self) -> None:
        """Reset the watchdog for a fresh worker. Call beside every ``start()``.

        Per WORKER, not per clock anchor -- the distinction is load-bearing
        and the first version of this mixin got it wrong. A gap fan-out
        launches a new thread per group inside ONE elapsed-clock anchor; if
        the flags reset only with the anchor, group two's dead thread is
        invisible, because group one's progress already set
        ``_gen_saw_progress``. (Measured in the 3ds Max window, which arms at
        all three of its ``worker.start()`` sites.) The anchor path above
        still arms, so a single-request host is covered even before its start
        sites call this.

        The deadline runs from HERE, not from the clock anchor. Group 2 of a
        fan-out arms minutes after the anchor, so a deadline measured against
        ``_gen_t0`` has already expired the moment the flags reset — the
        watchdog would cry stall on the very next tick of a perfectly healthy
        worker. Same class of bug as the per-anchor flags, one line lower.
        """
        self._gen_saw_progress = False
        self._gen_stall_reported = False
        self._gen_armed_at = time.monotonic()

    def _on_gen_elapsed_tick(self) -> None:
        """Push the running "Elapsed: Ns" readout into both progress rows.

        Also the stall watchdog. The worker announces itself on its first
        line, so ten seconds of silence means the thread never got there --
        and ``isRunning``/``isFinished`` is exactly what separates "never
        started" from "started and died". Reported once per run.

        Born in the 3ds Max plugin while debugging a generation that sat on
        "Preparing request" for 145 seconds with no clue anywhere; lived only
        there until the audit's AST sweep caught the mixin lagging its own
        consumer. In a DCC the traceback of a dead thread lands in a host
        console nobody watches, which is why silence needs a deadline.
        """
        if self._gen_t0 is None:
            return
        armed_at = self._gen_armed_at
        if armed_at is None:                     # never armed: nothing to time
            armed_at = self._gen_t0
        if (not self._gen_saw_progress and not self._gen_stall_reported
                and time.monotonic() - armed_at > 10.0):
            self._gen_stall_reported = True
            w = getattr(self, "_worker", None)
            self._log("error",
                      f"The generation thread has said nothing for 10s "
                      f"(running={w.isRunning() if w is not None else '-'}, "
                      f"finished={w.isFinished() if w is not None else '-'}). "
                      f"It never reached its first line, so the request was "
                      f"never sent -- check the host console for a traceback.")
        text = f"Elapsed: {int(time.monotonic() - self._gen_t0)}s"
        self.sec_generate.set_elapsed(text)
        tc = getattr(self, "_timeline_container", None)
        if tc is not None:
            tc.set_elapsed(text)

    @Slot(str)
    def _on_generate_progress(self, msg: str) -> None:
        self._gen_saw_progress = True
        self._log("info", msg)
        self.sec_generate.set_progress(msg)
        tc = getattr(self, "_timeline_container", None)
        if tc is not None:
            tc.set_status(msg, True)
