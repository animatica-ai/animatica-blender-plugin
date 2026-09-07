"""PySide2 / PySide6 compatibility shim.

Tries PySide6 first (Maya 2025+), falls back to PySide2 (Maya 2024 and
earlier).  Every other GUI module imports Qt classes through this file so
there is exactly one place to manage version differences.
"""

try:
    from PySide6 import QtWidgets, QtCore, QtGui
    from shiboken6 import wrapInstance, getCppPointer
    PYSIDE_VERSION = 6
except ImportError:
    from PySide2 import QtWidgets, QtCore, QtGui
    from shiboken2 import wrapInstance, getCppPointer
    PYSIDE_VERSION = 2

try:
    if PYSIDE_VERSION >= 6:
        from PySide6 import QtSvg
    else:
        from PySide2 import QtSvg
except ImportError:
    QtSvg = None


# -- Signal / Slot live in QtCore in both PySide2 and PySide6 ------------
Signal = QtCore.Signal
Slot = QtCore.Slot


# -- QAction moved from QtWidgets (PySide2) to QtGui (PySide6) -----------
if PYSIDE_VERSION >= 6:
    QAction = QtGui.QAction
else:
    QAction = QtWidgets.QAction


# -- QShortcut moved from QtWidgets (PySide2) to QtGui (PySide6) ---------
if PYSIDE_VERSION >= 6:
    QShortcut = QtGui.QShortcut
else:
    QShortcut = QtWidgets.QShortcut


# -- QMenu.exec_() renamed to QMenu.exec() in PySide6 --------------------
def menu_exec(menu, pos):
    """Call the correct exec variant for the active PySide version."""
    if PYSIDE_VERSION >= 6:
        menu.exec(pos)
    else:
        menu.exec_(pos)


# -- QSizePolicy enum nesting changed in PySide6 -------------------------
#    PySide2: QSizePolicy.Expanding
#    PySide6: QSizePolicy.Policy.Expanding
if PYSIDE_VERSION >= 6:
    SizePolicy = QtWidgets.QSizePolicy.Policy
else:
    SizePolicy = QtWidgets.QSizePolicy


# -- Mouse event position: QPoint in PySide2, QPointF in PySide6 ---------
def mouse_pos(ev):
    """Return event position as QPoint (works in both PySide2 and PySide6)."""
    if PYSIDE_VERSION >= 6:
        return ev.position().toPoint()
    return ev.pos()


# -- Wheel event x position -----------------------------------------------
def wheel_x(ev):
    """Return wheel event x position (float) in both PySide versions."""
    if PYSIDE_VERSION >= 6:
        return ev.position().x()
    return float(ev.x())


# -- QFontMetrics.width() removed in Qt6; use horizontalAdvance() --------
def font_width(fm, text):
    """Return pixel width of text using the correct QFontMetrics API."""
    if PYSIDE_VERSION >= 6:
        return fm.horizontalAdvance(text)
    return fm.width(text)
