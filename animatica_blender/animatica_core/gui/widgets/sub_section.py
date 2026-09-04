"""SubSection — nested collapsible card, smaller chrome than ``CollapsibleSection``."""

from __future__ import annotations

from ..qt_compat import QtCore, QtWidgets, Signal
from .. import styles, icons


Qt = QtCore.Qt
QWidget = QtWidgets.QWidget
QFrame = QtWidgets.QFrame
QLabel = QtWidgets.QLabel
QVBoxLayout = QtWidgets.QVBoxLayout
QHBoxLayout = QtWidgets.QHBoxLayout


class SubSection(QFrame):
    toggled = Signal(bool)

    def __init__(self, title: str, right: QWidget | None = None,
                 open: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("section_frame_sub")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header = QFrame()
        self._header.setObjectName("section_header_sub")
        self._header.setCursor(Qt.PointingHandCursor)
        hdr = QHBoxLayout(self._header)
        hdr.setContentsMargins(10, 7, 10, 7)
        hdr.setSpacing(8)

        self._chevron = QLabel()
        self._chevron.setFixedSize(11, 11)
        hdr.addWidget(self._chevron, 0, Qt.AlignVCenter)

        title_lbl = QLabel(title.upper())
        title_lbl.setObjectName("section_title_sub")
        hdr.addWidget(title_lbl)
        hdr.addStretch(1)
        if right is not None:
            hdr.addWidget(right, 0, Qt.AlignVCenter)
        outer.addWidget(self._header)

        self._body = QWidget()
        self.body_layout = QVBoxLayout(self._body)
        self.body_layout.setContentsMargins(12, 10, 12, 12)
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
            icons.svg_pixmap(glyph, size=11, color=styles.TEXT_SECONDARY)
        )
