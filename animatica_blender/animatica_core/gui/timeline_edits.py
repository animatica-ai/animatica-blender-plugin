"""Pure timeline-editing operations — pins and blocks, no widget, no host.

Everything a tool window does to a character's authored timeline when the user
deletes a pin, drags one onto another frame, clears a type, or moves/resizes a
prompt block. The rules are identical in every host (they are rules about the
data model, not about the scene), but they used to live as methods on the
MotionBuilder window, where nothing could reach them without a QApplication and
a running MotionBuilder.

The shape here is deliberate: each function takes the marker list, returns what
the list should become plus the message the user should see, and touches
nothing else. Redrawing the widget, rebuilding viewport proxies and arming the
undo commit stay with the window — those are its job and they differ per host.

``frame`` values are ABSOLUTE throughout (the data-model space). The widget's
take-local axis is the caller's business.

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

from animatica_core.core.prompt_model import constraint_can_add


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

def delete_pin(constraints, frame: int, ctype: str | None = None):
    """Drop the pin at *frame*. Returns ``(remaining, message_or_None)``.

    *ctype* narrows the delete to that single pin — ``(frame, type)`` is the pin
    identity, so one pin can be removed from a same-frame stack; ``None`` keeps
    the delete-all-at-frame behaviour the right-click menu relies on. A message
    of ``None`` means nothing matched, and the caller has nothing to resync.
    """
    frame = int(frame)
    before = len(constraints)
    remaining = [
        m for m in constraints
        if not (m.frame == frame and (ctype is None or m.type == ctype))
    ]
    if len(remaining) == before:
        return constraints, None
    what = f"'{ctype}' constraint" if ctype else "constraint(s)"
    return remaining, f"Removed {what} @ frame {frame}."


def delete_pins(constraints, pairs):
    """Batched delete for a timeline multi-selection.

    *pairs* is an iterable of ``(abs_frame, type_or_None)`` pin identities. All
    of them are filtered out in ONE pass so the caller can follow with ONE
    resync — the debounced history timer then folds the whole batch into a
    single undo step and the viewport viz rebuilds once instead of once per pin.
    A ``None`` type matches every pin at that frame, mirroring the single-delete
    path's wildcard. Returns ``(remaining, message_or_None)``.
    """
    if not pairs:
        return constraints, None
    doomed = {(int(f), t) for f, t in pairs}
    wild = {f for f, t in doomed if t is None}
    before = len(constraints)
    remaining = [
        m for m in constraints
        if (m.frame, m.type) not in doomed and m.frame not in wild
    ]
    removed = before - len(remaining)
    if not removed:
        return constraints, None
    return remaining, f"Removed {removed} constraint(s)."


def move_pins(constraints, old_frame: int, new_frame: int,
              ctype: str | None = None):
    """Move the pin(s) dragged from *old_frame* to *new_frame* (absolute).

    Returns ``(applied, level, message)``. ``level`` is ``None`` when there was
    nothing to move (the caller still resyncs, which snaps the dragged marker
    back to its data position), ``"warn"`` when the drop was rejected, ``"ok"``
    when the move landed. Mutates the matched markers in place on success —
    they are the caller's objects and their identity matters to the viz layer.

    Honours the per-frame coexistence policy: a same-type collision at the
    destination, or a combination :func:`constraint_can_add` forbids, is
    rejected rather than merged. Rejection is total — a multi-pin drag either
    lands whole or not at all — so a partially applied move can never leave the
    stack in a state the UI cannot express.
    """
    old_frame, new_frame = int(old_frame), int(new_frame)
    moving = [m for m in constraints
              if m.frame == old_frame and (ctype is None or m.type == ctype)]
    if not moving or old_frame == new_frame:
        return False, None, None
    dest_types = {m.type for m in constraints if m.frame == new_frame}
    moving_types = {m.type for m in moving}
    clash = moving_types & dest_types
    if clash:
        return False, "warn", (f"Can't move onto frame {new_frame}: "
                               f"{sorted(clash)} already there -- delete it first.")
    combined = set(dest_types)
    for t in moving_types:
        ok, reason = constraint_can_add(combined, t)
        if not ok:
            return False, "warn", f"Can't move to frame {new_frame}: {reason}"
        combined.add(t)
    for m in moving:
        m.frame = new_frame
    return True, "ok", (f"Moved {len(moving)} constraint(s): "
                        f"frame {old_frame} → {new_frame}.")


def clear_pins_of_type(constraints, ctype: str):
    """Drop every pin of *ctype*. Returns ``(remaining, message_or_None)``."""
    before = len(constraints)
    remaining = [m for m in constraints if m.type != ctype]
    removed = before - len(remaining)
    if not removed:
        return constraints, None
    return remaining, f"Cleared {removed} '{ctype}' constraint(s)."


def clear_all_pins(constraints):
    """Drop every pin. Returns ``(remaining, message_or_None)``."""
    removed = len(constraints)
    if not removed:
        return constraints, None
    return [], f"Cleared all {removed} constraint(s)."


def shift_pins_with_moved_blocks(constraints, moved, frame_offset: int) -> bool:
    """Carry interior pins when whole blocks are dragged. Returns True if any moved.

    *moved* is a list of ``(block_id, old_start, old_end, new_start, new_end)``
    take-LOCAL spans (drag-start → drag-end), one per block that shifted. Every
    pin whose absolute frame fell inside a block's **old** span travels by that
    block's delta; a pin sitting in an empty gap stays put. Reconciled from the
    drag-start snapshot, so dragging a block *across* a gap pin does not scoop
    it up.
    """
    from animatica_core.core.constraint_reconcile import reconcile_pins_on_moves
    if not moved or not constraints:
        return False
    off = int(frame_offset)
    moves = []
    for _bid, old_s, old_e, new_s, new_e in moved:
        delta = int(new_s) - int(old_s)
        if delta == 0:
            continue
        moves.append((int(old_s) + off, int(old_e) + off, delta))
    if not moves:
        return False
    new_frames = reconcile_pins_on_moves([m.frame for m in constraints], moves)
    shifted = False
    for m, nf in zip(constraints, new_frames):
        if nf != m.frame:
            m.frame = nf
            shifted = True
    return shifted


def scale_pins_into_resized_block(constraints, old_start: int, old_end: int,
                                  new_start: int, new_end: int,
                                  frame_offset: int) -> bool:
    """Map a resized block's interior pins into its new span. Returns True if any moved.

    Spans are take-LOCAL; each interior pin is mapped linearly from the old span
    into the new one (rounded, clamped). Callers gate this on the pin-scale
    modifier — without it, pins keep their absolute frames and may fall outside
    the resized block, which is the move-only decision, not a regression.
    """
    from animatica_core.core.constraint_reconcile import reconcile_pins_on_resize
    if not constraints:
        return False
    off = int(frame_offset)
    new_frames = reconcile_pins_on_resize(
        [m.frame for m in constraints],
        int(old_start) + off, int(old_end) + off,
        int(new_start) + off, int(new_end) + off,
    )
    moved = False
    for m, nf in zip(constraints, new_frames):
        if nf != m.frame:
            m.frame = nf
            moved = True
    return moved


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------

def classify_frame_in_blocks(blocks, frame: int) -> str:
    """Where *frame* sits relative to the prompt blocks, for the pin warning.

    Returns ``"no_blocks"`` (timeline empty), ``"boundary"`` (frame equals a
    block start/end — dropped during constraint merge), ``"interior"``
    (strictly inside a block), or ``"outside"`` (blocks exist, none cover it).
    *blocks* is any iterable of objects with ``start_frame`` / ``end_frame``.
    """
    blocks = list(blocks)
    if not blocks:
        return "no_blocks"
    f = int(frame)
    for b in blocks:
        start, end = int(b.start_frame), int(b.end_frame)
        if f == start or f == end:
            return "boundary"
        if start < f < end:
            return "interior"
    return "outside"


def bump_generation_count(prompts, block_id: str) -> int | None:
    """Increment one prompt's ``generation_count``; return the new value.

    Counted once per box per successful generation. Returns ``None`` when the
    id matches nothing, so the caller knows not to repaint. The widget's own
    ``PromptBlock`` mirror is the caller's to update — the regen/apply paths
    never rebuild it, so the badge would otherwise stay stale.
    """
    if not block_id:
        return None
    for p in prompts:
        if p.id == block_id:
            p.generation_count += 1
            return p.generation_count
    return None
