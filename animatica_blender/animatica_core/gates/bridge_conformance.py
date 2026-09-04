"""The bridge keeps its contract — shared conformance invariants (stage C).

Ported from the 3ds Max plugin's bridge-conformance family (``spike_axis.py``,
``accept_m2_bridge.py``, ``accept_m2_builder.py``, ``accept_m2_animator.py``,
``accept_m2_rest.py`` — stage C of PLAN-suita-wieloDCC.md). Those five gates
test the BRIDGE itself, so they are per-host calls by nature — but they all
test it against the SAME ``BRIDGE-CONTRACT.md``, so the invariants are one
parameterized suite, not two independent ones. This module is that suite: the
checks a THIRD bridge must also pass, spoken entirely through
``animatica_core.bridge`` and ``animatica_core.gates.scene_api``.

The invariants, by contract section
-----------------------------------
* **meters at the boundary** (§1 Units): the skeleton wire block equals the
  registry block to 0.1 mm, world reads come back in meters, the ground
  plane's height round-trips in meters, ``sample_root_2d`` reads meters.
* **Y-up on the wire** (§1 Coordinate system): the block is stamped
  ``right_handed_y_up`` and an applied Y-up FK solve reads back Y-up.
* **read-after-seek coherence** (§1 Read-after-seek): world reads are taken
  at frames visited deliberately OUT of order — a read that returned the
  stale playhead pose reddens the FK comparison.
* **take/animation-range semantics** (§3.4): playhead and range round-trip;
  an apply widens the range to cover its keys and the batch sampler restores
  the playhead it moved.
* **keys are seconds** (§1 Frames vs seconds): ``key_times`` lands on the
  ``i / fps`` grid, ``key_count`` speaks the cross-axis union, and two keys
  interpolate LINEARLY at a mid-frame read.
* **static vs keyed** (§1): ``set_neutral_pose`` leaves zero keys and the
  rest pose; ``clear_animation`` removes every rotation key; a pose captured
  at rest is identity rotation on every joint, root included (a host-frame
  matrix must never leak into anchor poses).
* **persistence** (§3.1 stamps): the bind-pose block, the canonical mark and
  the prompt payload survive ``save_scene`` → delete → ``load_scene``.
* **lifecycle** (§3.1/§3.2): ``is_alive`` goes False on delete; leaves-first
  deletes take the whole rig and spare unrelated nodes; ``skip_root``
  refuses a root with several joint children instead of guessing.

What a host injects (:class:`HostBridgeConformance`) — only reads the
contract does not carry: the rest-orientation criterion (Max: root carries
``C = Rx(90)`` and descendants identity; MoBu: no PreRotation / zero local
rotation), a world read at a FRACTIONAL frame (Max: ``at_frame(10.5)``;
MoBu: ``Goto`` a seconds ``FBTime``), a node's label name, a throwaway
helper node, and the host-suffixed scratch scene path — plus one OPTIONAL
hook, ``quiesce_gui``, that silences the host GUI's scene-event handlers
around the suite's own scratch save (default no-op; see the field).

What stays in the MAX gates, deliberately: everything that pins pymxs or the
Z-up port rather than the contract — the sliderTime clamp, the unclamped
``attime``, Euler-controller readback, ``objectOffsetRot`` bone visuals,
``same_node``/``node_handle`` wrapper identity, appData mechanics, the
undo/theHold notes, duplicate-name and rebuild refusals, the Biped refusal,
and the synthetic-carrier half of ``skip_root`` (it needs host stamping).
Take-manager SCENE verbs are excluded by contract §3.6 (mapped hosts
legitimately disagree); its two pure helpers are checked here.

The two remaining contract daggers — ``measure_contact_heights_m`` (in the
animator) and ``activate_take`` (in the take manager) — are NOT exercised
(and deliberately not spelled dotted here: ``bridge_audit.py`` greps for
``alias.symbol``, and a dotted mention would register this module as a
phantom call site on the very rows it avoids): see the worded SKIP
lines this suite prints, and BRIDGE-CONTRACT.md §2b.
"""

from __future__ import annotations

import copy
import math
from contextlib import nullcontext
from dataclasses import dataclass

