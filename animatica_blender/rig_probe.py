"""Rig probing — pure ``bpy`` inspection of the user's armature.

Everything here reads the armature/action data already sitting in the
Blender scene (deform-bone detection, control-rig detection, the T-pose
rest-layout rewrite, the request skeleton serialization, the authored
frame range, and "did the user actually edit this bone" detection). None
of it talks to the network or the MMCP wire format beyond the skeleton
dict shape, so it stays out of the shared ``animatica_core`` convergence
surface — there's nothing here another host could share, it's all reading
Blender's own data model.
"""

from __future__ import annotations

from typing import Any

import bpy
from mathutils import Matrix, Vector

from . import constraints_ui, coords


def compute_frame_range(
    prompt_blocks,
    armature_obj: bpy.types.Object | None,
    scene: bpy.types.Scene,
) -> tuple[int, int]:
    """The generation window — union of every piece of user-authored timing
    content on the timeline.

    Pulls from:
      * Enabled ``PromptBlock`` ranges (the addon's "actions" — text segments
        drawn on the timeline strip).
      * The target armature's active source action keyframe span (skipping
        Animatica-generated bakes so a regenerate doesn't latch onto its
        own previous output).

    Falls back to ``scene.frame_start..scene.frame_end`` only when nothing's
    been authored. Caller-side, this replaces the older "scene-range is the
    request length" heuristic — generating only as far as the user actually
    drew content keeps short-segment edits from paying for an empty 120-frame
    tail.
    """
    starts: list[int] = []
    ends: list[int] = []

    for b in prompt_blocks or ():
        if not getattr(b, "enabled", True):
            continue
        starts.append(int(b.frame_start))
        ends.append(int(b.frame_end))

    src = (
        armature_obj.animation_data.action
        if armature_obj is not None
        and armature_obj.animation_data
        and armature_obj.animation_data.action
        else None
    )
    if src is not None and not src.name.startswith(_GENERATED_ACTION_PREFIX):
        kfs: set[int] = set()
        for fc in constraints_ui.iter_action_fcurves(src):
            for kp in fc.keyframe_points:
                kfs.add(int(round(kp.co.x)))
        if kfs:
            starts.append(min(kfs))
            ends.append(max(kfs))

    if starts and ends:
        return (min(starts), max(ends))
    return (int(scene.frame_start), int(scene.frame_end))


# ---------------------------------------------------------------------------
# Deform-bone / control-rig detection
# ---------------------------------------------------------------------------

# Bone-following constraint types: when a deform bone carries one of
# these targeting a sibling bone, that's the universal signal that this
# rig has a control layer driving the deform layer (Mixamo Control Rig,
# Rigify, Auto-Rig Pro, custom). Used only for "is this a control rig?"
# detection — the deform set itself comes from ``bone.use_deform``, the
# flag rigs use to mark which bones skin the mesh.
_BONE_FOLLOWING_CONSTRAINTS = frozenset({
    "COPY_TRANSFORMS",
    "COPY_ROTATION",
    "COPY_LOCATION",
    "IK",
})


def detect_deform_bones(armature_obj: bpy.types.Object) -> set[str]:
    """Return the set of bone names that skin the mesh — i.e. bones with
    ``Bone.use_deform`` set. Every standard rig system (Mixamo, Rigify
    DEF-*, Auto-Rig Pro, custom) flags its deform bones with this; the
    flag is what drives vertex skinning, so it's the canonical "this is
    the actual character skeleton" signal that's stable across naming
    conventions and constraint structures.
    """
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return set()
    return {pb.name for pb in armature_obj.pose.bones if pb.bone.use_deform}


# Face / head-detail joints the server's body retarget can't use. Listed by
# the *first* dotted component of the bare bone name (after stripping a
# leading ``DEF-`` if present). Rigify's metarig and its generated DEF
# chain both use these tokens with `.L`/`.R`/`.NNN` suffixes — e.g.
# ``brow.B.L.002``, ``DEF-lid.T.R.001``. We drop the entire face subtree so
# it doesn't reach the body retargeter — analogous to how
# ``hands_follow_forearm`` strips Mixamo finger subtrees inside the
# retarget library. The bones stay on the rig; we just don't send them.
_FACE_BONE_TOKENS: frozenset[str] = frozenset({
    "brow", "cheek", "chin", "ear", "eye", "face", "forehead",
    "jaw", "lid", "lip", "mouth", "nose", "teeth", "temple",
    "tongue",
})


