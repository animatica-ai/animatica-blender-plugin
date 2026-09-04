"""Interactive prompt timeline widget for Kimodo Motion Tool.

Phase A/B improvements + GUI update pass:
  - time_source injection, QShortcut stepping, Ctrl+scroll zoom
  - Snap-to-frame/edge, multi-select, constraint context menu signals
  - Scrub on empty track click/drag (not just block drag)
  - Wider resize handle hit-area (_HANDLE_WIDTH=10)
  - Wider constraint icon hit-zone (16×16 clickable, 12×12 drawn)
  - _user_zoomed flag: resize recalculates ppf unless manually zoomed
  - step_zoom() / reset_zoom() public methods
  - Block gradient fill + 2px color bar at top
  - Hover scrub line on empty track
  - Constraint markers: color=side (left blue / right red), shape=limb
    (foot diamond / hand triangle); Path circle, Full Body square
  - Playhead glow effect
"""

from __future__ import annotations

from ..qt_compat import (
    QtWidgets, QtCore, QtGui,
    SizePolicy, menu_exec,
    mouse_pos, wheel_x, font_width,
)

QWidget       = QtWidgets.QWidget
QLineEdit     = QtWidgets.QLineEdit
QMenu         = QtWidgets.QMenu
QRubberBand   = QtWidgets.QRubberBand

Qt           = QtCore.Qt
QRect        = QtCore.QRect
QPoint       = QtCore.QPoint
QPointF      = QtCore.QPointF
Signal       = QtCore.Signal
QSize        = QtCore.QSize
QByteArray   = QtCore.QByteArray

QPainter          = QtGui.QPainter
QPainterPath      = QtGui.QPainterPath
QColor            = QtGui.QColor
QFont             = QtGui.QFont
QFontMetrics      = QtGui.QFontMetrics
QPen              = QtGui.QPen
QBrush            = QtGui.QBrush
QLinearGradient   = QtGui.QLinearGradient
QCursor           = QtGui.QCursor
QPolygonF         = QtGui.QPolygonF
QPixmap           = QtGui.QPixmap

from .. import styles

_TRACK_HEIGHT  = 54        # compact track: centered prompt + edge frame labels
_RULER_HEIGHT  = 22
_MARKER_ZONE   = 48        # 7e.5: bumped from 32 to fit a 3-marker stack cleanly
_MARKER_PIN    = 14        # constraint icon size (was inlined in paintEvent as `pin`)
_STACK_STEP    = 9         # 7e.5: vertical step per stacked marker (small overlap)
_MIN_BLOCK_W   = 24
_HANDLE_WIDTH  = 10        # resize hit-area AND the visible grip width (design HW)
_GRIP_MARK_H   = 11        # grip bar height (design: two 1.5×11 vertical marks)
SNAP_PX        = 6
_PLAYHEAD_GRAB_PX = 5     # click/hover distance at which the playhead wins a grab
MAX_PROMPT_SECONDS = 10

# Shown as the timeline tooltip when the cursor isn't over a block, so the two
# resize modifiers are discoverable. (promptboxes-animlayers-update Phase 4)
_DRAG_HINT = ("Drag edge: resize  •  Alt+drag edge: push neighbour  •  "
              "Alt+drag block: jump past neighbour  •  "
              "Ctrl+drag edge: scale pins  •  Shift: no snap")


def get_max_prompt_frames(fps):
    try:
        f = float(fps)
    except (TypeError, ValueError):
        f = 30.0
    return max(1, int(round(f * MAX_PROMPT_SECONDS)))


def validate_prompt_duration(start_frame, end_frame, fps):
    if end_frame <= start_frame:
        return False, "Prompt end must be greater than start."
    duration = end_frame - start_frame
    max_frames = get_max_prompt_frames(fps)
    if duration > max_frames:
        return False, (
            f"Prompt is {duration} frames; max at {float(fps):g} fps "
            f"is {max_frames} ({MAX_PROMPT_SECONDS}s)."
        )
    return True, ""


# 5-color pastel palette matching the Dusk design
_BLOCK_COLORS = [
    QColor("#3A5F8A"),   # blue
    QColor("#6A3A7A"),   # purple
    QColor("#3A6A52"),   # mint
    QColor("#7A5030"),   # amber
    QColor("#7A3A3A"),   # rose
    QColor("#2A5E6A"),   # teal
]

# Constraint marker style by type: color encodes side (left=blue / right=red),
# shape encodes limb (foot=diamond / hand=triangle); Path and Full Body get
# their own color + shape. Mirrors the viewport effector-cross scheme (Step 7).
_CONSTRAINT_STYLE = {
    "left-foot":  ("#6FB7FF", "diamond"),    # light blue
    "left-hand":  ("#3A7BD5", "triangle"),   # deeper blue
    "right-foot": ("#FF8A8A", "diamond"),    # light red
    "right-hand": ("#D5483A", "triangle"),   # deeper red
    "root2d":     ("#E0A24E", "circle"),     # amber  (Path)
    "fullbody":   ("#A879D0", "square"),     # purple (Full Body)
}
_CONSTRAINT_STYLE_DEFAULT = ("#5BB8C4", "diamond")  # teal fallback

# Wire type -> human label for context-menu text (mirrors the constraints-section
# selector vocabulary so "Path"/"Left Leg" etc. read the same everywhere).
_CONSTRAINT_LABELS = {
    "fullbody":   "Full Body",
    "left-foot":  "Left Leg",
    "right-foot": "Right Leg",
    "left-hand":  "Left Arm",
    "right-hand": "Right Arm",
    "root2d":     "Path",
}


class PromptBlock:
    __slots__ = ("id", "text", "start_frame", "end_frame", "color_idx",
                 "generation_count",
                 "_icon_rect", "_start_label_rect", "_end_label_rect")

    def __init__(self, text, start_frame, end_frame, color_idx=0, block_id=None,
                 generation_count=0):
        import uuid
        self.id = block_id or uuid.uuid4().hex
        self.text = text
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.color_idx = color_idx
        # How many times this box has been generated; painted as a corner badge
        # when >0. Forwarded from PromptBox state through load_segments.
        self.generation_count = generation_count
        self._icon_rect = None  # 16×16 clickable zone, set in paintEvent
        # Pixel hit-rects for the on-box start/end frame labels, set in the
        # wide-tier draw (None otherwise) so double-click can open the inline
        # numeric editor on a specific number. (timline-features Phase 2)
        self._start_label_rect = None
        self._end_label_rect   = None

    @property
    def duration(self):
        return self.end_frame - self.start_frame

    def to_dict(self):
        return {
            "id": self.id,
            "text": self.text,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
        }


