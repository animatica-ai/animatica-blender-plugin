"""Composite widget bundling a PromptTimeline with its controls.

The timeline used to live as a bare widget with its action buttons
(``Add Prompt`` / ``Save`` / ``Load``) sitting in the parent layout. That meant
the buttons were lost when the timeline was reparented into the floating dock.
This container keeps the timeline (left) and its right-side control column
together as a single unit, so reparenting the container moves the buttons
too — satisfying the "controls always attached to the timeline" UX.
"""

from ..qt_compat import QtWidgets, QtCore, QtGui, Signal, SizePolicy
from .. import styles, icons
from ..widgets import TextInput, Btn

# Multi-character compare strips are deferred (single-character v1, Q4).
# Replace MultiSkeletonStrip with a no-op QWidget so the container layout
# code below stays a structural mirror of maya_kimodo's, ready for a
# follow-up slice to drop the real widget back in.
MultiSkeletonStrip = QtWidgets.QWidget


QWidget = QtWidgets.QWidget
QFrame = QtWidgets.QFrame
QHBoxLayout = QtWidgets.QHBoxLayout
QVBoxLayout = QtWidgets.QVBoxLayout
QGridLayout = QtWidgets.QGridLayout
QPushButton = QtWidgets.QPushButton
QComboBox = QtWidgets.QComboBox
QLabel = QtWidgets.QLabel
QSize = QtCore.QSize


# Timeline-header constraint quick-add buttons (one per type). Mirrors the
# vocabulary + per-type colours of gui/sections/constraints_section.py
# (_CTYPE_COLORS) and its IconGrid glyphs (c_fullbody / c_leg / c_arm / c_path);
# Right Leg/Arm reuse the left glyph mirrored.
#   (wire_value, tooltip, glyph, mirror, icon_colour)
_QUICK_CONSTRAINTS = [
    ("fullbody",   "Full Body", "c_fullbody", False, "#A879D0"),
    ("left-foot",  "Left Leg",  "c_leg",      False, "#6FB7FF"),
    ("right-foot", "Right Leg", "c_leg",      True,  "#FF8A8A"),
    ("left-hand",  "Left Arm",  "c_arm",      False, "#3A7BD5"),
    ("right-hand", "Right Arm", "c_arm",      True,  "#D5483A"),
    ("root2d",     "Path",      "c_path",     False, "#E0A24E"),
]


class _ElideLabel(QtWidgets.QLabel):
    """QLabel that elides ("…") instead of expanding at narrow widths.

    QLabel's minimumSizeHint tracks the full text, so a long mode string would
    force the header wide; an explicit minimumWidth lets the layout squeeze
    this label and paintEvent draws the elided form. The full text stays in
    the tooltip.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full = text or ""
        self.setMinimumWidth(40)

    def setText(self, text: str) -> None:
        self._full = text or ""
        self.setToolTip(self._full)
        super().setText(self._full)

    def paintEvent(self, event):
        fm = self.fontMetrics()
        if fm.horizontalAdvance(self._full) <= self.contentsRect().width():
            super().paintEvent(event)
            return
        p = QtGui.QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        elided = fm.elidedText(self._full, QtCore.Qt.ElideRight,
                               self.contentsRect().width())
        p.drawText(self.contentsRect(),
                   int(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter), elided)


def _caption(text: str) -> "QtWidgets.QLabel":
    """Small uppercase section caption, matching the mockup's header labels."""
    lbl = QLabel(text.upper())
    lbl.setStyleSheet(
        f"color: {styles.TEXT_3}; font-size: 10px; font-weight: 600;"
        " letter-spacing: 0.8px;"
    )
    return lbl


