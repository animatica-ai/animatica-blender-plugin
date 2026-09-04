"""A served foot pin actually moves the foot (gate m17, shared half).

Ported from the 3ds Max plugin's ``accept_m17_effector_served.py`` (final
gate tranche of PLAN-suita-wieloDCC.md). m16 proves the offline half --
markers, write-back, reconcile, wire format -- and stops at the request
dict. This is the question that matters after it: when the wire says
``effector_target``, does the foot in the scene end up there? Split from m16
on purpose, so a dead server never drags the offline checks down with it
(the wrapper declares ``NEEDS_SERVER``; the launcher probes once up front).

Measured on the ground plane. Y is entangled with the ground fold and the
foot's own anatomy, so pinning a tolerance there would test the model rather
than the plumbing. The control run is the same seed with NO constraint: if
the pinned foot is not measurably closer to the target than the unpinned
one, the constraint did nothing and a loose tolerance would have hidden it.

Reset topology (the decided design, cleanup-before-re-reset): the Max
original's single textual reset lives in ``foot_at_pin()``, which runs TWICE
-- control and pinned -- so the second call's reset lands on a scene holding
the first call's rig. Here the helper deletes the rig it built as soon as
the foot is read (``builder.delete_skeleton_from_root``, contract MUST), so
every ``scene_api.new_scene()`` -- second call included -- finds an honestly
empty scene and keeps its full Q1(b) refusal. The helper also leaves nothing
behind on the way out; the wrapper's cleanup is a belt-and-braces sweep.

No host injection at all: every read and write this gate makes is already
contract vocabulary (``builder``, ``animator.apply_animation``,
``scene_api.world_position_m`` as the world read, the reset verb). The
``rt.resetMaxFile`` + ``invalidate_units`` pair became ``new_scene()`` whose
Max half performs both.

One form change, the m5/m16 rule: where the Max original ``sys.exit``-ed
through its reporter on a missing LeftFoot, this gate records the FAIL
through *check* and returns early -- the wrapper's ledger still makes the
verdict FAIL, and a gate that exits mid-scene tells you less than one that
fails with words and still cleans up.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request

PIN_FRAME = 30
TARGET = [0.25, 0.05, 1.0]          # x, y, z in metres, y-up


class _PinMarker:
    """The shape ``_marker_to_wire`` reads: a left-foot pin at one frame."""

    def __init__(self, frame, position):
        self.type = "left-foot"
        self.frame = frame
        self.joint = "LeftFoot"
        self.value = {"position": list(position)}


def run(check):
    """Run the shared m17 checks, reporting through *check*."""
    from animatica_core import constants
    from animatica_core.core import retarget
    from animatica_core.core.request_builder import (PROTOCOL_VERSION,
                                                     _marker_to_wire)
    from animatica_core.bridge import animator, builder
    from animatica_core.gates import scene_api
    from animatica_core.gltf_parser import parse_gltf
    from animatica_core.live.skeleton_adapter import skeleton_block_to_hierarchy
    from animatica_core.skeleton import (get_joint_hierarchy,
                                         get_neutral_positions)

    server = os.environ.get("ANIMATICA_SERVER", "http://127.0.0.1:8000")

    caps = json.load(urllib.request.urlopen(f"{server}/capabilities",
                                            timeout=10))
    model = next(m for m in caps["models"] if m.get("canonical_skeleton"))
    # Per MODEL, not global: the top level has no such key, and asking there
    # returned an empty set that looked like a refusal.
    supported = set(model.get("supported_constraints") or [])
    check("the model advertises effector_target",
          "effector_target" in supported, sorted(supported))
    canonical = model["canonical_skeleton"]
    fps = float(model.get("fps") or 30.0)

    def generate(constraints):
        body = {
            "protocol_version": PROTOCOL_VERSION,
            "model": model["id"],
            "skeleton": canonical,
            "segments": [{"type": "text", "prompt": "a person walks forward",
                          "duration_frames": 60}],
            "options": {"seed": 7},
            "constraints": constraints,
            "timing": {"fps": fps},
        }
        req = urllib.request.Request(
            f"{server}/generate", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())

    hierarchy = get_joint_hierarchy(constants.DEFAULT_SKELETON_NAME)
    rest = get_neutral_positions(constants.DEFAULT_SKELETON_NAME,
                                 hip_height=constants.DEFAULT_HIP_HEIGHT)
    src_h, src_r, _ = skeleton_block_to_hierarchy(canonical)

    def foot_at_pin(payload):
        """Apply *payload* to a fresh rig, read LeftFoot at the pin frame.

        Builds, reads, and DELETES its rig before returning -- so the next
        call's ``new_scene`` (and the suite's next gate) finds the empty
        scene the launcher was promised.
        """
        out = retarget.retarget_motion(parse_gltf(payload), src_h, src_r,
                                       hierarchy, rest)
        scene_api.new_scene()
        jm = builder.build_neutral_skeleton(constants.DEFAULT_PREFIX,
                                            hierarchy=hierarchy,
                                            rest_positions=rest)
        try:
            # One clock: key at the SCENE's rate so the frame-seeking read
            # below samples the pose the pin frame was keyed with. At the
            # model's fps on a 24 fps scene the read landed 0.42 s late
            # (data frame 62.5 for a pin at 50) -- it still passed live only
            # because the served pin holds the foot planted around its frame.
            animator.apply_animation(jm, out, None, fps,
                                     constants.DEFAULT_PREFIX,
                                     key_fps=float(scene_api.current_fps()))
            if jm.get("LeftFoot") is None:
                return None
            return scene_api.world_position_m(jm, "LeftFoot", PIN_FRAME)
        finally:
            builder.delete_skeleton_from_root(jm[hierarchy[0][0]])

    print("\n-- control: the same seed, no pin --", flush=True)
    free = foot_at_pin(generate([]))
    check("the control run keys a LeftFoot", free is not None)
    if free is None:
        return
    free_d = math.dist((free[0], free[2]), (TARGET[0], TARGET[2]))
    print(f"  ..   unpinned LeftFoot is {free_d * 100:.1f} cm from the "
          f"target", flush=True)

    print("\n-- pinned: the same seed, one effector_target --", flush=True)
    # Built by the REAL serializer, not by hand. A hand-written body here
    # carried a "weight" field that effector_target does not accept, and the
    # server refused it with a 422 -- a gate that tests my idea of the wire
    # rather than the code's is worth nothing.
    wire = _marker_to_wire(_PinMarker(PIN_FRAME, TARGET))
    check("the serializer produced an effector_target",
          wire is not None and wire.get("type") == "effector_target", wire)
    pinned = foot_at_pin(generate([wire]))
    check("the pinned run keys a LeftFoot", pinned is not None)
    if pinned is None:
        return
    pin_d = math.dist((pinned[0], pinned[2]), (TARGET[0], TARGET[2]))

    check(f"the pin moved the foot closer "
          f"({free_d * 100:.1f} cm -> {pin_d * 100:.1f} cm)",
          pin_d < free_d - 0.02,
          f"unpinned {free_d:.3f} m, pinned {pin_d:.3f} m")
    check(f"and it lands near the target ({pin_d * 100:.1f} cm away)",
          pin_d < 0.30, f"{pin_d:.3f} m from {TARGET}")
