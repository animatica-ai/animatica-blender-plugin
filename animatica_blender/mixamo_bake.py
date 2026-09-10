"""Bake deform-bone animation onto a Mixamo Rig control armature.

Ported from the Mixamo Rig addon's ``mr.import_anim_to_rig`` operator
(``mixamo_rig.py:_import_anim`` and ``lib/animation.py:bake_anim``) so we
own the action handling. The addon's operator always calls
``bpy.data.actions.new("Action")``, which clobbers the action name we
want and rules out single-frame use (the operator bakes the action's
full frame range). Owning the bake lets us:

  * Keep our action name (e.g. ``Animatica_Motion: <prompt>``).
  * Take an explicit ``frame_start``/``frame_end`` — works for the full
    multi-frame generate path *and* the single-frame text-to-pose path.
  * Skip the addon's ``redefine_source_rest_pose`` step. The addon needs
    it because its source armature (raw Mixamo FBX) has different rest
    rolls than the user's character. Our source is a duplicate of the
    user's own armature, so the deform rest pose already matches.

The high-level flow mirrors ``_import_anim``:
  1. Detect IK/FK switch state on each limb.
  2. Build a source-bone → control-bone name map (FK chains rotate from
     mixamorig deform bones; IK chains rotate from helper bones we
     create on the source that follow the deform IK chain via
     ``COPY_TRANSFORMS``).
  3. Add helper edit bones on the source armature for IK targets and
     IK chain tips, so pole positions can be derived geometrically and
     IK target chains can be matrix-baked.
  4. Add retarget constraints on the target's control bones (mostly
     ``COPY_ROTATION``, plus ``COPY_LOCATION`` on Hips and IK
     targets).
  5. Bake selected control bones over the requested frame range into
     the supplied action.
  6. Tear down all temp constraints and helper bones.

Public entry: :func:`apply_anim_to_control_rig`.
"""

from __future__ import annotations

import bpy
from mathutils import Matrix, Vector

from . import _bake_common, blender_compat


# ---------------------------------------------------------------------------
# Constants — pulled from the Mixamo addon's definitions/naming.py.
# Duplicated here so this module doesn't depend on the addon being
# importable (it is at runtime, but the addon source isn't on Python's
# import path; only its operators are exposed via bpy.ops.mr.*).
# ---------------------------------------------------------------------------

C_PREFIX = "Ctrl_"

ARM_NAMES = {
    "shoulder": "Shoulder",
    "arm_ik":   "Arm_IK",
    "arm_fk":   "Arm_FK",
    "forearm_ik": "ForeArm_IK",
    "forearm_fk": "ForeArm_FK",
    "pole_ik":  "ArmPole_IK",
    "hand_ik":  "Hand_IK",
    "hand_fk":  "Hand_FK",
}

LEG_NAMES = {
    "thigh_ik": "UpLeg_IK",
    "thigh_fk": "UpLeg_FK",
    "calf_ik":  "Leg_IK",
    "calf_fk":  "Leg_FK",
    "foot_fk":  "Foot_FK",
    "foot_ik":  "Foot_IK",
    "pole_ik":  "LegPole_IK",
}


# ---------------------------------------------------------------------------
# Shared helpers — bake loop, action/fcurve plumbing, select-without-3D-view,
# and pole-vector geometry live in ``_bake_common`` so the Rigify baker can
# reuse them. Re-export with the old underscore-prefixed names so existing
# call sites in this module (and in ``gltf_to_blender``) keep working.
# ---------------------------------------------------------------------------

_project_point_onto_plane = _bake_common.project_point_onto_plane
_get_ik_pole_pos          = _bake_common.get_ik_pole_pos
_has_slotted_actions      = _bake_common.has_slotted_actions
_ensure_action_slot       = _bake_common.ensure_action_slot
_action_fcurves           = _bake_common.action_fcurves
_ensure_fcurve            = _bake_common.ensure_fcurve


# ---------------------------------------------------------------------------
# Mixamo-specific matrix override: handles Ctrl_*Pole_* bones whose pose
# matrix is the geometric pole position derived from the source IK chain,
# plus the CHILD_OF compensation those poles need to round-trip at playback.
# ---------------------------------------------------------------------------