class TimelineContainer(QWidget):
    """``PromptTimeline`` + right-aligned controls (Add Prompt / Save / Load).

    The container exposes:
        * ``self.timeline`` — the inner ``PromptTimeline`` (data lives here).
        * ``self.add_block_btn`` — "Add Prompt" button.
        * ``self.save_prompts_btn`` / ``self.load_prompts_btn``.

    The container is the widget hosted by the always-floating timeline dock
    (Phase 3); reparent **the container** (not the inner timeline) so the
    buttons travel with it.
    """

    # Emitted by the in-timeline "Generate" button; the host wires this to the
    # same full-timeline generate path as the main Generate Motion button.
    generate_requested = Signal()
    # Header skeleton selector — mirrors Section 01's picker. The host wires
    # this to the same _on_skeleton_picked handler the section uses and keeps
    # both selectors' selection in lock-step.
    skeleton_picked = Signal(str)
    # Header constraint quick-add — emits the wire type to capture at the
    # current playhead (host routes to _add_constraint_at).
    quick_add_constraint_requested = Signal(str)
    # Header "Convert Animkeys" — routed to the same _on_from_curves handler as
    # the Constraints card's button; converts animation keys to constraints.
    from_curves_requested = Signal()
    # Header namespace field — mirrors Section 01's namespace input; the host is
    # the single writer of state.namespace and keeps both fields in lock-step.
    namespace_changed = Signal(str)
    # Header "Add Skeleton" button — routed to the same _on_create_skeleton
    # handler as Section 01's Create Skeleton.
    create_skeleton_requested = Signal()
    # Header "Fit" button — manual re-sync + zoom reset of the timeline to
    # MoBu's current take range (the range also auto-follows live in the host).
    # Animatica never writes MoBu's range; this is a one-way read.
    fit_to_mobu_requested = Signal()
    # Header pose control — generate a single pose at the playhead from the inline
    # prompt field. The host routes this to the same handler as Section 04's
    # Generate Pose and reflects the text into the side-panel field (one-way).
    generate_pose_with_prompt_requested = Signal(str)

    def __init__(self, timeline, parent=None):
        super().__init__(parent)
        self.timeline = timeline

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Left column: header (skeleton selector + constraint quick-add) on top,
        # compare strips, then the main timeline. The strip shares the donor
        # timeline's X-axis (no left gutter), so vertical stacking in the same
        # column gives pixel-perfect frame alignment.
        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(2)
        left_col.addWidget(self._build_header())
        self.compare_strip = MultiSkeletonStrip(timeline)
        self.compare_strip.setVisible(False)   # hidden until ≥2 skeletons
        left_col.addWidget(self.compare_strip)
        left_col.addWidget(timeline, 1)
        layout.addLayout(left_col, 1)

        self._controls = QWidget()
        self._controls.setSizePolicy(SizePolicy.Preferred, SizePolicy.Preferred)
        cl = QVBoxLayout(self._controls)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)

        # In-timeline Generate — same full-timeline path as the main Generate
        # Motion button; surfaced here so it travels with the (possibly undocked)
        # timeline. ``accent_btn`` objectName picks up the solid stylesheet.
        self.generate_btn = QPushButton("Generate")
        self.generate_btn.setObjectName("accent_btn")
        self.generate_btn.setToolTip("Generate motion from all timeline prompts")
        self._set_btn_icon(self.generate_btn, "spark")
        cl.addWidget(self.generate_btn)

        # Generation status mirror — a user who launches a run from this
        # (floating) timeline sees the same phase/timing text the side panel
        # shows. Indeterminate bar + status label, hidden when idle. Driven by
        # the host via ``set_status`` from the same busy/progress slots.
        self._status_row = QWidget()
        srl = QVBoxLayout(self._status_row)
        srl.setContentsMargins(0, 0, 0, 0)
        srl.setSpacing(2)
        self._status_lbl = QLabel()
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(f"color: {styles.TEXT_2}; font-size: 10px;")
        srl.addWidget(self._status_lbl)
        self._status_bar = QtWidgets.QProgressBar()
        self._status_bar.setObjectName("total_progress")
        self._status_bar.setTextVisible(False)
        self._status_bar.setRange(0, 0)   # indeterminate — the spinner
        self._status_bar.setFixedHeight(6)
        srl.addWidget(self._status_bar)
        # Elapsed-time readout under the bar — mirror of the Generate section's
        # label, ticked by the host's clock during a run.
        self._elapsed_lbl = QLabel()
        self._elapsed_lbl.setStyleSheet(f"color: {styles.TEXT_2}; font-size: 10px;")
        srl.addWidget(self._elapsed_lbl)
        self._status_row.setVisible(False)
        cl.addWidget(self._status_row)

        # Untitled bordered group around the 2x2 button grid.
        group = QFrame()
        group.setObjectName("timeline_btn_group")
        group.setStyleSheet(
            f"QFrame#timeline_btn_group {{"
            f" background-color: rgba(255,255,255,0.02);"
            f" border: 1px solid {styles.BORDER};"
            f" border-radius: 8px;"
            f"}}"
        )
        gl = QGridLayout(group)
        gl.setContentsMargins(8, 8, 8, 8)
        gl.setHorizontalSpacing(6)
        gl.setVerticalSpacing(6)
        gl.setColumnStretch(0, 1)
        gl.setColumnStretch(1, 1)

        self.add_block_btn = QPushButton("Add Prompt")
        self.add_block_btn.setObjectName("ghost_accent_btn")
        self._set_btn_icon(self.add_block_btn, "plus")

        self.save_prompts_btn = QPushButton("Save Prompts")
        self.save_prompts_btn.setObjectName("ghost_accent_btn")
        self._set_btn_icon(self.save_prompts_btn, "save")

        self.load_prompts_btn = QPushButton("Load Prompts")
        self.load_prompts_btn.setObjectName("ghost_accent_btn")
        self._set_btn_icon(self.load_prompts_btn, "open")

        gl.addWidget(self.add_block_btn,    0, 0, 1, 2)
        gl.addWidget(self.save_prompts_btn, 1, 0)
        gl.addWidget(self.load_prompts_btn, 1, 1)

        # Zoom controls row
        zoom_group = QFrame()
        zoom_group.setObjectName("timeline_btn_group")
        zoom_group.setStyleSheet(
            f"QFrame#timeline_btn_group {{"
            f" background-color: rgba(255,255,255,0.02);"
            f" border: 1px solid {styles.BORDER};"
            f" border-radius: 8px;"
            f"}}"
        )
        zl = QGridLayout(zoom_group)
        zl.setContentsMargins(8, 6, 8, 6)
        zl.setHorizontalSpacing(4)
        zl.setVerticalSpacing(4)
        zl.setColumnStretch(0, 1)
        zl.setColumnStretch(1, 1)
        zl.setColumnStretch(2, 1)

        self.zoom_in_btn = QPushButton("+")
        self.zoom_in_btn.setObjectName("ghost_accent_btn")
        self.zoom_in_btn.setToolTip("Zoom in  (Ctrl+Scroll)")

        self.zoom_out_btn = QPushButton("−")
        self.zoom_out_btn.setObjectName("ghost_accent_btn")
        self.zoom_out_btn.setToolTip("Zoom out  (Ctrl+Scroll)")

        self.zoom_fit_btn = QPushButton("Fit")
        self.zoom_fit_btn.setObjectName("ghost_accent_btn")
        self.zoom_fit_btn.setToolTip(
            "Fit: match the timeline length to the scene's current range and "
            "reset zoom to view (never changes the scene)"
        )

        zl.addWidget(self.zoom_in_btn,  0, 0)
        zl.addWidget(self.zoom_out_btn, 0, 1)
        zl.addWidget(self.zoom_fit_btn, 0, 2)

        cl.addWidget(group)
        cl.addWidget(zoom_group)
        cl.addStretch()

        layout.addWidget(self._controls, 0)

        def _add_and_edit():
            b = self.timeline.add_block()
            if b is not None:
                self.timeline._start_inline_edit(b)
        self.add_block_btn.clicked.connect(_add_and_edit)
        self.generate_btn.clicked.connect(self.generate_requested.emit)
        self.zoom_in_btn.clicked.connect(lambda: self.timeline.step_zoom(1.25))
        self.zoom_out_btn.clicked.connect(lambda: self.timeline.step_zoom(0.8))
        # Fit does double duty: sync the timeline length to MoBu's take range
        # (one-way read — see fit_to_mobu_requested) and reset the visual zoom.
        self.zoom_fit_btn.clicked.connect(self.fit_to_mobu_requested.emit)
        self.zoom_fit_btn.clicked.connect(self.timeline.reset_zoom)

        # Relay the timeline's keyboard-shortcut signals (F = fit, Ctrl+G =
        # generate) onto the container's own signals so tool_window's existing
        # wiring (fit_to_mobu_requested / generate_requested) handles them with no
        # extra connections. The timeline already calls reset_zoom() itself for F.
        self.timeline.fit_to_mobu_requested.connect(self.fit_to_mobu_requested.emit)
        self.timeline.generate_requested.connect(self.generate_requested.emit)

    @staticmethod
    def _set_btn_icon(btn, name):
        try:
            btn.setIcon(icons.svg_icon(name, size=13, color=styles.TEXT_SECONDARY))
            btn.setIconSize(QSize(13, 13))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Header: skeleton selector (left) + constraint quick-add (right)
    # ------------------------------------------------------------------

    def _build_header(self) -> "QtWidgets.QWidget":
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)

        # -- Skeleton selector (synced with Section 01's picker) -----------
        h.addWidget(_caption("Skeleton"))
        self.skel_combo = QComboBox()
        self.skel_combo.setMinimumWidth(150)
        self.skel_combo.currentTextChanged.connect(self._on_skel_combo_changed)
        h.addWidget(self.skel_combo)

        # -- Namespace field + Add Skeleton (mirror of Section 01) ----------
        h.addWidget(_caption("Namespace"))
        self.ns_input = TextInput(placeholder="animatica", mono=True)
        self.ns_input.setMinimumWidth(120)
        self.ns_input.textChanged.connect(self.namespace_changed.emit)
        h.addWidget(self.ns_input)
        add_btn = Btn("Add", icon="plus", variant="solid", size="sm")
        add_btn.setToolTip("Create a skeleton under the namespace above")
        add_btn.clicked.connect(self.create_skeleton_requested.emit)
        h.addWidget(add_btn)

        # -- Constraint quick-add (one icon-button per type) ----------------
        # Left-packed (no leading stretch): the constraint group sits next to the
        # skeleton controls; the trailing stretch absorbs the slack.
        h.addSpacing(8)
        h.addWidget(_caption("Constraints"))
        qrow = QHBoxLayout()
        qrow.setContentsMargins(0, 0, 0, 0)
        qrow.setSpacing(5)
        for value, title, glyph, mirror, color in _QUICK_CONSTRAINTS:
            qrow.addWidget(self._make_quick_btn(value, title, glyph, mirror, color))
        h.addLayout(qrow)

        # Convert Animkeys — duplicate of the Constraints card's button, surfaced
        # here so it travels with the floating timeline. Same host handler.
        conv_btn = Btn("Convert Animkeys", icon_ex="c_convert", variant="surface", size="sm")
        conv_btn.setToolTip("Convert the skeleton's animation keys into constraints")
        conv_btn.clicked.connect(self.from_curves_requested.emit)
        h.addSpacing(6)
        h.addWidget(conv_btn)

        # -- Pose control (inline prompt + Pose button) ---------------------
        # Single pose at the playhead, reusing the Section 04 pose pipeline. Sits
        # just after the constraint quick-add with a small gap; the host reflects
        # the prompt into the side-panel field and delegates to _on_generate_pose.
        h.addSpacing(14)
        h.addWidget(_caption("Pose"))
        self.pose_input = TextInput(
            placeholder="e.g. A person in a guarded fighting stance.")
        self.pose_input.setMinimumWidth(180)
        h.addWidget(self.pose_input)
        pose_btn = Btn("Pose", icon="wand", variant="soft", size="sm")
        pose_btn.setToolTip("Generate a single pose at the playhead from this prompt")
        pose_btn.clicked.connect(
            lambda: self.generate_pose_with_prompt_requested.emit(self.pose_input.text())
        )
        h.addWidget(pose_btn)

        # -- Generation-mode display (read-only, host-pushed) ---------------
        # Shows where Generate will write (Story / New Take / Existing
        # Take: …) without opening the Generate section. Elides at narrow
        # widths; the host pushes updates via set_mode_text.
        h.addSpacing(8)
        h.addWidget(_caption("Mode"))
        self.mode_lbl = _ElideLabel("")
        self.mode_lbl.setStyleSheet(f"color: {styles.TEXT_2}; font-size: 11px;")
        self.mode_lbl.setMaximumWidth(220)
        h.addWidget(self.mode_lbl)

        h.addStretch(1)
        return bar

    def _make_quick_btn(self, value, title, glyph, mirror, color):
        """A 28x26 ghost icon-button that quick-adds *value* at the playhead."""
        b = QPushButton()
        b.setObjectName("con_quick_btn")
        b.setToolTip(title)
        b.setFixedSize(28, 26)
        b.setCursor(QtCore.Qt.PointingHandCursor)
        try:
            b.setIcon(icons.svg_icon_ex(glyph, size=14, color=color, mirror=mirror))
            b.setIconSize(QSize(14, 14))
        except Exception:
            pass
        b.setStyleSheet(
            "QPushButton#con_quick_btn {"
            " background: transparent;"
            f" border: 1px solid {styles.BORDER};"
            " border-radius: 5px; padding: 0;"
            "}"
            "QPushButton#con_quick_btn:hover {"
            f" border-color: {styles.TEXT_3};"
            " background: rgba(255,255,255,0.04);"
            "}"
        )
        b.clicked.connect(
            lambda _=False, v=value: self.quick_add_constraint_requested.emit(v)
        )
        return b

    # ------------------------------------------------------------------
    # Generation-mode display API (host-pushed, display-only)
    # ------------------------------------------------------------------

    def set_mode_text(self, text: str) -> None:
        """Reflect the active generation mode in the header label."""
        self.mode_lbl.setText(text or "")

    # ------------------------------------------------------------------
    # Pose-control API (mirror of the namespace setter, signal-guarded)
    # ------------------------------------------------------------------

    def set_pose_prompt(self, text: str) -> None:
        """Reflect *text* in the pose field without re-emitting."""
        self.pose_input.blockSignals(True)
        self.pose_input.setText(text or "")
        self.pose_input.blockSignals(False)

    # ------------------------------------------------------------------
    # Generation-status mirror (driven from the host's busy/progress slots)
    # ------------------------------------------------------------------

    def set_status(self, msg: str, busy: bool) -> None:
        """Mirror the side-panel generation status onto the floating timeline.

        Purely visual — shows/hides the spinner row and updates the phase text.
        No effect on timeline generation itself.
        """
        # Arming transition only (explicit-hidden flag, not ancestor-dependent
        # isVisible) — progress updates mid-run must not blank the clock.
        if busy and self._status_row.isHidden():
            self._elapsed_lbl.setText("")
        self._status_row.setVisible(busy)
        self._status_lbl.setText(msg or "" if busy else "")

    def set_elapsed(self, text: str) -> None:
        """Update the elapsed-time readout under the mirror spinner."""
        self._elapsed_lbl.setText(text or "")

    # ------------------------------------------------------------------
    # Skeleton-selector API (mirror of SkeletonSection, signal-guarded)
    # ------------------------------------------------------------------

    def set_skeleton_choices(self, names, prefer=None) -> None:
        """Repopulate the header combo; preserve (or *prefer*) the selection."""
        names = list(names)
        prev = self.skel_combo.currentText()
        self.skel_combo.blockSignals(True)
        self.skel_combo.clear()
        self.skel_combo.addItems(names)        # only real skeletons; no blank row
        target = prefer or prev
        if target and target in names:
            self.skel_combo.setCurrentText(target)
        else:
            self.skel_combo.setCurrentIndex(-1)   # show no selection, unpickable
        self.skel_combo.blockSignals(False)

    def set_selected_skeleton(self, name: str) -> None:
        """Reflect *name* as the current selection without re-emitting."""
        self.skel_combo.blockSignals(True)
        if not name:
            self.skel_combo.setCurrentIndex(-1)   # clear; setCurrentText("") no-ops
        else:
            if self.skel_combo.findText(name) < 0:
                self.skel_combo.addItem(name)     # keep the active rig visible
            self.skel_combo.setCurrentText(name)
        self.skel_combo.blockSignals(False)

    def set_namespace(self, text: str) -> None:
        """Reflect *text* in the namespace field without re-emitting."""
        self.ns_input.blockSignals(True)
        self.ns_input.setText(text or "")
        self.ns_input.blockSignals(False)

    def _on_skel_combo_changed(self, value: str) -> None:
        if not value:
            return
        self.skeleton_picked.emit(value)
