"""08 · Live Drive (ARDY) -- real-time streamed motion with record-to-take.

Self-contained: owns the StreamClient (network threads), the LiveSession
buffer and the 33 ms preview tick. Thread discipline (plan R3): network
threads only fill ``client.packets``; every pyfbsdk call happens in
``_on_tick`` / ``_on_record_stop`` on the UI thread.

Availability follows plan K4: the Start button probes ``/stream/ardy/info``
in a worker; a server without the stream extension keeps the section in
the "unavailable" state (the section itself stays visible so the feature
is discoverable).

Driving: while a session is live, W/S/A/D are hold-to-move (opposites
cancel, diagonals combine). MoBu captures the keyboard natively before
its embedded Qt sees it, so Qt key events never fire — instead the
preview tick polls the OS key state (``live.keyboard``, GetAsyncKeyState)
while MoBu is the foreground window. The direction buttons are the mouse
fallback and win while no movement key is held.

The session is anchored to MotionBuilder's playhead: it takes its motion
prompt from the timeline block under it. **Play** walks the playhead
forward (so the prompt blocks play out) without keying; **Record** does
the same and bakes a take. Plain preview leaves the playhead alone — the
timeline must not scroll away while the operator rehearses a move.

Scope: local backend only, canonical Core27 skeleton at the origin,
direction/speed/facing control plus the timeline's prompts.
"""

from __future__ import annotations

import datetime
import queue

from ...live.controls import (DIRECTION_VECTORS, combine_directions,
                              prompt_at_frame)
from ...live.keyboard import pressed_directions
from ..qt_compat import QtCore, QtWidgets, Signal

_ARROW_QT_KEYS = frozenset({
    QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
    QtCore.Qt.Key_Left, QtCore.Qt.Key_Right,
})

_EDIT_WIDGETS = (
    "QLineEdit", "QAbstractSpinBox", "QSpinBox", "QDoubleSpinBox",
    "QTextEdit", "QPlainTextEdit",
)


class _ArrowEater(QtCore.QObject):
    """Application-level Qt filter consuming arrow keys while driving.

    Held arrows auto-repeat ~30 events/s; if they reach MoBu they step
    the playhead and re-evaluate the scene, collapsing the preview
    framerate. This filter eats them inside Qt only — it cannot affect
    the OS or other applications (the WH_KEYBOARD_LL alternative was
    reverted for freezing the system keyboard; see live/keyboard.py).
    Movement input is unaffected: the drive poller reads the OS key
    state, not Qt events. Arrows still work in editable widgets.
    """

    def __init__(self):
        super().__init__()
        self.active = False

    def eventFilter(self, _obj, event):
        if not self.active:
            return False
        t = event.type()
        if t not in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease,
                     QtCore.QEvent.ShortcutOverride):
            return False
        try:
            if event.key() not in _ARROW_QT_KEYS:
                return False
        except Exception:
            return False
        # Editable-widget guard: check the event's TARGET first (a spin
        # box delivers keys to its inner QLineEdit), then the app focus
        # widget as fallback — focusWidget() alone is unreliable.
        for w in (_obj, QtWidgets.QApplication.focusWidget()):
            if isinstance(w, QtWidgets.QWidget) and any(
                    w.inherits(cls) for cls in _EDIT_WIDGETS):
                return False
        event.accept()
        return True
from ..widgets import Btn, Check, CollapsibleSection, Field, NumberInput, Pill

# Preview cadence. 16 ms (~60 Hz) matches MotionBuilder's own refresh, so
# interpolated poses land one per drawn frame; the tick costs ~1.5 ms.
_TICK_MS = 16
_STATUS_EVERY = 10     # refresh the status line every N ticks

_DIRS = [
    ("forward", "↑", DIRECTION_VECTORS["forward"]),
    ("left",    "←", DIRECTION_VECTORS["left"]),
    ("back",    "↓", DIRECTION_VECTORS["back"]),
    ("right",   "→", DIRECTION_VECTORS["right"]),
]


