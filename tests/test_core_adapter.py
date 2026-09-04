"""The pure half of ``core_adapter`` — the Blender -> core mapping rules.

Nothing here touches Blender, the network or the A/B goldens: these are the
mapping decisions themselves (settings -> state, blocks -> boxes, scene
geometry -> markers), plus the few places where a mapping only means something
once core has turned it into a wire dict.
"""

from __future__ import annotations

import pytest


# A minimal model the core builder accepts: two canonical joints, retargeting
# on (so no skeleton_override is demanded of us), no per-segment seeds.
CAPS = {
    "id": "test-model",
    "fps": 30.0,
    "supports_retargeting": True,
    "canonical_skeleton": {"joints": [{"name": "Hips"},
                                      {"name": "Spine", "parent": "Hips"}]},
}
CAPS_SEG_SEED = dict(CAPS, supports_segment_seed=True)


# ---------------------------------------------------------------------------
# resolve_seed
# ---------------------------------------------------------------------------

class TestResolveSeed:
    def test_zero_becomes_a_fresh_random_seed(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter.random, "randint", lambda lo, hi: 4242)
        assert adapter.resolve_seed(0) == 4242

    def test_the_random_seed_is_drawn_from_the_positive_range(self, adapter):
        seen = []
        for _ in range(50):
            value = adapter.resolve_seed(0)
            assert value > 0
            seen.append(value)
        assert len(set(seen)) > 1, "the auto seed must not be a constant"

    def test_a_positive_seed_passes_through(self, adapter):
        assert adapter.resolve_seed(20260901) == 20260901

    def test_a_negative_seed_is_auto_too(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter.random, "randint", lambda lo, hi: 7)
        assert adapter.resolve_seed(-3) == 7

    def test_garbage_is_auto_rather_than_an_exception(self, adapter, monkeypatch):
        monkeypatch.setattr(adapter.random, "randint", lambda lo, hi: 7)
        assert adapter.resolve_seed(None) == 7
        assert adapter.resolve_seed("nope") == 7


# ---------------------------------------------------------------------------
# state_from_settings
# ---------------------------------------------------------------------------

class TestStateFromSettings:
    def test_a_known_quality_preset_picks_the_core_step_count(
            self, adapter, core, settings_cls):
        presets = core.request_builder.QUALITY_PRESETS
        assert presets, "core must publish QUALITY_PRESETS"
        for name, steps in presets.items():
            state = adapter.state_from_settings(
                settings_cls(quality_preset=name, custom_steps=999), seed=1)
            assert state.steps == steps, f"preset {name!r}"

    def test_an_unknown_preset_falls_back_to_custom_steps(
            self, adapter, core, settings_cls):
        assert "CUSTOM" not in core.request_builder.QUALITY_PRESETS
        state = adapter.state_from_settings(
            settings_cls(quality_preset="CUSTOM", custom_steps=300), seed=1)
        assert state.steps == 300

    @pytest.mark.parametrize("enabled,cfg_type",
                             [(True, "separated"), (False, "nocfg")])
    def test_the_cfg_toggle_picks_the_guidance_type(
            self, adapter, settings_cls, enabled, cfg_type):
        state = adapter.state_from_settings(
            settings_cls(cfg_enabled=enabled), seed=1)
        assert state.cfg_type == cfg_type

    def test_the_cfg_sliders_map_onto_the_two_weights(
            self, adapter, settings_cls):
        state = adapter.state_from_settings(
            settings_cls(cfg_text=3.5, cfg_constraint=1.25), seed=1)
        assert state.cfg_text_weight == 3.5
        assert state.cfg_constraint_weight == 1.25

    def test_post_processing_and_transition_frames_carry_over(
            self, adapter, settings_cls):
        state = adapter.state_from_settings(
            settings_cls(post_processing=False, num_transition_frames=11),
            seed=1)
        assert state.post_processing is False
        assert state.transition_frames == 11

    def test_the_frame_math_is_taken_verbatim_from_the_call(
            self, adapter, settings_cls):
        state = adapter.state_from_settings(
            settings_cls(), seed=99, start_frame=17, total_frames=64, fps=24.0,
            send_path_heading=True)
        assert (state.seed, state.start_frame, state.total_frames, state.fps) \
            == (99, 17, 64, 24.0)
        assert state.send_path_heading is True

    def test_the_addon_has_no_multi_sample_or_take_merge(
            self, adapter, settings_cls):
        state = adapter.state_from_settings(settings_cls(), seed=1)
        assert state.num_samples == 1
        assert state.animation_mode == ""

    def test_seed_zero_means_auto_in_the_addon_not_random_seed(
            self, adapter, settings_cls):
        # The addon resolves "auto" itself (resolve_seed) so the value can be
        # recorded; core's own random_seed path would drop options.seed.
        state = adapter.state_from_settings(settings_cls(), seed=1)
        assert state.random_seed is False

    def test_the_feature_gates_hold_their_product_defaults(
            self, adapter, settings_cls):
        state = adapter.state_from_settings(settings_cls(), seed=1)
        assert state.duplicate_seam_waypoints is True
        assert state.reorder_same_frame_waypoints is False
        assert state.send_effector_root_context is False
        assert state.send_path_heading is False
        assert state.match_scene_fps is False
        assert state.walk_speed_ms is None
        for name in ("debug_send_effector_rotations", "debug_omit_root_anchor",
                     "debug_omit_timing", "debug_send_scene_fps"):
            assert getattr(state, name) is False, name

    def test_every_gate_is_present_so_core_never_falls_back(
            self, adapter, settings_cls):
        # A missing attribute would let core's getattr default silently decide
        # behaviour; the state class exists precisely to prevent that.
        state = adapter.state_from_settings(settings_cls(), seed=1)
        for name in ("steps", "num_samples", "animation_mode", "post_processing",
                     "transition_frames", "random_seed", "seed", "cfg_type",
                     "cfg_text_weight", "cfg_constraint_weight", "start_frame",
                     "total_frames", "fps"):
            assert hasattr(state, name), name


