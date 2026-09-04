"""The shared A/B scenario — checkpoints c0..c4, one incremental session.

Stage P3 of PLAN-testy-ab.md (§3). One scenario, cumulative, exactly the
decided checkpoint list: c0 builds the model's canonical rig and exports it
bare; c1 adds prompt block 1; c2 adds prompt block 2; c3 adds a Path with
three waypoints (one on the prompt seam, the trajectory turning in both
horizontal axes); c4 adds a left-hand effector pin. Scene state flows forward
(that is how a user works), every checkpoint exports an FBX and emits a
manifest, and a failed checkpoint marks everything after it SKIP with words
("stan po cN niewiarygodny") — never a run on broken state, never a false
red.

Every authored number here is FIXED (prompt texts, frames, waypoint XZ,
pin position, seed): an authored value that drifts between two runs is a
real regression, and layer 1 (``ab_compare.canonicalize_request``) compares
it exact. Requests are built by the REAL product path —
``request_builder.build_request`` over the same ``AppState`` /
``CharacterState`` / ``PromptBox`` / ``ConstraintMarker`` model the tool
window authors into — so a drift in the builder is caught even when the
motion still looks right (axis C of the decisions plan).

What the host injects (:class:`HostABScenario`) and what stays shared: the
rig is built and deleted through ``animatica_core.bridge`` (builder), the
scene through ``gates.scene_api`` (``new_scene`` keeps its Q1(b) refusal —
:class:`~animatica_core.gates.scene_api.SceneNotEmptyError` propagates to
the wrapper, which words the SKIP). The host hands in the model's
``/capabilities`` entry (the scenario NEVER talks HTTP — the wrapper wires
``generate`` to the ab_cassette replay by default, or to the live server),
the product apply path, and the FBX export callable. The rig's joint count
is parameterized by the injected canonical skeleton, not hardcoded.

Retarget regime: this scenario builds the model's OWN canonical rig, so
generation runs the ``retarget: none`` fast path in every host. The
manifests say so first-class; they must never suggest coverage of the
``hik``/``server`` paths (PLAN-testy-ab v4.3 — that is checkpoint C5,
undecided).

Artifact interface (consumed by the ab_suite orchestrator,
``tools/ab_suite/run.py`` in the SDK repo — keep the two in step):

* Layout is FLAT: ``output_dir/<checkpoint>.manifest.json`` with the FBX
  beside it, named ``<checkpoint>.fbx`` (the manifest's ``fbx`` field is
  that FILENAME, relative to the output dir).
* Manifest keys:
  - ``checkpoint``   — ``"c0"`` .. ``"c4"``;
  - ``status``       — ``"PASS" | "FAIL" | "SKIP"`` (SKIP = cascade);
  - ``reason``       — words when not PASS, ``""`` otherwise;
  - ``request``      — the AUTHORED ``/generate`` request exactly as sent;
    canonicalization is a comparison-time operation (compare_requests
    canonicalizes both sides), so the manifest stays lossless for the
    cassette re-key migration and the report. OMITTED for c0 (bare rig, no
    generation) and for a checkpoint that failed before building one —
    never null: the orchestrator compares the request axis only when both
    sides carry the key;
  - ``canonical_request_hash`` — additive; present exactly where
    ``request`` is (``ab_compare.canonical_request_hash`` — THE cassette
    key);
  - ``fbx``          — ``"<checkpoint>.fbx"``, or null when the checkpoint
    did not export (FAIL/SKIP);
  - ``profile``      — the canonicalization profile used (JSON lists);
  - ``meta``         — every ``ab_compare.PROVENANCE_FIELDS`` key. The
    scenario is authoritative for ``generator`` / ``scenario_version`` /
    ``retarget_regime`` / ``model_id`` / ``backend``; ``blender_version``
    and ``dump_format_version`` are the judge's own and stay null here
    (the dumper stamps them on the dump at judge time).

Checkpoint filtering: ``run(..., checkpoints=("c2",))`` still executes
every step up to c2 (the scenario is incremental — c2's scene state IS
c0+c1+c2) but exports and writes manifests only for the requested ids.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

#: Bumped when the authored scenario content changes; part of provenance —
#: two manifests from different scenario versions are NOT comparable.
SCENARIO_VERSION = 1

#: This scenario builds the model's own canonical rig, so generation takes
#: the server's "retarget unnecessary" fast path in every host. First-class
#: in the manifest (fingerprint spec): green here says NOTHING about the
#: ``hik`` or ``server`` retarget paths.
RETARGET_REGIME = "none"

#: The checkpoint ids, in run order. Lowercase — the orchestrator's own set.
CHECKPOINTS = ("c0", "c1", "c2", "c3", "c4")

#: The authored content — all FIXED, exact on the wire after canonicalization.
SEED = 20260901
TOTAL_FRAMES = 100
# (text, start, end) HALF-OPEN [start, end) -- the product convention: the
# timeline widget defines duration = end - start and places the next block
# ON the previous block's end (gui/timeline/widget.py), so two blocks share
# one boundary NUMBER (0-60 / 60-100) while the server assigns the boundary
# frame index to the following segment (60 + 40 = 100, adjacent, no hole).
# The user's rule "the end frame of prompt 1 is the start frame of prompt 2"
# reads exactly like this; the earlier inclusive (0,59)/(60,99) authoring
# rendered as adjacent numbers (61/62 in 1-based reading) and looked like a
# one-frame gap that never existed on the wire (verified: identical request
# hashes either way).
PROMPT_1 = ("a man walks forward", 0, 60)
PROMPT_2 = ("then jumps once", 60, 100)
# Three waypoints (Matt, 2026-09-02): one sits ON the prompt seam (frame 60,
# the shared boundary frame of the half-open blocks), and the trajectory
# turns -- both horizontal coordinates differ between consecutive
# waypoints, so a wrong sign or a swapped axis on either host (Max is Z-up,
# the wire Y-up) reddens instead of hiding on a straight line.
PATH_WAYPOINTS = ((20, (0.0, 1.0)), (60, (0.6, 1.6)), (80, (0.0, 2.0)))   # (frame, (x, z))
# The hand pin keeps its height and moves onto the path (Matt, 2026-09-02):
# horizontally it is the MIDPOINT of the two waypoints it sits between --
# frames 20 (0.0, 1.0) and 60 (0.6, 1.6) -- so the arm target is reachable
# from where the body actually is, instead of hanging off to the side as
# (0.35, 0.40) did. Frame 50 is 3/4 of the way from 20 to 60, so a
# frame-proportional reading would be (0.45, 1.45); the midpoint is the
# literal one and the one authored here.
HAND_PIN = (50, "LeftHand", (0.30, 1.20, 1.30))         # frame, joint, pos m

#: The default layer-1 canonicalization profile for this scenario. Nothing
#: authored is volatile and nothing comes from a scene-state read (no
#: ``origin_offset``, no captured anchors), so nothing needs rounding; the
#: skeleton block is the one section that differs between hosts BY DESIGN
#: (PLAN-testy-ab §1 last row). JSON-shaped (lists, not tuples) because it
#: rides inside every manifest.
DEFAULT_PROFILE = {
    # Representation differences between the hosts' request builders,
    # each measured on the real c1 manifests of 2026-09-02 (Max/MoBu vs
    # Blender) — never semantics (the rule: a fold must change no byte
    # of the server's answer):
    #   display_name        Max/MoBu stamp null on joints, Blender omits —
    #                       presentation only.
    #   rest_translation    Blender stores float32 (mathutils), Max/MoBu
    #                       float64: ~1e-8 apart; 6 decimals is 1 µm, three
    #                       orders below the 1 mm integrity threshold.
    #   (the sign of zero — the bare rig's root anchor reads -0.0 from
    #   float32 and 0.0 from float64, the last c1 difference found at the
    #   JSON level — folds globally in core since b812cc0.)
    #   positions_xz /       Blender's curve points and pin positions are
    #   positions            float32 too: the authored waypoint 0.6 arrives
    #                        as 0.6000000238418579 (measured on the c3
    #                        request after the root_path repair, 2026-09-02),
    #                        Max/MoBu send 0.6. Same mechanism as
    #                        rest_translation, same 6-decimal floor (1 µm);
    #                        a float32 model sees one value either way. A
    #                        real 1 mm offset in the same field still diffs.
    #   timing.fps          Blender sends no timing section; measured on the
    #                       live server: the answer without it is bitwise
    #                       identical to the one with {"fps": 30}.
    #   segment seed        Blender repeats options.seed on every segment;
    #                       lifted only when EQUAL (a different one diffs).
    #   transition_frames   Blender writes it for a single segment too;
    #                       measured: 0, 5 or absent give the identical
    #                       answer for one segment (multi-segment compares).
    "volatile": ["display_name"],
    "measured": ["rest_translation", "positions_xz", "positions"],
    "measured_decimals": 6,
    "host_sections": ["skeleton"],
    "defaults": {"timing.fps": 30},
    #   (the frame-0 anchor is NOT folded here: the cassette keys on this
    #   profile, and the server's answer to an anchor at r0 is different
    #   BYTES from its answer to the rest anchor -- the root rides r0. One
    #   key for both would replay the other host's clip 8 cm off. See
    #   CROSS_HOST_PARITY_PROFILE for the semantic view.)
    "equivalences": ["segment_seed_lifts_to_options",
                     "single_segment_transition_frames_is_noise"],
}

#: The profile for CROSS-HOST request parity (axis B, the campaign
#: report): DEFAULT_PROFILE plus the frame-0 anchor translation. Max and
#: Blender anchor frame 0 where the rig stands, MoBu at the canonical rest
#: and reseats on apply; the server is translation-equivariant on the
#: whole constraint set (measured 2026-09-02), so the user gets one clip
#: and the two requests are one request SEMANTICALLY -- c2 (anchor only)
#: folds onto one form; c3/c4 (anchor moved, authored waypoints fixed)
#: keep a different relative geometry and stay a NAMED divergence. Never
#: the cassette's profile: keys speak bytes, parity speaks semantics.
CROSS_HOST_PARITY_PROFILE = {
    **DEFAULT_PROFILE,
    "equivalences": list(DEFAULT_PROFILE["equivalences"])
    + ["whole_constraint_set_translates_to_frame_zero_anchor"],
}

#: The SKIP-cascade wording (PLAN-testy-ab §3) — asserted by the teeth.
SKIP_CASCADE = ("SKIP-cascade: stan po {failed} niewiarygodny -- {cp} nie "
                "biegnie na zepsutym stanie")


@dataclass
class HostABScenario:
    """What one host injects into the shared scenario.

    Everything else the scenario needs travels through
    ``animatica_core.bridge`` (builder, scene verbs) — registered by the
    plugin at startup, exactly as for every other shared gate.
    """

    #: One entry of ``GET /capabilities .models[]`` — INJECTED, never
    #: fetched here. The wrapper decides where it comes from (a recorded
    #: golden file in replay mode, the live server in record mode).
    model_caps: dict
    #: ``request_dict -> glTF response dict``. The wrapper wires this to
    #: ``ab_cassette.Cassette.replay`` (default) or to the live
    #: ``/generate``; a :class:`~animatica_core.gates.ab_cassette
    #: .CassetteMiss` surfaces as this checkpoint's worded FAIL.
    generate: object
    #: ``(joint_map, motion_data) -> None`` — key the parsed motion onto
    #: the rig at frame 0 through the PRODUCT apply path
    #: (``animation_target.apply_to_target`` with the host's own kwargs),
    #: never a reimplementation.
    apply: object
    #: ``(root_node, out_dir) -> path`` — export the rig + current take to
    #: an FBX inside *out_dir* and return the written path. The scenario
    #: renames it to ``<checkpoint>.fbx``.
    export_fbx: object
    #: Provenance ``meta.generator`` — the host id ("mobu", "max", ...).
    generator: str
    #: Provenance ``meta.backend`` — which server family answered/recorded.
    backend: str = "local"
    #: Layer-1 canonicalization profile; None -> :data:`DEFAULT_PROFILE`.
    profile: dict = None
    #: ``() -> (x, z) | None`` — where the rig's root stands at the take's
    #: start frame, in the WIRE frame. Called before every build_request;
    #: the value becomes the injected frame-0 root anchor
    #: (``build_request(root_anchor_xz=...)``). None / absent -> the
    #: canonical rest anchor ``(0, 0)``, byte-identical to before the hook
    #: existed. Matt, 2026-09-02: "from where the character stands" is
    #: Max's and Blender's convention; MoBu stays on the rest anchor.
    root_anchor_xz: object = None


# ---------------------------------------------------------------------------
# Authoring steps — the product data model, exactly what the window writes
# ---------------------------------------------------------------------------

def _author_c1(cs):
    from animatica_core.core.prompt_model import PromptBox
    text, start, end = PROMPT_1
    cs.prompts.append(PromptBox(start=start, end=end, text=text,
                                id="ab-c1-prompt"))


def _author_c2(cs):
    from animatica_core.core.prompt_model import PromptBox
    text, start, end = PROMPT_2
    cs.prompts.append(PromptBox(start=start, end=end, text=text,
                                id="ab-c2-prompt"))


def _author_c3(cs):
    from animatica_core.core.prompt_model import ConstraintMarker
    for frame, (x, z) in PATH_WAYPOINTS:
        cs.constraints.append(ConstraintMarker(
            frame=frame, joint="", type="root2d",
            value={"xz": [float(x), float(z)]}))


def _author_c4(cs):
    from animatica_core.core.prompt_model import ConstraintMarker
    frame, joint, pos = HAND_PIN
    cs.constraints.append(ConstraintMarker(
        frame=frame, joint=joint, type="left-hand",
        value={"position": [float(c) for c in pos]}))


_STEPS = (
    ("c0", None),
    ("c1", _author_c1),
    ("c2", _author_c2),
    ("c3", _author_c3),
    ("c4", _author_c4),
)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _write_manifest(output_dir, cp, status, reason, request, fbx_name,
                    host, profile):
    """Write ``<cp>.manifest.json``; returns its path. Schema: module doc."""
    from animatica_core.gates.ab_compare import (PROVENANCE_FIELDS,
                                                 canonical_request_hash)
    meta = {field: None for field in PROVENANCE_FIELDS}
    meta.update({
        "generator": host.generator,
        "scenario_version": SCENARIO_VERSION,
        "retarget_regime": RETARGET_REGIME,
        "model_id": (host.model_caps or {}).get("id"),
        "backend": host.backend,
    })
    manifest = {
        "checkpoint": cp,
        "status": status,
        "reason": reason,
        "fbx": fbx_name,
        "profile": profile,
        "meta": meta,
    }
    if request is not None:
        # c0 (and a checkpoint that failed before building a request) OMITS
        # the key entirely — the orchestrator compares the request axis only
        # when both sides carry one.
        # The AUTHORED request, not its canonical form: canonicalization is
        # a comparison-time operation (compare_requests canonicalizes both
        # sides), and the manifest is the source of truth the cassette
        # re-key migration and the campaign report read -- a canonical form
        # is lossy under any non-identity profile (measured 2026-09-02:
        # rest_translation rounded, display_name gone). One convention on
        # three hosts; the hash below is computed from this same request.
        manifest["request"] = request
        manifest["canonical_request_hash"] = canonical_request_hash(
            request, profile)
    path = os.path.join(output_dir, f"{cp}.manifest.json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=1)
        fh.write("\n")
    return path


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

def run(host: HostABScenario, check, output_dir, checkpoints=None):
    """Run the incremental scenario, reporting through *check*.

    *output_dir* receives the FLAT artifact layout described in the module
    docstring. *checkpoints* filters which ids EMIT artifacts (default:
    all); every step up to the last requested one still executes, because
    the scenario is incremental.

    Returns a list of result dicts — ``{"checkpoint", "status", "reason",
    "manifest", "fbx"}`` — one per executed step (``manifest``/``fbx`` are
    None for steps outside the requested set). Raises
    :class:`~animatica_core.gates.scene_api.SceneNotEmptyError` when the
    open scene holds user content (Q1 b) — the wrapper words that SKIP.
    The rig this scenario builds is deleted on the way out, pass or fail,
    so the wrapper leaves the scene as found (empty).
    """
    from animatica_core import constants
    from animatica_core.bridge import builder
    from animatica_core.gates import scene_api
    from animatica_core.gltf_parser import parse_gltf
    from animatica_core.core.prompt_model import AppState, CharacterState
    from animatica_core.core.request_builder import build_request
    from animatica_core.live.skeleton_adapter import skeleton_block_to_hierarchy

    requested = tuple(checkpoints) if checkpoints else CHECKPOINTS
    unknown = sorted(set(requested) - set(CHECKPOINTS))
    if unknown:
        raise ValueError(f"unknown checkpoint id(s) {unknown} — this "
                         f"scenario has {list(CHECKPOINTS)}")
    profile = host.profile or DEFAULT_PROFILE
    last_idx = max(CHECKPOINTS.index(cp) for cp in requested)

    canonical = (host.model_caps or {}).get("canonical_skeleton") or {}
    hierarchy, rest, _names = skeleton_block_to_hierarchy(canonical)
    fps = float(host.model_caps.get("fps") or constants.DEFAULT_FPS)

    # Q1(b): the refusal propagates — the wrapper words the SKIP.
    scene_api.new_scene()
    os.makedirs(output_dir, exist_ok=True)

    results = []
    joint_map = None
    try:
        joint_map = builder.build_neutral_skeleton(
            constants.DEFAULT_PREFIX, hierarchy=hierarchy,
            rest_positions=rest)
        root = joint_map[hierarchy[0][0]]
        # The joint count is the MODEL's, from /capabilities — never a
        # hardcoded 77 (the cloud's canonical skeleton measures 30).
        check(f"the rig is parameterized by the model's canonical skeleton "
              f"({len(joint_map)} joints)",
              len(joint_map) == len(hierarchy),
              f"{len(joint_map)} built vs {len(hierarchy)} in caps")
        scene_api.set_take_range(0, TOTAL_FRAMES - 1)

        # The product state the window would author into. Fixed seed, model
        # fps (no mismatch warnings), everything else on product defaults —
        # a changed default that reshapes the wire is exactly the drift
        # layer 1 exists to catch.
        state = AppState()
        state.random_seed = False
        state.seed = SEED
        state.fps = fps
        state.total_frames = TOTAL_FRAMES
        state.start_frame = 0
        cs = CharacterState(character_id="ab_scenario", display_name="AB")

        failed = None
        for cp, author in _STEPS[:last_idx + 1]:
            emit = cp in requested
            if failed is not None:
                reason = SKIP_CASCADE.format(failed=failed, cp=cp)
                print(f"  skip {cp} -- {reason}", flush=True)
                manifest = (_write_manifest(output_dir, cp, "SKIP", reason,
                                            None, None, host, profile)
                            if emit else None)
                results.append({"checkpoint": cp, "status": "SKIP",
                                "reason": reason, "manifest": manifest,
                                "fbx": None})
                continue

            request = None
            fbx_name = None
            status, reason = "PASS", ""
            try:
                if author is not None:
                    author(cs)
                if cp != "c0":
                    # The REAL request path — a warnings list is passed so
                    # soft caps never raise; the content is fixed, so any
                    # warning would itself be drift worth seeing in the log.
                    warnings = []
                    # Built the way the product builds it (tool_window's
                    # full-timeline generate): explicit segments with
                    # duration = end - start and the group's frame range --
                    # never request_builder's legacy inclusive build_segments
                    # (e - s + 1), which no product path reaches. A/B must
                    # test what the product does.
                    boxes = sorted(cs.prompts, key=lambda b: b.start)
                    anchor = (host.root_anchor_xz()
                              if host.root_anchor_xz is not None else None)
                    request = build_request(
                        state=state, character_state=cs,
                        model_caps=host.model_caps, seed_override=SEED,
                        root_anchor_xz=anchor,
                        segments_override=[
                            {"text": b.text,
                             "duration_frames": max(1, b.end - b.start)}
                            for b in boxes],
                        frame_offset=0,
                        frame_range_override=(min(b.start for b in boxes),
                                              max(b.end for b in boxes)),
                        warnings=warnings)
                    for w in warnings:
                        print(f"  ..   {cp} warning: {w}", flush=True)
                    payload = host.generate(request)
                    motion = parse_gltf(payload)
                    host.apply(joint_map, motion)
                if emit:
                    written = host.export_fbx(root, output_dir)
                    fbx_name = f"{cp}.fbx"
                    target = os.path.join(output_dir, fbx_name)
                    if os.path.abspath(written) != os.path.abspath(target):
                        os.replace(written, target)
            except Exception as exc:                        # noqa: BLE001
                status = "FAIL"
                reason = f"{type(exc).__name__}: {exc}"
                fbx_name = None
                failed = cp

            check(f"{cp} completed", status == "PASS", reason)
            manifest = (_write_manifest(output_dir, cp, status, reason,
                                        request, fbx_name, host, profile)
                        if emit else None)
            results.append({"checkpoint": cp, "status": status,
                            "reason": reason, "manifest": manifest,
                            "fbx": fbx_name})
    finally:
        # The scenario deletes what it created — the wrapper must be able
        # to leave the scene as found (empty).
        try:
            if joint_map is not None:
                builder.delete_skeleton_from_root(joint_map[hierarchy[0][0]])
            else:
                builder.delete_skeleton(constants.DEFAULT_PREFIX)
        except Exception as exc:                            # noqa: BLE001
            print(f"  ..   cleanup: rig delete failed -- {exc}", flush=True)

    return results