class _StartWorker(QtCore.QThread):
    """Probe /info + POST /start off the UI thread (lazy model load takes
    seconds server-side)."""

    ok = Signal(object, object)     # client, handshake
    failed = Signal(str)
    busy = Signal(str)              # someone else holds the session

    def __init__(self, server_url, takeover=False, prompts=None, model=None,
                 parent=None):
        super().__init__(parent)
        self._url = server_url
        self._takeover = takeover
        self._prompts = list(prompts or ())
        self._model = model

    def run(self):
        from ...live.stream_client import StreamClient, StreamClientError
        client = StreamClient(self._url)
        info = client.info()
        if info is None:
            self.failed.emit(
                f"No live server at {self._url} — start the MMCP server "
                "(or fix the URL in Settings).")
            return
        # Realtime is a property of the MODEL, not of the server: the
        # stream is built on ARDY's autoregressive step, and a model that
        # denoises a whole clip at once has nothing to stream. The server
        # publishes which ids it can drive, so this is a check rather than
        # a guess at the name. Older servers omit the list — then trust
        # the operator and let /start answer.
        streamable = info.get("model_ids")
        if streamable and self._model and self._model not in streamable:
            self.failed.emit(
                f"{self._model} generates offline — use Generate. Realtime "
                f"driving needs one of: {', '.join(streamable)}.")
            return
        if info.get("active") and not self._takeover:
            self.busy.emit(
                "Another client holds the live session — click Start Live "
                "again to take it over.")
            return
        try:
            hs = client.start(takeover=self._takeover, prompts=self._prompts)
            client.open_frames()
        except StreamClientError as exc:
            self.failed.emit(str(exc))
            return
        self.ok.emit(client, hs)


class _StopWorker(QtCore.QThread):
    """client.stop() posts /stop over the network -- keep it off the UI.

    Deliberately PARENTLESS. Parenting it to the section crashed
    MotionBuilder twice: the section is destroyed (tool reload, window
    close) while this thread is still posting /stop, and Qt aborts when a
    running QThread's parent goes away. Ownership is the section's
    ``_workers`` set instead, and :meth:`LiveSection.shutdown` waits for
    it before returning.
    """

    def __init__(self, client):
        super().__init__(None)
        self._client = client

    def run(self):
        try:
            self._client.stop()
        except Exception:
            pass


