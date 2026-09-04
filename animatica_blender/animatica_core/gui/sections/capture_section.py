"""03 · Video Capture — a clip in, motion on the rig.

The second way motion arrives, beside Text to Motion, and the simpler
one to apply: the capture service returns the SOMA 77-joint rig, which
is the user's rig, so there is no source skeleton to build, characterise
or transfer through HIK. The joints are keyed where they land.

What the controls actually decide:

* **Camera** — "Static" grounds against a fixed camera; "Moving"
  estimates the camera's own path from the video (visual odometry) and
  grounds against that instead. Moving is slower and its result depends
  on how much background the camera can see.
* **People** — one performer, or everyone the tracker finds. Everyone
  costs roughly a second per frame *per person* and only works with a
  static camera — the service rejects the combination of "everyone" and
  a moving camera outright, so this section refuses it before it ever
  reaches the request.
* **Sample rate** — the estimator costs about a second per frame, so
  this is the length/latency dial, not a quality dial. 15 fps on a
  30 fps clip halves the wait.
* **More cameras** — other phones that filmed the SAME motion at the
  same moment (Q3). Not a batch: a batch is several takes and several
  jobs, this is several angles and one take, fused into one skeleton.
  The clip in the Video field is camera 1 and the reference — the fused
  take keeps its world, its scale and its root — and the list holds the
  rest, in the order they were picked. Empty is the single-clip
  capture, unchanged down to the request. The clips run through the
  estimator one at a time on the one GPU, so the estimate is
  multiplied by the number of cameras before the button is pressed, and
  the one instruction line under the list is the whole synchronisation
  protocol: the claps are what put three phones on one clock.

The preview under the path row is a `VideoPreview`: it plays the LOCAL
file, so the image is there the moment a path is known and never waits
for the upload. The upload still happens — the capture needs it, and so
does the player's fallback, where a clip Windows Media Foundation will
not decode is scrubbed as JPEGs fetched from the service one frame at a
time. Those fetches are this section's job: the widget asks, this card
answers, and it can only answer once the clip is up. The id is kept, so
the same clip captured twice uploads once.
"""

from __future__ import annotations

import math
import os

from ..qt_compat import QtWidgets, Signal
from ..video_preview import VideoPreview
from ..widgets import (Btn, CollapsibleSection, Combo, Field, Pill,
                       SubSection, TextInput)


#: What a frame costs before this session has measured anything. Wall
#: clock for one clip was seen between 223 s and 523 s depending on what
#: else held the GPU, so every estimate is a range and this one is also
#: marked as a guess.
NOMINAL_S_PER_FRAME = 1.0
#: The spread the range covers. Narrower than the observed 223–523 s
#: because that pair spans two different levels of GPU contention; the
#: range is meant to be usually-right, not always-right.
EST_LOW, EST_HIGH = 0.6, 1.5


