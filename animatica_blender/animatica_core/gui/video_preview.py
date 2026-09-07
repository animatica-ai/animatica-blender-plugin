"""Clip preview: a real player, with a server-frame scrub as its floor.

A `QLabel` holding a pixmap reports that pixmap's size as its own and
scales the image to fill its rect — a portrait clip overflows the
label's height and gets cropped, and a wide pixmap can pin the window
open. `FitImage` is a plain `QWidget` instead: it keeps the original,
unscaled pixmap and does the `KeepAspectRatio` scaling itself in
`paintEvent`, so a resize just repaints at the new size with no
relayout loop, the image is never cropped, and the widget can shrink
below the pixmap's width.

`VideoPreview` puts a `QMediaPlayer` in front of that image widget and
keeps the image as the fallback. Two limits shape it:

* **Playback needs PySide6's QtMultimedia.** The rest of the GUI goes
  through the `qt_compat` PySide2/PySide6 shim, but QtMultimedia does
  not: Qt5's player is a different API (`setMedia(QMediaContent(...))`,
  no `QAudioOutput`, `error` instead of `errorOccurred`) and would have
  to be written and tested twice. So native playback is PySide6-only —
  under PySide2, or under a PySide6 build shipped without QtMultimedia,
  the widget starts in the server-frame mode and says so.
* **Windows Media Foundation decodes mp4/h264 and most .mov, not
  everything a phone produces.** A clip WMF refuses is not a broken
  preview: on `errorOccurred` or `InvalidMedia` the widget swaps to the
  JPEG scrub, which is decoded server-side.

In that fallback the frames are fetched by the HOST, not here — this
module stays free of `capture_client` so it can be built and tested
without the network stack. The widget asks (`frame_requested`) and the
host answers (`show_frame`).

The same seam carries the timeline sync. "Sync with timeline" makes the
player a slave: the widget offers a checkbox (`sync_toggled`), an
availability switch (`set_sync_available`) and a one-way position input
(`set_timeline_seconds`) — and knows nothing about the host. Who
the master is, what a frame number means and what the scene's fps is are
all the host's business; this module never writes back towards it, which
is what keeps the sync from turning into a feedback loop.
"""

from .qt_compat import QtCore, QtGui, QtWidgets, Signal, SizePolicy, PYSIDE_VERSION

# Guarded on PYSIDE_VERSION as well as ImportError: importing PySide6 into
# a process already running PySide2 loads a second Qt, which is worse than
# having no player at all.
if PYSIDE_VERSION >= 6:
    try:
        from PySide6.QtMultimedia import (QAudioOutput, QMediaMetaData,
                                          QMediaPlayer)
        from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
        HAVE_QT_MULTIMEDIA = True
    except ImportError:                                   # pragma: no cover
        HAVE_QT_MULTIMEDIA = False
else:                                                     # pragma: no cover
    HAVE_QT_MULTIMEDIA = False