# ---------------------------------------------------------------------------
# prompt_boxes
# ---------------------------------------------------------------------------

class TestPromptBoxes:
    def test_block_frames_stay_inclusive(self, adapter, block_cls):
        (box,) = adapter.prompt_boxes([block_cls("walk", 0, 59)])
        assert (box.start, box.end) == (0, 59)
        assert box.text == "walk"

    def test_a_disabled_block_is_dropped(self, adapter, block_cls):
        boxes = adapter.prompt_boxes([
            block_cls("walk", 0, 49),
            block_cls("never", 50, 99, enabled=False),
        ])
        assert [b.text for b in boxes] == ["walk"]

    def test_a_block_seed_rides_in_params_only_when_it_is_set(
            self, adapter, block_cls):
        pinned, auto = adapter.prompt_boxes([
            block_cls("walk", 0, 9, seed=7),
            block_cls("wave", 10, 19, seed=0),
        ])
        assert pinned.params["seed"] == 7
        assert "seed" not in auto.params

    def test_boxes_come_back_sorted_by_start_frame(self, adapter, block_cls):
        boxes = adapter.prompt_boxes([
            block_cls("late", 50, 99), block_cls("early", 0, 49),
        ])
        assert [b.start for b in boxes] == [0, 50]

    def test_no_blocks_is_no_boxes(self, adapter):
        assert adapter.prompt_boxes([]) == []
        assert adapter.prompt_boxes(None) == []


# ---------------------------------------------------------------------------
# markers_from_root_path
# ---------------------------------------------------------------------------

class TestMarkersFromRootPath:
    def test_timed_points_keep_their_own_frames(self, adapter):
        markers = adapter.markers_from_root_path(
            [(0.0, 1.0), (0.6, 1.6), (0.0, 2.0)], [20, 60, 80], (0, 99))
        assert [m.frame for m in markers] == [20, 60, 80]
        assert all(m.type == "root2d" and m.joint == "" for m in markers)
        assert markers[1].value["xz"] == [0.6, 1.6]

    def test_one_marker_per_control_point(self, adapter):
        points = [(float(i), float(i)) for i in range(5)]
        markers = adapter.markers_from_root_path(points, None, (0, 100))
        assert len(markers) == len(points)

    def test_untimed_points_spread_evenly_over_the_range(self, adapter):
        markers = adapter.markers_from_root_path(
            [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)], None, (10, 110))
        assert [m.frame for m in markers] == [10, 60, 110]

    def test_the_untimed_spread_pins_both_ends_of_the_range(self, adapter):
        markers = adapter.markers_from_root_path(
            [(0.0, 0.0)] * 4, None, (0, 99))
        assert markers[0].frame == 0
        assert markers[-1].frame == 99

    def test_a_single_untimed_point_lands_on_the_range_start(self, adapter):
        (marker,) = adapter.markers_from_root_path([(1.0, 2.0)], None, (30, 90))
        assert marker.frame == 30

    def test_the_xz_value_is_a_pair_of_plain_floats(self, adapter):
        (marker,) = adapter.markers_from_root_path([(1, 2)], [0], (0, 10))
        xz = marker.value["xz"]
        assert len(xz) == 2
        assert all(type(c) is float for c in xz)

    def test_no_points_is_no_markers(self, adapter):
        assert adapter.markers_from_root_path([], None, (0, 10)) == []
        assert adapter.markers_from_root_path(None, None, (0, 10)) == []

    @pytest.mark.xfail(
        strict=True,
        reason="FINDING (not fixed here): the timed branch is "
               "`zip(frames, pts)`, so a PROP_POINT_FRAMES list left stale by "
               "a newly added control point silently DROPS the extra points "
               "instead of failing or falling back to the even spread. "
               "Delete this marker once the adapter validates the two lengths.",
    )
    def test_a_short_frame_list_must_not_silently_drop_control_points(
            self, adapter):
        points = [(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)]
        markers = adapter.markers_from_root_path(points, [5], (0, 99))
        assert len(markers) == len(points)


