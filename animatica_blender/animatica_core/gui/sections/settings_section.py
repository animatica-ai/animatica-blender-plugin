"""00 · Settings — backend toggle, server URL / cloud login, story path.

Restores the connection-settings panel that lived in the legacy
``main_window.py`` (Settings frame) so the new sectioned window can talk
to both a local MMCP server and Animatica Cloud.

The widget is read-only against ``AppState`` and emits signals for any
action that crosses the pyfbsdk / network boundary (login, logout, test
connection, browse story path). The host wires these into the actual
``animatica_auth`` / ``mmcp_client`` calls.
"""

from ..qt_compat import QtCore, QtWidgets, Signal
from ..widgets import (
    CollapsibleSection, SubSection, Pill, Field, Btn, TextInput, Check,
    Combo, NumberInput, reset_button,
)
from ... import host
from ...core.prompt_model import AppState


class SettingsSection(QtWidgets.QWidget):
    """Numbered workflow card 00."""

    test_connection_requested = Signal()
    login_requested = Signal(str, str)       # email, password
    logout_requested = Signal()
    browse_story_path_requested = Signal()

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)
        self._state = state
        self._on_patch = on_patch

        self._status_pill = Pill("idle", tone="muted", dot=True)
        self._section = CollapsibleSection(
            "Settings", step=0, icon="gear",
            right=self._status_pill, open=True,
        )

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)
        body = self._section.body_layout

        # -- First-run guidance banner (hidden until onboarding) ------------
        self._build_first_run_banner(body)

        # -- Grouped settings sub-sections ---------------------------------
        # Reparent the flat control run into labelled groups; every widget
        # instance and its on_patch / refresh() wiring is preserved — only the
        # parent layout changes (see change gui-fixes P3). Debug and
        # Non-canonical-rig start collapsed; the rest expanded.
        # Each group carries a header-mounted "Reset" button (right= slot) whose
        # handler is wired at the end of __init__ once every control exists.
        # Connection's reset is deliberately scoped to the server URL only — it
        # must NOT wipe credentials or flip the backend (see _reset_connection).
        conn_reset      = reset_button()
        naming_reset    = reset_button()
        rigs_reset      = reset_button()
        debug_reset     = reset_button()
        extra_reset     = reset_button()
        interface_reset = reset_button()
        conn      = SubSection("Connection", right=conn_reset, open=True)
        # The group holds the Story export path and the take-name length. Only
        # the first is Story-specific, so a host without Story keeps the group
        # and loses one row -- and the title stops promising a feature.
        _has_story = host.has(host.STORY)
        naming    = SubSection("Story & Naming" if _has_story else "Naming",
                               right=naming_reset, open=True)
        rigs      = SubSection("Non-canonical rigs", right=rigs_reset, open=False)
        debug_grp = SubSection("Debug", right=debug_reset, open=False)
        extra     = SubSection("Extra options", right=extra_reset, open=True)
        interface = SubSection("Interface", right=interface_reset, open=True)
        for _grp in (conn, naming, rigs, debug_grp, extra, interface):
            body.addWidget(_grp)

        # -- Backend toggle -------------------------------------------------
        backend_row = QtWidgets.QHBoxLayout()
        backend_row.setSpacing(6)
        self._local_btn = Btn("Local", variant="solid", size="sm")
        self._cloud_btn = Btn("Cloud (api.animatica.ai)", variant="surface", size="sm")
        self._local_btn.clicked.connect(lambda: self._set_backend("local"))
        self._cloud_btn.clicked.connect(lambda: self._set_backend("cloud"))
        backend_row.addWidget(self._local_btn, 0)
        backend_row.addWidget(self._cloud_btn, 0)
        backend_row.addStretch(1)
        conn.body_layout.addWidget(Field("Backend", None))
        conn.body_layout.addLayout(backend_row)

        # -- Local panel ----------------------------------------------------
        self._local_panel = QtWidgets.QWidget()
        local_layout = QtWidgets.QVBoxLayout(self._local_panel)
        local_layout.setContentsMargins(0, 0, 0, 0)
        local_layout.setSpacing(6)

        self._url = TextInput(state.server_url, placeholder="http://localhost:8000", mono=True)
        self._url.textChanged.connect(lambda v: on_patch({"server_url": v}))
        url_row = QtWidgets.QHBoxLayout()
        url_row.addWidget(self._url, 1)
        self._test_local_btn = Btn("Test", icon="check", variant="surface", size="sm")
        self._test_local_btn.clicked.connect(self.test_connection_requested.emit)
        url_row.addWidget(self._test_local_btn, 0)
        local_layout.addWidget(Field("Server URL", None))
        local_layout.addLayout(url_row)
        conn.body_layout.addWidget(self._local_panel)

        # -- Cloud panel ----------------------------------------------------
        self._cloud_panel = QtWidgets.QWidget()
        cloud_layout = QtWidgets.QVBoxLayout(self._cloud_panel)
        cloud_layout.setContentsMargins(0, 0, 0, 0)
        cloud_layout.setSpacing(6)

        self._email = TextInput(state.auth_email, placeholder="user@example.com", mono=True)
        self._email.textChanged.connect(lambda v: on_patch({"auth_email": v}))
        cloud_layout.addWidget(Field("Email", self._email))

        self._password = QtWidgets.QLineEdit()
        self._password.setEchoMode(QtWidgets.QLineEdit.Password)
        self._password.setPlaceholderText("••••••••")
        # Password never lands in AppState — held only in the widget.
        cloud_layout.addWidget(Field("Password", self._password))

        login_row = QtWidgets.QHBoxLayout()
        login_row.setSpacing(6)
        self._login_btn = Btn("Log In", icon="wand", variant="solid", size="sm")
        self._login_btn.clicked.connect(self._emit_login)
        self._logout_btn = Btn("Log Out", icon="trash", variant="ghost", size="sm")
        self._logout_btn.clicked.connect(self.logout_requested.emit)
        self._tier_pill = Pill("", tone="muted")
        login_row.addWidget(self._login_btn, 0)
        login_row.addWidget(self._logout_btn, 0)
        login_row.addWidget(self._tier_pill, 0)
        login_row.addStretch(1)
        cloud_layout.addLayout(login_row)

        cloud_test_row = QtWidgets.QHBoxLayout()
        self._test_cloud_btn = Btn("Test Connection", icon="check", variant="surface", size="sm")
        self._test_cloud_btn.clicked.connect(self.test_connection_requested.emit)
        cloud_test_row.addWidget(self._test_cloud_btn, 0)
        cloud_test_row.addStretch(1)
        cloud_layout.addLayout(cloud_test_row)

        conn.body_layout.addWidget(self._cloud_panel)

        # -- Story export path ---------------------------------------------
        # In one container so a host without Story can drop the whole row; the
        # widget is still built because the reset handler and the refresh both
        # read `self._story`.
        self._story_panel = QtWidgets.QWidget()
        story_col = QtWidgets.QVBoxLayout(self._story_panel)
        story_col.setContentsMargins(0, 0, 0, 0)
        story_col.setSpacing(6)
        story_col.addWidget(Field("Story Export Path", None))
        story_row = QtWidgets.QHBoxLayout()
        story_row.setSpacing(6)
        self._story = TextInput(state.story_path, placeholder="Folder for exported FBX files…")
        self._story.textChanged.connect(lambda v: on_patch({"story_path": v}))
        story_row.addWidget(self._story, 1)
        browse_btn = Btn("Browse", icon="folder", variant="surface", size="sm")
        browse_btn.clicked.connect(self.browse_story_path_requested.emit)
        story_row.addWidget(browse_btn, 0)
        story_col.addLayout(story_row)
        naming.body_layout.addWidget(self._story_panel)
        self._story_panel.setVisible(_has_story)

        # -- Wire-shaping global ------------------------------------------
        # Skip the topmost joint when shipping the user's hierarchy
        # (Path B). On by default — most rigs carry a synthetic
        # Reference/Root null above Hips that the server doesn't expect.
        skip_root = Check(
            "Skip top joint when sending hierarchy",
            checked=state.skip_root_joint,
            sublabel="Use the next joint, usually Hips, as the character root.",
        )
        skip_root.toggled.connect(lambda v: on_patch({"skip_root_joint": v}))
        rigs.body_layout.addWidget(skip_root)

        # -- How to reconcile a rig with a model that has another skeleton -
        # Consulted only when they differ. The two routes are not rivals:
        # the server's retarget model was trained for this, and HIK works
        # with nothing installed and is the only one usable live.
        route_row = QtWidgets.QHBoxLayout()
        route_row.addWidget(QtWidgets.QLabel("Retarget route"))
        self._route = Combo([
            ("auto", "Auto — server if it can, else HIK"),
            ("server", "Server — send my rig, server retargets"),
            ("hik", "HIK — generate on the model's rig, drive mine"),
            ("none", "None — leave my rig alone"),
        ], value=getattr(state, "retarget_route", "auto"))
        self._route.valueChanged.connect(
            lambda v: on_patch({"retarget_route": v}))
        route_row.addWidget(self._route, 1)
        rigs.body_layout.addLayout(route_row)

        # -- Parent-group scale compensation ------------------------------
        # When the rig sits under a scaled null, world-position deltas in
        # the wire payload come out pre-scaled. Dividing by the group's
        # scale on send and multiplying root translations on apply
        # restores the rig's intended geometry. Uniform only.
        comp_chk = Check(
            "Compensate group scale",
            checked=state.compensate_group_scale,
            sublabel="Divide wire offsets by the value on send; multiply back on apply.",
        )
        self._scale_input = NumberInput(
            state.group_scale, minimum=0.01, maximum=100.0, step=0.1, mono=True,
        )
        self._scale_input.valueChanged.connect(
            lambda v: on_patch({"group_scale": float(v)})
        )
        self._scale_field = Field("Group scale", self._scale_input)
        self._scale_field.setVisible(state.compensate_group_scale)

        def _on_comp_toggle(v):
            on_patch({"compensate_group_scale": v})
            self._scale_field.setVisible(v)

        comp_chk.toggled.connect(_on_comp_toggle)
        rigs.body_layout.addWidget(comp_chk)
        rigs.body_layout.addWidget(self._scale_field)

        # -- Match scene FPS ----------------------------------------------
        # The model generates at its native fps (30); the scene may run at a
        # different rate. When on, apply keys the motion frame-for-frame at the
        # scene fps so authored frames & constraints stay 1:1 (a no-op at 30fps).
        fps_chk = Check(
            "Match scene FPS",
            checked=state.match_scene_fps,
            sublabel="Key generated motion frame-for-frame at the scene fps so "
                     "authored frames & constraints stay 1:1. Off = key at the "
                     "model's native 30fps (real-time pace, frames may shift).",
        )
        fps_chk.toggled.connect(lambda v: on_patch({"match_scene_fps": v}))
        extra.body_layout.addWidget(fps_chk)

        # -- Show Motion Import panel --------------------------------------
        # The Motion Import group (load NPZ/BVH/glTF from disk) is hidden by
        # default for the generation-first workflow. Reveal it here; the host
        # toggles group visibility live and the choice is remembered.
        show_import_chk = Check(
            "Show Motion Import panel",
            checked=state.show_motion_import,
            sublabel="Reveal the panel for loading motion from NPZ / BVH / glTF files.",
        )
        show_import_chk.toggled.connect(lambda v: on_patch({"show_motion_import": v}))
        interface.body_layout.addWidget(show_import_chk)

        # Live Drive: realtime driving, ARDY only. Off by default so the
        # window looks and works the same whichever model is selected.
        show_live_chk = Check(
            "Show Live Drive panel",
            checked=getattr(state, "show_live_drive", False),
            sublabel="Realtime driving with the arrow keys. Works with ARDY only.",
        )
        show_live_chk.toggled.connect(lambda v: on_patch({"show_live_drive": v}))
        interface.body_layout.addWidget(show_live_chk)

        # -- Auto-open on MoBu startup -------------------------------------
        # Opt-in (default off): when on, the Animatica tool auto-opens once
        # each MotionBuilder launch via a deferred OnUIIdle one-shot fired
        # from _startup.register (never synchronous at import).
        auto_open_chk = Check(
            "Open Animatica on MotionBuilder startup",
            checked=state.auto_open_on_startup,
            sublabel="Auto-open the tool window once when MotionBuilder finishes loading.",
        )
        auto_open_chk.toggled.connect(lambda v: on_patch({"auto_open_on_startup": v}))
        interface.body_layout.addWidget(auto_open_chk)

        # -- Debug capture -------------------------------------------------
        # When on, every /generate round-trip writes request.json /
        # response.json / motion.json / meta.json under the folder below
        # so we can diff a misbehaving session against a known-good one.
        import os
        default_debug_dir = state.debug_dir or os.path.join(
            os.path.expanduser("~"), "Documents", "MB", "Animatica_Debug",
        )
        self._debug_dir_input = TextInput(
            default_debug_dir,
            placeholder=os.path.join("~", "Documents", "MB", "Animatica_Debug"),
            mono=True,
        )
        self._debug_dir_input.textChanged.connect(
            lambda v: on_patch({"debug_dir": v})
        )

        self._debug_dir_field = Field("Debug folder", self._debug_dir_input)
        self._debug_dir_field.setVisible(state.debug_capture)

        debug_chk = Check(
            "Debug: save request / response JSON",
            checked=state.debug_capture,
            sublabel="One folder per generate run under the path below.",
        )

        def _on_debug_toggle(v):
            on_patch({"debug_capture": v})
            self._debug_dir_field.setVisible(v)

        debug_chk.toggled.connect(_on_debug_toggle)
        debug_grp.body_layout.addWidget(debug_chk)
        debug_grp.body_layout.addWidget(self._debug_dir_field)

        # -- Debug: omit timing block -------------------------------------
        # Experiment: drop "timing":{"fps":N} from the request entirely. MMCP
        # v1 normally requires the model fps and rejects a mismatch, so the
        # server may default its fps or reject -- either outcome is informative.
        omit_timing_chk = Check(
            "Debug: omit timing block (send no fps)",
            checked=state.debug_omit_timing,
            sublabel="Experiment — server may default its fps or reject the request.",
        )
        omit_timing_chk.toggled.connect(lambda v: on_patch({"debug_omit_timing": v}))
        debug_grp.body_layout.addWidget(omit_timing_chk)

        # -- Debug: omit frame-0 root anchor -------------------------------
        # Experiment: drop the auto-injected root_path [0,0] at frame 0. Every
        # recorded rationale for it is pinned-case; the text-only branch has
        # never been measured. With it off a pin-free request ships
        # "constraints": [] -- deliberately single-variable (whether the key
        # should be omitted entirely is a separate question).
        omit_anchor_chk = Check(
            "Debug: omit frame-0 root anchor",
            checked=state.debug_omit_root_anchor,
            sublabel="Experiment — a pin-free request then sends no constraints at all.",
        )
        omit_anchor_chk.toggled.connect(
            lambda v: on_patch({"debug_omit_root_anchor": v})
        )
        debug_grp.body_layout.addWidget(omit_anchor_chk)

        # -- Debug: send effector rotations ---------------------------------
        # Gates the optional `rotations` array on effector_target wires
        # (request_builder._marker_to_wire). Probe-proven inert on the hosted
        # server (rotation_probe_20260726: pin-frame delta exactly 0.0); a
        # local kimodo_server DOES consume it (_build_end_effector_dict). A
        # pin dragged after capture keeps its quaternion flagged stale and
        # ships it anyway, with a soft warning naming the pin.
        send_rot_chk = Check(
            "Debug: send effector rotations",
            checked=state.debug_send_effector_rotations,
            sublabel="Probe lever — ignored by the hosted server; consumed only "
                     "by a local kimodo_server. May ship a rotation captured "
                     "before the pin was dragged (warns when it does).",
        )
        send_rot_chk.toggled.connect(
            lambda v: on_patch({"debug_send_effector_rotations": v})
        )
        debug_grp.body_layout.addWidget(send_rot_chk)

        # -- Debug: send scene FPS in timing --------------------------------
        # Experiment: ship the scene fps in "timing" instead of the model fps.
        # ANSWERED 2026-08-08 -- the server rejects it outright ("server-side
        # resampling is not implemented in v1"), so a client-side resample is
        # the only route to an arbitrary scene fps. Kept as the regression check
        # if that ever changes. Only the timing value changes; durations and
        # constraint frames stay unscaled. "Omit timing block" wins.
        send_scene_fps_chk = Check(
            "Debug: send scene FPS in timing",
            checked=state.debug_send_scene_fps,
            sublabel="Measured 2026-08-08 — the server REJECTS this and the "
                     "generation fails. Left in place to re-test if server-side "
                     "resampling ever ships. Overridden by 'omit timing block'.",
        )
        send_scene_fps_chk.toggled.connect(
            lambda v: on_patch({"debug_send_scene_fps": v})
        )
        debug_grp.body_layout.addWidget(send_scene_fps_chk)

        # -- Send path heading (A/B) --------------------------------------
        # Step 2 experiment: derive a body facing for each Path waypoint from
        # the trajectory (face toward the next point) and send it as
        # heading_radians. Off by default -- maya_kimodo sends none; the Path
        # front-cross stays visible regardless. A/B test motion quality.
        send_heading_chk = Check(
            "Send path heading (face along path)",
            checked=state.send_path_heading,
            sublabel="A/B experiment — may over-constrain turning; compare with it off.",
        )
        send_heading_chk.toggled.connect(lambda v: on_patch({"send_path_heading": v}))
        extra.body_layout.addWidget(send_heading_chk)

        # -- Send effector root context (A/B) -----------------------------
        # Gates the _eph_root_ctx injection (8dea32c): one same-frame root_path
        # waypoint carrying the rig's REAL pelvis XZ + heading per hand/foot pin
        # frame with no user root/pose coverage. OFF by default -- sent bare, an
        # effector pin makes the server fabricate pelvis XZ/height/facing from a
        # T-pose, but the injected waypoint also hard-pins the pelvis. Off ships
        # only what the user authored; on is the opt-in A/B lever.
        # NB: the label text is quoted verbatim by the suppressed-pin warning in
        # core/request_builder._warn_uncovered_effector_pins -- change both together.
        send_root_ctx_chk = Check(
            "Send real body context with hand/foot pins",
            checked=state.send_effector_root_context,
            sublabel="A/B experiment — off (default) lets the server approximate the body from a T-pose.",
        )
        send_root_ctx_chk.toggled.connect(
            lambda v: on_patch({"send_effector_root_context": v})
        )
        extra.body_layout.addWidget(send_root_ctx_chk)

        # -- Same-frame waypoint reorder (direction) ----------------------
        # Picks the direction of the deterministic same-frame reorder in
        # _collect_constraints (server dedup is last-listed-wins): on moves a
        # user Path waypoint sharing a frame with a hand/foot pin after the
        # effector wire (the authored root wins), off (default 2026-07-30)
        # moves it before (the fabricated T-pose root wins — near-exact pin
        # snap, pelvis free to ignore the waypoint on that frame). Marker
        # creation order never decides, in either state.
        reorder_chk = Check(
            "Path waypoint wins over same-frame hand/foot pins",
            checked=state.reorder_same_frame_waypoints,
            sublabel="Off (default): pins snap onto their target; the body may "
                     "jump to reach them. On: your waypoint wins — smoother "
                     "motion, pins land near (not on) their target.",
        )
        reorder_chk.toggled.connect(
            lambda v: on_patch({"reorder_same_frame_waypoints": v})
        )
        extra.body_layout.addWidget(reorder_chk)

        # -- Seam waypoint duplication (A/B) ------------------------------
        # Gates request_builder._duplicate_seam_waypoints: a Path waypoint on
        # the frame two touching prompt blocks share belongs to the LATER block
        # only (the model crops constraints per segment, half-open), so the
        # earlier one can run unsteered and the gap lands at the seam. On, the
        # waypoint is copied one frame earlier so both blocks are steered.
        # Default ON since 2026-08-08 (live A/B: seam visibly improved).
        seam_dup_chk = Check(
            "Duplicate path waypoints at block seams",
            checked=state.duplicate_seam_waypoints,
            sublabel="On (default) — a waypoint on a shared block boundary is "
                     "also sent one frame earlier, so both blocks are steered. "
                     "Off sends it to the later block only, which can leave the "
                     "earlier one unsteered and slide at the seam.",
        )
        seam_dup_chk.toggled.connect(
            lambda v: on_patch({"duplicate_seam_waypoints": v})
        )
        extra.body_layout.addWidget(seam_dup_chk)

        # -- Gated ground correction (default OFF) ------------------------
        # Subtract the response's measured ~2 cm float (canonical rigs only,
        # std-gated) from the applied root Y. Relocated here from the Generate
        # card's Ground group: it is a general response-float mitigation, not
        # a plane control, and its near-identical label next to "Apply ground
        # offset" misled an A/B (change ground-plane-problem).
        ground_corr_chk = Check(
            "Correct ground offset",
            checked=state.ground_correction_enabled,
            sublabel="Subtract the measured server float so feet land on the "
                     "ground (canonical rigs only; shifts pinned poses down "
                     "by the same amount)",
        )
        ground_corr_chk.toggled.connect(
            lambda v: on_patch({"ground_correction_enabled": v})
        )
        extra.body_layout.addWidget(ground_corr_chk)

        # -- Capture grounding (default ON) -------------------------------
        # Deliberately the opposite default to the checkbox above: a capture
        # has no pinned poses to shift, and a character floating above the
        # floor is the bug users report about this feature.
        capture_ground_chk = Check(
            "Ground captured motion",
            checked=getattr(state, "capture_ground_correction", True),
            sublabel="Measure the planted foot on your rig from the service's "
                     "foot contacts and seat the capture on the ground",
        )
        capture_ground_chk.toggled.connect(
            lambda v: on_patch({"capture_ground_correction": v})
        )
        extra.body_layout.addWidget(capture_ground_chk)

        # -- Auto take-name length ----------------------------------------
        # How many leading prompt chars seed the auto-generated take name
        # (clamped 1–120). Lives here (not in the Generate card) so it sits
        # with the other persisted tool settings; the auto-name toggle itself
        # stays in Section 03.
        self._name_len_input = NumberInput(
            value=int(getattr(state, "take_name_length", 50) or 50),
            minimum=1, maximum=120, step=1, mono=True,
        )
        self._name_len_input.valueChanged.connect(
            lambda v: on_patch({"take_name_length": int(v)})
        )
        self._take_len_field = Field(
            "Auto take-name length", self._name_len_input,
            hint="Leading prompt characters used when auto-naming a new take.",
        )
        naming.body_layout.addWidget(self._take_len_field)
        if not host.has(host.TAKES):
            # Nothing on this host is ever named from a prompt -- there are
            # no takes to name.
            self._take_len_field.setVisible(False)

        # -- Per-group reset handlers -------------------------------------
        # Push a fresh ``AppState()``'s values for each group's owned fields,
        # blocking control signals so no setter re-fires on_patch, then one
        # on_patch call updates state. Post-reset dependent-row visibility is
        # set to its known default (not recomputed). Runtime/session fields
        # (auth_*, connected) are never reset — controls don't own them.
        def _reset_connection():
            d = AppState()
            # Scoped to the server URL only: a blanket reset here would wipe
            # credentials and flip the backend to local (arming the re-probe /
            # recovery dialog). Backend + auth stay as the user left them.
            self._url.blockSignals(True)
            self._url.setText(d.server_url)
            self._url.blockSignals(False)
            self._on_patch({"server_url": d.server_url})

        def _reset_naming():
            d = AppState()
            self._story.blockSignals(True)
            self._story.setText(d.story_path)
            self._story.blockSignals(False)
            self._name_len_input.blockSignals(True)
            self._name_len_input.setValue(d.take_name_length)
            self._name_len_input.blockSignals(False)
            self._on_patch({
                "story_path": d.story_path,
                "take_name_length": d.take_name_length,
            })

        def _reset_rigs():
            d = AppState()
            skip_root.blockSignals(True)
            skip_root.setChecked(d.skip_root_joint)
            skip_root.blockSignals(False)
            comp_chk.blockSignals(True)
            comp_chk.setChecked(d.compensate_group_scale)
            comp_chk.blockSignals(False)
            self._scale_input.blockSignals(True)
            self._scale_input.setValue(d.group_scale)
            self._scale_input.blockSignals(False)
            self._scale_field.setVisible(d.compensate_group_scale)
            self._on_patch({
                "skip_root_joint": d.skip_root_joint,
                "compensate_group_scale": d.compensate_group_scale,
                "group_scale": d.group_scale,
            })

        def _reset_debug():
            d = AppState()
            debug_chk.blockSignals(True)
            debug_chk.setChecked(d.debug_capture)
            debug_chk.blockSignals(False)
            # ``debug_dir`` default is "" (meaning "use the computed folder");
            # show that computed path in the field, matching construction.
            computed = os.path.join(
                os.path.expanduser("~"), "Documents", "MB", "Animatica_Debug",
            )
            self._debug_dir_input.blockSignals(True)
            self._debug_dir_input.setText(computed)
            self._debug_dir_input.blockSignals(False)
            self._debug_dir_field.setVisible(d.debug_capture)
            omit_timing_chk.blockSignals(True)
            omit_timing_chk.setChecked(d.debug_omit_timing)
            omit_timing_chk.blockSignals(False)
            omit_anchor_chk.blockSignals(True)
            omit_anchor_chk.setChecked(d.debug_omit_root_anchor)
            omit_anchor_chk.blockSignals(False)
            send_rot_chk.blockSignals(True)
            send_rot_chk.setChecked(d.debug_send_effector_rotations)
            send_rot_chk.blockSignals(False)
            # Session-only (deliberately absent from _PERSISTED_FIELDS), but it
            # must still be resettable: a probe Reset-to-defaults can't clear is
            # the same trap as one that survives a restart.
            send_scene_fps_chk.blockSignals(True)
            send_scene_fps_chk.setChecked(d.debug_send_scene_fps)
            send_scene_fps_chk.blockSignals(False)
            self._on_patch({
                "debug_capture": d.debug_capture, "debug_dir": d.debug_dir,
                "debug_omit_timing": d.debug_omit_timing,
                "debug_omit_root_anchor": d.debug_omit_root_anchor,
                "debug_send_effector_rotations": d.debug_send_effector_rotations,
                "debug_send_scene_fps": d.debug_send_scene_fps,
            })

        def _reset_extra():
            d = AppState()
            fps_chk.blockSignals(True)
            fps_chk.setChecked(d.match_scene_fps)
            fps_chk.blockSignals(False)
            send_heading_chk.blockSignals(True)
            send_heading_chk.setChecked(d.send_path_heading)
            send_heading_chk.blockSignals(False)
            send_root_ctx_chk.blockSignals(True)
            send_root_ctx_chk.setChecked(d.send_effector_root_context)
            send_root_ctx_chk.blockSignals(False)
            reorder_chk.blockSignals(True)
            reorder_chk.setChecked(d.reorder_same_frame_waypoints)
            reorder_chk.blockSignals(False)
            seam_dup_chk.blockSignals(True)
            seam_dup_chk.setChecked(d.duplicate_seam_waypoints)
            seam_dup_chk.blockSignals(False)
            ground_corr_chk.blockSignals(True)
            ground_corr_chk.setChecked(d.ground_correction_enabled)
            ground_corr_chk.blockSignals(False)
            capture_ground_chk.blockSignals(True)
            capture_ground_chk.setChecked(d.capture_ground_correction)
            capture_ground_chk.blockSignals(False)
            self._on_patch({
                "match_scene_fps": d.match_scene_fps,
                "send_path_heading": d.send_path_heading,
                "send_effector_root_context": d.send_effector_root_context,
                "reorder_same_frame_waypoints": d.reorder_same_frame_waypoints,
                "duplicate_seam_waypoints": d.duplicate_seam_waypoints,
                "ground_correction_enabled": d.ground_correction_enabled,
                "capture_ground_correction": d.capture_ground_correction,
            })

        def _reset_interface():
            d = AppState()
            show_import_chk.blockSignals(True)
            show_import_chk.setChecked(d.show_motion_import)
            show_import_chk.blockSignals(False)
            auto_open_chk.blockSignals(True)
            auto_open_chk.setChecked(d.auto_open_on_startup)
            auto_open_chk.blockSignals(False)
            self._on_patch({
                "show_motion_import": d.show_motion_import,
                "auto_open_on_startup": d.auto_open_on_startup,
            })

        conn_reset.clicked.connect(_reset_connection)
        naming_reset.clicked.connect(_reset_naming)
        rigs_reset.clicked.connect(_reset_rigs)
        debug_reset.clicked.connect(_reset_debug)
        extra_reset.clicked.connect(_reset_extra)
        interface_reset.clicked.connect(_reset_interface)

        # -- Keyboard shortcuts (read-only reference) ---------------------
        self._build_shortcuts_group(body)

        self.refresh()

    # ------------------------------------------------------------------
    # Keyboard shortcuts reference
    # ------------------------------------------------------------------

    # (key, action) — the full timeline interaction vocabulary, split into a
    # Keyboard table and a Mouse & block-editing table. Each row is verified
    # against its handler in timeline/widget.py: keyPressEvent (1763), the inline
    # editor _kpe (1952/2057), mousePressEvent (1380), mouseMoveEvent (1461),
    # mouseDoubleClickEvent (2068), wheelEvent (2087), _show_context_menu (2108).
    # Purely presentational; keep BOTH tables in sync when any of those handlers'
    # bindings change.
    _SHORTCUTS_KEYBOARD = (
        ("← / →",           "Nudge selected block −1 / +1 frame (steps playhead if nothing selected)"),
        ("Shift+← / →",     "Nudge selected block −10 / +10 frames"),
        ("Ctrl+← / →",      "Step playhead −1 / +1 frame"),
        ("Ctrl+Shift+← / →", "Step playhead −10 / +10 frames"),
        ("Home / End",      "Snap playhead to selected block start / end"),
        ("Shift+Home / End", "Snap selected block start / end to playhead"),
        ("Ctrl+Space",    "Play / stop toggle"),
        ("[ / ]",         "Jump to previous / next constraint frame"),
        (", / .",         "Jump playhead to previous / next block edge"),
        ("F",             "Fit timeline to range"),
        ("Shift+F",       "Fit to selected prompt block(s)"),
        ("Z",             "Zoom to selected prompt block(s)"),
        ("Shift+Z",       "Fit zoom to all prompt blocks"),
        ("Ctrl+Z / Ctrl+Y", "Undo / redo timeline edits"),
        ("Ctrl+G",        "Generate motion from all prompts"),
        ("Delete",        "Remove selected prompt block(s) and constraint pin(s) — one undo step"),
        ("Shift+D",       "Clear selection (blocks + constraint pins)"),
        ("Esc",           "Cancel inline prompt edit"),
        ("Enter",         "Commit inline prompt edit"),
    )

    _SHORTCUTS_MOUSE = (
        ("Left-drag (empty)",       "Scrub playhead (any height)"),
        ("Left-click near playhead", "Scrub — the playhead wins over constraint pins near its line"),
        ("Left-click (empty)",      "Clear selection (blocks + constraint pins)"),
        ("Ctrl+Left-drag (empty)",  "Rubber-band select blocks + constraint pins"),
        ("Left-click block",        "Select block"),
        ("Ctrl+Left-click block",   "Add / remove block from selection"),
        ("Left-drag block",         "Move block(s); hold Shift to disable snapping"),
        ("Alt+Left-drag block",     "Jump past a neighbour — snaps into the nearest free gap on the drop side"),
        ("Left-drag block edge",    "Resize; Alt pushes the neighbour, Ctrl scales pins"),
        ("Left-click constraint icon", "Add constraint at block midpoint"),
        ("Left-click constraint pin", "Select the pin + its viewport marker; Ctrl toggles it in / out of the selection"),
        ("Left-drag constraint pin", "Move the constraint (grab the pin shape itself)"),
        ("Double-click block",      "Edit prompt text inline"),
        ("Double-click empty",      "Create a new block and edit it"),
        ("Middle-drag",             "Pan the view"),
        ("Ctrl+Scroll",             "Zoom timeline in / out at cursor"),
        ("Right-click",             "Context menu — constraints, prompt actions "
                                    "(edit / regenerate / export / snap) and view fits"),
    )

    def _build_shortcuts_group(self, body) -> None:
        sub = SubSection("Keyboard Shortcuts", open=False)
        self._add_shortcut_table(sub.body_layout, "Keyboard", self._SHORTCUTS_KEYBOARD)
        self._add_shortcut_table(
            sub.body_layout, "Mouse & block editing", self._SHORTCUTS_MOUSE
        )
        hint = QtWidgets.QLabel(
            "Shortcuts fire while the mouse hovers the timeline (otherwise they "
            "pass through to MotionBuilder). Left-drag scrubs the playhead at any "
            "height; middle-drag pans. Arrows, Home/End, [ / ], , / ., Z, Shift+D "
            "and undo/redo are suspended while editing a prompt inline."
        )
        hint.setObjectName("field_hint")
        hint.setWordWrap(True)
        sub.body_layout.addWidget(hint)
        body.addWidget(sub)

    def _add_shortcut_table(self, layout, title, rows) -> None:
        """Render one labelled key/action grid into *layout*."""
        header = QtWidgets.QLabel(title)
        header.setObjectName("field_label")
        layout.addWidget(header)
        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(0, 0, 0, 8)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        for r, (key, action) in enumerate(rows):
            key_lbl = QtWidgets.QLabel(key)
            key_lbl.setObjectName("field_label")
            key_lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            act_lbl = QtWidgets.QLabel(action)
            act_lbl.setWordWrap(True)
            grid.addWidget(key_lbl, r, 0, QtCore.Qt.AlignTop)
            grid.addWidget(act_lbl, r, 1)
        layout.addLayout(grid)

    # ------------------------------------------------------------------
    # First-run guidance banner
    # ------------------------------------------------------------------

    def _build_first_run_banner(self, body) -> None:
        """Hidden-by-default onboarding banner with an actionable next step for
        both backends. Shown once via ``show_first_run_banner`` on first launch;
        the Dismiss control hides it and it never reappears (the host's
        ``first_run_done`` flag gates re-showing across sessions)."""
        banner = QtWidgets.QFrame()
        banner.setObjectName("section_frame_sub")
        lay = QtWidgets.QVBoxLayout(banner)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)

        title = QtWidgets.QLabel("Welcome to Animatica")
        title.setObjectName("field_label")
        lay.addWidget(title)

        guide = QtWidgets.QLabel(
            "Pick how you generate motion:\n"
            "• Cloud — log in below, or create an account at animatica.ai.\n"
            "• Local — switch to Local, set your server URL, and start your "
            "MMCP server (see the docs)."
        )
        guide.setObjectName("field_hint")
        guide.setWordWrap(True)
        lay.addWidget(guide)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        dismiss = Btn("Dismiss", variant="ghost", size="sm")
        dismiss.clicked.connect(self._dismiss_first_run_banner)
        btn_row.addWidget(dismiss, 0)
        lay.addLayout(btn_row)

        banner.setVisible(False)
        self._first_run_banner = banner
        body.addWidget(banner)

    def show_first_run_banner(self) -> None:
        """Reveal the onboarding banner (called by the host on first launch)."""
        self._first_run_banner.setVisible(True)

    def _dismiss_first_run_banner(self) -> None:
        self._first_run_banner.setVisible(False)

    # ------------------------------------------------------------------
    # Backend toggle
    # ------------------------------------------------------------------

    def _set_backend(self, mode: str) -> None:
        if mode == self._state.backend:
            return
        self._on_patch({"backend": mode})
        self.refresh()

    def _emit_login(self) -> None:
        email = self._email.text().strip()
        pwd = self._password.text()
        if not email or not pwd:
            return
        self.login_requested.emit(email, pwd)
        self._password.clear()

    # ------------------------------------------------------------------
    # External setters (host calls these on browse / network completion)
    # ------------------------------------------------------------------

    def set_story_path(self, path: str) -> None:
        self._story.setText(path)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        s = self._state

        is_cloud = s.backend == "cloud"
        self._local_panel.setVisible(not is_cloud)
        self._cloud_panel.setVisible(is_cloud)
        self._local_btn.set_variant("surface" if is_cloud else "solid")
        self._cloud_btn.set_variant("solid" if is_cloud else "surface")

        # Login / logout visibility
        self._login_btn.setVisible(not s.auth_logged_in)
        self._logout_btn.setVisible(s.auth_logged_in)
        self._email.setEnabled(not s.auth_logged_in)
        if s.auth_logged_in and s.auth_email and self._email.text() != s.auth_email:
            self._email.setText(s.auth_email)
        if s.auth_tier:
            self._tier_pill.set_text(s.auth_tier)
            self._tier_pill.set_tone("info" if s.auth_logged_in else "muted")
        else:
            self._tier_pill.set_text("")

        # Header status pill
        if is_cloud and s.auth_logged_in and s.connected:
            self._status_pill.set_text("cloud online")
            self._status_pill.set_tone("success")
        elif is_cloud and s.auth_logged_in:
            self._status_pill.set_text("logged in")
            self._status_pill.set_tone("info")
        elif not is_cloud and s.connected:
            self._status_pill.set_text("local online")
            self._status_pill.set_tone("success")
        else:
            self._status_pill.set_text("idle")
            self._status_pill.set_tone("muted")
