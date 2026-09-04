"""Atomic widgets mirroring the Kimodo-to-Maya JSX components.

Each widget here corresponds 1:1 to a component in
``doc/design/Kimodo to Maya.html`` (Pill, Field, Btn, IconBtn, Segment,
Check, Toggle, NumberInput, TextInput). They consume styles from
``gui.styles`` via Qt object names and dynamic properties.
"""

from __future__ import annotations

from ..qt_compat import QtCore, QtGui, QtWidgets, Signal, SizePolicy
from .. import styles, icons


Qt = QtCore.Qt
QWidget = QtWidgets.QWidget
QLabel = QtWidgets.QLabel
QPushButton = QtWidgets.QPushButton
QHBoxLayout = QtWidgets.QHBoxLayout
QVBoxLayout = QtWidgets.QVBoxLayout
QLineEdit = QtWidgets.QLineEdit
QCheckBox = QtWidgets.QCheckBox
QFrame = QtWidgets.QFrame
QSpinBox = QtWidgets.QSpinBox
QDoubleSpinBox = QtWidgets.QDoubleSpinBox
QComboBox = QtWidgets.QComboBox


# ---------------------------------------------------------------------------
# Pill — status chip with tone {neutral, success, info, danger, accent, muted}
# ---------------------------------------------------------------------------

class Pill(QLabel):
    """Compact status chip. Tone changes via dynamic property."""

    def __init__(self, text: str = "", tone: str = "neutral", dot: bool = False, parent=None):
        super().__init__(parent)
        self._dot = dot
        self._tone = tone
        self.setProperty("class", "pill")
        self.setObjectName("pill")
        self.setProperty("tone", tone)
        self.setAlignment(Qt.AlignCenter)
        self.set_text(text)

    def set_text(self, text: str) -> None:
        prefix = "● " if self._dot else ""
        super().setText(f"{prefix}{text}")

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)


# ---------------------------------------------------------------------------
# Field — labelled wrapper around an editor widget (+ optional hint line)
# ---------------------------------------------------------------------------