class PromptTimeline(QWidget):
    """Horizontal timeline with draggable prompt blocks.

    Pass *time_source* (QObject with ``time_changed = Signal(float)``) to
    receive external playhead updates.  Leave None for headless / test use.
    """

    # --- existing signals ---
    prompts_changed           = Signal()
    generate_block_requested  = Signal(object)
    export_block_requested    = Signal(object)   # export a block's frame range → FBX
    duration_warning          = Signal(str)
    clear_keyframes_requested = Signal()

    # Emitted once on resize-release (id, old_start, old_end, new_start,
    # new_end, scale_pins). Distinct from prompts_changed (which mirrors block
    # geometry): the host uses this to optionally scale interior constraint pins
    # into the new span. old/new spans ride in the payload so the handler is
    # independent of prompts_changed ordering.
    # (promptboxes-animlayers-update Phase 4, item 1)
    block_resized = Signal(str, int, int, int, int, bool)

    # Emitted once on move-release; payload is a list of
    # ``(block_id, old_start, old_end, new_start, new_end)`` for every block that
    # actually shifted. The host carries each block's interior constraint pins by
    # its delta. Emitted on release (not per mouse-move) with the *drag-start*
    # spans, so a pin sitting in an empty gap is only carried if it was inside the
    # block when the drag began — dragging a block *over* a gap pin leaves it put.
    # (promptboxes-animlayers-update Phase 4, item 1)
    blocks_moved = Signal(object)

    # --- constraint signals (Phase A) ---
    # delete/move carry the pin's wire type since the Phase 3 rect hit-test made
    # single pins targetable inside a same-frame stack: (frame, type) is the pin
    # identity end-to-end. ``None`` type = whole-frame (legacy) behavior.
    add_constraint_requested            = Signal(int)
    delete_constraint_requested         = Signal(int, object)       # (abs_frame, type|None)
    # Batched delete (Phase 4): every selected pin rides in ONE payload — a
    # list of (abs_frame, type|None) pairs — so the host filters its constraint
    # list once and re-syncs once (single undo step, no per-pin viz flicker).
    # The single-pin context-menu path keeps delete_constraint_requested.
    delete_constraints_requested        = Signal(object)            # [(abs_frame, type|None), …]
    clear_constraints_type_requested    = Signal(str)   # wire type -> clear that type
    clear_all_constraints_requested     = Signal()      # clear every type
    move_constraints_requested          = Signal(int, int, object)  # (old_abs, new_abs, type|None)
    # Pin click-select (Phase 5): a plain click on a pin — release without a
    # frame change, the same deferred-to-release path that sets the timeline
    # selection — also announces the pin so the host can select its viewport
    # proxy marker. Read-only for the host: no state mutation, no history arm.
    # Ctrl-clicks (multi-select building) deliberately do NOT emit.
    constraint_clicked                  = Signal(int, object)       # (abs_frame, type)

    # --- scrub signals (Phase B) ---
    scrub_requested = Signal(float)
    scrub_finished  = Signal(float)

    # --- transport ---
    play_toggle_requested = Signal()

    # --- keyboard shortcut requests (hover-gated keyPressEvent dispatch) ---
    # F → fit the timeline to MoBu's take range (relayed by the container to the
    # same handler the Fit button uses); Ctrl+G → full-timeline generate (same
    # path as the container's Generate button). Both are emitted here and relayed
    # through TimelineContainer so tool_window's existing wiring is untouched.
    fit_to_mobu_requested = Signal()
    generate_requested    = Signal()

    # --- constraint navigation (snap playhead to authored constraint frames) ---
    prev_constraint_requested = Signal()
    next_constraint_requested = Signal()

    # --- timeline edit undo / redo (the host owns the snapshot stack; the
    # widget only forwards the keystroke) ---
    undo_requested = Signal()
    redo_requested = Signal()

    # --- viewport signal (Phase D — compare strips) ---
    viewport_changed      = Signal()
    current_frame_changed = Signal(float)

    # Emitted (start, stop) in take-LOCAL frames whenever the user zooms/pans or
    # triggers a fit/zoom action, so the host can drive MoBu's native transport
    # zoom bar to match (widget→native half of the zoom bridge). NOT emitted by
    # host-driven ``set_visible_frame_window`` (emit=False) so the two surfaces
    # can't feed back into each other. (timline-features Phase 3)
    visible_window_changed = Signal(int, int)

    def __init__(self, total_frames=150, fps=30.0, frame_offset=0,
                 time_source=None, parent=None):
        super().__init__(parent)

        self._total_frames  = max(total_frames, 1)
        self._frame_offset  = int(frame_offset)
        self._fps           = fps
        self._blocks: list  = []
        self._color_counter = 0
        self._display_range = None
        # list of (frame:int, type:str|None) -- multiple markers may share a frame
        self._constraint_markers: list = []
        # Take-local frames where the root/Hips joint carries keys; painted as
        # faint grey dots (a read-only overlay, no interaction).
        self._keyframe_dots: list = []

        # Zoom / scroll
        self._ppf: float | None = None
        self._scroll_x: float   = 0.0
        self._user_zoomed: bool = False   # True once Ctrl+scroll is used
        # A frame window handed to set_visible_frame_window before the widget had
        # a real width (e.g. session-restore firing while the timeline dock is
        # still laying out). Drained in resizeEvent so the restore isn't lost to
        # a width==0 early-return. (timline-features Phase 3)
        self._pending_window: tuple[float, float] | None = None

        # Playhead
        self._current_frame: float = 0.0
        if time_source is not None:
            time_source.time_changed.connect(self._on_external_time)

        # Drag state
        self._drag_block       = None
        self._drag_mode        = None
        self._drag_start_pos   = None
        self._drag_start_frame = None
        self._drag_start_end   = None
        self._group_move_bounds = (-(10 ** 9), 10 ** 9)
        self._hover_block      = None
        # Whether the pin-scale modifier (Ctrl) was held when a resize started;
        # captured at press, read on resize-release to set block_resized's
        # scale_pins flag. (promptboxes-animlayers-update Phase 4, item 1)
        self._resize_scale_pins = False
        # Drag-start spans {block_id: (start, end)} for the blocks in a move,
        # captured at press; diffed on release to emit blocks_moved so pin-follow
        # references the true drag-start span, not a per-move-advancing one.
        self._move_start_spans: dict = {}

        # Ruler pan state
        self._pan_active: bool        = False
        self._pan_start_x: int        = 0
        self._pan_start_scroll: float = 0.0

        # Playhead flag clickable rect (updated in paintEvent)
        self._playhead_flag_rect: QRect | None = None

        # Empty-track scrub
        self._scrubbing: bool = False

        # Hover line (empty track only)
        self._hover_x: int | None = None

        # Constraint-pin hit/hover state (help-example-clean Phase 3): per-paint
        # cache of the pins' drawn rects as (QRect, take-local frame, type) —
        # mirrors the block._icon_rect pattern so hit zones match the painted
        # pixels (Y-aware, unlike the old X-only column test). ``_hover_marker``
        # is the (frame, type) under the cursor; ``_hover_playhead`` is True when
        # the cursor sits within grab distance of the playhead line/flag.
        self._marker_pin_rects: list = []
        self._hover_marker: tuple | None = None
        self._hover_playhead: bool = False

        # Multi-select
        self._selected: set          = set()
        # Selected constraint pins as (take-local frame, type) pairs — the pin
        # half of the unified selection (Phase 4). Pruned against the live
        # marker list in set_constraint_frames so stale pairs can't linger.
        self._selected_markers: set  = set()
        self._press_block            = None
        self._press_did_drag: bool   = False
        self._rubber: QRubberBand | None = None
        self._rubber_origin: QPoint | None = None
        self._press_offsets: dict    = {}

        # Snap hint
        self._snap_hint_x: float | None = None

        # Constraint-marker drag (take-LOCAL frames). ``_marker_drag_orig`` is
        # the frame grabbed at press (immutable for the drag); ``_marker_drag_cur``
        # is the live target; ``_marker_drag_snapshot`` is the markers list at
        # press so each move re-maps orig->cur from a stable base.
        # ``_marker_drag_type`` narrows the drag to the grabbed pin's type so one
        # pin can leave a same-frame stack (Phase 3: identity = (frame, type)).
        self._marker_drag_orig: int | None = None
        self._marker_drag_cur: int | None = None
        self._marker_drag_type: str | None = None
        self._marker_drag_snapshot: list | None = None

        # Inline prompt editor (replaces QInputDialog)
        self._inline_edit: QLineEdit | None = None

        # Cached constraint-add icon (12×12 drawn inside 16×16 hit-zone)
        self._constraint_icon_pm: QPixmap | None = None

        h = _RULER_HEIGHT + _MARKER_ZONE + _TRACK_HEIGHT + 8
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)
        self.setSizePolicy(SizePolicy.Expanding, SizePolicy.Fixed)
        self.setMouseTracking(True)
        # StrongFocus + focus-follows-hover (see enterEvent): keyPressEvent only
        # fires on the focused widget, so the timeline grabs focus when the mouse
        # enters it. That makes F / Shift+F / Ctrl+G fire whenever the mouse
        # hovers the timeline even if the MoBu viewport previously held focus
        # (resolves the #6/#7 viewport-focus problem).
        self.setFocusPolicy(Qt.StrongFocus)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self._build_constraint_icon()

    # ------------------------------------------------------------------
    # Shortcuts
    # ------------------------------------------------------------------
    # All keyboard shortcuts are dispatched in keyPressEvent (hover-gated) rather
    # than via QShortcut, so they (a) fire whenever the mouse hovers the timeline
    # regardless of Qt focus and (b) pass through to MoBu otherwise. The
    # text-conflicting keys ([ / ] and Ctrl+Z / Ctrl+Y) are suppressed there while
    # the inline prompt editor is open. See keyPressEvent below.

    def step_frames(self, delta: int):
        new_frame = max(0, self._current_frame + delta)
        self._current_frame = new_frame
        self.scrub_requested.emit(new_frame)
        self.current_frame_changed.emit(float(new_frame))
        self._safe_update()

    def nudge_selected_blocks(self, delta: int):
        """Shift the selected prompt block(s) by *delta* frames (feature 3).

        The group shift is clamped to the free room around the selection — the
        same per-block ``_free_gap_at`` bounds the mouse-move path computes — so
        no block drops below frame 0 or crosses a non-selected neighbour. Emits
        ``blocks_moved`` (drag-start → new spans) then ``prompts_changed``, in
        that order, exactly as ``mouseReleaseEvent`` does, so the host carries
        interior constraint pins, persists the geometry, and arms one undo
        commit — keyboard nudges are undoable/persistent like mouse moves.
        No-op when the selection is empty."""
        selection = self.selected_blocks()
        if not selection:
            return
        sel_set = set(selection)
        # Generalised group-shift bounds (no len<=1 short-circuit, so a single
        # selected block is still clamped to its neighbours). ``_free_gap_at``'s
        # lo defaults to 0, so the frame-0 floor is folded in per block.
        min_d, max_d = -(10 ** 9), 10 ** 9
        for blk in selection:
            lo, hi = self._free_gap_at(blk.start_frame, exclude=sel_set)
            min_d = max(min_d, lo - blk.start_frame)   # can't move start below lo
            max_d = min(max_d, hi - blk.end_frame)     # can't move end past hi
        if max_d < min_d:                              # interleaved -> no room
            max_d = min_d = 0
        int_delta = max(min_d, min(int(delta), max_d))
        if int_delta == 0:
            return
        moved = []
        for blk in selection:
            old_s, old_e = blk.start_frame, blk.end_frame
            blk.start_frame = old_s + int_delta
            blk.end_frame   = old_e + int_delta
            moved.append((blk.id, int(old_s), int(old_e),
                          int(blk.start_frame), int(blk.end_frame)))
        if moved:
            self.blocks_moved.emit(moved)
        self.prompts_changed.emit()
        self.update()

    def snap_playhead_to_block_edge(self, edge: str):
        """Move the playhead to the selected block(s)' start or end (feature 1).

        *edge* is ``"start"`` or ``"end"``. Uses the min start / max end across
        the selection so a multi-select snaps to the selection's extent. Playhead
        frames are absolute; block frames are take-local, so ``_frame_offset`` is
        added. Emits the same signals as ``step_frames`` (``scrub_requested`` +
        ``current_frame_changed``) so the host seeks MoBu identically. No-op when
        the selection is empty."""
        selection = self.selected_blocks()
        if not selection:
            return
        if edge == "start":
            local = min(b.start_frame for b in selection)
        else:
            local = max(b.end_frame for b in selection)
        new_frame = max(0, local + self._frame_offset)
        self._current_frame = float(new_frame)
        self.scrub_requested.emit(float(new_frame))
        self.current_frame_changed.emit(float(new_frame))
        self._safe_update()

    def snap_block_edge_to_playhead(self, edge: str):
        """Move the selected block(s)' start or end edge to the playhead frame
        (feature 12).

        *edge* is ``"start"`` or ``"end"``. The playhead is absolute; the target
        is converted to take-local via ``_frame_offset``. Each block's edge is
        clamped to start<end, the max-prompt-duration cap, and the free gap to
        neighbours (so an edge can't cross another block) — mirroring the mouse
        resize path. Emits ``block_resized`` (scale_pins=False) per changed block
        then ``prompts_changed``, so resizes are undoable/persistent like mouse
        edits. No-op when the selection is empty."""
        selection = self.selected_blocks()
        if not selection:
            return
        target = max(0, int(round(self._current_frame)) - self._frame_offset)
        max_fr = self._max_frames()
        changed = False
        for b in selection:
            old_s, old_e = b.start_frame, b.end_frame
            lo, hi = self._free_gap_at(b.start_frame, exclude={b})
            if edge == "start":
                new_start = max(lo, min(target, b.end_frame - 1))
                new_start = max(new_start, b.end_frame - max_fr)
                b.start_frame = new_start
            else:
                new_end = min(hi, max(target, b.start_frame + 1))
                new_end = min(new_end, b.start_frame + max_fr)
                b.end_frame = new_end
            if b.start_frame != old_s or b.end_frame != old_e:
                self.block_resized.emit(
                    b.id, int(old_s), int(old_e),
                    int(b.start_frame), int(b.end_frame), False,
                )
                changed = True
        if changed:
            self.prompts_changed.emit()
            self.update()

    def jump_playhead_to_adjacent_edge(self, direction: int):
        """Jump the playhead to the nearest block edge before / after it
        (item 10, `,` / `.`).

        *direction* is -1 (previous) or +1 (next). The edge set is every
        block's start and end (deduped), selection-independent. Block frames
        are take-local while the playhead is absolute, so edges are lifted
        into absolute space via ``_frame_offset`` before comparing. Strictly
        before / after the current frame, so repeated presses walk edge to
        edge; no-op at the extremes and with no blocks. Emits the same
        signals as ``step_frames`` so the host seeks MoBu identically."""
        edges = sorted({b.start_frame + self._frame_offset for b in self._blocks}
                       | {b.end_frame + self._frame_offset for b in self._blocks})
        cur = int(round(self._current_frame))
        if direction < 0:
            hits = [e for e in edges if e < cur]
        else:
            hits = [e for e in edges if e > cur]
        if not hits:
            return
        new_frame = float(max(0, hits[-1] if direction < 0 else hits[0]))
        self._current_frame = new_frame
        self.scrub_requested.emit(new_frame)
        self.current_frame_changed.emit(new_frame)
        self._safe_update()

    # ------------------------------------------------------------------
    # External time (Phase B)
    # ------------------------------------------------------------------
    def _on_external_time(self, frame: float):
        self._current_frame = frame
        self.current_frame_changed.emit(float(frame))
        self._safe_update()

    # ------------------------------------------------------------------
    # Constraint icon cache
    # ------------------------------------------------------------------
    def _build_constraint_icon(self):
        try:
            from .icons import lucide_pixmap
            self._constraint_icon_pm = lucide_pixmap("anchor", size=12, color=styles.TEXT_3)
        except Exception:
            self._constraint_icon_pm = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def total_frames(self):
        return self._total_frames

    @total_frames.setter
    def total_frames(self, value):
        self._total_frames = max(value, 1)
        if not self._user_zoomed:
            self._ppf = None
        elif self._ppf is not None:
            # Even a user-zoomed view must not stay zoomed OUT past the (possibly
            # shrunk) take extent — enforce the zoom-out floor. On a fresh MoBu /
            # first-run / autostart the take resolves to its real length only
            # after the view was already framed (from a wide native zoombar), so
            # without this the ruler corrects to the take end but the view stays
            # zoomed out, leaving empty space beyond the take. Preserves zoom-in.
            self._ppf = max(self._ppf, self._min_ppf())
            self._clamp_scroll()
        self._safe_update()

    def _effective_total(self):
        # Scroll/zoom extent is capped at the current MoBu take end
        # (``_total_frames``, fed from the take via _apply_mobu_range). A block
        # dragged/loaded past the take end is still painted, but the scrollable
        # canvas stops at the take end so the timeline can't pan/zoom into empty
        # space beyond real take data.
        # (promptboxes-animlayers-update Phase 3, item 2)
        return max(self._total_frames, 1)

    @property
    def frame_offset(self):
        return self._frame_offset

    @frame_offset.setter
    def frame_offset(self, value):
        self._frame_offset = int(value)
        self._safe_update()

    @property
    def fps(self):
        return self._fps

    @fps.setter
    def fps(self, value):
        self._fps = max(value, 1.0)
        self._safe_update()

    @property
    def display_range(self):
        return self._display_range

    @display_range.setter
    def display_range(self, value):
        if value is None:
            self._display_range = None
        else:
            lo, hi = int(value[0]), int(value[1])
            if hi < lo:
                lo, hi = hi, lo
            self._display_range = (lo, hi)
        self._safe_update()

    def _safe_update(self):
        try:
            self.update()
        except RuntimeError:
            pass

    def set_constraint_frames(self, frames: list) -> None:
        """Accepts bare frame ints or ``(frame, type)`` tuples. The type drives
        the per-marker color + shape; ``None`` falls back to the default style."""
        norm = []
        for it in frames:
            if isinstance(it, (tuple, list)):
                norm.append((int(it[0]), it[1] if len(it) > 1 else None))
            else:
                norm.append((int(it), None))
        self._constraint_markers = norm
        # Drop selected pairs that no longer exist (deleted, moved, or another
        # character's set was loaded) so the selection never points at ghosts.
        self._selected_markers &= set(norm)
        self._safe_update()

    def set_keyframe_dots(self, frames: list) -> None:
        """Faint grey dots at take-local *frames* where the root/Hips is keyed.

        A read-only overlay (no hit-testing). Frames are already take-local —
        the caller subtracts ``frame_offset`` (mirror ``set_constraint_frames``)."""
        self._keyframe_dots = [int(f) for f in frames]
        self._safe_update()

    @property
    def blocks(self):
        return list(self._blocks)

    def selected_blocks(self):
        """Snapshot of the currently selected prompt blocks (copy, never the
        live ``_selected`` set). Empty when nothing is selected."""
        return list(self._selected)

    def get_prompts(self):
        return [b.to_dict() for b in sorted(self._blocks, key=lambda b: b.start_frame)]

    def load_segments(self, segments):
        """Replace the block list from external state in one batch.

        *segments* is an iterable of (start, end, text, color_idx) tuples —
        the shape used by ``core.prompt_model.PromptBox`` once mapped at the
        boundary. Five-tuples ``(start, end, text, color_idx, block_id)`` are
        also accepted so block ids round-trip across save/load and the
        widget can re-associate with persistent ``PromptBox.id``; six-tuples
        add a trailing ``generation_count`` for the corner badge. Fires
        ``prompts_changed`` exactly once.
        """
        self._blocks.clear()
        self._selected.clear()
        max_color = 0
        for seg in segments:
            block_id = None
            gen_count = 0
            if len(seg) >= 6:
                start, end, text, color_idx, block_id, gen_count = seg[:6]
            elif len(seg) >= 5:
                start, end, text, color_idx, block_id = seg[0], seg[1], seg[2], seg[3], seg[4]
            else:
                start, end, text, color_idx = seg
            block = PromptBlock(
                text=text or "",
                start_frame=int(start),
                end_frame=int(end),
                color_idx=int(color_idx) % len(_BLOCK_COLORS),
                block_id=block_id or None,
                generation_count=int(gen_count or 0),
            )
            self._blocks.append(block)
            max_color = max(max_color, int(color_idx))
        self._color_counter = max_color + 1
        self.prompts_changed.emit()
        self.update()

    def find_block_by_id(self, block_id):
        """Return the block whose ``id`` matches *block_id*, or ``None``."""
        for b in self._blocks:
            if b.id == block_id:
                return b
        return None

    # ------------------------------------------------------------------
    # Zoom / scroll
    # ------------------------------------------------------------------
    def _ensure_ppf(self):
        if self._ppf is None:
            track = self._track_rect()
            eff = self._effective_total()
            if track.width() > 0 and eff > 0:
                self._ppf = track.width() / eff

    def _clamp_scroll(self):
        if self._ppf is None:
            return
        max_scroll = max(0.0, self._effective_total() * self._ppf - self._track_rect().width())
        self._scroll_x = max(0.0, min(self._scroll_x, max_scroll))

    def _min_ppf(self) -> float:
        """Zoom-out floor: the pixels-per-frame at which the full take extent
        (``_effective_total``) exactly fills the track width. Zooming out past
        this would reveal empty space beyond the take end, so it bounds the
        Ctrl+scroll / zoom-out low end. Falls back to a tiny absolute floor when
        the width/extent isn't known yet. (timline-features Phase 3)"""
        track = self._track_rect()
        eff = self._effective_total()
        if track.width() <= 0 or eff <= 0:
            return 0.5
        return track.width() / eff

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._user_zoomed:
            self._ppf = None          # recalculate to fill new width
        self._ensure_ppf()
        # Re-enforce the zoom-out floor now that the widget has a REAL width, even
        # on a user-zoomed view. A ``_ppf`` computed by the ``total_frames`` setter
        # (fix E) or ``set_visible_frame_window`` while width was 0 — e.g. the
        # timeline seeded from MoBu's zoombar on autostart while still inside its
        # not-yet-opened floating dock — used ``_min_ppf()``'s 0.5 zero-width
        # fallback, so a sub-min (zoomed-OUT-past-take) ``_ppf`` could survive and
        # then paint with empty space beyond the take end. On File→Open the dock is
        # already open (width known), so this only bites first MoBu start/autostart.
        # ``max`` preserves a genuine zoom-IN; it only lifts an illegal zoom-out.
        if self._ppf is not None and self._track_rect().width() > 0:
            self._ppf = max(self._ppf, self._min_ppf())
        self._clamp_scroll()
        # Drain a window stashed while the widget had no width yet (see
        # _pending_window): now that we have a real track width, apply it. Done
        # after the refit so the restored window wins over the fill-width default.
        if self._pending_window is not None and self._track_rect().width() > 0:
            s, e = self._pending_window
            self.set_visible_frame_window(s, e)   # emit=False; clears _pending_window
        self.update()
        self.viewport_changed.emit()

    def step_zoom(self, factor: float):
        """Zoom in (factor > 1) or out (factor < 1), centred on the view."""
        self._ensure_ppf()
        if self._ppf is None:
            return
        cx = self._track_rect().width() // 2
        center_frame = self._x_to_frame(cx)
        self._ppf = max(self._min_ppf(), min(200.0, self._ppf * factor))
        self._user_zoomed = True
        self._scroll_x = center_frame * self._ppf - cx
        self._clamp_scroll()
        self.update()
        self.viewport_changed.emit()
        self.visible_window_changed.emit(*self._visible_frame_window())

    def reset_zoom(self):
        """Fit the full timeline range in the current widget width."""
        self._ppf = None
        self._scroll_x = 0.0
        self._user_zoomed = False
        self._ensure_ppf()
        self.update()
        self.viewport_changed.emit()

    def fit_to_selected_blocks(self):
        """Zoom the view to the selected prompt block(s)' frame span, padded by a
        small margin. No-op when nothing is selected. Mirrors the ppf/scroll +
        clamp logic of ``reset_zoom`` / ``step_zoom`` but driven off the block
        selection rather than the full timeline range (the ``Shift+F`` action)."""
        if not self._selected:
            return
        lo = min(b.start_frame for b in self._selected)
        hi = max(b.end_frame   for b in self._selected)
        if hi <= lo:
            return
        width = self._track_rect().width()
        if width <= 0:
            return
        # Pad the span by ~8% on each side so the block edges aren't flush with
        # the viewport border.
        margin  = max(1.0, (hi - lo) * 0.08)
        span_lo = max(0.0, lo - margin)
        span    = (hi + margin) - span_lo
        if span <= 0:
            return
        # track.x() is 0, so _frame_to_x(f) = f*ppf - scroll_x; placing span_lo at
        # x=0 means scroll_x = span_lo * ppf.
        self._ppf = max(0.5, min(200.0, width / span))
        self._user_zoomed = True
        self._scroll_x = span_lo * self._ppf
        self._clamp_scroll()
        self.update()
        self.viewport_changed.emit()

    def _visible_frame_window(self) -> tuple[int, int]:
        """Current visible take-LOCAL ``(start, stop)`` frame window derived from
        ``_ppf``/``_scroll_x`` and the track width. Falls back to the full extent
        when the view isn't zoomed yet. Used to report the widget's window to the
        host so it can mirror it onto MoBu's native zoom bar. (Phase 3)"""
        self._ensure_ppf()
        if self._ppf is None:
            return 0, self._effective_total()
        track = self._track_rect()
        left  = self._x_to_frame(track.left())
        right = self._x_to_frame(track.right())
        if right <= left:
            right = left + 1
        return int(left), int(right)

    def set_visible_frame_window(self, start, stop, emit: bool = False):
        """Frame the take-LOCAL window ``[start, stop]`` in the view.

        Sets ``_ppf``/``_scroll_x`` so *start* sits at the left track edge and the
        span fills the track width, and marks the view user-zoomed. When *emit* is
        True (user-initiated fit/zoom) also emits ``visible_window_changed`` with
        the *actual* post-clamp window so the host can drive MoBu's native zoom
        bar; the host-driven native→widget path calls with emit=False so the two
        surfaces never feed back. When the widget has no width yet the window is
        stashed in ``_pending_window`` and applied on the next resize. (Phase 3)

        *stop* is clamped to the take end (``_effective_total``) so a native
        zoombar wider than the take (MoBu's bar often spans the global timeline)
        never frames the view into empty space beyond real take data — the same
        zoom-out floor ``_min_ppf`` enforces for the widget's own zoom paths."""
        start = max(0.0, float(start))
        stop  = min(float(stop), float(self._effective_total()))
        if stop <= start:
            return
        width = self._track_rect().width()
        if width <= 0:
            # Widget not laid out yet — remember the request and drain on resize.
            self._pending_window = (start, stop)
            return
        span = stop - start
        if span <= 0:
            return
        self._pending_window = None
        self._ppf = max(0.5, min(200.0, width / span))
        self._user_zoomed = True
        self._scroll_x = start * self._ppf
        self._clamp_scroll()
        self.update()
        self.viewport_changed.emit()
        if emit:
            self.visible_window_changed.emit(*self._visible_frame_window())

    def fit_zoom_to_prompts(self):
        """Zoom the view to span *all* prompt blocks (feature 8), padded slightly,
        and drive the native zoom bar to match (emit=True). No-op when empty."""
        if not self._blocks:
            return
        lo = min(b.start_frame for b in self._blocks)
        hi = max(b.end_frame   for b in self._blocks)
        if hi <= lo:
            return
        margin = max(1.0, (hi - lo) * 0.08)
        self.set_visible_frame_window(max(0.0, lo - margin), hi + margin, emit=True)

    def zoom_to_selected_block(self):
        """Zoom the view to the selected prompt block(s)' span (feature 13) and
        drive the native zoom bar to match. No-op when nothing is selected."""
        if not self._selected:
            return
        lo = min(b.start_frame for b in self._selected)
        hi = max(b.end_frame   for b in self._selected)
        if hi <= lo:
            return
        margin = max(1.0, (hi - lo) * 0.08)
        self.set_visible_frame_window(max(0.0, lo - margin), hi + margin, emit=True)

    def zoom_to_block(self, block):
        """Zoom the view to a single *block*'s span (context-menu zoom-to-box,
        feature 13) and drive the native zoom bar to match. No-op on None."""
        if block is None or block.end_frame <= block.start_frame:
            return
        lo, hi = block.start_frame, block.end_frame
        margin = max(1.0, (hi - lo) * 0.08)
        self.set_visible_frame_window(max(0.0, lo - margin), hi + margin, emit=True)

    # ------------------------------------------------------------------
    # Coordinate mapping
    # ------------------------------------------------------------------
    def _track_rect(self):
        return QRect(0, _RULER_HEIGHT + _MARKER_ZONE, self.width(), _TRACK_HEIGHT)

    def _frame_to_x(self, frame):
        self._ensure_ppf()
        if self._ppf is None:
            return 0.0
        return self._track_rect().x() + frame * self._ppf - self._scroll_x

    def _x_to_frame(self, x):
        self._ensure_ppf()
        if self._ppf is None:
            return 0
        raw = (x - self._track_rect().x() + self._scroll_x) / self._ppf
        return int(round(max(0.0, raw)))

    # ------------------------------------------------------------------
    # Snap
    # ------------------------------------------------------------------
    def _snap_frame(self, raw_frame: float, exclude=None, enabled: bool = True):
        if not enabled:
            return raw_frame, None
        if exclude is None:
            exclude = set()
        candidates = [round(raw_frame)]
        for b in self._blocks:
            if b in exclude:
                continue
            candidates += [b.start_frame, b.end_frame]
        self._ensure_ppf()
        px_threshold = SNAP_PX / (self._ppf or 1.0)
        best = min(candidates, key=lambda c: abs(c - raw_frame))
        if abs(best - raw_frame) <= px_threshold:
            hint = "frame" if best == round(raw_frame) else "edge"
            return float(best), hint
        return raw_frame, None

    # ------------------------------------------------------------------
    # Block operations
    # ------------------------------------------------------------------
    def _max_frames(self):
        return get_max_prompt_frames(self._fps)

    def add_block(self, text="", start_frame=None, end_frame=None, block_id=None):
        if start_frame is None:
            gap_start = 0
            for b in sorted(self._blocks, key=lambda b: b.start_frame):
                if b.start_frame > gap_start:
                    break
                gap_start = max(gap_start, b.end_frame)
            start_frame = gap_start

        max_frames = self._max_frames()
        if end_frame is None:
            default_dur = max(int(self._total_frames * 0.15), 10)
            default_dur = min(default_dur, max_frames)
            end_frame = min(start_frame + default_dur, self._total_frames)
        else:
            end_frame = min(end_frame, start_frame + max_frames)

        # Clamp into the free gap around *start_frame* so the new block never
        # overlaps an existing one (covers explicit double-click / context-menu
        # adds as well as auto-placement).
        lo, hi = self._free_gap_at(start_frame, exclude=())
        if start_frame < lo:
            start_frame = lo
        if end_frame > hi:
            end_frame = hi

        if start_frame >= self._total_frames or end_frame <= start_frame:
            return

        block = PromptBlock(
            text=text,
            start_frame=start_frame,
            end_frame=end_frame,
            color_idx=self._color_counter % len(_BLOCK_COLORS),
            block_id=block_id,
        )
        self._color_counter += 1
        self._blocks.append(block)
        self.prompts_changed.emit()
        self.update()
        return block

    def remove_block(self, block):
        if block in self._blocks:
            self._blocks.remove(block)
            self._selected.discard(block)
            self.prompts_changed.emit()
            self.update()

    def remove_blocks(self, blocks):
        """Remove every block in *blocks* in one batch, emitting
        ``prompts_changed`` exactly ONCE (the batched-Delete path, Phase 4 —
        looping ``remove_block`` would rebuild/sync the host N times)."""
        removed = False
        for block in blocks:
            if block in self._blocks:
                self._blocks.remove(block)
                self._selected.discard(block)
                removed = True
        if removed:
            self.prompts_changed.emit()
            self.update()

    def clear_blocks(self):
        self._blocks.clear()
        self._selected.clear()
        self._color_counter = 0
        self.prompts_changed.emit()
        self.update()

    def previous_block(self, block):
        candidates = [b for b in self._blocks
                      if b is not block and b.end_frame <= block.start_frame]
        return max(candidates, key=lambda b: b.end_frame) if candidates else None

    def next_block(self, block):
        candidates = [b for b in self._blocks
                      if b is not block and b.start_frame >= block.end_frame]
        return min(candidates, key=lambda b: b.start_frame) if candidates else None

    def _free_gap_at(self, frame, exclude=()):
        """Return ``(lo, hi)``: the free frame interval containing *frame*,
        bounded by the nearest blocks not in *exclude*. Unlike
        ``previous_block`` / ``next_block`` this is position-based, so it also
        sees blocks that currently *overlap* the candidate (a block covering
        *frame* pushes ``lo`` to its end). Used to clamp mutations so blocks
        never overlap."""
        lo, hi = 0, self._total_frames
        for b in self._blocks:
            if b in exclude:
                continue
            if b.end_frame <= frame:
                lo = max(lo, b.end_frame)
            elif b.start_frame >= frame:
                hi = min(hi, b.start_frame)
            else:
                # b straddles *frame* -> place after it
                lo = max(lo, b.end_frame)
        return lo, hi

    def _clamp_block_to_neighbors(self, block):
        """Clamp *block* into the free gap around its start so it cannot
        overlap any other block. Safe even when *block* currently overlaps a
        neighbor (e.g. just added at an explicit frame)."""
        lo, hi = self._free_gap_at(block.start_frame, exclude={block})
        if block.start_frame < lo:
            block.start_frame = lo
        if block.end_frame > hi:
            block.end_frame = hi
        if block.end_frame <= block.start_frame:
            block.end_frame = block.start_frame + 1

    def _compute_group_move_bounds(self, selection):
        """Return ``(min_delta, max_delta)`` -- the frame range the whole
        *selection* may shift without any selected block crossing a
        non-selected neighbour. The window collapses toward 0 when a
        non-selected block is interleaved with the selection."""
        if len(selection) <= 1:
            return (-(10 ** 9), 10 ** 9)
        min_d, max_d = -(10 ** 9), 10 ** 9
        for blk in selection:
            lo, hi = self._free_gap_at(blk.start_frame, exclude=selection)
            min_d = max(min_d, lo - blk.start_frame)   # can't move start below lo
            max_d = min(max_d, hi - blk.end_frame)     # can't move end past hi
        if max_d < min_d:                              # interleaved -> no room
            max_d = min_d = 0
        return (min_d, max_d)

    def _resolve_drop_span(self, block, desired_start) -> int:
        """Return the start frame where *block*, dropped at *desired_start*
        after an unclamped single-block drag, may legally land (jump-over,
        Phase 6). A free desired span lands as-is — the same result the old
        neighbour clamp produced for a non-overlapping drop. An overlapping
        span snaps to the nearest position inside a free gap that fits the
        block's duration, considering only gaps on the drop side (the sign of
        the drag delta from ``_drag_start_frame``); when no gap on that side
        fits, the drag-start frame is returned so an overlap never persists.
        Reads the block list, mutates nothing."""
        dur     = block.end_frame - block.start_frame
        origin  = int(self._drag_start_frame)
        desired = max(0, min(int(desired_start), self._total_frames - dur))
        others  = sorted((o for o in self._blocks if o is not block),
                         key=lambda o: o.start_frame)
        if all(o.end_frame <= desired or o.start_frame >= desired + dur
               for o in others):
            return desired
        # Candidate landings: *desired* clamped into every free gap that fits
        # the duration (gap edges from the sorted neighbour spans + take ends).
        candidates = []
        cursor = 0
        for o in others:
            if o.start_frame - cursor >= dur:
                candidates.append(max(cursor, min(desired, o.start_frame - dur)))
            cursor = max(cursor, o.end_frame)
        if self._total_frames - cursor >= dur:
            candidates.append(max(cursor, min(desired, self._total_frames - dur)))
        # Keep the drop side only: dragged right → never land left of the
        # origin span (and vice versa); the origin frame itself stays legal.
        direction = desired - origin
        if direction > 0:
            candidates = [c for c in candidates if c >= origin]
        elif direction < 0:
            candidates = [c for c in candidates if c <= origin]
        if not candidates:
            return origin
        return min(candidates, key=lambda c: abs(c - desired))

    def _after_block_change(self):
        self.prompts_changed.emit()
        self.update()

    def snap_start_to_prev(self, block):
        prev = self.previous_block(block)
        if prev is None:
            return
        new_start = prev.end_frame
        if new_start >= block.end_frame:
            return
        if (block.end_frame - new_start) > self._max_frames():
            self.duration_warning.emit(f"Snap would exceed {MAX_PROMPT_SECONDS}s prompt limit.")
            return
        block.start_frame = new_start
        self._after_block_change()

    def snap_end_to_next(self, block):
        nxt = self.next_block(block)
        if nxt is None:
            return
        new_end = nxt.start_frame
        if new_end <= block.start_frame:
            return
        if (new_end - block.start_frame) > self._max_frames():
            self.duration_warning.emit(f"Snap would exceed {MAX_PROMPT_SECONDS}s prompt limit.")
            return
        block.end_frame = new_end
        self._after_block_change()

    def snap_to_prev(self, block):
        prev = self.previous_block(block)
        if prev is None:
            return
        duration = block.end_frame - block.start_frame
        new_start = prev.end_frame
        new_end = new_start + duration
        nxt = self.next_block(block)
        if nxt is not None and new_end > nxt.start_frame:
            new_end = nxt.start_frame
            new_start = max(prev.end_frame, new_end - duration)
        if new_end > self._total_frames:
            new_end = self._total_frames
            new_start = max(prev.end_frame, new_end - duration)
        if new_end <= new_start:
            return
        block.start_frame = new_start
        block.end_frame = new_end
        self._after_block_change()

    def snap_to_next(self, block):
        nxt = self.next_block(block)
        if nxt is None:
            return
        duration = block.end_frame - block.start_frame
        new_end = nxt.start_frame
        new_start = new_end - duration
        prev = self.previous_block(block)
        if prev is not None and new_start < prev.end_frame:
            new_start = prev.end_frame
            new_end = min(nxt.start_frame, new_start + duration)
        if new_start < 0:
            new_start = 0
            new_end = min(nxt.start_frame, new_start + duration)
        if new_end <= new_start:
            return
        block.start_frame = new_start
        block.end_frame = new_end
        self._after_block_change()

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------
    def _block_rect(self, block):
        track = self._track_rect()
        x1 = self._frame_to_x(block.start_frame)
        x2 = self._frame_to_x(block.end_frame)
        return QRect(int(x1), track.y() + 2, max(int(x2 - x1), 2), track.height() - 4)

    def _hit_test(self, pos):
        for b in reversed(self._blocks):
            r = self._block_rect(b)
            if not r.contains(pos):
                continue
            if b._icon_rect and b._icon_rect.contains(pos):
                return b, "icon"
            left_h  = QRect(r.x(), r.y(), _HANDLE_WIDTH, r.height())
            right_h = QRect(r.right() - _HANDLE_WIDTH, r.y(), _HANDLE_WIDTH, r.height())
            if left_h.contains(pos):
                return b, "resize_left"
            if right_h.contains(pos):
                return b, "resize_right"
            return b, "move"
        return None, None

    def _marker_at(self, pos) -> "tuple[int, str | None] | None":
        """Return ``(take-local frame, type)`` of the topmost constraint pin
        whose drawn rect (plus a small padding) contains *pos*, or ``None``.
        Rects are cached per paint in ``_marker_pin_rects`` (mirrors the
        ``block._icon_rect`` pattern), so the hit zone matches the pixels
        actually painted — including the stacked-pin Y offsets. Iterated in
        reverse so the top-of-stack (last-drawn) pin wins an overlap."""
        pad = 2
        for rect, cf, ctype in reversed(self._marker_pin_rects):
            if rect.adjusted(-pad, -pad, pad, pad).contains(pos):
                return cf, ctype
        return None

    def _near_playhead(self, pos) -> bool:
        """True when *pos* is within ``_PLAYHEAD_GRAB_PX`` of the playhead line.
        Used to give the playhead grab priority over constraint pins (a pin at
        the playhead frame must not steal the scrub) and for hover feedback."""
        ph_x = self._frame_to_x(self._current_frame - self._frame_offset)
        return abs(pos.x() - ph_x) <= _PLAYHEAD_GRAB_PX

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        self._ensure_ppf()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        track = self._track_rect()

        # --- Ruler ---
        p.fillRect(0, 0, w, _RULER_HEIGHT, QColor(styles.BG_2))

        ruler_font = QFont("Inter", 9)
        p.setFont(ruler_font)
        fm = QFontMetrics(ruler_font)
        p.setPen(QColor(styles.TEXT_3))

        eff = self._effective_total()
        step = _ruler_step(eff, w)
        for f in range(0, eff + 1, step):
            x = int(self._frame_to_x(f))
            if x < 0 or x > w:
                continue
            p.setPen(QPen(QColor(styles.BORDER_S), 1))
            p.drawLine(x, _RULER_HEIGHT - 4, x, _RULER_HEIGHT)
            p.setPen(QColor(styles.TEXT_3))
            label = str(f + self._frame_offset)
            lw = font_width(fm, label)
            p.drawText(x - lw // 2, 0, lw + 4, _RULER_HEIGHT - 5, Qt.AlignCenter, label)

        # --- Marker zone + Track (single background) ---
        full_bg = QRect(0, _RULER_HEIGHT, w, _MARKER_ZONE + _TRACK_HEIGHT)
        p.fillRect(full_bg, QColor(styles.BG_0))
        p.setPen(QPen(QColor(styles.BORDER), 1))
        p.drawRect(full_bg)

        # Subtle vertical grid lines
        p.setPen(QPen(QColor(styles.BORDER), 1, Qt.SolidLine))
        for f in range(0, eff + 1, step):
            x = int(self._frame_to_x(f))
            if 0 <= x <= w:
                grid_color = QColor(styles.BORDER)
                grid_color.setAlpha(60)
                p.setPen(QPen(grid_color, 1))
                p.drawLine(x, _RULER_HEIGHT, x, track.bottom())

        # Dim outside display_range
        if self._display_range is not None:
            d_lo, d_hi = self._display_range
            r_lo = max(0, d_lo - self._frame_offset)
            r_hi = max(r_lo, d_hi - self._frame_offset)
            x_lo = int(self._frame_to_x(r_lo))
            x_hi = int(self._frame_to_x(r_hi))
            dim = QColor(0, 0, 0, 80)
            if x_lo > 0:
                p.fillRect(0, 0, x_lo, h, dim)
            if x_hi < w:
                p.fillRect(x_hi, 0, w - x_hi, h, dim)

        # --- Dim empty track-band spans (between/around blocks) ---
        # Paint the complement of the block extents inside the track band with a
        # light dim overlay so authored regions read as raised clips against a
        # gently recessed empty timeline (the dashed gap connectors, drawn after
        # the blocks, bridge the gaps — design intent). Blocks draw on top
        # afterwards (untouched); ruler and marker zone are excluded.
        gap_dim = QColor(0, 0, 0, 40)
        covered = sorted(
            (self._block_rect(b).left(), self._block_rect(b).right())
            for b in self._blocks
        )
        cursor_x = track.left()
        for bx1, bx2 in covered:
            if bx1 > cursor_x:
                p.fillRect(cursor_x, track.top(),
                           bx1 - cursor_x, track.height(), gap_dim)
            cursor_x = max(cursor_x, bx2 + 1)
        if cursor_x < track.right():
            p.fillRect(cursor_x, track.top(),
                       track.right() - cursor_x, track.height(), gap_dim)

        # --- Blocks ---
        block_font = QFont("Inter", 10)
        block_font_small = QFont("Inter", 9)
        # Frame numbers use a mono face pinned to the block inner edges, matching
        # the design (doc/design/Pantomim to MoBu - Timeline.html — 'Geist Mono').
        frame_font = QFont("JetBrains Mono", 8)
        p.setFont(block_font)
        bfm = QFontMetrics(block_font)
        bfm_s = QFontMetrics(block_font_small)
        ffm = QFontMetrics(frame_font)

        # Jump-over drag (Phase 6): a single block mid-move drags unclamped, so
        # it may currently overlap a neighbour. While it does, paint it LAST
        # (on top) and semi-transparent so both boxes stay readable — the cue
        # that release will resolve the overlap (snap into a gap or revert).
        paint_blocks = self._blocks
        drag_float   = None
        if (self._drag_mode == "move" and self._drag_block is not None
                and len(self._move_start_spans) == 1):
            db = self._drag_block
            if any(o is not db and o.start_frame < db.end_frame
                   and o.end_frame > db.start_frame for o in self._blocks):
                drag_float   = db
                paint_blocks = [o for o in self._blocks if o is not db] + [db]

        for block in paint_blocks:
            r = self._block_rect(block)
            color = _BLOCK_COLORS[block.color_idx % len(_BLOCK_COLORS)]
            is_selected = block in self._selected
            is_hovered  = block is self._hover_block
            if block is drag_float:
                p.setOpacity(0.55)
            # Clear last frame's label hit-rects; only the wide tier re-sets them,
            # so a block that shrank out of the wide tier drops its stale rects
            # (else a double-click there would mis-open the numeric editor).
            block._start_label_rect = None
            block._end_label_rect   = None

            # Rounded body (design: border-radius 4px). Build one path and reuse
            # it for the gradient fill, the clipped accent bar, and the border so
            # every edge shares the same corner radius.
            radius = 4
            path = QPainterPath()
            path.addRoundedRect(r.x(), r.y(), r.width(), r.height(), radius, radius)

            # Gradient fill
            grad = QLinearGradient(
                QPointF(r.left(), r.top()), QPointF(r.left(), r.bottom())
            )
            alpha_top = 110 if (is_selected or is_hovered) else 85
            alpha_bot = 65  if (is_selected or is_hovered) else 50
            top_col = QColor(color)
            top_col.setAlpha(alpha_top)
            bot_col = QColor(color)
            bot_col.setAlpha(alpha_bot)
            grad.setColorAt(0, top_col)
            grad.setColorAt(1, bot_col)
            p.fillPath(path, grad)

            # 2px accent bar at top, clipped to the rounded body so its corners
            # follow the radius instead of poking out square.
            bar_color = QColor(color).lighter(140) if is_selected else color
            p.save()
            p.setClipPath(path)
            p.fillRect(QRect(r.x(), r.y(), r.width(), 2), bar_color)
            p.restore()

            # Border
            border_color = QColor(styles.ACCENT) if is_selected else QColor(color).lighter(120)
            border_w = 1.5 if is_selected else 1.0
            p.setPen(QPen(border_color, border_w))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

            # Resize grips (design makeGrip): a grey full-height well at each end
            # with two vertical bars, drawn over the existing resize hit-areas so
            # the draggable zone is visible. Only when the block is wide enough to
            # hold both grips without them overlapping.
            if r.width() > 2 * _HANDLE_WIDTH + 6:
                grips = (
                    QRect(r.left(), r.top(), _HANDLE_WIDTH, r.height()),
                    QRect(r.right() - _HANDLE_WIDTH, r.top(),
                          _HANDLE_WIDTH, r.height()),
                )
                # Fill clipped to the rounded body so outer corners follow radius.
                p.save()
                p.setClipPath(path)
                for g in grips:
                    p.fillRect(g, QColor(0, 0, 0, 50))     # ≈ rgba(0,0,0,0.20)
                p.restore()
                mark_pen = QPen(QColor(styles.TEXT_2), 1.5)
                mark_pen.setCapStyle(Qt.RoundCap)
                p.setPen(mark_pen)
                for g in grips:
                    gcx = g.center().x()
                    gy0 = g.center().y() - _GRIP_MARK_H // 2
                    gy1 = g.center().y() + _GRIP_MARK_H // 2
                    p.drawLine(gcx - 2, gy0, gcx - 2, gy1)
                    p.drawLine(gcx + 2, gy0, gcx + 2, gy1)

            # Constraint icon — 15×15 hit zone, 11×11 drawn pixmap, pinned to the
            # top-right corner (out of the vertically-centered label row so the
            # start/end frame labels can sit symmetric at the inner edges).
            icon_hit  = QRect(r.right() - 17, r.top() + 3, 15, 15)
            icon_draw = QRect(r.right() - 15, r.top() + 4, 11, 11)
            block._icon_rect = icon_hit
            if self._constraint_icon_pm and not self._constraint_icon_pm.isNull():
                p.drawPixmap(icon_draw, self._constraint_icon_pm)

            # Generation-count badge — faint "×N" pinned to the top-left corner
            # (just past the left grip well, above the vertically-centered label
            # row so it clears the frame labels and prompt). Only when the box has
            # been generated and is wide enough to hold it. (general-features #1)
            if block.generation_count > 0:
                badge = f"×{block.generation_count}"
                badge_inset = _HANDLE_WIDTH + 4
                badge_w = font_width(ffm, badge)
                if r.width() > badge_inset + badge_w + 6 and r.height() > 14:
                    p.setFont(frame_font)
                    bcol = QColor(styles.TEXT_3)
                    bcol.setAlpha(170)
                    p.setPen(bcol)
                    p.drawText(
                        QRect(r.left() + badge_inset, r.top() + 3,
                              badge_w + 2, ffm.height()),
                        Qt.AlignLeft | Qt.AlignTop, badge)

            # Text (design layout — doc/design/Pantomim to MoBu - Timeline.html):
            # start# and end# pinned just inboard of the grips at the *same* inset
            # (EDGE_PAD = grip width + 4, matching the mock's HW+4), both mono and
            # vertically centered; the prompt is centered in the span between them.
            EDGE_PAD = _HANDLE_WIDTH + 4
            sframe = str(block.start_frame + self._frame_offset)
            eframe = str(block.end_frame + self._frame_offset)
            sw = font_width(ffm, sframe)
            ew = font_width(ffm, eframe)
            lbl_h = ffm.height()
            cy    = r.center().y()
            left_x  = r.left() + EDGE_PAD
            right_x = r.right() - EDGE_PAD
            # Full layout only when both edge labels plus a legible centered
            # prompt fit; this is a *string-width* gate, not a pixel-width one, so
            # a short block can't collide its two labels (design's width>72 tier).
            if r.width() > sw + ew + 40 and r.height() > 16:
                p.setFont(frame_font)
                p.setPen(QColor(styles.TEXT_2))
                p.drawText(QRect(left_x, cy - lbl_h // 2, sw + 2, lbl_h),
                           Qt.AlignLeft | Qt.AlignVCenter, sframe)
                p.drawText(QRect(right_x - ew - 2, cy - lbl_h // 2, ew + 2, lbl_h),
                           Qt.AlignRight | Qt.AlignVCenter, eframe)
                # Full-height thin hit-strips at the inner edges (comfortable
                # double-click targets) that clear both resize grips and the
                # centered prompt region. (timline-features Phase 2)
                block._start_label_rect = QRect(left_x, r.top(), sw + 4, r.height())
                block._end_label_rect   = QRect(right_x - ew - 2, r.top(),
                                                 ew + 6, r.height())
                pl = left_x + sw + 8
                pr = right_x - ew - 8
                if pr - pl > 12 and block.text:
                    p.setFont(block_font)
                    p.setPen(QColor(styles.TEXT))
                    prompt_r = QRect(pl, r.top() + 2, pr - pl, r.height() - 4)
                    p.drawText(prompt_r, Qt.AlignCenter,
                               bfm.elidedText(block.text, Qt.ElideRight, prompt_r.width()))
                # Duration readout: total frame count at the bottom-center of the
                # block. The prompt is vertically centered, so the bottom strip is
                # free. (timline-features Phase 2, feature 4)
                p.setFont(frame_font)
                p.setPen(QColor(styles.TEXT_3))
                p.drawText(QRect(r.left(), r.bottom() - lbl_h - 1, r.width(), lbl_h),
                           Qt.AlignHCenter | Qt.AlignBottom, f"{block.duration}f")
            elif r.width() > 34:
                # Medium block: centered "start–end" range (design's width>34
                # tier), kept between the grips.
                p.setFont(frame_font)
                p.setPen(QColor(styles.TEXT_2))
                p.drawText(
                    r.adjusted(_HANDLE_WIDTH + 2, 2, -(_HANDLE_WIDTH + 2), -2),
                    Qt.AlignCenter, f"{sframe}–{eframe}")
            # else: too narrow to label — leave blank (matches the design).
            if block is drag_float:
                p.setOpacity(1.0)   # floats last in paint order; restore for overlays

        # --- Dashed gap connectors between consecutive blocks ---
        # Design (doc/design/Pantomim to MoBu - Timeline.html): a faint 1px dashed
        # line at the lane mid-line bridges the empty gap between each pair of
        # adjacent blocks. Skipped for touching/overlapping blocks and sub-4px
        # gaps. Drawn after the blocks so it never underlaps a clip.
        ordered = sorted(self._blocks, key=lambda b: b.start_frame)
        if len(ordered) > 1:
            mid_y = track.center().y()
            conn = QColor(styles.TEXT)
            conn.setAlpha(34)                      # faint — softer than the mock
            p.setPen(QPen(conn, 1, Qt.DashLine))
            for a, b in zip(ordered, ordered[1:]):
                if b.start_frame <= a.end_frame:
                    continue                       # touching/overlapping — no gap
                gl = max(int(self._frame_to_x(a.end_frame)), track.left())
                gr = min(int(self._frame_to_x(b.start_frame)), track.right())
                if gr - gl >= 4:
                    p.drawLine(gl, mid_y, gr, mid_y)

        # --- Hip keyframe dots: faint grey ticks in the constraint-marker band ---
        # A read-only overlay marking frames where the root/Hips joint is keyed
        # (a generation left a pose there). Placed in the marker zone just above
        # the blocks, where the constraint markers/icons live. Frames are
        # take-local; cull the ones scrolled off-screen (same gate the grid uses).
        # Drawn before the constraint markers so the (interactive) pins sit on top.
        if self._keyframe_dots:
            dot = QColor(styles.TEXT_3)
            dot.setAlpha(150)
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            dot_d = 3
            dot_y = track.top() - dot_d - 4
            for f in self._keyframe_dots:
                dx = int(self._frame_to_x(f))
                if dx < -dot_d or dx > w + dot_d:
                    continue
                p.drawEllipse(QRect(dx - dot_d // 2, dot_y, dot_d, dot_d))

        # --- Constraint markers: per-type color + shape, stacked vertically ---
        # 7e.5: multiple constraints on the same frame stack UPWARD at the
        # same X (was: symmetric horizontal spread that drifted away from
        # the playhead column). Bottom marker sits at the original cy;
        # each subsequent marker is _STACK_STEP px higher. Z-order =
        # insertion order, so the most recently authored marker lands on
        # top of the stack -- a small temporal cue without forcing a
        # canonical sort.
        pin = _MARKER_PIN
        half = pin // 2
        self._marker_pin_rects = []   # re-cached every paint (Phase 3 hit-test)
        if self._constraint_markers:
            # Group markers per frame in insertion order so the stack
            # index is deterministic and we can draw a single stem from
            # the lowest marker per frame.
            by_frame: dict = {}
            for f, ctype in self._constraint_markers:
                by_frame.setdefault(f, []).append(ctype)
            bottom_cy = _RULER_HEIGHT + _MARKER_ZONE - half - 4  # 4px pad above track
            for f, types in by_frame.items():
                base_x = int(self._frame_to_x(f))
                if base_x < -pin or base_x > w + pin:
                    continue
                # Stem: one tint per frame, taken from the bottom-most
                # marker (= first in insertion order). Drawing once cuts
                # paint work and avoids the per-marker stem visually
                # merging into one line of indeterminate color.
                first_color_hex, _ = _CONSTRAINT_STYLE.get(
                    types[0], _CONSTRAINT_STYLE_DEFAULT,
                )
                stem = QColor(first_color_hex)
                stem.setAlpha(70)
                p.setPen(QPen(stem, 1))
                p.drawLine(base_x, bottom_cy + half, base_x, track.bottom())
                # Now stack the markers themselves upward.
                for i, ctype in enumerate(types):
                    cy = bottom_cy - i * _STACK_STEP
                    color_hex, shape = _CONSTRAINT_STYLE.get(
                        ctype, _CONSTRAINT_STYLE_DEFAULT,
                    )
                    # Cache the drawn rect for hit-testing/hover (Phase 3);
                    # appended in draw order so _marker_at's reversed scan
                    # matches the visual top-of-stack.
                    mrect = QRect(base_x - half, cy - half, pin, pin)
                    self._marker_pin_rects.append((mrect, f, ctype))
                    col = QColor(color_hex)
                    p.setPen(QPen(QColor(styles.BG_0), 1.5))
                    p.setBrush(col)
                    if shape == "circle":
                        p.drawEllipse(mrect)
                    elif shape == "square":
                        p.drawRect(mrect)
                    elif shape == "triangle":
                        p.drawPolygon(QPolygonF([
                            QPointF(base_x,        cy - half),
                            QPointF(base_x + half, cy + half),
                            QPointF(base_x - half, cy + half),
                        ]))
                    else:  # diamond
                        p.drawPolygon(QPolygonF([
                            QPointF(base_x,        cy - half),
                            QPointF(base_x + half, cy),
                            QPointF(base_x,        cy + half),
                            QPointF(base_x - half, cy),
                        ]))
                    # Selection beats hover: a selected pin paints the stronger
                    # outline (thicker + brighter) so it reads over the 1.5px
                    # hover cue; both key off the (frame, type) identity.
                    if (f, ctype) in self._selected_markers:
                        p.setPen(QPen(QColor(styles.ACCENT_H), 2.5))
                        p.setBrush(Qt.NoBrush)
                        p.drawRect(mrect.adjusted(-2, -2, 2, 2))
                    elif self._hover_marker == (f, ctype):
                        p.setPen(QPen(QColor(styles.ACCENT), 1.5))
                        p.setBrush(Qt.NoBrush)
                        p.drawRect(mrect.adjusted(-2, -2, 2, 2))

        # --- Hover scrub line on empty track ---
        if self._hover_x is not None and self._hover_block is None:
            hx = self._hover_x
            if track.left() <= hx <= track.right():
                hover_pen = QPen(QColor(styles.TEXT_3), 1)
                hover_pen.setStyle(Qt.SolidLine)
                p.setPen(hover_pen)
                hover_col = QColor(styles.TEXT_3)
                hover_col.setAlpha(120)
                p.setPen(QPen(hover_col, 1))
                p.drawLine(hx, _RULER_HEIGHT, hx, h)
                # Frame label at bottom
                frame = self._x_to_frame(hx)
                lbl = str(frame + self._frame_offset)
                p.setFont(block_font_small)
                lbl_w = font_width(bfm_s, lbl) + 6
                lbl_r = QRect(hx - lbl_w // 2, h - 14, lbl_w, 12)
                p.fillRect(lbl_r, QColor(styles.BG_3))
                p.setPen(QColor(styles.TEXT_2))
                p.drawText(lbl_r, Qt.AlignCenter, lbl)

        # --- Playhead with glow ---
        ph_x = int(self._frame_to_x(self._current_frame - self._frame_offset))
        if 0 <= ph_x <= w:
            # Glow — widened/brightened while the cursor is within grab
            # distance so the "this click scrubs" affordance is visible.
            glow_col = QColor(styles.ACCENT)
            glow_col.setAlpha(0x66 if self._hover_playhead else 0x44)
            p.setPen(QPen(glow_col, 6 if self._hover_playhead else 4))
            p.drawLine(ph_x, _RULER_HEIGHT, ph_x, h)
            # Crisp line
            p.setPen(QPen(QColor(styles.ACCENT), 1.5))
            p.drawLine(ph_x, _RULER_HEIGHT, ph_x, h)
            # Flag on ruler
            flag_w = 34
            flag_r = QRect(ph_x - flag_w // 2, 2, flag_w, _RULER_HEIGHT - 4)
            self._playhead_flag_rect = flag_r
            flag_bg = QColor(styles.ACCENT)
            flag_bg.setAlpha(210)
            p.fillRect(flag_r, flag_bg)
            if self._hover_playhead:
                p.setPen(QPen(QColor(styles.ACCENT_H), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRect(flag_r.adjusted(-1, -1, 1, 1))
            p.setPen(QColor(styles.ON_ACCENT))
            p.setFont(QFont("JetBrains Mono", 8))
            p.drawText(flag_r, Qt.AlignCenter, str(int(self._current_frame)))
        else:
            self._playhead_flag_rect = None

        # --- Snap hint ---
        if self._snap_hint_x is not None:
            sh_x = int(self._snap_hint_x)
            if 0 <= sh_x <= w:
                p.setPen(QPen(QColor(styles.ACCENT_H), 1, Qt.DashLine))
                p.drawLine(sh_x, _RULER_HEIGHT, sh_x, h)

        p.end()

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------
    def mousePressEvent(self, event):
        pos = mouse_pos(event)
        block, mode = self._hit_test(pos)

        if event.button() == Qt.LeftButton:
            self._press_pos      = pos
            self._press_block    = block
            self._press_did_drag = False

            if mode == "icon" and block:
                mid = (block.start_frame + block.end_frame) // 2
                self.add_constraint_requested.emit(int(mid + self._frame_offset))
                event.accept()
                return

            if block:
                if block not in self._selected and not (event.modifiers() & Qt.ControlModifier):
                    self._selected = {block}
                    # A plain click replaces the WHOLE selection — pins too
                    # (unified selection, Phase 4); Ctrl stays additive.
                    self._selected_markers.clear()
                self._press_offsets    = {b: b.start_frame for b in self._selected}
                self._drag_block       = block
                self._drag_mode        = mode
                self._drag_start_pos   = pos
                self._drag_start_frame = block.start_frame
                self._drag_start_end   = block.end_frame
                # Record the pin-scale modifier (Ctrl) at resize-start; read on
                # release to drive block_resized's scale_pins flag. Ctrl at press
                # would normally toggle selection, but a resize sets
                # _press_did_drag so the deferred toggle in mouseReleaseEvent
                # never fires — repurposing it here is safe.
                self._resize_scale_pins = (
                    mode in ("resize_left", "resize_right")
                    and bool(event.modifiers() & Qt.ControlModifier)
                )
                # Snapshot drag-start spans for the whole move set so pin-follow
                # on release references the true starting span (see blocks_moved).
                self._move_start_spans = (
                    {b.id: (b.start_frame, b.end_frame) for b in self._selected}
                    if mode == "move" else {}
                )
                # For a multi-block move, compute once how far the whole
                # selection may shift without any selected block crossing a
                # *non-selected* neighbour. Positions are valid (non-overlapping)
                # at press, so this is the correct reference for clamping the
                # group delta on every move.
                self._group_move_bounds = self._compute_group_move_bounds(self._selected)
            else:
                # No block/icon hit. Left-drag scrubs the playhead at ALL
                # heights (ruler, marker zone, track body) — pan has moved to the
                # middle button so the two no longer conflict. Two exceptions:
                # a press on a constraint pin's drawn rect grabs the pin — but a
                # press within _PLAYHEAD_GRAB_PX of the playhead line always
                # scrubs, so a pin sitting at the playhead frame can't steal the
                # grab (Phase 3 priority: blocks > playhead > pins > scrub) —
                # and Ctrl+press never scrubs: on a pin it targets the pin even
                # on the playhead line (the select/toggle gesture needs no grab
                # priority), on empty area it anchors the rubber-band (Phase 4),
                # which mouseMoveEvent shows on the first drag.
                ctrl = bool(event.modifiers() & Qt.ControlModifier)
                hit = (None if (self._near_playhead(pos) and not ctrl)
                       else self._marker_at(pos))
                if hit is not None:
                    self._marker_drag_orig     = int(hit[0])
                    self._marker_drag_cur      = int(hit[0])
                    self._marker_drag_type     = hit[1]
                    self._marker_drag_snapshot = list(self._constraint_markers)
                elif ctrl:
                    self._rubber_origin = pos
                    if self._rubber is None:
                        self._rubber = QRubberBand(QRubberBand.Rectangle, self)
                else:
                    self._scrubbing = True
                    frame = self._x_to_frame(pos.x()) + self._frame_offset
                    self.scrub_requested.emit(float(frame))
                    self._current_frame = float(frame)

            self.update()
            event.accept()
            return

        if event.button() == Qt.MiddleButton:
            # Middle-drag pans the view. Reuses the existing _pan_active
            # move/release path; pan moved here from the left-ruler band so
            # left-drag can scrub everywhere (scrub-anywhere interaction model).
            self._pan_active       = True
            self._pan_start_x      = pos.x()
            self._pan_start_scroll = self._scroll_x
            self.update()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = mouse_pos(event)

        # Block drag
        if self._drag_block and self._drag_start_pos:
            self._press_did_drag = True
            dx  = pos.x() - self._drag_start_pos.x()
            ppf = self._ppf or 1.0
            b   = self._drag_block
            raw_delta = dx / ppf
            snap_enabled = not bool(event.modifiers() & QtCore.Qt.ShiftModifier)

            if self._drag_mode == "move":
                if len(self._selected) > 1:
                    raw_anchor = self._drag_start_frame + raw_delta
                    snapped, hint = self._snap_frame(raw_anchor, exclude=self._selected, enabled=snap_enabled)
                    int_delta = int(round(snapped - self._drag_start_frame))
                    # Clamp the group shift so no selected block crosses a
                    # non-selected neighbour (bounds computed at press).
                    lo_d, hi_d = self._group_move_bounds
                    int_delta = max(lo_d, min(int_delta, hi_d))
                    for blk, press_start in self._press_offsets.items():
                        dur = blk.end_frame - blk.start_frame
                        blk.start_frame = max(0, press_start + int_delta)
                        blk.end_frame   = blk.start_frame + dur
                    self._snap_hint_x = self._frame_to_x(snapped) if hint else None
                else:
                    dur = self._drag_start_end - self._drag_start_frame
                    snapped, hint = self._snap_frame(self._drag_start_frame + raw_delta, exclude={b}, enabled=snap_enabled)
                    new_start = max(0, int(round(snapped)))
                    # Alt = jump-over (Phase 6): the box follows the mouse with
                    # NO neighbour clamp — only the take bounds hold — so it can
                    # pass over other blocks (painted semi-transparent on top
                    # while overlapping). The overlap is resolved on release by
                    # _resolve_drop_span: land when free, snap to the nearest
                    # fitting gap on the drop side, or revert to the origin
                    # span. Read live per move (like the resize-push Alt), so
                    # dropping Alt mid-drag falls back to the clamped path —
                    # any leftover overlap still resolves on release.
                    if event.modifiers() & QtCore.Qt.AltModifier:
                        new_start = min(new_start, self._total_frames - dur)
                        b.start_frame = max(0, new_start)
                        b.end_frame   = b.start_frame + dur
                    else:
                        # Keep the block inside its free gap (fixed duration) so
                        # it stops at a neighbour instead of overlapping it.
                        # When the containing gap can't fit the duration (only
                        # possible if Alt was released while still overlapping a
                        # neighbour), hold position instead of shrinking the
                        # block — the release resolution sorts it out.
                        lo, hi = self._free_gap_at(b.start_frame, exclude={b})
                        if hi - lo >= dur:
                            new_start = max(lo, min(new_start, hi - dur))
                            b.start_frame = new_start
                            b.end_frame   = new_start + dur
                    self._snap_hint_x = self._frame_to_x(snapped) if hint else None

            elif self._drag_mode == "resize_left":
                max_fr = self._max_frames()
                # Alt = push the previous block's end (shrink it, 1-frame floor)
                # instead of stopping the left edge at it. No ripple: only the
                # single adjacent block is mutated, and it only shrinks.
                push = bool(event.modifiers() & QtCore.Qt.AltModifier)
                snapped, hint = self._snap_frame(self._drag_start_frame + raw_delta, exclude={b}, enabled=snap_enabled)
                new_start = max(0, min(int(round(snapped)), b.end_frame - 1))
                new_start = max(new_start, b.end_frame - max_fr)
                prev = self.previous_block(b)
                if push and prev is not None:
                    # Keep the neighbour ≥ 1 frame; push its end down to new_start.
                    new_start = max(new_start, prev.start_frame + 1)
                    if new_start < prev.end_frame:
                        prev.end_frame = new_start
                    b.start_frame = new_start
                else:
                    # Don't let the left edge cross the previous block's end.
                    lo, _hi = self._free_gap_at(b.start_frame, exclude={b})
                    new_start = max(new_start, lo)
                    b.start_frame = new_start
                self._snap_hint_x = self._frame_to_x(snapped) if hint else None

            elif self._drag_mode == "resize_right":
                max_fr = self._max_frames()
                # Alt = push the next block's start (shrink it, 1-frame floor)
                # instead of stopping the right edge at it. Symmetric to
                # resize_left above.
                push = bool(event.modifiers() & QtCore.Qt.AltModifier)
                snapped, hint = self._snap_frame(self._drag_start_end + raw_delta, exclude={b}, enabled=snap_enabled)
                new_end = max(b.start_frame + 1, min(int(round(snapped)), self._total_frames))
                new_end = min(new_end, b.start_frame + max_fr)
                nxt = self.next_block(b)
                if push and nxt is not None:
                    # Keep the neighbour ≥ 1 frame; push its start up to new_end.
                    new_end = min(new_end, nxt.end_frame - 1)
                    if new_end > nxt.start_frame:
                        nxt.start_frame = new_end
                    b.end_frame = new_end
                else:
                    # Don't let the right edge cross the next block's start.
                    _lo, hi = self._free_gap_at(b.start_frame, exclude={b})
                    new_end = min(new_end, hi)
                    b.end_frame = new_end
                self._snap_hint_x = self._frame_to_x(snapped) if hint else None

            self.prompts_changed.emit()
            self.update()
            event.accept()
            return

        # Constraint-marker drag (take-local; snaps to grid/block edges)
        if self._marker_drag_orig is not None and self._marker_drag_snapshot is not None:
            snap_enabled = not bool(event.modifiers() & QtCore.Qt.ShiftModifier)
            raw = float(self._x_to_frame(pos.x()))
            snapped, hint = self._snap_frame(raw, enabled=snap_enabled)
            new_cf = max(0, int(round(snapped)))
            self._marker_drag_cur = new_cf
            # Re-map ONLY the grabbed pin — matched on (frame, type) — to the new
            # frame from the stable press snapshot so paint reflects the drag
            # live; same-frame stack-mates stay put (Phase 3 single-pin drag).
            self._constraint_markers = [
                (new_cf if (f == self._marker_drag_orig
                            and t == self._marker_drag_type) else f, t)
                for (f, t) in self._marker_drag_snapshot
            ]
            self._snap_hint_x = self._frame_to_x(new_cf) if hint else None
            self.update()
            event.accept()
            return

        # Ruler / marker zone pan
        if self._pan_active:
            dx = pos.x() - self._pan_start_x
            self._scroll_x = self._pan_start_scroll - dx
            self._clamp_scroll()
            self.update()
            self.viewport_changed.emit()
            event.accept()
            return

        # Empty track scrub drag
        if self._scrubbing:
            frame = self._x_to_frame(pos.x()) + self._frame_offset
            self._current_frame = float(frame)
            self.scrub_requested.emit(float(frame))
            self.update()
            event.accept()
            return

        # Rubber-band selection (Ctrl+left-drag from empty area, Phase 4):
        # the selection becomes exactly what the band touches — blocks and
        # constraint pins alike. Shown lazily on the first move so a Ctrl+click
        # without drag stays a no-op (must not wipe a selection mid-build).
        if self._rubber is not None and self._rubber_origin is not None:
            r = QRect(self._rubber_origin, pos).normalized()
            self._rubber.setGeometry(r)
            if not self._rubber.isVisible():
                self._rubber.show()
            for b in self._blocks:
                if self._block_rect(b).intersects(r):
                    self._selected.add(b)
                else:
                    self._selected.discard(b)
            for prect, f, t in self._marker_pin_rects:
                if prect.intersects(r):
                    self._selected_markers.add((f, t))
                else:
                    self._selected_markers.discard((f, t))
            self.update()
            event.accept()
            return

        # Hover updates
        block, mode = self._hit_test(pos)
        need_repaint = block != self._hover_block

        # Pin + playhead hover (Phase 3). Playhead proximity wins over pins,
        # mirroring the press priority; both are suppressed over a block
        # (blocks keep top priority there too).
        hover_ph = block is None and self._near_playhead(pos)
        hover_marker = (None if (block is not None or hover_ph)
                        else self._marker_at(pos))
        if hover_ph != self._hover_playhead:
            self._hover_playhead = hover_ph
            need_repaint = True
        if hover_marker != self._hover_marker:
            self._hover_marker = hover_marker
            need_repaint = True

        # Hover line: show over the scrub area — marker zone + empty track body
        # (left-drag now scrubs at all heights). Suppressed over the ruler (it
        # has its own flag readout), over a block, and over a constraint pin
        # (a click there grabs the pin, not the playhead).
        new_hover_x = pos.x() if (
            not block and hover_marker is None and pos.y() >= _RULER_HEIGHT
        ) else None
        if new_hover_x != self._hover_x:
            self._hover_x = new_hover_x
            need_repaint = True

        if block != self._hover_block:
            self._hover_block = block
            # Block text when over a block; otherwise the modifier-hint so the
            # push-neighbour / scale-pins modifiers are discoverable.
            self.setToolTip(block.text if block and block.text else _DRAG_HINT)

        if need_repaint:
            self.update()

        if self._pan_active:
            # Middle-drag pan in progress.
            self.setCursor(Qt.ClosedHandCursor)
        elif mode in ("resize_left", "resize_right"):
            self.setCursor(Qt.SizeHorCursor)
        elif mode == "move":
            self.setCursor(Qt.OpenHandCursor)
        elif mode == "icon":
            self.setCursor(Qt.PointingHandCursor)
        elif hover_ph:
            # Within grab distance of the playhead line → a click scrubs
            # (wins over pins), signalled with the horizontal-drag cursor.
            self.setCursor(Qt.SizeHorCursor)
        elif hover_marker is not None:
            # A grabbable constraint pin under the cursor.
            self.setCursor(Qt.OpenHandCursor)
        else:
            # Everywhere else (ruler, marker zone, empty track body) scrubs.
            self.setCursor(Qt.IBeamCursor)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = mouse_pos(event)

        # Deferred Ctrl-toggle
        if (self._press_block and not self._press_did_drag
                and event.modifiers() & Qt.ControlModifier):
            if self._press_block in self._selected:
                self._selected.discard(self._press_block)
            else:
                self._selected.add(self._press_block)

        self._snap_hint_x = None

        # Finalize a constraint-marker drag. Emit absolute frames (take-local +
        # offset) like the delete path; the host updates the data model and
        # re-syncs, which is the authoritative redraw (also rolls back a move
        # the host rejects on a coexistence conflict).
        if self._marker_drag_orig is not None:
            orig_local = self._marker_drag_orig
            old_abs = self._marker_drag_orig + self._frame_offset
            new_abs = (self._marker_drag_cur if self._marker_drag_cur is not None
                       else self._marker_drag_orig) + self._frame_offset
            drag_type = self._marker_drag_type
            self._marker_drag_orig     = None
            self._marker_drag_cur      = None
            self._marker_drag_type     = None
            self._marker_drag_snapshot = None
            if new_abs != old_abs:
                self.move_constraints_requested.emit(
                    int(old_abs), int(new_abs), drag_type)
            else:
                # Release without a frame change = a click on the pin → select
                # it (Phase 4, deferred-to-release like the block Ctrl-toggle).
                # Ctrl toggles the pair in/out of the pin selection; a plain
                # click replaces BOTH selection kinds, matching block semantics.
                pair = (orig_local, drag_type)
                if event.modifiers() & Qt.ControlModifier:
                    if pair in self._selected_markers:
                        self._selected_markers.discard(pair)
                    else:
                        self._selected_markers.add(pair)
                else:
                    self._selected.clear()
                    self._selected_markers = {pair}
                    # Phase 5: a plain click also selects the pin's viewport
                    # proxy — emit the pin identity in absolute frames. Ctrl
                    # (the additive branch above) stays timeline-only so
                    # building a multi-selection can't churn the 3D selection.
                    self.constraint_clicked.emit(
                        int(orig_local + self._frame_offset), drag_type)
            self.update()
            event.accept()
            return

        if self._drag_block:
            b    = self._drag_block
            mode = self._drag_mode
            # Fire the dedicated resize signal once, carrying the true old span
            # (captured at press) and the final new span. The host scales
            # interior pins only when scale_pins is set. Emitted before
            # prompts_changed so the geometry mirror still runs afterwards; the
            # two are independent (block_resized rides old/new in its payload).
            if mode in ("resize_left", "resize_right"):
                self.block_resized.emit(
                    b.id,
                    int(self._drag_start_frame), int(self._drag_start_end),
                    int(b.start_frame), int(b.end_frame),
                    bool(self._resize_scale_pins),
                )
            elif mode == "move" and self._move_start_spans:
                # Resolve a single-block drop before diffing (Phase 6): an
                # Alt-drag runs unclamped, so an overlapping drop must snap into
                # the nearest fitting gap on the drop side — or revert to the
                # origin span — before the host sees the final geometry. A no-op
                # for clamped (plain / group) drags, whose drop is always free.
                if len(self._move_start_spans) == 1 and self._press_did_drag:
                    resolved = self._resolve_drop_span(b, b.start_frame)
                    if resolved != b.start_frame:
                        dur = b.end_frame - b.start_frame
                        b.start_frame = resolved
                        b.end_frame   = resolved + dur
                # Diff each moved block against its drag-start span; report only
                # blocks that actually shifted so the host carries their pins.
                moved = []
                for blk in self._blocks:
                    old = self._move_start_spans.get(blk.id)
                    if old is None:
                        continue
                    old_s, old_e = old
                    if blk.start_frame != old_s or blk.end_frame != old_e:
                        moved.append((blk.id, int(old_s), int(old_e),
                                      int(blk.start_frame), int(blk.end_frame)))
                if moved:
                    self.blocks_moved.emit(moved)
            self._drag_block     = None
            self._drag_mode      = None
            self._drag_start_pos = None
            self._press_offsets  = {}
            self._resize_scale_pins = False
            self._move_start_spans = {}
            self.prompts_changed.emit()
            self.update()
            event.accept()
            return

        if self._scrubbing:
            frame = self._x_to_frame(pos.x()) + self._frame_offset
            self._current_frame = float(frame)
            self.scrub_finished.emit(float(frame))
            self._scrubbing = False
            # A plain click on empty track (press+release without crossing the
            # drag threshold) also clears the selection — blocks and pins both
            # (Phase 4); an actual scrub-drag leaves it untouched. Deferred to
            # release like the Ctrl-toggle above. Ctrl is excluded — it's the
            # additive-selection modifier, so Ctrl+empty-click must not wipe a
            # selection mid-build.
            if ((self._selected or self._selected_markers)
                    and not (event.modifiers() & Qt.ControlModifier)
                    and (pos - self._press_pos).manhattanLength() <= 3):
                self._selected.clear()
                self._selected_markers.clear()
            self.update()
            event.accept()
            return

        if self._pan_active:
            self._pan_active = False
            self.update()
            # Report the panned window so the native zoom bar follows (Phase 3).
            self.visible_window_changed.emit(*self._visible_frame_window())
            event.accept()
            return

        if self._rubber is not None:
            self._rubber.hide()
        self._rubber_origin = None  # prevent ghost rubber-band on subsequent hovers

        self._press_block = None
        self.update()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        # Focus-follows-hover: grab keyboard focus when the mouse enters so the
        # hover-gated shortcuts in keyPressEvent fire even if the MoBu viewport
        # held focus. Guarded so it never yanks focus from an open inline prompt
        # editor (which would commit/close the rename mid-edit) or from any other
        # text field the user is typing in.
        if self._inline_edit is None:
            fw = QtWidgets.QApplication.focusWidget()
            if not isinstance(fw, QLineEdit):
                self.setFocus(Qt.MouseFocusReason)
        super().enterEvent(event)

    def leaveEvent(self, event):
        changed = (self._hover_x is not None
                   or self._hover_marker is not None
                   or self._hover_playhead)
        self._hover_x        = None
        self._hover_marker   = None
        self._hover_playhead = False
        if changed:
            self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        # Hover gate: consume a shortcut only when the mouse is over this tool's
        # window; otherwise pass the key through to MoBu so the timeline never
        # steals keys while the user works elsewhere. Combined with
        # focus-follows-hover (enterEvent) this makes the shortcuts fire on
        # hover regardless of which Qt widget last held focus.
        win = self.window()
        if win is not None and not win.frameGeometry().contains(QCursor.pos()):
            super().keyPressEvent(event)
            return

        key   = event.key()
        mods  = event.modifiers()
        ctrl  = bool(mods & Qt.ControlModifier)
        shift = bool(mods & Qt.ShiftModifier)
        # While the inline prompt editor is open the text-conflicting keys
        # ([ / ] and Ctrl+Z / Ctrl+Y) must type into / undo that field, not act on
        # the timeline. A focused child QLineEdit already eats them first; this is
        # a belt-and-suspenders guard.
        editing = self._inline_edit is not None

        # Delete removes the WHOLE selection — prompt blocks and constraint
        # pins — in one batch: one prompts_changed rebuild plus one
        # delete_constraints_requested payload, so the host's debounced history
        # timer folds everything into a single undo step. (The inline rename
        # editor is a focused child QLineEdit, so Delete during an edit goes
        # there.)
        if key == Qt.Key_Delete and (self._selected or self._selected_markers):
            if self._selected_markers:
                pairs = [(int(f + self._frame_offset), t) for f, t in
                         sorted(self._selected_markers,
                                key=lambda ft: (ft[0], ft[1] or ""))]
                self._selected_markers.clear()
                self.delete_constraints_requested.emit(pairs)
            if self._selected:
                self.remove_blocks(list(self._selected))
            event.accept()
            return

        # Shift+D → clear the selection, prompt blocks and constraint pins
        # alike (Phase 4). Suppressed while the inline editor is open so "D"
        # types into the prompt field instead. No-op (but still consumed)
        # when nothing is selected.
        if not editing and shift and not ctrl and key == Qt.Key_D:
            if self._selected or self._selected_markers:
                self._selected.clear()
                self._selected_markers.clear()
                self.update()
            event.accept()
            return

        # Arrows: plain ← / → nudge the selected block ∓1 (Shift ∓10); Ctrl+← / →
        # step the playhead ∓1 (Ctrl+Shift ∓10). With nothing selected, plain
        # arrows fall back to stepping the playhead (preserves the pre-rebind
        # muscle memory). Suppressed while the inline prompt editor is open so the
        # arrows move the text caret there instead.
        if key == Qt.Key_Right:
            if ctrl:
                self.step_frames(10 if shift else 1)
                event.accept()
                return
            if not editing:
                if self._selected:
                    self.nudge_selected_blocks(10 if shift else 1)
                else:
                    self.step_frames(10 if shift else 1)
                event.accept()
                return
        if key == Qt.Key_Left:
            if ctrl:
                self.step_frames(-10 if shift else -1)
                event.accept()
                return
            if not editing:
                if self._selected:
                    self.nudge_selected_blocks(-10 if shift else -1)
                else:
                    self.step_frames(-10 if shift else -1)
                event.accept()
                return

        # Home / End → snap the playhead to the selected block start / end
        # (feature 1); Shift+Home / Shift+End → snap the selected block's start /
        # end to the playhead (feature 12). Suppressed while editing (Home/End
        # move the text caret in the prompt field).
        if not editing and key == Qt.Key_Home:
            if shift:
                self.snap_block_edge_to_playhead("start")
            else:
                self.snap_playhead_to_block_edge("start")
            event.accept()
            return
        if not editing and key == Qt.Key_End:
            if shift:
                self.snap_block_edge_to_playhead("end")
            else:
                self.snap_playhead_to_block_edge("end")
            event.accept()
            return

        # Ctrl+Space → toggle MoBu play/stop (mirrors the native shortcut Qt
        # otherwise swallows while the tool window is focused).
        if ctrl and key == Qt.Key_Space:
            self.play_toggle_requested.emit()
            event.accept()
            return

        # [ / ] → prev / next authored constraint frame (suppressed while editing).
        if not editing and key == Qt.Key_BracketLeft:
            self.prev_constraint_requested.emit()
            event.accept()
            return
        if not editing and key == Qt.Key_BracketRight:
            self.next_constraint_requested.emit()
            event.accept()
            return

        # , / . → jump the playhead to the previous / next block edge (any
        # block's start or end, selection-independent — item 10). Suppressed
        # while editing (, / . type into the prompt field).
        if not editing and key == Qt.Key_Comma:
            self.jump_playhead_to_adjacent_edge(-1)
            event.accept()
            return
        if not editing and key == Qt.Key_Period:
            self.jump_playhead_to_adjacent_edge(1)
            event.accept()
            return

        # Ctrl+Z / Ctrl+Y → undo / redo timeline edits (suppressed while editing).
        if not editing and ctrl and key == Qt.Key_Z:
            self.undo_requested.emit()
            event.accept()
            return
        if not editing and ctrl and key == Qt.Key_Y:
            self.redo_requested.emit()
            event.accept()
            return

        # F → fit timeline (general fit, same as the Fit button + Fit-to-MoBu);
        # Shift+F → fit to the selected prompt block(s).
        if not ctrl and key == Qt.Key_F:
            if shift:
                self.fit_to_selected_blocks()
            else:
                self.reset_zoom()
                self.fit_to_mobu_requested.emit()
            event.accept()
            return

        # Z → zoom to the selected prompt block(s) (feature 13); Shift+Z → fit
        # zoom to all prompt blocks (feature 8). Both drive MoBu's native zoom bar
        # too via visible_window_changed. Suppressed while editing (Ctrl+Z undo is
        # already handled above; plain Z types into the prompt field).
        if not editing and not ctrl and key == Qt.Key_Z:
            if shift:
                self.fit_zoom_to_prompts()
            else:
                self.zoom_to_selected_block()
            event.accept()
            return

        # Ctrl+G → generate motion from all timeline prompts (same path as the
        # in-timeline Generate button).
        if ctrl and key == Qt.Key_G:
            self.generate_requested.emit()
            event.accept()
            return

        super().keyPressEvent(event)

    def _start_inline_edit(self, block):
        """Open a QLineEdit overlaid on *block* in place of the lost-behind-MoBu QInputDialog."""
        if self._inline_edit is not None:
            try:
                self._inline_edit.blockSignals(True)
                self._inline_edit.deleteLater()
            except Exception:
                pass
            self._inline_edit = None

        edit = QLineEdit(self)
        edit.setText(block.text)
        edit.selectAll()
        edit.setPlaceholderText("type prompt…")
        edit.setStyleSheet(
            "QLineEdit { background: #1e1e1e; color: #e0e0e0; "
            "border: 1px solid #7fbf7f; padding: 2px; border-radius: 2px; }"
        )
        brect = self._block_rect(block)
        m = 4
        edit.setGeometry(
            brect.x() + m, brect.y() + m,
            max(brect.width() - 2 * m, 80),
            brect.height() - 2 * m,
        )
        edit.show()
        edit.raise_()
        edit.setFocus(Qt.OtherFocusReason)
        self._inline_edit = edit
        # While _inline_edit is set, keyPressEvent suppresses the text-conflicting
        # shortcuts ([ / ] and Ctrl+Z / Ctrl+Y) so they type into / undo the
        # prompt field instead of acting on the timeline.

        _done = [False]
        _cancel = [False]

        def _finish(save=True):
            if _done[0]:
                return
            _done[0] = True
            if save and not _cancel[0]:
                block.text = edit.text()
                self.prompts_changed.emit()
            edit.blockSignals(True)
            try:
                edit.deleteLater()
            except Exception:
                pass
            self._inline_edit = None
            self.update()

        def _kpe(ev):
            if ev.key() == Qt.Key_Escape:
                _cancel[0] = True
                _finish(save=False)
            else:
                type(edit).keyPressEvent(edit, ev)

        edit.keyPressEvent = _kpe
        edit.returnPressed.connect(lambda: _finish(save=True))
        edit.editingFinished.connect(lambda: _finish(save=True))

    def _apply_frame_edit(self, block, edge: str, abs_value: int):
        """Set *block*'s start or end edge to *abs_value* (an ABSOLUTE frame the
        user typed), converting to take-local via ``_frame_offset`` and clamping
        exactly as ``snap_block_edge_to_playhead`` does (start<end, the
        max-prompt-duration cap, and the free gap to neighbours). Emits
        ``block_resized`` (scale_pins=False) then ``prompts_changed`` only when
        the span actually changed, so a typed resize is undoable/persistent like
        a mouse edit. (timline-features Phase 2)"""
        target = max(0, int(abs_value) - self._frame_offset)
        max_fr = self._max_frames()
        old_s, old_e = block.start_frame, block.end_frame
        lo, hi = self._free_gap_at(block.start_frame, exclude={block})
        if edge == "start":
            new_start = max(lo, min(target, block.end_frame - 1))
            new_start = max(new_start, block.end_frame - max_fr)
            block.start_frame = new_start
        else:
            new_end = min(hi, max(target, block.start_frame + 1))
            new_end = min(new_end, block.start_frame + max_fr)
            block.end_frame = new_end
        if block.start_frame != old_s or block.end_frame != old_e:
            self.block_resized.emit(
                block.id, int(old_s), int(old_e),
                int(block.start_frame), int(block.end_frame), False,
            )
            self.prompts_changed.emit()
        self.update()

    def _start_frame_edit(self, block, edge: str):
        """Open a small inline numeric editor over *block*'s start/end frame label
        so the user can type a new absolute frame to scale the box. Seeded with
        the current absolute frame and pre-selected (so it's copyable); commits on
        Enter / focus-out, cancels on Esc. Reuses the ``self._inline_edit`` slot so
        the ``keyPressEvent`` editing-guard and ``enterEvent`` focus-guard already
        cover it. (timline-features Phase 2)"""
        if self._inline_edit is not None:
            try:
                self._inline_edit.blockSignals(True)
                self._inline_edit.deleteLater()
            except Exception:
                pass
            self._inline_edit = None

        label_rect = (block._start_label_rect if edge == "start"
                      else block._end_label_rect)
        if label_rect is None:
            return
        cur_abs = ((block.start_frame if edge == "start" else block.end_frame)
                   + self._frame_offset)

        edit = QLineEdit(self)
        edit.setValidator(QtGui.QIntValidator(0, 10 ** 9, edit))
        edit.setText(str(cur_abs))
        edit.selectAll()
        edit.setAlignment(Qt.AlignCenter)
        edit.setStyleSheet(
            "QLineEdit { background: #1e1e1e; color: #e0e0e0; "
            "border: 1px solid #7fbf7f; padding: 1px; border-radius: 2px; }"
        )
        # Size for usability (the label hit-rect is only a few px wide): a
        # comfortable ~64px field, centered on the label, full label height.
        ew = 64
        cx = label_rect.center().x()
        edit.setGeometry(
            int(cx - ew // 2), label_rect.y() + 1,
            ew, max(label_rect.height() - 2, 16),
        )
        edit.show()
        edit.raise_()
        edit.setFocus(Qt.OtherFocusReason)
        self._inline_edit = edit

        _done = [False]
        _cancel = [False]

        def _finish(save=True):
            if _done[0]:
                return
            _done[0] = True
            if save and not _cancel[0]:
                try:
                    val = int(edit.text())
                except (TypeError, ValueError):
                    val = None
                if val is not None:
                    self._apply_frame_edit(block, edge, val)
            edit.blockSignals(True)
            try:
                edit.deleteLater()
            except Exception:
                pass
            self._inline_edit = None
            self.update()

        def _kpe(ev):
            if ev.key() == Qt.Key_Escape:
                _cancel[0] = True
                _finish(save=False)
            else:
                type(edit).keyPressEvent(edit, ev)

        edit.keyPressEvent = _kpe
        edit.returnPressed.connect(lambda: _finish(save=True))
        edit.editingFinished.connect(lambda: _finish(save=True))

    def mouseDoubleClickEvent(self, event):
        pos = mouse_pos(event)
        block, mode = self._hit_test(pos)
        if block and mode != "icon":
            # A double-click on an on-box frame number opens the inline numeric
            # editor for that edge; anywhere else on the body renames.
            if block._start_label_rect and block._start_label_rect.contains(pos):
                self._start_frame_edit(block, "start")
                return
            if block._end_label_rect and block._end_label_rect.contains(pos):
                self._start_frame_edit(block, "end")
                return
            self._start_inline_edit(block)
        elif not block:
            frame = self._x_to_frame(pos.x())
            new_block = self.add_block(text="", start_frame=frame)
            if new_block is not None:
                self._start_inline_edit(new_block)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            mx = wheel_x(event)
            frame_at_cursor = self._x_to_frame(int(mx))
            steps = max(-3.0, min(3.0, event.angleDelta().y() / 120.0))
            self._ensure_ppf()
            if self._ppf is not None:
                self._ppf = max(self._min_ppf(), min(200.0, self._ppf * (1.15 ** steps)))
                self._user_zoomed = True
                self._scroll_x = frame_at_cursor * self._ppf - mx
                self._clamp_scroll()
                self.update()
                self.viewport_changed.emit()
                self.visible_window_changed.emit(*self._visible_frame_window())
            event.accept()
        else:
            super().wheelEvent(event)

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos):
        block, mode = self._hit_test(pos)
        frame = self._x_to_frame(pos.x())
        abs_frame = frame + self._frame_offset

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {styles.BG_2};
                color: {styles.TEXT};
                border: 1px solid {styles.BORDER_S};
                padding: 4px 0;
            }}
            QMenu::item {{ padding: 6px 20px; }}
            QMenu::item:selected {{ background-color: {styles.ACCENT_2}; }}
        """)

        hit = self._marker_at(pos)
        if hit is not None:
            hit_frame, hit_type = hit
            type_label = (_CONSTRAINT_LABELS.get(hit_type, hit_type)
                          if hit_type else "constraint")
            act = menu.addAction(
                f"Delete {type_label} @ {hit_frame + self._frame_offset}")
            act.triggered.connect(
                lambda _=False, f=hit_frame + self._frame_offset, t=hit_type:
                self.delete_constraint_requested.emit(f, t)
            )
        else:
            act = menu.addAction(f"Add constraint @ frame {abs_frame}")
            act.triggered.connect(
                lambda _=False, f=abs_frame: self.add_constraint_requested.emit(f)
            )

        # Bulk-clear: one "Clear all <Label>" per type currently present, plus
        # "Clear all constraints" for every type. Shown only when markers exist.
        if self._constraint_markers:
            present = sorted(
                {ct for _f, ct in self._constraint_markers if ct},
                key=lambda t: _CONSTRAINT_LABELS.get(t, t),
            )
            clear_menu = menu.addMenu("Clear constraints")
            for ct in present:
                label = _CONSTRAINT_LABELS.get(ct, ct)
                a = clear_menu.addAction(f"Clear all {label}")
                a.triggered.connect(
                    lambda _=False, t=ct: self.clear_constraints_type_requested.emit(t)
                )
            clear_menu.addSeparator()
            clear_menu.addAction("Clear all constraints").triggered.connect(
                # Lambda absorbs QAction.triggered's `checked` bool; connecting it
                # straight to a zero-arg Signal().emit raises TypeError (swallowed
                # by Qt) so the signal never fires. Mirrors the per-type clear above.
                lambda _=False: self.clear_all_constraints_requested.emit()
            )

        menu.addSeparator()

        if block and mode != "icon":
            menu.addAction("Edit prompt…").triggered.connect(
                lambda: self._edit_block_text(block)
            )
            menu.addAction("Generate Selected Prompt…").triggered.connect(
                lambda: self.generate_block_requested.emit(block)
            )
            menu.addAction("Export Prompt to FBX…").triggered.connect(
                lambda _=False, b=block: self.export_block_requested.emit(b)
            )
            menu.addAction("Zoom to Prompt").triggered.connect(
                lambda _=False, b=block: self.zoom_to_block(b)
            )
            menu.addSeparator()

            has_prev = self.previous_block(block) is not None
            has_next = self.next_block(block) is not None

            a = menu.addAction("Snap Start to Previous End")
            a.setEnabled(has_prev)
            a.triggered.connect(lambda: self.snap_start_to_prev(block))

            a = menu.addAction("Snap End to Next Start")
            a.setEnabled(has_next)
            a.triggered.connect(lambda: self.snap_end_to_next(block))

            a = menu.addAction("Snap to Previous Prompt")
            a.setEnabled(has_prev)
            a.triggered.connect(lambda: self.snap_to_prev(block))

            a = menu.addAction("Snap to Next Prompt")
            a.setEnabled(has_next)
            a.triggered.connect(lambda: self.snap_to_next(block))

            menu.addSeparator()
            menu.addAction("Remove block").triggered.connect(lambda: self.remove_block(block))
        else:
            menu.addAction("Add prompt block here").triggered.connect(
                lambda: self.add_block(start_frame=frame)
            )

        menu.addSeparator()
        menu.addAction("Fit view").triggered.connect(self.reset_zoom)
        a = menu.addAction("Fit Zoom to All Prompts")
        a.setEnabled(bool(self._blocks))
        a.triggered.connect(lambda _=False: self.fit_zoom_to_prompts())
        menu.addAction("Clear all blocks").triggered.connect(self.clear_blocks)
        menu.addAction("Clear All Keyframes…").triggered.connect(
            self.clear_keyframes_requested.emit
        )

        menu_exec(menu, self.mapToGlobal(pos))

    def _edit_block_text(self, block):
        self._start_inline_edit(block)

    def sizeHint(self):
        return QSize(400, _RULER_HEIGHT + _MARKER_ZONE + _TRACK_HEIGHT + 8)


def _ruler_step(total_frames, pixel_width):
    target_ticks = max(pixel_width // 60, 4)
    raw = total_frames / target_ticks
    for nice in [1, 2, 5, 10, 15, 20, 24, 25, 30, 48, 50, 60, 100, 120, 150,
                 200, 250, 300, 500, 1000]:
        if nice >= raw:
            return nice
    return max(1, int(raw))
