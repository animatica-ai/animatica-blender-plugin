"""Bake deform-bone animation onto a Rigify control armature.

**Strategy**: helper-bone retargeting (adapted from :mod:`mixamo_bake`).

For each target control bone we want to bake (hips, chest, FK chain, IK
target, etc.), we create a helper bone on the *source* armature at the
target control's REST position + orientation, and parent it to the
source's corresponding DEF bone. The helper then inherits the source
DEF's animation while preserving the target control's rest reference —
so its world delta from rest exactly equals the source DEF's world delta
from rest. We add a ``COPY_TRANSFORMS`` constraint on the target control
pointing at this helper and bake.

This sidesteps two thorny problems direct matrix retargeting hits:

1. **Rest orientation mismatch.** Rigify's master controls (``hips``,
   ``chest``, ``neck``, ``head``) have local axes 88°–104° rotated from
   their corresponding ``DEF-spine.NNN`` bones. A direct rotation copy
   would force the controls into the wrong orientation at rest;
   helpers-at-target-rest sidestep it because the helper's reference
   *is* the target's rest, so source's delta from source's rest applied
   to that reference produces the target's delta from target's rest in
   world space.

2. **Chain composition.** Rigify's spine chain runs through MCH/ORG/tweak
   bones with COPY_TRANSFORMS resets at every spine_fk level. Manually
   simulating that chain for each target bone is fragile; letting
   Blender's depsgraph evaluate the constraint stack during the bake is
   correct by construction. ``bake_control_bones`` then samples each
   target's POSE matrix and converts to LOCAL relative to the depsgraph-
   evaluated parent — automatically producing the right basis.

Public entry: :func:`apply_anim_to_rigify`.
"""

from __future__ import annotations

import bpy
from mathutils import Matrix, Vector

from . import _bake_common, blender_compat


# ---------------------------------------------------------------------------
# Bone-name maps (target_control → source_def). The source is the bone
# whose animation we want the control to follow. Per-rig-type knowledge
# (which DEF drives which control) is encoded here.
# ---------------------------------------------------------------------------

# Spine FK chain: per-segment articulation, one spine_fk.NNN per metarig
# spine.NNN. The spine_fk chain is full-influence FK — rotating one bone
# accumulates cleanly through the chain — unlike the ``chest`` master,
# which drives ``MCH-spine.002/003`` at influence 0.5 LOCAL/LOCAL and
# produces visible discontinuities once the required rotation crosses
# the 180° quaternion boundary (typical for forward-roll motions whose
# chest delta reaches ~175°).
#
# Trade-off vs ``hips``/``chest`` masters: the user loses single-control
# upper-body steering. They can re-introduce hips/chest by manually
# keying those bones — or by re-enabling them here once we have a
# quaternion-sign-safe variant of the chest LOCAL/LOCAL copy.
SPINE_MASTER_PAIRS: list[tuple[str, str]] = [
    ("spine_fk",     "DEF-spine"),
    ("spine_fk.001", "DEF-spine.001"),
    ("spine_fk.002", "DEF-spine.002"),
    ("spine_fk.003", "DEF-spine.003"),
    ("neck",         "DEF-spine.005"),
    ("head",         "DEF-spine.006"),
]

# Torso translates with the deform root. ``apply_anim_to_rigify`` adds a
# post-bake correction pass that adjusts torso.location per-frame so the
# downstream DEF-spine lands exactly on the source root — Rigify's
# MCH-chain rest offsets shift positions under rotation and a naive
# helper-bone bake cannot anticipate them.
TORSO_TRANSLATION_PAIR: tuple[str, str] = ("torso", "DEF-spine")

# Shoulders.
SHOULDER_PAIRS: list[tuple[str, str]] = [
    ("shoulder.L", "DEF-shoulder.L"),
    ("shoulder.R", "DEF-shoulder.R"),
]