def _is_face_bone(name: str) -> bool:
    """True if ``name`` looks like a face / head-detail bone — matches the
    head token (first dotted component) against :data:`_FACE_BONE_TOKENS`.
    Strips a leading ``DEF-`` first so Rigify's generated chain matches the
    same way as the bare metarig names.
    """
    bare = name[4:] if name.startswith("DEF-") else name
    head_token = bare.split(".", 1)[0].lower()
    return head_token in _FACE_BONE_TOKENS


# Rigify splits each major limb segment into two halves at generate time —
# ``DEF-upper_arm.L``/``DEF-upper_arm.L.001``, ``DEF-forearm.L``/`.001``,
# ``DEF-thigh.L``/`.001``, ``DEF-shin.L``/`.001``. The ``.001`` halves are
# pure skinning helpers for bendy-bone smoothing — they don't appear on
# the metarig (which has a single bone per segment) and they don't map to
# anything in the canonical SOMA chain (shoulder/elbow/wrist =
# upper_arm/forearm/hand, 3 joints per arm). Sending them doubles the
# joint count on every limb and confuses the bone classifier's
# region/side picks. We drop them at the request edge.
_TWEAK_HALF_SEGMENTS: frozenset[str] = frozenset({
    "upper_arm", "forearm", "thigh", "shin",
})


def _is_tweak_half(name: str) -> bool:
    """True if ``name`` is a Rigify bendy ``.001`` half of a major limb
    segment — e.g. ``DEF-upper_arm.L.001``, ``DEF-shin.R.001``.

    Matches by stripping a leading ``DEF-``, splitting the rest by ``.``,
    and asserting (a) the first component is in
    :data:`_TWEAK_HALF_SEGMENTS` and (b) the final component is exactly
    ``001``. Conservative enough to leave ``DEF-spine.001`` (a real spine
    segment, not a tweak half) alone.
    """
    bare = name[4:] if name.startswith("DEF-") else name
    parts = bare.split(".")
    if len(parts) < 2 or parts[-1] != "001":
        return False
    return parts[0].lower() in _TWEAK_HALF_SEGMENTS


def emitted_deform_bones(armature_obj: bpy.types.Object) -> set[str]:
    """Return the subset of deform bones that :func:`armature_to_skeleton`
    actually serializes — i.e. ``detect_deform_bones`` minus the face
    sub-tree and minus the Rigify bendy ``.001`` tweak halves.

    The pose-keyframe sampler and any other code that needs to know
    "which bones does the server see in this request" should call this
    so it stays in sync with what we emit.
    """
    deform = detect_deform_bones(armature_obj)
    if not deform:
        return deform

    face_drop = {n for n in deform if _is_face_bone(n)}
    if face_drop:
        all_pbs = {pb.name: pb for pb in armature_obj.pose.bones}
        for n in list(face_drop):
            stack = [all_pbs[n]] if n in all_pbs else []
            while stack:
                pb = stack.pop()
                for child in pb.children:
                    if child.bone.use_deform and child.name not in face_drop:
                        face_drop.add(child.name)
                    stack.append(child)
    deform = deform - face_drop
    deform = deform - {n for n in deform if _is_tweak_half(n)}
    return deform


# Arm-chain bones we rewrite to T-pose layout in the outgoing skeleton.
# Rigify's default human metarig has the upper arm at ~28° below
# horizontal — an A-pose that the bone classifier was not trained on and
# that warps how the reverse retarget maps SOMA77 onto the rig. We send
# horizontal arms instead, then pre-multiply the returned rotation keys
# by the bone's actual rest rotation to compensate (see
# :func:`t_pose_correction_quat` in ``gltf_to_blender``).
_T_POSE_ARM_TOKENS: frozenset[str] = frozenset({"upper_arm", "forearm", "hand"})


def is_t_pose_arm_bone(name: str) -> bool:
    """True if ``name`` is an arm-chain bone subject to T-pose rewriting."""
    bare = name[4:] if name.startswith("DEF-") else name
    return bare.split(".", 1)[0].lower() in _T_POSE_ARM_TOKENS


_IDENTITY_3X3 = Matrix.Identity(3)