#: Frames of synthesized motion — enough for the FK solve to matter, short
#: enough for a live host's event loop.
N_FRAMES = 60
TRAVEL_M = 3.0
PREFIX = "wf"


@dataclass
class HostBridgeConformance:
    """The five reads/makes one host injects. Everything else is contract."""

    #: ``(node, is_top) -> (ok, detail)`` — does this joint carry the host's
    #: legal rest orientation? (Max: top == C, descendants identity; MoBu: no
    #: PreRotation, zero local rotation, XYZ order.)
    rest_orientation_ok: object
    #: ``(joint_map, joint_name, frame_float) -> (x, y, z)`` world position in
    #: METERS, Y-up, at a possibly FRACTIONAL frame — the one read
    #: ``scene_api.world_position_m`` cannot make (integer frames only).
    world_pos_frac_m: object
    #: ``node -> str`` — the label name ``skeleton.find_by_name`` resolves.
    node_name: object
    #: ``name -> node`` — a plain helper node that is NOT a joint.
    make_helper: object
    #: ``node -> None`` — delete one node.
    delete_node: object
    #: ``() -> str`` — a unique scratch path WITH the host's scene extension.
    scratch_scene_path: object
    #: OPTIONAL ``() -> context manager`` — quiesce the host GUI's scene-event
    #: handlers for the duration of the suite's scratch SAVE. Measured on
    #: MoBu: the tool window's OnFileSave handler writes the WINDOW's prompt
    #: state into the scene property on ANY FileSave — the gate's scratch save
    #: included — clobbering the payload the persistence checks just stored.
    #: Wrapped around ``save_scene`` ONLY (the clobber happens on OnFileSave,
    #: not on load); default no-op — a host whose GUI does not listen to saves
    #: (Max), or a headless host, injects nothing. Product behavior unchanged:
    #: the window may stay open, only the gate's own save runs quiesced.
    quiesce_gui: object = None


def _synth_motion(hierarchy, rest, n_frames, fps, travel_m):
    """Motion whose ``posed_joints`` come from an INDEPENDENT Y-up FK solve.

    The same construction as the Max animator gate: local rotations are
    authored, world positions are solved here — so applying the motion and
    reading the scene back checks the whole chain (euler decomposition, axis
    conversion, unit scaling, offset keys, root transport) against something
    other than itself.
    """
    import numpy as np

    def rx(d):
        a = math.radians(d)
        c, s = math.cos(a), math.sin(a)
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], float)

    def ry(d):
        a = math.radians(d)
        c, s = math.cos(a), math.sin(a)
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], float)

    def rz(d):
        a = math.radians(d)
        c, s = math.cos(a), math.sin(a)
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], float)

    names = [n for n, _p in hierarchy]
    idx = {n: i for i, n in enumerate(names)}
    parent_of = dict(hierarchy)
    nj = len(names)
    local = np.zeros((n_frames, nj, 3, 3), dtype=np.float64)
    posed = np.zeros((n_frames, nj, 3), dtype=np.float64)

    for f in range(n_frames):
        t = f / max(n_frames - 1, 1)
        swing = 35.0 * math.sin(2.0 * math.pi * t)
        r_local = {n: np.eye(3) for n in names}
        r_local[names[0]] = ry(20.0 * t)          # the root turns as it walks
        for arm, sign in (("LeftArm", 1.0), ("RightArm", -1.0)):
            if arm in idx:
                r_local[arm] = rz(sign * swing)
        for leg, sign in (("LeftUpLeg", 1.0), ("RightUpLeg", -1.0)):
            if leg in idx:
                r_local[leg] = rx(sign * swing * 0.6)
        if "Spine1" in idx:
            r_local["Spine1"] = rx(6.0 * math.sin(4.0 * math.pi * t))

        w_r, w_t = {}, {}
        root = names[0]
        w_r[root] = r_local[root]
        w_t[root] = np.array([0.0, rest[root][1], travel_m * t], float)
        for n in names:
            p = parent_of[n]
            if p is None:
                continue
            off = np.array(rest[n], float) - np.array(rest[p], float)
            w_r[n] = w_r[p] @ r_local[n]
            w_t[n] = w_t[p] + w_r[p] @ off
        for n in names:
            local[f, idx[n]] = r_local[n]
            posed[f, idx[n]] = w_t[n]

    return {"local_rot_mats": local, "posed_joints": posed,
            "joint_names": names, "fps": float(fps)}, idx


