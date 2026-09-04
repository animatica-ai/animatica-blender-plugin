"""Standalone Motion Capture window — the capture UI and its workers.

Split out of the main tool window (T2/W1 of PLAN-motion-capture-window):
this window owns the ``CaptureSection`` widgets, the upload/capture/
reapply workers and their session bookkeeping (last job id, measured
rate). The main window keeps the one thing that touches the rig —
``AnimaticaToolWindow.apply_capture_motion`` — which this window calls
through an explicit seam: a ``meta`` dict read off the finished worker
plus a small UI protocol (``set_status`` / ``set_reapply_available`` /
``set_capture_rate``) that this window implements.

``AppState`` stays single: the window receives the main window's state
and its ``_apply_patch`` at construction, so the path typed here is the
same ``state.capture_video`` everything else reads.

No host SDK and no ``tool_window`` import at module level — the main
window imports this module, and everything host-side is reached lazily
through ``self._main``.
"""

from __future__ import annotations

import os

from .. import host
from . import styles
from .qt_compat import QtCore, QtWidgets
from .sections.capture_section import CaptureSection
from .widgets import Btn, SubSection

# Shown in place of the list when no stamped take is in the scene. The
# restart sentence is not an apology: provenance is written to custom
# properties the host owns, which could not be verified outside a live
# session, so a reopened scene may genuinely come back with no
# shots. Better said out loud than left as an empty rectangle.
_EMPTY_SHOTS_TEXT = (
    "No shots yet. Every capture you apply lands in its own take and "
    "shows up here.\n"
    "After a restart this list only comes back for shots whose capture "
    "stamp survived in the scene file."
)

# Extra role on column 0 of a shot-list row: the index of the batch-queue
# entry the row stands for. Take rows leave it unset, which is what tells
# the two kinds of row apart. Column 0's ``UserRole`` stays the clip path
# for both.
_QUEUE_ROLE = QtCore.Qt.UserRole + 1

# The statuses a queued clip moves through. The upload pump below drives
# ``queued`` -> ``uploading`` -> ``ready``/``failed``; the capture half
# (``capturing``/``done``/``skipped``) belongs to the batch executor,
# which drives this queue through the same ``update_queue_row``.
QUEUE_STATUSES = ("queued", "uploading", "ready", "capturing", "done",
                  "failed", "skipped")