def _make_pole_matrix_override(armature, ik_data: dict):
    """Return a matrix_override callback for ``_bake_common.bake_control_bones``
    that substitutes geometric pole positions for Ctrl_*Pole_* bones and
    pre-inverts the CHILD_OF parent so the constraint round-trips at playback.
    """
    src_arm = ik_data.get("src_arm")

    def override(pb, _current_matrix):
        if not (pb.name.startswith("Ctrl_ArmPole") or pb.name.startswith("Ctrl_LegPole")):
            return None
        if src_arm is None:
            return None

        kind = "Leg" if "Leg" in pb.name else ("Arm" if "Arm" in pb.name else "")
        side = pb.name.split("_")[-1]
        if not kind or kind + side not in ik_data:
            return None

        b1_name, b2_name = ik_data[kind + side]
        b1 = src_arm.pose.bones.get(b1_name)
        b2 = src_arm.pose.bones.get(b2_name)
        if b1 is None or b2 is None:
            return None

        if kind == "Leg":
            axis = (b1.z_axis * 0.5) + (b2.z_axis * 0.5)
        else:
            axis = b2.x_axis if side == "Left" else -b2.x_axis

        try:
            bmat = Matrix.Translation(_get_ik_pole_pos(b1, b2, axis))
        except AttributeError:
            return None

        # CHILD_OF compensation — the pole control inherits from an IK chain
        # bone via Child Of. The constraint re-applies at playback, so we
        # pre-divide it out of the matrix we store.
        co = pb.constraints.get("Child Of")
        if co and co.subtarget and co.influence == 1.0 and not co.mute:
            sb = armature.pose.bones.get(co.subtarget)
            if sb is not None:
                bmat = sb.matrix_channel.inverted() @ bmat
        return bmat

    return override


def _bake_control_bones(
    armature,
    *,
    action,
    frame_start: int,
    frame_end: int,
    only_selected: bool,
    ik_data: dict,
) -> int:
    """Bake selected control bones of ``armature`` into ``action`` over the
    given frame range. Wraps the shared bake loop with a Mixamo-specific
    pole-vector override (geometric pole pos + CHILD_OF compensation).
    """
    return _bake_common.bake_control_bones(
        armature,
        action=action,
        frame_start=frame_start,
        frame_end=frame_end,
        only_selected=only_selected,
        matrix_override=_make_pole_matrix_override(armature, ik_data),
    )


# ---------------------------------------------------------------------------
# Setup helpers — port of the matrix-collection / helper-bone / retarget-
# constraint logic from ``_import_anim``. These run on a *prepared* source
# armature: a deform-only duplicate of the user's rig with its Copy*/IK
# constraints muted. The caller is responsible for that setup (it's already
# done in ``gltf_to_blender._bake_to_control_rig``).
# ---------------------------------------------------------------------------

def _detect_mixamo_prefix(arm) -> tuple[bool, str]:
    """Return (use_prefix, prefix) — ``("mixamorig:",)`` for typical
    Mixamo-named rigs, ``("",)`` for raw control-rig deform bones (some
    workflows keep the bare ``Hips``/``Spine``/``LeftArm`` names).
    """
    for b in arm.data.bones:
        if b.name.startswith("mixamorig") and ":" in b.name:
            return True, b.name.split(":")[0] + ":"
    return False, ""


