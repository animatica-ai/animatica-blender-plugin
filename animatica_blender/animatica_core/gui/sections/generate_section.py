"""03 · Text to Motion — prompt + General / Advanced params.

Backend selection, server URL, and cloud login moved to Section 00
(Settings) so this card focuses on the generation request itself.

Advanced parameters mirror ``maya_kimodo``'s Advanced subsection:
Num Samples, CFG Type, CFG weights (text + constraint), Transition
Frames, Initial Heading, server post-processing, Root Margin.
"""

from ..qt_compat import QtWidgets, Signal
from ..widgets import (
    CollapsibleSection, SubSection, Field, Btn, TextInput, NumberInput,
    Check, Toggle, Segment, Combo, reset_button,
)
from ... import host
from ...core.prompt_model import (AppState, available_target_modes,
                                  coerce_animation_mode)


_CFG_TYPES = ("separated", "regular", "nocfg")

# Diffusion-step quality presets: (key, label, step_count). The General combo
# drives ``state.steps`` from these. "custom" carries no fixed count (None) --
# it reveals a 1-1000 field that sets ``state.steps`` directly and soft-warns
# above the server's 500 cap.
_STEP_PRESETS = [
    ("draft",    "Draft (100)",    100),
    ("standard", "Standard (300)", 300),
    ("fine",     "Fine (500)",     500),
    ("custom",   "Custom…",        None),
]
_STEP_PRESET_VALUE = {key: n for key, _, n in _STEP_PRESETS if n is not None}
_STEP_SERVER_CAP = 500