# ---------------------------------------------------------------------------
# markers_from_effector
# ---------------------------------------------------------------------------

class TestMarkersFromEffector:
    def test_one_marker_per_keyed_frame(self, adapter):
        markers = adapter.markers_from_effector(
            "LeftHand", {10: (0.0, 1.0, 0.0), 20: (0.1, 1.1, 0.1),
                         30: (0.2, 1.2, 0.2)}, (0, 99))
        assert [m.frame for m in markers] == [10, 20, 30]
        assert {m.joint for m in markers} == {"LeftHand"}

    @pytest.mark.parametrize("joint,mtype", [
        ("LeftHand", "left-hand"), ("RightHand", "right-hand"),
        ("LeftFoot", "left-foot"), ("RightFoot", "right-foot"),
    ])
    def test_the_marker_type_names_the_effector_family(
            self, adapter, joint, mtype):
        assert adapter.EFFECTOR_MARKER_TYPES[joint] == mtype
        (marker,) = adapter.markers_from_effector(
            joint, {5: (0.0, 0.0, 0.0)}, (0, 10))
        assert marker.type == mtype
        assert marker.joint == joint

    def test_the_position_is_three_plain_floats(self, adapter):
        (marker,) = adapter.markers_from_effector(
            "LeftHand", {5: (0.35, 1.20, 0.40)}, (0, 10))
        position = marker.value["position"]
        assert len(position) == 3
        assert all(type(c) is float for c in position)
        assert position == [0.35, 1.20, 0.40]

    def test_markers_come_back_in_frame_order(self, adapter):
        markers = adapter.markers_from_effector(
            "LeftFoot", {30: (0, 0, 0), 10: (0, 0, 0), 20: (0, 0, 0)}, (0, 99))
        assert [m.frame for m in markers] == [10, 20, 30]

    def test_keys_outside_the_range_are_dropped(self, adapter):
        markers = adapter.markers_from_effector(
            "LeftHand", {5: (0, 0, 0), 50: (0, 0, 0), 500: (0, 0, 0)},
            (10, 99))
        assert [m.frame for m in markers] == [50]

    def test_no_keys_in_range_pins_the_range_start(self, adapter):
        # The "no keyframes -> pin at frame_range[0]" rule (diff report §3.6)
        # is applied UPSTREAM by constraints_ui.sample_effector_target, which
        # substitutes the empty's static location at the range start. This
        # function only sees the result — hand it that substitution and it
        # yields exactly one marker on the range start.
        markers = adapter.markers_from_effector(
            "RightFoot", {10: (0.0, 0.1, 0.0)}, (10, 99))
        assert len(markers) == 1
        assert markers[0].frame == 10

    def test_with_nothing_in_range_the_adapter_itself_yields_nothing(
            self, adapter):
        # Same rule seen from below: the fallback is not this function's, so a
        # sampler that returned only out-of-range keys produces no marker here.
        assert adapter.markers_from_effector(
            "RightFoot", {5: (0, 0, 0)}, (10, 99)) == []

    def test_a_joint_the_model_has_no_family_for_is_ignored(self, adapter):
        assert adapter.markers_from_effector(
            "Head", {5: (0, 0, 0)}, (0, 10)) == []
        assert adapter.markers_from_effector(
            "", {5: (0, 0, 0)}, (0, 10)) == []


# ---------------------------------------------------------------------------
# markers_from_pose
# ---------------------------------------------------------------------------

