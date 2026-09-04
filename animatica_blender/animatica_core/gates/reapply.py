"""Applying motion twice must not deform the rig (gate m8, shared half).

Ported from the 3ds Max plugin's ``accept_m8_reapply.py`` (final gate
tranche of PLAN-suita-wieloDCC.md). The story it pins: ``apply_animation``
keys a CONSTANT parent-relative offset on every non-root joint, and it used
to read that offset as a world-space delta -- correct on a rig at rest,
wrong the moment the rig holds a pose. So the first generate was right and
every one after it keyed a POSED offset as the bone's rest: 0.9 mm on the
first apply, 698 mm on the second, 1123 mm on the third. Bone LENGTH is a
useless check (it survives a wrong offset direction); what catches it is
scene world positions against the retargeted data the apply was given.

Reset topology (the decided design, cleanup-before-re-reset): the Max
original resets twice -- at start, and mid-gate before the ``apply_to_target``
section, wiping its own first rig to test the GUI seam on a fresh one. Here
the mid-gate reset is preceded by the gate explicitly deleting the rig it
built (``builder.delete_skeleton_from_root`` -- a contract MUST verb), so
``scene_api.new_scene()`` finds an honestly empty scene and keeps its full
Q1(b) refusal. No bypass, no exception in the verb.

Key-count translation (the flagged risk): the Max original walks the three
position sub-controllers of one probe joint and takes the LATEST key time in
ticks. The contract verb is ``scene_api.key_times(node, "translation")`` --
the sorted UNION of key times in SECONDS across the axis curves. For a
"latest key" assertion the union is exactly as sharp as the per-axis walk:
``max(union)`` >= every axis's own latest time, so a stale key surviving on
ONE axis past the new end still surfaces. The teeth test pins that with a
fake whose re-apply leaves stale keys on a single axis only. Ticks became
seconds against the fps the keys were written at -- same half-frame slack.

What the host injects (:class:`HostReapply`): ``corrupt_rig`` -- write a
wrong parent-relative offset onto each given node's keyed translation at
frame 0, the way the old buggy apply did (Max: each position sub-value
``* 3 + 5`` under animate; MoBu: the Translation FCurve keys at t=0). The
heal section needs a rig that is ALREADY broken, and breaking a keyed curve
in place is a host-vocabulary write.

Two form changes, meaning-preserving and test-pinned: ``key_fps=
float(rt.frameRate)`` became ``key_fps=scene_api.current_fps()`` (contract
3.4 -- the same number); ``rt.animationRange`` / ``rt.sliderTime`` became
``scene_api.set_take_range`` / ``goto_frame``.

One clock, everywhere (the fourth live MoBu sweep's lesson): every apply in
this gate keys at ``key_fps=scene_api.current_fps()`` -- the SCENE's own
rate -- because the measurement loop samples through
``scene_api.world_position_m(joint_map, name, f)``, whose host half seeks
INTEGER SCENE FRAMES. Keys written at the model's fps (30) on a scene
transported at 24 put frame 65's key at 2.167 s while the seek lands at
2.708 s -- 0.54 s of walk, measured live as 1060 mm of "error" on a correct
apply. With ``key_fps`` at the scene rate, model frame N lands on scene
frame N and write clock == read clock on every host; on a batch host whose
rate already matches the model (Max at 30) the key times are bit-identical
to before. The stale-key frame conversion below uses the same clock, since
that is the rate the keys were actually written at.

What stays in the MAX wrapper, deliberately: the four closing source pins on
``max_bridge/animator.py`` ("derives the offset from the local transform",
"no longer differences world positions", "prefers the stamped bind pose",
"the clear step covers non-root position controllers") -- they scan Max's
own bridge source for Max's own historical bug.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass

TOLERANCE_M = 0.005          # 5 mm; a healthy apply lands under 1 mm


@dataclass
class HostReapply:
    """What one host lets this gate do to its rig."""

    #: ``(nodes) -> None`` -- corrupt each node's keyed translation at
    #: frame 0 (multiply by 3, add 5, in the host's native unit), as the
    #: old buggy apply would have. The heal check needs a broken rig.
    corrupt_rig: object

    #: ``() -> str`` -- the CURRENT take's name, for the apply_to_target
    #: leg. Max treats an empty name as "the current take"; MoBu REFUSES it
    #: ("existing_take requires a take name") -- measured on the first live
    #: run, where the fake's tolerance had hidden the asymmetry. The
    #: difference is the host's, so the name comes from the host.
    current_take_name: object = staticmethod(lambda: "")


def run(host: HostReapply, check):
    """Run the shared m8 checks, reporting through *check*.

    Returns the joint map of the LAST rig built (the ``apply_to_target``
    one) for the wrapper to clean up.
    """
    import numpy as np

    from animatica_core import constants
    from animatica_core.core import retarget
    from animatica_core.core.request_builder import PROTOCOL_VERSION
    from animatica_core.bridge import (animation_target, animator, builder,
                                       skeleton as skel)
    from animatica_core.gates import scene_api
    from animatica_core.gltf_parser import parse_gltf
    from animatica_core.live.skeleton_adapter import skeleton_block_to_hierarchy
    from animatica_core.skeleton import (get_joint_hierarchy,
                                         get_neutral_positions)

    server = os.environ.get("ANIMATICA_SERVER", "http://127.0.0.1:8000")

    scene_api.new_scene()
    hierarchy = get_joint_hierarchy(constants.DEFAULT_SKELETON_NAME)
    rest = get_neutral_positions(constants.DEFAULT_SKELETON_NAME,
                                 hip_height=constants.DEFAULT_HIP_HEIGHT)
    joint_map = builder.build_neutral_skeleton(
        constants.DEFAULT_PREFIX, hierarchy=hierarchy, rest_positions=rest)

    caps = json.load(urllib.request.urlopen(f"{server}/capabilities",
                                            timeout=60))
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
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = json.loads(r.read())
        return retarget.retarget_motion(parse_gltf(payload), src_hier,
                                        src_rest, hierarchy, rest)

    # The one clock rule (module docstring): keys go down at the SCENE's own
    # rate so the frame-seeking measurement reads them where they were written.
    key_fps = float(scene_api.current_fps())

    def apply_and_measure(out):
        animator.apply_animation(joint_map, out, hierarchy, fps,
                                 constants.DEFAULT_PREFIX, key_fps=key_fps)
        last = len(out["local_rot_mats"]) - 1
        scene_api.set_take_range(0, last)
        idx = {n: i for i, n in enumerate(out["joint_names"])}
        worst, where = 0.0, ""
        for f in range(0, last + 1, 5):
            for name, _p in hierarchy:
                got = np.array(scene_api.world_position_m(joint_map, name, f),
                               float)
                want = np.asarray(out["posed_joints"][f][idx[name]], float)
                err = float(np.max(np.abs(got - want)))
                if err > worst:
                    worst, where = err, f"frame {f}, {name}"
        return worst, where, last

    print("\n-- first apply, rig at rest (this always worked) --", flush=True)
    out1 = generate(101, 42)
    w1, where1, last1 = apply_and_measure(out1)
    check(f"scene matches the data ({w1 * 1000:.2f} mm)",
          w1 < TOLERANCE_M, f"{w1 * 1000:.1f} mm at {where1}")

    print("\n-- second apply, rig left POSED (this is the regression) --",
          flush=True)
    # Exactly what the GUI does: the playhead sits wherever the user left it,
    # so the rig holds a pose when the next generate is applied.
    scene_api.goto_frame(last1 // 2)
    out2 = generate(101, 7)
    w2, where2, _ = apply_and_measure(out2)
    check(f"a posed rig does not corrupt the bone offsets ({w2 * 1000:.2f} mm)",
          w2 < TOLERANCE_M, f"{w2 * 1000:.1f} mm at {where2}")

    print("\n-- third apply, shorter take over a longer one --", flush=True)
    scene_api.goto_frame(100)
    out3 = generate(60, 11)
    w3, where3, _ = apply_and_measure(out3)
    check(f"a shorter re-apply stays exact ({w3 * 1000:.2f} mm)",
          w3 < TOLERANCE_M, f"{w3 * 1000:.1f} mm at {where3}")

    print("\n-- nothing of the longer take may survive past the new end --",
          flush=True)
    # The clear step used to touch rotation on every joint but position only
    # on the ROOT, so a shorter take left the longer one's position keys past
    # the new end and the limb stretched toward the stale value. The union
    # key-time read keeps every axis in sight: one surviving axis is enough
    # to move max() past the boundary.
    probe = [c for c, pn in hierarchy if pn and c in joint_map][10]
    times = scene_api.key_times(joint_map[probe], "translation")
    latest = max(times) if times else 0.0
    last_new = len(out3["local_rot_mats"]) - 1
    check(f"no position key on {probe} past frame {last_new}",
          latest <= (last_new + 0.5) / key_fps,
          f"latest key at {latest:.3f} s = frame {latest * key_fps:.1f}")
    # The grid is pinned on the RE-APPLY path too. Measured on 3ds Max
    # (2026-09-02): a fresh take keyed exactly on i/fps while a second apply
    # onto the same rig keyed every frame 1/4800 s early -- the conformance
    # clip walks a fresh take only, so it stayed green through the whole
    # regression. This is the one shared gate that re-applies, and the union
    # read above is the same seconds the transport will seek to.
    off = max((abs(float(t) - round(float(t) * key_fps) / key_fps)
               for t in times), default=0.0)
    check(f"re-applied keys land on the seconds grid i/fps "
          f"({off * 1000:.4f} ms off)", off <= 1e-6, off)

    # And measure it over the OLD range -- the part every earlier pass
    # skipped, because the data only covers the new one.
    pairs = [(c, pn) for c, pn in hierarchy
             if pn and pn in joint_map and c in joint_map]
    base = {c: math.dist(scene_api.world_position_m(joint_map, c, 0),
                         scene_api.world_position_m(joint_map, pn, 0))
            for c, pn in pairs}
    worst_tail, tail_where = 0.0, ""
    for f in range(last_new, last1 + 1):
        for c, pn in pairs:
            length = math.dist(scene_api.world_position_m(joint_map, c, f),
                               scene_api.world_position_m(joint_map, pn, f))
            if base[c] > 1e-6:
                dev = abs(length - base[c]) / base[c]
                if dev > worst_tail:
                    worst_tail, tail_where = dev, f"frame {f}, {c}"
    check(f"bones keep their length past the new end "
          f"({worst_tail * 100:.2f}%)",
          worst_tail < 0.01, f"{worst_tail * 100:.1f}% at {tail_where}")

    print("\n-- the builder must stamp the rest pose itself --", flush=True)
    # The heal has to have a source. A real broken scene turned up with no
    # bind block at all, so the corrupted offsets could not be recovered from
    # anything. The builder knows the rest pose by construction; it stamps it.
    stamped = skel.load_bind_pose_property(joint_map[hierarchy[0][0]])
    check("build_neutral_skeleton leaves a bind-pose block on the root",
          bool(stamped) and len(stamped.get("joints", [])) == len(hierarchy),
          f"{len((stamped or {}).get('joints', []))} joints")

    print("\n-- a rig already corrupted by the old code must heal --",
          flush=True)
    # The point of reading the bind pose stamped at build time rather than
    # the live transform: a rig whose offsets a previous buggy apply already
    # wrote wrong would otherwise have those wrong values read back and
    # re-keyed forever.
    victims = [c for c, pn in hierarchy if pn and c in joint_map][:12]
    host.corrupt_rig([joint_map[name] for name in victims])
    broken = math.dist(
        scene_api.world_position_m(joint_map, victims[0], 0),
        scene_api.world_position_m(joint_map, dict(hierarchy)[victims[0]], 0))
    check("the corruption actually took hold",
          abs(broken - base[victims[0]]) / base[victims[0]] > 0.5,
          f"{broken:.4f} m vs rest {base[victims[0]]:.4f} m")

    out4 = generate(101, 5)
    w4, where4, _ = apply_and_measure(out4)
    check(f"the next apply heals it ({w4 * 1000:.2f} mm)",
          w4 < TOLERANCE_M, f"{w4 * 1000:.1f} mm at {where4}")

    print("\n-- the GUI's own seam: apply_to_target --", flush=True)
    # Its root-offset capture used the FULL world position of the motion
    # root, hip height included, so the rig came out one hip-height too high.
    # A fresh rig, on a fresh scene -- and the decided design: this gate
    # DELETES the rig it built before asking for that fresh scene, so the
    # verb's empty-scene discriminator passes on its own terms.
    builder.delete_skeleton_from_root(joint_map[hierarchy[0][0]])
    scene_api.new_scene()
    jm2 = builder.build_neutral_skeleton(constants.DEFAULT_PREFIX,
                                         hierarchy=hierarchy,
                                         rest_positions=rest)
    root2 = jm2[hierarchy[0][0]]
    skel.store_bind_pose_property(
        root2, skel.fbmodel_skeleton_to_skeleton(root2, skip_root=False))
    out5 = generate(60, 3)
    animation_target.apply_to_target(
        jm2, out5, mode="existing_take",
        take_name=host.current_take_name(), story_path=None,
        skeleton_root=root2, label="gate", fps=fps,
        prefix=constants.DEFAULT_PREFIX,
        root_scale=1.0, frame_offset=0, use_hip_pos=True,
        preserve_height=True, root_offset_cm=None, root_yaw=None,
        key_fps=float(scene_api.current_fps()), scoped_clear=False,
        base_layer_only=True, ground_offset_m=0.0, xz_reseat=True)
    idx5 = {n: i for i, n in enumerate(out5["joint_names"])}
    got_y = scene_api.world_position_m(jm2, hierarchy[0][0], 0)[1]
    want_y = float(out5["posed_joints"][0][idx5[hierarchy[0][0]]][1])
    check(f"the capture does not add the hip height to Y "
          f"({(got_y - want_y) * 1000:+.1f} mm)",
          abs(got_y - want_y) < 0.05,
          f"root Y {got_y:.3f} m vs data {want_y:.3f} m")

    return jm2
