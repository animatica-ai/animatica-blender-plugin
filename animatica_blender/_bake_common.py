"""Rig-agnostic helpers shared by the Mixamo and Rigify control-rig bakers.

Each control rig (Mixamo Rig, Rigify, future ARP, etc.) needs the same per-frame
"sample pose matrix → write fcurves" loop, the same slotted-action shim for
Blender 4.4+/5.0, and the same select-without-3D-view utilities. Pulling them
here keeps the rig-specific modules focused on what's actually rig-specific:
the bone name map, IK/FK switch detection, and pole-vector geometry.

The bake loop accepts a ``matrix_override`` callback that lets callers
substitute the pose-space matrix for specific bones (used for IK pole vectors,
which can't be derived from a single source bone — they're computed
geometrically from the IK chain).
"""

from __future__ import annotations

from typing import Callable

import bpy
from mathutils import Matrix, Vector

from . import blender_compat


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def project_point_onto_plane(q: Vector, p: Vector, n: Vector) -> Vector:
    n = n.normalized()
    return q - ((q - p).dot(n)) * n


def get_ik_pole_pos(b1, b2, axis: Vector) -> Vector:
    """Pole sits along ``axis`` from ``b2.head``, distance equal to the
    lower bone's length. Stable across frames and matches the conventions
    of Mixamo's Ctrl_*Pole_* and Rigify's *_ik_target controls.
    """
    return b2.head + (axis.normalized() * (b2.tail - b2.head).magnitude)


# ---------------------------------------------------------------------------
# Slotted-action compatibility (Blender 4.4+ / 5.0)
# ---------------------------------------------------------------------------

def has_slotted_actions() -> bool:
    return bpy.app.version >= (4, 4, 0)


def ensure_action_slot(anim_data, action, datablock) -> None:
    """Bind a slot to ``anim_data`` for ``datablock`` if one isn't bound yet.

    In Blender 4.4+, an action without an assigned slot resolves to no
    fcurves at evaluation time. ``anim_data.action = X`` used to create
    one implicitly; now you have to pick from ``action_suitable_slots``
    or trigger creation via ``fcurve_ensure_for_datablock``.
    """
    if not has_slotted_actions():
        return
    if not hasattr(anim_data, "action_slot"):
        return
    try:
        if anim_data.action_slot is not None:
            return
        suitable = getattr(anim_data, "action_suitable_slots", None)
        if suitable and len(suitable) > 0:
            anim_data.action_slot = suitable[0]
            return
        if datablock is not None and hasattr(action, "fcurve_ensure_for_datablock"):
            data_path = "location"
            if (hasattr(datablock, "pose")
                    and hasattr(datablock.pose, "bones")
                    and len(datablock.pose.bones) > 0):
                data_path = (
                    f'pose.bones["{datablock.pose.bones[0].name}"].rotation_euler'
                )
            action.fcurve_ensure_for_datablock(datablock, data_path, index=0)
    except Exception:
        pass


def action_fcurves(action, datablock):
    """Return the fcurve collection for ``action``, traversing the layered
    action API on Blender 5.0+ where ``action.fcurves`` was removed.
    """
    if action is None:
        return None
    if bpy.app.version >= (5, 0, 0):
        if not hasattr(action, "layers") or len(action.layers) == 0:
            if datablock is not None and hasattr(action, "fcurve_ensure_for_datablock"):
                try:
                    bone_name = datablock.pose.bones[0].name
                    action.fcurve_ensure_for_datablock(
                        datablock, f'pose.bones["{bone_name}"].rotation_euler', index=0
                    )
                except Exception:
                    return None
        layer = action.layers[0]
        if not hasattr(layer, "strips") or len(layer.strips) == 0:
            return None
        strip = layer.strips[0]
        if not hasattr(strip, "channelbag"):
            return None
        slot = action.slots[0] if (hasattr(action, "slots") and len(action.slots) > 0) else None
        if slot is None:
            return None
        try:
            cbag = strip.channelbag(slot)
            if cbag is not None and hasattr(cbag, "fcurves"):
                return cbag.fcurves
        except Exception:
            return None
        return None
    return getattr(action, "fcurves", None)