class TestMarkersFromPose:
    def test_a_sampled_pose_is_one_fullbody_marker(self, adapter):
        marker = adapter.markers_from_pose(
            12, {"Hips": [0.0, 0.0, 0.0, 1.0]}, None, "rest")
        assert marker.type == "fullbody"
        assert marker.joint == ""
        assert marker.frame == 12
        assert marker.value["joint_rotations"] == {"Hips": [0.0, 0.0, 0.0, 1.0]}

    def test_the_fill_mode_is_kept_on_the_marker(self, adapter):
        for mode in ("rest", "generate"):
            marker = adapter.markers_from_pose(0, {"Hips": [0, 0, 0, 1]},
                                               fill_mode=mode)
            assert marker.value["fill_mode"] == mode

    def test_a_root_position_is_optional_and_floated(self, adapter):
        bare = adapter.markers_from_pose(0, {"Hips": [0, 0, 0, 1]})
        assert "root_position" not in bare.value
        posed = adapter.markers_from_pose(
            0, {"Hips": [0, 0, 0, 1]}, root_position=(1, 2, 3))
        assert posed.value["root_position"] == [1.0, 2.0, 3.0]
        assert all(type(c) is float for c in posed.value["root_position"])

    @pytest.mark.parametrize("mode", ["rest", "generate"])
    def test_the_fill_mode_survives_all_the_way_to_the_wire(
            self, adapter, core, block_cls, settings_cls, mode):
        marker = adapter.markers_from_pose(
            0, {"Hips": [0.0, 0.0, 0.0, 1.0]}, fill_mode=mode)
        request = _build(adapter, core, settings_cls,
                         [block_cls("walk", 0, 29)], [marker])
        poses = [c for c in request["constraints"]
                 if c.get("type") == "pose_keyframe"]
        assert len(poses) == 1
        assert poses[0]["fill_mode"] == mode


# ---------------------------------------------------------------------------
# character_state
# ---------------------------------------------------------------------------

class TestCharacterState:
    def test_a_blender_scene_is_exactly_one_character(self, adapter, core):
        marker = adapter.markers_from_pose(0, {"Hips": [0, 0, 0, 1]})
        state = adapter.character_state([], [marker])
        assert isinstance(state, core.prompt_model.CharacterState)
        assert state.character_id == "blender"
        assert state.constraints == [marker]


# ---------------------------------------------------------------------------
# segments: prompt_boxes fed through core.build_segments / build_request
# ---------------------------------------------------------------------------

def _build(adapter, core, settings_cls, blocks, markers=(), model_caps=CAPS):
    """The pure adapter path core's builder sees, in one call."""
    boxes = adapter.prompt_boxes(blocks)
    frame_range = core.request_builder.compute_frame_range(boxes, (0, 0))
    span = frame_range[1] - frame_range[0] + 1
    state = adapter.state_from_settings(
        settings_cls(), seed=1, start_frame=frame_range[0], total_frames=span,
        fps=float(model_caps.get("fps") or 30.0))
    return core.request_builder.build_request(
        state=state,
        character_state=adapter.character_state(boxes, list(markers)),
        model_caps=model_caps,
        seed_override=1,
        frame_offset=0,
        frame_range_override=frame_range,
        warnings=[],
    )


class TestSegments:
    def test_a_gap_between_two_blocks_is_filled_unconditioned(
            self, adapter, core, block_cls):
        boxes = adapter.prompt_boxes([block_cls("walk", 0, 29),
                                      block_cls("jump", 50, 99)])
        frame_range = core.request_builder.compute_frame_range(boxes, (0, 0))
        segments = core.request_builder.build_segments(boxes, frame_range)
        assert [s["type"] for s in segments] == \
            ["text", "unconditioned", "text"]
        assert [s["duration_frames"] for s in segments] == [30, 20, 50]

    def test_a_disabled_block_leaves_only_the_enabled_one(
            self, adapter, core, block_cls):
        boxes = adapter.prompt_boxes([
            block_cls("walk", 0, 49),
            block_cls("never", 50, 99, enabled=False),
        ])
        frame_range = core.request_builder.compute_frame_range(boxes, (0, 0))
        segments = core.request_builder.build_segments(boxes, frame_range)
        assert segments == [{"type": "text", "prompt": "walk",
                             "duration_frames": 50}]

    def test_an_empty_prompt_becomes_unconditioned_not_a_gap(
            self, adapter, core, block_cls):
        boxes = adapter.prompt_boxes([block_cls("walk", 0, 49),
                                      block_cls("   ", 50, 99)])
        frame_range = core.request_builder.compute_frame_range(boxes, (0, 0))
        segments = core.request_builder.build_segments(boxes, frame_range)
        assert [s["type"] for s in segments] == ["text", "unconditioned"]
        assert segments[1]["duration_frames"] == 50

    def test_a_block_seed_reaches_the_wire_when_the_model_reads_them(
            self, adapter, core, block_cls, settings_cls):
        blocks = [block_cls("walk", 0, 29, seed=7),
                  block_cls("jump", 50, 99, seed=99)]
        request = _build(adapter, core, settings_cls, blocks,
                         model_caps=CAPS_SEG_SEED)
        assert [s.get("seed") for s in request["segments"]] == [7, None, 99]

    def test_without_that_capability_no_segment_carries_a_seed(
            self, adapter, core, block_cls, settings_cls):
        blocks = [block_cls("walk", 0, 29, seed=7),
                  block_cls("jump", 50, 99, seed=99)]
        request = _build(adapter, core, settings_cls, blocks, model_caps=CAPS)
        assert all("seed" not in s for s in request["segments"])