def t_pose_q_matrix(armature_obj: bpy.types.Object, bone_name: str | None):
    """``Q[bone]`` — rotation taking the **request-layout** rest direction
    of ``bone_name`` (horizontal T-pose for arm-chain bones) to the
    **actual armature** rest direction (the metarig's A-pose, baked into
    ``matrix_local``).

    Used on *both* sides of the wire:

    * **Outbound** (``constraints_ui.sample_pose_keyframes``): convert the
      user-keyed pose, sampled in A-pose-relative basis, into a
      T-pose-relative rotation the server expects from the lied skeleton.
      Formula: ``R_T = Q[parent].T · R_A · Q[child]``.
    * **Inbound** (``gltf_to_blender.bake_gltf_to_armature``): convert the
      server's T-pose-relative rotation back to A-pose-relative so it
      lands correctly on the actual A-pose ``matrix_local``.
      Formula: ``R_A = Q[parent] · R_T · Q[child].T``.

    Mirrors the ``RestPoseAugmentor`` reference at
    ``animatica/retarget/augment.py`` — same per-bone change-of-rest math.
    Returns identity for any bone whose request layout matches the actual
    armature (everything outside the arm chain).
    """
    if not bone_name or not is_t_pose_arm_bone(bone_name):
        return _IDENTITY_3X3
    pb = armature_obj.pose.bones.get(bone_name)
    if pb is None:
        return _IDENTITY_3X3
    sign  = -1.0 if (".R" in bone_name) else 1.0
    t_dir = Vector((sign, 0.0, 0.0))                       # request rest dir
    a_dir = Vector(pb.bone.matrix_local.to_3x3().col[1])   # actual rest dir
    return t_dir.rotation_difference(a_dir).to_matrix()


def is_control_rig(armature_obj: bpy.types.Object) -> bool:
    """``True`` when the armature's deform layer is driven from a separate
    control layer via constraints — what people mean by "control rig".

    Plain rigs (just the deform bones, animated directly) return False:
    no constraints means no control layer, so request building runs
    against the bones the user authored without filtering.
    """
    if armature_obj is None or armature_obj.type != 'ARMATURE':
        return False
    deform_names = detect_deform_bones(armature_obj)
    if not deform_names:
        return False
    bone_names = {pb.name for pb in armature_obj.pose.bones}
    for pb in armature_obj.pose.bones:
        if pb.name not in deform_names:
            continue
        for c in pb.constraints:
            if getattr(c, "mute", False):
                continue
            if getattr(c, "influence", 1.0) <= 0.0:
                continue
            if c.type not in _BONE_FOLLOWING_CONSTRAINTS:
                continue
            if getattr(c, "target", None) is armature_obj and getattr(c, "subtarget", "") in bone_names:
                return True
    return False


def _closest_deform_ancestor(pb: bpy.types.PoseBone, deform: set[str]):
    """Walk up parents until one is in ``deform``. Returns the PoseBone or
    None for the topmost deform bone in the chain."""
    p = pb.parent
    while p is not None and p.name not in deform:
        p = p.parent
    return p


