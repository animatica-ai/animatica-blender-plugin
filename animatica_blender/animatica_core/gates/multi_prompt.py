"""More than one prompt box, in both shapes it takes (gate m11, shared half).

Ported from the 3ds Max plugin's ``accept_m11_multi_prompt.py`` (final gate
tranche of PLAN-suita-wieloDCC.md). "Multiple prompt boxes" is two code
paths, chosen by ``group_contiguous_boxes``: adjacent/overlapping boxes
merge into ONE request carrying several segments; a real gap splits into
groups and fans out -- one request per group, each applied with
``scoped_clear`` at its own frame offset, and the gap is promised untouched.
Both are covered, because the difference is invisible from the UI and the
second one exists only to protect the gap.

Reset topology (the decided design, cleanup-before-re-reset): the Max
original resets twice -- before the adjacent case, and mid-gate before the
fan-out case, wiping the adjacent case's rig. Here the mid-gate reset is
preceded by the gate deleting the rig it built
(``builder.delete_skeleton_from_root``, contract MUST), so
``scene_api.new_scene()`` finds an honestly empty scene and keeps its full
Q1(b) refusal. No bypass, no verb change.

Key-count translation (the flagged risk): the Max original sampled ONE
rotation axis per joint (``X_Rotation``'s ``numKeys``/``getKeyTime``) as a
convenience representative for "this joint is keyed here". The contract verb
``scene_api.key_times(node, "rotation")`` answers the UNION of key times in
SECONDS across the axes. For BOTH assertions the union serves the stated
meaning at least as faithfully as the single axis did:

* "no joint carries a key inside the gap" -- the union is a superset of any
  one axis, so a gap key on ANY axis now reddens the gate; the X-only read
  would have missed a Y- or Z-only leak. Stricter in exactly the direction
  the label promises, never looser.
* "both spans were keyed" -- ``apply_animation`` keys all three axes
  together, so union-vs-single-axis reads the same fact; the teeth test
  keys the fake's axes asymmetrically to pin that the union (not one lucky
  axis) is what is asserted, in both directions.

Frames-vs-seconds: keys are written at seconds from ``key_fps`` (the scene
rate, contract 3.4), so a union time maps back to a frame as ``t * fps`` --
the gap bounds 41..59 translate unchanged.

What the host injects (:class:`HostMultiPrompt`): the parent-relative offset
read, meters at the seam -- the same single injection, with the same
bind-pose comparison, as the m9 and m14 gates.

Nothing of this gate stays host-side: the Max original's only pymxs was the
scene vocabulary now spoken by ``scene_api`` and the bridge.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass

DRIFT_M = 0.005


@dataclass
class HostMultiPrompt:
    """What one host tells this gate about reading its rig."""

    #: ``(node, parent_node) -> (x, y, z)`` -- parent-relative offset in
    #: METERS at the current playhead, same convention as the host bind-pose
    #: block's ``rest_translation`` (see the m14 gate).
    local_offset_m: object


class Box:
    """The bit of a PromptBox that grouping actually reads."""

    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text
        self.id = f"{text[:8]}_{start}"


def run(host: HostMultiPrompt, check):
    """Run the shared m11 checks, reporting through *check*.

    Returns the joint map of the LAST rig built (the fan-out one) for the
    wrapper to clean up.
    """
    from animatica_core import constants
    from animatica_core.core import retarget
    from animatica_core.core.request_builder import (PROTOCOL_VERSION,
                                                     group_contiguous_boxes)
    from animatica_core.bridge import animator, builder, skeleton as skel
    from animatica_core.gates import scene_api
    from animatica_core.gltf_parser import parse_gltf
    from animatica_core.live.skeleton_adapter import skeleton_block_to_hierarchy
    from animatica_core.skeleton import (get_joint_hierarchy,
                                         get_neutral_positions)

    server = os.environ.get("ANIMATICA_SERVER", "http://127.0.0.1:8000")

    print("\n-- which shape does a pair of boxes take? --", flush=True)
    adjacent = [Box(0, 60, "a person walks forward"),
                Box(60, 100, "a person jumps high")]
    gapped = [Box(0, 40, "a person walks forward"),
              Box(60, 100, "a person jumps high")]
    check("back-to-back boxes are ONE group (one request, two segments)",
          len(group_contiguous_boxes(adjacent)) == 1,
          f"{len(group_contiguous_boxes(adjacent))} groups")
    check("a real gap splits into TWO groups (fan-out)",
          len(group_contiguous_boxes(gapped)) == 2,
          f"{len(group_contiguous_boxes(gapped))} groups")

    scene_api.new_scene()
    hierarchy = get_joint_hierarchy(constants.DEFAULT_SKELETON_NAME)
    rest = get_neutral_positions(constants.DEFAULT_SKELETON_NAME,
                                 hip_height=constants.DEFAULT_HIP_HEIGHT)
    joint_map = builder.build_neutral_skeleton(
        constants.DEFAULT_PREFIX, hierarchy=hierarchy, rest_positions=rest)
    block = skel.load_bind_pose_property(joint_map[hierarchy[0][0]])
    want = {str(j["name"]).split(":")[-1]: tuple(float(c) for c in
                                                 j["rest_translation"])
            for j in block["joints"] if j.get("parent")}

    caps = json.load(urllib.request.urlopen(f"{server}/capabilities",
                                            timeout=90))
    model = next(m for m in caps["models"] if m["id"].startswith("kimodo"))
    canonical, fps = model["canonical_skeleton"], float(model["fps"])
    src_hier, src_rest, _ = skeleton_block_to_hierarchy(canonical)

    def generate(segments):
        body = {"protocol_version": PROTOCOL_VERSION, "model": model["id"],
                "skeleton": canonical, "segments": segments,
                "options": {"seed": 42}, "constraints": [],
                "timing": {"fps": fps}}
        req = urllib.request.Request(
            f"{server}/generate", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            payload = json.loads(r.read())
        return retarget.retarget_motion(parse_gltf(payload), src_hier,
                                        src_rest, hierarchy, rest)

    def drift():
        worst, where = 0.0, ""
        scene_api.seek_and_evaluate(0)
        for name, parent in hierarchy:
            if not parent or name not in want:
                continue
            got = host.local_offset_m(joint_map[name], joint_map[parent])
            e = max(abs(float(got[i]) - want[name][i]) for i in range(3))
            if e > worst:
                worst, where = e, name
        return worst, where

    print("\n-- one request, two segments (the adjacent case) --", flush=True)
    out = generate([{"type": "text", "prompt": "a person walks forward",
                     "duration_frames": 60},
                    {"type": "text", "prompt": "a person jumps high",
                     "duration_frames": 40}])
    n = len(out["local_rot_mats"])
    check(f"the server returned both segments as one clip ({n} frames)",
          n >= 95, f"{n} frames for 60+40 asked")
    animator.apply_animation(joint_map, out, None, fps,
                             constants.DEFAULT_PREFIX,
                             key_fps=float(scene_api.current_fps()))
    scene_api.set_take_range(0, n - 1)
    d, where = drift()
    check(f"the rig survives it ({d * 1000:.3f} mm)", d < DRIFT_M,
          f"{d * 1000:.1f} mm at {where}")

    # The two halves must actually differ -- one clip of "walk" pasted twice
    # would pass every geometric check and be wrong.
    root_name = hierarchy[0][0]

    def hips_height(frame):
        return scene_api.world_position_m(joint_map, root_name, frame)[1]

    walk_y = max(hips_height(f) for f in range(5, 55, 5))
    jump_y = max(hips_height(f) for f in range(62, n - 2, 5))
    check(f"the second segment is not the first again "
          f"(hips peak {walk_y:.2f} m walking vs {jump_y:.2f} m jumping)",
          abs(jump_y - walk_y) > 0.02,
          f"{walk_y:.3f} vs {jump_y:.3f} m")

    print("\n-- two groups with a gap (the fan-out case) --", flush=True)
    # The decided design: delete the adjacent case's rig BEFORE asking for a
    # fresh scene, so the verb's empty-scene discriminator passes honestly.
    builder.delete_skeleton_from_root(joint_map[hierarchy[0][0]])
    scene_api.new_scene()
    joint_map = builder.build_neutral_skeleton(
        constants.DEFAULT_PREFIX, hierarchy=hierarchy, rest_positions=rest)
    scene_api.set_take_range(0, 100)
    a = generate([{"type": "text", "prompt": "a person walks forward",
                   "duration_frames": 40}])
    b = generate([{"type": "text", "prompt": "a person jumps high",
                   "duration_frames": 40}])
    for out_i, offset in ((a, 0), (b, 60)):
        animator.apply_animation(joint_map, out_i, None, fps,
                                 constants.DEFAULT_PREFIX,
                                 frame_offset=offset, scoped_clear=True,
                                 set_take_range=False,
                                 key_fps=float(scene_api.current_fps()))
    d, where = drift()
    check(f"the rig survives the fan-out ({d * 1000:.3f} mm)", d < DRIFT_M,
          f"{d * 1000:.1f} mm at {where}")

    # Every joint, not a sampled one: an arbitrary probe can land on LeftEye,
    # which the retarget does not carry, and then "no keys in the gap" is
    # true for a reason that has nothing to do with the gap. Union key times
    # in seconds, mapped back to frames by the fps the keys were written at.
    key_fps = float(scene_api.current_fps())
    offenders, keyed_both = [], 0
    for name, _p in hierarchy:
        frames = [t * key_fps
                  for t in scene_api.key_times(joint_map[name], "rotation")]
        if not frames:
            continue
        if [f for f in frames if 41.0 < f < 59.0]:
            offenders.append(name)
        if any(f <= 40.5 for f in frames) and any(f >= 59.5 for f in frames):
            keyed_both += 1
    check("no joint carries a key inside the gap (frames 41-59)",
          not offenders, f"{len(offenders)}: {offenders[:5]}")
    check(f"both spans were keyed, on {keyed_both} joints",
          keyed_both >= 20, f"only {keyed_both}")

    return joint_map
