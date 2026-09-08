"""SubSection — nested collapsible card, smaller chrome than ``CollapsibleSection``.

Like the card it nests in, it has ONE presentation and it is the tight one:
Compact hides controls, it does not restyle them.

This module must not import ``section``, and ``section`` no longer imports this
one either — neither edge of that cycle is needed now that density is fixed.
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


class SubSection(QFrame):
    toggled = Signal(bool)

    def __init__(self, title: str, right: QWidget | None = None,
                 open: bool = True, parent=None):
        """The chrome is tight for everyone: Compact hides controls, it does not
        restyle them (Matt, 2026-09-08). The header keeps a 22px floor from
        ``styles`` — it is this group's toggle.
        """
        super().__init__(parent)
        self.setObjectName("section_frame_sub")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header = QFrame()
        self._header.setObjectName("section_header_sub")
        self._header.setCursor(Qt.PointingHandCursor)
        hdr = QHBoxLayout(self._header)
        hdr.setContentsMargins(8, 4, 8, 4)
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
        self.body_layout.setContentsMargins(10, 6, 10, 8)
        self.body_layout.setSpacing(6)
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
