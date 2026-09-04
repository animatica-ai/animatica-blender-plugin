"""Generating over and over must not drift the rig (gate m9).

Ported from the 3ds Max plugin's ``accept_m9_repeat_generate.py`` (G3 of
PLAN-suita-wieloDCC.md). The reported break was not one bad apply: on the
real broken scene the local offsets grew about 1.42x per level down every
chain -- Spine1 1.05 m, RightHand 8.73 m, fingertip 55.42 m, joints 94 m from
the origin. An offset re-read off an already-wrong rig and re-keyed, once per
generate, compounding until it became visible.

So the thing to test is not "does apply work" -- every earlier gate said yes
while the user's rig was quietly doubling. It is: after N applies through the
GUI's own seam (``animation_target.apply_to_target``), with the playhead left
wherever the last one put it, is the rig still the rig it was built as? The
measure is each joint's parent-relative offset against the bind pose the
builder stamped -- the only description of the rest rig that the scene's
animation cannot reach.

Reset topology (the m17 lesson, traced): the Max original resets EXACTLY
once, at gate start, before it creates anything. Clean ``new_scene()`` port;
no mid-gate reset to park over. NEEDS the MMCP server: the launcher probes
once up front and turns a dead server into a worded SKIP.

What the host injects (:class:`HostRepeatGenerate`): the parent-relative
offset read -- the same injection, with the same meters-at-the-seam contract,
as the m14 gate's. Everything else the gate touches is already contract
vocabulary (``apply_to_target``, the builder, the bind-pose block, the
playhead and take-range verbs, ``sample_effector_position`` as the world
read behind ``scene_api.world_position_m``).

Two form changes against the Max original, both meaning-preserving and both
test-pinned: ``key_fps=float(rt.frameRate)`` became
``key_fps=scene_api.current_fps()`` (contract 3.4 -- the same number, real
frames-per-second); and a missing bind-pose block, which there printed FAIL
and ``sys.exit(1)``, here records its FAIL through *check* and returns early
(the m5/m16 rule: a gate that crashes -- or exits -- tells you less than one
that fails with words; the wrapper's ledger still makes the verdict FAIL).
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass

DRIFT_M = 0.005          # 5 mm on any joint's rest offset


@dataclass
class HostRepeatGenerate:
    """What one host tells this gate about reading its rig."""

    #: ``(node, parent_node) -> (x, y, z)`` -- parent-relative offset in
    #: METERS at the current playhead, in the same axis convention as the
    #: host bind-pose block's ``rest_translation`` (see the m14 gate).
    local_offset_m: object

    #: ``() -> str`` -- the CURRENT take's name for ``apply_to_target``.
    #: Same asymmetry the m8 gate hit first: Max treats an empty name as
    #: "the current take", MoBu refuses it outright.
    current_take_name: object = staticmethod(lambda: "")


def run(host: HostRepeatGenerate, check):
    """Run the shared m9 checks, reporting through *check*.

    Returns the joint map for the wrapper to clean up, or ``None`` when the
    bind-pose block was missing and the gate bailed with its FAIL recorded.
    """
    import numpy as np

    from animatica_core import constants
    from animatica_core.core import retarget
    from animatica_core.core.request_builder import PROTOCOL_VERSION
    from animatica_core.bridge import animation_target, builder, skeleton as skel
    from animatica_core.gates import scene_api
    from animatica_core.gltf_parser import parse_gltf
    from animatica_core.live.skeleton_adapter import skeleton_block_to_hierarchy
    from animatica_core.skeleton import (get_joint_hierarchy,
                                         get_neutral_positions)

    server = os.environ.get("ANIMATICA_SERVER", "http://127.0.0.1:8000")
    rounds = int(os.environ.get("ANIMATICA_ROUNDS", "6"))

    # ---- what the Create Skeleton button does -----------------------------
    scene_api.new_scene()
    hierarchy = get_joint_hierarchy(constants.DEFAULT_SKELETON_NAME)
    rest = get_neutral_positions(constants.DEFAULT_SKELETON_NAME,
                                 hip_height=constants.DEFAULT_HIP_HEIGHT)
    joint_map = builder.build_neutral_skeleton(
        constants.DEFAULT_PREFIX, hierarchy=hierarchy, rest_positions=rest)
    root = joint_map[hierarchy[0][0]]
    skel.mark_canonical(root)

    print(f"\n-- the rig as built ({len(joint_map)} joints) --", flush=True)
    block = skel.load_bind_pose_property(root)
    check("the builder stamped a bind pose",
          bool(block) and len(block.get("joints", [])) == len(hierarchy),
          f"{len((block or {}).get('joints', []))} joints")
    if not block:
        # The Max original exits 1 here; the FAIL is already on the ledger,
        # and every measurement past this point would only crash on it.
        return None

    want = {}
    for j in block["joints"]:
        t = j.get("rest_translation")
        if j.get("parent") and t and len(t) == 3:
            want[str(j["name"]).split(":")[-1]] = tuple(float(c) for c in t)

    def drift():
        """Worst parent-relative offset error against the bind pose, in metres."""
        worst, where = 0.0, ""
        scene_api.seek_and_evaluate(0)
        for name, parent in hierarchy:
            if not parent or name not in want:
                continue
            got = host.local_offset_m(joint_map[name], joint_map[parent])
            err = max(abs(float(got[i]) - want[name][i]) for i in range(3))
            if err > worst:
                worst, where = err, name
        return worst, where

    def farthest():
        """Worst distance of any joint from the origin, over the whole range."""
        last = int(scene_api.take_range()[1])
        worst, where = 0.0, ""
        for f in range(0, last + 1, 5):
            for name, _p in hierarchy:
                d = math.dist(scene_api.world_position_m(joint_map, name, f),
                              (0.0, 0.0, 0.0))
                if d > worst:
                    worst, where = d, f"frame {f}, {name}"
        return worst, where

    d0, _ = drift()
    check(f"a fresh rig matches its own bind pose ({d0 * 1000:.3f} mm)",
          d0 < 1e-6, f"{d0 * 1000:.3f} mm")

    # ---- two motions, applied alternately ---------------------------------
    caps = json.load(urllib.request.urlopen(f"{server}/capabilities",
                                            timeout=90))
    model = next(m for m in caps["models"] if m["id"].startswith("kimodo"))
    canonical, fps = model["canonical_skeleton"], float(model["fps"])
    src_hier, src_rest, _ = skeleton_block_to_hierarchy(canonical)

    def generate(frames, seed):
        body = {
            "protocol_version": PROTOCOL_VERSION, "model": model["id"],
            "skeleton": canonical,
            "segments": [{"type": "text",
                          "prompt": "a person walks forward confidently",
                          "duration_frames": frames}],
            "options": {"seed": seed},
            "constraints": [
                {"type": "root_path", "frames": [0],
                 "positions_xz": [[0.0, 0.0]]},
                {"type": "root_path", "frames": [frames - 1],
                 "positions_xz": [[0.0, 3.0]]}],
            "timing": {"fps": fps},
        }
        req = urllib.request.Request(
            f"{server}/generate", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            payload = json.loads(r.read())
        return retarget.retarget_motion(parse_gltf(payload), src_hier,
                                        src_rest, hierarchy, rest)

    print(f"\n-- {rounds} generates in a row, through apply_to_target --",
          flush=True)
    # Two takes of different lengths, alternated: the long-then-short order is
    # what leaves stale keys past the new end, and the alternation is what makes
    # a compounding error visible instead of a one-off.
    motions = [generate(101, 42), generate(60, 7)]

    prev_drift = d0
    for i in range(rounds):
        out = motions[i % 2]
        # The playhead is left wherever the previous round put it -- the GUI
        # never returns it to rest, and that is what made the offset capture
        # read a posed rig.
        scene_api.goto_frame(0 if i == 0 else
                             int(scene_api.take_range()[1]) // 2)
        animation_target.apply_to_target(
            joint_map, out, mode="existing_take",
            take_name=host.current_take_name(),
            story_path=None, skeleton_root=root, label=f"round{i}", fps=fps,
            prefix=constants.DEFAULT_PREFIX,
            root_scale=1.0, frame_offset=0, use_hip_pos=True,
            preserve_height=True, root_offset_cm=None, root_yaw=None,
            key_fps=float(scene_api.current_fps()), scoped_clear=False,
            base_layer_only=True, ground_offset_m=0.0, xz_reseat=True)
        scene_api.set_take_range(0, len(out["local_rot_mats"]) - 1)
        d, where = drift()
        far, far_where = farthest()
        print(f"     round {i + 1}: {len(out['local_rot_mats']):3d} frames, "
              f"offset drift {d * 1000:7.3f} mm ({where or '-'}), "
              f"farthest joint {far:5.2f} m", flush=True)
        check(f"round {i + 1}: the rest offsets did not drift "
              f"({d * 1000:.3f} mm)", d < DRIFT_M, f"{d * 1000:.1f} mm at {where}")
        check(f"round {i + 1}: nothing flew away ({far:.2f} m)",
              far < 6.0, f"{far:.1f} m at {far_where}")
        # The failure mode was compounding, so growth matters as much as size.
        check(f"round {i + 1}: no compounding vs the round before",
              d <= max(prev_drift * 1.05, 1e-4),
              f"{d * 1000:.3f} mm after {prev_drift * 1000:.3f} mm")
        prev_drift = d

    print("\n-- and the motion still lands where the data says --", flush=True)
    out = motions[(rounds - 1) % 2]
    idx = {n: i for i, n in enumerate(out["joint_names"])}
    last = len(out["local_rot_mats"]) - 1
    root_name = hierarchy[0][0]
    worst, where = 0.0, ""
    for f in range(0, last + 1, 5):
        root_delta = (
            np.array(scene_api.world_position_m(joint_map, root_name, f), float)
            - np.asarray(out["posed_joints"][f][idx[root_name]], float))
        for name, _p in hierarchy:
            got = np.array(scene_api.world_position_m(joint_map, name, f),
                           float)
            w = np.asarray(out["posed_joints"][f][idx[name]], float)
            # The armed gen0 XZ snap re-seats the whole trajectory on
            # purpose, so compare shape: subtract the root's own offset.
            e = float(np.max(np.abs((got - w) - root_delta)))
            if e > worst:
                worst, where = e, f"frame {f}, {name}"
    check(f"the pose matches the data ({worst * 1000:.2f} mm)",
          worst < 0.005, f"{worst * 1000:.1f} mm at {where}")

    return joint_map