class CaptureSection(QtWidgets.QWidget):
    """Numbered workflow card 03 — or, with ``chrome=False``, its controls alone.

    The step number and collapsible card only mean something in the main
    column, where this section competes for space with other steps. A
    window dedicated to capture alone has nothing to number against and
    nothing to collapse, so ``chrome=False`` skips the ``CollapsibleSection``
    and places the same body widgets directly in this widget's layout.
    """

    browse_requested = Signal()
    # The Q3 multi-file pick. A signal of its own rather than a flag on
    # ``browse_requested`` because the two picks mean different things:
    # Browse REPLACES the clip in the field (and, with several files,
    # queues a batch of separate takes), this one ADDS angles to the take
    # already in the field.
    cameras_requested = Signal()
    # path, camera, target_fps, upload_id ("" when the clip is not up yet),
    # people ("single" / "all")
    #
    # The extra cameras are deliberately NOT in this signal: they are read
    # off the card with ``camera_paths()``, the way ``props_classes()``
    # already is, so a host that has never heard of them keeps working and
    # the batch executor -- which builds its own request, one clip per job
    # -- cannot pick them up by accident.
    capture_requested = Signal(str, str, float, str, str)
    reapply_requested = Signal()
    upload_requested = Signal(str)                  # path
    cancel_requested = Signal()
    # The preview's "Sync with timeline", re-emitted for the host: the
    # clock it follows is the host's, which neither this card nor the
    # player is allowed to know about.
    sync_toggled = Signal(bool)

    def __init__(self, state, on_patch, parent=None, *, chrome: bool = True):
        super().__init__(parent)
        self._state = state
        self._on_patch = on_patch
        self._running = False
        self._reapply_available = False
        # The clip currently on the service, and what it is. The path is
        # kept beside the id because the id is only valid for that exact
        # path: editing the field invalidates both.
        self._upload_id: str | None = None
        self._uploaded_path: str | None = None
        self._info: dict | None = None
        self._uploading = False
        # The local file the player is pointed at, "" for none. Kept so a
        # per-keystroke path change only touches the player when the file
        # it should show actually changes.
        self._preview_path = ""
        # Seconds per estimated frame, measured by the host's last run in
        # this session. None until one finishes.
        self._rate_s_per_frame: float | None = None
        # The OTHER phones that filmed the same motion, in the order they
        # were picked. Empty is the single-clip capture: not one line of
        # the request, the worker or the service behaves differently.
        self._cameras: list[str] = []

        self._pill = Pill("", tone="info")
        self._pill.setVisible(False)
        # With chrome, the pill rides in the card header. Without one,
        # there is no header to put it in, so it is placed in the button
        # row further down instead — still the same widget the running
        # state toggles, just parked somewhere else.
        if chrome:
            self._section = CollapsibleSection(
                "Video Capture", step=3, icon_ex="record_dot", right=self._pill,
            )
        else:
            self._section = None
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        if self._section is not None:
            outer.addWidget(self._section)
            body = self._section.body_layout
        else:
            body = outer

        self._path = TextInput(getattr(state, "capture_video", "") or "",
                               placeholder="path/to/clip.mp4", mono=True)
        self._path.textChanged.connect(self._on_path_changed)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._path, 1)
        browse = Btn("Browse", icon="folder", variant="surface", size="sm")
        browse.clicked.connect(self.browse_requested.emit)
        row.addWidget(browse, 0)
        body.addWidget(Field("Video", None,
                             hint="One person, fixed camera — .mp4 / .mov"))
        body.addLayout(row)

        # Collapsed by default: opening it uploads the clip, which is the
        # one expensive thing this card does before the user asks for a
        # capture, so it happens on a deliberate click and not on show.
        # The picture does not depend on it: the player reads the file
        # from disk.
        self._preview = SubSection("Preview", open=False)
        self._preview.toggled.connect(self._on_preview_toggled)
        pbody = self._preview.body_layout

        self._video_preview = VideoPreview()
        # Only asked in the widget's fallback mode, and only answerable
        # once the clip is on the service — see `_on_frame_requested`.
        self._video_preview.frame_requested.connect(self._on_frame_requested)
        self._video_preview.sync_toggled.connect(self.sync_toggled.emit)
        pbody.addWidget(self._video_preview)

        # The player captions the position ("frame N / M"); this one
        # captions the cost of capturing the clip. Two different facts,
        # two labels — merging them would make the estimate flicker with
        # playback.
        self._caption = QtWidgets.QLabel("")
        self._caption.setObjectName("field_hint")
        self._caption.setWordWrap(True)
        pbody.addWidget(self._caption)
        body.addWidget(self._preview)

        # -- more cameras of the same motion (Q3) -------------------------
        # Collapsed by default and empty by default: a capture from one
        # phone must not grow a control it has to think about. Opening it
        # costs nothing -- nothing is uploaded here, the clips go up when
        # the capture starts, because until then they are just paths.
        self._cameras_section = SubSection("More cameras", open=False)
        cambody = self._cameras_section.body_layout
        cambody.addWidget(Field(
            "Same motion, other angles", None,
            hint="Start all phones, clap once above your head, count to "
                 "two, act; clap again at the end."))
        self._camera_list = QtWidgets.QListWidget()
        self._camera_list.setObjectName("field_hint")
        # Left on the default single selection: the enum for anything
        # else is nested differently in PySide2 and PySide6 (see
        # ``qt_compat``), and removing one camera at a time is not a
        # hardship in a list that holds two or three.
        # Four rows before it scrolls: enough to see a three-phone take
        # whole, small enough that an empty list is not a hole in the card.
        self._camera_list.setMaximumHeight(96)
        self._camera_list.setVisible(False)
        cambody.addWidget(self._camera_list)
        add = Btn("Add cameras", icon="folder", variant="surface", size="sm")
        add.clicked.connect(self.cameras_requested.emit)
        self._camera_remove = Btn("Remove", variant="surface", size="sm")
        self._camera_remove.setEnabled(False)
        self._camera_remove.clicked.connect(self._remove_selected_cameras)
        self._camera_list.itemSelectionChanged.connect(self._sync_camera_buttons)
        camrow = QtWidgets.QHBoxLayout()
        camrow.addWidget(add, 0)
        camrow.addWidget(self._camera_remove, 0)
        camrow.addStretch(1)
        cambody.addLayout(camrow)
        self._camera_caption = QtWidgets.QLabel("")
        self._camera_caption.setObjectName("field_hint")
        self._camera_caption.setWordWrap(True)
        cambody.addWidget(self._camera_caption)
        body.addWidget(self._cameras_section)

        self._camera = Combo([("static", "Static (tripod, phone on a shelf)"),
                              ("moving", "Moving")],
                             value="static")
        self._camera.valueChanged.connect(self._on_camera_changed)
        body.addWidget(Field("Camera", self._camera,
                             hint="Moving estimates the camera's path from "
                                  "the video (visual odometry) instead of "
                                  "assuming it is fixed. Slower, and the "
                                  "result depends on how much background "
                                  "stays visible."))

        # One rig per person, all of them keyed into the same take. Kept
        # to a plain two-way choice: the count is the tracker's to find,
        # not the operator's to type.
        self._people = Combo([("single", "One person"),
                              ("all", "Everyone in the clip")],
                             value="single")
        self._people.valueChanged.connect(self._on_people_changed)
        body.addWidget(Field("People", self._people,
                             hint="Everyone: one rig per person, all keyed "
                                  "into one take. Fixed camera only."))

        self._fps = Combo([("15", "15 fps — half the wait"),
                           ("30", "30 fps — every frame")], value="15")
        # The sample rate is what the estimate is priced on, so the
        # caption follows the dropdown.
        self._fps.valueChanged.connect(lambda _v: self._update_caption())
        body.addWidget(Field("Sample rate", self._fps,
                             hint="About a second per frame. Changes "
                                  "the wait, never the playback speed."))

        # A plain text field and not a picker: the detector knows COCO's
        # 80 classes, and 80 checkboxes would be a wall for a feature
        # most captures leave empty. The names are validated where the
        # vocabulary actually lives — the service answers an unknown one
        # with a 422 listing every name that works, and this card shows
        # it like any other failure.
        self._props = TextInput(getattr(state, "capture_props", "") or "",
                                placeholder="sports ball, chair")
        self._props.textChanged.connect(
            lambda v: self._on_patch({"capture_props": v}))
        body.addWidget(Field("Props", self._props,
                             hint="Objects to track as animated nulls, by "
                                  "COCO class name — e.g. sports ball, "
                                  "chair, cell phone. Empty tracks none. "
                                  "Position only, and only where the floor "
                                  "or a hand can place it."))

        self._button = Btn("Capture Motion", icon="wand", variant="solid")
        self._button.clicked.connect(self._emit_capture)
        # A capture costs minutes, applying one costs nothing, so the
        # finished job stays one click away — clearing a take and putting
        # the motion back should not mean estimating it again. Disabled
        # until a job has finished in this session: the id lives in the
        # window and the service forgets its jobs on restart, so there is
        # nothing to offer on a fresh start.
        self._reapply = Btn("Reapply last", variant="surface", size="sm")
        self._reapply.setEnabled(False)
        self._reapply.clicked.connect(self.reapply_requested.emit)
        # Only while something is running: a capture is minutes long, and
        # without this the only ways out are waiting it out or killing
        # the host. Hidden rather than disabled off-run, because a
        # permanently greyed Cancel reads as broken.
        self._cancel = Btn("Cancel", variant="surface", size="sm")
        self._cancel.setVisible(False)
        self._cancel.clicked.connect(self.cancel_requested.emit)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self._button, 1)
        buttons.addWidget(self._reapply, 0)
        buttons.addWidget(self._cancel, 0)
        if self._section is None:
            buttons.addWidget(self._pill, 0)
        body.addLayout(buttons)

        self._status = QtWidgets.QLabel("")
        self._status.setObjectName("field_hint")
        self._status.setWordWrap(True)
        body.addWidget(self._status)

        self.refresh()

    # -- host callbacks ----------------------------------------------------

    def set_path(self, path: str) -> None:
        self._path.setText(path)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_running(self, running: bool) -> None:
        """A capture is minutes long and the service refuses a second
        live job, so the button locks rather than queueing a request the
        server will reject with a 409."""
        self._running = running
        self._button.setEnabled(not running)
        self._button.setText("Capturing…" if running else "Capture Motion")
        # Shown for a re-fetch too, which cannot be cancelled: this card
        # cannot tell the two runs apart, and cancelling a job the service
        # already finished is a 409 the client swallows.
        self._cancel.setVisible(running)
        self._cancel.setEnabled(running)
        self._sync_reapply()
        self._pill.setVisible(running)
        if running:
            self._pill.set_text("running")

    def set_reapply_available(self, available: bool) -> None:
        """Offer the re-fetch once a job id exists to re-fetch."""
        self._reapply_available = bool(available)
        self._sync_reapply()

    def set_upload_pending(self) -> None:
        """The host started an upload for the path in the field."""
        self._uploading = True
        self._caption.setText("uploading…")

    def set_upload(self, path: str, upload_id: str, info) -> None:
        """A clip is on the service: remember it and price it.

        *path* is checked against the field rather than trusted: an
        upload of a 200 MB clip takes long enough for the user to have
        typed a different path by the time it lands, and adopting a stale
        id would price and preview the wrong clip.
        """
        self._uploading = False
        if path.strip() != self._path.text().strip():
            return
        self._upload_id = upload_id
        self._uploaded_path = path.strip()
        self._info = dict(info or {})
        self._sync_preview_source(self._uploaded_path)
        # What the service measured beats what the demuxer guesses, and in
        # the fallback it is the only thing that gives the scrub bar a
        # range. Handing it over re-asks for the current frame, which is
        # the first request this card can actually answer.
        # The pixel size goes with it: the player only learns the clip's
        # shape once it has decoded a frame, and until then the preview
        # would be sized on a guess. Absent or 0 from an older service
        # just leaves the player to work it out.
        self._video_preview.set_clip_info(
            fps=self._info.get("fps"),
            frames=self._info.get("frames"),
            duration_s=self._info.get("duration_s"),
            width=self._info.get("width"),
            height=self._info.get("height"),
        )
        self._update_caption()

    def set_upload_failed(self, message: str) -> None:
        """The clip never made it up — say so where the estimate would be."""
        self._uploading = False
        self._caption.setText(f"Preview unavailable: {message}")

    def set_capture_rate(self, seconds_per_frame: float) -> None:
        """Price future clips off the run that just finished.

        Session-only by design: yesterday's rate was measured against
        yesterday's GPU contention and predicts nothing about this run.
        """
        if seconds_per_frame and seconds_per_frame > 0:
            self._rate_s_per_frame = float(seconds_per_frame)
            self._update_caption()

    def set_sync_available(self, available: bool, reason: str = "") -> None:
        """Pass the host's verdict on timeline sync to the player."""
        self._video_preview.set_sync_available(available, reason)

    def set_timeline_seconds(self, seconds: float) -> None:
        """Pass the host's clock position to the player (one way, always)."""
        self._video_preview.set_timeline_seconds(seconds)

    def pause_preview(self) -> None:
        """Pause the video preview — for a host window that just hid.

        Decoding video for a hidden window is pure waste. Defensive
        against a section built without a preview widget, which some
        tests do.
        """
        preview = getattr(self, "_video_preview", None)
        if preview is not None:
            preview.pause()

    def props_classes(self) -> list:
        """The Props field as a list of class names — ``[]`` for empty.

        Read by the host when it starts a job (single clip or batch),
        which is why it is a method and not a signal argument: the batch
        executor builds its own request and would otherwise have no way
        to ask. Trimmed, empties dropped, order and case kept — the
        service matches case-insensitively and owns the vocabulary.
        """
        return [part.strip() for part in self._props.text().split(",")
                if part.strip()]

    # -- more cameras ------------------------------------------------------

    def add_cameras(self, paths) -> None:
        """Add angles on the same motion, from the host's file dialog.

        Duplicates and the clip already in the Video field are dropped
        rather than refused: the field's clip is camera 1 and listing it
        again would ask the service to fuse a clip with itself, which it
        refuses with a 422 -- a refusal the user would have to read to
        learn something the card already knew.
        """
        for path in paths or []:
            path = str(path).strip()
            if not path or path in self._cameras:
                continue
            if path == self._path.text().strip():
                continue
            self._cameras.append(path)
        self._refresh_cameras()
        if self._cameras:
            self._cameras_section.set_open(True)

    def camera_paths(self) -> list:
        """The extra cameras, in pick order -- ``[]`` for one clip.

        Read by the host when it starts a job, like ``props_classes``:
        the clip in the Video field is camera 1 and the reference (the
        fused take keeps its world, scale and root), and these follow it.
        """
        return list(self._cameras)

    def clear_cameras(self) -> None:
        self._cameras = []
        self._refresh_cameras()

    def _remove_selected_cameras(self) -> None:
        # By ROW, not by the label: the label is a basename and two
        # phones both call their file VID_0001.mp4, so removing "the one
        # named that" would remove the wrong camera. The list is rebuilt
        # from ``_cameras`` in order, so row N is camera N.
        rows = sorted((self._camera_list.row(item)
                       for item in self._camera_list.selectedItems()),
                      reverse=True)
        for row in rows:
            if 0 <= row < len(self._cameras):
                self._cameras.pop(row)
        self._refresh_cameras()

    def _sync_camera_buttons(self) -> None:
        self._camera_remove.setEnabled(
            bool(self._camera_list.selectedItems()))

    def _refresh_cameras(self) -> None:
        """Redraw the list, the count and the cost of what is in it."""
        # The clip in the field can become one of these by being typed
        # in later; it is camera 1 either way, so it leaves the list.
        field = self._path.text().strip()
        self._cameras = [p for p in self._cameras if p != field]
        self._camera_list.clear()
        for path in self._cameras:
            item = QtWidgets.QListWidgetItem(os.path.basename(path))
            item.setToolTip(path)
            self._camera_list.addItem(item)
        self._camera_list.setVisible(bool(self._cameras))
        self._sync_camera_buttons()
        count = len(self._cameras) + 1
        self._camera_caption.setText(
            "" if not self._cameras else
            f"{count} cameras, one take. The estimator runs on each clip "
            f"in turn, so the wait is about {count}x a single capture.")
        # The estimate is priced per clip, so it moves with this list.
        self._update_caption()
        # Adding an angle can be what makes the request impossible (a
        # crowd, or objects); say so now rather than on the click.
        self._check_camera_people()

    def refresh(self) -> None:
        if not self._running:
            self._pill.setVisible(False)

    # -- internals ---------------------------------------------------------

    def _sync_reapply(self) -> None:
        # The service runs one job at a time, and a re-fetch competes for
        # the same single worker slot in the host, so a live capture
        # locks this button too.
        self._reapply.setEnabled(self._reapply_available and not self._running)

    def _on_camera_changed(self, value: str) -> None:
        self._check_camera_people()

    def _on_people_changed(self, value: str) -> None:
        self._check_camera_people()

    def _pair_blocked(self) -> bool:
        """People="all" + Camera="moving" is a combination the service
        itself refuses (422) rather than one the client invented, so the
        check here exists only to catch it before the request goes out."""
        return (str(self._people.value()) == "all"
                and str(self._camera.value()) == "moving")

    def _cameras_blocked(self) -> str:
        """Why several cameras cannot be captured as asked, or "".

        Both of these are 422s the service already writes; catching them
        here turns a rejected request into a sentence beside the control
        that caused it. Both are refusals about the REQUEST, not about
        the footage: fusion averages one skeleton's joint rotations, and
        an object's position is recovered in each camera's own frame
        with no geometry to bring them together.
        """
        if not self._cameras:
            return ""
        if str(self._people.value()) == "all":
            return ("Several cameras capture one performer — set People to "
                    "\"One person\", or capture the crowd from one camera.")
        if self.props_classes():
            return ("Props are not fused across cameras — clear the Props "
                    "field, or capture the objects from one camera.")
        return ""

    def _check_camera_people(self) -> None:
        if self._pair_blocked():
            self.set_status("Everyone needs a static camera — set People "
                            "to \"One person\" or Camera to \"Static\".")
            return
        blocked = self._cameras_blocked()
        if blocked:
            self.set_status(blocked)

    def _on_path_changed(self, value: str) -> None:
        self._on_patch({"capture_video": value})
        # The field's clip is camera 1; if it is also in the extra list
        # it leaves the list rather than being sent twice.
        if value.strip() in self._cameras:
            self._refresh_cameras()
        # The field emits per keystroke, so this must never upload — it
        # only drops what the previous path put here (and re-points the
        # player, which reads the file directly and needs no upload).
        # Trigger (b) or (c) sends the new clip up.
        if value.strip() != (self._uploaded_path or ""):
            self._clear_upload()

    def _on_preview_toggled(self, is_open: bool) -> None:
        """Trigger (b): opening the preview uploads a typed-in clip."""
        if not is_open:
            # Nothing to see, nothing to decode.
            self._video_preview.pause()
            return
        path = self._path.text().strip()
        if not path:
            self._caption.setText("Choose a video first.")
            return
        # The player never needed the upload; a file that appeared after
        # the path was typed is picked up here.
        self._sync_preview_source(path)
        if self._upload_id:
            # Requests made while this was collapsed were dropped; if the
            # widget is showing server frames it has an empty image.
            if self._video_preview.is_fallback():
                self._on_frame_requested(0)
            return
        if self._uploading:
            return
        if not os.path.isfile(path):
            self._caption.setText("No file at that path.")
            return
        self.upload_requested.emit(path)

    def _sync_preview_source(self, path: str) -> None:
        """Point the player at the field's file — or at nothing.

        A path that is not a file on disk clears the preview rather than
        being handed over: the player would only fail asynchronously and
        fall back to server frames of a clip that was never uploaded.
        """
        path = (path or "").strip()
        source = path if os.path.isfile(path) else ""
        if source == self._preview_path:
            return
        self._preview_path = source
        self._video_preview.set_source(source)

    def _on_frame_requested(self, index: int) -> None:
        """Fallback mode: the player wants frame *index* from the service.

        Only answerable with a clip on the service, which is why the
        request doubles as an upload trigger — the fallback is exactly the
        case where the preview cannot happen without one.
        """
        if not self._preview.is_open():
            # Collapsed: nobody would see the frame, so it is not worth a
            # round trip. Opening the section asks again.
            return
        path = self._path.text().strip()
        if not self._upload_id:
            self.set_status("This clip does not decode locally — its frames "
                            "come from the service, which needs the clip "
                            "uploaded first.")
            if path and not self._uploading and os.path.isfile(path):
                self.upload_requested.emit(path)
            return
        # Imported here, as the workers do: the client pulls numpy in,
        # and a card that builds is worth more than one that refuses to.
        from ... import capture_client
        try:
            data = capture_client.fetch_frame(self._upload_id, int(index),
                                              max_px=480)
        except Exception as exc:                       # noqa: BLE001
            # A frame that will not load is a preview problem only — the
            # capture itself does not go through this endpoint.
            self.set_status(f"Frame {index} unavailable: {exc}")
            return
        self._video_preview.show_frame(data)

    def _clear_upload(self) -> None:
        """Forget the clip on the service and everything shown about it."""
        self._upload_id = None
        self._uploaded_path = None
        self._info = None
        # The player follows the field, not the upload: a new path must
        # never be left showing the previous clip.
        self._sync_preview_source(self._path.text())
        self._caption.setText("")

    # -- caption -----------------------------------------------------------

    def _sample_stride(self, fps: float) -> int:
        """How many source frames the estimator skips per kept frame."""
        target = float(self._fps.value() or 0) or fps
        if fps <= 0 or target <= 0:
            return 1
        return max(1, int(round(fps / target)))

    def _estimate_text(self, frames: int, fps: float) -> str:
        sampled = int(math.ceil(frames / self._sample_stride(fps))) if frames else 0
        if sampled <= 0:
            return ""
        # One GPU, one clip at a time: N cameras of one motion cost N
        # estimator runs, and the range has to say so BEFORE the button
        # is pressed. Priced off the clip in the field for want of the
        # others' frame counts -- they are paths here, not uploads, so
        # nobody has decoded them; phones filming one take run for
        # roughly the same length, which is what makes that honest
        # enough to show.
        sampled *= 1 + len(self._cameras)
        rate = self._rate_s_per_frame or NOMINAL_S_PER_FRAME
        # Marked as a guess until a run in this session has been timed:
        # the nominal second per frame is the service's own rule of
        # thumb, not a measurement of this machine.
        mark = "" if self._rate_s_per_frame else "~"
        low, high = sampled * rate * EST_LOW, sampled * rate * EST_HIGH
        return f"{mark}{_duration(low)}–{_duration(high)}"

    def _update_caption(self) -> None:
        """Clip facts and what capturing them costs — never the position,
        which is the player's caption to write."""
        if self._uploading:
            self._caption.setText("uploading…")
            return
        if not self._info:
            return
        fps = float(self._info.get("fps") or 0.0)
        frames = int(self._info.get("frames") or 0)
        duration = float(self._info.get("duration_s") or 0.0)
        parts = [f"{duration:.1f} s", f"{fps:.1f} fps", f"{frames} frames"]
        if self._cameras:
            parts.append(f"{len(self._cameras) + 1} cameras")
        estimate = self._estimate_text(frames, fps)
        if estimate:
            parts.append(f"est. {estimate}")
        self._caption.setText(" · ".join(parts))

    def _emit_capture(self) -> None:
        path = self._path.text().strip()
        if not path:
            self.set_status("Choose a video first.")
            return
        if self._pair_blocked():
            # Same check as _check_camera_people, kept here too: that one
            # only fires on a combo edit, and this is the last chance to
            # stop a request the service would answer with a 422.
            self.set_status("Everyone needs a static camera — set People "
                            "to \"One person\" or Camera to \"Static\".")
            return
        blocked = self._cameras_blocked()
        if blocked:
            self.set_status(blocked)
            return
        # Trigger (c): an empty id tells the host the clip is not up yet,
        # and it uploads before starting the job.
        upload_id = self._upload_id if self._uploaded_path == path else ""
        self.capture_requested.emit(path, str(self._camera.value() or "static"),
                                    float(self._fps.value()),
                                    upload_id or "",
                                    str(self._people.value() or "single"))


def _duration(seconds: float) -> str:
    """Seconds under a minute and a half, whole minutes above it."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min"