def _build_bones_map(
    src_prefix: str,
    *,
    arm_left_kin: str,
    arm_right_kin: str,
    leg_left_kin: str,
    leg_right_kin: str,
) -> dict[str, str]:
    """Source-bone-name → target-control-bone-name. FK chains are driven
    by the equivalent mixamo deform bone (rotation only). IK chains are
    driven by helper bones we add to the source rig at the IK target /
    pole positions (those names equal the target control name; matched
    1:1 below).
    """
    def s(n):  # source name
        return src_prefix + n

    m: dict[str, str] = {}

    # Spine + head
    m[s("Hips")]   = C_PREFIX + "Hips"
    m[s("Spine")]  = C_PREFIX + "Spine"
    m[s("Spine1")] = C_PREFIX + "Spine1"
    m[s("Spine2")] = C_PREFIX + "Spine2"
    m[s("Neck")]   = C_PREFIX + "Neck"
    m[s("Head")]   = C_PREFIX + "Head"
    m[s("LeftShoulder")]  = C_PREFIX + "Shoulder_Left"
    m[s("RightShoulder")] = C_PREFIX + "Shoulder_Right"

    # Arms
    if arm_left_kin == "FK":
        m[s("LeftArm")]     = C_PREFIX + "Arm_FK_Left"
        m[s("LeftForeArm")] = C_PREFIX + "ForeArm_FK_Left"
        m[s("LeftHand")]    = C_PREFIX + "Hand_FK_Left"
    else:
        m[C_PREFIX + "Hand_IK_Left"] = C_PREFIX + "Hand_IK_Left"
    if arm_right_kin == "FK":
        m[s("RightArm")]     = C_PREFIX + "Arm_FK_Right"
        m[s("RightForeArm")] = C_PREFIX + "ForeArm_FK_Right"
        m[s("RightHand")]    = C_PREFIX + "Hand_FK_Right"
    else:
        m[C_PREFIX + "Hand_IK_Right"] = C_PREFIX + "Hand_IK_Right"

    # Fingers
    for side, side_short in (("Left", "Left"), ("Right", "Right")):
        for finger in ("Thumb", "Index", "Middle", "Ring", "Pinky"):
            for j in (1, 2, 3):
                m[s(f"{side}Hand{finger}{j}")] = C_PREFIX + f"{finger}{j}_{side_short}"

    # Legs
    if leg_left_kin == "FK":
        m[s("LeftUpLeg")]   = C_PREFIX + "UpLeg_FK_Left"
        m[s("LeftLeg")]     = C_PREFIX + "Leg_FK_Left"
        m[C_PREFIX + "Foot_FK_Left"] = C_PREFIX + "Foot_FK_Left"
        m[s("LeftToeBase")] = C_PREFIX + "Toe_FK_Left"
    else:
        m[C_PREFIX + "Foot_IK_Left"] = C_PREFIX + "Foot_IK_Left"
        m[s("LeftToeBase")] = C_PREFIX + "Toe_IK_Left"
    if leg_right_kin == "FK":
        m[s("RightUpLeg")]   = C_PREFIX + "UpLeg_FK_Right"
        m[s("RightLeg")]     = C_PREFIX + "Leg_FK_Right"
        m[C_PREFIX + "Foot_FK_Right"] = C_PREFIX + "Foot_FK_Right"
        m[s("RightToeBase")] = C_PREFIX + "Toe_FK_Right"
    else:
        m[C_PREFIX + "Foot_IK_Right"] = C_PREFIX + "Foot_IK_Right"
        m[s("RightToeBase")] = C_PREFIX + "Toe_IK_Right"

    return m


