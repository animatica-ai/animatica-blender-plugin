"""CollapsibleSection — numbered card matching the JSX ``<Section>``.

Header layout: chevron + zero-padded step (``01``) + icon + title + right
widget. Click header to toggle. The body is a QWidget; consumers add
children via ``section.body_layout``.
"""

from __future__ import annotations

from ..qt_compat import QtCore, QtWidgets, Signal
from .. import styles, icons


Qt = QtCore.Qt
QWidget = QtWidgets.QWidget
QFrame = QtWidgets.QFrame
QLabel = QtWidgets.QLabel
QVBoxLayout = QtWidgets.QVBoxLayout
QHBoxLayout = QtWidgets.QHBoxLayout


class CollapsibleSection(QFrame):
    toggled = Signal(bool)  # True = open

    def __init__(self, title: str, step: int | None = None, icon: str | None = None,
                 right: QWidget | None = None, open: bool = True,
                 icon_ex: str | None = None, accent: bool = False, parent=None):
        """``icon_ex`` selects a full-SVG glyph from ``icons.ICON_SVG`` (rendered
        inside a 26px badge); the legacy ``icon`` names an ``ICON_PATHS`` glyph.
        ``accent`` tints the badge with the ember accent (primary groups) instead
        of the neutral grey used by secondary groups.
        """
        super().__init__(parent)
        self.setObjectName("section_frame")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- header --------------------------------------------------------
        self._header = QFrame()
        self._header.setObjectName("section_header")
        self._header.setCursor(Qt.PointingHandCursor)
        hdr = QHBoxLayout(self._header)
        hdr.setContentsMargins(12, 9, 12, 9)
        hdr.setSpacing(8)

        self._chevron = QLabel()
        self._chevron.setFixedSize(12, 12)
        hdr.addWidget(self._chevron, 0, Qt.AlignVCenter)

        if step is not None:
            step_lbl = QLabel(f"{step:02d}")
            step_lbl.setObjectName("section_step")
            hdr.addWidget(step_lbl, 0, Qt.AlignVCenter)

        if icon is not None or icon_ex is not None:
            ico_color = styles.ACCENT if accent else styles.TEXT_SECONDARY
            if icon_ex is not None:
                pm = icons.svg_pixmap_ex(icon_ex, size=15, color=ico_color)
            else:
                pm = icons.svg_pixmap(icon, size=14, color=ico_color)
            badge = QFrame()
            badge.setObjectName("section_icon_chip")
            badge.setProperty("tone", "accent" if accent else "neutral")
            badge.setFixedSize(26, 26)
            bl = QHBoxLayout(badge)
            bl.setContentsMargins(0, 0, 0, 0)
            ico_lbl = QLabel()
            ico_lbl.setPixmap(pm)
            ico_lbl.setAlignment(Qt.AlignCenter)
            bl.addWidget(ico_lbl)
            hdr.addWidget(badge, 0, Qt.AlignVCenter)

        title_lbl = QLabel(title.upper())
        title_lbl.setObjectName("section_title")
        hdr.addWidget(title_lbl)
        hdr.addStretch(1)
        if right is not None:
            hdr.addWidget(right, 0, Qt.AlignVCenter)
        outer.addWidget(self._header)

        # --- body ----------------------------------------------------------
        self._body = QWidget()
        self._body.setObjectName("section_body")
        self.body_layout = QVBoxLayout(self._body)
        self.body_layout.setContentsMargins(12, 11, 12, 11)
        self.body_layout.setSpacing(8)
        outer.addWidget(self._body)

        self._open = open
        self._apply_open()
        self._header.mousePressEvent = lambda _ev: self.set_open(not self._open)

    def set_open(self, open: bool) -> None:
        if open == self._open:
            return
        self._open = open
        self._apply_open()
        self.toggled.emit(open)

    def is_open(self) -> bool:
        return self._open

    def _apply_open(self) -> None:
        self._body.setVisible(self._open)
        glyph = "chevronDown" if self._open else "chevronRight"
        self._chevron.setPixmap(
            icons.svg_pixmap(glyph, size=12, color=styles.TEXT_SECONDARY)
        )
