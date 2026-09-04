"""Constraints card — a collapsed sub-section under Text to Motion.

Constraints steer the generation request, so the card sits inside the
"Text to Motion" group (collapsed by default) rather than the Skeleton
section. Consumes ``AppState`` read-only and emits patches via ``on_patch``;
action buttons emit higher-level signals the host wires into ``bridge``
calls.
"""

from __future__ import annotations

from ..qt_compat import QtWidgets, Signal
from ..widgets import SubSection, Pill, Field, Btn, Check, IconGrid


class ConstraintsSection(QtWidgets.QWidget):
    """Constraint authoring sub-card (nested under the timeline group)."""

    create_constraint_requested = Signal()
    from_curves_requested = Signal()
    clear_keyframes_requested = Signal()
    prev_constraint_frame_requested = Signal()
    next_constraint_frame_requested = Signal()

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)
        self._state = state
        self._on_patch = on_patch

        self._con_pill = Pill("0 active", tone="muted")
        sub = SubSection("Constraints", right=self._con_pill, open=False)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(sub)

        # Label → wire type 1:1 (matches maya_kimodo CONSTRAINT_TYPE_CHOICES).
        # The selected value IS the marker/constraint type that reaches the
        # server, so the Segment value slot must carry the wire vocabulary.
        # Per-type swatch colors mirror the timeline pins (_CONSTRAINT_STYLE in
        # gui/timeline/widget.py) and the scene markers (_VIZ_STYLE in
        # bridge/constraint_viz.py) so the selector matches what the user
        # sees on the timeline and in the viewport. Keep these hexes in sync.
        _CTYPE_COLORS = {
            "fullbody":   "#A879D0",   # purple
            "left-foot":  "#6FB7FF",   # light blue
            "right-foot": "#FF8A8A",   # light red
            "left-hand":  "#3A7BD5",   # deeper blue
            "right-hand": "#D5483A",   # deeper red
            "root2d":     "#E0A24E",   # amber (Path)
        }
        # 3×2 icon grid (Compact GUI redesign). Right Leg/Arm reuse the left
        # glyph mirrored. Tuple shape: (wire_value, label, icon_name, mirror).
        ctype = IconGrid(
            [("fullbody",   "Full Body", "c_fullbody", False),
             ("left-foot",  "Left Leg",  "c_leg",      False),
             ("right-foot", "Right Leg", "c_leg",      True),
             ("left-hand",  "Left Arm",  "c_arm",      False),
             ("right-hand", "Right Arm", "c_arm",      True),
             ("root2d",     "Path",      "c_path",     False)],
            value=state.constraint_type,
            colors=_CTYPE_COLORS,
            columns=3,
        )
        ctype.valueChanged.connect(lambda v: on_patch({"constraint_type": v}))
        sub.body_layout.addWidget(Field("Constraint type", ctype))

        # Full Body always pins the body POSE SHAPE only (joint_rotations); the
        # root is left free for the prompt / Path so the character travels and
        # reaches the pose during the animation. No root-location pin -- emitting
        # root_position from a static capture relativizes to the origin and stalls
        # locomotion. (See request_builder fullbody branch.)

        sub_btns = QtWidgets.QHBoxLayout()
        cc_btn = Btn("Add Constraint", icon_ex="c_add", variant="surface")
        cc_btn.setToolTip(
            "Capture a constraint from the current (Control-Rig-driven) pose "
            "at the playhead."
        )
        cc_btn.clicked.connect(self.create_constraint_requested.emit)
        fc_btn = Btn("Convert Animkeys", icon_ex="c_convert", variant="surface")
        fc_btn.clicked.connect(self.from_curves_requested.emit)
        sub_btns.addWidget(cc_btn, 1)
        sub_btns.addWidget(fc_btn, 1)
        sub.body_layout.addLayout(sub_btns)

        bottom = QtWidgets.QHBoxLayout()
        keep = Check("Keep constraint keyframes", checked=state.keep_keyframes)
        keep.toggled.connect(lambda v: on_patch({"keep_keyframes": v}))
        bottom.addWidget(keep, 1)
        clear_btn = Btn("Clear All Keyframes", icon="trash", variant="ghost", size="sm")
        clear_btn.clicked.connect(self.clear_keyframes_requested.emit)
        bottom.addWidget(clear_btn, 0)
        sub.body_layout.addLayout(bottom)

        # Path (root2d) marker display toggles. Viz-only -- they never touch the
        # wire; the host threads them to constraint_viz and re-draws on flip.
        show_len = Check("Show path length", checked=state.show_path_length)
        show_len.toggled.connect(lambda v: on_patch({"show_path_length": v}))
        sub.body_layout.addWidget(show_len)
        show_lbl = Check("Show marker name", checked=state.show_marker_label)
        show_lbl.toggled.connect(lambda v: on_patch({"show_marker_label": v}))
        sub.body_layout.addWidget(show_lbl)

        # Step 7e: prev/next constraint-frame nav. Jumps the playhead to the
        # adjacent authored frame (wraps around at the ends).
        nav_row = QtWidgets.QHBoxLayout()
        prev_btn = Btn("◀ Prev", icon="chevronLeft",  variant="surface", size="sm")
        next_btn = Btn("Next ▶", icon="chevronRight", variant="surface", size="sm")
        prev_btn.clicked.connect(self.prev_constraint_frame_requested.emit)
        next_btn.clicked.connect(self.next_constraint_frame_requested.emit)
        nav_row.addWidget(prev_btn, 1)
        nav_row.addWidget(next_btn, 1)
        sub.body_layout.addLayout(nav_row)

        self.refresh()

    def refresh(self) -> None:
        s = self._state
        ac = s.active_character()
        n = len(ac.constraints) if ac is not None else 0
        self._con_pill.set_text(f"{n} active")
        self._con_pill.set_tone("neutral" if n else "muted")
