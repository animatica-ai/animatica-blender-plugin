"""In-memory data model for the Animatica tool window.

Mirrors the state shape used by the `Kimodo to Maya` design mockup
(`doc/design/Kimodo to Maya.html`) and extends it with per-character
containers so multiple FBCharacters in one scene can be authored side by
side.

Pure-Python dataclasses; no pyfbsdk imports. Persistence lives in
``prompt_store_json``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict, fields as _dc_fields
from typing import Any
import uuid


# Bump when the in-scene prompt payload shape changes; ``prompts_from_dict``
# tolerates unknown keys so a forward bump never crashes an older client.
PROMPTS_SCHEMA_VERSION = 1


CONSTRAINT_TYPES = (
    "fullbody",
    "left-foot",
    "right-foot",
    "left-hand",
    "right-hand",
    "root2d",
)

EFFECTOR_TYPES = frozenset({"left-foot", "right-foot", "left-hand", "right-hand"})


def constraint_can_add(existing_types, new_type: str) -> tuple[bool, str]:
    """Per-frame coexistence policy for one timeline frame.

    *existing_types* is the set of constraint types already on the frame; the
    same type as *new_type* is ignored (a same-type add is an upsert, handled by
    the caller). Returns ``(ok, reason)`` -- ``reason`` is empty when ok.

    Rules (MMCP permits any combination on the wire; this is a stricter
    client-side authoring guard):
      - ``root2d`` (Path) coexists with everything.
      - effectors (hand/leg) coexist with each other and with Path, but NOT with
        a Full Body pose (which already pins the whole body).
      - ``fullbody`` coexists only with Path.
    """
    others = set(existing_types) - {new_type}
    if new_type == "root2d":
        return True, ""
    if new_type in EFFECTOR_TYPES:
        if "fullbody" in others:
            return False, (
                "Can't add a hand/leg constraint on a frame that already has a "
                "Full Body constraint -- Full Body already pins the whole pose."
            )
        return True, ""
    if new_type == "fullbody":
        blockers = others - {"root2d"}
        if blockers:
            return False, (
                "Full Body can only share a frame with Path -- remove the other "
                f"constraint(s) on this frame first ({sorted(blockers)})."
            )
        return True, ""
    return True, ""


def _new_id() -> str:
    return uuid.uuid4().hex


#: Every output target the GUI knows, in display order. Which of them a given
#: host may actually offer is :func:`available_target_modes` -- "story" needs a
#: Story timeline, and 3ds Max has none.
TARGET_MODES = (
    ("story", "Story"),
    ("new_take", "New Take"),
    ("existing_take", "Existing Take"),
)

DEFAULT_TARGET_MODE = "existing_take"


def available_target_modes(has_story: bool, has_takes: bool = True):
    """The output targets this host can offer, as ``[(value, label)]``.

    Kept here rather than in the section that draws it, because a rule inside a
    Qt widget can only be tested by building a Qt widget -- and the hosts that
    need this rule are exactly the ones where that is hardest.

    A host without takes (3ds Max: one animation, Replace-only) is NOT an empty
    list -- it is the single mode ``("existing_take", "Replace")``. The internal
    vocabulary stays ``existing_take`` so the apply path is untouched; "Replace"
    is what that host actually does, and the one honest label for it. The GUI
    hides the picker when there is nothing to pick between.
    """
    if not has_takes:
        return [("existing_take", "Replace")]
    return [m for m in TARGET_MODES if has_story or m[0] != "story"]


def coerce_animation_mode(mode: str, has_story: bool,
                          has_takes: bool = True) -> str:
    """*mode*, or the default when this host cannot honour it.

    A settings file outlives a capability check: it may have been written
    before the check existed, or by a host that does have Story. Without this
    the picker would hold a value it has no button for.
    """
    if mode == "story" and not (has_story and has_takes):
        # Story implies takes (a clip sits over a take); a host declaring
        # story without takes is a contradiction, and the picker resolves it
        # the same way available_target_modes does -- Replace only.
        return DEFAULT_TARGET_MODE
    if mode == "new_take" and not has_takes:
        return DEFAULT_TARGET_MODE
    return mode


@dataclass
class ConstraintMarker:
    """One pin on the timeline.

    ``type`` matches the MMCP server contract (see ``constraints.py``).
    ``value`` holds the type-specific payload — for ``fullbody`` and
    end-effector constraints that's a pose snapshot dict; for ``root2d``
    it's ``{"xz": [x, z]}``. Joint name routes through
    ``skeleton.get_joint_hierarchy(...)`` and is stored verbatim.

    ``frame`` is the **absolute** (displayed) scene frame — never a take-local or
    relativized value. Relativization to the server's 0-based request timeline is a
    request-time concern (``request_builder._collect_constraints`` reframes against
    ``frame_range[0] + frame_offset``; apply re-adds the offset, small-fixes #16).
    Keeping the stored frame absolute means a scene saved at one take start reopens
    with its pins intact and a later generate reconciles them against the *current*
    take's offset. Cross-take-range-reload edge: if the take range moved so a pin
    now falls outside it, the request gate drops that pin (it is not mis-mapped); a
    moved-but-still-in-range take just relativizes against the new base. No JSON
    schema change — only the request math reconciles. See
    ``context/changes/constraint-frame-space/``.
    """

    frame: int
    joint: str
    type: str
    value: dict = field(default_factory=dict)


@dataclass
class PromptBox:
    """One generation request painted on the timeline."""

    start: int
    end: int
    text: str = ""
    id: str = field(default_factory=_new_id)
    params: dict = field(default_factory=dict)
    server_request: dict | None = None
    last_result_ref: str | None = None
    color_idx: int = 0
    generation_count: int = 0


def boxes_out_of_range(boxes, total_frames):
    """Return the prompt boxes whose take-local span falls outside ``[0, total]``.

    Box ``start``/``end`` are take-local (the timeline paints them at
    ``start + frame_offset``); the take-local valid window is ``[0, total_frames]``
    regardless of ``frame_offset``. A box is out-of-range when ``start < 0`` or
    ``end > total_frames`` (general-features #3). Pure logic — no pyfbsdk — so the
    scene-range predicate is unit-testable without MotionBuilder.
    """
    total = int(total_frames)
    return [b for b in boxes if b.start < 0 or b.end > total]


@dataclass
class LogEntry:
    t: str
    level: str
    msg: str


@dataclass
class CharacterState:
    """Per-character authoring state — one row in ``AppState.characters``."""

    character_id: str
    display_name: str = ""
    prompts: list[PromptBox] = field(default_factory=list)
    constraints: list[ConstraintMarker] = field(default_factory=list)
    active_prompt_idx: int | None = None


@dataclass
class AppState:
    """Top-level state owned by the tool window.

    The per-character split lives under ``characters`` keyed by a stable
    character UUID (stamped by ``bridge.character_enum``). Globals
    that are not character-scoped (server URL, model, theme, transport
    sync flag) live on this top object.
    """

    characters: dict[str, CharacterState] = field(default_factory=dict)
    active_character_id: str | None = None

    fps: float = 24.0
    total_frames: int = 181
    start_frame: int = -1
    current_frame: int = 0

    namespace: str = "animatica"
    skeleton_ready: bool = False
    # Currently-chosen skeleton root, by FBModel name. Empty = use the
    # default prefix-based lookup (Create / Load flow). Not persisted —
    # scene-scoped only.
    selected_skeleton_name: str = ""
    auto_create_skeleton: bool = False
    # When True, the generator walks the in-scene FBModelSkeleton and ships
    # it as the request.skeleton block (requires server supports_retargeting).
    # When False (default), the canonical skeleton from /capabilities is sent.
    use_local_skeleton: bool = False
    # How to reconcile a scene rig with a model whose skeleton differs.
    # Only consulted when they DO differ — a matching rig needs nothing.
    #   "server" — ship the rig, let the server's retarget model handle it
    #   "hik"    — generate on the model's own rig, drive the scene rig
    #              from it through MotionBuilder's HIK retargeting
    #   "none"   — neither; generate on the model's rig and leave the
    #              scene rig alone
    #   "auto"   — server when it reports a retarget model, else hik, else
    #              none; always says in the log which and why
    retarget_route: str = "auto"
    # Speed of the straight walk synthesised for a trajectory-driven
    # model when no Path is drawn (see request_builder). Model-specific:
    # it means nothing for a model that travels from the text alone.
    walk_speed_ms: float = 1.4
    # When True, exclude the topmost joint from the wire payload so the
    # next joint (typically Hips) acts as the character root. On by default
    # because most rigs carry a synthetic Reference/Root null above Hips.
    skip_root_joint: bool = True
    # Manual uniform-scale compensation for rigs sitting under a scaled
    # parent group. When the toggle is on, rest_translations on the wire
    # are divided by group_scale, and root-translation playback values
    # are multiplied by it on apply. Uniform only; min 0.01, no negatives.
    compensate_group_scale: bool = False
    group_scale: float = 1.0
    # The model generates at its native fps (30); the scene may run at a
    # different rate. When True, apply keys the returned motion frame-for-frame
    # at the scene fps (model frame N -> scene frame N) so authored frames and
    # constraints stay 1:1. A no-op when the scene already runs at the model
    # fps. Off = key at the model's native fps (real-time pace, frames shift).
    # Interim client-side reinterpret; a faithful resample is the server's job.
    match_scene_fps: bool = True
    constraint_type: str = "fullbody"
    keep_keyframes: bool = True
    # Path (root2d) marker display toggles (viz-only; do not touch the wire).
    # show_path_length -> the red movement line's built-in length label (cm);
    # show_marker_label -> a friendly "Path F<frame>" name label per marker.
    show_path_length: bool = False
    show_marker_label: bool = False

    motion_file: str = ""

    # Video Capture. The clip path persists like motion_file does, so the
    # section reopens on the last thing the operator captured rather than
    # empty; the service URL is env-overridable and not a user setting,
    # because pointing the plugin at an arbitrary host is an operator
    # decision, not a per-scene one.
    capture_video: str = ""
    # COCO class names to also track as objects, as the operator typed
    # them ("sports ball, chair"). Persisted beside the clip path and for
    # the same reason: a session that captures props tends to capture the
    # same ones again. Empty disables the feature entirely.
    capture_props: str = ""

    # Backend / connection — mirrors the old Settings panel.
    backend: str = "local"             # "local" | "cloud"
    server_url: str = "http://localhost:8000"
    connected: bool = False

    # Animatica Cloud — credentials live in animatica_auth's token file;
    # only the display state is mirrored here.
    auth_email: str = ""
    auth_tier: str = ""
    auth_logged_in: bool = False

    # First-run onboarding: False until the tool window has shown once and run
    # its onboarding flow (auto-open Settings + Cloud pre-select). Persisted so
    # onboarding fires exactly once across sessions; absence in settings.json
    # reads as False (first run).
    first_run_done: bool = False

    # Last folder used by the Save/Load Prompts dialogs. Persisted; empty means
    # "never picked" and the dialogs fall back to the shipped example folder.
    last_prompt_dir: str = ""

    # Story / FBX export path used by future story-export action.
    story_path: str = ""
    # Story track PassThrough: gaps between clips show the underlying take's
    # animation through. Default True so gap preservation works out of the box;
    # bottom-track ordering alone handles version override.
    story_passthrough: bool = True
    # Story export: False (default) adds a new _vNNN FBX on a new bottom track;
    # True overwrites the newest existing version in place.
    story_overwrite_fbx: bool = False

    duration: int = 0
    # ``steps`` is the effective diffusion-step count sent to the server, driven
    # by the General quality preset (``steps_preset``).
    steps: int = 300
    steps_preset: str = "standard"   # draft | standard | fine | custom
    model: str = "kimodo-soma-rp-v1.1"
    seed: int = 0
    random_seed: bool = True
    advanced_on: bool = False

    # Advanced (maya_kimodo parity) ---------------------------------------
    num_samples: int = 1
    cfg_type: str = "separated"        # "separated" | "regular" | "nocfg"
    cfg_text_weight: float = 2.0
    cfg_constraint_weight: float = 2.0
    transition_frames: int = 5
    heading_deg: float = 0.0
    # Server-side post-processing, sent as ``options.post_processing``. The only
    # cleanup lever that reaches the model; the former client-side "Foot
    # cleanup" path was deleted (it pinned ``posed_joints``, which the FK-only
    # apply never reads). Documented server-side effect is root-drift
    # correction (maya_kimodo's root_margin tooltip) -- nothing in the MMCP
    # contract claims it touches feet, so don't promise that in the UI.
    post_processing: bool = True
    # Horizontal margin (m) used by server post-processing when deciding whether
    # to correct root drift. Collected + persisted but NEVER sent -- unwired in
    # maya_kimodo too (main_window.py:1719 TODO(backend)).
    root_margin: float = 0.04

    pose_prompt: str = ""

    # Animation target: where the generated motion lands.
    animation_mode: str = "new_take"   # "story" | "new_take" | "existing_take"
    take_name: str = ""                 # used by new_take (custom) and existing_take
    # When True (default), a new take is named from the first
    # ``take_name_length`` chars of the earliest timeline prompt; collisions get
    # a _v001/_v002… suffix. When False, the custom ``take_name`` above is used
    # instead (still versioned).
    auto_take_name: bool = True
    # How many leading prompt chars seed the auto take name (clamped 1–120).
    take_name_length: int = 50
    # When True, generated motion starts from the character's current scene XZ
    # at the prompt's start frame instead of the world origin (the request is
    # canonicalised to origin; this re-applies the scene offset on apply).
    use_hip_pos: bool = True
    # When True, the rig's CURRENT hip height is sent to the model as a
    # Hips-only start anchor (plane-relative constraint root Y) so the motion
    # is generated at that height — the server-side Y analog of use_hip_pos's
    # XZ re-seat. Creates no apply-side offset (item-1 redefinition).
    preserve_height: bool = False
    # Single-pose placement (decoupled from the motion flags above — patching
    # these must NOT touch use_hip_pos / preserve_height). "Use current
    # position": key the generated pose at the rig's current world XZ + facing
    # (yaw); off lands it at the origin facing canonical forward.
    pose_use_xz: bool = True
    # "Keep current height": keep the rig's current hip Y as-is (its offset
    # relative to the ground plane rides along); off seats the pose on the
    # ground plane using its own generated height.
    pose_keep_height: bool = True
    # Master gate for the ground-plane fold: when True (default), the plane's
    # live world-Y is folded into the vertical offset in EVERY generation mode
    # (story included); when False no mode folds it. Generation math only —
    # the marker/line viz rides the plane regardless.
    ground_offset_enabled: bool = True
    # Gated ground correction (default OFF): subtract the response's MEASURED
    # ground float (core.ground_measure, stashed on each sample by the worker
    # as ``ground_summary``) from the applied root Y so the planted foot lands
    # ON the ground instead of the server's ~+2 cm above it. Acts only on
    # canonical-skeleton responses whose measurement std passes the trust gate
    # (correction_from_summary). Consequence: pinned poses land shifted down
    # by the correction amount — the exact WYSIWYG-pin guarantee holds only
    # with this OFF (see animator.apply_animation's Y-convention comment).
    ground_correction_enabled: bool = False
    # The same idea for CAPTURE, and default ON — the opposite of the line
    # above, on purpose. Generation's correction is a mitigation for a ~2 cm
    # server float, and it costs the WYSIWYG pin guarantee, so the user opts
    # in. A capture has no pins to lose and its whole promise is "the clip's
    # motion, grounded": the character standing off the floor is the bug the
    # user reported, not a refinement. Measured on the USER'S RIG from the
    # service's own contacts (core.ground_measure.rig_ground_offset), and
    # skipped whenever the contact spread fails the capture-specific trust
    # gate — so switching this off is for reproducing an old take, not for
    # rescuing a bad measurement.
    capture_ground_correction: bool = True
    # When True (default), generated keys are forced onto the take's base layer
    # (index 0) regardless of the active layer selection. Key-placement only —
    # non-base additive layers still contribute to the evaluated pose. Off keys
    # onto the currently active layer.
    base_layer_only: bool = True
    # When True, generated skeleton motion is plotted onto the HIK Control Rig
    # after apply and the HIK input source is re-enabled, leaving a
    # characterized, hand-editable result. No-op without an HIK character or in
    # story mode (motion lives in a passthrough clip, not on skeleton keys).
    bake_to_control_rig: bool = False
    # When True (default), the bake plots the WHOLE take. Off scopes every bake
    # to the span it just applied, so a per-block bake can't overwrite sibling
    # blocks' rig keys. Defaults ON because scoping has one case it can't cover:
    # an existing-take apply clears skeleton keys wholesale but a scoped bake
    # only re-plots the rig inside the new span, so rig keys outside it survive
    # and keep driving the character. Whole-take is the safe default; turn it off
    # deliberately to protect sibling blocks.
    bake_whole_range: bool = True
    # Generate-Selected dialog (per-block regen): auto-pin the block's own
    # start/end poses as fullbody ``pose_keyframe`` anchors so the seams with the
    # neighbouring prompts stay smooth. Both default ON; a boundary is skipped
    # when a manual constraint already sits at that frame (manual wins).
    pin_start_pose: bool = True
    pin_end_pose: bool = True
    auto_constraint: bool = False
    key_pose: bool = True

    sync_transport: bool = True

    # When True, every /generate round-trip is dumped to disk under
    # ``debug_dir`` so the request payload, raw glTF response, and parsed
    # motion summary can be inspected offline. No-op otherwise.
    debug_capture: bool = False
    debug_dir: str = ""

    # Debug experiment: when True, build_request omits the ``timing`` block
    # entirely (no ``fps`` sent). MMCP v1 normally requires the model fps and
    # rejects a mismatch -- this lets us probe what the server does without it.
    debug_omit_timing: bool = False

    # Debug experiment: when True, build_request skips the auto-injected frame-0
    # ``root_path`` [0,0] anchor. Every recorded rationale for that anchor is
    # PINNED-case evidence (10.6 -> 10.8 -> 10.9); the pin-free, text-only branch
    # has never been measured, and Proscenium's own comment says the model starts
    # at origin by default there -- i.e. the anchor may just restate the default.
    # This buys the A/B. See context/changes/root-path-constraint/frame.md.
    debug_omit_root_anchor: bool = False

    # Debug experiment: when True, effector_target wires carry the pin's
    # captured local quaternion as the optional ``rotations`` array. Default
    # OFF -- the hosted server IGNORES the field (rotation_probe_20260726:
    # resending a byte-identical request with rotations added moved the pinned
    # hand by exactly 0.0), but a local kimodo_server DOES consume it in
    # _build_end_effector_dict, so this is a real lever on the local path
    # only. A pin dragged after capture keeps its quaternion flagged
    # ``rotation_stale`` (constraint_viz marks instead of popping) and ships
    # it anyway, with a soft warning naming the pin.
    debug_send_effector_rotations: bool = False

    # Debug experiment: when True, build_request ships the SCENE fps in the
    # ``timing`` block instead of the model fps. Only the timing VALUE changes:
    # segment durations and constraint frames stay unscaled.
    # ``debug_omit_timing`` still wins (no block at all).
    #
    # ANSWERED 2026-08-08 (research Q6) -- the server REJECTS it, in its own
    # words: "timing.fps=24.0 does not match model fps=30.0; server-side
    # resampling is not implemented in v1" (24 fps scene, 30 fps model). The
    # May-2026 claim (commit f3b6ec1) is CONFIRMED, so a client-side resample is
    # the only route to an arbitrary scene fps -- see the TODO(fps-resample) in
    # request_builder. Kept in tree as the regression check if the server ever
    # ships resampling; there is no reason to re-run it otherwise (the rejection
    # took 62.8s, i.e. a full generation cycle, not a fast edge check).
    debug_send_scene_fps: bool = False

    # When True, build_request derives a body heading for every ``root_path``
    # waypoint from the path geometry (face toward the next waypoint) and sends
    # it as ``heading_radians`` on the wire. Default OFF since 2026-07-31 (ON
    # 2026-06-03..2026-07-31): the A/B suggests the derived facing may
    # over-constrain turning, so heading is opt-in from Settings -> Extra
    # options. The facing convention itself remains verified live in MoBu
    # (both sides face correctly -- see reference_mmcp_heading_convention);
    # the front-cross viz is independent and always on. maya_kimodo sends no
    # heading. Session-only (not persisted); pinned by
    # tests/test_request_builder.py::test_send_path_heading_default_off.
    send_path_heading: bool = False

    # When True, build_request auto-injects a same-frame ``root_path`` waypoint
    # carrying the rig's REAL pelvis XZ + heading for every hand/foot pin frame
    # with no user root/pose coverage (the ``_eph_root_ctx`` injection shipped
    # in 8dea32c). Sent bare, an effector pin makes the server fabricate pelvis
    # XZ/height/facing from a T-pose (the slide/push-back mechanism) -- but the
    # injected waypoint also hard-pins the pelvis, which over-constrains the
    # trajectory the model would otherwise solve. Default OFF since 2026-07-27:
    # the request carries only what the user authored, and the injection is the
    # opt-in A/B lever rather than the baseline.
    send_effector_root_context: bool = False

    # Which root wins when a user ``root_path`` waypoint shares a frame with a
    # hand/foot pin (the server's same-frame dedup is deterministic
    # last-listed-wins). Both states reorder the wire deterministically —
    # marker creation order never decides (the pre-8dea32c walk order did, and
    # the 2026-07-30 controlled A/B showed a pin-first scene let the waypoint
    # win even with the reorder "off"). True: waypoint moved AFTER the effector
    # wire — the authored root wins; honest soft-reach residual on the pin
    # (~0.24 m measured) but better gross motion. False (default since
    # 2026-07-30, user decision off the controlled A/B): waypoint moved BEFORE
    # the effector wire — the effector's fabricated root wins and the pin
    # snaps near-exactly (~2 cm measured), at the cost of the pelvis ignoring
    # the waypoint on that frame (teleport-class motion risk).
    reorder_same_frame_waypoints: bool = False

    # When True, build_request copies a user ``root_path`` waypoint that lands
    # on a segment-start index onto the PRECEDING index (the ``_seam_dup`` pass),
    # so both blocks around a prompt-block seam are steered to the same XZ.
    # The mechanism is confirmed from the Kimodo model source, not inferred:
    # constraints are cropped per segment with a half-open ``>= start & < end``
    # rule over a running frame cumsum (``kimodo/constraints.py:125``), so a
    # waypoint authored on the shared boundary frame provably never reaches the
    # PRECEDING segment -- which can then run its whole length with nothing but
    # the frame-0 anchor and leave the gap to be absorbed at the seam (the
    # reported "slide"). Efficacy was the open question -- the transition blend
    # overwrites the preceding segment's last frames -- and the live A/B moved
    # it: True (default since 2026-08-08, user decision off ON/OFF pairs on the
    # reported two-block layout, matched on post_processing / transition_frames
    # / diffusion_steps, seam visibly improved with the copy present). That A/B
    # is SIGNAL, NOT PROOF -- every run drew a different seed, the one control
    # the plan asked to hold; the flip rests as much on the mechanism being
    # confirmed from the model source as on the observation. Pin the seed if
    # this default is ever re-litigated. Shipped OFF first and flipped only on
    # the measurement, the house rule for wire-altering gates here
    # (send_path_heading shipped ON
    # for two months and was flipped OFF 2026-07-31 when its A/B disagreed).
    # Pinned by tests/test_request_builder_seam_dup.py.
    duplicate_seam_waypoints: bool = True

    # Reveal the Motion Import panel group in the tool window. Hidden by
    # default (generation-first workflow); toggled from Settings and
    # remembered across sessions. Absent key loads as False (no migration).
    show_motion_import: bool = False

    # Reveal the Live Drive panel. Off by default: realtime driving is an
    # ARDY-only mode, and having it on screen made the tool look as if it
    # behaved differently depending on the selected model. Hidden, not
    # removed — the feature is verified and returns behind this switch.
    show_live_drive: bool = False

    # Auto-open the Animatica tool window once each MoBu launch. Opt-in
    # (default off); toggled from Settings and remembered across sessions.
    # Honoured by _startup.register via a deferred OnUIIdle one-shot.
    auto_open_on_startup: bool = False

    # When True (default), the skeleton root picker lists only plugin-created
    # (canonical) rigs — those stamped by bridge.skeleton.mark_canonical.
    # The host auto-falls-back to all roots when the filtered list is empty so
    # a legacy-only scene never strands the user. Unchecking shows all roots.
    canonical_only_filter: bool = True

    # Last native transport zoom window as absolute ``(start, stop)`` frames,
    # remembered so it survives a new-take generation and a window close/reopen
    # (timline-features Phase 3). ``None`` = never set; JSON round-trips it as a
    # 2-element list, normalized back to a tuple at the use site.
    zoom_window: tuple[int, int] | None = None

    logs: list[LogEntry] = field(default_factory=list)

    def active_character(self) -> CharacterState | None:
        if self.active_character_id is None:
            return None
        return self.characters.get(self.active_character_id)

    def ensure_character(self, character_id: str, display_name: str = "") -> CharacterState:
        cs = self.characters.get(character_id)
        if cs is None:
            cs = CharacterState(character_id=character_id, display_name=display_name or character_id)
            self.characters[character_id] = cs
            if self.active_character_id is None:
                self.active_character_id = character_id
        return cs

    def adopt_pending_prompts(self, target_id: str) -> bool:
        """Move the ``"_pending"`` bucket's authored prompts onto *target_id*.

        Pre-rig authoring lands under the fallback character key ``"_pending"``
        (see ``gui.tool_window._ensure_timeline_character``); when the first
        canonical skeleton is created those boxes would otherwise be orphaned
        by the character-key swap. This is the pure state move: ``prompts`` and
        ``active_prompt_idx`` transfer together (a split would desync the
        active-block highlight) and ``_pending`` is emptied so a later
        character cannot re-inherit. Constraints are deliberately untouched --
        they cannot exist pre-rig (capture requires a live joint map).

        Returns ``True`` only when a move happened. Skip-guards (each returns
        ``False`` without mutating): *target_id* is ``"_pending"`` itself; no
        ``_pending`` bucket or its ``prompts`` empty; the target bucket
        (auto-created via ``ensure_character``) already has prompts.
        """
        if target_id == "_pending":
            return False
        pending = self.characters.get("_pending")
        if pending is None or not pending.prompts:
            return False
        target = self.ensure_character(target_id)
        if target.prompts:
            return False
        target.prompts = pending.prompts
        target.active_prompt_idx = pending.active_prompt_idx
        pending.prompts = []
        pending.active_prompt_idx = None
        return True


def _filter_kwargs(cls, data: dict) -> dict:
    """Keep only keys that name a field of dataclass *cls*.

    Defends ``cls(**data)`` against a schema bump adding an unknown key (which
    would otherwise raise ``TypeError`` and crash a load).
    """
    allowed = {f.name for f in _dc_fields(cls)}
    return {k: v for k, v in data.items() if k in allowed}


def _character_from_dict(cid: str, cdata: dict) -> CharacterState:
    """Rebuild one ``CharacterState`` from its serialized dict.

    Shared by ``from_dict`` (settings sidecar) and ``prompts_from_dict``
    (in-scene store) so the two reconstruction paths can't drift.
    """
    prompts = [PromptBox(**_filter_kwargs(PromptBox, p)) for p in cdata.get("prompts", [])]
    constraints = [
        ConstraintMarker(**_filter_kwargs(ConstraintMarker, c)) for c in cdata.get("constraints", [])
    ]
    return CharacterState(
        character_id=cdata.get("character_id", cid),
        display_name=cdata.get("display_name", cid),
        prompts=prompts,
        constraints=constraints,
        active_prompt_idx=cdata.get("active_prompt_idx"),
    )


def to_dict(state: AppState) -> dict[str, Any]:
    return asdict(state)


def from_dict(data: dict[str, Any]) -> AppState:
    chars_raw = data.get("characters") or {}
    characters: dict[str, CharacterState] = {}
    for cid, cdata in chars_raw.items():
        characters[cid] = _character_from_dict(cid, cdata)
    logs = [LogEntry(**l) for l in data.get("logs", [])]
    state = AppState(
        characters=characters,
        active_character_id=data.get("active_character_id"),
        logs=logs,
    )
    for k, v in data.items():
        if k in ("characters", "active_character_id", "logs"):
            continue
        if hasattr(state, k):
            setattr(state, k, v)
    return state


# ---------------------------------------------------------------------------
# In-scene (.fbx) prompt persistence subset
# ---------------------------------------------------------------------------
# ``to_dict``/``from_dict`` round-trip the *whole* AppState (settings sidecar).
# The in-scene store carries only the authoring data that should travel inside
# the scene file -- per-character prompts/constraints -- and deliberately omits
# global settings (server URL, auth, toggles) which belong in settings.json and
# must not be baked into a shared .fbx.


def prompts_to_dict(state: AppState) -> dict[str, Any]:
    """Serialize the per-character prompt/constraint subset for in-scene storage."""
    return {
        "version": PROMPTS_SCHEMA_VERSION,
        "active_character_id": state.active_character_id,
        "characters": {
            cid: {
                "character_id": cs.character_id,
                "display_name": cs.display_name,
                "prompts": [asdict(p) for p in cs.prompts],
                "constraints": [asdict(c) for c in cs.constraints],
                "active_prompt_idx": cs.active_prompt_idx,
            }
            for cid, cs in state.characters.items()
        },
    }


def prompts_from_dict(data: dict[str, Any], into: AppState | None = None) -> AppState:
    """Rebuild per-character prompt/constraint state from ``prompts_to_dict`` output.

    When *into* is given, its ``characters``/``active_character_id`` are replaced
    in place (preserving every global setting on it) and it is returned; otherwise
    a fresh ``AppState`` is built. Unknown keys are ignored (forward-compat via the
    ``version`` field), so a pre-feature or newer payload never crashes the load.
    """
    state = into if into is not None else AppState()
    chars_raw = data.get("characters") or {}
    characters: dict[str, CharacterState] = {}
    for cid, cdata in chars_raw.items():
        characters[cid] = _character_from_dict(cid, cdata)
    state.characters = characters
    state.active_character_id = data.get("active_character_id")
    return state
