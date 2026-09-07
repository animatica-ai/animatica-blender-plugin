"""01 · Skeleton — namespace, FPS, Create/HIK/Delete, and Constraints sub-card."""

from __future__ import annotations

from ..qt_compat import QtWidgets, Signal
from ..widgets import (
    CollapsibleSection, Pill, Field, Btn, TextInput, Check,
)


class SkeletonSection(QtWidgets.QWidget):
    """Numbered workflow card 01.

    ``on_patch(dict)`` is invoked with a partial-state dict whenever a
    control changes. Action buttons emit higher-level signals that the
    host wires into ``bridge`` calls.
    """

    create_skeleton_requested = Signal()
    create_hik_requested = Signal()
    link_model_rig_requested = Signal()
    adopt_skeleton_requested = Signal()
    delete_skeleton_requested = Signal()
    refresh_skeletons_requested = Signal()
    skeleton_picked = Signal(str)
    namespace_changed = Signal(str)

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)
        self._state = state
        self._on_patch = on_patch

        self._ready_pill = Pill("not created", tone="muted", dot=True)
        self._section = CollapsibleSection(
            "Skeleton", step=1, icon_ex="skeleton_fig", accent=True,
            right=self._ready_pill,
        )

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)

        body = self._section.body_layout

        # -- Existing skeleton picker --------------------------------------
        # Lists every FBModelSkeleton root in the scene so the user can
        # target an existing rig instead of always creating a new one. No
        # humanoid filter yet — every hierarchy shows up.
        pick_row = QtWidgets.QHBoxLayout()
        pick_row.setSpacing(6)
        self._skel_combo = QtWidgets.QComboBox()
        self._skel_combo.setMinimumWidth(180)
        self._skel_combo.currentTextChanged.connect(self._on_combo_changed)
        refresh_btn = Btn("Refresh", icon="folder", variant="surface", size="sm")
        refresh_btn.clicked.connect(self.refresh_skeletons_requested.emit)
        pick_row.addWidget(self._skel_combo, 1)
        pick_row.addWidget(refresh_btn, 0)
        body.addWidget(Field("Existing skeleton", None))
        body.addLayout(pick_row)

        # -- Canonical-only filter -----------------------------------------
        # Default on: show only plugin-created rigs. Patch is synchronous
        # (_apply_patch mutates state.canonical_only_filter before returning),
        # so re-emitting the refresh here reads the fresh value. The host
        # auto-falls-back to all roots when the filtered list is empty.
        canon = Check("Show canonical skeletons only",
                      checked=state.canonical_only_filter)
        canon.toggled.connect(
            lambda v: (on_patch({"canonical_only_filter": v}),
                       self.refresh_skeletons_requested.emit())
        )
        body.addWidget(canon)

        # -- Namespace row -------------------------------------------------
        # FPS now mirrors MoBu's scene transport via TimeBridge -- no manual
        # field here. The header pill shows the live rate.
        # Routed through namespace_changed (not on_patch directly) so the host
        # is the single writer of state.namespace and can mirror the value into
        # the timeline header's namespace field — kept in lock-step like the
        # skeleton selector.
        ns = TextInput(state.namespace, placeholder="animatica", mono=True)
        ns.textChanged.connect(self.namespace_changed.emit)
        self._ns_input = ns
        body.addWidget(Field("Namespace", ns))

        # -- Auto-create check ---------------------------------------------
        auto = Check("Create new skeleton on generate", checked=state.auto_create_skeleton)
        auto.toggled.connect(lambda v: on_patch({"auto_create_skeleton": v}))
        body.addWidget(auto)

        # -- Action buttons -------------------------------------------------
        btns = QtWidgets.QHBoxLayout()
        create_btn = Btn("Create Skeleton", icon="plus", variant="solid")
        create_btn.clicked.connect(self.create_skeleton_requested.emit)
        # Adopt: make the *selected* rig pipeline-ready (bind pose + hip height
        # + namespace stamped on its root). Does NOT mark it canonical, so an
        # adopted rig stays out of the canonical-only picker filter by design.
        adopt_btn = Btn("Adopt", icon="check", variant="surface")
        adopt_btn.setToolTip(
            "Store the selected skeleton's current pose as its bind pose, "
            "plus hip height and namespace."
        )
        adopt_btn.clicked.connect(self.adopt_skeleton_requested.emit)
        del_btn = Btn("Delete", icon="trash", variant="danger")
        del_btn.clicked.connect(self.delete_skeleton_requested.emit)
        btns.addWidget(create_btn, 1)
        btns.addWidget(adopt_btn, 0)
        btns.addWidget(del_btn, 0)
        body.addLayout(btns)

        # Route (B): when the scene rig is not the selected model's own
        # skeleton, the model generates on ITS rig and this one follows
        # through HIK. One button because the manual version is four
        # steps (build the source rig, characterise both, bind them).
        link_btn = Btn("Link to model rig", icon="bone", variant="surface")
        link_btn.setToolTip(
            "Build the selected model's own skeleton and drive this rig "
            "from it through HIK retargeting."
        )
        link_btn.clicked.connect(self.link_model_rig_requested.emit)
        body.addWidget(link_btn)
        self._link_btn = link_btn

        # HIK status — kept under the button row so the label can grow
        # without squeezing the buttons. Updated by the tool window via
        # set_hik_status() on combo change, after Create HIK, on show.
        self._hik_status_lbl = QtWidgets.QLabel("HIK: —")
        self._hik_status_lbl.setStyleSheet("color: #888; padding-left: 2px;")
        body.addWidget(self._hik_status_lbl)
        from ... import host
        if not host.has(host.CHARACTER_SYSTEM):
            # No HIK on this host: the link button routes through HIK
            # retargeting and the badge reports HIK state, so both would
            # only ever say "not available". Generate already covers the
            # same need with the local retarget route.
            self._link_btn.setVisible(False)
            self._hik_status_lbl.setVisible(False)

        # Constraints moved to their own card under the Prompt Timeline group
        # (see gui/sections/constraints_section.py) — they're timeline markers.

        self.refresh()

    def set_retargeting_capability(self, supported: bool) -> None:
        """No-op kept for call-site compatibility."""

    def set_hik_status(self, text: str, ok: bool, warn: bool = False) -> None:
        """Update the HIK status label.

        ok=True   → green  (HIK present)
        warn=True → amber  (skeleton selected but no HIK — actionable warning)
        default   → gray   (no skeleton selected)
        """
        self._hik_status_lbl.setText(text)
        if ok:
            color = "#7fbf7f"
        elif warn:
            color = "#e8a030"
        else:
            color = "#888"
        self._hik_status_lbl.setStyleSheet(f"color: {color}; padding-left: 2px;")

    def set_skeleton_choices(self, names: list, prefer: str | None = None) -> None:
        """Populate the picker combo with *names*; preserve selection.

        When *prefer* is given and present in *names*, it wins over the
        previously-shown combo text — used right after Create / Load to
        focus the just-built rig instead of whatever was selected before.
        """
        prev = self._skel_combo.currentText()
        self._skel_combo.blockSignals(True)
        self._skel_combo.clear()
        self._skel_combo.addItems(names)        # only real skeletons; no blank row
        target = prefer or prev or self._state.selected_skeleton_name
        if target and target in names:
            self._skel_combo.setCurrentText(target)
        else:
            # Nothing to select -> show no selection rather than auto-picking the
            # first rig the user never chose. index -1 = blank display, unpickable.
            self._skel_combo.setCurrentIndex(-1)
        self._skel_combo.blockSignals(False)

    def set_selected_skeleton(self, name: str) -> None:
        """Reflect *name* as the current selection without re-emitting.

        Used to keep this picker in lock-step with the timeline header's
        skeleton selector when the pick originated there.
        """
        self._skel_combo.blockSignals(True)
        if not name:
            self._skel_combo.setCurrentIndex(-1)   # clear; setCurrentText("") no-ops
        else:
            if self._skel_combo.findText(name) < 0:
                self._skel_combo.addItem(name)
            self._skel_combo.setCurrentText(name)
        self._skel_combo.blockSignals(False)

    def set_namespace(self, text: str) -> None:
        """Reflect *text* in the namespace field without re-emitting.

        Keeps this field in lock-step with the timeline header's namespace
        input when the edit originated there (or after a skeleton pick).
        """
        self._ns_input.blockSignals(True)
        self._ns_input.setText(text or "")
        self._ns_input.blockSignals(False)

    def _on_combo_changed(self, value: str) -> None:
        if not value:
            return
        self._on_patch({"selected_skeleton_name": value})
        self.skeleton_picked.emit(value)

    def refresh(self) -> None:
        s = self._state
        if s.skeleton_ready:
            self._ready_pill.set_tone("success")
            self._ready_pill.set_text("ready")
        else:
            self._ready_pill.set_tone("muted")
            self._ready_pill.set_text("not created")