def _build_deform_parent_map(
    armature_obj: bpy.types.Object,
    deform: set[str],
) -> dict[str, str | None]:
    """Build a parent map for the deform-only skeleton tree.

    Returns ``{deform_bone_name: parent_deform_bone_name | None}`` with
    exactly one bone mapped to ``None`` (the root).

    Strategy:
      1. **Parent walk** — for each DEF bone, walk up ``pb.parent`` looking
         for another DEF. Handles the common case where DEFs form a clean
         chain in the bone hierarchy (Mixamo, plain deform rigs).
      2. **Spatial fallback** — when the walk yields no DEF ancestor (which
         happens on Rigify because DEFs parent through ``ORG-*``/``MCH-*``
         intermediaries), use rest-pose tail-to-head proximity: each
         orphan's parent is the DEF whose tail is closest to its head.
      3. **Single root** — among bones that still have no parent (orphans
         with no spatial match, or cycle-breakers), pick the one closest
         to the armature's origin as the root and attach the rest to it.

    The server requires exactly one ``parent: null`` joint per the MMCP
    skeleton schema — without the fallback, Rigify rigs emit ~98 roots
    and the request fails schema validation.
    """
    mw = armature_obj.matrix_world
    head_w: dict[str, "Vector"] = {}
    tail_w: dict[str, "Vector"] = {}
    for name in deform:
        pb = armature_obj.pose.bones[name]
        head_w[name] = mw @ pb.bone.head_local
        tail_w[name] = mw @ pb.bone.tail_local

    parent_map: dict[str, str | None] = {}

    # Step 1: parent walk.
    for name in deform:
        pb = armature_obj.pose.bones[name]
        anc = _closest_deform_ancestor(pb, deform)
        if anc is not None:
            parent_map[name] = anc.name

    # Step 2: spatial fallback for orphans, with cycle prevention.
    orphans = [n for n in deform if n not in parent_map]
    if orphans:
        # Per-orphan: closest DEF tail to my head (any DEF, not just other orphans).
        candidates: dict[str, tuple[str | None, float]] = {}
        for name in orphans:
            h = head_w[name]
            best = None
            best_dist = float("inf")
            for other in deform:
                if other == name:
                    continue
                d = (tail_w[other] - h).length
                if d < best_dist:
                    best_dist = d
                    best = other
            candidates[name] = (best, best_dist)

        # Pick the root: the orphan whose closest-tail distance is largest
        # (= most disconnected from any other bone's tail). Tiebreak by
        # distance to armature origin so the pelvis wins over an outlier.
        armature_origin = mw.translation
        root = max(
            orphans,
            key=lambda n: (candidates[n][1], -(head_w[n] - armature_origin).length),
        )
        parent_map[root] = None

        # Assign remaining orphans in order of increasing dist (closest matches
        # first). Cycle prevention: walking up parent_map from the candidate
        # must not reach ``name``. Stopping at an unresolved bone (one not yet
        # in parent_map) is fine — that bone will get its own parent assigned
        # in a later iteration, and the final tree resolves bottom-up.
        for name in sorted([o for o in orphans if o != root], key=lambda n: candidates[n][1]):
            candidate, _dist = candidates[name]
            cur = candidate
            cycle = False
            visited_in_walk: set[str] = set()
            while cur is not None:
                if cur == name:
                    cycle = True
                    break
                if cur in visited_in_walk:
                    # Existing cycle not involving ``name`` — defensive; shouldn't
                    # happen given how we build, but bail out cleanly if it does.
                    break
                visited_in_walk.add(cur)
                cur = parent_map.get(cur)  # None when cur is root or not yet placed
            parent_map[name] = root if cycle else candidate

    # Sanity: exactly one root.
    roots = [n for n in deform if parent_map.get(n) is None]
    assert len(roots) == 1, (
        f"_build_deform_parent_map produced {len(roots)} roots (expected 1): "
        f"{roots[:5]}{'...' if len(roots) > 5 else ''}"
    )

    return parent_map


def armature_to_skeleton(armature_obj: bpy.types.Object) -> dict[str, Any]:
    """Serialize the armature's rest layout to the MMCP `Skeleton` shape.

    Positions are expressed in MMCP frame (Y-up, meters). Heads are converted
    through ``armature.matrix_world`` before differencing, so rigs with a
    non-identity world transform (Mixamo's 90° + 0.01 scale is the common
    case) end up in the same world frame as the per-frame ``root_position``.
    Without this, offsets would live in armature-local frame while root pins
    live in world frame, and the 90° / scale would silently misalign them.

    When the armature has a control-rig setup (deform bones driven by
    constraints), only the deform bones are emitted — the server's bone
    classifier would otherwise see Ctrl_*/IK helpers/pole vectors and pick
    wrong slots. Parent links of deform bones get rewritten via
    :func:`_build_deform_parent_map` so the result is a single tree (the
    server's schema requires exactly one ``parent: null`` joint).
    """
    mw = armature_obj.matrix_world
    pose_bones = list(armature_obj.pose.bones)
    head_world_by_name: dict[str, Vector] = {
        pb.name: mw @ pb.bone.head_local for pb in pose_bones
    }

    # Apply the request-side filters (face strip + Rigify bendy ``.001``
    # tweak strip). :func:`emitted_deform_bones` is the single source of
    # truth — :func:`sample_pose_keyframes` calls it too so the
    # pose-keyframe constraints reference the same joint set the server
    # sees in the skeleton.
    deform = emitted_deform_bones(armature_obj)

    use_deform_filter = is_control_rig(armature_obj)
    parent_map = _build_deform_parent_map(armature_obj, deform) if use_deform_filter else None

    # The server requires parents to appear before their children in joints[].
    # Blender's pose-bone iteration order tracks edit-bone parent links, which
    # holds for plain rigs but breaks once ``_build_deform_parent_map`` rewires
    # parents spatially (e.g. ``DEF-forehead.L → DEF-brow.T.L.002`` may flip
    # the natural order). Emit in topological order: root, then each bone
    # whose parent is already emitted.
    if use_deform_filter:
        emit_order = _topological_order(parent_map)
        ordered_bones = [armature_obj.pose.bones[name] for name in emit_order]
    else:
        ordered_bones = pose_bones

    joints: list[dict[str, Any]] = []
    for pb in ordered_bones:
        if use_deform_filter and pb.name not in deform:
            continue
        if use_deform_filter:
            parent_name = parent_map[pb.name]  # may be None for the root
        else:
            parent_name = pb.parent.name if pb.parent else None
        if parent_name is None:
            local = head_world_by_name[pb.name]
        else:
            local = head_world_by_name[pb.name] - head_world_by_name[parent_name]
        mx, my, mz = coords.blender_pos_to_mmcp(local)
        joints.append({
            "name":             pb.name,
            "parent":           parent_name,
            "rest_translation": [float(mx), float(my), float(mz)],
            "rest_rotation":    [0.0, 0.0, 0.0, 1.0],
        })
    return {
        "joints":            joints,
        "coordinate_system": "right_handed_y_up",
        "units":             "meters",
    }