# Arm FK / IK.
ARM_FK_PAIRS: list[tuple[str, str]] = [
    ("upper_arm_fk.{S}", "DEF-upper_arm.{S}"),
    ("forearm_fk.{S}",   "DEF-forearm.{S}"),
    ("hand_fk.{S}",      "DEF-hand.{S}"),
]
ARM_IK_TARGET: tuple[str, str] = ("hand_ik.{S}", "DEF-hand.{S}")

# Leg FK / IK.
LEG_FK_PAIRS: list[tuple[str, str]] = [
    ("thigh_fk.{S}", "DEF-thigh.{S}"),
    ("shin_fk.{S}",  "DEF-shin.{S}"),
    ("foot_fk.{S}",  "DEF-foot.{S}"),
    ("toe_fk.{S}",   "DEF-toe.{S}"),
]
LEG_IK_TARGET: tuple[str, str] = ("foot_ik.{S}", "DEF-foot.{S}")
LEG_IK_TOE:    tuple[str, str] = ("toe_ik.{S}",  "DEF-toe.{S}")

# IK pole targets — pole position is computed geometrically per frame from
# the source upper/lower limb pair (perpendicular to the chord between the
# limb's root and tip, in the bend direction). No helper-bone retarget for
# these; they're handled via the bake's ``matrix_override`` callback.
ARM_POLE: tuple[str, str, str] = ("upper_arm_ik_target.{S}", "DEF-upper_arm.{S}", "DEF-forearm.{S}")
LEG_POLE: tuple[str, str, str] = ("thigh_ik_target.{S}",     "DEF-thigh.{S}",     "DEF-shin.{S}")


# ---------------------------------------------------------------------------
# Mapping builders
# ---------------------------------------------------------------------------

def _expand_side(pair: tuple[str, str], side: str) -> tuple[str, str]:
    return (pair[0].format(S=side), pair[1].format(S=side))


def _resolve_source(src_arm, def_name: str) -> str | None:
    """Resolve a DEF-style bone name to whatever the source rig actually uses.

    The bake supports two source-rig shapes:

    * A duplicate of the target control rig (the original
      ``_bake_to_control_rig`` path): bones keep their ``DEF-`` prefix, so
      ``DEF-spine`` resolves to ``DEF-spine``.
    * A Rigify **metarig** (the alternate workflow where the user generates
      motion onto the metarig directly): bones use the bare names that
      Rigify's ``rigify_generate`` derived the ``DEF-*`` bones from, so
      ``DEF-spine`` resolves to ``spine``, ``DEF-upper_arm.L`` to
      ``upper_arm.L``, etc.

    Returns the actual source bone name or ``None`` if neither variant
    exists on ``src_arm``.
    """
    if def_name in src_arm.pose.bones:
        return def_name
    bare = def_name[4:] if def_name.startswith("DEF-") else def_name
    if bare in src_arm.pose.bones:
        return bare
    return None