class GenerateSection(QtWidgets.QWidget):
    generate_requested = Signal()
    refresh_takes_requested = Signal()
    create_ground_requested = Signal()
    refresh_ground_requested = Signal()
    toggle_ground_visible_requested = Signal()
    ground_y_changed = Signal(float)

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)
        self._state = state
        self._on_patch = on_patch

        self._section = CollapsibleSection(
            "Text to Motion", step=3, icon_ex="run_figure", accent=True,
        )
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)
        body = self._section.body_layout

        # ---- General sub-section -----------------------------------------
        gen_reset = reset_button()
        gen = SubSection("General", right=gen_reset, open=False)
        info_grid = QtWidgets.QGridLayout()
        info_grid.setColumnStretch(1, 1)
        info_grid.setHorizontalSpacing(8)
        info_grid.setVerticalSpacing(4)
        info_grid.addWidget(QtWidgets.QLabel("Duration"), 0, 0)
        self._duration_lbl = QtWidgets.QLabel()
        self._duration_lbl.setObjectName("duration_value")
        info_grid.addWidget(self._duration_lbl, 0, 1)
        info_grid.addWidget(QtWidgets.QLabel("Max single prompt"), 1, 0)
        max_lbl = QtWidgets.QLabel("10s")
        max_lbl.setObjectName("duration_value")
        info_grid.addWidget(max_lbl, 1, 1)
        gen.body_layout.addLayout(info_grid)

        params_row = QtWidgets.QHBoxLayout()
        self._steps_preset = Combo(
            [(key, label) for key, label, _ in _STEP_PRESETS],
            value=state.steps_preset,
        )
        self._steps_preset.valueChanged.connect(self._on_preset_changed)
        # Keep the displayed preset honest with the effective step count. A
        # saved ``steps`` that matches a fixed preset selects that preset; any
        # other value (an old 800/1000 save, or a Custom entry) selects
        # "Custom" and is shown verbatim in the revealed field, so the General
        # combo never lies about what ships.
        match = next(
            (k for k, _, n in _STEP_PRESETS if n is not None and n == state.steps),
            None,
        )
        preset = match if match is not None else "custom"
        if preset != state.steps_preset:
            state.steps_preset = preset
            self._steps_preset.blockSignals(True)
            self._steps_preset.setValue(preset)
            self._steps_preset.blockSignals(False)
        params_row.addWidget(Field("Diffusion steps", self._steps_preset), 1)
        gen.body_layout.addLayout(params_row)

        # Custom diffusion-steps override: revealed only when the preset is
        # "Custom". Absorbs the retired Advanced manual-steps control. Its value
        # sets ``state.steps`` directly; a soft warning appears above the
        # server's 500 cap (higher values may 422).
        self._steps_custom_input = NumberInput(
            state.steps, minimum=1, maximum=1000, mono=True,
        )
        self._steps_custom_input.valueChanged.connect(self._on_custom_steps_changed)
        self._steps_custom_row = Field("Custom diffusion steps", self._steps_custom_input)
        self._steps_custom_row.setVisible(state.steps_preset == "custom")
        gen.body_layout.addWidget(self._steps_custom_row)

        self._steps_warn_lbl = QtWidgets.QLabel(
            f"Server caps diffusion steps at {_STEP_SERVER_CAP}; "
            "higher values may be rejected."
        )
        self._steps_warn_lbl.setObjectName("field_hint")
        self._steps_warn_lbl.setWordWrap(True)
        self._steps_warn_lbl.setVisible(
            state.steps_preset == "custom" and state.steps > _STEP_SERVER_CAP
        )
        gen.body_layout.addWidget(self._steps_warn_lbl)

        seed_row = QtWidgets.QHBoxLayout()
        seed_input = NumberInput(state.seed, minimum=0, maximum=2**31 - 1, mono=True)
        seed_input.valueChanged.connect(lambda v: on_patch({"seed": int(v)}))
        seed_input.setEnabled(not state.random_seed)
        seed_row.addWidget(seed_input, 1)
        rand = Check("Random", checked=state.random_seed)
        rand.toggled.connect(
            lambda v: (on_patch({"random_seed": v}), seed_input.setEnabled(not v))
        )
        seed_row.addWidget(rand, 0)
        gen.body_layout.addWidget(Field("Seed", None))
        gen.body_layout.addLayout(seed_row)

        def _reset_general():
            d = AppState()
            self._steps_preset.blockSignals(True)
            self._steps_preset.setValue(d.steps_preset)
            self._steps_preset.blockSignals(False)
            self._steps_custom_input.blockSignals(True)
            self._steps_custom_input.setValue(d.steps)
            self._steps_custom_input.blockSignals(False)
            self._steps_custom_row.setVisible(d.steps_preset == "custom")
            self._steps_warn_lbl.setVisible(False)
            seed_input.blockSignals(True)
            seed_input.setValue(d.seed)
            seed_input.blockSignals(False)
            seed_input.setEnabled(not d.random_seed)
            rand.blockSignals(True)
            rand.setChecked(d.random_seed)
            rand.blockSignals(False)
            # 'model' is server-driven (populated from /capabilities); leave it.
            self._on_patch({
                "steps_preset": d.steps_preset, "steps": d.steps,
                "seed": d.seed, "random_seed": d.random_seed,
            })

        gen_reset.clicked.connect(_reset_general)
        body.addWidget(gen)

        # ---- Advanced sub-section (maya_kimodo parity) -------------------
        # The header's ``right=`` slot must carry BOTH the enable toggle and a
        # reset button; wrap them in a small row (SubSection takes a single
        # ``right`` widget). Reset restores the Advanced *parameters* only — it
        # deliberately leaves ``advanced_on`` (the toggle / body visibility)
        # untouched so a reset doesn't collapse the panel the user is editing.
        adv_toggle = Toggle(checked=state.advanced_on)
        adv_reset = reset_button()
        adv_right = QtWidgets.QWidget()
        adv_right_row = QtWidgets.QHBoxLayout(adv_right)
        adv_right_row.setContentsMargins(0, 0, 0, 0)
        adv_right_row.setSpacing(6)
        adv_right_row.addWidget(adv_reset)
        adv_right_row.addWidget(adv_toggle)
        self._adv = SubSection("Advanced", right=adv_right, open=False)
        adv_toggle.toggled.connect(lambda v: (on_patch({"advanced_on": v}),
                                              self._adv_body.setVisible(v)))

        self._adv_body = QtWidgets.QWidget()
        adv_inner = QtWidgets.QVBoxLayout(self._adv_body)
        adv_inner.setContentsMargins(0, 0, 0, 0)
        adv_inner.setSpacing(8)

        # Row: Num Samples + CFG Type
        r1 = QtWidgets.QHBoxLayout()
        ns = NumberInput(state.num_samples, minimum=1, maximum=8, mono=True)
        self._num_samples_input = ns
        ns.valueChanged.connect(lambda v: on_patch({"num_samples": int(v)}))
        r1.addWidget(Field("Num samples", ns), 1)
        cfg_seg = Segment(
            [(t, t.title()) for t in _CFG_TYPES],
            value=state.cfg_type,
        )
        cfg_seg.valueChanged.connect(lambda v: on_patch({"cfg_type": v}))
        r1.addWidget(Field("CFG type", cfg_seg), 1)
        adv_inner.addLayout(r1)

        # Row: CFG weights
        r2 = QtWidgets.QHBoxLayout()
        cw_text = NumberInput(state.cfg_text_weight, minimum=0.0, maximum=10.0, step=0.1, mono=True)
        cw_text.valueChanged.connect(lambda v: on_patch({"cfg_text_weight": float(v)}))
        r2.addWidget(Field("CFG weight (text)", cw_text), 1)
        cw_con = NumberInput(state.cfg_constraint_weight, minimum=0.0, maximum=10.0, step=0.1, mono=True)
        cw_con.valueChanged.connect(lambda v: on_patch({"cfg_constraint_weight": float(v)}))
        r2.addWidget(Field("CFG weight (constraint)", cw_con), 1)
        adv_inner.addLayout(r2)

        # Row: Transition frames + Initial heading
        r3 = QtWidgets.QHBoxLayout()
        tf = NumberInput(state.transition_frames, minimum=0, maximum=30, mono=True)
        tf.valueChanged.connect(lambda v: on_patch({"transition_frames": int(v)}))
        r3.addWidget(Field("Transition frames", tf), 1)
        hd = NumberInput(state.heading_deg, minimum=0.0, maximum=360.0, step=15.0, mono=True)
        hd.valueChanged.connect(lambda v: on_patch({"heading_deg": float(v)}))
        r3.addWidget(Field("Initial heading (deg)", hd), 1)
        adv_inner.addLayout(r3)

        # ---- Server post-processing --------------------------------------
        # The real server flag (``options.post_processing``), ON by default.
        # This replaced a "Foot cleanup" checkbox that drove a client-side
        # pinning pass on ``posed_joints`` -- which the FK-only apply never
        # reads, so it was a no-op mislabelled "server-side". Root margin is a
        # post-processing SUB-parameter, so it sits under this box and greys out
        # with it.
        pp_sep = QtWidgets.QFrame()
        pp_sep.setFrameShape(QtWidgets.QFrame.HLine)
        pp_sep.setObjectName("section_separator")
        adv_inner.addWidget(pp_sep)

        pp_chk = Check(
            "Server post-processing",
            checked=state.post_processing,
            sublabel="Run the server's own post-processing pass (root-drift "
                     "correction) on the sampled motion.",
        )
        pp_chk.toggled.connect(lambda v: (
            on_patch({"post_processing": v}),
            self._root_margin_row.setEnabled(v),
        ))
        adv_inner.addWidget(pp_chk)

        # Root margin: horizontal slack the server's post-processing uses when
        # deciding whether to correct root drift. Collected and persisted but
        # NEVER sent -- unwired in maya_kimodo too (main_window.py:1719
        # TODO(backend)); sending a speculative field risks a 422.
        rm = NumberInput(state.root_margin, minimum=0.0, maximum=0.2, step=0.01, mono=True)
        rm.valueChanged.connect(lambda v: on_patch({"root_margin": float(v)}))
        self._root_margin_row = Field("Root margin (m)", rm)
        self._root_margin_row.setEnabled(state.post_processing)
        adv_inner.addWidget(self._root_margin_row)

        self._adv_body.setVisible(state.advanced_on)
        self._adv.body_layout.addWidget(self._adv_body)

        def _reset_advanced():
            d = AppState()
            for w, val in ((ns, d.num_samples), (cw_text, d.cfg_text_weight),
                           (cw_con, d.cfg_constraint_weight),
                           (tf, d.transition_frames), (hd, d.heading_deg),
                           (rm, d.root_margin)):
                w.blockSignals(True)
                w.setValue(val)
                w.blockSignals(False)
            cfg_seg.blockSignals(True)
            cfg_seg.setValue(d.cfg_type)
            cfg_seg.blockSignals(False)
            pp_chk.blockSignals(True)
            pp_chk.setChecked(d.post_processing)
            pp_chk.blockSignals(False)
            self._root_margin_row.setEnabled(d.post_processing)
            self._on_patch({
                "num_samples": d.num_samples, "cfg_type": d.cfg_type,
                "cfg_text_weight": d.cfg_text_weight,
                "cfg_constraint_weight": d.cfg_constraint_weight,
                "transition_frames": d.transition_frames,
                "heading_deg": d.heading_deg, "root_margin": d.root_margin,
                "post_processing": d.post_processing,
            })

        adv_reset.clicked.connect(_reset_advanced)
        body.addWidget(self._adv)

        # Prompts now live in Section 06 (Prompt Timeline). The Generate
        # button below builds MMCP segments from the active character's
        # timeline blocks.

        # ---- Output Target sub-section -----------------------------------
        target_reset = reset_button()
        target = SubSection("Output Target", right=target_reset)
        # Story exists only where the host has a Story timeline. 3ds Max has
        # none -- `apply_to_target` refuses the mode outright -- so offering it
        # here would sell a button whose only possible outcome is an error.
        # Asked, never inferred: the host declares its own capabilities.
        self._has_story = host.has(host.STORY)
        self._has_takes = host.has(host.TAKES)
        modes = available_target_modes(self._has_story, self._has_takes)
        # A settings file written before this check existed -- or carried over
        # from a host that does have Story -- can still say "story", and the
        # Segment would then hold a value it has no button for.
        mode_value = coerce_animation_mode(state.animation_mode,
                                           self._has_story, self._has_takes)
        if mode_value != state.animation_mode:
            # Direct mutation, NOT on_patch: this runs inside the section
            # constructor, and the host's patch handler refreshes sibling
            # sections that do not exist yet at that point -- routing the
            # coercion through it took the whole window down with an
            # AttributeError on a section built two lines later. The state
            # object is live; persistence rides on the next ordinary save.
            state.animation_mode = mode_value
        self._mode_seg = Segment(modes, value=mode_value)
        # One mode is not a choice. A host without takes has exactly Replace,
        # and a picker with a single button would only imply alternatives
        # exist somewhere. Built either way -- handlers address it by name.
        self._mode_field = Field("Mode", self._mode_seg)

        def _on_mode_changed(v):
            on_patch({"animation_mode": v})
            self._refresh_target_rows()
            if v == "existing_take":
                self.refresh_takes_requested.emit()

        self._mode_seg.valueChanged.connect(_on_mode_changed)
        target.body_layout.addWidget(self._mode_field)
        if len(modes) < 2:
            self._mode_field.setVisible(False)

        # Story-only placement modifiers. Both rows live in one container so
        # mode-visibility toggling stays a single call in
        # ``_refresh_target_rows``. Note the intentional split-brain: the story
        # export path itself lives in Section 00 (Settings) while these two
        # modifiers sit here — deliberate, not an oversight.
        self._story_container = QtWidgets.QWidget()
        st_layout = QtWidgets.QVBoxLayout(self._story_container)
        st_layout.setContentsMargins(0, 0, 0, 0)
        st_layout.setSpacing(6)

        self._passthrough_chk = Check(
            "Passthrough",
            checked=state.story_passthrough,
            sublabel="Gaps between clips show the underlying take's animation.",
        )
        self._passthrough_chk.toggled.connect(
            lambda v: on_patch({"story_passthrough": v})
        )
        st_layout.addWidget(self._passthrough_chk)

        self._overwrite_fbx_chk = Check(
            "Replace current Clip",
            checked=state.story_overwrite_fbx,
            sublabel="Overwrite the newest version in place instead of adding a new one.",
        )
        self._overwrite_fbx_chk.toggled.connect(
            lambda v: on_patch({"story_overwrite_fbx": v})
        )
        st_layout.addWidget(self._overwrite_fbx_chk)
        target.body_layout.addWidget(self._story_container)
        if not self._has_story:
            # Built and then hidden for good, rather than not built: the
            # refresh and the per-group reset both address it by name, and a
            # missing attribute would turn one absent capability into an
            # AttributeError somewhere unrelated.
            self._story_container.setVisible(False)

        # New-take naming — a checkbox is the primary control. When on
        # (default), the take is named from the first 30 chars of the
        # earliest timeline prompt with a _v001/_v002… suffix on repeats;
        # the custom input is hidden. When off, the custom Take name input
        # is revealed. Both rows live in one container so mode-visibility
        # toggling stays a single call in ``_refresh_target_rows``.
        self._new_take_container = QtWidgets.QWidget()
        nt_layout = QtWidgets.QVBoxLayout(self._new_take_container)
        nt_layout.setContentsMargins(0, 0, 0, 0)
        nt_layout.setSpacing(6)

        self._auto_name_chk = Check(
            "Auto-name from first prompt",
            checked=state.auto_take_name,
            sublabel="First N chars of the earliest prompt; adds _001 on repeat.",
        )
        nt_layout.addWidget(self._auto_name_chk)

        # The auto-name length lives in Section 00 (Settings); see
        # settings_section.py "Auto take-name length".

        self._take_name_input = TextInput(
            state.take_name,
            placeholder="Custom take name…",
            mono=True,
        )
        self._take_name_input.textChanged.connect(lambda v: on_patch({"take_name": v}))
        self._custom_name_row = Field("Take name", self._take_name_input)
        self._custom_name_row.setVisible(not state.auto_take_name)
        nt_layout.addWidget(self._custom_name_row)

        def _on_auto_name_toggle(v):
            on_patch({"auto_take_name": v})
            self._custom_name_row.setVisible(not v)

        self._auto_name_chk.toggled.connect(_on_auto_name_toggle)
        target.body_layout.addWidget(self._new_take_container)
        if not self._has_takes:
            # No takes: nothing to name. Hidden for good; the mode is pinned
            # to Replace, so _refresh_target_rows never re-shows it.
            self._new_take_container.setVisible(False)

        # Existing-take row: READ-ONLY display of MoBu's current take (P4,
        # item 11). Generate snapshots the current take at the click, so an
        # editable combo could only lie about the target; this mirror shows
        # what will be written into. Refresh re-reads the name via the host
        # (also pushed on the mode switch to existing_take and on first show).
        # No state patch — the display never drives the target.
        ex_inner = QtWidgets.QWidget()
        ex_layout = QtWidgets.QHBoxLayout(ex_inner)
        ex_layout.setContentsMargins(0, 0, 0, 0)
        ex_layout.setSpacing(6)
        self._current_take_display = TextInput("", placeholder="(current take)", mono=True)
        self._current_take_display.setReadOnly(True)
        refresh_btn = Btn("Refresh", icon="folder", variant="surface", size="sm")
        refresh_btn.clicked.connect(self.refresh_takes_requested.emit)
        ex_layout.addWidget(self._current_take_display, 1)
        ex_layout.addWidget(refresh_btn, 0)
        self._existing_row = Field("Current take", ex_inner)
        target.body_layout.addWidget(self._existing_row)
        if not self._has_takes:
            # "Current take" mirrors a concept the host does not have; the
            # timeline mode label says "Replace" instead.
            self._existing_row.setVisible(False)

        # Placement group: re-seat the generated (origin-canonicalised) motion to
        # the character's scene position at the prompt start frame. "Use
        # current position" owns XZ + yaw only (offset fold + apply re-add).
        # "Preserve height" is the Y analog but SERVER-SIDE (item-1
        # redefinition): it creates no offset — instead the rig's current hip
        # height is sent to the model as a Hips-only start anchor
        # (plane-relative on the wire), so the motion is generated at that
        # height. See tool_window._on_generate (anchor) / _compute_reseat
        # (ground-only off_y). The label deliberately dropped "offset" — there
        # is none any more — and the switch is now INDEPENDENT of "Use current
        # position" (it used to be gated out when that was off).
        placement_reset = reset_button()
        placement = SubSection("Placement", right=placement_reset)
        hip = Check("Use current position", checked=state.use_hip_pos,
                    sublabel="Start from the skeleton's scene XZ, not the origin")
        hip.toggled.connect(lambda v: on_patch({"use_hip_pos": v}))
        placement.body_layout.addWidget(hip)
        ph = Check("Preserve height", checked=state.preserve_height,
                   sublabel="Send the current hip height to the model")
        ph.toggled.connect(lambda v: on_patch({"preserve_height": v}))
        placement.body_layout.addWidget(ph)
        target.body_layout.addWidget(placement)

        # Ground group: a movable ground-plane scene object whose live world-Y
        # is folded into the vertical offset as an additive base, so applied /
        # generated motion stands at ``ground_Y + hip_height`` (see the ground
        # fold in tool_window._on_generate). "Create Ground" spawns or finds the
        # plane; the mirror field reads the plane's Y on refresh and writes it
        # back on edit. The PLANE is the source of truth — this field is a
        # derived display cache, disabled until a plane exists. Sync is
        # refresh-in / edit-out, never real-time (no viewport-drag callback):
        # Refresh pulls the plane's live Y into the field (no write-back), and
        # editing the field value moves the plane. Hide/Show toggles plane
        # visibility; its label reflects the plane's true visibility, driven by
        # the host.
        ground_grp = SubSection("Ground")
        self._create_ground_btn = Btn(
            "Create Ground", icon="plus", variant="surface", size="sm",
        )
        self._create_ground_btn.clicked.connect(self.create_ground_requested.emit)
        self._refresh_ground_btn = Btn("Refresh", variant="ghost", size="sm")
        self._refresh_ground_btn.clicked.connect(self.refresh_ground_requested.emit)
        self._ground_vis_btn = Btn("Hide", variant="ghost", size="sm")
        self._ground_vis_btn.clicked.connect(self.toggle_ground_visible_requested.emit)
        ground_btn_row = QtWidgets.QHBoxLayout()
        ground_btn_row.setContentsMargins(0, 0, 0, 0)
        ground_btn_row.setSpacing(6)
        ground_btn_row.addWidget(self._create_ground_btn)
        ground_btn_row.addWidget(self._refresh_ground_btn)
        ground_btn_row.addWidget(self._ground_vis_btn)
        ground_btn_row.addStretch(1)
        ground_grp.body_layout.addLayout(ground_btn_row)
        self._ground_y_input = NumberInput(
            0.0, minimum=-1000.0, maximum=1000.0, step=0.1, mono=True,
        )
        # Overwrite the plane live as the value changes. Refresh-in
        # (``set_ground_y``) goes through ``blockSignals``, so a programmatic
        # refresh never trips this and reverts the user's edit.
        self._ground_y_input.valueChanged.connect(self._on_ground_y_edited)
        ground_grp.body_layout.addWidget(Field("Ground height (m)", self._ground_y_input))
        # Master switch for the ground FOLD (generation math, all modes — story
        # included). Policy, not a plane control: stays enabled with no plane
        # (set_ground_y disables only the field/buttons above), and the viz
        # riding the plane is unaffected either way.
        go = Check("Apply ground offset", checked=state.ground_offset_enabled,
                   sublabel="Fold the plane's height into generated motion (all modes)")
        go.toggled.connect(lambda v: on_patch({"ground_offset_enabled": v}))
        ground_grp.body_layout.addWidget(go)
        # "Correct ground offset" (the gated server-float correction) lives in
        # Settings → Extra options — it is plane-independent policy, and its
        # near-identical label next to the fold switch above misled an A/B.
        target.body_layout.addWidget(ground_grp)
        self.set_ground_y(None)   # no plane yet → field + buttons disabled at 0.0

        # Layer & Rig: the two post-apply key-placement / characterization
        # toggles, grouped so they read as one concern rather than two loose
        # checkboxes at the bottom of Output Target.
        layer_rig_reset = reset_button()
        layer_rig = SubSection("Layer & Rig", right=layer_rig_reset)

        # Force generated keys onto the take's base layer regardless of the
        # active layer. Key-placement only — additive upper layers still show.
        bl = Check("Generate on base layer only", checked=state.base_layer_only,
                   sublabel="Key onto the base layer, not the active animation layer")
        bl.toggled.connect(lambda v: on_patch({"base_layer_only": v}))
        layer_rig.body_layout.addWidget(bl)

        # After apply, plot the generated skeleton motion onto the HIK Control
        # Rig and re-enable the HIK input source, leaving a characterized,
        # hand-editable result. No-op without an HIK character or in story mode.
        bake_chk = Check("Bake to Control Rig after import", checked=state.bake_to_control_rig,
                         sublabel="Plot generated motion onto the HIK Control Rig, then re-enable input")
        # on_patch → _apply_patch → refresh() → _refresh_target_rows re-gates the
        # "whole time range" box below, so no explicit re-gate is needed here.
        bake_chk.toggled.connect(lambda v: on_patch({"bake_to_control_rig": v}))
        layer_rig.body_layout.addWidget(bake_chk)
        # Promoted to an instance attr so _refresh_target_rows can grey it out in
        # Story mode (bake is a runtime no-op there — motion lives in a
        # passthrough clip, not on skeleton keys). Disable only; the checked
        # value is preserved for when the user leaves Story mode.
        self._bake_chk = bake_chk

        # Opt out of per-block bake scoping: plot the whole take instead of only
        # the span just applied. Modifies the bake above, so it's dead while that
        # is unchecked — gated in _refresh_target_rows on both flags.
        whole_chk = Check("Bake whole time range", checked=state.bake_whole_range,
                          sublabel="Plot the entire take, not just the applied block's frames")
        whole_chk.toggled.connect(lambda v: on_patch({"bake_whole_range": v}))
        layer_rig.body_layout.addWidget(whole_chk)
        self._bake_whole_chk = whole_chk
        if not host.has(host.CONTROL_RIG):
            # Both bake boxes plot onto an HIK Control Rig. Without a
            # character system the plot is a stub, and a checkbox that
            # silently does nothing teaches the user not to trust the rest.
            bake_chk.setVisible(False)
            whole_chk.setVisible(False)
        target.body_layout.addWidget(layer_rig)

        # Reset handlers for the Output Target cluster. Each is scoped to the
        # fields its own group's controls edit; the nested Placement / Layer &
        # Rig cards get their own resets (they own separate fields).
        def _reset_output_target():
            d = AppState()
            self._mode_seg.blockSignals(True)
            self._mode_seg.setValue(d.animation_mode)
            self._mode_seg.blockSignals(False)
            self._auto_name_chk.blockSignals(True)
            self._auto_name_chk.setChecked(d.auto_take_name)
            self._auto_name_chk.blockSignals(False)
            self._take_name_input.blockSignals(True)
            self._take_name_input.setText(d.take_name)
            self._take_name_input.blockSignals(False)
            self._passthrough_chk.blockSignals(True)
            self._passthrough_chk.setChecked(d.story_passthrough)
            self._passthrough_chk.blockSignals(False)
            self._overwrite_fbx_chk.blockSignals(True)
            self._overwrite_fbx_chk.setChecked(d.story_overwrite_fbx)
            self._overwrite_fbx_chk.blockSignals(False)
            self._on_patch({
                "animation_mode": d.animation_mode, "take_name": d.take_name,
                "auto_take_name": d.auto_take_name,
                "story_passthrough": d.story_passthrough,
                "story_overwrite_fbx": d.story_overwrite_fbx,
            })
            self._refresh_target_rows()

        def _reset_placement():
            d = AppState()
            hip.blockSignals(True)
            hip.setChecked(d.use_hip_pos)
            hip.blockSignals(False)
            ph.blockSignals(True)
            ph.setChecked(d.preserve_height)
            ph.blockSignals(False)
            self._on_patch({
                "use_hip_pos": d.use_hip_pos, "preserve_height": d.preserve_height,
            })

        def _reset_layer_rig():
            d = AppState()
            bl.blockSignals(True)
            bl.setChecked(d.base_layer_only)
            bl.blockSignals(False)
            bake_chk.blockSignals(True)
            bake_chk.setChecked(d.bake_to_control_rig)
            bake_chk.blockSignals(False)
            whole_chk.blockSignals(True)
            whole_chk.setChecked(d.bake_whole_range)
            whole_chk.blockSignals(False)
            self._on_patch({
                "base_layer_only": d.base_layer_only,
                "bake_to_control_rig": d.bake_to_control_rig,
                "bake_whole_range": d.bake_whole_range,
            })

        target_reset.clicked.connect(_reset_output_target)
        placement_reset.clicked.connect(_reset_placement)
        layer_rig_reset.clicked.connect(_reset_layer_rig)

        body.addWidget(target)
        self._refresh_target_rows()

        # ---- Generate button ---------------------------------------------
        self._gen_btn = Btn("Generate Motion", icon="spark", variant="solid", size="lg")
        self._gen_btn.clicked.connect(self.generate_requested.emit)
        body.addWidget(self._gen_btn)

        # ---- Generation progress row -------------------------------------
        # An indeterminate QProgressBar(range 0,0) as an honest spinner + a
        # phase/timing status label. Added to ``outer`` (a SIBLING of the
        # collapsible section), NOT into ``body`` — ``set_busy`` disables the
        # section during a run, and a QProgressBar greyed by a disabled ancestor
        # is not guaranteed to keep animating. Kept outside that subtree it stays
        # full-colour and live for the whole async run. Hidden when idle.
        self._progress_row = QtWidgets.QWidget()
        pr = QtWidgets.QVBoxLayout(self._progress_row)
        pr.setContentsMargins(0, 6, 0, 0)
        pr.setSpacing(3)
        self._progress_lbl = QtWidgets.QLabel()
        self._progress_lbl.setObjectName("duration_value")
        self._progress_lbl.setWordWrap(True)
        pr.addWidget(self._progress_lbl)
        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setObjectName("total_progress")
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setRange(0, 0)   # indeterminate — the spinner
        pr.addWidget(self._progress_bar)
        # Elapsed-time readout UNDER the bar (status text sits above it) —
        # ticked once per second by the host's clock during a run.
        self._elapsed_lbl = QtWidgets.QLabel()
        self._elapsed_lbl.setObjectName("duration_value")
        pr.addWidget(self._elapsed_lbl)
        self._progress_row.setVisible(False)
        outer.addWidget(self._progress_row)

        self.refresh()

    # ------------------------------------------------------------------
    # Generation progress (driven from the host's busy/progress slots)
    # ------------------------------------------------------------------

    def set_busy(self, busy: bool) -> None:
        """Show/hide the spinner + status row and gate the interactive body.

        Disables ``self._section`` (so prompt/param edits can't corrupt the
        in-flight request) while leaving the progress row — its sibling —
        enabled and animating.
        """
        self._section.setEnabled(not busy)
        self._progress_row.setVisible(busy)
        if busy:
            self._progress_lbl.setText("")
            self._elapsed_lbl.setText("")

    def set_progress(self, msg: str) -> None:
        """Update the phase/timing status label (no-op on the spinner)."""
        self._progress_lbl.setText(msg or "")

    def set_elapsed(self, text: str) -> None:
        """Update the elapsed-time readout under the spinner."""
        self._elapsed_lbl.setText(text or "")

    def add_body_widget(self, w: "QtWidgets.QWidget") -> None:
        """Append an external widget into the Text to Motion body, just above
        the Generate Motion button (so it lands after Output Target). Used by
        the host to host the relocated Constraints card inside this group."""
        body = self._section.body_layout
        idx = body.indexOf(self._gen_btn)
        if idx < 0:
            body.addWidget(w)
        else:
            body.insertWidget(idx, w)

    def refresh(self) -> None:
        s = self._state
        secs = s.duration / max(1.0, s.fps)
        self._duration_lbl.setText(f"{s.duration} frames · {secs:.2f}s")
        # Keep the Mode segment + target-row visibility in sync with state
        # on every refresh, so any path that mutates animation_mode (load
        # from JSON, programmatic set, etc.) is reflected without relying
        # solely on the segment's own clicked signal.
        if self._mode_seg.value() != s.animation_mode:
            self._mode_seg.setValue(s.animation_mode)
        self._refresh_target_rows()
        # Re-apply the last-known ground height so any refresh path keeps the
        # mirror field consistent with the plane's cached world-Y.
        self.set_ground_y(self._ground_y)

    # ------------------------------------------------------------------
    # Diffusion steps (preset)
    # ------------------------------------------------------------------

    def _on_preset_changed(self, key: str) -> None:
        if key == "custom":
            # Reveal the field, seeding it with the current effective count so
            # the step total stays continuous across the switch, and adopt that
            # value as ``state.steps``.
            val = int(self._state.steps)
            self._steps_custom_input.blockSignals(True)
            self._steps_custom_input.setValue(val)
            self._steps_custom_input.blockSignals(False)
            self._steps_custom_row.setVisible(True)
            self._steps_warn_lbl.setVisible(val > _STEP_SERVER_CAP)
            self._on_patch({"steps_preset": key, "steps": val})
        else:
            self._steps_custom_row.setVisible(False)
            self._steps_warn_lbl.setVisible(False)
            self._on_patch({
                "steps_preset": key,
                "steps": _STEP_PRESET_VALUE.get(key, self._state.steps),
            })

    def _on_custom_steps_changed(self, v) -> None:
        val = int(v)
        self._steps_warn_lbl.setVisible(val > _STEP_SERVER_CAP)
        self._on_patch({"steps": val})

    # ------------------------------------------------------------------
    # Model dropdown
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Ground height (mirror field ↔ scene plane)
    # ------------------------------------------------------------------

    def _on_ground_y_edited(self, v) -> None:
        """Field value change → cache + emit so the host drives the plane's
        world-Y. Wired to ``valueChanged`` (live on every step / keystroke).
        Caching here keeps a later ``refresh`` from reverting the user's edit."""
        self._ground_y = float(v)
        self.ground_y_changed.emit(float(v))

    def set_ground_y(self, y_m) -> None:
        """Reconcile the mirror field FROM the live plane (refresh-in path).

        ``y_m`` is the plane's world-Y in meters, or ``None`` when no plane
        exists (field + Refresh/toggle buttons disabled, field shown at 0.0).
        Never writes back to the plane — the plane is the source of truth; this
        only reflects it.
        """
        self._ground_y = None if y_m is None else float(y_m)
        has_plane = self._ground_y is not None
        self._ground_y_input.setEnabled(has_plane)
        self._refresh_ground_btn.setEnabled(has_plane)
        self._ground_vis_btn.setEnabled(has_plane)
        self._ground_y_input.blockSignals(True)
        self._ground_y_input.setValue(self._ground_y if has_plane else 0.0)
        self._ground_y_input.blockSignals(False)

    def ground_field_has_focus(self) -> bool:
        """True while the Ground-height field owns keyboard focus (user typing).

        The idle-tick live sync (host ``_sync_ground_field_live``) checks this
        so a plane drag never clobbers a value being typed into the field.
        Focus lands on the wrapped spin box (or its internal line edit), never
        the ``NumberInput`` wrapper itself, hence the descendant check.
        """
        fw = QtWidgets.QApplication.focusWidget()
        return fw is not None and (
            fw is self._ground_y_input
            or self._ground_y_input.isAncestorOf(fw)
        )

    def set_ground_visible(self, visible) -> None:
        """Reflect the plane's true visibility on the Hide/Show toggle button.

        ``visible`` is ``True``/``False``, or ``None`` when no plane exists
        (button disabled). Label reads **Hide** when the plane is visible,
        **Show** when hidden — derived from the plane, never a local guess.
        """
        has_plane = visible is not None
        self._ground_vis_btn.setEnabled(has_plane)
        # None (no plane) reads as the neutral build-time default "Hide";
        # only an explicit False (hidden plane) flips the label to "Show".
        self._ground_vis_btn.setText("Show" if visible is False else "Hide")

    # ------------------------------------------------------------------
    # Output Target helpers
    # ------------------------------------------------------------------

    def _refresh_target_rows(self) -> None:
        mode = self._state.animation_mode
        # Story mode never bakes (motion lives in a passthrough clip), so grey
        # out the bake checkbox there — disable only, no state mutation, so the
        # prior checked value returns when the user leaves Story mode.
        self._bake_chk.setEnabled(mode != "story")
        # "Bake whole time range" only modifies a bake that's actually going to
        # run, so it follows the bake toggle on top of the story gate.
        self._bake_whole_chk.setEnabled(
            mode != "story" and self._state.bake_to_control_rig
        )
        # num_samples > 1 can only fan into distinct takes (Story/New Take);
        # Existing Take merges into the current take, so multiple samples cannot
        # fan -- grey the spinbox (disable only; builder clamps the request to 1).
        self._num_samples_input.setEnabled(mode != "existing_take")
        # Passthrough / Replace-FBX only describe story clips, so the container
        # is mode-exclusive rather than merely greyed out.
        self._story_container.setVisible(self._has_story and mode == "story")
        # New-take naming only matters for 'new_take' -- story mode names the
        # clip from the prompt label, existing_take mode targets the current
        # take snapshotted at the Generate click (P4). The custom input inside
        # the container is gated again on the auto flag.
        # Both rows describe the take machinery; a host without takes keeps
        # them hidden whatever the pinned mode is -- without the guard this
        # refresh would re-show the Current-take row on a Replace-only host,
        # because its pinned mode IS "existing_take".
        self._new_take_container.setVisible(
            self._has_takes and mode == "new_take")
        self._custom_name_row.setVisible(
            self._has_takes and mode == "new_take"
            and not self._state.auto_take_name
        )
        self._existing_row.setVisible(
            self._has_takes and mode == "existing_take")

    def set_current_take(self, name: str) -> None:
        """Mirror MoBu's current take name into the read-only display (P4)."""
        self._current_take_display.setText(name or "")