def ensure_fcurve(action, datablock, data_path: str, index: int):
    """Find or create the fcurve for ``data_path``/``index`` on ``action``."""
    if hasattr(action, "fcurve_ensure_for_datablock"):
        try:
            return action.fcurve_ensure_for_datablock(datablock, data_path, index=index)
        except Exception:
            pass
    fcurves = action_fcurves(action, datablock)
    if fcurves is None:
        return None
    for fc in fcurves:
        if fc.data_path == data_path and fc.array_index == index:
            return fc
    if hasattr(fcurves, "new"):
        try:
            return fcurves.new(data_path, index=index)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Select / activate without a 3D-View context
# ---------------------------------------------------------------------------

def select_only(obj) -> None:
    """Make ``obj`` the only selected + active object.

    Uses direct API rather than ``bpy.ops.object.select_all(...)`` so the
    call works from contexts without a 3D-View area (modal timers,
    background threads).
    """
    for o in bpy.context.view_layer.objects:
        try:
            o.select_set(False)
        except Exception:
            pass
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


# ---------------------------------------------------------------------------
# The bake loop
# ---------------------------------------------------------------------------

# Signature: matrix_override(pose_bone, current_pose_matrix) -> Matrix | None.
# Return a replacement matrix to substitute, or None to keep the original.
MatrixOverride = Callable[["bpy.types.PoseBone", "Matrix"], "Matrix | None"]


def bake_control_bones(
    armature,
    *,
    action,
    frame_start: int,
    frame_end: int,
    only_selected: bool,
    matrix_override: MatrixOverride | None = None,
) -> int:
    """Sample each (selected) pose bone's local matrix at every integer frame
    in ``[frame_start, frame_end]`` and write rotation/location/scale fcurves
    for it into ``action``. Returns the number of bones that contributed at
    least one keyframe.

    If ``matrix_override`` is supplied, it's called per (bone, frame) before
    the POSE→LOCAL conversion; returning a Matrix substitutes that matrix
    (used for IK pole vectors computed geometrically; the caller's override
    handles whatever CHILD_OF / parent compensation is needed for that bone).

    Existing keyframes inside ``[frame_start, frame_end]`` on the same
    fcurves are overwritten; existing keyframes at other frames are preserved.
    """
    bones_data = _collect_pose_matrices(
        armature,
        frame_start=frame_start,
        frame_end=frame_end,
        only_selected=only_selected,
        matrix_override=matrix_override,
    )
    return write_keyframes_from_bone_data(
        armature, action, bones_data, only_selected=only_selected
    )


def _collect_pose_matrices(
    armature,
    *,
    frame_start: int,
    frame_end: int,
    only_selected: bool,
    matrix_override: MatrixOverride | None,
) -> "list[tuple[int, dict[str, Matrix]]]":
    """Sample per-frame pose matrices for each (selected) bone, run them
    through ``matrix_override``, convert POSE → LOCAL, and return the
    collected data ready for ``write_keyframes_from_bone_data``.

    The POSE → LOCAL conversion uses each bone's *current* parent pose at
    the moment of sampling, which is correct when the parent chain is
    driven by Blender constraints (the Mixamo bake path). Callers that
    need rest-aware retargeting through a chain that constraints can't
    express should compute LOCAL matrices themselves and call
    ``write_keyframes_from_bone_data`` directly.
    """
    scn = bpy.context.scene
    bones_data: list[tuple[int, dict[str, Matrix]]] = []

    def _is_selected(pb) -> bool:
        return blender_compat.pose_bone_is_selected(pb)

    def _get_bones_matrix() -> dict[str, Matrix]:
        m: dict[str, Matrix] = {}
        for pb in armature.pose.bones:
            if only_selected and not _is_selected(pb):
                continue
            bmat = pb.matrix
            if matrix_override is not None:
                replacement = matrix_override(pb, bmat)
                if replacement is not None:
                    bmat = replacement
            m[pb.name] = armature.convert_space(
                pose_bone=pb, matrix=bmat, from_space="POSE", to_space="LOCAL"
            )
        return m

    saved_frame = scn.frame_current
    for f in range(int(frame_start), int(frame_end) + 1):
        scn.frame_set(f)
        bpy.context.view_layer.update()
        bones_data.append((f, _get_bones_matrix()))
    scn.frame_set(saved_frame)
    return bones_data