class FitImage(QtWidgets.QWidget):
    """Paints a pixmap scaled `KeepAspectRatio` and centred in its rect."""

    _MIN_HEIGHT = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap = None
        # Ignored lets the widget shrink below the pixmap's width instead
        # of pinning the window open at whatever size the image arrived
        # at (the same QLabel-width trap noted at `_thumb` in
        # capture_section.py); Preferred keeps a sane vertical size.
        self.setSizePolicy(SizePolicy.Ignored, SizePolicy.Preferred)
        self.setMinimumHeight(self._MIN_HEIGHT)

    def set_pixmap(self, pixmap) -> None:
        """Remember the original pixmap and repaint; None/empty clears it."""
        if pixmap is None or pixmap.isNull():
            self._pixmap = None
        else:
            self._pixmap = pixmap
        self.update()

    def clear(self) -> None:
        """Drop the image; the widget paints only its background."""
        self._pixmap = None
        self.update()

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        if self._pixmap is not None:
            scaled = self._pixmap.scaled(
                self.width(), self.height(),
                QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        painter.end()

    def sizeHint(self):
        # Height-for-a-reasonable-width guess based on the pixmap's
        # aspect ratio; never widens the minimum size hint (see setSizePolicy
        # above) since that would reintroduce the QLabel pin-open bug.
        if self._pixmap is None or self._pixmap.isNull():
            return QtCore.QSize(160, self._MIN_HEIGHT)
        w = self._pixmap.width()
        h = self._pixmap.height()
        width = 240
        height = max(self._MIN_HEIGHT, int(width * h / w)) if w else self._MIN_HEIGHT
        return QtCore.QSize(width, height)


class _VideoView(QtWidgets.QGraphicsView):
    """The player's picture page: a `QGraphicsVideoItem` inside a view.

    Deliberately not a `QVideoWidget`. A clip shot on a phone stores a
    landscape picture plus a rotation the viewer is meant to apply, and
    `QVideoWidget` does not apply it — measured on Qt 6.8.3 with a 90°
    clip: the sink reported `videoSize` 1080x1920 and handed out frames
    tagged `Rotation.Clockwise90`, and the widget still painted the man
    lying on his side. `QGraphicsVideoItem` paints through
    `QVideoFrame.paint()`, which honours that tag; the same clip came out
    upright. So there is nothing to set here — in particular the item must
    NOT be given a `setRotation()` of its own, or the two turns would add.

    Keeping the item at the viewport's size with `KeepAspectRatio` is what
    letterboxes rather than crops, which is what `QVideoWidget`'s own
    aspect mode used to do.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # Black behind the picture: a bar beside a clip whose shape does not
        # match the panel should read as a video bar, not as a hole.
        self.setBackgroundBrush(QtGui.QBrush(QtCore.Qt.black))
        self._scene = QtWidgets.QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsVideoItem()
        self._item.setAspectRatioMode(QtCore.Qt.KeepAspectRatio)
        self._scene.addItem(self._item)

    def video_item(self):
        """`setVideoOutput` takes the ITEM, not the view around it."""
        return self._item

    def videoSink(self):
        """Qt-cased: callers hold this the way they held the widget's."""
        return self._item.videoSink()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        size = QtCore.QSizeF(self.viewport().size())
        self._item.setSize(size)
        # The scene is pinned to the viewport: a scene bigger than the view
        # scrolls, and a scrolled preview looks like a cropped clip.
        self._scene.setSceneRect(QtCore.QRectF(QtCore.QPointF(0, 0), size))


class VideoPreview(QtWidgets.QWidget):
    """Local clip playback with transport, scrub bar and a frame caption.

    Two modes share one layout. The native mode plays the local file
    through `QMediaPlayer`; the fallback mode paints JPEG frames the host
    fetched from the service, and the scrub bar then counts frames
    instead of milliseconds. The widget decides which mode it is in — a
    decode failure switches it — and the host only supplies frames when
    asked.

    Audio is muted and stays muted: nobody proofreads motion by ear, and
    a preview that makes noise when a window opens is a bug report.
    """

    #: Fallback mode only: "send me frame N as JPEG bytes". The host must
    #: have the clip uploaded to answer — that is the price of the mode.
    frame_requested = Signal(int)
    #: "Sync with timeline" was switched. The host subscribes to whatever
    #: drives the position while this is on, and unsubscribes when it goes
    #: off; the widget itself only stops accepting `set_timeline_seconds`.
    sync_toggled = Signal(bool)

    #: Floor for the picture area: a panel narrower than ~215 px would
    #: otherwise compute a strip too thin to read as a video at all.
    _MIN_HEIGHT = 120
    #: Ceiling for the picture area. Height follows width, so without one
    #: a 9:16 phone clip in a 700 px panel would ask for 1244 px and push
    #: the window off the bottom of the screen. 640 px still leaves the
    #: transport row and caption visible on a 720p-tall desktop. Past the
    #: ceiling `KeepAspectRatio` puts the bars left and right (nothing is
    #: cropped), which is the honest way to run out of room.
    _MAX_HEIGHT = 640
    #: Used until a clip declares its own size: most footage is 16:9, and
    #: guessing it beats showing a strip at the minimum height.
    _DEFAULT_ASPECT = 16.0 / 9.0
    _NO_CODEC_NOTE = ("this clip does not decode locally — frames come "
                      "from the server")
    _NO_MULTIMEDIA_NOTE = ("QtMultimedia is unavailable here — frames come "
                           "from the server")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path = ""
        self._player = None
        self._audio = None
        self._video = None
        # Clip facts from `upload_info`; None until `set_clip_info`, and
        # then filled in from the player's own metadata where possible.
        self._fps = None
        self._frames = None
        self._duration_s = None
        # Clip shape as width/height. `_info_aspect` is what the service
        # measured and outranks anything the player reports: the player's
        # size arrives late (first decoded frame) and is only consulted
        # when `/info` had no dimensions to give.
        self._aspect = None
        self._info_aspect = None
        # Last height handed to the picture widgets; compared before
        # writing so a relayout triggered by our own setFixedHeight does
        # not bounce back through resizeEvent.
        self._preview_height = None
        # The slider is written from two directions. Without this flag the
        # player's `positionChanged` would call `setValue`, `valueChanged`
        # would read that back as a user seek and call `setPosition`, and
        # playback would stutter against its own scrub bar — the classic
        # seek/update loop. Anything this widget writes into the slider
        # raises the flag; only unflagged changes count as the user's.
        self._syncing = False
        self._fallback = not HAVE_QT_MULTIMEDIA
        self._note = "" if HAVE_QT_MULTIMEDIA else self._NO_MULTIMEDIA_NOTE

        # The picture area is sized from this widget's own width (see
        # `_update_preview_height`), so the whole preview is only ever as
        # tall as its content: a vertically Expanding policy would hand it
        # spare window height that the fixed-height picture cannot use and
        # that would show up as a gap above the transport row.
        self.setSizePolicy(SizePolicy.Preferred, SizePolicy.Fixed)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        # Stacked rather than swapped: the fallback happens mid-playback,
        # inside a signal handler, and re-parenting widgets from there is
        # how layouts end up with dangling children.
        self._image = FitImage()
        # A QStackedWIDGET, not a QStackedLayout. The pages carry an
        # `Ignored` horizontal policy so a 1080p clip cannot pin the panel
        # open, and a bare QStackedLayout holding such pages reports a
        # zero-height geometry to the layout above it — measured: the video
        # page sized itself to 319 px while the stacked layout claimed 0,
        # so the transport row was placed as if the picture took no room
        # and the gap between them was the difference. A widget has its own
        # geometry to fix.
        self._stack = QtWidgets.QStackedWidget()
        self._stack.setSizePolicy(SizePolicy.Ignored, SizePolicy.Fixed)
        self._stack.addWidget(self._image)
        if HAVE_QT_MULTIMEDIA:
            # Letterboxing (and the clip's rotation) live inside this one.
            self._video = _VideoView()
            # Same reasoning as FitImage: a video view reporting the
            # clip's own width would pin the window open at 1080 px.
            self._video.setSizePolicy(SizePolicy.Ignored, SizePolicy.Preferred)
            self._stack.addWidget(self._video)
            self._stack.setCurrentWidget(self._video)
        # No stretch: the picture's height is computed from the width, not
        # taken from whatever the window has left over.
        outer.addWidget(self._stack, 0)

        transport = QtWidgets.QHBoxLayout()
        transport.setSpacing(6)
        # Text-only: the icon set has `play_tri` and `stop_sq` but no pause
        # glyph, and a play triangle above the word "Pause" reads as a bug.
        self._play_btn = QtWidgets.QPushButton("Play")
        self._play_btn.clicked.connect(self._toggle_play)
        self._stop_btn = QtWidgets.QPushButton("Stop")
        self._stop_btn.clicked.connect(self._on_stop)
        transport.addWidget(self._play_btn, 0)
        transport.addWidget(self._stop_btn, 0)
        transport.addStretch(1)
        # A bare QCheckBox rather than the `Check` atom: that one is a
        # labelled column built for a form field, and this belongs on the
        # transport row beside the buttons it turns off. It also has to
        # carry its own enabled state and tooltip, which `Check` keeps for
        # its inner checkbox.
        self._sync = QtWidgets.QCheckBox("Sync with timeline")
        self._sync.toggled.connect(self._on_sync_toggled)
        transport.addWidget(self._sync, 0)
        outer.addLayout(transport)

        self._slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._slider.setRange(0, 0)
        # valueChanged covers every way the value moves — drag, arrow keys,
        # a click on the groove — and the guard flag above keeps the
        # player's own updates out of it. sliderReleased exists only for
        # the fallback, where mid-drag values are deliberately not fetched.
        self._slider.valueChanged.connect(self._on_slider_value)
        self._slider.sliderReleased.connect(self._on_slider_released)
        outer.addWidget(self._slider)

        self._caption = QtWidgets.QLabel("")
        self._caption.setObjectName("field_hint")
        self._caption.setWordWrap(True)
        outer.addWidget(self._caption)

        if HAVE_QT_MULTIMEDIA:
            self._player = QMediaPlayer(self)
            self._audio = QAudioOutput(self)
            self._audio.setMuted(True)
            self._player.setAudioOutput(self._audio)
            self._player.setVideoOutput(self._video.video_item())
            self._player.positionChanged.connect(self._on_position_changed)
            self._player.durationChanged.connect(self._on_duration_changed)
            self._player.playbackStateChanged.connect(self._on_playback_state)
            self._player.mediaStatusChanged.connect(self._on_media_status)
            self._player.errorOccurred.connect(self._on_error)
            # Qt 6's sink reports the decoded frame size, which is the one
            # number that is right even when the container lies. The signal
            # carries no argument — the size is read back from the sink.
            # It belongs to the video widget, not the player, so it
            # survives every `setSource`.
            self._video.videoSink().videoSizeChanged.connect(
                self._on_video_size_changed)

        self._sync_controls()
        self._update_caption()
        self._update_preview_height()

    # -- host API ----------------------------------------------------------

    def set_source(self, path: str) -> None:
        """Point the preview at a LOCAL file; playback starts paused at 0.

        No upload is involved: the clip plays from disk the moment it is
        chosen. A path that will not decode lands in the fallback through
        `errorOccurred` / `InvalidMedia`, asynchronously — this call never
        raises for a missing or malformed file.
        """
        self._path = (path or "").strip()
        if not self._path:
            self.clear()
            return
        self._image.clear()
        # A different file is a different shape; the old one must not
        # decide this clip's height while the new size is on its way.
        self._set_aspect(None, from_info=True)
        if self._player is None:
            self._enter_fallback(self._NO_MULTIMEDIA_NOTE)
            return
        # A new clip earns a fresh attempt at native playback: the last
        # one failing says nothing about this one.
        self._fallback = False
        self._note = ""
        self._stack.setCurrentWidget(self._video)
        self._player.stop()
        self._player.setSource(QtCore.QUrl.fromLocalFile(self._path))
        self._player.setPosition(0)
        self._set_slider(0)
        self._sync_controls()
        self._update_caption()

    def set_clip_info(self, fps=None, frames=None, duration_s=None,
                      width=None, height=None) -> None:
        """Feed in what the service reported for this clip (`upload_info`).

        Drives the frame caption and, in fallback mode, the scrub bar's
        range — which is why arriving clip info re-asks for the current
        frame: it is normally the moment the upload finished, i.e. the
        first moment the host is able to answer at all.

        *width* / *height* are the clip's pixel size, 0 or missing when
        the container does not declare it. They are the early source of
        the aspect ratio: they land before anything has been decoded, so
        the preview is the right shape from the first paint rather than
        snapping to it once playback starts.
        """
        self._fps = float(fps) if fps else None
        self._frames = int(frames) if frames else None
        self._duration_s = float(duration_s) if duration_s else None
        w = int(width or 0)
        h = int(height or 0)
        if w > 0 and h > 0:
            self._set_aspect(w / float(h), from_info=True)
        if self._fallback:
            self._set_slider_range()
            self._sync_controls()
            if self._frames:
                self.frame_requested.emit(int(self._slider.value()))
        self._update_caption()

    def show_frame(self, data) -> None:
        """Display the host's answer to `frame_requested`.

        Accepts JPEG/PNG bytes or a ready `QPixmap`; None (or anything
        that will not decode) leaves the last image up and says so in the
        caption, because a blanked preview looks like a broken widget.
        """
        pixmap = data
        if not isinstance(pixmap, QtGui.QPixmap):
            pixmap = QtGui.QPixmap()
            if data:
                pixmap.loadFromData(data)
        if pixmap.isNull():
            self._caption.setText("That frame did not decode.")
            return
        self._image.set_pixmap(pixmap)
        # The server frame is a scaled copy of the clip, so its shape is
        # the clip's shape — the only aspect source this mode has.
        if pixmap.height() > 0:
            self._set_aspect(pixmap.width() / float(pixmap.height()))
        self._update_caption()

    def pause(self) -> None:
        """Stop decoding without losing the position.

        For the window's `closeEvent`: the singleton hides rather than
        dies, and decoding video into a hidden window is pure waste.
        """
        if self._player is not None:
            self._player.pause()

    def clear(self) -> None:
        """Forget the clip: player stopped, image dropped, controls off."""
        self._path = ""
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QtCore.QUrl())
        self._image.clear()
        self._fps = self._frames = self._duration_s = None
        self._set_aspect(None, from_info=True)
        self._set_slider(0, maximum=0)
        self._sync_controls()
        self._caption.setText("")

    def is_fallback(self) -> bool:
        """True when frames must come from the service (upload required)."""
        return self._fallback

    # -- shape -------------------------------------------------------------

    def _set_aspect(self, aspect, from_info: bool = False) -> None:
        """Record the clip's width/height and re-size the picture area.

        *from_info* marks the service's own measurement, which wins: the
        player's size comes from the demuxer and arrives after the first
        frame, so it is only allowed to fill a gap `/info` left. Passing
        None with *from_info* is how a new clip forgets the old shape.
        """
        if aspect is not None and aspect <= 0:
            aspect = None
        if from_info:
            self._info_aspect = aspect
        elif self._info_aspect:
            return
        if aspect == self._aspect:
            return
        self._aspect = aspect
        self._update_preview_height()

    def _update_preview_height(self) -> None:
        """Height = width / aspect, clamped — so the picture fills the width.

        `KeepAspectRatio` fits the image inside whatever rect it is given,
        which means any rect that is not the clip's own shape shows as
        bars. Giving the picture area exactly the matching height removes
        them, and doing it here rather than through `heightForWidth`
        keeps it independent of whether the parent layout honours that.
        """
        aspect = self._aspect or self._DEFAULT_ASPECT
        height = int(round(max(1, self.width()) / aspect))
        height = max(self._MIN_HEIGHT, min(self._MAX_HEIGHT, height))
        if height == self._preview_height:
            return
        self._preview_height = height
        # The CONTAINER is what the layout above measures; the pages then
        # fill it. Fixing the pages instead leaves the stack free to report
        # something else entirely, which is what opened the gap.
        self._stack.setFixedHeight(height)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Width drives height; the guard in `_update_preview_height` stops
        # the relayout our own setFixedHeight causes from looping back.
        self._update_preview_height()

    def _on_video_size_changed(self, *_args) -> None:
        """The decoder reported a frame size (Qt 6 signal, no argument).

        Already the SHOWN size: the sink reported 1080x1920 for the 90°
        clip whose container stores 1920x1080, so nothing is swapped here.
        """
        if self._video is None:
            return
        size = self._video.videoSink().videoSize()
        if size.width() > 0 and size.height() > 0:
            self._set_aspect(size.width() / float(size.height()))

    def _adopt_player_resolution(self) -> None:
        """Second chance at the shape from the loaded file's metadata.

        Some backends publish `Resolution` on LoadedMedia, before a frame
        has been decoded and therefore before `videoSizeChanged`; others
        publish nothing, hence the guarded read. For a clip that never
        plays before the panel is measured it is the ONLY source: with the
        player paused at frame 0 nothing has been decoded, so the sink's
        size is still (-1, -1) and this is what shapes the picture area.
        """
        if self._player is None:
            return
        try:
            meta = self._player.metaData()
            size = meta.value(QMediaMetaData.Key.Resolution)
            orientation = meta.value(QMediaMetaData.Key.Orientation)
        except Exception:                                  # noqa: BLE001
            return
        if size is None:
            return
        try:
            width, height = int(size.width()), int(size.height())
        except AttributeError:
            return
        # Unlike every other source of the shape, `Resolution` is the size
        # the frames are STORED at — the rotation is a separate key the
        # viewer is meant to apply. The service's dimensions come out of
        # OpenCV, which applies it before measuring, so the swap belongs
        # here and must not be repeated on the `from_info` path.
        width, height = _shown_size(width, height, orientation)
        if width > 0 and height > 0:
            self._set_aspect(width / float(height))

    # -- timeline sync -----------------------------------------------------

    def set_sync_available(self, available: bool, reason: str = "") -> None:
        """Offer or withdraw "Sync with timeline", with a reason when not.

        The host may have no clock to follow — no bridge to the DCC,
        or one that failed to build. Withdrawing switches the sync off
        first (which unsubscribes the host through `sync_toggled`) and then
        greys the box out with *reason* on it: a checkbox that is present
        and explains itself is worth more than one that silently does
        nothing.
        """
        if available:
            self._sync.setEnabled(True)
            self._sync.setToolTip("")
            return
        # setChecked BEFORE disabling: a disabled box still emits `toggled`,
        # and the host must hear the off so it drops its subscription.
        self._sync.setChecked(False)
        self._sync.setEnabled(False)
        self._sync.setToolTip(reason)

    def set_timeline_seconds(self, seconds: float) -> None:
        """Follow the host's clock: show the frame at *seconds* into the clip.

        Time-based on purpose. A capture lands on take frame 0 at the
        clip's own fps, so clip second *t* is take second *t* whatever the
        scene is running at — the host does the frame→seconds conversion
        with the scene's rate and this widget only ever sees seconds.

        Ignored unless the checkbox is on: the host is expected to
        unsubscribe on `sync_toggled(False)`, but a late signal already in
        the queue must not yank a player the user has just taken back.
        Past the end of the clip the position clamps to the last frame —
        a take is usually longer than the clip that seeded it, and the
        preview holding its final frame is the honest answer there.
        """
        if not self._sync.isChecked():
            return
        seconds = max(0.0, float(seconds))
        if self._fallback:
            self._seek_fallback_frame(seconds)
            return
        if self._player is None:
            return
        ms = int(seconds * 1000.0)
        duration = self._player.duration()
        if duration > 0:
            # Not `duration` itself: landing exactly on the end reports
            # EndOfMedia and leaves the widget black. One frame back (or a
            # millisecond, with no fps to go on) is the last picture.
            fps = self._effective_fps()
            step = int(1000.0 / fps) if fps else 1
            ms = min(ms, max(0, duration - max(step, 1)))
        # No guard flag needed on this side: `setPosition` comes back as
        # `positionChanged`, and that handler already writes the slider
        # through `_set_slider`, which raises the flag.
        self._player.setPosition(ms)

    def _seek_fallback_frame(self, seconds: float) -> None:
        """Server-frame half of `set_timeline_seconds`: seconds → frame N."""
        fps = self._effective_fps()
        if not fps:
            return
        frame = int(seconds * fps)
        frames = self._effective_frames()
        if frames:
            frame = min(frame, frames - 1)
        frame = max(0, frame)
        if frame == int(self._slider.value()):
            # The host's clock ticks faster than the clip has frames; a
            # repeat is a round trip for a picture already on screen.
            return
        # Through the guard: an unflagged `setValue` reads back as a user
        # seek and would fetch the same frame a second time.
        self._set_slider(frame)
        self.frame_requested.emit(frame)
        self._update_caption()

    def _on_sync_toggled(self, on: bool) -> None:
        # A slave does not play on its own: whatever was running would
        # fight the host's positions frame for frame.
        if on and self._player is not None:
            self._player.pause()
        self._sync_controls()
        self.sync_toggled.emit(bool(on))

    # -- transport ---------------------------------------------------------

    def _toggle_play(self) -> None:
        if self._player is None:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_stop(self) -> None:
        # Pause-and-rewind rather than QMediaPlayer.stop(): stop() drops
        # the decoded frame and leaves a black rectangle where the first
        # frame of the clip should be.
        if self._player is None:
            return
        self._player.pause()
        self._player.setPosition(0)

    def _on_playback_state(self, state) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play_btn.setText("Pause" if playing else "Play")

    # -- slider ------------------------------------------------------------

    def _set_slider(self, value: int, maximum=None) -> None:
        """Write the slider from this widget — never read back as a seek."""
        self._syncing = True
        if maximum is not None:
            self._slider.setRange(0, max(0, int(maximum)))
        self._slider.setValue(int(value))
        self._syncing = False

    def _set_slider_range(self) -> None:
        if self._fallback:
            frames = self._frames or 0
            self._set_slider(0, maximum=max(frames - 1, 0))
        elif self._player is not None:
            self._set_slider(self._player.position(),
                             maximum=self._player.duration())

    def _on_slider_value(self, value: int) -> None:
        if self._syncing:
            return
        if self._fallback:
            # Mid-drag the number is free and the frame is a round trip,
            # so the fetch waits for the handle to be dropped.
            if self._slider.isSliderDown():
                self._update_caption()
                return
            self.frame_requested.emit(int(value))
            self._update_caption()
            return
        if self._player is not None:
            self._player.setPosition(int(value))

    def _on_slider_released(self) -> None:
        if self._fallback:
            self.frame_requested.emit(int(self._slider.value()))

    # -- player signals ----------------------------------------------------

    def _on_position_changed(self, position: int) -> None:
        if self._slider.isSliderDown():
            return
        self._set_slider(position)
        self._update_caption()

    def _on_duration_changed(self, duration: int) -> None:
        if not self._fallback:
            self._set_slider(self._player.position(), maximum=duration)
        self._sync_controls()
        self._update_caption()

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._enter_fallback(self._NO_CODEC_NOTE)
        elif status == QMediaPlayer.MediaStatus.LoadedMedia:
            self._adopt_player_resolution()
            self._sync_controls()
            self._update_caption()

    def _on_error(self, error, message: str = "") -> None:
        # `message` is taken but not shown: WMF's strings are backend
        # jargon ("Cannot create video renderer") that tells the user
        # nothing they can act on. Every error means the same thing here.
        if error == QMediaPlayer.Error.NoError:
            return
        self._enter_fallback(self._NO_CODEC_NOTE)

    # -- fallback ----------------------------------------------------------

    def _enter_fallback(self, note: str) -> None:
        """Swap to server frames and say why, once per clip."""
        if self._fallback:
            return
        self._fallback = True
        self._note = note
        if self._player is not None:
            self._player.stop()
        self._stack.setCurrentWidget(self._image)
        self._set_slider_range()
        self._sync_controls()
        self._update_caption()
        # Asked unconditionally: the host knows whether the clip is
        # uploaded yet, this widget does not, and an ignored request costs
        # nothing while a missing one leaves the preview empty.
        self.frame_requested.emit(int(self._slider.value()))

    def _sync_controls(self) -> None:
        has_clip = bool(self._path)
        # Following the timeline means the position is not this widget's to
        # set: leaving Play live would have two writers on one player.
        slaved = self._sync.isChecked()
        playable = (has_clip and not self._fallback
                    and self._player is not None and not slaved)
        self._play_btn.setEnabled(playable)
        self._stop_btn.setEnabled(playable)
        if not has_clip or slaved:
            self._slider.setEnabled(False)
        elif self._fallback:
            self._slider.setEnabled((self._frames or 0) > 1)
        else:
            self._slider.setEnabled(self._player.duration() > 0)

    # -- caption -----------------------------------------------------------

    def _effective_fps(self):
        """Clip info first, player metadata second: the service measured
        the file, the player only reports what its demuxer believes."""
        if self._fps:
            return self._fps
        if self._player is None:
            return None
        try:
            value = self._player.metaData().value(
                QMediaMetaData.Key.VideoFrameRate)
        except Exception:                                  # noqa: BLE001
            return None
        return float(value) if value else None

    def _effective_frames(self):
        if self._frames:
            return self._frames
        fps = self._effective_fps()
        seconds = self._duration_s
        if seconds is None and self._player is not None:
            seconds = self._player.duration() / 1000.0
        if fps and seconds:
            return int(round(fps * seconds))
        return None

    def _current_frame(self) -> int:
        if self._fallback:
            return int(self._slider.value())
        fps = self._effective_fps()
        if self._player is None or not fps:
            return 0
        return int(self._player.position() / 1000.0 * fps)

    def _update_caption(self) -> None:
        if not self._path:
            self._caption.setText(self._note)
            return
        frames = self._effective_frames()
        if self._effective_fps() or self._fallback:
            frame = self._current_frame()
            head = f"frame {frame} / {frames}" if frames else f"frame {frame}"
        else:
            # No fps from either source: seconds are the only honest unit.
            head = _clock(self._position_s())
        parts = [head]
        if self._note:
            parts.append(self._note)
        self._caption.setText("  ·  ".join(parts))

    def _position_s(self) -> float:
        if self._player is None:
            return 0.0
        return self._player.position() / 1000.0


def _shown_size(width, height, orientation):
    """The size a clip is SHOWN at, from its stored size and its rotation.

    `QMediaMetaData.Orientation` is the clockwise angle the stored picture
    has to be turned through to be seen the right way up, so a phone clip
    filmed upright reports 1920x1080 with 90 for a picture that is
    1080x1920 on screen (measured on Qt 6.8.3). A quarter turn swaps the
    two; a half turn (180) leaves the shape alone. Negative angles are
    accepted because a backend is free to report -90 for the same turn.

    Anything that is not a whole number of degrees — None from a container
    that declares nothing, or a string from some other backend — means no
    rotation, which is the same answer as 0.
    """
    try:
        quarter = abs(int(orientation)) % 180 == 90
    except (TypeError, ValueError):
        quarter = False
    return (height, width) if quarter else (width, height)


def _clock(seconds: float) -> str:
    """m:ss for a caption — clips are minutes long at most."""
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"