class LiveSection(QtWidgets.QWidget):
    def __init__(self, state, on_patch, log=None, parent=None):
        super().__init__(parent)
        self._state = state
        self._log = log or (lambda level, msg: None)

        # runtime
        self._client = None
        self._session = None
        self._joint_map = None
        self._joint_names = None
        self._last_frame = None
        self._move_dir = None          # None = idle (no direction held)
        self._frozen_facing = None
        self._kb_active = False        # keyboard drove on the previous tick
        self._held_keys = set()        # last polled keys (status display)
        self._offer_takeover = False   # next Start Live replaces the holder
        self._viz = None               # LiveViz, built with the session
        self._goal_model = None        # scene marker the autopilot chases
        self._auto_arrived = False     # autopilot hysteresis state
        self._workers = set()          # GC-safety: keep QThreads referenced
        self._tick_count = 0
        # Timeline coupling (all three are scene-frame based)
        self._prompt_source = None     # callable -> [(text, start, end)]
        self._retarget_target = None   # callable -> characterized FBCharacter
        self._live_char = None         # live rig's own FBCharacter when linked
        self._prompt_blocks = []       # snapshot taken at session start
        self._prompt_text = ""         # block under the playhead right now
        self._playhead_base = None     # frame Record started at (None = not driving)
        self._playhead_at = None       # last frame we sought to
        self._scene_fps = 30.0
        self._t_origin = None          # stream t of the session's first frame
        self._record_start_frame = 0   # playhead when Record was pressed

        self._tick = QtCore.QTimer(self)
        self._tick.setInterval(_TICK_MS)
        self._tick.timeout.connect(self._on_tick)

        self._arrow_eater = _ArrowEater()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self._arrow_eater)

        # -- UI ----------------------------------------------------------
        self._pill = Pill("offline", tone="info")
        self._section = CollapsibleSection(
            "Live Drive (ARDY)", step=8, icon_ex="run_figure", right=self._pill,
        )
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)
        body = self._section.body_layout

        self._start_btn = Btn("Start Live", icon="wand", variant="solid")
        self._start_btn.clicked.connect(self._on_start_stop)
        body.addWidget(self._start_btn)

        # Keep the hint SHORT: Field's hint label does not word-wrap, so a
        # long string here sets the minimum width of the WHOLE tool panel.
        body.addWidget(Field(
            "Drive", None,
            hint="Hold the arrow keys to move (W/S/A/D also work)."))

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(4)
        self._dir_btns = {}
        for name, label, vec in _DIRS:
            b = Btn(label, variant="surface", size="sm")
            b.setCheckable(True)
            b.clicked.connect(
                lambda _=False, n=name: self._on_dir_clicked(n))
            self._dir_btns[name] = (b, vec)
        grid.addWidget(self._dir_btns["forward"][0], 0, 1)
        grid.addWidget(self._dir_btns["left"][0], 1, 0)
        grid.addWidget(self._dir_btns["back"][0], 1, 1)
        grid.addWidget(self._dir_btns["right"][0], 1, 2)
        body.addLayout(grid)

        speed_row = QtWidgets.QHBoxLayout()
        speed_row.addWidget(QtWidgets.QLabel("Speed"))
        self._speed = NumberInput(value=1.4, minimum=0.0, maximum=3.0, step=0.1)
        self._speed.valueChanged.connect(lambda _v: self._push_control())
        speed_row.addWidget(self._speed, 1)
        body.addLayout(speed_row)

        self._face_check = Check(
            "Face movement direction", checked=True,
            sublabel="Off freezes the current facing (strafe).")
        self._face_check.toggled.connect(self._on_face_toggled)
        body.addWidget(self._face_check)

        self._viz_check = Check(
            "Show path & trail", checked=True,
            sublabel="Commanded path, target and where you have been.")
        self._viz_check.toggled.connect(self._on_viz_toggled)
        body.addWidget(self._viz_check)

        self._goal_btn = Btn("Go to goal", icon="wand", variant="surface")
        self._goal_btn.setCheckable(True)
        self._goal_btn.setEnabled(False)
        self._goal_btn.clicked.connect(self._on_goal_toggled)
        body.addWidget(self._goal_btn)
        # Keep hints SHORT — Field's hint label does not word-wrap, so a
        # long one sets the minimum width of the whole tool panel.
        body.addWidget(Field(
            "", None, hint="Drag the Goal marker; any key cancels."))

        # Play runs the timeline without keying: same playhead motion the
        # recording produces, so prompt blocks come and go as they will
        # during the take — a rehearsal of the timeline, not of the move.
        transport = QtWidgets.QHBoxLayout()
        transport.setSpacing(4)
        self._play_btn = Btn("Play", icon_ex="play_tri", variant="surface")
        self._play_btn.setCheckable(True)
        self._play_btn.setEnabled(False)
        self._play_btn.clicked.connect(self._on_play_toggled)
        transport.addWidget(self._play_btn)
        self._record_btn = Btn("Record", icon_ex="record_dot", variant="surface")
        self._record_btn.setEnabled(False)
        self._record_btn.clicked.connect(self._on_record)
        transport.addWidget(self._record_btn)
        body.addLayout(transport)

        self._status = QtWidgets.QLabel("")
        self._status.setObjectName("field_hint")
        self._status.setWordWrap(True)
        body.addWidget(self._status)

    # ------------------------------------------------------------------
    # session lifecycle
    # ------------------------------------------------------------------

    @property
    def live(self):
        return self._client is not None

    def set_retarget_target(self, fn):
        """Register the callable that yields the scene rig's FBCharacter.

        ``fn() -> FBCharacter | None`` — a characterised character the
        live rig should drive through HIK (M4). Wired by the tool window,
        which owns the notion of "the user's rig"; the live section only
        knows how to bind to whatever it is handed.
        """
        self._retarget_target = fn

    def set_prompt_source(self, fn):
        """Register the callable that yields the timeline's prompt blocks.

        ``fn() -> [(text, start_frame, end_frame)]`` in SCENE frames. The
        tool window wires this after it builds the timeline (which is
        created after this section, so it cannot be a constructor arg).
        """
        self._prompt_source = fn

    def _read_prompt_blocks(self):
        if self._prompt_source is None:
            return []
        try:
            return [(str(t), int(s), int(e))
                    for t, s, e in self._prompt_source() if t]
        except Exception as exc:
            self._log("warn", f"Live Drive: prompt blocks unreadable: {exc}")
            return []

    def _on_start_stop(self):
        if self.live:
            self._stop_session("stopped")
            return
        # Live always talks to the local stream server (state.server_url).
        # The local/cloud switch belongs to batch generation only — tying
        # Live to it just produced a silent refusal that read as "the
        # controls do nothing".
        self._start_btn.setEnabled(False)
        self._pill.set_text("connecting")
        # Declared up front so the server encodes every block's text while
        # it is loading the model, not mid-session when the playhead
        # crosses into one.
        self._prompt_blocks = self._read_prompt_blocks()
        worker = _StartWorker(self._state.server_url,
                              takeover=self._offer_takeover,
                              prompts=[t for t, _s, _e in self._prompt_blocks],
                              model=getattr(self._state, "model", None))
        self._offer_takeover = False       # consumed by this attempt
        worker.ok.connect(self._on_started)
        worker.failed.connect(self._on_start_failed)
        worker.busy.connect(self._on_start_busy)
        self._retain(worker)
        worker.start()

    def _on_start_busy(self, message):
        """Session held elsewhere: arm takeover for the next click."""
        self._offer_takeover = True
        self._start_btn.setEnabled(True)
        self._pill.set_text("busy")
        self._set_status(message)
        self._log("warn", f"Live Drive: {message}")

    def _retain(self, worker):
        """Keep the QThread referenced until its native finished fires."""
        self._workers.add(worker)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))

    def _on_start_failed(self, message):
        self._start_btn.setEnabled(True)
        self._pill.set_text("offline")
        self._set_status(message)
        self._log("warn", f"Live Drive: {message}")

    def _on_started(self, client, handshake):
        from ...bridge import live_apply as mobu_apply
        from ...live.session import LiveSession

        try:
            self._joint_map, self._joint_names = mobu_apply.ensure_skeleton(
                handshake["skeleton"])
        except Exception as exc:
            self._start_btn.setEnabled(True)
            self._pill.set_text("offline")
            self._set_status(f"Skeleton build failed: {exc}")
            self._log("error", f"Live Drive skeleton: {exc}")
            stopper = _StopWorker(client)
            self._retain(stopper)      # unreferenced QThreads get collected
            stopper.start()
            return

        # Shared-skeleton preview (M4): the stream keeps writing into the
        # live rig, and the user's characterised rig follows through HIK,
        # solved by the Scene.Evaluate the preview already runs per tick.
        # Route "none" opts out; "server" degrades to HIK here — a
        # per-window server round trip has no place in a realtime loop.
        self._live_char = None
        route = (getattr(self._state, "retarget_route", None) or "auto").lower()
        if route != "none" and self._retarget_target is not None:
            try:
                target_char = self._retarget_target()
            except Exception:
                target_char = None
            if target_char is not None:
                self._live_char = mobu_apply.link_user_rig(
                    self._joint_map, target_char)
                if self._live_char is not None:
                    self._log("info",
                              f"Live Drive: '{target_char.Name}' follows the "
                              "live rig (HIK retarget).")
                else:
                    self._log("warn",
                              "Live Drive: could not link the scene rig — "
                              "preview runs on the live rig only.")
            else:
                # Say it, quietly: a silent no-link earlier read as "the
                # feature does nothing" during verification.
                self._log("info",
                          "Live Drive: no characterised scene rig targeted "
                          "— preview on the live rig only (Skeleton → "
                          "Create/Load + Create HIK to retarget).")

        self._client = client
        # Half a window of buffer (0.2 s at 20 fps): every 0.1 s of buffer
        # is 0.1 s of extra input latency, and the measured stream jitter
        # is small enough to ride on half a window — an occasional underrun
        # just clamps to the newest frame instead of stuttering.
        self._session = LiveSession(
            handshake["fps"], handshake["horizon_frames"], buffer_windows=0.5)
        self._last_frame = None
        self._tick_count = 0

        # The playhead is READ while driving (it decides which prompt
        # block is in force) but only DRIVEN while recording — see
        # _sync_playhead. Rehearsing a move must not scroll the timeline.
        from ...bridge import time_bridge
        self._playhead_base = None
        self._t_origin = None
        try:
            self._playhead_at = time_bridge.current_frame()
            self._scene_fps = time_bridge.current_fps() or 30.0
        except Exception as exc:
            self._log("warn", f"Live Drive: playhead unavailable: {exc}")
            self._playhead_at = None
        self._prompt_text = ""

        self._start_btn.setText("Stop Live")
        self._start_btn.setEnabled(True)
        self._play_btn.setChecked(False)
        self._play_btn.setEnabled(True)
        self._refresh_play_btn()
        self._record_btn.setEnabled(True)
        self._goal_btn.setEnabled(True)
        self._refresh_goal_btn()
        self._pill.set_text("live")
        self._kb_active = False
        self._arrow_eater.active = True

        # Fresh viz per session: a previous run's path/trail would
        # otherwise hang around next to the new one.
        from ...bridge import live_apply as mobu_apply
        from ...bridge import live_viz
        if self._viz is not None:
            self._viz.clear()
        self._viz = live_viz.LiveViz()
        self._viz.set_visible(self._viz_check.isChecked())

        # The goal marker exists from the start of the session, not only
        # once the autopilot is switched on — otherwise the operator is
        # told to "drag the Goal marker" while nothing is in the scene.
        try:
            self._goal_model = mobu_apply.ensure_goal_marker()
        except Exception as exc:
            self._log("warn", f"Live Drive: goal marker unavailable: {exc}")
            self._goal_model = None

        self._set_status("Session up — hold the arrow keys to move.")
        blocks = len(self._prompt_blocks)
        self._log("info",
                  f"Live Drive: session started ({handshake['fps']:g} fps, "
                  f"window {handshake['horizon_frames']} frames) at frame "
                  f"{self._playhead_at}, {blocks} prompt block(s) on the "
                  "timeline.")
        self._push_control()
        self._tick.start()

    def _stop_session(self, reason):
        self._kb_active = False
        self._arrow_eater.active = False   # arrows return to MoBu at once
        client = self._client
        self._tick.stop()
        self._client = None

        if self._session is not None and self._session.recording:
            self._bake(self._session.stop_record())
        self._session = None
        self._last_frame = None
        self._playhead_base = None     # stop driving the playhead

        if client is not None:
            w = _StopWorker(client)
            self._retain(w)
            w.start()

        self._start_btn.setText("Start Live")
        self._start_btn.setEnabled(True)
        self._record_btn.setText("Record")
        self._record_btn.setEnabled(False)
        self._play_btn.setChecked(False)
        self._play_btn.setEnabled(False)
        self._refresh_play_btn()
        self._goal_btn.setChecked(False)
        self._goal_btn.setEnabled(False)
        self._refresh_goal_btn()
        self._auto_arrived = False
        self._pill.set_text("offline")
        self._set_status(f"Session {reason}.")
        self._log("info", f"Live Drive: session {reason}.")

    def shutdown(self):
        """Tool-window teardown: stop the tick, the session and the threads.

        MUST leave no thread running: the caller (window close, dev
        reload) deletes this widget right after, and a live QThread whose
        objects are being torn down takes MotionBuilder with it.
        """
        if self.live:
            self._stop_session("closed")
        for worker in list(self._workers):
            try:
                if worker.isRunning():
                    worker.wait(5000)
            except Exception:
                pass
        self._workers.clear()
        self._arrow_eater.active = False
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._arrow_eater)
        if self._viz is not None:
            try:
                self._viz.clear()          # no scene litter after teardown
            except Exception:
                pass
            self._viz = None

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------

    def _on_dir_clicked(self, name):
        btn, vec = self._dir_btns[name]
        if self._autopilot:
            self._cancel_autopilot("manual input")
        if btn.isChecked():
            for other, (b, _v) in self._dir_btns.items():
                if other != name:
                    b.setChecked(False)
            self._move_dir = vec
        else:
            self._move_dir = None      # toggled off -> halt
        self._push_control()

    def _poll_keyboard(self):
        """Poll arrows/WASD each tick (see live.keyboard). Keyboard wins
        while any movement key is held; releasing all keys hands control
        back to the buttons (and stops if the keys were driving)."""
        pressed = pressed_directions()
        self._held_keys = pressed
        if pressed and self._autopilot:
            self._cancel_autopilot("manual input")
        if pressed:
            new_dir = combine_directions(pressed)
            for name, (btn, _vec) in self._dir_btns.items():
                btn.setChecked(name in pressed)
            self._kb_active = True
            if new_dir != self._move_dir:
                self._move_dir = new_dir
                self._push_control()
        elif self._kb_active:
            # keys just released -> halt and clear the button highlights
            self._kb_active = False
            for _n, (btn, _v) in self._dir_btns.items():
                btn.setChecked(False)
            self._move_dir = None
            self._push_control()

    # -- autopilot ------------------------------------------------------

    @property
    def _autopilot(self):
        return self._goal_btn.isChecked() and self._goal_model is not None

    def _refresh_goal_btn(self):
        """Go to goal <-> Ignore goal: the button shows the action."""
        self._goal_btn.setText(
            "Ignore goal" if self._goal_btn.isChecked() else "Go to goal")

    def _on_goal_toggled(self):
        self._refresh_goal_btn()
        if not self._goal_btn.isChecked():
            self._cancel_autopilot("off")
            return
        from ...bridge import live_apply as mobu_apply
        near = None
        if self._last_frame is not None:
            near = (self._last_frame.root_pos[0], self._last_frame.root_pos[2])
        try:
            self._goal_model = mobu_apply.ensure_goal_marker(near_xz_m=near)
            mobu_apply.select_goal_marker(self._goal_model)
        except Exception as exc:
            self._goal_btn.setChecked(False)
            self._refresh_goal_btn()
            self._log("error", f"Live Drive: goal marker failed: {exc}")
            return
        self._auto_arrived = False
        goal = mobu_apply.read_goal_xz(self._goal_model)
        where = f" at x={goal[0]:.1f} z={goal[1]:.1f} m" if goal else ""
        self._log("info",
                  f"Live Drive: autopilot on — green Goal marker{where} "
                  "is selected; drag it.")

    def _cancel_autopilot(self, reason):
        was_on = self._goal_btn.isChecked()
        self._goal_btn.setChecked(False)
        self._refresh_goal_btn()       # also fires for arrow keys / errors
        self._auto_arrived = False
        if was_on and self._client is not None:
            self._move_dir = None
            self._client.send_control((0.0, 1.0), 0.0,
                                      facing=self._frozen_facing, flush=True)
        if was_on and reason != "off":
            self._log("info", f"Live Drive: autopilot off ({reason}).")

    def _update_autopilot(self):
        """Steer toward the goal marker. Called from the tick (UI thread)."""
        from ...bridge import live_apply as mobu_apply
        from ...live.autopilot import goal_control

        goal = mobu_apply.read_goal_xz(self._goal_model)
        if goal is None:                     # marker deleted mid-session
            self._cancel_autopilot("goal marker gone")
            return
        frame = self._last_frame
        if frame is None:
            return
        move_dir, speed, self._auto_arrived = goal_control(
            (frame.root_pos[0], frame.root_pos[2]), goal,
            float(self._speed.value()), self._auto_arrived)
        self._move_dir = move_dir if speed > 1e-6 else None
        self._client.send_control(move_dir, speed,
                                  facing=self._frozen_facing,
                                  prompt=self._prompt_text)

    def _on_viz_toggled(self, checked):
        if self._viz is not None:
            try:
                self._viz.set_visible(checked)
            except Exception as exc:
                self._log("warn", f"Live Drive viz toggle failed: {exc}")

    def _on_face_toggled(self, checked):
        if not checked:
            # freeze facing at the direction active right now
            self._frozen_facing = self._move_dir or (0.0, 1.0)
        else:
            self._frozen_facing = None
        self._push_control()

    def _push_control(self):
        """Send the operator's current drive state — IMMEDIATELY.

        This is the direct-input path (keys, buttons, speed field), so it
        posts inline rather than waiting for the next tick: measured, the
        wait was costing 1.5 s of the input latency.
        """
        if self._client is None:
            return
        moving = self._move_dir is not None
        self._client.send_control(
            self._move_dir or (0.0, 1.0),
            float(self._speed.value()) if moving else 0.0,
            facing=self._frozen_facing,
            prompt=self._prompt_text,
            flush=True,
        )

    # ------------------------------------------------------------------
    # timeline coupling (playhead + prompt blocks)
    # ------------------------------------------------------------------

    def _sync_playhead(self, frame):
        """Drive the playhead under Play/Record; otherwise just read it.

        Driving it during plain preview would scroll the timeline away
        while the operator is still rehearsing a move, so the session
        leaves it alone until Play or Record says otherwise. It is still
        read (at ~15 Hz) so scrubbing during preview changes which prompt
        block is in force.

        While driven, the playhead follows the pose actually on screen
        (``frame.t`` relative to the first recorded frame), not wall
        clock, so the timeline never runs ahead of what the operator is
        watching. Only the integer scene frame is sought — at 24 fps that
        is one seek per ~2.5 ticks rather than one per tick.
        """
        from ...bridge import time_bridge

        if self._playhead_base is None:
            if self._tick_count % 4 == 0:
                try:
                    self._playhead_at = time_bridge.current_frame()
                except Exception:
                    pass                # scene gone; prompt keeps its value
            return
        if frame is None:
            return
        if self._t_origin is None:
            self._t_origin = frame.t
        target = self._playhead_base + int(
            round((frame.t - self._t_origin) * self._scene_fps))
        if target == self._playhead_at:
            return
        from ...bridge import time_bridge
        try:
            start, stop = time_bridge.take_range()
            if target > stop:
                # Grow a second at a time: the take span is what the
                # playhead is clamped to, so a session outrunning it would
                # silently stall at the old end.
                time_bridge.set_take_range(
                    start, target + int(self._scene_fps))
            time_bridge.goto_frame(target)
        except Exception as exc:
            self._log("warn", f"Live Drive: playhead follow off ({exc}).")
            self._playhead_base = None
            return
        self._playhead_at = target

    def _update_prompt(self):
        text = prompt_at_frame(self._prompt_blocks, self._playhead_at)
        if text == self._prompt_text:
            return
        self._prompt_text = text
        self._push_control()           # carries the new prompt to the server
        shown = (text[:48] + "…") if len(text) > 48 else (text or "(none)")
        self._log("info", f"Live Drive: prompt → {shown}")

    # ------------------------------------------------------------------
    # preview tick (UI thread -- the only place pyfbsdk is touched)
    # ------------------------------------------------------------------

    def _on_tick(self):
        from ...live.stream_client import EndOfStream

        client, session = self._client, self._session
        if client is None or session is None:
            return

        self._poll_keyboard()
        # Autopilot at ~15 Hz: it only needs to track a dragged marker,
        # and it must not re-post control every 16 ms.
        if self._autopilot and self._tick_count % 4 == 0:
            self._update_autopilot()
        # Control POSTs ride the UI tick: background threads are
        # GIL-starved inside MoBu for seconds at a time (measured), while
        # the POST itself takes single milliseconds on localhost.
        client.flush_control()
        while True:
            try:
                item = client.packets.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, EndOfStream):
                reason = ("ended (server: %s)" % item.error) if item.error \
                    else "ended by server"
                self._stop_session(reason)
                return
            session.ingest_packet(item)
            # Viz rides the PACKET, not the tick (2.5 Hz vs 60 Hz): the
            # curves only change when a new window lands, and rebuilding
            # them per tick would cost more than the preview itself.
            if self._viz is not None:
                try:
                    self._viz.update_from_packet(item)
                    self._viz.update_trail(session.frames)
                except Exception as exc:
                    self._log("warn", f"Live Drive viz off: {exc}")
                    self._viz = None

        frame = session.pose_for_now()
        if frame is not None and frame is not self._last_frame:
            from ...bridge import live_apply as mobu_apply
            # Seek BEFORE writing the pose: the seek re-evaluates the
            # scene at the new frame, which would undo a pose written
            # first. The live skeleton carries no keys on the current
            # take, so nothing of ours is evaluated back over it.
            self._sync_playhead(frame)
            self._update_prompt()
            try:
                mobu_apply.apply_preview(
                    self._joint_map, frame, self._joint_names)
            except Exception as exc:
                self._log("error", f"Live Drive preview failed: {exc}")
                self._stop_session("failed")
                return
            self._last_frame = frame

        self._tick_count += 1
        if self._tick_count % _STATUS_EVERY == 0:
            if session.recording:
                rec = " · REC"
            elif self._play_btn.isChecked():
                rec = f" · play {self._playhead_at}"
            else:
                rec = ""
            if self._autopilot:
                drive = "goal" + (" (arrived)" if self._auto_arrived else "")
            else:
                drive = "+".join(sorted(self._held_keys)) or "none"
            self._set_status(
                f"live · buffer {session.lag_frames()} frames · "
                f"keys: {drive}{rec}")

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------

    def _start_playhead(self):
        """Anchor the playhead here and start driving it from the stream."""
        from ...bridge import time_bridge
        try:
            self._playhead_base = time_bridge.current_frame()
        except Exception as exc:
            self._log("warn", f"Live Drive: playhead unavailable: {exc}")
            self._playhead_base = self._playhead_at or 0
        self._playhead_at = self._playhead_base
        self._t_origin = None          # re-anchored on the next pose
        return self._playhead_base

    def _refresh_play_btn(self):
        """Play <-> Stop: the button shows the action, not the state."""
        playing = self._play_btn.isChecked()
        self._play_btn.setText("Stop" if playing else "Play")
        self._play_btn.set_icon_ex("stop_sq" if playing else "play_tri")

    def _on_play_toggled(self):
        self._refresh_play_btn()
        if not self.live:
            self._play_btn.setChecked(False)
            self._refresh_play_btn()
            return
        if self._play_btn.isChecked():
            at = self._start_playhead()
            self._log("info", f"Live Drive: timeline running from frame {at}.")
        elif not self._session.recording:
            # Recording owns the playhead too; only stop it when the take
            # is not the one driving.
            self._playhead_base = None
            self._log("info",
                      f"Live Drive: timeline paused at {self._playhead_at}.")

    def _on_record(self):
        session = self._session
        if session is None:
            return
        if not session.recording:
            session.start_record()
            # Recording anchors the playhead: keys land where it stands
            # now (not at frame 0). Already running under Play means it
            # keeps running from wherever it has got to, uninterrupted.
            if self._playhead_base is None:
                self._start_playhead()
            self._record_start_frame = self._playhead_at
            self._record_btn.setText("Stop Recording")
            self._pill.set_text("REC")
            self._log("info", "Live Drive: recording started at frame "
                              f"{self._record_start_frame}.")
        else:
            frames = session.stop_record()
            # The playhead stays where the recording ended; it keeps
            # running only if Play is holding it.
            if not self._play_btn.isChecked():
                self._playhead_base = None
            self._record_btn.setText("Record")
            self._pill.set_text("live")
            self._bake(frames)

    def _bake(self, frames):
        if not frames:
            self._log("warn", "Live Drive: nothing recorded.")
            return
        from ...bridge import live_apply as mobu_apply
        label = "ardy live " + datetime.datetime.now().strftime("%H%M%S")
        self._set_status(f"Baking {len(frames)} frames…")
        try:
            take = mobu_apply.bake_recording(
                self._joint_map, frames, self._session.fps,
                self._joint_names, label,
                start_scene_frame=self._record_start_frame)
        except Exception as exc:
            self._log("error", f"Live Drive bake failed: {exc}")
            self._set_status(f"Bake failed: {exc}")
            return
        self._log("info",
                  f"Live Drive: baked {len(frames)} frames into take "
                  f"'{take}'.")
        self._set_status(f"Recorded → take '{take}'.")

    # ------------------------------------------------------------------

    def _set_status(self, text):
        self._status.setText(text)
