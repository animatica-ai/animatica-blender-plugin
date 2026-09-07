"""Keeping the prompt timeline and the character state in step — with undo.

The timeline widget and the ``CharacterState`` behind it are two views of one
thing, and every edit has to cross between them: the widget emits a change, the
state absorbs it, the undo stack records it, and a restore pushes the whole
state back at the widget. That round trip is identical in every host — the
widget is core's and the state is core's — so it lives here rather than being
re-derived per plugin.

Two details are easy to get wrong and are handled here once. A drag emits its
change signal on every mouse-move, so commits are coalesced through a settle
timer and deferred while a drag is still in progress: one drag is one undo step.
And a restore sets ``_restoring`` so the write-back cannot arm a fresh commit
against the state it is restoring.

A window mixes :class:`TimelineBindingMixin` in and calls
:meth:`~TimelineBindingMixin.init_timeline_binding` from its ``__init__``.

What the host window still owns, and what this mixin calls on it:

``self.state`` / ``self._timeline``     the AppState and the PromptTimeline widget
``self._log(level, msg)``               the console
``self._save_timer``                    the debounced settings save
``self._ensure_timeline_character()``   resolve the CharacterState being edited
``self._sync_constraints_to_timeline()``  rebuild pins AND host viewport proxies
``self._refresh_timeline_keyframe_dots()``  read keyed frames off the host rig
``self.sec_constraints``                the card whose pill counts pins

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

import copy

from animatica_core.core.timeline_history import TimelineHistory

from . import timeline_edits
from .qt_compat import QtCore


#: Settle window for coalescing a burst of timeline edits into one undo step.
HISTORY_SETTLE_MS = 250


class TimelineBindingMixin:
    """State ↔ widget sync, pin/block edit slots, and per-character undo."""

    def init_timeline_binding(self) -> None:
        """Create the undo stacks and the settle timer. Call from ``__init__``.

        One snapshot stack per character (keyed by ``character_id``) of that
        character's prompts + constraints. ``_restoring`` suppresses re-commit
        while an undo/redo is being applied.
        """
        self._restoring = False
        self._history: dict[str, TimelineHistory] = {}
        self._history_timer = QtCore.QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(HISTORY_SETTLE_MS)
        self._history_timer.timeout.connect(self._commit_history)

    # ------------------------------------------------------------------
    # state → widget
    # ------------------------------------------------------------------

    def _push_state_to_timeline(self) -> None:
        """One-shot state → widget sync (call on character switch / show)."""
        cs = self._ensure_timeline_character()
        segs = [
            (p.start, p.end, p.text, p.color_idx, p.id, p.generation_count)
            for p in cs.prompts
        ]
        # blockSignals avoids a re-entrant prompts_changed echo back to state.
        self._timeline.blockSignals(True)
        try:
            self._timeline.load_segments(segs)
        finally:
            self._timeline.blockSignals(False)
        self._sync_constraints_to_timeline()
        self._refresh_timeline_keyframe_dots()
        # Seed this character's undo baseline the first time its state is shown
        # (load / character switch), so the first edit has a pre-edit state to
        # fall back to. Non-empty histories (incl. mid-undo/redo) are untouched.
        h = self._active_history()
        if len(h) == 0:
            h.seed(self._snapshot_active())

    def _refresh_timeline_constraint_markers(self) -> None:
        """Push the active character's constraint frames onto the timeline widget.

        The timeline's internal axis is take-LOCAL (0 = take start), like the
        blocks/playhead/ruler; ``marker.frame`` in the data is ABSOLUTE. Subtract
        the offset so pins sit on their blocks and the delete menu's
        ``hit_marker + _frame_offset`` resolves back to the correct absolute
        frame. This is the *cheap* widget-only refresh; it does not rebuild the
        viewport proxies (a frame-only pin shift leaves their world positions
        unchanged) or touch the section pill. Callers that also change pin
        counts / spatial values go through ``_sync_constraints_to_timeline``.
        """
        cs = self._ensure_timeline_character()
        off = int(self._timeline.frame_offset)
        items = sorted(
            ((m.frame - off, m.type) for m in cs.constraints),
            key=lambda ft: (ft[0], ft[1] or ""),
        )
        self._timeline.set_constraint_frames(items)

    # ------------------------------------------------------------------
    # widget → state
    # ------------------------------------------------------------------

    def _live_prompt_blocks(self) -> list:
        """Timeline blocks as ``[(text, start, end)]`` in SCENE frames.

        Block frames are take-local, so ``frame_offset`` is added — the Live
        section compares them against the host's playhead, which is absolute.
        """
        off = self._timeline.frame_offset
        return [(b.text, b.start_frame + off, b.end_frame + off)
                for b in self._timeline.blocks if b.text]

    def _on_timeline_prompts_changed(self) -> None:
        """Mirror the widget's block list back into the active character's prompts.

        Single-character v1: if no character is mapped, the prompts live in an
        anonymous CharacterState keyed by the placeholder id ``"_pending"`` so
        edits aren't lost while the user sets up the rig.
        """
        from animatica_core.core.prompt_model import PromptBox

        cs = self._ensure_timeline_character()
        # The widget's PromptBlock only carries id/start/end/text/color_idx, so a
        # naive rebuild would wipe every state-only field on each timeline edit
        # (generation_count — painted; params/server_request/last_result_ref —
        # load-bearing for regen). Index the pre-edit boxes by id and forward
        # those fields onto the matching new box; freshly-added blocks (id absent
        # from the index) fall through to PromptBox defaults.
        prev_by_id = {p.id: p for p in cs.prompts}
        rebuilt = []
        for b in sorted(self._timeline.blocks, key=lambda b: b.start_frame):
            box = PromptBox(
                id=b.id,
                start=int(b.start_frame),
                end=int(b.end_frame),
                text=b.text,
                color_idx=int(b.color_idx),
            )
            prev = prev_by_id.get(b.id)
            if prev is not None:
                box.generation_count = prev.generation_count
                box.params           = prev.params
                box.server_request   = prev.server_request
                box.last_result_ref  = prev.last_result_ref
            rebuilt.append(box)
        cs.prompts = rebuilt
        self._save_timer.start()
        self._arm_history_commit()

    # ------------------------------------------------------------------
    # pin edit slots — the rules live in timeline_edits, the views here
    # ------------------------------------------------------------------

    def _on_timeline_delete_constraint(self, frame: int,
                                       ctype: str | None = None) -> None:
        cs = self._ensure_timeline_character()
        cs.constraints, msg = timeline_edits.delete_pin(cs.constraints, frame, ctype)
        if msg:
            self._log("ok", msg)
            self._sync_constraints_to_timeline()

    def _on_timeline_delete_constraints(self, pairs) -> None:
        """Batched delete for the timeline's multi-selection.

        One filter pass followed by ONE resync — the debounced history timer
        then folds the whole batch (plus any simultaneous block removal riding
        on the same Del press) into a single undo step.
        """
        if not pairs:
            return
        cs = self._ensure_timeline_character()
        cs.constraints, msg = timeline_edits.delete_pins(cs.constraints, pairs)
        if msg:
            self._log("ok", msg)
            self._sync_constraints_to_timeline()

    def _on_timeline_move_constraints(self, old_frame: int, new_frame: int,
                                      ctype: str | None = None) -> None:
        """Move the pin(s) dragged on the timeline.

        Always resyncs: a rejected drop has to roll the dragged marker back to
        its data position, and the widget only learns that by being re-pushed.
        """
        cs = self._ensure_timeline_character()
        _applied, level, msg = timeline_edits.move_pins(
            cs.constraints, old_frame, new_frame, ctype)
        if msg:
            self._log(level, msg)
        self._sync_constraints_to_timeline()

    def _on_timeline_clear_constraints_type(self, ctype: str) -> None:
        cs = self._ensure_timeline_character()
        cs.constraints, msg = timeline_edits.clear_pins_of_type(cs.constraints, ctype)
        if msg:
            self._log("ok", msg)
            self._sync_constraints_to_timeline()

    def _on_timeline_clear_all_constraints(self) -> None:
        cs = self._ensure_timeline_character()
        cs.constraints, msg = timeline_edits.clear_all_pins(cs.constraints)
        if msg:
            self._log("ok", msg)
            self._sync_constraints_to_timeline()

    # ------------------------------------------------------------------
    # block edit slots
    # ------------------------------------------------------------------

    def _on_blocks_moved(self, moved) -> None:
        """Carry interior constraint pins when whole blocks are moved.

        *moved* is a list of ``(block_id, old_start, old_end, new_start,
        new_end)`` take-local spans (drag-start → drag-end), one per block that
        shifted. Reconciled once on release from the drag-start snapshot.
        """
        if self._restoring or not moved:
            return
        cs = self._ensure_timeline_character()
        if timeline_edits.shift_pins_with_moved_blocks(
                cs.constraints, moved, self._timeline.frame_offset):
            # prompts_changed fires right after this on release, arming the undo
            # commit that captures the shifted pins; here we only refresh the view.
            self._refresh_timeline_constraint_markers()

    def _on_block_resized(self, block_id: str, old_start: int, old_end: int,
                          new_start: int, new_end: int, scale_pins: bool) -> None:
        """Scale a resized block's interior constraint pins into its new span.

        Fires once on resize-release. When *scale_pins* is False (the default —
        the pin-scale modifier was not held) this is a no-op: pins stay at their
        absolute frames and may fall outside the resized block. That is the
        move-only decision, not a regression.
        """
        if self._restoring or not scale_pins:
            return
        cs = self._ensure_timeline_character()
        if timeline_edits.scale_pins_into_resized_block(
                cs.constraints, old_start, old_end, new_start, new_end,
                self._timeline.frame_offset):
            self._refresh_timeline_constraint_markers()

    def _bump_block_generation_count(self, block_id: str) -> None:
        """Increment a box's ``generation_count`` and repaint its badge.

        Counted once per box per generation. The badge paints from the widget's
        ``PromptBlock``, which the regen/apply paths never rebuild, so the bump
        is mirrored onto the live block and repainted — else the badge stays
        stale until an unrelated push.
        """
        cs = self._ensure_timeline_character()
        count = timeline_edits.bump_generation_count(cs.prompts, block_id)
        if count is None:
            return
        blk = self._timeline.find_block_by_id(block_id)
        if blk is not None:
            blk.generation_count = count
            self._timeline.update()
        self._save_timer.start()

    def _classify_frame_in_blocks(self, frame: int) -> str:
        """Classify *frame* against the timeline blocks, for the pin warning."""
        return timeline_edits.classify_frame_in_blocks(self._timeline.blocks, frame)

    # ------------------------------------------------------------------
    # undo / redo
    # ------------------------------------------------------------------

    def _active_history(self) -> TimelineHistory:
        """The undo stack for the active character (created on first use)."""
        cid = self._ensure_timeline_character().character_id
        h = self._history.get(cid)
        if h is None:
            h = self._history[cid] = TimelineHistory()
        return h

    def _snapshot_active(self) -> dict:
        """Deep-copied snapshot of the active character's editable timeline state."""
        cs = self._ensure_timeline_character()
        return {
            "character_id": cs.character_id,
            "prompts":      copy.deepcopy(cs.prompts),
            "constraints":  copy.deepcopy(cs.constraints),
        }

    def _arm_history_commit(self) -> None:
        """(Re)start the settle timer so a burst of changes commits once."""
        if self._restoring:
            return
        self._history_timer.start()

    def _commit_history(self) -> None:
        """Settle-timer slot: record the current state as one undo step.

        Deferred while a block drag is in progress (the timer can fire if the
        user pauses mid-drag with the button held) so one drag is one undo step,
        not two -- re-arm and let the post-release settle commit the final state.
        """
        if self._restoring:
            return
        if getattr(self._timeline, "_drag_mode", None) is not None:
            self._history_timer.start()
            return
        self._active_history().commit(self._snapshot_active())

    def _flush_history(self) -> None:
        """Commit any pending settle-timer edit NOW.

        Undoability must not depend on the 250 ms debounce having elapsed: an
        edit followed immediately by Ctrl+Z would otherwise be unrecorded (and
        so neither undoable nor redoable). Called at the top of undo/redo.
        """
        if self._history_timer.isActive():
            self._history_timer.stop()
            self._commit_history()

    def _on_undo(self) -> None:
        self._flush_history()
        self._apply_history_step(self._active_history().undo())

    def _on_redo(self) -> None:
        self._flush_history()
        self._apply_history_step(self._active_history().redo())

    def _apply_history_step(self, snap: dict | None) -> None:
        """Restore *snap* onto the active character and rebuild both views.

        Deep-copies out of the stored snapshot so a later edit can't mutate the
        history entry. ``_restoring`` blocks the change choke points from arming
        a fresh commit while we write the restored state back.
        """
        if snap is None:
            return
        cs = self._ensure_timeline_character()
        self._restoring = True
        try:
            cs.prompts = copy.deepcopy(snap["prompts"])
            cs.constraints = copy.deepcopy(snap["constraints"])
            self._push_state_to_timeline()
        finally:
            self._restoring = False
        self._save_timer.start()