def run(host: HostBridgeConformance, check) -> dict:
    """Run every shared conformance check, reporting through *check*.

    Returns cleanup facts for the host wrapper: the prefix whose rig may
    remain, and the scratch scene file written. The rig built here is deleted
    by the suite itself (that deletion IS one of the checks); ground plane,
    prompt property, take range and the scratch file are wrapper business.
    """
    from animatica_core.bridge import (animation_target, animator, builder,
                                       constraint_capture as cap,
                                       ground, prompt_store_scene,
                                       skeleton as skel, take_manager)
    from animatica_core.gates import scene_api
    from animatica_core.skeleton import (get_joint_hierarchy,
                                         get_neutral_positions)

    scene_api.new_scene()
    hierarchy = get_joint_hierarchy("soma77")
    rest = get_neutral_positions("soma77", hip_height=1.0)
    root_name = hierarchy[0][0]
    result = {"prefix": PREFIX, "scene_path": None}

    print("\n-- time: fps is real, playhead and range round-trip --",
          flush=True)
    fps = scene_api.current_fps()
    check(f"current_fps is a real positive fps ({fps})",
          isinstance(fps, float) and math.isfinite(fps) and fps > 0, fps)
    scene_api.set_take_range(0, 100)
    check("take_range reads back what set_take_range wrote",
          tuple(scene_api.take_range()) == (0, 100),
          str(scene_api.take_range()))
    scene_api.goto_frame(37)
    check("goto_frame / current_frame round-trip",
          scene_api.current_frame() == 37, scene_api.current_frame())
    scene_api.goto_frame(0)

    print("\n-- pure helpers speak the same strings everywhere --", flush=True)
    check("unique_take_name is deterministic",
          take_manager.unique_take_name("AnimaticaProbe")
          == "AnimaticaProbe_000",
          take_manager.unique_take_name("AnimaticaProbe"))
    check("block_take_name truncates the id to 8",
          take_manager.block_take_name("abcdefghijklmnop") == "block_abcdefgh",
          take_manager.block_take_name("abcdefghijklmnop"))

    print("\n-- the rig builds, in meters, with legal rest orientation --",
          flush=True)
    jm = builder.build_neutral_skeleton(PREFIX, hierarchy=hierarchy,
                                        rest_positions=rest)
    root = jm[root_name]
    check(f"builds every joint ({len(jm)}/{len(hierarchy)})",
          len(jm) == len(hierarchy), len(jm))
    worst, worst_j = 0.0, ""
    for name, _p in hierarchy:
        got = scene_api.world_position_m(jm, name, 0)
        err = max(abs(got[i] - rest[name][i]) for i in range(3))
        if err > worst:
            worst, worst_j = err, name
    check(f"rest positions round-trip in meters ({worst * 1000:.3f} mm)",
          worst <= 1e-4, f"worst {worst:.2e} m at {worst_j}")
    bad = []
    for name, parent in hierarchy:
        ok, detail = host.rest_orientation_ok(jm[name], parent is None)
        if not ok:
            bad.append(f"{name}: {detail}")
    check("every joint carries the host's legal rest orientation",
          not bad, str(bad[:4]))

    print("\n-- the wire block: Y-up, meters, and equal to the registry --",
          flush=True)
    block = skel.fbmodel_skeleton_to_skeleton(root)
    check("block serialises every joint",
          len(block["joints"]) == len(hierarchy),
          f"{len(block['joints'])}/{len(hierarchy)}")
    check("block is stamped Y-up / meters",
          block.get("coordinate_system") == "right_handed_y_up"
          and block.get("units") == "meters",
          f"{block.get('coordinate_system')} / {block.get('units')}")
    try:
        skel.validate_skeleton_block(block)
        check("validate_skeleton_block accepts it", True)
    except ValueError as exc:
        check("validate_skeleton_block accepts it", False, str(exc)[:120])
    check("rest_rotation is identity everywhere",
          all(list(j["rest_rotation"]) == [0.0, 0.0, 0.0, 1.0]
              for j in block["joints"]))
    canon = skel.build_canonical_skeleton_block("soma77", hip_height=1.0)
    by_name = {str(j["name"]).split(":")[-1]: j for j in block["joints"]}
    worst, worst_j = 0.0, ""
    for j in canon["joints"]:
        got = by_name.get(j["name"])
        if got is None:
            worst, worst_j = 9.9, j["name"] + " (missing)"
            break
        err = max(abs(float(a) - float(b)) for a, b in
                  zip(got["rest_translation"], j["rest_translation"]))
        if err > worst:
            worst, worst_j = err, j["name"]
    check("scene block == registry block (conversion transparent on the wire)",
          worst <= 1e-4, f"worst {worst:.2e} m at {worst_j}")
    seen, ordered = set(), True
    for j in block["joints"]:
        if j["parent"] is not None and j["parent"] not in seen:
            ordered = False
            break
        seen.add(j["name"])
    check("DFS emits parents before children", ordered)

    scaled = skel.apply_uniform_scale(
        skel.build_canonical_skeleton_block("soma77"), 2.0)
    check("apply_uniform_scale halves the offsets",
          abs(scaled["joints"][1]["rest_translation"][1]
              - canon["joints"][1]["rest_translation"][1] / 2.0) < 1e-9)
    check("is_canonical_skeleton recognises the rig",
          skel.is_canonical_skeleton(jm) is True)
    check("is_canonical_skeleton rejects a foreign rig",
          skel.is_canonical_skeleton({"Bip001": 1, "Bip001 Neck": 2})
          is False)

    # SOMA-77's root IS Hips, with three joint children; the contract requires
    # a refusal here, never a guess about which limb is "the" spine.
    try:
        skel.fbmodel_skeleton_to_skeleton(root, skip_root=True)
        check("skip_root refuses a root with several joint children", False,
              "no ValueError raised")
    except ValueError as exc:
        check("skip_root refuses a root with several joint children",
              "single" in str(exc), str(exc)[:120])

    print("\n-- block write-back moves the rig, restores it, keys nothing --",
          flush=True)
    probe = "Spine1"
    before = scene_api.world_position_m(jm, probe, 0)
    # first prove the pen writes: halved offsets must visibly move a joint
    half = skel.apply_uniform_scale(copy.deepcopy(block), 2.0)
    skel.apply_skeleton_block_to_fbmodel(root, half)
    moved = scene_api.world_position_m(jm, probe, 0)
    dist = math.dist(moved, before)
    check(f"write-back writes (halved offsets moved {probe} "
          f"{dist * 1000:.1f} mm)", dist > 0.005, dist)
    skel.apply_skeleton_block_to_fbmodel(root, copy.deepcopy(block))
    back = scene_api.world_position_m(jm, probe, 0)
    check("write-back restores the rest pose",
          math.dist(back, rest[probe]) <= 1e-3,
          f"{back} vs {rest[probe]}")
    check("write-back creates no keys",
          scene_api.key_count(jm[probe], "rotation") == 0
          and scene_api.key_count(jm[probe], "translation") == 0,
          f"rot={scene_api.key_count(jm[probe], 'rotation')} "
          f"trn={scene_api.key_count(jm[probe], 'translation')}")

    print(f"\n-- apply {N_FRAMES} frames, read back against independent FK --",
          flush=True)
    scene_api.set_take_range(0, 40)          # deliberately short: apply widens
    md, idx = _synth_motion(hierarchy, rest, N_FRAMES, fps, TRAVEL_M)
    animator.apply_animation(jm, md, hierarchy, fps, PREFIX)

    n = N_FRAMES
    worst, worst_at = 0.0, ""
    # Frames visited OUT of order on purpose: a world read that skipped the
    # seek-and-evaluate would hand back the previous frame's pose.
    for f in (n - 1, 0, n // 2, n // 3):
        for name, _p in hierarchy:
            got = scene_api.world_position_m(jm, name, f)
            want = md["posed_joints"][f][idx[name]]
            err = max(abs(got[i] - float(want[i])) for i in range(3))
            if err > worst:
                worst, worst_at = err, f"{name}@f{f}"
    check(f"all {len(hierarchy)} joints match the independent Y-up FK solve "
          f"(worst {worst * 100:.3f} cm)", worst <= 2e-3,
          f"worst {worst:.2e} m at {worst_at}")
    end = scene_api.world_position_m(jm, root_name, n - 1)
    check(f"root travelled {TRAVEL_M} m +/- 0.05",
          abs(end[2] - TRAVEL_M) <= 0.05, f"z={end[2]:.4f}")
    check("the apply widened the take range to cover its keys",
          scene_api.take_range()[1] >= n - 1, str(scene_api.take_range()))

    print("\n-- keys: a seconds grid, union counts, linear interpolation --",
          flush=True)
    kt = scene_api.key_times(jm[root_name], "translation")
    check(f"{n} translation key times on the root", len(kt) == n, len(kt))
    check("key_times comes back sorted", list(kt) == sorted(kt))
    grid = max((abs(float(kt[i]) - i / fps) for i in range(len(kt))),
               default=9.9) if len(kt) == n else 9.9
    # 1e-6 s, not the earlier 1e-4: measured 2026-09-02, both hosts key
    # EXACTLY on the grid once the Max animator snapped to its tick grid
    # (before that its keys sat 1/4800 s = 0.208 ms early -- a value the
    # old 0.1 ms bound would have caught only if the host's key_times verb
    # reported tick-resolution seconds rather than rounded frames). A
    # microsecond leaves three orders of margin over float noise and none
    # for a tick.
    check(f"key times land on the seconds grid i/fps "
          f"({grid * 1000:.4f} ms off)", grid <= 1e-6, grid)
    check(f"key_count speaks the cross-axis union ({n} on a driven joint)",
          scene_api.key_count(jm["LeftArm"], "rotation") == n,
          scene_api.key_count(jm["LeftArm"], "rotation"))
    a = host.world_pos_frac_m(jm, root_name, 10.0)
    b = host.world_pos_frac_m(jm, root_name, 11.0)
    mid = host.world_pos_frac_m(jm, root_name, 10.5)
    want_mid = [(a[i] + b[i]) / 2.0 for i in range(3)]
    lin = max(abs(mid[i] - want_mid[i]) for i in range(3))
    check("keys interpolate LINEARLY (mid-frame == chord midpoint)",
          lin <= 1e-4,
          f"mid={[round(v, 5) for v in mid]} "
          f"want={[round(v, 5) for v in want_mid]}")

    print("\n-- the reads the request builder lives on --", flush=True)
    check("resolve_motion_root_name names the write root",
          bool(animation_target.resolve_motion_root_name(jm, md)),
          animation_target.resolve_motion_root_name(jm, md))
    world = animation_target.capture_motion_root_world_m(jm, root, n - 1, md)
    check("capture_motion_root_world_m agrees with the scene",
          world is not None and abs(world[2] - TRAVEL_M) < 0.05, str(world))
    yaw = animation_target.capture_motion_root_world_yaw_rad(jm, root, 0, md)
    check("capture_motion_root_world_yaw_rad is zero on the unturned frame",
          yaw is not None and abs(float(yaw)) < 1e-3, str(yaw))

    root2d = cap.sample_root_2d(jm, n - 1, include_heading=True)
    check("sample_root_2d returns the wire shape",
          "xz" in root2d and "heading_radians" in root2d, str(root2d)[:100])
    check("sample_root_2d reads meters, at the requested frame",
          abs(float(root2d["xz"][1]) - TRAVEL_M) < 0.05,
          f"z={root2d['xz'][1]}")
    eff = cap.sample_effector_position(jm, "LeftHand", 15,
                                       ctype="effector_target")
    check("sample_effector_position carries position + root context",
          "position" in eff and "root_xz" in eff, str(list(eff))[:120])
    frames = cap.scan_keyed_frames(jm, joint_names=["LeftArm"])
    check("scan_keyed_frames finds exactly the keyed span",
          len(frames) == n and frames[0] == 0 and frames[-1] == n - 1,
          f"{len(frames)} frames {frames[:3]}..{frames[-3:]}")
    scene_api.goto_frame(0)
    batch = cap.sample_keyed_frames(jm, "root2d", [0, 15, n - 1])
    check("sample_keyed_frames batches by frame",
          set(batch) == {0, 15, n - 1}, str(sorted(batch)))
    check("the batch sampler restored the playhead",
          scene_api.current_frame() == 0, scene_api.current_frame())
    pose = cap.sample_pose_keyframe(jm, 10)
    check("sample_pose_keyframe returns quats + root position",
          "joint_rotations" in pose and "root_position" in pose
          and len(pose["joint_rotations"]) == len(hierarchy),
          str(list(pose))[:100])
    q = list(pose["joint_rotations"].values())[0]
    check("its quaternions are unit length",
          abs(sum(float(c) * float(c) for c in q) - 1.0) < 1e-6, str(q))

    print("\n-- static vs keyed: clear, then a neutral pose with no curves --",
          flush=True)
    animator.clear_animation(jm)
    left = sum(scene_api.key_count(jm[nm], "rotation")
               for nm, _p in hierarchy)
    check("clear_animation removes every rotation key", left == 0, left)
    animator.set_neutral_pose(jm, hierarchy, rest)
    keyed = [nm for nm, _p in hierarchy
             if scene_api.key_count(jm[nm], "rotation")
             or scene_api.key_count(jm[nm], "translation")]
    check("set_neutral_pose leaves ZERO keys (static values only)",
          not keyed, str(keyed[:5]))
    worst = 0.0
    for name, _p in hierarchy:
        got = scene_api.world_position_m(jm, name, 0)
        worst = max(worst, max(abs(got[i] - rest[name][i]) for i in range(3)))
    check(f"set_neutral_pose restores the rest positions "
          f"({worst * 1000:.3f} mm)", worst <= 1e-4, f"{worst:.2e} m")
    # A rig at rest must capture as identity on EVERY joint, root included.
    # The Max bridge once read the root's controller raw and shipped its
    # local frame C = Rx(+90) inside every anchoring pose ("the character
    # starts face-down"); this catches that class on any host. q and -q are
    # the same rotation, so compare through |w|.
    pose0 = cap.sample_pose_keyframe(jm, 0)
    worst, worst_j = 0.0, ""
    for name, q in pose0["joint_rotations"].items():
        dev = max(abs(float(q[0])), abs(float(q[1])), abs(float(q[2])),
                  abs(1.0 - abs(float(q[3]))))
        if dev > worst:
            worst, worst_j = dev, str(name)
    check("a pose captured at rest is identity on every joint, root "
          "included (no host-frame matrix leaks into anchor poses)",
          worst <= 1e-5, f"worst {worst:.2e} at {worst_j}")

    print("\n-- stamps and prompts survive save / delete / load --",
          flush=True)
    payload = {"blocks": [{"id": "b1", "text": "a person walks"}], "v": 2}
    prompt_store_scene.store_prompts_property(payload)
    check("prompts round-trip in memory",
          prompt_store_scene.load_prompts_property() == payload,
          str(prompt_store_scene.load_prompts_property())[:120])
    skel.mark_canonical(root)
    skel.store_hip_height(root, 0.97)
    skel.store_namespace(root, PREFIX)
    skel.store_bind_pose_property(root, block)
    roots = skel.list_scene_skeleton_roots()
    check("list_scene_skeleton_roots finds exactly one root",
          len(roots) == 1, len(roots))
    check("...and the canonical mark is readable through canonical_only",
          len(skel.list_scene_skeleton_roots(canonical_only=True)) == 1,
          len(skel.list_scene_skeleton_roots(canonical_only=True)))
    root_label = host.node_name(root)
    check("find_by_name resolves the root's label name",
          skel.find_by_name(root_label) is not None, root_label)
    deep = jm[hierarchy[-1][0]]
    climbed = skel.find_skeleton_root(deep)
    check("find_skeleton_root climbs from a leaf to the root",
          climbed is not None and host.node_name(climbed) == root_label,
          "None" if climbed is None else host.node_name(climbed))

    path = host.scratch_scene_path()
    result["scene_path"] = path
    # The one save this suite performs, quiesced through the OPTIONAL host
    # hook so a GUI that writes its own state on every FileSave (MoBu's
    # prompt store) cannot overwrite the payload stored above.
    with (host.quiesce_gui or nullcontext)():
        saved = scene_api.save_scene(path)
    check("save_scene returns the path it wrote", saved == path, saved)

    check("is_alive is True on a live node", scene_api.is_alive(root) is True)
    builder.delete_skeleton_from_root(root)
    check("delete_skeleton_from_root kills the whole rig (is_alive False)",
          scene_api.is_alive(root) is False
          and scene_api.is_alive(deep) is False,
          f"root={scene_api.is_alive(root)} leaf={scene_api.is_alive(deep)}")
    check("no skeleton root is left after the delete",
          len(skel.list_scene_skeleton_roots()) == 0,
          len(skel.list_scene_skeleton_roots()))

    scene_api.load_scene(path)
    reloaded = skel.find_by_name(root_label)
    check("the rig survives save/load", reloaded is not None, root_label)
    if reloaded is not None:
        lb = skel.load_bind_pose_property(reloaded)
        check("the bind-pose block survives save/load",
              isinstance(lb, dict)
              and len(lb.get("joints", [])) == len(hierarchy),
              str(type(lb)))
        check("the canonical mark survives save/load",
              len(skel.list_scene_skeleton_roots(canonical_only=True)) == 1,
              len(skel.list_scene_skeleton_roots(canonical_only=True)))
    check("prompts survive save/load (they live in the scene file)",
          prompt_store_scene.load_prompts_property() == payload,
          str(prompt_store_scene.load_prompts_property())[:120])

    print("\n-- the prefix sweep spares unrelated nodes --", flush=True)
    helper = host.make_helper("not_ours")
    builder.delete_skeleton(PREFIX)
    check("delete_skeleton leaves unrelated nodes alone",
          scene_api.is_alive(helper) is True
          and (reloaded is None or scene_api.is_alive(reloaded) is False),
          f"helper={scene_api.is_alive(helper)}")
    host.delete_node(helper)

    print("\n-- the ground plane answers in meters --", flush=True)
    check("no plane yet -> height is None",
          ground.get_ground_world_y_m() is None,
          ground.get_ground_world_y_m())
    check("setting height with no plane is a no-op",
          ground.set_ground_world_y_m(1.0) is None)
    plane = ground.create_ground(0.0)
    check("create_ground makes a plane", plane is not None)
    again = ground.create_ground(0.0)
    check("create_ground is idempotent",
          again is not None
          and host.node_name(again) == host.node_name(plane),
          "None" if again is None else host.node_name(again))
    ground.set_ground_world_y_m(0.45)
    got = ground.get_ground_world_y_m()
    check("ground height round-trips in meters",
          got is not None and abs(float(got) - 0.45) < 1e-4, got)
    ground.set_ground_visible(False)
    check("ground visibility round-trips",
          ground.get_ground_visible() is False, ground.get_ground_visible())
    ground.set_ground_visible(True)
    check("...and back", ground.get_ground_visible() is True,
          ground.get_ground_visible())

    # The two contract daggers, cited and NOT exercised — per BRIDGE-CONTRACT
    # §2b these rows are still genuine omissions in the Max fork of the
    # window, and a shared invariant that called them would fail one host on
    # a known, documented hole rather than on a regression.
    print("\n  ..   SKIP measure_contact_heights_m (animator) -- contract "
          "dagger (BRIDGE-CONTRACT.md §2b): still absent from max_bridge; "
          "not exercised by the shared suite", flush=True)
    print("  ..   SKIP activate_take (take manager) -- contract dagger "
          "(BRIDGE-CONTRACT.md §2b): still absent from max_bridge; "
          "not exercised by the shared suite", flush=True)
    print("  ..   note: take_manager scene verbs are excluded by contract "
          "§3.6 -- mapped hosts legitimately disagree; only the two pure "
          "helpers are shared", flush=True)

    return result
