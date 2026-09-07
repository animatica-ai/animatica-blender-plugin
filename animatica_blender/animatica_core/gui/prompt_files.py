"""Prompt files on disk — Save, Load, and inserting a shipped example.

Three file paths in and out of the prompt timeline, all of them host-neutral:
the widget is core's, the JSON schema is core's, and the only scene question
asked anywhere here is "where is the playhead", which goes through the bridge.

The two loads differ on purpose and the difference is the interesting part.
**Load** replaces the timeline at the frames the file recorded. **Insert
example** keeps what is already there and shifts the whole file so its first
block lands on the playhead, placing each block through the widget's gap-clamped
``add_block`` — and refusing, with a summary, any block whose free gap cannot
fit it. Nothing is ever silently truncated.

File frames are take-LOCAL (schema v3) and marker frames in the data model are
ABSOLUTE, so every path here converts across the current take offset rather than
assuming the file was saved in the take it is being loaded into.

A window mixes :class:`PromptFilesMixin` in and calls
:meth:`~PromptFilesMixin.init_prompt_files` from its ``__init__``.

What the host window still owns, and what this mixin calls on it:

``self.state``                          ``last_prompt_dir`` lives there
``self._timeline``                      the ``PromptTimeline`` widget
``self._log(level, msg)``               the console
``self._persist_state()``               remembers the chosen folder
``self._ensure_timeline_character()``   the ``CharacterState`` being edited
``self._sync_constraints_to_timeline()``  rebuild pins + viewport proxies
``self._flush_viz_drags_to_data()``     flush pending marker drags before a save
``self._arm_history_commit()``          fold the insert into one undo step

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b.
"""

from __future__ import annotations

import os

from .qt_compat import QtWidgets
from .window_scaffold import bring_window_to_front


