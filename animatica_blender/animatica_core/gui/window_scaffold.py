"""The parts a host's tool window is assembled from — chrome, sections, surfacing.

The Animatica tool window is one scrolling column: a small branded header row on
top, then the numbered workflow cards, then a stretch that keeps everything
top-aligned. None of that shape is host-specific — only *which* extra widgets a
host slots in between, and where the window is docked.

So the shape lives here as four small builders. A host window calls them in
order from its ``_build_ui`` and spends its own lines on the parts that really
are its own: the timeline dock, the scene-time bridge, the host-specific
signals.

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

from typing import NamedTuple

from . import icons, styles
from .qt_compat import QtCore, QtWidgets
from .sections import (
    SettingsSection, SkeletonSection, ConstraintsSection,
    GenerateSection, PoseSection, LiveSection, ModelSection,
)
from .widgets import Btn


class Sections(NamedTuple):
    """The seven cards a tool window always owns, in construction order.

    ``settings`` and ``live`` are built even where they are not shown inline
    (Settings opens in its own window; Live Drive is hidden behind a toggle) —
    they still share the window's ``AppState``, and Live Drive owns threads
    whose ``shutdown()`` must run on window close.
    """
    settings: SettingsSection
    model: ModelSection
    skeleton: SkeletonSection
    constraints: ConstraintsSection
    generate: GenerateSection
    pose: PoseSection
    live: LiveSection


def build_scroll_column(window) -> tuple[QtWidgets.QScrollArea, QtWidgets.QVBoxLayout]:
    """Give *window* a full-bleed vertical scroll area; return it and its column.

    The window itself has no margins — the padding lives on the inner column, so
    the scrollbar sits flush against the panel edge. Horizontal scrolling is off:
    every card is expected to wrap rather than push the column wider.
    """
    outer = QtWidgets.QVBoxLayout(window)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)

    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    outer.addWidget(scroll)

    content = QtWidgets.QWidget()
    scroll.setWidget(content)
    col = QtWidgets.QVBoxLayout(content)
    col.setContentsMargins(12, 12, 12, 12)
    col.setSpacing(8)
    return scroll, col


def build_header_row(*, title: str, subtitle: str,
                     on_timeline=None) -> QtWidgets.QHBoxLayout:
    """The branded header: mark chip, two-line title, and the Timeline button.

    *on_timeline* is connected to a right-aligned button that re-opens the
    floating Prompt Timeline (the timeline has no in-panel home); pass ``None``
    in a host that has no such window and the button is omitted.

    Status pills deliberately do not live here — skeleton readiness is already
    shown by the Skeleton card's own pill and FPS lives in the host's transport,
    so duplicating either only added noise.
    """
    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 8)
    row.setSpacing(10)

    chip = QtWidgets.QFrame()
    chip.setObjectName("header_icon_chip")
    cl = QtWidgets.QHBoxLayout(chip)
    cl.setContentsMargins(0, 0, 0, 0)
    mark = QtWidgets.QLabel()
    mark.setPixmap(icons.header_mark_pixmap(color=styles.ACCENT, size=16))
    mark.setAlignment(QtCore.Qt.AlignCenter)
    cl.addWidget(mark)
    row.addWidget(chip, 0, QtCore.Qt.AlignVCenter)

    title_col = QtWidgets.QVBoxLayout()
    title_col.setSpacing(2)
    title_lbl = QtWidgets.QLabel(title)
    title_lbl.setObjectName("header_title")
    title_col.addWidget(title_lbl)
    sub_lbl = QtWidgets.QLabel(subtitle)
    sub_lbl.setObjectName("header_subtitle")
    title_col.addWidget(sub_lbl)
    row.addLayout(title_col, 1)
    row.addStretch(1)

    if on_timeline is not None:
        tl_btn = Btn("Timeline", icon="timeline", variant="surface", size="sm")
        tl_btn.setToolTip("Open the floating Prompt Timeline window")
        tl_btn.clicked.connect(on_timeline)
        row.addWidget(tl_btn, 0, QtCore.Qt.AlignVCenter)
    return row


def build_sections(state, on_patch, log) -> Sections:
    """Construct the seven workflow cards over one shared ``AppState``.

    Every card reads *state* read-only and emits its changes as a patch dict
    through *on_patch*; only Live Drive also needs a console, because it logs
    from its own threads.
    """
    return Sections(
        settings=SettingsSection(state, on_patch),
        model=ModelSection(state, on_patch),
        skeleton=SkeletonSection(state, on_patch),
        constraints=ConstraintsSection(state, on_patch),
        generate=GenerateSection(state, on_patch),
        pose=PoseSection(state, on_patch),
        live=LiveSection(state, on_patch, log=log),
    )


def pack_sections(col: QtWidgets.QVBoxLayout, secs: Sections, *,
                  show_live_drive: bool,
                  after_skeleton: "QtWidgets.QWidget | None" = None) -> None:
    """Lay the cards into *col* in workflow order, then absorb the slack.

    Settings is not a card in this column — it opens as a window of its own.
    Motion Import differs BY HOST: MotionBuilder moved it to its own window,
    3ds Max still shows it as an inline card between Skeleton and Generate —
    that is what *after_skeleton* is for. A host-owned widget passed there is
    packed in that slot; visibility stays the caller's business.

    *show_live_drive* is likewise the CALLER'S verdict, not a raw preference:
    a host without the LIVE_DRIVE capability passes the conjunction
    ``host.has(LIVE_DRIVE) and preference`` — every button in a Live card
    without the capability dead-ends, whatever the user toggled.

    Constraints is nested INSIDE the Generate card (collapsed by default):
    pins steer the generation request, so they belong with it rather than
    with the timeline.

    The trailing stretch is what keeps every card at its natural (collapsed =
    header-only) height: the extra viewport height collects at the bottom
    instead of leaving a gap mid-column.
    """
    col.addWidget(secs.model)
    col.addWidget(secs.skeleton)
    if after_skeleton is not None:
        col.addWidget(after_skeleton)
    col.addWidget(secs.generate)
    secs.generate.add_body_widget(secs.constraints)
    col.addWidget(secs.pose)
    col.addWidget(secs.live)
    # Live Drive is hidden by default; the Settings "Show Live Drive" toggle
    # reveals it. Hidden rather than removed: the section keeps owning its
    # threads, and its shutdown() still runs on window close.
    secs.live.setVisible(bool(show_live_drive))
    col.addStretch(1)


def bring_window_to_front(win) -> None:
    """Force a top-level window above the host, defeating Win32 foreground-lock.

    ``raise_()`` / ``activateWindow()`` are advisory: after a few open/close
    cycles Windows denies the foreground change and the window restacks behind
    the host (``isVisible()`` stays True but it's occluded — the "can't reopen"
    symptom). ``AttachThreadInput`` briefly shares input state with the current
    foreground thread so ``SetForegroundWindow`` is honoured. Pure-Qt first,
    then the Win32 kicker (no native-window recreation, fully guarded, and a
    no-op anywhere that is not Windows).
    """
    win.setWindowState(win.windowState() & ~QtCore.Qt.WindowMinimized)
    win.show()
    win.raise_()
    win.activateWindow()
    wh = win.windowHandle()
    if wh is not None:
        wh.requestActivate()
    try:  # Windows-only kicker
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = int(win.winId())
        fg = u32.GetForegroundWindow()
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        fg_thread = u32.GetWindowThreadProcessId(fg, None)
        u32.AttachThreadInput(fg_thread, cur, True)
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        u32.AttachThreadInput(fg_thread, cur, False)
    except Exception:
        pass  # never let surfacing break the open path
