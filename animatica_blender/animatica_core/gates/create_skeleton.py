"""The Create Skeleton flow builds a rig the GUI then recognises (gate m5).

Ported from the 3ds Max plugin's ``accept_m5_create_skeleton.py`` (G2 of
PLAN-suita-wieloDCC.md). The original pins three defects of the MoBu-to-Max
port, all of which looked like "this skeleton is not supported" from the
outside: a ``root.LongName`` read pymxs nodes do not have, a GUI that asked
the HIK stub and printed its by-design refusal as a red error, and a pick
highlight that imported ``pyfbsdk`` in Max.

**This module is the flow those defects broke, not the defects themselves.**
The flow -- build a neutral rig, mark it canonical, resolve it by the
namespace-qualified name the picker shows, round-trip that name back to the
node, derive a bare-named joint map, select the rig in the viewport -- is the
product behaviour, identical in every host. The defect pins are host facts
and STAY IN THE HOST WRAPPER, exactly as ``surface.py`` keeps 3ds Max's
``_reports_exceptions`` check in the Max gate:

* ``not hasattr(root, "LongName")`` and the "no node ``.LongName`` in the
  tool window" AST scan -- Max-only: ``LongName`` is a real MotionBuilder
  property that MoBu's window uses legitimately, and the hazard being pinned
  is precisely that pymxs lacks it;
* the HIK trio (``is_available() is False``, ``unsupported_reason``, the
  misleading ``validate_hik_slots`` list) -- Max's ``hik`` is a documented
  stub; MoBu's is a real HumanIK and has no ``is_available`` at all;
* "create never characterises unguarded" -- the guard exists to keep a
  stub host from asking; a real-HIK host calls ``_run_create_hik`` freely.

Two host differences that are data, not logic, ride on
:class:`HostCreateSkeleton`: how a node's namespace-qualified name is read
(Max grew ``skeleton.long_name`` because pymxs has no ``LongName``; MoBu
reads the property), and how two node wrappers are compared for identity.
``verify_adoptable`` is optional because only Max's builder exposes it -- a
host without the verifier skips that one check WITH WORDS rather than
pretending it ran.

Two calls changed form, both meaning-preserving: ``rt.resetMaxFile`` +
``invalidate_units`` became ``scene_api.new_scene()`` (the Max half performs
both), and the viewport-selection count is read back through
``scene_api.selection()`` instead of ``rt.selection.count`` -- same scene
fact, contract vocabulary. The Max-only half of that check (``select_joints``
RETURNING 77, a Max bridge signature; MoBu's returns None) stays in the Max
wrapper.
"""

from __future__ import annotations

import ast
import io
from dataclasses import dataclass


@dataclass
class HostCreateSkeleton:
    """What one host tells this gate about its create-skeleton flow."""

    #: The host's main window source -- the create handler must exist there.
    tool_window_path: str
    #: ``node -> "namespace:Joint"`` -- the name the picker displays and
    #: ``find_by_name`` resolves (Max: ``skeleton.long_name``; MoBu: the
    #: node's own ``LongName`` property).
    long_name: object
    #: ``(a, b) -> bool`` -- same underlying scene node, across wrappers.
    same_node: object
    #: ``joint_map -> list_of_problems``, or None where the host's builder
    #: has no such verifier (only Max's does today).
    verify_adoptable: object = None


def run(host: HostCreateSkeleton, check) -> dict:
    """Run the shared create-skeleton checks, reporting through *check*.

    Builds a rig in the scene and leaves it there, returning its joint map --
    cleanup is the host wrapper's business (a batch host's scene is thrown
    away; a live host deletes the rig from its root so the next gate's
    ``new_scene`` is not refused by the gate's own leftovers).
    """
    from animatica_core import constants
    from animatica_core.bridge import builder, skeleton as skel
    from animatica_core.gates import scene_api
    from animatica_core.skeleton import (get_joint_hierarchy,
                                         get_neutral_positions)

    scene_api.new_scene()

    prefix = constants.DEFAULT_PREFIX
    hierarchy = get_joint_hierarchy(constants.DEFAULT_SKELETON_NAME)
    rest = get_neutral_positions(constants.DEFAULT_SKELETON_NAME,
                                 hip_height=constants.DEFAULT_HIP_HEIGHT)

    print("\n-- the rig the button builds --", flush=True)
    joint_map = builder.build_neutral_skeleton(prefix, hierarchy=hierarchy,
                                               rest_positions=rest)
    check("77 joints built", len(joint_map) == 77, len(joint_map))
    if host.verify_adoptable is not None:
        problems = host.verify_adoptable(joint_map)
        check("rig is drivable by the apply path", problems == [], problems)
    else:
        print("     (no adoptable-verifier in this host's builder -- the "
              "apply path is exercised by its own gates)", flush=True)

    root = joint_map["Hips"]
    skel.mark_canonical(root)

    print("\n-- the identity the GUI resolves it by --", flush=True)
    check("long_name is namespace-qualified",
          host.long_name(root) == f"{prefix}:Hips", host.long_name(root))

    # The exact expression that aborted _on_create_skeleton in the Max port.
    roots = skel.list_scene_skeleton_roots(canonical_only=True)
    names = [host.long_name(r) for r in roots]
    check("picker list resolves without raising",
          names == [f"{prefix}:Hips"], names)
    # One form change against the Max original, meaning-preserving: there an
    # empty picker list made ``names[0]`` raise into an exit-99 with no reason;
    # here the round-trip check FAILs with words instead (the m16 gate's own
    # "a gate that crashes tells you less than one that fails" rule).
    check("find_by_name round-trips the picked name",
          bool(names) and host.same_node(skel.find_by_name(names[0]), root),
          "picker list is empty -- nothing to round-trip" if not names else "")

    print("\n-- what the pick handler then does --", flush=True)
    jm = builder.joint_map_from_root(root, prefix=prefix)
    check("joint_map_from_root returns bare canonical names",
          len(jm) == 77 and "Hips" in jm and f"{prefix}:Hips" not in jm,
          f"{len(jm)} keys")
    skel.select_joints(list(joint_map.values()))
    selected = len(scene_api.selection())
    check("picking a rig selects it in the viewport",
          selected == 77, f"scene selection {selected}")

    print("\n-- the create handler exists, read off the source --",
          flush=True)
    src = io.open(host.tool_window_path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((f for f in ast.walk(tree)
               if isinstance(f, ast.FunctionDef)
               and f.name == "_on_create_skeleton"), None)
    check("_on_create_skeleton exists", fn is not None)

    return joint_map
