"""End-to-end demo: server -> retarget -> rig, driven entirely from script.

Ported from the 3ds Max plugin's ``scripts/demo_end_to_end.py`` (G3 of
PLAN-suita-wieloDCC.md 2). Runs the real pipeline against the running
``motionmcp-multi-cloud`` server and puts the result on a rig in the scene.
No GUI clicks -- the pipeline is the same one the buttons drive.

**The request carries a drawn path, and that matters.** The server here runs
with ``TEXT_ENCODER_MODE=dummy`` (its DEVLOG explains why: the real encoder
needs Llama-3/LLM2Vec), so a text prompt carries no semantics. Motion comes
from **constraints** -- which is what the plugin's Path pins produce. Sending
``constraints: []`` and blaming the server for a static result was the
original author's recorded mistake, not a limitation: with a path, the same
server walks 3 m on request.

What this proves, in order: the server is reachable and serves its models;
the bridge builder puts a SOMA-77 rig in the scene; ``/generate`` returns
real motion on the model's canonical skeleton; ``core.retarget`` transfers it
onto the user's rig; the bridge animator keys it, and the joints land where
the retargeter said.

Reset topology (the m17 lesson, traced): one reset, at start, before the rig
exists -- a clean ``scene_api.new_scene()``. The gate's one other Max
coupling, the viewport-framing block (``select`` + ``zoomext`` +
``clearSelection`` + ``redrawViews``), is now the ``scene_api`` selection
trio around ``frame_viewport()`` -- the verb S2 added for exactly this line.

**Playback is deliberately absent, in every host.** The Max source carries a
comment that ``rt.playAnimation`` is NOT called: in ``3dsmaxbatch`` the
playback loop never returns and the run hangs until killed. A live
MotionBuilder host HAS a playback loop and could play the applied take back
(``time_bridge.toggle_play``) -- recorded here as a real possibility the
port deliberately does not exercise, per the conservatism rule: the ported
gate proves what the Max gate proves, and playback stays an interactive-
session affordance, not a gate step.

What the host injects (:class:`HostDemo`): setting the scene frame rate to
the model's fps (Max: ``rt.frameRate = int(fps)``; MoBu: the transport
FBTimeMode) -- the one time-domain WRITE the contract has no verb for -- and
an optional one-line scene description for the log.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass

SEED = 42
PREFIX = "animatica"


@dataclass
class HostDemo:
    """What one host tells this gate about its scene."""

    #: ``(fps) -> None`` -- make the scene's frame rate *fps*.
    set_scene_fps: object
    #: ``() -> str`` for the log line about units/axes, or None to skip it.
    describe_scene: object = None


def log(*a):
    print("[demo]", *a, flush=True)


def run(host: HostDemo):
    """Run the demo. Returns ``(ok, joint_map)``; the wrapper owns cleanup."""
    import numpy as np

    from animatica_core import skeleton as registry
    from animatica_core.core import retarget
    from animatica_core.core.request_builder import PROTOCOL_VERSION
    from animatica_core.bridge import animator, builder
    from animatica_core.gates import scene_api
    from animatica_core.gltf_parser import parse_gltf
    from animatica_core.live.skeleton_adapter import skeleton_block_to_hierarchy

    server = os.environ.get("ANIMATICA_SERVER", "http://127.0.0.1:8000")
    prompt = os.environ.get("ANIMATICA_PROMPT",
                            "a person walks forward confidently")
    frames = int(os.environ.get("ANIMATICA_FRAMES", "90"))
    travel_m = float(os.environ.get("ANIMATICA_TRAVEL", "3.0"))

    # -- 1. the server ------------------------------------------------------
    caps = json.load(urllib.request.urlopen(f"{server}/capabilities",
                                            timeout=60))
    health = json.load(urllib.request.urlopen(f"{server}/health", timeout=15))
    log("server:", health["status"],
        "| retargeting:", health["retargeting"],
        "| models:", [m["id"] for m in caps["models"]])
    model = next(m for m in caps["models"] if m["id"].startswith("kimodo"))
    canonical = model["canonical_skeleton"]
    fps = float(model["fps"])
    log(f"model {model['id']}: {len(canonical['joints'])} canonical joints "
        f"@ {fps} fps")

    # -- 2. the rig ---------------------------------------------------------
    scene_api.new_scene()
    host.set_scene_fps(int(fps))
    # Trust, but verify -- and never depend on it: the fourth live MoBu sweep
    # measured a 790.97 mm "mismatch" on a correct apply because the host's
    # SetTransportFps did not change the frame<->seconds conversion the
    # measurement seeks with (the scene stayed at 24 fps while keys went down
    # at 30 fps seconds). The log line below records what the pin actually
    # achieved, and the apply below keys at the SCENE's effective rate
    # (``key_fps``) so write clock == read clock whether or not the pin took.
    scene_fps = float(scene_api.current_fps())
    log(f"scene fps now {scene_fps:g} (asked for {int(fps)})")
    if host.describe_scene is not None:
        log(f"scene: {host.describe_scene()} "
            f"(bridge boundary: {scene_api.units()})")

    hierarchy = registry.get_joint_hierarchy("soma77")
    rest = registry.get_neutral_positions("soma77", hip_height=1.0)
    joint_map = builder.build_neutral_skeleton(PREFIX, hierarchy=hierarchy,
                                               rest_positions=rest)
    log(f"built {len(joint_map)} joints as '{PREFIX}:*'")

    # -- 3. generate, with a path -------------------------------------------
    # Three waypoints: stand at the origin, walk travel_m along +Z. This is
    # the wire form of a Path pin dragged in the timeline.
    path = [(0, (0.0, 0.0)),
            (frames // 2, (0.0, travel_m / 2.0)),
            (frames - 1, (0.0, travel_m))]
    constraints = [{"type": "root_path", "frames": [f],
                    "positions_xz": [[x, z]]}
                   for f, (x, z) in path]

    body = {
        "protocol_version": PROTOCOL_VERSION,
        "model": model["id"],
        "skeleton": canonical,
        "segments": [{"type": "text", "prompt": prompt,
                      "duration_frames": frames}],
        "options": {"seed": SEED},
        "constraints": constraints,
        "timing": {"fps": fps},
    }
    log(f'generating: "{prompt}" + a {travel_m:.1f} m path '
        f'({frames} frames, seed {SEED})')
    t0 = time.time()
    req = urllib.request.Request(
        f"{server}/generate", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        payload = json.loads(r.read())
    log(f"server answered in {time.time() - t0:.1f}s")

    motion = parse_gltf(payload)
    log(f"parsed: {len(motion['local_rot_mats'])} frames x "
        f"{len(motion['joint_names'])} joints")

    src_root = np.asarray(motion["posed_joints"])[:, 0, :]
    src_travel = float(np.linalg.norm(src_root[-1][[0, 2]]
                                      - src_root[0][[0, 2]]))
    log(f"the server's own motion travels {src_travel:.2f} m")

    # -- 4. retarget --------------------------------------------------------
    src_hier, src_rest, _ = skeleton_block_to_hierarchy(canonical)
    out = retarget.retarget_motion(motion, src_hier, src_rest, hierarchy, rest)
    log(f"retargeted {len(src_hier)} -> {len(hierarchy)} joints "
        f"(proportion scale {out['retarget_scale']:.3f})")

    # -- 5. apply -----------------------------------------------------------
    t0 = time.time()
    animator.apply_animation(joint_map, out, hierarchy, fps, PREFIX,
                             key_fps=scene_fps)
    log(f"keyed onto the rig in {time.time() - t0:.2f}s")

    # -- 6. check it landed --------------------------------------------------
    idx = {n: i for i, n in enumerate(out["joint_names"])}
    root = hierarchy[0][0]
    last = len(out["local_rot_mats"]) - 1
    worst = 0.0
    for f in (0, last // 2, last):
        for name, _p in hierarchy:
            got = np.array(scene_api.world_position_m(joint_map, name, f),
                           float)
            want = out["posed_joints"][f][idx[name]]
            worst = max(worst, float(np.max(np.abs(got - want))))

    start = scene_api.world_position_m(joint_map, root, 0)
    end = scene_api.world_position_m(joint_map, root, last)
    travel = ((end[0] - start[0]) ** 2 + (end[2] - start[2]) ** 2) ** 0.5

    # feet near the floor across the clip -- the cheapest sanity check that the
    # character is walking rather than sliding through the ground
    lows = []
    for f in range(0, last + 1, 10):
        lows.append(min(
            scene_api.world_position_m(joint_map, "LeftFoot", f)[1],
            scene_api.world_position_m(joint_map, "RightFoot", f)[1]))

    log(f"scene matches the retargeted motion to {worst * 1000:.2f} mm")
    log(f"the rig travelled {travel:.2f} m over {last + 1} frames")
    log(f"lower foot stays between {min(lows):+.2f} m and {max(lows):+.2f} m")

    scene_api.set_take_range(0, last)
    try:
        scene_api.select(list(joint_map.values()))
        scene_api.frame_viewport()
        scene_api.clear_selection()
        # NOT playback: see the module docstring. The Max original cannot
        # (batch hangs); a live host could, and deliberately does not.
    except Exception:
        pass

    ok = worst < 0.005 and travel > travel_m * 0.7 and min(lows) > -0.25
    log("RESULT:", "PASS" if ok else "FAIL")
    return ok, joint_map