class PromptFilesMixin:
    """Save / Load / Insert-example for the prompt timeline."""

    def init_prompt_files(self) -> None:
        """Reset the modeless example chooser handle. Call from ``__init__``."""
        self._example_dialog: "QtWidgets.QDialog | None" = None

    # ------------------------------------------------------------------
    # where the file dialogs start
    # ------------------------------------------------------------------

    def _prompt_dialog_dir(self) -> str:
        """Start folder for the Save/Load Prompts dialogs.

        Last-used folder when remembered (persisted), the shipped example
        prompts folder on first use.
        """
        from animatica_core import resources
        return self.state.last_prompt_dir or resources.example_prompts_dir()

    def _remember_prompt_dir(self, path: str) -> None:
        self.state.last_prompt_dir = os.path.dirname(path)
        self._persist_state()

    # ------------------------------------------------------------------
    # save / load
    # ------------------------------------------------------------------

    def _on_save_prompts(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Prompts", self._prompt_dialog_dir(),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        self._remember_prompt_dir(path)
        if not path.lower().endswith(".json"):
            path += ".json"
        from animatica_core.gui.timeline.prompt_store_json import save_to_file
        segments = [
            {
                "text": b.text,
                "start_frame": int(b.start_frame),
                "end_frame": int(b.end_frame),
                "color_idx": int(b.color_idx),
            }
            for b in self._timeline.blocks
        ]
        # Flush pending viz-marker drags so the saved JSON captures the dragged
        # positions, not the original captured ones.
        self._flush_viz_drags_to_data()
        cs = self._ensure_timeline_character()
        # Marker frames are absolute; the file stores one coherent take-LOCAL
        # space (schema v3), so subtract the take offset here and record it.
        off = int(self._timeline.frame_offset)
        constraints = [
            {"frame": int(m.frame) - off, "joint": m.joint, "type": m.type,
             "value": m.value}
            for m in cs.constraints
        ]
        try:
            save_to_file(path, segments, constraints, frame_offset=off)
        except Exception as exc:                       # noqa: BLE001
            self._log("error", f"Save prompts failed: {exc}")
            return
        self._log(
            "ok",
            f"Saved {len(segments)} prompt(s) and {len(constraints)} constraint(s) "
            f"to {os.path.basename(path)}.",
        )

    def _on_load_prompts(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load Prompts", self._prompt_dialog_dir(),
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        self._remember_prompt_dir(path)
        from animatica_core.gui.timeline.prompt_store_json import load_from_file
        from animatica_core.core.prompt_model import ConstraintMarker
        try:
            segs, cons = load_from_file(path, with_constraints=True)
        except Exception as exc:                       # noqa: BLE001
            self._log("error", f"Load prompts failed: {exc}")
            return
        self._timeline.load_segments(segs)
        # load_segments emits prompts_changed; that writes through to state.
        cs = self._ensure_timeline_character()
        # File frames are take-LOCAL (schema v3) — lift into absolute with the
        # CURRENT take offset so pins stay glued to their blocks even when the
        # file was saved in a take starting elsewhere.
        off = int(self._timeline.frame_offset)
        cs.constraints = [
            ConstraintMarker(
                frame=int(c["frame"]) + off,
                joint=c.get("joint", "") or "",
                type=c["type"],
                value=c.get("value", {}) or {},
            )
            for c in cons
        ]
        self._sync_constraints_to_timeline()
        self._log(
            "ok",
            f"Loaded {len(segs)} prompt(s) and {len(cs.constraints)} constraint(s) "
            f"from {os.path.basename(path)}.",
        )

    # ------------------------------------------------------------------
    # the shipped examples
    # ------------------------------------------------------------------

    def open_example_prompt_chooser(self) -> None:
        """Menu entry: open (or raise) the MODELESS example-prompt chooser.

        Modeless so the viewport and timeline stay interactive while it is up --
        and since ``_load_example_prompts`` inserts at the CURRENT playhead, the
        user can scrub to the insertion point with the window open. One
        persistent instance, built lazily and held alive by ``self``, so
        re-invoking the menu focuses the existing dialog instead of stacking a
        second one; ``reload_listing()`` on every open keeps the "new files
        appear without a restart" guarantee. The load fires from the dialog's
        ``accepted`` signal ONLY -- ``QDialog.accept()`` hides it, so "Load
        inserts and closes" comes for free and there is no second return-value
        path that could double-load.
        """
        if self._example_dialog is None:
            from animatica_core.gui.example_prompts_dialog import (
                ExamplePromptsDialog)
            dlg = ExamplePromptsDialog(self)
            dlg.accepted.connect(self._on_example_prompt_accepted)
            self._example_dialog = dlg
        self._example_dialog.reload_listing()
        self._example_dialog.show()
        # raise_()/activateWindow() alone lose to Win32 foreground-lock after a
        # few open/close cycles (see bring_window_to_front).
        bring_window_to_front(self._example_dialog)

    def _on_example_prompt_accepted(self) -> None:
        """``accepted`` fires after the dialog hid itself but while it is still
        alive, so ``chosen_path()`` is safe to read here.
        """
        dlg = self._example_dialog
        path = dlg.chosen_path() if dlg is not None else None
        if path:
            self._load_example_prompts(path)

    def _playhead_frame(self) -> int:
        """The host's current frame, falling back to the widget's own mirror.

        A headless context (CI / docstrings) has no transport to read, and there
        the widget mirror is by definition in sync.
        """
        try:
            from animatica_core.bridge import time_bridge
            return int(time_bridge.current_frame())
        except Exception:
            return int(round(getattr(self._timeline, "_current_frame", 0.0)))

    def _load_example_prompts(self, path: str) -> None:
        """Insert *path*'s prompt blocks at the playhead, keeping existing blocks.

        Unlike ``_on_load_prompts`` (replace at saved frames), the whole file is
        shifted so its first block starts at the playhead, then each block is
        placed through the widget's gap-clamped ``add_block`` — skipping, with a
        summary warning, any block whose free gap can't fit its full duration
        (never silently truncated). Constraints ride along with the same shift:
        file frames are take-LOCAL (schema v3, one space with the segments), so
        the block delta applies directly and each pin is lifted into absolute
        with the CURRENT take offset. Pins whose anchor block was skipped, or
        that would land outside the take, are dropped into the same summary
        warning (an orphaned or off-take pin would be invisible/undeletable).
        """
        from animatica_core.gui.timeline.prompt_store_json import load_from_file
        from animatica_core.core.prompt_model import ConstraintMarker, constraint_can_add
        try:
            segs, cons = load_from_file(path, with_constraints=True)
        except Exception as exc:                       # noqa: BLE001
            self._log("error", f"Load example failed: {exc}")
            return
        if not segs and not cons:
            self._log("warn", f"{os.path.basename(path)} has no prompts.")
            return

        tl = self._timeline
        playhead_abs = self._playhead_frame()
        playhead_local = max(0, playhead_abs - int(tl.frame_offset))

        if segs:
            delta = playhead_local - min(s[0] for s in segs)
        else:  # constraints-only file: anchor the first marker instead
            delta = playhead_local - min(int(c["frame"]) for c in cons)

        added = skipped = 0
        skipped_spans = []      # file-space spans of blocks that didn't fit
        max_fr = tl._max_frames()
        for start, end, text, _color in segs:
            dur = int(end) - int(start)
            s = int(start) + delta
            lo, hi = tl._free_gap_at(s, exclude=())
            s = max(s, lo)
            if dur <= 0 or dur > max_fr or s + dur > hi:
                skipped += 1
                skipped_spans.append((int(start), int(end)))
                continue
            if tl.add_block(text=text, start_frame=s, end_frame=s + dur) is None:
                skipped += 1
                skipped_spans.append((int(start), int(end)))
                continue
            added += 1

        cs = self._ensure_timeline_character()
        off = int(tl.frame_offset)
        dropped = 0
        for c in cons:
            f_file = int(c["frame"])
            # A pin anchored inside a block we couldn't place would sit
            # orphaned in an empty gap — drop it (span test in file space).
            if any(s <= f_file <= e for s, e in skipped_spans):
                dropped += 1
                continue
            f_local = f_file + delta
            # Off-take pins are invisible on the timeline and undeletable
            # through the UI — drop rather than clamp (a clamped pin could
            # collide with an existing one at the clamp frame).
            if not (0 <= f_local <= tl.total_frames):
                dropped += 1
                continue
            f_abs = f_local + off
            # Same per-frame coexistence policy every other write path runs:
            # a same-type duplicate or a forbidden combo with the pins already
            # there — the user's, or ones this loop just inserted — is dropped,
            # never upserted over.
            existing = {m.type for m in cs.constraints if m.frame == f_abs}
            ok, _reason = constraint_can_add(existing, c["type"])
            if c["type"] in existing or not ok:
                dropped += 1
                continue
            cs.constraints.append(ConstraintMarker(
                frame=f_abs,
                joint=c.get("joint", "") or "",
                type=c["type"],
                value=c.get("value", {}) or {},
            ))
        if cons:
            self._sync_constraints_to_timeline()

        self._arm_history_commit()
        msg = (f"Inserted {added} prompt(s) and {len(cons) - dropped} "
               f"constraint(s) from {os.path.basename(path)} at frame "
               f"{playhead_abs}.")
        warn_bits = []
        if skipped:
            warn_bits.append(f"skipped {skipped} block(s) — no free gap fits")
        if dropped:
            warn_bits.append(f"dropped {dropped} constraint(s) — "
                             f"orphaned, off-take, or conflicting")
        if warn_bits:
            self._log("warn", f"{msg} ({'; '.join(warn_bits)}.)")
        else:
            self._log("ok", msg)