_select_only = _bake_common.select_only


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def apply_anim_to_control_rig(
    src_arm,
    tar_arm,
    *,
    action,
    frame_start: int,
    frame_end: int,
) -> int:
    """Bake the per-frame deform pose of ``src_arm`` onto the control
    bones of ``tar_arm`` and write the result into ``action``.

    ``src_arm`` is expected to be a *prepared* source: deform-only
    skeleton with mixamorig (or unprefixed) bone names and an animation
    that resolves on its own pose bones — i.e. already-baked keyframes,
    or a constraint chain that doesn't depend on the bones we strip.
    The caller (``gltf_to_blender._bake_to_control_rig``) builds
    this from a duplicate of ``tar_arm`` with control bones removed and
    the deform bones' Copy*/IK constraints muted.

    ``action`` is the action that receives the new control-bone fcurves.
    Existing keyframes on it are preserved except for those at frames
    inside ``[frame_start, frame_end]`` on the same fcurves we're
    overwriting (see ``_bake_control_bones``).

    Returns the number of control bones that received keyframes.
    """
    use_prefix, prefix = _detect_mixamo_prefix(src_arm)

    # Mark source for cleanup (callers also tag with "animatica_temp_source",
    # but the cleanup loop tolerates either marker).
    src_arm["mix_to_del"] = True

    def s(n):  # source bone name
        return prefix + n if use_prefix else n

    # --- IK/FK switch state from existing control bone properties ---
    def _ik_state(bone_name: str) -> str:
        pb = tar_arm.pose.bones.get(bone_name)
        if pb is None:
            return "FK"
        try:
            return "IK" if pb["ik_fk_switch"] < 0.5 else "FK"
        except (KeyError, TypeError):
            return "FK"

    arm_left_kin  = _ik_state(C_PREFIX + ARM_NAMES["hand_ik"] + "_Left")
    arm_right_kin = _ik_state(C_PREFIX + ARM_NAMES["hand_ik"] + "_Right")
    leg_left_kin  = _ik_state(C_PREFIX + LEG_NAMES["foot_ik"] + "_Left")
    leg_right_kin = _ik_state(C_PREFIX + LEG_NAMES["foot_ik"] + "_Right")

    bones_map = _build_bones_map(
        prefix if use_prefix else "",
        arm_left_kin=arm_left_kin,
        arm_right_kin=arm_right_kin,
        leg_left_kin=leg_left_kin,
        leg_right_kin=leg_right_kin,
    )

    # --- Collect target rest-pose data (helper-bone matrices, IK chains) ---
    _select_only(tar_arm)
    bpy.ops.object.mode_set(mode="EDIT")

    ctrl_matrices: dict[str, tuple[Matrix, str]] = {}
    ik_bones_data: dict[str, tuple[str, str, dict[str, tuple]]] = {}

    kinematics = {
        "HandLeft":  ("Hand", arm_left_kin,  "Left"),
        "HandRight": ("Hand", arm_right_kin, "Right"),
        "FootLeft":  ("Foot", leg_left_kin,  "Left"),
        "FootRight": ("Foot", leg_right_kin, "Right"),
    }
    for slot_id, (kind, kin, side) in kinematics.items():
        ctrl_name = C_PREFIX + kind + "_" + kin + "_" + side
        ctrl_eb = tar_arm.data.edit_bones.get(ctrl_name)
        if ctrl_eb is None:
            continue
        mix_bone_name = s(side + kind)  # e.g. mixamorig:LeftHand
        ctrl_matrices[ctrl_name] = (ctrl_eb.matrix.copy(), mix_bone_name)

        if kin == "IK":
            chain_names = (["UpLeg_IK_" + side, "Leg_IK_" + side]
                           if kind == "Foot"
                           else ["Arm_IK_" + side, "ForeArm_IK_" + side])
            ik1 = tar_arm.data.edit_bones.get(chain_names[0])
            ik2 = tar_arm.data.edit_bones.get(chain_names[1])
            if ik1 is None or ik2 is None:
                continue
            ik_bones_data[slot_id] = (
                kind,
                side,
                {
                    "ik1": (ik1.name, ik1.head.copy(), ik1.tail.copy(), ik1.roll),
                    "ik2": (ik2.name, ik2.head.copy(), ik2.tail.copy(), ik2.roll),
                },
            )

    # --- Source: apply transforms (rotation+scale) and rescale location curves ---
    bpy.ops.object.mode_set(mode="OBJECT")
    _select_only(src_arm)
    bpy.context.view_layer.update()

    scale_fac = src_arm.scale[0]
    try:
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        bpy.context.evaluated_depsgraph_get().update()
    except Exception:
        pass

    if scale_fac != 1.0 and src_arm.animation_data and src_arm.animation_data.action:
        src_action = src_arm.animation_data.action
        src_fcurves = _action_fcurves(src_action, src_arm)
        if src_fcurves is not None:
            for fc in src_fcurves:
                dp = fc.data_path
                if dp.startswith("pose.bones") and dp.endswith(".location"):
                    for k in fc.keyframe_points:
                        k.co[1] *= scale_fac

    # --- Add helper bones on source (IK target shadows + IK chain shadows) ---
    bpy.ops.object.mode_set(mode="EDIT")
    eb = src_arm.data.edit_bones
    for ctrl_name, (mat, parent_name) in ctrl_matrices.items():
        helper = eb.new(ctrl_name)
        helper.head = Vector((0.0, 0.0, 0.0))
        helper.tail = Vector((0.0, 0.0, 0.1))
        helper.matrix = mat
        parent = eb.get(parent_name)
        if parent is not None:
            helper.parent = parent

    for slot_id, (kind, side, ikb) in ik_bones_data.items():
        for key in ("ik1", "ik2"):
            bname, bhead, btail, broll = ikb[key]
            if bname in eb:
                continue
            helper = eb.new(bname)
            helper.head = bhead
            helper.tail = btail
            helper.roll = broll

    # --- Add COPY_TRANSFORMS on IK helpers so they follow the deform chain ---
    bpy.ops.object.mode_set(mode="POSE")
    bake_ik_data: dict = {"src_arm": src_arm}

    for slot_id, (kind, side, ikb) in ik_bones_data.items():
        b1_name = ikb["ik1"][0]
        b2_name = ikb["ik2"][0]
        b1_pb = src_arm.pose.bones.get(b1_name)
        b2_pb = src_arm.pose.bones.get(b2_name)
        if b1_pb is None or b2_pb is None:
            continue

        if kind == "Foot":
            chain = (s(side + "UpLeg"), s(side + "Leg"))
            bake_ik_data["Leg" + side] = chain
        else:  # Hand
            chain = (s(side + "Arm"), s(side + "ForeArm"))
            bake_ik_data["Arm" + side] = chain

        for pb, sub in ((b1_pb, chain[0]), (b2_pb, chain[1])):
            cns = pb.constraints.new("COPY_TRANSFORMS")
            cns.name = "Copy Transforms"
            cns.target = src_arm
            cns.subtarget = sub

    # --- Add retarget constraints on target's control bones ---
    _select_only(tar_arm)
    bpy.ops.object.mode_set(mode="POSE")
    # Direct deselect instead of ``bpy.ops.pose.select_all`` so we don't
    # need a 3D-View area in the context (modal/timer paths lack one).
    for pb in tar_arm.pose.bones:
        blender_compat.pose_bone_select_set(pb, False)
    bpy.context.view_layer.update()

    for src_name, tar_name in bones_map.items():
        src_pb = src_arm.pose.bones.get(src_name)
        tar_pb = tar_arm.pose.bones.get(tar_name)
        if src_pb is None or tar_pb is None:
            continue

        cns = tar_pb.constraints.new("COPY_ROTATION")
        cns.name = "Copy Rotation_retarget"
        cns.target = src_arm
        cns.subtarget = src_name

        if "Hips" in src_name:
            cns = tar_pb.constraints.new("COPY_LOCATION")
            cns.name = "Copy Location_retarget"
            cns.target = src_arm
            cns.subtarget = src_name
            cns.owner_space = cns.target_space = "LOCAL"

        is_ik_target = (
            (leg_left_kin  == "IK" and "Foot_IK_Left"  in src_name)
            or (leg_right_kin == "IK" and "Foot_IK_Right" in src_name)
            or (arm_left_kin  == "IK" and "Hand_IK_Left"  in src_name)
            or (arm_right_kin == "IK" and "Hand_IK_Right" in src_name)
        )
        if is_ik_target:
            cns = tar_pb.constraints.new("COPY_LOCATION")
            cns.name = "Copy Location_retarget"
            cns.target = src_arm
            cns.subtarget = src_name
            cns.target_space = cns.owner_space = "POSE"

            side_suffix = "_Left" if "Left" in src_name else "_Right"
            pole_kind = ARM_NAMES["pole_ik"] if "Hand" in src_name else LEG_NAMES["pole_ik"]
            pole_name = C_PREFIX + pole_kind + side_suffix
            pole_pb = tar_arm.pose.bones.get(pole_name)
            if pole_pb is not None:
                tar_arm.data.bones.active = pole_pb.bone
                blender_compat.pose_bone_select_set(pole_pb, True)

        tar_arm.data.bones.active = tar_pb.bone
        blender_compat.pose_bone_select_set(tar_pb, True)

    bpy.context.view_layer.update()

    # --- Bake into the supplied action ---
    baked = _bake_control_bones(
        tar_arm,
        action=action,
        frame_start=frame_start,
        frame_end=frame_end,
        only_selected=True,
        ik_data=bake_ik_data,
    )

    # --- Tear down retarget constraints ---
    for tar_name in set(bones_map.values()):
        pb = tar_arm.pose.bones.get(tar_name)
        if pb is None:
            continue
        for c in list(pb.constraints):
            if c.name.endswith("_retarget"):
                pb.constraints.remove(c)
    for slot_id in ik_bones_data:
        side_suffix = "_Left" if "Left" in slot_id else "_Right"
        pole_kind = ARM_NAMES["pole_ik"] if "Hand" in slot_id else LEG_NAMES["pole_ik"]
        # No retarget constraint on poles, but they were left selected.
        pole_pb = tar_arm.pose.bones.get(C_PREFIX + pole_kind + side_suffix)
        if pole_pb is not None:
            blender_compat.pose_bone_select_set(pole_pb, False)

    return baked
