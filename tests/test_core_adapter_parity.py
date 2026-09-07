"""B0 as a regression: the pure adapter + core must still hash to the goldens.

Side A ("the addon as it shipped") is the ``request`` frozen in the Blender
A/B golden manifests, together with the ``canonical_request_hash`` computed
over it. Side B is ``core_adapter``'s pure mapping (``state_from_settings`` /
``prompt_boxes`` / ``markers_from_*`` / ``character_state``) handed to
``animatica_core.core.request_builder.build_request``.

Two reads are NOT builder decisions — they come off the Blender scene — so
they are injected from the golden itself, exactly as
``scratchpad/b2/parity_check.py`` does: ``skeleton_override`` is the golden's
``skeleton`` block, and ``root_anchor_xz`` is the XZ of its injected frame-0
root anchor.

Needs no Blender and no network; skips (loudly) when the goldens are not on
this machine — see ``conftest.golden_dir``.
"""

from __future__ import annotations

import pytest

from conftest import CHECKPOINTS


#: The scenario owns its own numbers. An earlier revision of this file pinned
#: ``HAND_PIN`` to a golden-era value because the Blender goldens predated a
#: change to it; the goldens have since been re-recorded, so reading the
#: scenario directly is now both simpler and correct. Pinning it again would
#: only re-create the same staleness in the other direction.


class GoldenSettings:
    """The settings surface for the golden run: the same INPUT numbers the
    other A/B hosts used (300 steps, separated [2.0, 2.0], 5 transition
    frames)."""

    def __init__(self, seed):
        self.seed = int(seed)
        self.last_used_seed = 0
        self.quality_preset = "CUSTOM"      # unknown preset -> custom_steps
        self.custom_steps = 300
        self.post_processing = True
        self.num_transition_frames = 5
        self.cfg_enabled = True
        self.cfg_text = 2.0
        self.cfg_constraint = 2.0


def _blocks(checkpoint, ab_scenario, block_cls):
    """The scenario's prompts as the addon holds them: INCLUSIVE blocks.

    ``ab_scenario`` authors half-open ``PromptBox`` spans (0-60, 60-100); the
    addon's ``PromptBlock`` is inclusive, so the same two blocks are 0-59 and
    60-99 — which is what ``blender_runner`` feeds the addon's builder.
    """
    text, start, end = ab_scenario.PROMPT_1
    out = [block_cls(text, start, end - 1)]
    if checkpoint in ("c2", "c3", "c4"):
        text, start, end = ab_scenario.PROMPT_2
        out.append(block_cls(text, start, end - 1))
    return out


def _markers(checkpoint, ab_scenario, adapter, frame_range):
    markers = []
    if checkpoint in ("c3", "c4"):
        frames = [f for f, _ in ab_scenario.PATH_WAYPOINTS]
        points = [xz for _, xz in ab_scenario.PATH_WAYPOINTS]
        markers.extend(adapter.markers_from_root_path(points, frames,
                                                      frame_range))
    if checkpoint == "c4":
        frame, joint, position = ab_scenario.HAND_PIN
        markers.extend(adapter.markers_from_effector(joint, {frame: position},
                                                     frame_range))
    return markers


def _golden_anchor_xz(golden_request):
    """The XZ of the frame-0 root anchor core injected into the golden."""
    for constraint in golden_request.get("constraints") or ():
        if constraint.get("type") == "root_path" \
                and 0 in (constraint.get("frames") or ()):
            x, z = constraint["positions_xz"][0]
            return (float(x), float(z))
    return None


def _build(checkpoint, golden_request, model_caps, adapter, core, block_cls):
    ab_scenario = core.ab_scenario
    build_request = core.request_builder.build_request
    compute_frame_range = core.request_builder.compute_frame_range

    blocks = _blocks(checkpoint, ab_scenario, block_cls)
    boxes = adapter.prompt_boxes(blocks)
    # The adapter's Blender path takes this from rig_probe.compute_frame_range
    # (prompt union + action key span); with no action it is the prompt union,
    # which is exactly core's own compute_frame_range over the same boxes.
    frame_range = compute_frame_range(boxes, (0, 0))
    span = frame_range[1] - frame_range[0] + 1
    state = adapter.state_from_settings(
        GoldenSettings(ab_scenario.SEED),
        seed=ab_scenario.SEED,
        start_frame=frame_range[0],
        total_frames=span,
        fps=float(model_caps.get("fps") or 30.0),
    )
    warnings: list[str] = []
    request = build_request(
        state=state,
        character_state=adapter.character_state(
            boxes, _markers(checkpoint, ab_scenario, adapter, frame_range)),
        model_caps=model_caps,
        skeleton_override=golden_request.get("skeleton"),
        seed_override=ab_scenario.SEED,
        root_anchor_xz=_golden_anchor_xz(golden_request),
        frame_offset=0,
        frame_range_override=frame_range,
        warnings=warnings,
    )
    return request, warnings


def _pin_note(core):
    """Kept as a hook for future scenario drift; empty while none exists.

    The parity failure message carries it so a moved SCENARIO is never read as
    a moved builder. Today the test reads ``ab_scenario`` directly, so there is
    nothing to warn about.
    """
    return ""


def test_the_core_under_test_is_the_copy_vendored_in_the_addon(core):
    """Prove the parity is measured against the ADDON's core, not an SDK
    checkout that happens to sit on ``sys.path``. Run with ``-s`` to read the
    paths; they are printed either way when this test fails."""
    import animatica_core

    print(f"animatica_core : {animatica_core.__file__}")
    print(f"ab_compare     : {core.ab_compare.__file__}")
    print(f"request_builder: {core.request_builder.__file__}")
    print(_pin_note(core) or "ab_scenario.HAND_PIN matches the golden era.")
    assert "animatica-blender-plugin" in animatica_core.__file__.replace(
        "\\", "/"), (
        f"animatica_core resolved to {animatica_core.__file__} — the tests "
        f"must exercise the copy vendored in the addon, not an SDK checkout"
    )


@pytest.mark.parametrize("checkpoint", CHECKPOINTS)
def test_the_pure_adapter_reproduces_the_golden_request_hash(
        checkpoint, adapter, core, block_cls, golden_manifests,
        golden_capabilities, golden_dir):
    manifest = golden_manifests[checkpoint]
    golden_request = manifest["request"]
    profile = manifest["profile"]
    model_caps = next(m for m in golden_capabilities["models"]
                      if m.get("id") == golden_request["model"])

    built, warnings = _build(checkpoint, golden_request, model_caps, adapter,
                             core, block_cls)

    built_hash = core.ab_compare.canonical_request_hash(built, profile)
    print(f"{checkpoint}: goldens {golden_dir}")
    print(f"{checkpoint}: animatica_core {core.animatica_core.__file__}")
    print(f"{checkpoint}: golden hash {manifest['canonical_request_hash']}")
    print(f"{checkpoint}: built  hash {built_hash}")
    for warning in warnings:
        print(f"{checkpoint}: core warning: {warning[:120]}")

    note = _pin_note(core)
    diff = core.ab_compare.compare_requests(golden_request, built, profile)
    assert not diff, (
        f"{checkpoint}: the pure adapter + core no longer reproduce the "
        f"frozen request:\n  " + "\n  ".join(str(d) for d in diff)
        + (f"\n  {note}" if note else "")
    )
    assert built_hash == manifest["canonical_request_hash"], (
        f"{checkpoint}: compare_requests found no difference but the "
        f"canonical hash moved." + (f"\n  {note}" if note else "")
    )