def _topological_order(parent_map: dict[str, str | None]) -> list[str]:
    """Return bone names in parent-before-child order given ``parent_map``.

    BFS from the root so siblings emit in the order they were inserted into
    the map (which mirrors the source iteration). Cycles in ``parent_map``
    would leave some nodes unvisited; asserting catches that since the
    server requires a tree.
    """
    children_of: dict[str, list[str]] = {}
    roots: list[str] = []
    for name, parent in parent_map.items():
        if parent is None:
            roots.append(name)
        else:
            children_of.setdefault(parent, []).append(name)

    order: list[str] = []
    queue = list(roots)
    while queue:
        name = queue.pop(0)
        order.append(name)
        queue.extend(children_of.get(name, ()))

    assert len(order) == len(parent_map), (
        f"_topological_order: visited {len(order)} of {len(parent_map)} bones "
        f"— parent_map has a cycle or disconnected node"
    )
    return order


# ---------------------------------------------------------------------------
# Frame range / generated-action detection
# ---------------------------------------------------------------------------

_GENERATED_ACTION_PREFIXES: tuple[str, ...] = (
    "Animatica_Motion",      # current motion-bake naming
    "Proscenium_Motion",     # pre-rename motion-bake naming
    "Proscenium_Generated",  # legacy pre-rename motion-bake naming
)
# Kept for back-compat in case anything imports it; ``str.startswith`` accepts
# either a string or a tuple, so the change is transparent at call sites.
_GENERATED_ACTION_PREFIX = _GENERATED_ACTION_PREFIXES


def _preview_frame_extent(
    action: bpy.types.Action | None,
) -> tuple[int | None, int | None]:
    """Min/max keyed frame across every fcurve on ``action``. Used by the
    per-block regen path to decide which boundary frames can supply a pose
    observation — only those that actually lie inside the existing bake.
    """
    if action is None:
        return None, None
    lo: int | None = None
    hi: int | None = None
    for fc in constraints_ui.iter_action_fcurves(action):
        for kp in fc.keyframe_points:
            f = int(round(kp.co.x))
            if lo is None or f < lo:
                lo = f
            if hi is None or f > hi:
                hi = f
    return lo, hi


# Value-delta epsilon for the "did the user actually rotate this bone, or did
# pressing I → Whole Character just re-stamp the existing pose value with a
# KEYFRAME type?" check. Quaternion components are in [-1, 1]; 5e-3 is a few
# tenths of a degree of rotation per component — well below user perception
# but well above float-roundtrip noise from depsgraph evaluation.
_EDIT_EPSILON = 5e-3


