"""Generic, DCC-agnostic undo/redo stack for timeline edits.

Pure Python -- no ``pyfbsdk`` and no Qt -- so it lives under ``core/`` and is
unit-testable in plain CPython. It stores **opaque snapshots**: the host
(``gui/tool_window.py``) decides what a snapshot is (currently a dict bundling a
deep copy of the active character's prompt blocks + constraint markers) and how to
apply one. This class only owns the linear history and the current pointer.

Model: a single list with an index, NOT a before/after pair. ``_index`` points at
the snapshot currently applied. ``commit`` truncates any redo tail and appends;
``undo``/``redo`` walk the pointer and hand back the snapshot to apply. This keeps
redo correct without separate stacks.
"""

from __future__ import annotations

from typing import Any


DEFAULT_MAX_DEPTH = 50


def _equal(a: Any, b: Any) -> bool:
    """Best-effort equality used to skip no-op commits.

    Snapshots are dicts of dataclass lists; dataclasses compare by value, so ``==``
    is the right check. Guarded because a stray numpy array inside a constraint
    ``value`` would make ``==`` raise (ambiguous truth value) -- on any failure we
    report "not equal" so the commit is recorded rather than silently dropped.
    """
    try:
        return bool(a == b)
    except Exception:
        return False


class TimelineHistory:
    """Bounded linear undo/redo history of opaque snapshots."""

    def __init__(self, max_depth: int = DEFAULT_MAX_DEPTH):
        self._stack: list[Any] = []
        self._index: int = -1          # -1 == empty
        self._max_depth = max(1, int(max_depth))

    # -- queries -------------------------------------------------------
    def can_undo(self) -> bool:
        return self._index > 0

    def can_redo(self) -> bool:
        return -1 < self._index < len(self._stack) - 1

    @property
    def current(self) -> Any:
        """The snapshot currently applied, or ``None`` when empty."""
        return self._stack[self._index] if self._index >= 0 else None

    def __len__(self) -> int:
        return len(self._stack)

    # -- mutation ------------------------------------------------------
    def seed(self, snapshot: Any) -> None:
        """Set the initial baseline, discarding any prior history.

        Called once after the timeline first loads so the very first edit has a
        state to fall back to (and so ``can_undo`` is correctly False until then).
        """
        self._stack = [snapshot]
        self._index = 0

    def commit(self, snapshot: Any) -> bool:
        """Record *snapshot* as a new history entry.

        No-op (returns ``False``) when it equals the current entry -- e.g. a
        settle timer fired but nothing actually changed. Otherwise truncates the
        redo tail, appends, advances the pointer, and evicts the oldest entry
        past ``max_depth``. Returns ``True`` when an entry was added.
        """
        if self._index >= 0 and _equal(snapshot, self._stack[self._index]):
            return False
        # Drop any redo tail -- a fresh edit invalidates undone future states.
        del self._stack[self._index + 1:]
        self._stack.append(snapshot)
        self._index = len(self._stack) - 1
        # Enforce depth cap by dropping the oldest entries.
        overflow = len(self._stack) - self._max_depth
        if overflow > 0:
            del self._stack[:overflow]
            self._index -= overflow
        return True

    def undo(self) -> Any:
        """Step back one entry and return the snapshot to apply, or ``None``."""
        if not self.can_undo():
            return None
        self._index -= 1
        return self._stack[self._index]

    def redo(self) -> Any:
        """Step forward one entry and return the snapshot to apply, or ``None``."""
        if not self.can_redo():
            return None
        self._index += 1
        return self._stack[self._index]
