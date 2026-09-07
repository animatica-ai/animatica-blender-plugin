"""05 · Console — render ``state.logs`` and emit copy/clear actions."""

from datetime import datetime
import html

from ..qt_compat import QtCore, QtGui, QtWidgets, Signal
from ..widgets import CollapsibleSection, Pill, IconBtn
from .. import styles


class ConsoleSection(QtWidgets.QWidget):
    copy_requested = Signal()
    clear_requested = Signal()

    LEVEL_COLOR = {
        "info":  styles.INFO,
        "ok":    styles.SUCCESS,
        "warn":  styles.ACCENT,
        "error": styles.DANGER,
        "debug": styles.TEXT_MUTED,
    }

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)
        self._state = state
        self._on_patch = on_patch

        self._pill = Pill("0 entries", tone="muted")
        self._section = CollapsibleSection(
            "Console", step=5, icon="terminal", right=self._pill,
        )
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)
        body = self._section.body_layout

        self._view = QtWidgets.QTextEdit()
        self._view.setObjectName("status_field")
        self._view.setReadOnly(True)
        self._view.setMinimumHeight(120)
        self._view.setLineWrapMode(QtWidgets.QTextEdit.NoWrap)
        body.addWidget(self._view)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        copy_btn = IconBtn("check", title="Copy")
        copy_btn.clicked.connect(self._on_copy)
        clear_btn = IconBtn("trash", title="Clear", danger=True)
        clear_btn.clicked.connect(self._on_clear)
        row.addWidget(copy_btn)
        row.addWidget(clear_btn)
        body.addLayout(row)

        self.refresh()

    # ----------------------------------------------------------------------
    # API used by the host
    # ----------------------------------------------------------------------

    def append(self, level: str, msg: str) -> None:
        """Append a log line — caller is expected to also mutate ``state.logs``."""
        t = datetime.now().strftime("%H:%M")
        color = self.LEVEL_COLOR.get(level, styles.TEXT_SECONDARY)
        line = (
            f'<div style="margin:0;">'
            f'<span style="color:{styles.TEXT_MUTED};">{t}</span>'
            "&nbsp;&nbsp;"
            f'<span style="color:{color};">[{html.escape(level)}]</span>'
            "&nbsp;"
            f'<span style="color:{styles.TEXT_PRIMARY};">{html.escape(msg)}</span>'
            "</div>"
        )
        cursor = self._view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        cursor.insertHtml(line)
        cursor.insertHtml("<br>")
        self._view.verticalScrollBar().setValue(
            self._view.verticalScrollBar().maximum()
        )
        self._pill.set_text(f"{len(self._state.logs)} entries")

    def refresh(self) -> None:
        self._view.clear()
        for entry in self._state.logs:
            color = self.LEVEL_COLOR.get(entry.level, styles.TEXT_SECONDARY)
            line = (
                f'<div style="margin:0;">'
                f'<span style="color:{styles.TEXT_MUTED};">{html.escape(entry.t)}</span>'
                "&nbsp;&nbsp;"
                f'<span style="color:{color};">[{html.escape(entry.level)}]</span>'
                "&nbsp;"
                f'<span style="color:{styles.TEXT_PRIMARY};">{html.escape(entry.msg)}</span>'
                "</div>"
            )
            cursor = self._view.textCursor()
            cursor.movePosition(QtGui.QTextCursor.End)
            cursor.insertHtml(line)
            cursor.insertHtml("<br>")
        self._pill.set_text(f"{len(self._state.logs)} entries")

    # ----------------------------------------------------------------------

    def _on_copy(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self._view.toPlainText())
        self.copy_requested.emit()

    def _on_clear(self) -> None:
        self._view.clear()
        self._state.logs.clear()
        self._pill.set_text("0 entries")
        self.clear_requested.emit()