def _user_edited_bones_per_frame(
    action: bpy.types.Action | None,
    frame_start: int,
    frame_end: int,
) -> dict[int, set[str]]:
    """Map each user-keyed frame inside ``[frame_start, frame_end]`` to the
    set of bones the user *actually* rotated at that frame.

    Why this isn't just "bones with non-GENERATED keys at frame f":
    pressing ``I`` in Pose mode with Blender's default "Whole Character" or
    "Available" keying set inserts a KEYFRAME on every rotation channel of
    every bone, *using each bone's current evaluated value*. For bones the
    user didn't touch, that value equals the bake's GENERATED kp at frame
    f, so the new KEYFRAME-typed kp has the same value as the kp it
    replaced. Treating "kp.type != 'GENERATED'" as "the user edited this
    bone" would over-constrain the regen request — every joint would be
    pinned to the previous bake's pose at frame f — and the model would
    fall back to producing motion that doesn't transition to the user's
    deliberate edit.

    Heuristic: a bone counts as edited at frame f when any of its rotation
    quaternion components' value at f differs by more than ``_EDIT_EPSILON``
    from the linear interpolation of the surrounding GENERATED keys on the
    same fcurve. Bones whose value matches the natural interpolation are
    the "incidentally keyed" ones — the user pressed I and the value got
    re-stamped without actually changing.

    For dense bakes (a GENERATED key every frame) the linear interp
    collapses to "is the value at f equal to the value at f-1 / f+1?", which
    is a tight delta check. For sparse anchors (a kp with no surrounding
    GENERATED neighbours), the bone is conservatively flagged as edited —
    we can't disprove the user's intent, so we honour it.
    """
    if action is None:
        return {}

    # First pass: collect every non-GENERATED rotation kp in range, grouped
    # by (bone, frame) so we can evaluate "is this bone edited at frame f"
    # across all 4 quaternion components in one pass.
    candidates: dict[tuple[str, int], list[bpy.types.FCurve]] = {}
    kp_value_at: dict[tuple[str, int, int], float] = {}  # (bone, frame, axis) -> value

    for fc in constraints_ui.iter_action_fcurves(action):
        if "rotation" not in fc.data_path:
            continue
        bone = constraints_ui._bone_name_from_data_path(fc.data_path)
        if bone is None:
            continue
        for kp in fc.keyframe_points:
            if kp.type == 'GENERATED':
                continue
            f = int(round(kp.co.x))
            if not (frame_start <= f <= frame_end):
                continue
            candidates.setdefault((bone, f), []).append(fc)
            kp_value_at[(bone, f, int(fc.array_index))] = float(kp.co.y)

    edited: dict[int, set[str]] = {}
    for (bone, f), fcs in candidates.items():
        # Bone is edited if ANY of its rotation channels at frame f differs
        # from the surrounding-GENERATED interpolation at f.
        bone_edited = False
        for fc in fcs:
            axis = int(fc.array_index)
            v_user = kp_value_at[(bone, f, axis)]
            v_interp = _interp_from_generated(fc, f)
            if v_interp is None:
                # No surrounding GENERATED keys to compare against → trust
                # the user; treat as edited.
                bone_edited = True
                break
            if abs(v_user - v_interp) > _EDIT_EPSILON:
                bone_edited = True
                break
        if bone_edited:
            edited.setdefault(f, set()).add(bone)

    return edited


def _interp_from_generated(fc: bpy.types.FCurve, target_frame: int) -> float | None:
    """Linearly interpolate ``fc``'s GENERATED keyframes at ``target_frame``.

    Returns ``None`` when no GENERATED neighbours exist on either side
    (sparse fcurve, no bake context). Used by ``_user_edited_bones_per_frame``
    to decide whether a user kp's value matches what the bake would have
    produced at that frame.
    """
    left: tuple[float, float] | None = None
    right: tuple[float, float] | None = None
    for kp in fc.keyframe_points:
        if kp.type != 'GENERATED':
            continue
        x = float(kp.co.x)
        if x < target_frame and (left is None or x > left[0]):
            left = (x, float(kp.co.y))
        elif x > target_frame and (right is None or x < right[0]):
            right = (x, float(kp.co.y))
        elif x == target_frame:
            # GENERATED kp sitting on target_frame (rare — usually the
            # user's KEYFRAME replaced it) — that's the most direct
            # answer.
            return float(kp.co.y)
    if left is None or right is None:
        return None
    fl, vl = left
    fr, vr = right
    if fr == fl:
        return vl
    return vl + (target_frame - fl) * (vr - vl) / (fr - fl)