def write_keyframes_from_bone_data(
    armature,
    action,
    bones_data: "list[tuple[int, dict[str, Matrix]]]",
    *,
    only_selected: bool,
) -> int:
    """Write the per-frame LOCAL matrices in ``bones_data`` into ``action``
    as ``pose.bones["X"].location/rotation_*/scale`` fcurves.

    ``bones_data`` is a list of ``(frame, {bone_name: local_matrix})`` entries
    — the same shape ``_collect_pose_matrices`` produces, and what custom
    direct-write retargeters (e.g. ``rigify_bake``) should hand off here.

    Existing keyframes inside the bake frame range on each touched fcurve
    are removed before insertion so the new values land cleanly; keys at
    other frames on the same fcurve are preserved.

    Returns the number of bones that contributed at least one keyframe.
    """
    if armature.animation_data is None:
        armature.animation_data_create()
    if armature.animation_data.action is not action:
        armature.animation_data.action = action
    ensure_action_slot(armature.animation_data, action, armature)

    baked = 0

    for pb in armature.pose.bones:
        if only_selected and not blender_compat.pose_bone_is_selected(pb):
            continue

        keyframes: dict[tuple[str, int], list[float]] = {}

        def store(prop: str, idx: int, frame: int, val: float) -> None:
            key = (f'pose.bones["{pb.name}"].{prop}', idx)
            keyframes.setdefault(key, []).extend((frame, val))

        rot_mode = pb.rotation_mode
        euler_prev = None
        quat_prev = None

        for f, mats in bones_data:
            if pb.name not in mats:
                continue
            pb.matrix_basis = mats[pb.name].copy()

            for i, v in enumerate(pb.location):
                store("location", i, f, v)

            if rot_mode == "QUATERNION":
                if quat_prev is not None:
                    q = pb.rotation_quaternion.copy()
                    q.make_compatible(quat_prev)
                    pb.rotation_quaternion = q
                    quat_prev = q
                else:
                    quat_prev = pb.rotation_quaternion.copy()
                for i, v in enumerate(pb.rotation_quaternion):
                    store("rotation_quaternion", i, f, v)
            elif rot_mode == "AXIS_ANGLE":
                for i, v in enumerate(pb.rotation_axis_angle):
                    store("rotation_axis_angle", i, f, v)
            else:
                if euler_prev is not None:
                    e = pb.rotation_euler.copy()
                    e.make_compatible(euler_prev)
                    pb.rotation_euler = e
                    euler_prev = e
                else:
                    euler_prev = pb.rotation_euler.copy()
                for i, v in enumerate(pb.rotation_euler):
                    store("rotation_euler", i, f, v)

            for i, v in enumerate(pb.scale):
                store("scale", i, f, v)

        if not keyframes:
            continue
        baked += 1

        for (data_path, index), pairs in keyframes.items():
            fc = ensure_fcurve(action, armature, data_path, index)
            if fc is None:
                continue
            n = len(pairs) // 2
            try:
                bake_frames = set(int(pairs[i]) for i in range(0, len(pairs), 2))
                kp = fc.keyframe_points
                for i in range(len(kp) - 1, -1, -1):
                    if int(kp[i].co[0]) in bake_frames:
                        kp.remove(kp[i], fast=True)
            except Exception:
                pass
            fc.keyframe_points.add(n)
            existing = len(fc.keyframe_points) - n
            for i in range(n):
                fc.keyframe_points[existing + i].co = (pairs[i * 2], pairs[i * 2 + 1])
                fc.keyframe_points[existing + i].interpolation = "LINEAR"
            try:
                fc.update()
            except Exception:
                pass
            try:
                grp = action.groups.get(pb.name) or action.groups.new(pb.name)
                fc.group = grp
            except Exception:
                pass

    return baked
