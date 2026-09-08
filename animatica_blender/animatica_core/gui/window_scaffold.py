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
from .widgets import Btn, Segment


#: Metrics for the scrolling column itself — the padding around the cards and
#: the gap between them. One tuple, not two: the column is as tight as the
#: cards are, in Compact and in Full alike.
_COL_MARGINS = (10, 8, 10, 8)
_COL_SPACING = 6


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

    The column's padding and gap are one size, the tight one, whichever mode
    the window is in.
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
    col.setContentsMargins(*_COL_MARGINS)
    col.setSpacing(_COL_SPACING)
    return scroll, col


def build_header_row(*, title: str, subtitle: str,
                     on_timeline=None,
                     trailing: "QtWidgets.QWidget | None" = None,
                     ) -> QtWidgets.QHBoxLayout:
    """The branded header: mark chip, two-line title, and the Timeline button.

    *on_timeline* is connected to a right-aligned button that re-opens the
    floating Prompt Timeline (the timeline has no in-panel home); pass ``None``
    in a host that has no such window and the button is omitted.

    *trailing* is a host-owned widget (the Compact/Full switch, in practice)
    packed on the right AFTER the stretch and BEFORE the Timeline button, so
    the header reads title … switch, Timeline. A host that passes nothing —
    3ds Max today — gets exactly the row it got before.

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

    if trailing is not None:
        row.addWidget(trailing, 0, QtCore.Qt.AlignVCenter)

    if on_timeline is not None:
        tl_btn = Btn("Timeline", icon="timeline", variant="surface", size="sm")
        tl_btn.setToolTip("Open the floating Prompt Timeline window")
        tl_btn.clicked.connect(on_timeline)
        row.addWidget(tl_btn, 0, QtCore.Qt.AlignVCenter)
    return row


def build_layout_switch(state, on_patch,
                        field: str = "ui_compact") -> Segment:
    """A Compact/Full switch bound to one persisted layout flag on *state*.

    The switch carries no state of its own: it starts where *state* is and
    reports a change as the one patch that flag turns on, leaving the window
    to re-read it and re-apply it.

    *field* names the flag, because there is one per window — ``ui_compact``
    for the tool column, ``ui_compact_settings`` for the Settings window. A
    window shows its own switch once; :func:`sync_layout_switches` is there
    for a host that mirrors one switch in more than one place.
    """
    seg = Segment(
        [("compact", "Compact"), ("full", "Full")],
        value="compact" if getattr(state, field) else "full",
    )
    seg.setToolTip("Compact hides advanced controls without resetting them")
    seg.valueChanged.connect(
        lambda v: on_patch({field: v == "compact"}))
    return seg


def sync_layout_switches(switches, compact: bool) -> None:
    """Point every Compact/Full switch at *compact* without echoing a patch.

    ``Segment.setValue`` moves the checked button and does NOT emit
    ``valueChanged``, which is precisely what this needs: the host calls it
    from the patch the other switch just sent, and an echo would loop.
    """
    value = "compact" if compact else "full"
    for switch in switches:
        switch.setValue(value)


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


def apply_compact(secs: Sections, compact: bool, *,
                  show_live_drive: bool) -> None:
    """Put every card in Compact or Full, and settle Live Drive's visibility.

    The window is never rebuilt for this: each card hides its own advanced
    containers (the policy is ``layout_policy.hidden_parts``) and keeps every
    value it holds, so switching back shows exactly what was there.

    Live Drive is the one card with no interior to fold, so the whole card is
    the unit — it shows only when the host allows it AND the column is Full.

    Settings is skipped: it opens as a window of its own and follows its own
    flag (``ui_compact_settings``, applied by :func:`apply_settings_compact`),
    so folding the column no longer folds the Settings groups.

    Compact is one thing: WHICH controls are on screen. The chrome around them
    — header heights, body padding, the gaps between rows and cards — is one
    size, the tight one, and this function never touches it.

    ``set_compact`` is looked up with ``getattr``: a host vendoring an older
    section set is missing a card's worth of folding, not a working window.
    """
    for section in secs:
        if section is secs.settings:
            continue
        set_compact = getattr(section, "set_compact", None)
        if set_compact is not None:
            set_compact(compact)
    secs.live.setVisible(bool(show_live_drive) and not compact)


def apply_settings_compact(secs: Sections, compact: bool) -> None:
    """Put the Settings card in Compact or Full — its own window, its own flag.

    Separate from :func:`apply_compact` because the two switches are
    independent: the operator folds the column without folding Settings, and
    the other way round. The Settings window's chrome is the same tight chrome
    as the tool column's, in either mode — only the groups fold.
    """
    set_compact = getattr(secs.settings, "set_compact", None)
    if set_compact is not None:
        set_compact(compact)


def pack_sections(col: QtWidgets.QVBoxLayout, secs: Sections, *,
                  show_live_drive: bool,
                  after_skeleton: "QtWidgets.QWidget | None" = None,
                  compact: bool = False) -> None:
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
    # threads, and its shutdown() still runs on window close. *compact* rides
    # along the same path the runtime switch takes, so a window built Compact
    # and a window switched to Compact end up in the same place.
    apply_compact(secs, compact, show_live_drive=show_live_drive)
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