class Field(QWidget):
    def __init__(self, label: str = "", widget: QWidget | None = None,
                 hint: str = "", right: QWidget | None = None, parent=None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        if label or right is not None:
            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(6)
            if label:
                lbl = QLabel(label)
                lbl.setObjectName("field_label")
                head.addWidget(lbl)
            head.addStretch(1)
            if right is not None:
                head.addWidget(right)
            col.addLayout(head)
        if widget is not None:
            col.addWidget(widget)
        if hint:
            hint_lbl = QLabel(hint)
            hint_lbl.setObjectName("field_hint")
            # Without this a hint is one unbreakable line, and its width
            # becomes the card's minimum width -- the panel then refuses to
            # narrow and its right-hand controls fall outside the viewport.
            # Sections that wrote their own hint labels already set this;
            # Field is where it belongs.
            hint_lbl.setWordWrap(True)
            col.addWidget(hint_lbl)


# ---------------------------------------------------------------------------
# Btn — variant in {solid, surface, soft, ghost, danger}
# ---------------------------------------------------------------------------

_VARIANT_OBJECT_NAME = {
    "solid":   "accent_btn",
    "surface": "",
    "soft":    "soft_btn",
    "ghost":   "ghost_accent_btn",
    "danger":  "danger_btn",
}


class Btn(QPushButton):
    def __init__(self, text: str = "", icon: str | None = None,
                 variant: str = "surface", danger: bool = False,
                 size: str = "md", icon_ex: str | None = None, parent=None):
        """``icon_ex`` selects a full-SVG glyph from ``icons.ICON_SVG`` (custom
        viewBox / fill); the legacy ``icon`` names a single-path ICON_PATHS glyph.
        """
        super().__init__(text, parent)
        if danger:
            variant = "danger"
        self._variant = variant
        self.setObjectName(_VARIANT_OBJECT_NAME.get(variant, ""))
        if icon or icon_ex:
            color = self._icon_color()
            if icon_ex:
                self.setIcon(icons.svg_icon_ex(icon_ex, size=13, color=color))
            else:
                self.setIcon(icons.svg_icon(icon, size=13, color=color))
            self.setIconSize(QtCore.QSize(13, 13))
        if size == "sm":
            self.setStyleSheet("padding: 0 8px; min-height: 24px; font-size: 11px;")
        elif size == "lg":
            self.setStyleSheet("padding: 0 18px; min-height: 36px; font-size: 13px;")

    def _icon_color(self) -> str:
        return (styles.ON_ACCENT if self._variant == "solid"
                else styles.TEXT_SECONDARY)

    def set_icon_ex(self, name: str) -> None:
        """Swap the ``ICON_SVG`` glyph, keeping the variant's icon colour.

        For buttons whose glyph tracks their state (Live Drive's Play
        turning into Stop) — the colour rule stays here rather than being
        restated at every call site.
        """
        self.setIcon(icons.svg_icon_ex(name, size=13, color=self._icon_color()))
        self.setIconSize(QtCore.QSize(13, 13))

    def set_variant(self, variant: str) -> None:
        """Switch the button's visual variant by re-applying its objectName.

        The stylesheet keys off objectName (e.g. ``QPushButton#accent_btn``),
        so changing variant means re-setting the objectName and re-polishing.
        """
        self._variant = variant
        self.setObjectName(_VARIANT_OBJECT_NAME.get(variant, ""))
        self.style().unpolish(self)
        self.style().polish(self)


def reset_button(tooltip: str = "Restore this group's defaults") -> "Btn":
    """Small ghost 'Reset' button for a section/sub-section header ``right=`` slot.

    Returned unconnected — the caller wires ``.clicked`` to its per-group reset
    handler. As a ``QPushButton`` it consumes its own press, so a click never
    reaches the header frame's toggle handler (``SubSection``/``CollapsibleSection``
    monkeypatch ``mousePressEvent`` on the *frame*, not its children).
    """
    btn = Btn("Reset", variant="ghost", size="sm")
    btn.setToolTip(tooltip)
    return btn


# ---------------------------------------------------------------------------
# IconBtn — icon-only ghost button with optional danger tint
# ---------------------------------------------------------------------------

class IconBtn(QPushButton):
    def __init__(self, icon: str, title: str = "", danger: bool = False,
                 size: int = 14, parent=None):
        super().__init__(parent)
        self.setObjectName("ibtn")
        self.setProperty("danger", "true" if danger else "false")
        self.setToolTip(title)
        color = styles.DANGER if danger else styles.TEXT_SECONDARY
        self.setIcon(icons.svg_icon(icon, size=size, color=color))
        self.setIconSize(QtCore.QSize(size, size))
        self.setFixedSize(size + 10, size + 10)
        self.setCursor(Qt.PointingHandCursor)


# ---------------------------------------------------------------------------
# TextInput — QLineEdit with optional mono font flag
# ---------------------------------------------------------------------------

class TextInput(QLineEdit):
    def __init__(self, value: str = "", placeholder: str = "",
                 mono: bool = False, parent=None):
        super().__init__(value, parent)
        if placeholder:
            self.setPlaceholderText(placeholder)
        if mono:
            self.setProperty("mono", "true")


# ---------------------------------------------------------------------------
# NumberInput — QSpinBox/QDoubleSpinBox with mono flag
# ---------------------------------------------------------------------------

class NumberInput(QWidget):
    """Thin wrapper that exposes ``value``, ``setValue``, ``valueChanged``.

    Wraps a QSpinBox (int) or QDoubleSpinBox (float) depending on ``step``.
    """

    valueChanged = Signal(object)

    def __init__(self, value=0, minimum=-10**6, maximum=10**6, step=1,
                 mono: bool = True, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        if isinstance(step, float) or isinstance(value, float):
            spin = QDoubleSpinBox()
            spin.setDecimals(2)
        else:
            spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setValue(value)
        if mono:
            spin.setProperty("mono", "true")
        spin.valueChanged.connect(lambda v: self.valueChanged.emit(v))
        row.addWidget(spin)
        self._spin = spin

    def value(self):
        return self._spin.value()

    def setValue(self, v):
        self._spin.setValue(v)


# ---------------------------------------------------------------------------
# Check — labelled checkbox with optional sublabel
# ---------------------------------------------------------------------------

class Check(QWidget):
    toggled = Signal(bool)

    def __init__(self, label: str, checked: bool = False,
                 sublabel: str = "", parent=None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        self._cb = QCheckBox(label)
        self._cb.setChecked(checked)
        self._cb.toggled.connect(self.toggled.emit)
        col.addWidget(self._cb)
        if sublabel:
            sub = QLabel(sublabel)
            sub.setObjectName("field_hint")
            sub.setContentsMargins(22, 0, 0, 0)
            # Wrap long hints (e.g. Match scene FPS) so the sublabel's preferred
            # width can't stretch the whole tool window — minimumSizeHint then
            # tracks the longest word, not the full sentence.
            sub.setWordWrap(True)
            col.addWidget(sub)

    def isChecked(self) -> bool:
        return self._cb.isChecked()

    def setChecked(self, v: bool) -> None:
        self._cb.setChecked(v)


# ---------------------------------------------------------------------------
# Toggle — pill switch styled via objectName=toggle
# ---------------------------------------------------------------------------

class Toggle(QCheckBox):
    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("toggle")
        self.setChecked(checked)


# ---------------------------------------------------------------------------
# Segment — exclusive button group laid out as a segmented control
# ---------------------------------------------------------------------------

class Segment(QFrame):
    """Exclusive segmented control. ``options`` is a list of (value, label)
    tuples or plain strings. Emits ``valueChanged(str)``.
    """

    valueChanged = Signal(str)

    def __init__(self, options, value: str | None = None, colors=None, parent=None):
        super().__init__(parent)
        self.setObjectName("seg")
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(2)
        # No QButtonGroup: with checkable QPushButtons + an exclusive group
        # the per-button clicked() signal occasionally fails to dispatch on
        # PySide6 builds shipped with older MoBu versions (observed in MoBu
        # 2024). Manage exclusivity manually so a single click reliably
        # both updates the visual state and emits valueChanged.
        # ``colors`` (optional ``{value: hex}``) tags each button with a small
        # color swatch icon. Icons render independently of the ``#seg_btn``
        # stylesheet, so the per-type color does not fight the :checked theming
        # (the setProperty("variant", ...) footgun noted in CLAUDE.md).
        colors = colors or {}
        self._buttons = {}
        self._current = None
        for opt in options:
            v, label = (opt, opt) if isinstance(opt, str) else (opt[0], opt[1])
            btn = QPushButton(label)
            btn.setObjectName("seg_btn")
            btn.setCheckable(True)
            # Stash the segment value on the button so the shared click
            # handler can look it up without closure capture pitfalls.
            btn.setProperty("seg_value", v)
            swatch = colors.get(v)
            if swatch:
                btn.setIcon(self._swatch_icon(swatch))
                btn.setIconSize(QtCore.QSize(9, 9))
            btn.clicked.connect(self._on_button_clicked)
            row.addWidget(btn)
            self._buttons[v] = btn
        if value is None and self._buttons:
            value = next(iter(self._buttons))
        if value is not None:
            self.setValue(value)

    @staticmethod
    def _swatch_icon(hex_color: str):
        """Return a small rounded color-square QIcon for a segment button."""
        size = 12
        pm = QtGui.QPixmap(size, size)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(hex_color))
        p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
        p.end()
        return QtGui.QIcon(pm)

    def _on_button_clicked(self) -> None:
        btn = self.sender()
        if btn is None:
            return
        v = btn.property("seg_value")
        if v is None:
            return
        self._select(v)

    def _select(self, v: str) -> None:
        if v not in self._buttons:
            return
        # Enforce exclusivity by hand: uncheck every other button, check
        # the target. Reusing the setChecked path avoids a QButtonGroup
        # signal-routing quirk in older PySide6.
        for vv, btn in self._buttons.items():
            btn.setChecked(vv == v)
        self._current = v
        self.valueChanged.emit(v)

    def value(self) -> str | None:
        if self._current is not None:
            return self._current
        for v, btn in self._buttons.items():
            if btn.isChecked():
                return v
        return None

    def setValue(self, v: str) -> None:
        if v not in self._buttons:
            return
        for vv, btn in self._buttons.items():
            btn.setChecked(vv == v)
        self._current = v


# ---------------------------------------------------------------------------
# IconGrid — exclusive icon+label grid selector (constraint-type picker).
# Emits valueChanged(str). Each option is (value, label, icon_name, mirror);
# the per-value colour tints the icon always and the chip when selected.
# ---------------------------------------------------------------------------

class IconGrid(QWidget):
    """Grid of icon+label chips, one selectable at a time.

    Layout matches the Compact GUI mockup's 3×2 constraint grid. Exclusivity is
    managed by hand (no ``QButtonGroup``) — the same PySide6-in-MoBu click-routing
    trap ``Segment`` documents. The selected chip is tinted with its per-type
    colour via an inline stylesheet (the colour varies per value, so it can't
    live in the shared ``#icon_grid_btn`` rule, which carries only the metrics).
    """

    valueChanged = Signal(str)

    def __init__(self, options, value: str | None = None, colors=None,
                 columns: int = 3, parent=None):
        super().__init__(parent)
        colors = colors or {}
        self._colors: dict = {}
        self._buttons: dict = {}
        self._current = None
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        for i, opt in enumerate(options):
            v, label, icon_name, mirror = opt
            color = colors.get(v, styles.TEXT_SECONDARY)
            self._colors[v] = color
            btn = QPushButton(label)
            btn.setObjectName("icon_grid_btn")
            btn.setCheckable(True)
            btn.setProperty("seg_value", v)
            btn.setIcon(icons.svg_icon_ex(icon_name, size=17, color=color, mirror=mirror))
            btn.setIconSize(QtCore.QSize(17, 17))
            # Fill the column so the grid reads as even thirds (the mockup's
            # repeat(3,1fr)); QPushButton is Fixed-horizontal by default.
            btn.setSizePolicy(SizePolicy.Expanding, SizePolicy.Fixed)
            btn.clicked.connect(self._on_button_clicked)
            grid.addWidget(btn, i // columns, i % columns)
            self._buttons[v] = btn
        for c in range(columns):
            grid.setColumnStretch(c, 1)
        if value is None and self._buttons:
            value = next(iter(self._buttons))
        if value is not None:
            self.setValue(value)

    @staticmethod
    def _rgba(hex_color: str, alpha: float) -> str:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"rgba({r},{g},{b},{alpha})"

    def _restyle(self, v: str) -> None:
        btn = self._buttons[v]
        color = self._colors[v]
        if v == self._current:
            bg = self._rgba(color, 0.16)
            bd = self._rgba(color, 0.55)
            fg = styles.TEXT_PRIMARY
        else:
            bg = styles.INPUT_BG
            bd = styles.BORDER
            fg = styles.TEXT_SECONDARY
        btn.setStyleSheet(
            f"QPushButton#icon_grid_btn {{ background-color:{bg};"
            f" border:1px solid {bd}; color:{fg}; }}"
        )

    def _on_button_clicked(self) -> None:
        btn = self.sender()
        if btn is None:
            return
        v = btn.property("seg_value")
        if v is not None:
            self._select(v)

    def _select(self, v: str) -> None:
        if v not in self._buttons:
            return
        for vv, btn in self._buttons.items():
            btn.setChecked(vv == v)
        self._current = v
        for vv in self._buttons:
            self._restyle(vv)
        self.valueChanged.emit(v)

    def value(self) -> str | None:
        return self._current

    def setValue(self, v: str) -> None:
        if v not in self._buttons:
            return
        for vv, btn in self._buttons.items():
            btn.setChecked(vv == v)
        self._current = v
        for vv in self._buttons:
            self._restyle(vv)


# ---------------------------------------------------------------------------
# Combo — QComboBox carrying a hidden value per item; emits valueChanged(str)
# ---------------------------------------------------------------------------

class Combo(QComboBox):
    """Dropdown selector. ``options`` is a list of (value, label) tuples or
    plain strings; the value (not the label) is what ``valueChanged`` emits and
    what ``value()`` returns -- mirroring ``Segment``'s value/label split.
    """

    valueChanged = Signal(str)

    def __init__(self, options=None, value: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("combo")
        self._values: list = []
        if options:
            self.set_items(options)
        if value is not None:
            self.setValue(value)
        self.currentIndexChanged.connect(self._on_index_changed)

    def set_items(self, options) -> None:
        """Repopulate the dropdown. Selection is left at index 0 -- callers
        that need to preserve a selection should read ``value()`` first and
        ``setValue()`` after.
        """
        self.blockSignals(True)
        self.clear()
        self._values = []
        for opt in options:
            v, label = (opt, opt) if isinstance(opt, str) else (opt[0], opt[1])
            self.addItem(label)
            self._values.append(v)
        self.blockSignals(False)

    def _on_index_changed(self, idx: int) -> None:
        if 0 <= idx < len(self._values):
            self.valueChanged.emit(str(self._values[idx]))

    def value(self):
        idx = self.currentIndex()
        return self._values[idx] if 0 <= idx < len(self._values) else None

    def setValue(self, v) -> None:
        if v in self._values:
            self.setCurrentIndex(self._values.index(v))
