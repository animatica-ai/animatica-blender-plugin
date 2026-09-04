"""Windows key-state polling for live driving.

MotionBuilder captures the keyboard at the native level before its embedded
Qt ever sees a key event, so Qt's ``grabKeyboard`` / ``keyPressEvent`` never
fire for the drive keys. The reliable input path is to poll the OS key
state each preview tick with ``GetAsyncKeyState`` (independent of focus,
only while MoBu is the foreground process).

DO NOT reintroduce a ``WH_KEYBOARD_LL`` hook here. It was tried and
reverted (2026-08-12): Windows serializes EVERY keystroke system-wide
through the hook callback, and inside MoBu that callback needs the GIL —
which the host starves for seconds at a time. Result: the whole system's
keyboard lagged/died, in and outside MoBu. Additionally, keys swallowed by
an LL hook never reach ``GetAsyncKeyState``, so the hook also killed this
module's own polling. Arrow suppression toward MoBu is handled with a pure
Qt application event filter in the GUI section instead — worst case it
does nothing, but it cannot harm the system.

Windows-only, which matches the plugin (Windows x64). Import is lazy and
guarded; on any other platform :func:`pressed_directions` returns an empty
set.
"""

from __future__ import annotations

import os

from .controls import DIRECTION_VECTORS

# Arrow keys are the primary bindings (user preference; they also collide
# less with MoBu than W/E/R-style manipulator shortcuts). W/A/S/D stay as
# secondary aliases. Any listed VK held -> direction held.
_VK = {
    "forward": (0x26, 0x57),   # Up,    W
    "left":    (0x25, 0x41),   # Left,  A
    "back":    (0x28, 0x53),   # Down,  S
    "right":   (0x27, 0x44),   # Right, D
}

try:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetForegroundWindow.restype = wintypes.HWND

    def _mobu_is_foreground() -> bool:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()

    def pressed_directions() -> set:
        """Direction names whose key is held right now (empty off-Windows,
        or when MoBu is not the foreground window)."""
        if not _mobu_is_foreground():
            return set()
        held = set()
        for name, vks in _VK.items():
            for vk in vks:
                if _user32.GetAsyncKeyState(vk) & 0x8000:
                    held.add(name)
                    break
        return held

    def raw_probe() -> dict:
        """Diagnostics: foreground state + raw key bits — for the remote
        console when chasing 'keys do nothing' reports."""
        return {
            "mobu_foreground": _mobu_is_foreground(),
            "held": sorted(pressed_directions()),
            "raw": {
                f"{name}:{hex(vk)}": bool(
                    _user32.GetAsyncKeyState(vk) & 0x8000)
                for name, vks in _VK.items() for vk in vks
            },
        }

except Exception:  # non-Windows, or ctypes unavailable
    def pressed_directions() -> set:
        return set()

    def raw_probe() -> dict:
        return {"mobu_foreground": False, "held": [], "raw": {}}


assert set(_VK) <= set(DIRECTION_VECTORS)   # names stay in sync