def _build_pairs(
    tar_arm,
    src_arm,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (rotation_pairs, translation_pairs).

    Each entry is ``(target_control_name, resolved_source_bone_name)``.
    The resolved source name may have either a ``DEF-`` prefix (control-rig
    duplicate source) or the bare metarig name — :func:`_resolve_source`
    picks whichever exists on ``src_arm``.

    Both FK and IK control sets are emitted for every limb regardless of
    the rig's current ``IK_FK`` switch state — see the ``Arms`` / ``Legs``
    blocks below for the rationale.
    """
    rotation_pairs: list[tuple[str, str]] = []
    translation_pairs: list[tuple[str, str]] = []

    def add_rot(target: str, def_source: str) -> None:
        if target not in tar_arm.pose.bones:
            return
        resolved = _resolve_source(src_arm, def_source)
        if resolved is not None:
            rotation_pairs.append((target, resolved))

    def add_loc(target: str, def_source: str) -> None:
        if target not in tar_arm.pose.bones:
            return
        resolved = _resolve_source(src_arm, def_source)
        if resolved is not None:
            translation_pairs.append((target, resolved))

    # Spine masters (rotation only).
    for target, source in SPINE_MASTER_PAIRS:
        add_rot(target, source)

    # Torso translation.
    target, source = TORSO_TRANSLATION_PAIR
    add_loc(target, source)

    # Shoulders.
    for target, source in SHOULDER_PAIRS:
        add_rot(target, source)

    # Arms — bake BOTH FK and IK regardless of current switch state, so the
    # user can flip ``upper_arm_parent.{S}["IK_FK"]`` at any frame and both
    # control sets stay in sync with the source motion. The active set drives
    # the deform chain at runtime; the inactive set rides along as data.
    for side in ("L", "R"):
        for pair in ARM_FK_PAIRS:
            t, s = _expand_side(pair, side)
            add_rot(t, s)
        t, s = _expand_side(ARM_IK_TARGET, side)
        add_rot(t, s)
        add_loc(t, s)

    # Legs — same dual-bake.
    for side in ("L", "R"):
        for pair in LEG_FK_PAIRS:
            t, s = _expand_side(pair, side)
            add_rot(t, s)
        t, s = _expand_side(LEG_IK_TARGET, side)
        add_rot(t, s)
        add_loc(t, s)
        t, s = _expand_side(LEG_IK_TOE, side)
        add_rot(t, s)

    return rotation_pairs, translation_pairs


# ---------------------------------------------------------------------------
# Helper bone construction + constraint setup
# ---------------------------------------------------------------------------

_HELPER_PREFIX = "_animatica_rigify_helper__"
_CONSTRAINT_TAG = "_animatica_rigify_retarget"


def _add_helpers_on_source(
    src_arm,
    tar_arm,
    *,
    rotation_pairs: list[tuple[str, str]],
    translation_pairs: list[tuple[str, str]],
) -> dict[str, str]:
    """Create one helper bone on ``src_arm`` per target control we plan to bake.

    The helper sits at the target control's REST world position+orientation
    (so it shares the target control's rest reference frame) and is parented
    to the corresponding source DEF bone (so it inherits the DEF's animation,
    with the helper's rest offset preserved).

    Returns ``{target_control_name: helper_bone_name}``.
    """
    helpers: dict[str, str] = {}

    # Union of rotation + translation pairs so each unique (target, source)
    # gets exactly one helper. The same helper drives both rotation and
    # location constraints (COPY_TRANSFORMS = both).
    seen: dict[str, str] = {}
    for target, source in rotation_pairs + translation_pairs:
        if target in seen:
            continue  # Already have a helper for this target
        seen[target] = source

    # Must enter EDIT mode on src_arm to create edit bones.
    _bake_common.select_only(src_arm)
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        ebones = src_arm.data.edit_bones
        for target, source in seen.items():
            tar_pb = tar_arm.pose.bones.get(target)
            if tar_pb is None:
                continue
            helper_name = _HELPER_PREFIX + target

            # Place helper at target control's REST world pose. We use the
            # target's matrix_local (rest in armature-local space) directly;
            # both rigs share matrix_world (src is a duplicate of tar), so
            # the same matrix_local positions the helper at the matching
            # world location.
            tar_rest_local = tar_pb.bone.matrix_local

            helper = ebones.new(helper_name)
            helper.head = Vector((0.0, 0.0, 0.0))
            helper.tail = Vector((0.0, 0.1, 0.0))
            helper.matrix = tar_rest_local
            parent_eb = ebones.get(source)
            if parent_eb is not None:
                helper.parent = parent_eb
                helper.use_connect = False

            helpers[target] = helper_name
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    return helpers


def _add_retarget_constraints(
    tar_arm,
    src_arm,
    helpers: dict[str, str],
    *,
    rotation_pairs: list[tuple[str, str]],
    translation_pairs: list[tuple[str, str]],
) -> set[str]:
    """Add temporary constraints on the target controls pointing at helpers
    on src_arm. Returns the set of target bone names that received constraints
    (= bones to bake).

    For pure rotation bones: COPY_ROTATION (world space).
    For pure translation bones (torso): COPY_LOCATION (world space).
    For bones with both (IK targets): COPY_TRANSFORMS (world space).
    """
    bones_to_bake: set[str] = set()

    rot_targets = {t for t, _ in rotation_pairs}
    loc_targets = {t for t, _ in translation_pairs}

    _bake_common.select_only(tar_arm)
    bpy.ops.object.mode_set(mode="POSE")

    for target in (rot_targets | loc_targets):
        helper_name = helpers.get(target)
        if helper_name is None:
            continue
        tpb = tar_arm.pose.bones.get(target)
        if tpb is None:
            continue

        wants_rot = target in rot_targets
        wants_loc = target in loc_targets

        if wants_rot and wants_loc:
            cns = tpb.constraints.new("COPY_TRANSFORMS")
            cns.name = _CONSTRAINT_TAG + "_xfm"
            cns.target = src_arm
            cns.subtarget = helper_name
        else:
            if wants_rot:
                cns = tpb.constraints.new("COPY_ROTATION")
                cns.name = _CONSTRAINT_TAG + "_rot"
                cns.target = src_arm
                cns.subtarget = helper_name
            if wants_loc:
                cns = tpb.constraints.new("COPY_LOCATION")
                cns.name = _CONSTRAINT_TAG + "_loc"
                cns.target = src_arm
                cns.subtarget = helper_name

        bones_to_bake.add(target)

    return bones_to_bake


def _teardown(tar_arm, src_arm, helpers: dict[str, str], bones_baked: set[str]) -> None:
    """Remove temp constraints from target controls and temp helper bones
    from source. Caller's source-rig cleanup (``mix_to_del`` marker) handles
    deletion of the full source object; this just makes sure target controls
    don't carry stale constraints if the source survives.
    """
    # Remove constraints on target controls.
    for name in bones_baked:
        pb = tar_arm.pose.bones.get(name)
        if pb is None:
            continue
        for c in list(pb.constraints):
            if c.name.startswith(_CONSTRAINT_TAG):
                pb.constraints.remove(c)

    # Remove helper bones from source (in case caller wants to keep src around).
    if not helpers:
        return
    try:
        _bake_common.select_only(src_arm)
        bpy.ops.object.mode_set(mode="EDIT")
        ebones = src_arm.data.edit_bones
        for helper_name in helpers.values():
            eb = ebones.get(helper_name)
            if eb is not None:
                ebones.remove(eb)
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        # If src is already getting cleaned up by the caller, this can fail
        # harmlessly — the helper bones go with the source object.
        pass


# ---------------------------------------------------------------------------
# IK pole-vector geometric placement
# ---------------------------------------------------------------------------

def _collect_pole_specs(tar_arm, src_arm) -> "list[tuple[str, str, str]]":
    """Return ``[(target_pole_bone, source_upper_bone, source_lower_bone), ...]``
    for every IK pole control that exists on the target and whose upper/lower
    limb counterparts exist on the source.
    """
    out: list[tuple[str, str, str]] = []
    for spec in (ARM_POLE, LEG_POLE):
        for side in ("L", "R"):
            tgt = spec[0].format(S=side)
            upper = _resolve_source(src_arm, spec[1].format(S=side))
            lower = _resolve_source(src_arm, spec[2].format(S=side))
            if tgt in tar_arm.pose.bones and upper and lower:
                out.append((tgt, upper, lower))
    return out


def _make_pole_override(src_arm, pole_specs: "list[tuple[str, str, str]]"):
    """Return a ``matrix_override`` callback for ``bake_control_bones`` that
    computes IK pole positions geometrically from the source's upper/lower
    limb pair at each frame.

    The pole sits *past* the elbow/knee in the direction the joint bends to —
    derived as the component of (elbow − chord_midpoint) along the bend
    plane, normalized and scaled by the lower bone's length. This is robust
    to bone-axis conventions (Mixamo vs Rigify vs ARP all differ) because it
    uses head/tail positions only.
    """
    pole_map = {tgt: (upper, lower) for tgt, upper, lower in pole_specs}
    if not pole_map:
        return None

    def override(pb, _current_matrix):
        names = pole_map.get(pb.name)
        if names is None:
            return None
        upper_name, lower_name = names
        upper = src_arm.pose.bones.get(upper_name)
        lower = src_arm.pose.bones.get(lower_name)
        if upper is None or lower is None:
            return None

        # Stay in source-armature-local space; the bake samples target.matrix
        # in target-armature pose space and converts to LOCAL via the target's
        # parent chain. Source and target share rest-pose alignment here, so
        # source's pose-local coords map directly into the target's pose-local
        # frame (modulo any armature-world offset between them, which is the
        # caller's responsibility to keep zero — same as the helper bones).
        head  = upper.head    # shoulder / hip
        elbow = lower.head    # joint (= upper.tail)
        tip   = lower.tail    # wrist / ankle

        chord = tip - head
        if chord.length < 1e-6:
            return None
        mid = head + chord * 0.5
        bend = elbow - mid
        if bend.length < 1e-4:
            # Limb is nearly straight — no defined bend direction. Skip
            # this frame; the pole will keep whatever pose it had.
            return None

        distance = (tip - elbow).length
        pole_pos = elbow + bend.normalized() * distance
        return Matrix.Translation(pole_pos)

    return override


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_anim_to_rigify(
    src_arm,
    tar_arm,
    *,
    action,
    frame_start: int,
    frame_end: int,
) -> int:
    """Bake the per-frame deform pose of ``src_arm`` onto the control bones
    of ``tar_arm`` (a Rigify control rig) and write the result into ``action``.

    Strategy: add helper bones on ``src_arm`` at target controls' rest
    positions parented to their corresponding source DEF bones, then add
    ``COPY_TRANSFORMS``/``COPY_ROTATION``/``COPY_LOCATION`` constraints on
    target controls pointing at these helpers, then run the standard
    bake. See module docstring for details.

    Returns the number of control bones that received keyframes.
    """
    # Mark the source for cleanup ONLY if it's a temporary duplicate (the
    # ``_bake_to_control_rig`` flow stamps this marker on the duplicate it
    # creates so its cleanup loop can sweep it up). When the caller passes
    # a user-owned source — e.g. a Rigify metarig that should persist —
    # don't touch the marker. Detection: a temp-duplicate source carries
    # ``animatica_temp_source`` from the ``_bake_to_control_rig`` setup.
    if src_arm.get("animatica_temp_source"):
        src_arm["mix_to_del"] = True

    # Enable pole-vector IK on every limb. Default Rigify rigs ship with
    # ``pole_vector=False``: the IK chain is controlled by rotating
    # ``thigh_ik.L``/``upper_arm_ik.L`` instead of by a pole bone. We bake
    # the pole bones with geometric placement, so the pole-using IK
    # constraint variant has to be the active one for our keyframes to
    # actually steer the elbow/knee at playback. Toggling this also
    # un-mutes the ``IK.001`` constraint on the underlying ``MCH-*_ik``
    # bone via Rigify's driver setup.
    for parent_name in ("upper_arm_parent.L", "upper_arm_parent.R",
                        "thigh_parent.L", "thigh_parent.R"):
        ppb = tar_arm.pose.bones.get(parent_name)
        if ppb is not None and "pole_vector" in ppb.keys():
            ppb["pole_vector"] = True
    tar_arm.update_tag(refresh={"OBJECT"})
    bpy.context.evaluated_depsgraph_get().update()

    rotation_pairs, translation_pairs = _build_pairs(tar_arm, src_arm)

    if not rotation_pairs and not translation_pairs:
        print("[animatica] rigify_bake: no matching control bones found — "
              "is this a standard Rigify human metarig?")
        return 0

    # Step 1: create helper bones on source rig at target control rest poses.
    helpers = _add_helpers_on_source(
        src_arm, tar_arm,
        rotation_pairs=rotation_pairs,
        translation_pairs=translation_pairs,
    )

    # Step 2: add retarget constraints on target controls.
    bones_to_bake = _add_retarget_constraints(
        tar_arm, src_arm, helpers,
        rotation_pairs=rotation_pairs,
        translation_pairs=translation_pairs,
    )

    if not bones_to_bake:
        _teardown(tar_arm, src_arm, helpers, bones_to_bake)
        return 0

    # Step 2b: also bake IK pole bones (computed geometrically per frame).
    pole_specs = _collect_pole_specs(tar_arm, src_arm)
    for target_name, _, _ in pole_specs:
        bones_to_bake.add(target_name)

    # Step 3: select only the bones we're baking and bake. The bake's
    # POSE → LOCAL conversion uses the depsgraph-evaluated parent chain,
    # which composes correctly through Rigify's MCH/ORG bones.
    for pb in tar_arm.pose.bones:
        blender_compat.pose_bone_select_set(pb, pb.name in bones_to_bake)

    try:
        baked = _bake_common.bake_control_bones(
            tar_arm,
            action=action,
            frame_start=int(frame_start),
            frame_end=int(frame_end),
            only_selected=True,
            matrix_override=_make_pole_override(src_arm, pole_specs),
        )
    finally:
        _teardown(tar_arm, src_arm, helpers, bones_to_bake)

    # Step 4: torso position correction.
    #
    # The helper-bone bake puts torso at the WORLD position of the source
    # spine's head, but DEF-spine on the target is not co-located with
    # torso — Rigify's spine_fk → MCH-spine → tweak_spine → DEF-spine
    # chain inserts MCH bones whose rest offsets get rotated as the FK
    # chain rotates, shifting DEF-spine away from torso under motion.
    # The drift is dominated by a single rigid translation (every DEF
    # head moves by the same amount), so a per-frame correction on
    # torso.location pulls the whole rig back into alignment.
    _correct_torso_translation(
        src_arm, tar_arm,
        action=action,
        frame_start=int(frame_start),
        frame_end=int(frame_end),
    )

    return baked


def _correct_torso_translation(
    src_arm,
    tar_arm,
    *,
    action,
    frame_start: int,
    frame_end: int,
) -> None:
    """Adjust torso.location keyframes so DEF-spine lands on the source spine.

    Walks ``[frame_start, frame_end]``, measures the world-space delta
    between ``tar_arm.DEF-spine.head`` and the source's spine head, and
    adds that delta (transformed into torso's parent local frame) to
    torso's location keyframe for the frame.
    """
    tar_def_pb = tar_arm.pose.bones.get("DEF-spine")
    torso_pb = tar_arm.pose.bones.get("torso")
    src_root_name = _resolve_source(src_arm, "DEF-spine")
    if tar_def_pb is None or torso_pb is None or src_root_name is None:
        return
    src_pb = src_arm.pose.bones.get(src_root_name)
    if src_pb is None:
        return

    fc_x = _bake_common.ensure_fcurve(action, tar_arm, 'pose.bones["torso"].location', 0)
    fc_y = _bake_common.ensure_fcurve(action, tar_arm, 'pose.bones["torso"].location', 1)
    fc_z = _bake_common.ensure_fcurve(action, tar_arm, 'pose.bones["torso"].location', 2)
    if fc_x is None or fc_y is None or fc_z is None:
        return

    scn = bpy.context.scene
    saved_frame = scn.frame_current
    try:
        for f in range(frame_start, frame_end + 1):
            scn.frame_set(f)
            bpy.context.view_layer.update()

            rh = tar_arm.matrix_world @ tar_def_pb.head
            mh = src_arm.matrix_world @ src_pb.head
            delta_world = mh - rh

            parent_pose = (
                torso_pb.parent.matrix if torso_pb.parent else Matrix.Identity(4)
            )
            delta_local = parent_pose.to_3x3().inverted() @ delta_world

            for fc, dv in ((fc_x, delta_local.x), (fc_y, delta_local.y), (fc_z, delta_local.z)):
                for kp in fc.keyframe_points:
                    if int(kp.co[0]) == f:
                        kp.co = (f, kp.co[1] + dv)
                        kp.interpolation = "LINEAR"
                        break
    finally:
        scn.frame_set(saved_frame)

    for fc in (fc_x, fc_y, fc_z):
        try:
            fc.update()
        except Exception:
            pass
