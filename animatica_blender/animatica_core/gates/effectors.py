"""Leg and arm constraints exist in the viewport, offline half (gate m16).

Ported from the 3ds Max plugin's ``accept_m16_effectors.py`` (G2 of
PLAN-suita-wieloDCC.md). The story it pins: the effector pipeline was
four-fifths built and one-fifth absent, and the absent fifth was the visible
one -- ``constraint_viz.refresh()`` skipped everything that was not
``root2d``, so an authored leg pin produced no marker, a dragged marker never
wrote back, and a deleted one was never reconciled. A constraint you cannot
see is a constraint you cannot trust.

Offline only: markers, colours, write-back, reconcile keys, replace, and the
wire format straight off ``_marker_to_wire``. The served proof -- generate a
walk with a foot pin and measure that the foot lands on it -- stays a
SEPARATE gate (``effector_served``, shared since the final tranche: its
mid-gate ``new_scene`` follows the decided cleanup-before-re-reset design,
so a dead server never drags these offline checks down with it).

What is shared, and what a host injects
---------------------------------------
The gate speaks to ``constraint_viz`` through ``animatica_core.bridge`` --
``refresh`` / ``replace`` / ``live_marker_keys`` / ``set_skeleton_namespace``
are the same contract in every bridge. What is NOT the same is how one
inspects a single marker node: 3ds Max reads ``node.transform.pos`` through
its axis module and asks ``classOf`` for the shape, MotionBuilder reads a
world ``GetVector`` in centimeters and asks the ``LookUI`` property. Those
five reads/writes (find, position get/set, colour, shape) plus the delete are
:class:`HostEffectorViz` callables. Every position crossing the seam is
METERS, Y-up -- the same boundary the bridge contract fixes -- so the
assertions and their tolerances are byte-identical to the Max gate's.

The one call that changed form: ``rt.resetMaxFile`` + ``invalidate_units``
became ``scene_api.new_scene()``, whose Max half performs both (the units
cache is the verb's own business now). Behaviour-neutral by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The colour the left-hand marker must wear -- MoBu's left-hand blue, the
#: same 4-tone scheme the timeline uses. A shared constant, not a host fact:
#: both plugins author it from the same palette.
LEFT_HAND_RGB = (0x3A, 0x7B, 0xD5)

#: What the gate authors: one waypoint and all four effector types, each at
#: its own frame (10, 15, 20, 25) so keys are distinguishable.
EFFECTORS = {
    "left-foot":  (0.10, 0.05, 0.6),
    "right-foot": (-0.10, 0.05, 0.9),
    "left-hand":  (0.35, 1.20, 0.4),
    "right-hand": (-0.35, 1.20, 0.7),
}

CHARACTER_ID = "demo"
DISPLAY_NAME = "Demo"


@dataclass
class HostEffectorViz:
    """How one host inspects a single viz marker node.

    Positions are METERS, Y-up, world -- both directions. The host converts
    to and from its native frame (Max: Z-up cm/inches via its axis module;
    MoBu: Y-up centimeters) exactly as its bridge does everywhere else.
    """

    #: ``(character_id, (type, frame)) -> node | None``
    find_marker: object
    #: ``node -> (x, y, z)`` world position, meters, Y-up.
    marker_pos: object
    #: ``(node, (x, y, z))`` -- move the marker, as a user drag would.
    set_marker_pos: object
    #: ``node -> (r, g, b)`` ints 0-255.
    marker_color: object
    #: ``node -> (is_cross, detail)`` -- the shape check, in the host's own
    #: vocabulary (Max: ``classOf`` says Point; MoBu: ``LookUI == 1``).
    marker_is_cross: object
    #: ``node -> None`` -- delete the marker as a user viewport delete would.
    delete_marker: object


class Marker:
    """The duck shape ``constraint_viz`` and ``_marker_to_wire`` read."""

    def __init__(self, ctype, frame, value, joint=""):
        self.type, self.frame, self.value, self.joint = ctype, frame, value, joint


def _need(host: HostEffectorViz, check, key):
    """The marker node for *key*, or None with a FAIL already recorded."""
    node = host.find_marker(CHARACTER_ID, key)
    if node is None:
        check(f"marker {key[0]} @F{key[1]} exists to inspect", False, "missing")
    return node


def run(host: HostEffectorViz, check) -> None:
    """Run every shared m16 check, reporting through *check*.

    Returns early when a marker it must inspect is missing -- the FAIL is
    already on the ledger, and every check past that point would raise
    instead of reporting (a gate that crashes tells you less than one that
    fails). The wrapper's ledger decides the verdict.
    """
    from animatica_core.bridge import constraint_viz as viz
    from animatica_core.gates import scene_api

    scene_api.new_scene()
    viz.set_skeleton_namespace("animatica")

    markers = [Marker("root2d", 0, {"xz": [0.0, 0.0]})]
    markers += [Marker(t, 10 + i * 5, {"position": list(p)})
                for i, (t, p) in enumerate(EFFECTORS.items())]
    viz.refresh(CHARACTER_ID, DISPLAY_NAME, markers)

    print("\n-- every effector type gets a marker --", flush=True)
    keys = viz.live_marker_keys(CHARACTER_ID)
    for i, t in enumerate(EFFECTORS):
        check(f"{t} marker exists", (t, 10 + i * 5) in keys, sorted(keys))
    check("the waypoint is still there too", ("root2d", 0) in keys)

    print("\n-- it sits at the authored 3D position, colour-coded --",
          flush=True)
    node = _need(host, check, ("left-hand", 20))
    if node is None:
        return
    got = host.marker_pos(node)
    want = EFFECTORS["left-hand"]
    err = max(abs(got[i] - want[i]) for i in range(3))
    check(f"left-hand marker at its position ({err * 1000:.2f} mm off)",
          err < 1e-4, f"{got} vs {want}")
    check("and NOT slammed onto the floor (Y survives)",
          abs(got[1] - 1.20) < 1e-4, got[1])
    rgb = tuple(host.marker_color(node))
    check("in MoBu's left-hand blue", rgb == LEFT_HAND_RGB, rgb)
    is_cross, shape_detail = host.marker_is_cross(node)
    check("a cross, not a circle", is_cross, shape_detail)

    print("\n-- a drag writes the FULL position back --", flush=True)
    dragged = (0.5, 1.45, 0.9)
    host.set_marker_pos(node, dragged)
    lh_marker = markers[3]
    assert lh_marker.type == "left-hand"
    viz.refresh(CHARACTER_ID, DISPLAY_NAME, markers)
    wrote = lh_marker.value.get("position")
    err = max(abs(float(wrote[i]) - dragged[i]) for i in range(3))
    check(f"position round-trips through the drag ({err * 1000:.2f} mm)",
          err < 1e-4, wrote)
    check("Y specifically survived the write-back",
          abs(float(wrote[1]) - 1.45) < 1e-4, wrote[1])

    print("\n-- replace() overrides a drag, for effectors too --", flush=True)
    fresh = Marker("left-hand", 20, {"position": [0.0, 1.0, 0.0]})
    viz.replace(CHARACTER_ID, DISPLAY_NAME, fresh)
    node = _need(host, check, ("left-hand", 20))
    if node is None:
        return
    got = host.marker_pos(node)
    check("the fresh capture wins over the drag",
          max(abs(got[i] - (0.0, 1.0, 0.0)[i]) for i in range(3)) < 1e-4, got)

    print("\n-- deleting a marker is seen by the reconcile keys --",
          flush=True)
    host.delete_marker(node)
    keys = viz.live_marker_keys(CHARACTER_ID)
    check("the deleted effector left the live key set",
          ("left-hand", 20) not in keys, sorted(keys))

    print("\n-- fullbody still gets no marker, deliberately --", flush=True)
    # It pins a body SHAPE; no point in space could honestly stand for it.
    viz.refresh(CHARACTER_ID, DISPLAY_NAME, [Marker("fullbody", 40, {})])
    check("a fullbody constraint builds nothing",
          ("fullbody", 40) not in viz.live_marker_keys(CHARACTER_ID))

    print("\n-- the wire speaks effector_target --", flush=True)
    from animatica_core.core.request_builder import _marker_to_wire
    wire = _marker_to_wire(
        Marker("left-foot", 30, {"position": [0.1, 0.05, 0.6]},
               joint="LeftFoot"))
    check("a foot pin serializes", wire is not None, wire)
    if wire:
        check("as effector_target", wire.get("type") == "effector_target",
              wire)
        check("with the joint and the frame",
              wire.get("joint") == "LeftFoot" and wire.get("frames") == [30],
              wire)
        pos = (wire.get("positions") or [[None] * 3])[0]
        check("carrying the full 3D position",
              pos and abs(float(pos[1]) - 0.05) < 1e-9, wire)