class MotionCaptureWindow(QtWidgets.QWidget):
    """Top-level window hosting the Video Capture controls.

    * ``state`` — the shared ``AppState`` (the main window's).
    * ``on_patch`` — the main window's ``_apply_patch``, so edits made
      here flow through the same single reducer.
    * ``main`` — the main-window singleton: the apply seam
      (``apply_capture_motion``), the rig checks and the log live there.
    """

    def __init__(self, state, on_patch, main, parent=None):
        super().__init__(parent)
        self.state = state
        self._apply_patch = on_patch
        self._main = main

        # -- capture session state, moved here from the main window ------
        # Live capture worker, or None. Guards against a second Capture
        # click while one is running: the service refuses a concurrent job
        # with a 409, and a queued request would surface as a confusing
        # server error rather than a disabled button.
        self._capture_worker = None
        # Last job the capture service finished for this window, and the
        # clip it came from. Re-fetching one is seconds and re-running it
        # is minutes, so the id is worth keeping — but only for as long
        # as the window lives: the service keeps a handful of recent jobs
        # and forgets them on restart, so a persisted id would mostly be
        # an offer that 404s.
        self._last_capture_job_id: str | None = None
        self._last_capture_source: str | None = None
        # Live upload worker for the capture preview, or None. The clip
        # goes up when it is chosen, not when Capture is pressed, so the
        # preview has something to show and a second capture of the same
        # clip costs no upload.
        self._upload_worker = None
        # (camera, target_fps, people) waiting for that upload to land,
        # when the user pressed Capture on a clip that was not up yet.
        self._pending_capture: tuple | None = None
        # (path, then_capture) picked while another clip was still going
        # up — one upload runs at a time, the newer pick waits its turn.
        self._pending_upload: tuple | None = None
        # Index of the queue entry whose upload holds the slot right now,
        # or None when the slot is free or held by the single-clip flow.
        # This is what tells an arriving result/failure which of the two
        # it belongs to — ``failed`` carries only a message, so the path
        # alone could not answer it.
        self._queue_upload_index: int | None = None
        # Measured seconds per estimated frame from the last capture in
        # THIS session, for the preview's cost range. Deliberately not
        # persisted: the same clip took 223 s and 523 s on this machine
        # depending on what else held the GPU, so a rate from another
        # session predicts nothing.
        self._capture_rate_s_per_frame: float | None = None
        # -- timeline sync ------------------------------------------------
        # Whether the preview's "Sync with timeline" is on (what the user
        # asked for) and whether this window is actually subscribed to the
        # main window's bridge (what is true right now). They part company
        # while the window is hidden: the box stays ticked, the 30 Hz
        # subscription does not, for the same reason the player pauses.
        self._sync_wanted = False
        self._sync_connected = False
        # -- batch queue ---------------------------------------------------
        # Clips picked together in Browse, in the order they were picked:
        # one dict per clip, ``{path, status, upload_id, info, job_id}``.
        # Nothing here talks to the service - these rows are the visible
        # half of a batch, and what moves them off ``queued`` is the
        # executor calling ``update_queue_row``.
        self._queue: list[dict] = []
        # -- batch executor (T4) -------------------------------------------
        # True while "Capture all" is walking the queue. This flag is the
        # executor's stop point: ``_advance_batch`` re-reads it before
        # every job, so dropping it to False is how a batch is stopped.
        # ``_stop_batch`` is the one place that drops it, and it doubles
        # as the "already down" guard so two paths onto the same stop
        # (Cancel and the job's own ``cancelled``) stand the batch down
        # exactly once.
        self._batch_active = False
        # Index of the queue entry whose capture job holds the worker
        # slot, or None. The capture handlers below serve the single-clip
        # flow and the batch through the same signals, and only this
        # index tells them which queue row (if any) an answer belongs to.
        self._batch_index: int | None = None

        # -- window chrome, the Settings-window way -----------------------
        self.setWindowFlag(QtCore.Qt.Window, True)
        self.setWindowTitle("Animatica Video to Motion Capture")
        self.setStyleSheet(styles.complete_stylesheet())
        self.resize(460, 620)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        il = QtWidgets.QVBoxLayout(inner)
        il.setContentsMargins(16, 16, 16, 16)
        # chrome=False: a dedicated window has no column of numbered steps
        # to collapse against, so the section drops its "03" card shell.
        self.sec_capture = CaptureSection(self.state, self._apply_patch,
                                          chrome=False)
        il.addWidget(self.sec_capture)
        # Below the capture card, not above it: the controls are what the
        # window is opened to press, and the list is the record of what
        # pressing them produced — it grows downwards as the session goes
        # on, and it reads in that order.
        # Session shots are TAKES: named containers you switch between.
        # A host without them has one animation, so every capture
        # replaces the last -- a list of switchable shots would be
        # describing something the host cannot do.
        shots = self._build_shot_list()
        shots.setVisible(host.has(host.TAKES))
        il.addWidget(shots)
        il.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll)

        self.sec_capture.browse_requested.connect(self._on_browse_capture_video)
        self.sec_capture.cameras_requested.connect(self._on_browse_extra_cameras)
        self.sec_capture.capture_requested.connect(self._on_capture_requested)
        self.sec_capture.reapply_requested.connect(self._on_reapply_capture)
        self.sec_capture.upload_requested.connect(self._on_capture_upload_requested)
        self.sec_capture.cancel_requested.connect(self._on_capture_cancel_requested)
        self.sec_capture.sync_toggled.connect(self._on_sync_toggled)
        # The bridge is built lazily by the main window and may not exist
        # yet (or ever), so the offer is re-made on every show.
        self._refresh_sync_available()

    # ------------------------------------------------------------------
    # Timeline sync — the host's playhead drives the clip preview
    # ------------------------------------------------------------------

    def _bridge(self):
        """The main window's playhead bridge, or None.

        Read through ``getattr`` every time rather than cached: it is
        built lazily inside a ``try`` over there, so "absent" is a state
        this window can leave and enter, not a fact settled at import.
        """
        return getattr(self._main, "_time_bridge", None)

    def _refresh_sync_available(self) -> None:
        if self._bridge() is None:
            self.sec_capture.set_sync_available(
                False, "The host's playhead bridge is not available.")
            return
        self.sec_capture.set_sync_available(True)

    def _on_sync_toggled(self, on: bool) -> None:
        self._sync_wanted = bool(on)
        if on:
            self._connect_timeline()
        else:
            self._disconnect_timeline()

    def _connect_timeline(self) -> None:
        """Subscribe to the signal the main window already emits.

        No second poll of ``LocalTime``: the main window runs one 30 Hz
        timer for its Prompt Timeline and this rides on the same emission.
        """
        if self._sync_connected:
            return
        bridge = self._bridge()
        if bridge is None:
            return
        try:
            bridge.time_changed.connect(self._on_timeline_frame)
        except (TypeError, RuntimeError):                   # noqa: BLE001
            return
        self._sync_connected = True

    def _disconnect_timeline(self) -> None:
        """Drop the subscription; safe to call when there is none."""
        if not self._sync_connected:
            return
        self._sync_connected = False
        bridge = self._bridge()
        if bridge is None:
            return
        try:
            bridge.time_changed.disconnect(self._on_timeline_frame)
        except (TypeError, RuntimeError):                   # noqa: BLE001
            # Already gone: the bridge was rebuilt, or Qt tore the
            # connection down with a dead receiver. Nothing left to undo.
            pass

    def _on_timeline_frame(self, frame: float) -> None:
        """Playhead moved: hand the preview the same instant in seconds.

        ``time_changed`` carries a scene FRAME, so the scene's fps is what
        turns it into a time — and a time is what the preview wants,
        because a capture applies at take frame 0 at the CLIP's own fps.
        Video second *t* is take second *t*; the scene rate belongs to
        this conversion and nowhere near the preview.
        """
        try:
            from animatica_core.bridge import time_bridge
            fps = time_bridge.current_fps()
        except Exception:                                  # noqa: BLE001
            # No host bridge, or a transport that will not answer. Without a
            # rate the frame number means nothing — better no move than a
            # wrong one.
            return
        if not fps:
            return
        self.sec_capture.set_timeline_seconds(float(frame) / float(fps))

    # ------------------------------------------------------------------
    # Shot list — the session's takes, rebuilt from the take stamps
    # ------------------------------------------------------------------

    def _build_shot_list(self) -> QtWidgets.QWidget:
        """Build the shot list card: one row per stamped capture take.

        A ``QTreeWidget`` rather than a ``QListWidget`` because a row is
        several fields that should line up as columns across rows; the
        tree is used flat, with no child items.

        The last column is an empty, unlabelled status slot: it carries
        the ``queued`` / ``uploading`` / ``capturing`` word of a clip the
        batch queue is still working on, and stays blank on the rows of
        takes already recorded - a shot that exists has no status left to
        report.
        """
        self.sec_shots = SubSection("Session shots")

        # "Capture all" lives with the shot list because that is where
        # its work becomes visible: queue rows turning into take rows.
        # Enabled only while the queue holds something startable, and
        # disabled for the whole run — the executor is single-flight and
        # a second click must not start a second one.
        self.btn_capture_all = Btn("Capture all", variant="surface",
                                   size="sm")
        self.btn_capture_all.setEnabled(False)
        self.btn_capture_all.clicked.connect(self._on_capture_all)
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_capture_all)
        self.sec_shots.body_layout.addLayout(btn_row)

        self.shot_list = QtWidgets.QTreeWidget()
        self.shot_list.setObjectName("shot_list")
        self.shot_list.setColumnCount(4)
        self.shot_list.setHeaderLabels(["Take", "Clip", "Length", ""])
        self.shot_list.setRootIsDecorated(False)
        self.shot_list.setUniformRowHeights(True)
        self.shot_list.setAlternatingRowColors(False)
        self.shot_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection)
        self.shot_list.setMinimumHeight(110)
        header = self.shot_list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        self.shot_list.itemClicked.connect(self._on_shot_clicked)
        self.sec_shots.body_layout.addWidget(self.shot_list)

        self._shots_empty = QtWidgets.QLabel(_EMPTY_SHOTS_TEXT)
        self._shots_empty.setObjectName("field_hint")
        self._shots_empty.setWordWrap(True)
        self.sec_shots.body_layout.addWidget(self._shots_empty)

        self._set_shots_empty(True)
        return self.sec_shots

    def _set_shots_empty(self, empty: bool) -> None:
        """Swap the list for its explanation, so neither is ever blank."""
        self.shot_list.setVisible(not empty)
        self._shots_empty.setVisible(empty)

    def refresh_shots(self) -> None:
        """Rebuild the rows from the scene's stamped takes.

        Without a host take manager — and with one whose custom
        properties refused the stamp — this is an empty list rather than
        an error: the list is a convenience over the scene, and a scene
        that cannot answer is the empty case, not a failure.
        """
        try:
            from animatica_core.bridge import take_manager
            takes = take_manager.capture_takes()
        except Exception:                                  # noqa: BLE001
            takes = []

        self.shot_list.clear()
        # Queued clips first, recorded takes below: the queue is what the
        # session is about to do, the takes are what it has done, and the
        # shots keep growing downwards as they did before the queue
        # existed. Rebuilding from the scene must never drop the queue -
        # it lives on this window, not in the scene.
        for index, entry in enumerate(self._queue):
            item = QtWidgets.QTreeWidgetItem(["", "", "", ""])
            item.setData(0, _QUEUE_ROLE, index)
            self._fill_queue_row(item, entry)
            self.shot_list.addTopLevelItem(item)
        for name, provenance in takes:
            clip = str((provenance or {}).get("clip") or "")
            item = QtWidgets.QTreeWidgetItem([
                str(name),
                (os.path.basename(clip) or "—")
                + self._shot_people(provenance or {})
                + self._shot_ground(provenance or {}),
                self._shot_length(provenance or {}),
                "",
            ])
            # The full path is the tooltip and the row's payload: the
            # column shows the file name because the directory is the
            # same for every shot of a session and would push the name
            # off the row.
            item.setData(0, QtCore.Qt.UserRole, clip)
            if clip:
                item.setToolTip(0, clip)
                item.setToolTip(1, clip)
            item.setTextAlignment(2, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self.shot_list.addTopLevelItem(item)
        self._set_shots_empty(not takes and not self._queue)
        self._refresh_capture_all()

    @staticmethod
    def _shot_people(provenance: dict) -> str:
        """`" · 3 people"` for a crowd, ``""`` for the ordinary one.

        Only when there is more than one: every other shot in the list is
        one person, so saying so on each of them would be noise on the
        rows that carry no news.
        """
        try:
            count = int(provenance.get("subjects") or 0)
        except (TypeError, ValueError):
            return ""
        return f" · {count} people" if count > 1 else ""

    @staticmethod
    def _shot_ground(provenance: dict) -> str:
        """`" · approx ground"` when the take's floor was a hip-height
        guess rather than measured contacts (T4), ``""`` otherwise.

        An older service never stamped ``grounded`` at all, and that
        absence must read exactly as today: nothing on the row.
        """
        return (" · approx ground"
                if provenance.get("grounded") == "approximate" else "")

    @staticmethod
    def _shot_length(provenance: dict) -> str:
        """Format the take's length, or ``""`` when the stamp cannot say.

        Provenance values are all strings, and only ``fps`` is recorded
        today — the frame count is read if some later stamp carries it.
        Without both numbers the column stays empty rather than showing a
        guessed duration.
        """
        try:
            fps = float(provenance.get("fps") or 0.0)
            frames = float(provenance.get("frames")
                           or provenance.get("num_frames") or 0.0)
        except (TypeError, ValueError):
            return ""
        if fps <= 0 or frames <= 0:
            return ""
        return MotionCaptureWindow._format_length(frames / fps)

    @staticmethod
    def _format_length(seconds: float) -> str:
        """Seconds as the Length column shows them."""
        if seconds >= 60:
            return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
        return f"{seconds:.1f} s"

    def _on_shot_clicked(self, item, _column: int = 0) -> None:
        """Make the clicked shot current: its take, and its clip in the
        preview.

        The take switch and the preview are independent — a take whose
        clip has been moved or was never recorded still selects, it just
        brings no video with it.
        """
        if item is None:
            return
        index = item.data(0, _QUEUE_ROLE)
        if index is not None:
            # A queued clip is not a shot yet: there is no take to make
            # current, so the click only points the preview at the file.
            entry = self._queue_entry(int(index))
            if entry is not None:
                self.sec_capture.set_path(str(entry.get("path") or ""))
            return
        name = item.text(0)
        from animatica_core.bridge import take_manager
        try:
            take_manager.set_current_take(name)
        except KeyError:
            # The take was deleted in the host after this list was
            # built. Warn and leave the row: the next refresh drops it.
            self._log("warn", f"Take '{name}' is no longer in the scene.")
        except Exception as exc:                           # noqa: BLE001
            self._log("warn", f"Could not switch to take '{name}': {exc}")
        clip = item.data(0, QtCore.Qt.UserRole) or ""
        if clip:
            self.sec_capture.set_path(str(clip))

    # ------------------------------------------------------------------
    # Batch queue - pending clips as rows in the same shot list
    # ------------------------------------------------------------------

    def queue_entries(self) -> list:
        """The live queue, in the order the clips were picked.

        Handed out rather than copied: the executor walks these entries
        and writes back through ``update_queue_row``, and a copy would
        only invite the two to disagree.
        """
        return self._queue

    def enqueue_clips(self, paths) -> list:
        """Add *paths* to the queue as ``queued`` rows; return the new
        entries.

        The entry point for a batch - Browse calls it with a
        multi-selection, and the executor can call it directly. No upload
        and no job starts here: rows sit at ``queued`` until something
        drives them, one clip at a time.
        """
        added = []
        for path in paths or []:
            path = str(path or "").strip()
            if not path:
                continue
            entry = {"path": path, "status": "queued", "upload_id": None,
                     "info": None, "job_id": None}
            self._queue.append(entry)
            added.append(entry)
        if added:
            self.refresh_shots()
        return added

    def update_queue_row(self, key, **changes):
        """Apply *changes* to one queue entry and repaint its row.

        *key* is the clip path or the entry's index. A path resolves to
        the FIRST entry with that path, so a queue holding the same clip
        twice must be addressed by index - which is how the executor
        walks it anyway.

        Returns the entry, or ``None`` when nothing matches: the row can
        be gone by the time a worker answers about it.
        """
        entry = self._queue_entry(key)
        if entry is None:
            return None
        entry.update(changes)
        item = self._queue_row(self._queue.index(entry))
        if item is not None:
            self._fill_queue_row(item, entry)
        # Status changes flip what "Capture all" could do without any
        # rebuild of the list, so the button follows every write.
        self._refresh_capture_all()
        return entry

    def _pump_queue_uploads(self) -> None:
        """Start the next ``queued`` clip's upload, if the slot is free.

        The whole batch driver for uploads: it takes the FIRST entry
        still at ``queued``, marks it ``uploading`` and hands it to the
        one upload slot this window owns. Everything after that arrives
        through the ordinary upload handlers, which push this again from
        ``finished`` - so one call walks the queue to its end.

        Strictly one upload in flight GLOBALLY, not one per queue: the
        slot is shared with the single-clip flow. The 200 MB cap is per
        clip and the service streams each one to disk, so parallel
        uploads would only make two clips fight for the same disk.

        Idempotent on purpose - a busy slot or a queue with nothing left
        at ``queued`` is a no-op - because it is called from several
        places that cannot know the state of the others: after an
        enqueue, off every upload's ``finished``, and (T4) after a
        capture job frees the machine.
        """
        if self._upload_worker is not None:
            return
        for index, entry in enumerate(self._queue):
            if entry.get("status") != "queued":
                continue
            path = str(entry.get("path") or "")
            self.update_queue_row(index, status="uploading")
            self._start_capture_upload(path, queue_index=index)
            return

    def _queue_entry(self, key):
        """Resolve *key* - an index or a clip path - to an entry, or None."""
        if isinstance(key, int) and not isinstance(key, bool):
            if 0 <= key < len(self._queue):
                return self._queue[key]
            return None
        path = str(key or "").strip()
        for entry in self._queue:
            if entry.get("path") == path:
                return entry
        return None

    def _queue_row_settled(self, key) -> bool:
        """Has entry *key* reached a status nothing may write over?

        ``done``/``failed``/``skipped`` are terminal, and a worker that
        answers about a settled row answers too late: the row was
        graduated, given up on, or stood down while its work was still in
        flight. A missing entry counts as settled — there is nothing left
        to write to.
        """
        entry = self._queue_entry(key)
        if entry is None:
            return True
        return entry.get("status") in ("done", "failed", "skipped")

    def _queue_row(self, index: int):
        """The row standing for queue entry *index*, or None.

        Found by scanning rather than kept in a map: ``refresh_shots``
        destroys every item, so a remembered one would be a dangling C++
        pointer within a take of being useful.
        """
        for i in range(self.shot_list.topLevelItemCount()):
            item = self.shot_list.topLevelItem(i)
            if item.data(0, _QUEUE_ROLE) == index:
                return item
        return None

    def _fill_queue_row(self, item, entry: dict) -> None:
        """Write *entry* into *item* - used to build and to repaint."""
        path = str(entry.get("path") or "")
        # No take name to show yet: the em dash keeps the column from
        # reading as a nameless take.
        item.setText(0, "\u2014")
        item.setText(1, os.path.basename(path) or "\u2014")
        item.setText(2, self._queue_length(entry))
        item.setText(3, str(entry.get("status") or ""))
        item.setData(0, QtCore.Qt.UserRole, path)
        if path:
            item.setToolTip(0, path)
            item.setToolTip(1, path)
        item.setTextAlignment(2, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

    @staticmethod
    def _queue_length(entry: dict) -> str:
        """The clip's duration once its upload reported one, else ``""``.

        ``upload_info`` carries ``duration_s`` directly, so a row that has
        not been uploaded yet simply has no length to show.
        """
        info = entry.get("info") or {}
        try:
            seconds = float(info.get("duration_s") or 0.0)
        except (TypeError, ValueError):
            return ""
        if seconds <= 0:
            return ""
        return MotionCaptureWindow._format_length(seconds)

    # ------------------------------------------------------------------
    # Batch executor - "Capture all" walks the queue, one job at a time
    # ------------------------------------------------------------------

    def _refresh_capture_all(self) -> None:
        """Enable "Capture all" only when pressing it could do work.

        ``queued`` and ``uploading`` count as startable: the executor
        waits at the head of the queue for uploads to land rather than
        demanding the whole queue be ``ready`` up front. During a batch
        the button goes dark — the executor is single-flight and a
        second click must not start a second walk; the section's own
        Cancel is what stops it (see ``_on_capture_cancel_requested``).

        ``skipped`` is terminal, so a stood-down queue leaves the button
        dark: pressing Capture all again must not silently re-run clips
        the user watched being skipped.
        """
        startable = any(e.get("status") in ("queued", "uploading", "ready")
                        for e in self._queue)
        self.btn_capture_all.setEnabled(startable and not self._batch_active)

    def _on_capture_all(self) -> None:
        """Start the batch: from here the queue drives itself.

        Client-side and sequential on purpose — the service's JobStore
        is single-flight, so a second concurrent job would only earn a
        409. The executor shares the one capture slot with the manual
        flow: a capture the user started by hand simply makes the batch
        wait for its ``finished``.
        """
        if self._batch_active:
            return
        self._batch_active = True
        self._refresh_capture_all()
        self._log("info", "Capture all: working through the queue.")
        self._advance_batch()

    def _advance_batch(self) -> None:
        """Start the next batch job, if this is the moment for one.

        The whole executor. Called from "Capture all", from every
        upload's ``finished`` (the head of the queue may just have
        reached ``ready``) and from every capture job's ``finished`` —
        and a no-op unless a batch is active, the capture slot is free
        and the first unfinished entry is ``ready``. Being callable
        from anywhere at any time is what lets the batch survive the
        upload pump running alongside it.
        """
        if not self._batch_active:
            return
        if self._batch_index is not None or self._capture_worker is not None:
            # A job holds the slot — the batch's own, or one the user
            # started by hand. Its ``finished`` calls back here.
            return
        for index, entry in enumerate(self._queue):
            status = entry.get("status")
            if status in ("done", "failed", "skipped"):
                # Settled rows. ``failed`` covers both halves — an
                # upload that never produced an id and a capture the
                # batch already gave up on — and the batch moves past
                # both rather than retrying.
                continue
            if status != "ready":
                # Still ``queued``/``uploading``: strict queue order, so
                # the batch waits for the head instead of capturing
                # around it. The upload handlers call back here.
                return
            self.update_queue_row(index, status="capturing")
            self._batch_index = index
            # Camera, sample rate and the people choice are all read off
            # the section so a batch obeys the same dropdowns a manual
            # capture would. Private access, but the alternative is a
            # second set of settings for batches.
            camera = str(self.sec_capture._camera.value() or "static")
            target_fps = float(self.sec_capture._fps.value())
            people = str(self.sec_capture._people.value() or "single")
            if people == "all" and camera == "moving":
                # Same combination the section itself refuses to submit —
                # the service answers it with a 422, so the whole batch
                # stands down rather than sending clip after clip into it.
                self._stop_batch("Everyone needs a static camera — check "
                                 "People/Camera in Motion Capture",
                                 level="warn")
                return
            self._start_capture_job(str(entry.get("path") or ""), camera,
                                    target_fps,
                                    str(entry.get("upload_id") or ""), people)
            if self._capture_worker is None:
                # ``_start_capture_job`` refused. With the slot known
                # free, the only refusal left is the rig check — and a
                # session without an actor records nothing, so the whole
                # batch stands down rather than failing clip after clip.
                self._stop_batch("no rig to key", level="warn")
            return
        # Nothing left to start: the walk is over. ``failed`` rows stay
        # in the queue as the record of what did not make it.
        self._batch_active = False
        self._log("ok", "Capture all: queue finished.")
        self._refresh_capture_all()

    def _stop_batch(self, reason: str, level: str = "info") -> None:
        """Stand the whole batch down: *reason* says why, in the log.

        The one way out of a running batch, whatever ended it — the rig
        vanishing mid-walk (T4) or the user cancelling it (T5). Every
        row that has not settled becomes ``skipped``, NOT ``failed``:
        nothing broke, the walk simply stopped before reaching them, and
        the two must not look alike in the list or read alike in the
        log (hence ``info``/``warn``, never ``error``). The rows are
        terminal on purpose — see ``_refresh_capture_all``.

        Idempotent through ``_batch_active``: a cancel arrives on two
        paths that can run in either order (the button's own handler and
        the job's ``cancelled`` signal), and only the first of them
        finds a batch to stop.

        ``_batch_index`` is cleared only when no worker holds the
        capture slot. While one is live the cursor is that worker's, and
        ``_on_capture_finished`` is what frees it — clearing it here
        would let a job that answers after the cancel be mistaken for a
        single-clip capture and applied to the rig.
        """
        if not self._batch_active:
            return
        self._batch_active = False
        if self._capture_worker is None:
            self._batch_index = None
        skipped = 0
        for index, entry in enumerate(self._queue):
            if entry.get("status") in ("done", "failed", "skipped"):
                continue
            self.update_queue_row(index, status="skipped")
            skipped += 1
        self._log(level,
                  f"Capture all stopped: {reason}, {skipped} queued "
                  "clip(s) skipped.")
        self._refresh_capture_all()

    def _remove_queue_entry(self, index: int) -> None:
        """Drop entry *index* and keep the positional cursors honest.

        Two live cursors point into ``_queue`` by position — the upload
        slot's ``_queue_upload_index`` and the batch's ``_batch_index``
        — and removing an entry shifts everything behind it. An upload
        in flight for a later entry keeps working because its index is
        re-based here; without this, its result would be written onto
        the wrong row (or dropped by the path check) and the row would
        sit at ``uploading`` forever.
        """
        if not (0 <= index < len(self._queue)):
            return
        del self._queue[index]
        if self._queue_upload_index is not None:
            if self._queue_upload_index > index:
                self._queue_upload_index -= 1
            elif self._queue_upload_index == index:
                self._queue_upload_index = None
        if self._batch_index is not None:
            if self._batch_index > index:
                self._batch_index -= 1
            elif self._batch_index == index:
                self._batch_index = None

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Reopening the window re-reads the scene: takes can have been
        added, renamed or deleted in the host while it was hidden."""
        super().showEvent(event)
        self.refresh_shots()
        # The bridge may have appeared (or gone) since the last show, and
        # a sync the user left on is re-subscribed here — `hideEvent` drops
        # the subscription without touching the checkbox.
        self._refresh_sync_available()
        if self._sync_wanted:
            self._connect_timeline()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Stop following the playhead while nobody is looking.

        Reached from `closeEvent` too — that one hides rather than
        destroys, and `hide()` sends this event. A 30 Hz signal seeking a
        player inside an invisible window is exactly the waste the
        preview's own `pause()` avoids; the checkbox keeps its state, so
        showing the window again resumes the follow.

        The preview itself is paused here too: decoding video for a
        hidden window is pure waste, and reopening the section (or the
        window) starts it again.
        """
        self._disconnect_timeline()
        self.sec_capture.pause_preview()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        """Hide instead of destroy — the Settings-singleton pattern.

        Deliberately NO worker teardown here: the upload and capture
        workers this window parents keep polling while it is hidden, and
        a capture that finishes with the window closed still applies —
        the result lands in ``_on_capture_result``, which calls the main
        window's ``apply_capture_motion``. Destroying the window (or
        stopping the workers) on close would turn "I closed a progress
        window" into "my ten-minute job was silently thrown away".
        Reopening simply shows the same widget with its true state.

        Only while this is a top-level window. Docked in its ``FBTool`` it
        is a child of the tool region, and refusing to close would fight
        the host's own teardown when the tool is destroyed (same guard the
        Console carries).
        """
        if not self.isWindow():
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()

    # ------------------------------------------------------------------
    # UI protocol for the apply seam (tool_window.apply_capture_motion)
    # ------------------------------------------------------------------

    def set_status(self, text: str) -> None:
        self.sec_capture.set_status(text)

    def set_reapply_available(self, available: bool) -> None:
        self.sec_capture.set_reapply_available(available)

    def set_capture_rate(self, seconds_per_frame: float) -> None:
        self._capture_rate_s_per_frame = float(seconds_per_frame)
        self.sec_capture.set_capture_rate(seconds_per_frame)

    # ------------------------------------------------------------------
    # Plumbing to the main window
    # ------------------------------------------------------------------

    def _log(self, level: str, text: str) -> None:
        self._main._log(level, text)

    # ------------------------------------------------------------------
    # Video Capture handlers (moved verbatim from AnimaticaToolWindow;
    # only the rig checks and the log now route through self._main)
    # ------------------------------------------------------------------

    def _on_browse_capture_video(self) -> None:
        """Pick one clip, or several.

        ONE file is the single-clip flow unchanged, queue untouched: the
        path lands in the field and the clip goes up immediately. TWO or
        more is a batch - the clips become ``queued`` rows and nothing is
        uploaded here, because a batch uploads one clip at a time and
        that order is the executor's to decide. The first pick still goes
        in the field, so the preview has something to show.
        """
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Choose a video", self.state.capture_video or "",
            "Video (*.mp4 *.mov *.avi *.mkv);;All files (*)")
        if not paths:
            return
        if len(paths) == 1:
            path = paths[0]
            self.sec_capture.set_path(path)
            self._apply_patch({"capture_video": path})
            # Trigger (a): the clip goes up as soon as it is chosen, so
            # the preview has frames to fetch and the capture that
            # follows starts from an id instead of a file.
            self._start_capture_upload(path)
            return
        self.enqueue_clips(paths)
        self.sec_capture.set_path(paths[0])
        self._apply_patch({"capture_video": paths[0]})
        self._log("info", f"{len(paths)} clips queued.")
        # Field and state first, pump last: the first clip of the batch is
        # the one now in the field, so its result is allowed to adopt the
        # card's upload id — but only if the card already points at it.
        self._pump_queue_uploads()

    def _on_browse_extra_cameras(self) -> None:
        """Pick the OTHER phones that filmed the same motion.

        Nothing is uploaded here, unlike Browse: these clips are not
        previewed and not queued, and they go up inside the capture job
        that wants them -- one at a time, like everything else that
        talks to this service. Until then they are paths, and a path the
        user removes again has cost nothing.
        """
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Choose the other cameras", self.state.capture_video or "",
            "Video (*.mp4 *.mov *.avi *.mkv);;All files (*)")
        if not paths:
            return
        self.sec_capture.add_cameras(paths)
        self._log("info", f"{len(paths)} more camera(s) on this take.")

    def _on_capture_upload_requested(self, path: str) -> None:
        """Trigger (b): the user opened the preview on a typed-in path."""
        self._start_capture_upload(path)

    def _start_capture_upload(self, path: str, then_capture=None,
                              queue_index=None) -> None:
        """Put *path* on the capture service, off the UI thread.

        *then_capture* is ``(camera, target_fps, people)`` when the upload
        is the first step of a Capture the user already asked for; the job
        starts from the result.

        *queue_index* marks this as the batch pump's turn at the slot
        rather than the user's: the result is written back onto that
        queue row, and the card is left alone because the clip going up
        is usually not the clip the card is showing.

        One upload at a time. Asking again for the clip already going up
        (Capture pressed while the Browse upload runs) only attaches the
        capture to it — that is the "uploads once" the preview is worth.
        Asking for a different clip (a second Browse) queues it instead,
        so the newer pick is not silently dropped.
        """
        if self._upload_worker is not None:
            if queue_index is not None:
                # Unreachable through the pump, which only starts on a
                # free slot — and a no-op rather than a re-queue on
                # purpose: ``_pending_upload`` carries no row index, so
                # parking a queue clip there would lose the row it
                # belongs to. The pump picks the entry up again from the
                # next ``finished``; it is still at ``uploading``, so
                # reset it to ``queued`` first.
                self.update_queue_row(queue_index, status="queued")
                return
            if path == getattr(self._upload_worker, "video_path", None):
                self._pending_capture = then_capture or self._pending_capture
                return
            then_capture = then_capture or self._pending_capture
            self._pending_capture = None
            self._pending_upload = (path, then_capture)
            return
        from animatica_core.gui.capture_worker import UploadWorker
        self._pending_capture = then_capture
        self._queue_upload_index = queue_index
        if queue_index is None:
            # The card's "uploading…" caption belongs to the clip in the
            # field. A batch clip going up behind it must not relabel it.
            self.sec_capture.set_upload_pending()
        self._log("info", f"Uploading {path} …")
        worker = UploadWorker(path, parent=self)
        worker.result.connect(self._on_capture_upload_result)
        worker.failed.connect(self._on_capture_upload_failed)
        # Same GC-safe cleanup contract as the capture worker below.
        worker.finished.connect(self._on_capture_upload_finished)
        self._upload_worker = worker
        worker.start()

    def _queue_upload_row(self, path: str):
        """The queue index this upload result belongs to, or None.

        The slot is shared, so "is this a batch clip?" is answered by the
        index the pump wrote when it took the slot — confirmed against
        the path the worker reports, so a result can never be written
        onto a row it did not come from.
        """
        index = self._queue_upload_index
        if index is None:
            return None
        entry = self._queue_entry(index)
        if entry is None or entry.get("path") != str(path or ""):
            return None
        return index

    def _on_capture_upload_result(self, payload) -> None:
        index = self._queue_upload_row(payload.get("path") or "")
        if index is not None and self._queue_row_settled(index):
            # The batch stood down (cancelled, or no rig) while this clip
            # was still going up. There is no cancelling an upload — the
            # client has no such call — so it lands anyway, and the row
            # it belongs to is already terminal. Writing ``ready`` back
            # would re-offer a clip the user watched being skipped, and
            # would light "Capture all" up again.
            index = None
        if index is not None:
            # ``ready`` and nothing more: the clip is on the service, the
            # Length column fills itself from ``info["duration_s"]``, and
            # the job that consumes it is the executor's to start.
            self.update_queue_row(index, status="ready",
                                  upload_id=payload.get("upload_id") or "",
                                  info=payload.get("info") or {})
        # Falls through to the card on purpose: ``set_upload`` checks the
        # path against the field and returns when they differ, so the one
        # batch clip that IS in the field gets its preview and its id
        # (no second upload when Capture is pressed on it), and the rest
        # leave the card untouched.
        info = payload.get("info") or {}
        self.sec_capture.set_upload(payload.get("path") or "",
                                    payload.get("upload_id") or "", info)
        self._log("ok",
                  f"Clip uploaded: {float(info.get('duration_s') or 0):.1f} s, "
                  f"{int(info.get('frames') or 0)} frames.")
        pending = self._pending_capture
        self._pending_capture = None
        if pending is not None:
            camera, target_fps, people = pending
            # Trigger (c) finishing: the user pressed Capture on a clip
            # that was not up yet, so this is the manual flow and the
            # card's extra cameras belong to it.
            self._start_capture_job(payload.get("path") or "", camera,
                                    target_fps,
                                    payload.get("upload_id") or "", people,
                                    extra_paths=self.sec_capture.camera_paths())

    def _on_capture_upload_failed(self, message: str) -> None:
        index = self._queue_upload_index
        if index is not None:
            # One row's problem, not the batch's: the clip is marked and
            # the pump carries on from ``finished``. The card is left
            # alone for the same reason the pending caption was.
            if not self._queue_row_settled(index):
                # Same terminal-row rule as the result above, the other
                # way round: a row the batch already stood down must not
                # turn red after the fact. The break still gets its line.
                self.update_queue_row(index, status="failed")
            self._log("error", f"Upload failed: {message}")
            return
        # A failed upload cancels the capture that was waiting on it —
        # there is nothing to start a job from.
        pending, self._pending_capture = self._pending_capture, None
        self._log("error", f"Upload failed: {message}")
        self.sec_capture.set_upload_failed(str(message))
        if pending is not None:
            self.sec_capture.set_status(f"Upload failed: {message}")

    def _on_capture_upload_finished(self) -> None:
        self._upload_worker = None
        self._queue_upload_index = None
        queued, self._pending_upload = self._pending_upload, None
        if queued is not None:
            path, then_capture = queued
            # The user's own pick jumps the batch: it is what they are
            # looking at. The pump resumes from THAT upload's finished.
            self._start_capture_upload(path, then_capture=then_capture)
            # The upload that just landed may have turned the head of
            # the queue ``ready``: a waiting batch still gets its look.
            self._advance_batch()
            return
        self._pump_queue_uploads()
        self._advance_batch()

    def _capture_rig_ready(self) -> bool:
        """A rig must exist before a capture: unlike Generate, capture
        keys an existing skeleton and builds nothing. The rig lives with
        the main window, so the check does too."""
        self._main._prune_dead_joint_map()
        if self._main._joint_map:
            return True
        self._log("error",
                  "Create or select a skeleton first — capture keys an "
                  "existing rig, it does not build one.")
        self.sec_capture.set_status("No rig to key. Create a skeleton first.")
        return False

    def _on_capture_requested(self, path: str, camera: str,
                              target_fps: float, upload_id: str,
                              people: str = "single") -> None:
        """Send a clip to the capture service, off the UI thread.

        Requires a rig to key: the service returns the SOMA 77-joint rig,
        which IS this rig, so there is nothing to characterise and
        nothing to retarget — but that only holds if a rig is there to
        receive it.

        An empty *upload_id* is trigger (c): the clip was never uploaded
        (typed path, preview never opened), so it goes up first and the
        job starts when it lands.
        """
        if self._capture_worker is not None:
            self._log("warn", "A capture is already running.")
            return
        if not self._capture_rig_ready():
            return
        if not upload_id:
            self.sec_capture.set_status("Uploading…")
            self._start_capture_upload(path,
                                       then_capture=(camera, target_fps, people))
            return
        # The extra cameras are read off the card here rather than
        # carried through the signal, exactly as Props is -- and read
        # ONLY on this path: the batch executor calls
        # ``_start_capture_job`` positionally and gets the default, so a
        # queue of separate takes cannot pick up somebody's second phone.
        self._start_capture_job(path, camera, target_fps, upload_id, people,
                                extra_paths=self.sec_capture.camera_paths())

    def _start_capture_job(self, path: str, camera: str, target_fps: float,
                           upload_id: str, people: str = "single", *,
                           extra_paths=()) -> None:
        """Start the estimator on an already-uploaded clip.

        The rig is re-checked because this can run minutes after the
        click that asked for it, with an upload in between.

        *extra_paths* are the other cameras of the same motion, still on
        disk: the worker uploads them and asks for one fused take. Empty
        -- the default, and what the batch executor passes -- is the
        single-clip capture, request and all.
        """
        if self._capture_worker is not None:
            self._log("warn", "A capture is already running.")
            return
        if not self._capture_rig_ready():
            return

        from animatica_core.gui.capture_worker import CaptureWorker
        self.sec_capture.set_running(True)
        self.sec_capture.set_status("Starting…")
        self._log("info", f"Capturing motion from {path} …")
        # Read off the card here rather than carried through the request
        # signal, because the batch path builds its own request and would
        # otherwise capture a crowd's clips with nobody's objects (it
        # already reads People the same way). The cost is that a props
        # field edited during a queued upload applies to the job that
        # starts, not the click that asked for it.
        props = self.sec_capture.props_classes()
        if props:
            self._log("info", f"Also tracking objects: {', '.join(props)}.")
        cameras = [str(p) for p in (extra_paths or [])]
        if cameras:
            self._log("info",
                      f"{len(cameras) + 1} cameras on this take; the clips "
                      "run one after another and are then synchronised and "
                      "fused.")
        worker = CaptureWorker(path, camera=camera, target_fps=target_fps,
                               upload_id=upload_id, people=people,
                               props=props, extra_paths=cameras, parent=self)
        worker.progress.connect(self._on_capture_progress)
        worker.result.connect(self._on_capture_result)
        worker.failed.connect(self._on_capture_failed)
        worker.cancelled.connect(self._on_capture_cancelled)
        # Same GC-safe cleanup as the generation worker: QThread.finished is
        # left free for exactly this, so a worker cannot be collected while
        # it is still emitting.
        worker.finished.connect(self._on_capture_finished)
        self._capture_worker = worker
        worker.start()

    def _on_reapply_capture(self) -> None:
        """Fetch the last finished job again and key it onto the rig.

        No job is started: the estimator already ran, the service still
        holds the result, and the whole point is that putting the motion
        back should cost a click rather than the minutes it cost the
        first time.

        The fetch is a few megabytes of JSON — seconds, but seconds the
        UI thread must not spend — so it runs on a worker, and it takes
        the same single-worker slot as a capture.
        """
        if self._capture_worker is not None:
            self._log("warn", "A capture is already running.")
            return
        job_id = self._last_capture_job_id
        if not job_id:
            self.sec_capture.set_status("No capture to reapply yet.")
            return
        self._main._prune_dead_joint_map()
        if not self._main._joint_map:
            self._log("error",
                      "Create or select a skeleton first — capture keys an "
                      "existing rig, it does not build one.")
            self.sec_capture.set_status("No rig to key. Create a skeleton first.")
            return

        from animatica_core.gui.capture_worker import ReapplyWorker
        self.sec_capture.set_running(True)
        self.sec_capture.set_status("Fetching the last capture…")
        self._log("info", f"Reapplying capture job {job_id} …")
        worker = ReapplyWorker(job_id, video_path=self._last_capture_source,
                               parent=self)
        worker.progress.connect(self._on_capture_progress)
        # Deliberately the capture handler and not a bare apply_animation:
        # that handler is where the HIK input is switched off and the
        # joint names are checked against the rig. A shortcut past it
        # would key motion the viewport never shows.
        worker.result.connect(self._on_capture_result)
        worker.failed.connect(self._on_reapply_failed)
        worker.finished.connect(self._on_capture_finished)
        self._capture_worker = worker
        worker.start()

    def _on_reapply_failed(self, message: str) -> None:
        """A gone job is ordinary, not a fault — say so in those words.

        The service keeps only its most recent jobs and forgets all of
        them when it restarts, so the id this window holds outlives the
        result it points at more often than not.
        """
        text = str(message)
        if "404" in text:
            self._last_capture_job_id = None
            self.sec_capture.set_reapply_available(False)
            self._log("warn", f"Capture job is gone from the service: {text}")
            self.sec_capture.set_status(
                "Server no longer has that capture — run a new one.")
            return
        self._on_capture_failed(text)

    def _on_capture_progress(self, text: str) -> None:
        self.sec_capture.set_status(str(text))

    def _on_capture_finished(self) -> None:
        self._capture_worker = None
        self.sec_capture.set_running(False)
        # If this was a batch job, its outcome (done/failed/skipped) was
        # already written by the handler that ran before this queued
        # signal — here the cursor is freed and the walk continues. A
        # manual job ends here too: the batch that waited on it gets its
        # turn at the freed slot.
        self._batch_index = None
        if self._batch_active:
            self._advance_batch()

    def _on_capture_failed(self, message: str) -> None:
        if self._batch_index is not None:
            # One row's failure, not the batch's: mark it, keep whatever
            # job id the worker earned before breaking, and let
            # ``finished`` move the executor to the next clip.
            self.update_queue_row(
                self._batch_index, status="failed",
                job_id=getattr(self._capture_worker, "job_id", None))
        self._log("error", f"Capture failed: {message}")
        self.sec_capture.set_status(str(message))

    def _on_capture_cancel_requested(self) -> None:
        """Ask the service to stop the job this window is waiting on.

        Straight on the UI thread, unlike everything else that talks to
        the service: the endpoint answers immediately and leaves the job
        to notice at its next frame. The terminal state still arrives the
        one way it ever does, through the poll the worker is already in.

        Before the job exists there is nothing to stop — the click landed
        during the upload, or after the run was already cleaned up — and
        cancelling the upload is not what this button offers.

        During a batch this is the CANCEL BATCH button, because it is the
        only Cancel there is: the live job is asked to stop the same way
        a single capture's would be, and the rows behind it stand down
        through ``_stop_batch``. Nothing already recorded is touched —
        takes applied earlier in the walk stay in the scene.
        """
        job_id = getattr(self._capture_worker, "job_id", None)
        if not job_id:
            if self._batch_active:
                # Pressed between two jobs — the walk is waiting on an
                # upload, so there is no job to stop, but the walk is
                # what the user is cancelling. An upload in flight is
                # left to finish (the client has no upload cancel); its
                # row is terminal by then, so nothing starts from it.
                self._stop_batch("cancelled")
                self.sec_capture.set_status("Cancelled.")
                return
            self.sec_capture.set_status("Nothing to cancel yet.")
            return
        from animatica_core import capture_client

        self.sec_capture.set_status("Cancelling…")
        try:
            capture_client.cancel(job_id)
        except Exception as exc:                       # noqa: BLE001
            # The job was not asked to stop, so the batch keeps its
            # state: standing the queue down over a failed request would
            # skip clips whose capture is still running.
            self._log("warn", f"Could not cancel capture job {job_id}: {exc}")
            self.sec_capture.set_status(f"Cancel failed: {exc}")
            return
        self._log("info", f"Cancel requested for capture job {job_id}.")
        # After the request, not before: the rest of the queue stands
        # down only once the live job has actually been told to stop.
        # A no-op when no batch is running, and a no-op again if the
        # worker's ``cancelled`` already arrived (it can, synchronously,
        # if the poll answers inside the call above).
        self._stop_batch("cancelled")

    def _on_capture_cancelled(self) -> None:
        """The user's own stop, so: no error log, no red, no message.

        The rest of the teardown is the failed path's — ``finished``
        clears the worker slot and unlocks the buttons — because a
        cancelled run leaves the scene exactly as untouched as a failed
        one does.

        A batch job that comes back cancelled takes the batch with it:
        starting the next clip against a deliberate stop would be a
        fight. The stop runs from here as well as from the button so
        that a cancel the SERVICE reports (rather than one this window
        asked for) stands the walk down too; ``_stop_batch`` makes the
        second of the two a no-op.
        """
        self._stop_batch("cancelled")
        self._log("info", "Capture cancelled.")
        self.sec_capture.set_status("Cancelled.")

    def _on_capture_result(self, motions) -> None:
        """The seam: read the worker here, apply on the main window.

        *motions* is the list of people the clip yielded — one entry for
        an ordinary capture, several when the job ran with people="all".

        Everything the apply needs from the worker is copied into
        ``meta`` NOW, because the worker slot is this window's and the
        main window no longer sees it. ``motions`` carries no id, so
        it comes off the worker that emitted this signal; the queued emit
        orders the worker's write before this read, and the worker is
        still held in ``_capture_worker`` until its ``finished`` fires
        (queued after this one).

        The job is remembered BEFORE the apply can refuse or fail: an
        apply that lands on the wrong rig, or on no rig, is exactly the
        case where the user wants to fix the scene and press Reapply —
        not to spend the estimator's minutes again.
        """
        worker = self._capture_worker
        meta = {
            "job_id": getattr(worker, "job_id", None),
            "video_path": getattr(worker, "video_path", None),
            "elapsed_s": getattr(worker, "elapsed_s", None),
            "summary": getattr(worker, "summary", None),
            # The clip's objects ride in ``meta`` and not in ``motions``
            # because they belong to the CLIP, not to any person in it —
            # the same reason the service puts them at the top level of
            # the payload. ``props_stride`` comes with them: their frame
            # indices are the video's, not the sampling grid's.
            "props": getattr(worker, "props", None) or [],
            "props_stride": getattr(worker, "props_stride", 1) or 1,
        }
        if meta["job_id"]:
            self._last_capture_job_id = meta["job_id"]
            if meta["video_path"]:
                self._last_capture_source = meta["video_path"]
        batch_index = self._batch_index
        if batch_index is None:
            self._main.apply_capture_motion(motions, meta=meta, ui=self)
        else:
            entry = self._queue_entry(batch_index) or {}
            clip = os.path.basename(str(entry.get("path") or ""))
            # The id lands on the row BEFORE the apply, for the same
            # reason the window remembered it above: an apply that
            # refuses or breaks must not lose the minutes the estimator
            # already spent on this row's job.
            self.update_queue_row(batch_index, job_id=meta["job_id"])
            # Applying switches the current take for a moment — the
            # accepted, named jump. The status says whose moment it is.
            self.set_status(f"Applying {clip}…")
            try:
                self._main.apply_capture_motion(motions, meta=meta,
                                                ui=self)
            except Exception as exc:                   # noqa: BLE001
                # Defensive only for the batch: a raise here must not
                # stop the walk. The single-clip path keeps its bare
                # call and its Qt-level surfacing.
                self._log("error", f"Applying {clip} failed: {exc}")
                self.update_queue_row(batch_index, status="failed")
            else:
                # Graduation: the finished row leaves the queue, and the
                # refresh below shows the freshly stamped take where it
                # stood — "about to do" turned into "done".
                self.update_queue_row(batch_index, status="done")
                self._remove_queue_entry(batch_index)
        # After the apply, not before: the take is created and stamped
        # inside it, so this is the first moment the new shot exists.
        # Runs even when the apply refused — the list then simply shows
        # what is really there.
        self.refresh_shots()
        self._report_multiview(meta.get("summary"), batch_index)

    def _report_multiview(self, summary, batch_index) -> None:
        """Say how many cameras made it into the take, and how sure the
        sync was — the one thing a multi-camera capture must not leave
        the user guessing at.

        The sentence is the SERVICE's: it is the only side that knows
        whether the clips were fused or whether the take fell back to
        the reference camera because they could not be placed on one
        clock. Absent for a single-clip capture, which is why nothing is
        written then — the apply's own status stands.

        Logged as a warning when fusion did NOT happen: a take that came
        back from one camera after the estimator ran on three is not a
        failure, but it is not what was asked for either.
        """
        # Imported here, as everything that touches the client in this
        # window is: it pulls numpy in, and a window that builds is worth
        # more than one that refuses to.
        from animatica_core import capture_client

        note = capture_client.multiview_note(summary)
        if not note or batch_index is not None:
            return
        fused = bool((summary or {}).get("multiview", {}).get("fused"))
        self.sec_capture.set_status(note)
        self._log("ok" if fused else "warn", note)
