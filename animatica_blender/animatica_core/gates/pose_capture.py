"""Capturing a Full Body pose must not move the rig (gate m14).

Ported from the 3ds Max plugin's ``accept_m14_pose_capture.py`` (G3 of
PLAN-suita-wieloDCC.md). The story it pins: "Auto apply as constraint" keys
the whole pose through ``key_model_local_transform`` -- once per joint, all
77 of them. That function was ported from MoBu, where ``FBModel.Translation``
is LOCAL; its Max counterpart, ``node.transform.pos``, is WORLD. The two read
the same in the source and mean different things, so every capture wrote each
joint's distance from the origin into the controller that holds its offset
from its PARENT, and the error compounded ~1.42x per level down each chain --
Spine1 at 1.05 m, a fingertip at 55 m, a rig visibly torn apart after a few
pose-and-constrain rounds.

Bone length is useless as a check here (it survives a wrong offset
direction), and so is a single capture (the first one is only mildly wrong).
What catches it is the parent-relative offset measured against the bind pose,
over repeated captures -- the same measure and the same shape of test as m9.

Reset topology (the m17 lesson, traced): the Max original resets EXACTLY
once, at gate start, before it creates anything -- so the reset maps cleanly
to one ``scene_api.new_scene()`` and no mid-gate reset exists to park over.
``rt.resetMaxFile`` + ``invalidate_units`` became ``new_scene()`` (the Max
half performs both); the ``units_per_meter`` divisions moved INTO the host's
``local_offset_m`` injection, which speaks meters at the seam.

What the host injects (:class:`HostPoseCapture`): the one read the contract
does not cover -- a joint's parent-relative offset at the current playhead
(Max: ``child.transform * rt.inverse(parent.transform)`` over ``upm``; MoBu:
the local translation over ``M_TO_CM``) -- and the keyed-rotation read for
the finiteness probe (Max: the Euler controller's sub-values; MoBu: the
evaluated local rotation). Same scene facts, host vocabulary.

What stays in the MAX wrapper, deliberately: the two closing source pins on
``max_bridge/animator.py`` ("derives the offset from the parent, not from
world", "world position is taken only where there is no parent"). They pin
the pymxs world-vs-local hazard in Max's own bridge source; MoBu's
``key_model_local_transform`` is the ORIGINAL whose ``Translation`` is local,
so the hazard those lines guard does not exist there to scan for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

ROUNDS = 5
DRIFT_M = 0.001          # 1 mm; a correct capture moves nothing at all


@dataclass
class HostPoseCapture:
    """What one host tells this gate about reading its rig."""

    #: ``(node, parent_node) -> (x, y, z)`` -- the node's parent-relative
    #: offset in METERS at the current playhead, in the same axis convention
    #: as the ``rest_translation`` values of the host's bind-pose block (the
    #: two are compared directly, so they must agree with each other, not
    #: with any global convention).
    local_offset_m: object
    #: ``node -> (x, y, z)`` -- the node's keyed local rotation at the
    #: current playhead, three floats. Only their FINITENESS is asserted.
    keyed_rotation: object


def run(host: HostPoseCapture, check):
    """Run the shared m14 checks, reporting through *check*.

    Builds a rig and leaves it (keyed) in the scene, returning its joint
    map -- cleanup is the host wrapper's business, exactly as in the m5 gate.
    """
    from animatica_core import constants
    from animatica_core.bridge import animator, builder, skeleton as skel
    from animatica_core.gates import scene_api
    from animatica_core.skeleton import (get_joint_hierarchy,
                                         get_neutral_positions)

    scene_api.new_scene()
    hierarchy = get_joint_hierarchy(constants.DEFAULT_SKELETON_NAME)
    rest = get_neutral_positions(constants.DEFAULT_SKELETON_NAME,
                                 hip_height=constants.DEFAULT_HIP_HEIGHT)
    joint_map = builder.build_neutral_skeleton(
        constants.DEFAULT_PREFIX, hierarchy=hierarchy, rest_positions=rest)
    root = joint_map[hierarchy[0][0]]
    scene_api.set_take_range(0, 100)

    block = skel.load_bind_pose_property(root)
    want = {str(j["name"]).split(":")[-1]: tuple(float(c) for c in
                                                 j["rest_translation"])
            for j in (block or {}).get("joints", []) if j.get("parent")}
    check(f"the builder left a bind pose to measure against ({len(want)})",
          len(want) == len(hierarchy) - 1, len(want))

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
        worst, where = 0.0, ""
        for f in (0, 10, 25):
            for name, _p in hierarchy:
                d = math.dist(scene_api.world_position_m(joint_map, name, f),
                              (0.0, 0.0, 0.0))
                if d > worst:
                    worst, where = d, f"frame {f}, {name}"
        return worst, where

    d0, _ = drift()
    check(f"a fresh rig sits on its bind pose ({d0 * 1000:.3f} mm)", d0 < 1e-6)

    print(f"\n-- {ROUNDS} Full Body captures, the way the checkbox does it --",
          flush=True)
    # key_model_local_transform keys every joint in the map at the frame being
    # captured. Different frames each round, because that is what an operator
    # does.
    fps = float(scene_api.current_fps())
    prev = d0
    for i in range(ROUNDS):
        frame = 10 + i * 5
        scene_api.goto_frame(frame)
        for node in joint_map.values():
            animator.key_model_local_transform(node, int(frame), fps)
        d, where = drift()
        far, far_where = farthest()
        print(f"     round {i + 1} @ frame {frame:3d}: drift {d * 1000:8.3f} mm "
              f"({where or '-'}), farthest joint {far:5.2f} m", flush=True)
        check(f"round {i + 1}: the rig did not move ({d * 1000:.3f} mm)",
              d < DRIFT_M, f"{d * 1000:.1f} mm at {where}")
        check(f"round {i + 1}: nothing flew away ({far:.2f} m)",
              far < 3.0, f"{far:.1f} m at {far_where}")
        # The failure mode is compounding, so growth matters as much as size.
        check(f"round {i + 1}: no compounding vs the round before",
              d <= max(prev * 1.05, 1e-6),
              f"{d * 1000:.3f} mm after {prev * 1000:.3f} mm")
        prev = d

    print("\n-- and the pose it was asked to preserve is still there --",
          flush=True)
    # Rotations are the point of the capture; they must survive unchanged.
    probe = [c for c, p in hierarchy if p][12]
    scene_api.seek_and_evaluate(10)
    rot = tuple(float(v) for v in host.keyed_rotation(joint_map[probe]))
    check(f"{probe} carries keyed rotation at the captured frame",
          all(math.isfinite(v) for v in rot), rot)

    return joint_map
