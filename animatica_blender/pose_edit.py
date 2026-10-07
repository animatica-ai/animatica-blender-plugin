# SPDX-License-Identifier: GPL-3.0-or-later
"""Key a pose: Set Key Pose, Key This Frame, and the keying the Autoposer's edits use.

A key pose is what steers a generation: the take passes through it. Set Key
Pose writes the pose you see onto this frame as your own key; Key This Frame
keeps a take's pose as one, so the next Redo passes through it.

The keys it writes are typed ``KEYFRAME``, not ``GENERATED``. On a rig
carrying a generated take that distinction is everything: Blender preserves a
keyframe's existing type when you key over one, so a pose keyed on top of a
bake stays typed as the model's own output and the request builder drops it.
See :func:`constraints_ui.authored_pose_frames`.

Editing a pose by hand is Animatica Marionette's (clicking a ghost to edit
it, the handles, the trail): it keys through ``write_channels`` and the rest
of the keying here, so its edits land in the take the same way.
"""

from __future__ import annotations

import bpy
from bpy.types import Operator

#: where the Autoposer keeps the rig's action while it holds the rig (its old control rig)
_AP_STASHED_ACTION = "autoposer_stashed_action"


def _editing_action(arm):
    """The action an edit must be written into.

    Whatever is bound, first: that is what plays, and what the request reads.
    The Autoposer's stash is the fallback, for the window where it holds the
    rig and nothing is bound at all — writing into the stash while a different
    action is bound would put the pose somewhere nothing is looking.
    """
    if arm.animation_data is None:
        arm.animation_data_create()
    if arm.animation_data.action is not None:
        return arm.animation_data.action
    stashed = arm.get(_AP_STASHED_ACTION)
    if stashed:
        return bpy.data.actions.get(str(stashed))
    return None


# ---------------------------------------------------------------------------
# Pose snapshot
# ---------------------------------------------------------------------------

def _rotation_path(pb) -> str:
    mode = pb.rotation_mode
    if mode == 'QUATERNION':
        return "rotation_quaternion"
    if mode == 'AXIS_ANGLE':
        return "rotation_axis_angle"
    return "rotation_euler"


def _rotation_values(pb):
    path = _rotation_path(pb)
    return path, list(getattr(pb, path))


def _edited_bones(arm):
    """The bones an edit is written for: the deform skeleton, never controls.

    Control bones are handles — the request never sees them, and keying them
    would put the Autoposer's rig plumbing into the artist's action.
    """
    from . import request_builder

    try:
        deform = request_builder.emitted_deform_bones(arm)
    except Exception:                       # noqa: BLE001 — fall back to all
        deform = set()
    if deform:
        return [pb for pb in arm.pose.bones if pb.name in deform]
    return [pb for pb in arm.pose.bones if not pb.bone.hide]


def edit_key_type(arm, frame) -> str:
    """How an edit at ``frame`` is keyed: 'GENERATED' where a take's motion
    already is (the edit reshapes the take, and the next Generate does not
    take it for a pose to steer through), 'KEYFRAME' where nothing was made
    yet (posing first, the key poses a take is made from). A key pose that is
    there stays one: write_channels keeps it."""
    from . import constraints_ui
    action = _editing_action(arm)
    if action is None:
        return 'KEYFRAME'
    hips = hips_bone(arm)
    prefix = f'pose.bones["{hips}"].' if hips else "pose.bones["
    fc = next((c for c in constraints_ui.iter_action_fcurves(action)
               if c.data_path.startswith(prefix) and len(c.keyframe_points)), None)
    if fc is None:
        return 'KEYFRAME'
    before = after = None
    for k in fc.keyframe_points:
        f = int(round(k.co.x))
        if f == frame:
            return 'GENERATED' if k.type == 'GENERATED' else 'KEYFRAME'
        if f < frame:
            before = k
        elif after is None:
            after = k
    # between two keys of a take (a sparse one): its motion too
    if before is not None and after is not None and 'GENERATED' in (before.type, after.type):
        return 'GENERATED'
    return 'KEYFRAME'


def _continuous_quaternions(action, frame, channels) -> list:
    """Each quaternion on the side the curve already is at ``frame``.

    q and -q are the same turn, but an action interpolates the four numbers
    one by one: a key written on the other side from its neighbours sends the
    frames between through a near-zero quaternion, and the bone spins. That
    was the jitter in posed and propagated motion."""
    from . import constraints_ui
    quats = {}
    for i, (path, index, value) in enumerate(channels):
        if path.endswith("rotation_quaternion"):
            quats.setdefault(path, {})[index] = i
    if not quats:
        return channels
    curves = {}
    for fc in constraints_ui.iter_action_fcurves(action):
        if fc.data_path in quats:
            curves[(fc.data_path, fc.array_index)] = fc
    out = list(channels)
    for path, idx in quats.items():
        if len(idx) != 4 or any((path, k) not in curves for k in range(4)):
            continue
        now = [curves[(path, k)].evaluate(frame) for k in range(4)]
        new = [channels[idx[k]][2] for k in range(4)]
        if sum(a * b for a, b in zip(now, new)) < 0.0:
            for k in range(4):
                p_, i_, v_ = channels[idx[k]]
                out[idx[k]] = (p_, i_, -v_)
    return out


def write_channels(action, frame: int, channels, key_type: str = 'KEYFRAME', *, finish: bool = True,
                   existing_only: bool = False) -> int:
    """Write ``(data_path, index, value)`` triples onto ``action`` at ``frame``.

    Goes through the F-curves directly rather than ``keyframe_insert`` for two
    reasons: it works while the action is detached — the state the Autoposer
    leaves the rig in — and it can key a frame the rig is not standing on,
    which is what the motion-curve drag needs.

    Keys are typed ``KEYFRAME``. On a rig carrying a generated take that is
    the whole difference between a pose the request sends and one it drops.
    ``key_type`` 'GENERATED' writes motion rather than a key pose (the frames
    a trail stroke carries along); a key that is already a key pose stays one.
    """
    from . import _bake_common, constraints_ui

    channels = _continuous_quaternions(action, frame, list(channels))
    # ``existing_only``: update the keys that are there, add none -- what an
    # edit does to the frames around the one it was made on
    curves = ({(fc.data_path, fc.array_index): fc for fc in constraints_ui.iter_action_fcurves(action)}
              if existing_only else None)
    written = 0
    for data_path, index, value in channels:
        fc = (curves.get((data_path, index)) if existing_only
              else constraints_ui._ensure_fcurve(action, data_path, index))
        if fc is None:
            continue
        kp = next((k for k in fc.keyframe_points if int(round(k.co.x)) == frame), None)
        if kp is None and existing_only:
            continue
        if kp is None:
            kp = fc.keyframe_points.insert(frame, value)
            kp.type = key_type
        else:
            kp.co.y = value
            kp.handle_left.y = value
            kp.handle_right.y = value
            if key_type == 'KEYFRAME' or kp.type == 'GENERATED':
                kp.type = key_type
        written += 1
    if finish:
        finish_channels(action)
    return written


def finish_channels(action) -> None:
    """Re-sort and re-handle the curves after keys were written (once, after
    a batch written with ``finish=False``)."""
    from . import _bake_common, constraints_ui
    for fcurves in constraints_ui._iter_fcurve_collections(action):
        for fc in fcurves:
            fc.update()
    _bake_common.group_curves(action)     # keep the dope sheet a list of bones


def pose_channels(arm) -> list:
    """The rig's current pose as ``(data_path, index, value)`` triples.

    Split from the write so a pose can be *captured* at one moment and
    *written* at another. That distinction is not academic: a solve lives in
    ``matrix_basis``, which the next animation evaluation overwrites, so
    reading the pose even a fraction of a second later reads the action's pose
    back instead of the one just solved.
    """
    channels = []
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    hips = hips_bone(arm)
    for pb in _edited_bones(arm):
        path, values = _rotation_values(pb)
        channels += [(f'pose.bones["{pb.name}"].{path}', i, v) for i, v in enumerate(values)]
        if pb is root or pb.parent is None or pb.name == hips:
            # The root's placement is half the pose: without it the body's
            # rotation is keyed and its position is left to whatever else is
            # driving the rig.
            channels += [
                (f'pose.bones["{pb.name}"].location', i, v)
                for i, v in enumerate(pb.location)
            ]
    return channels


def hips_bone(arm) -> str:
    """The bone that carries the body's placement when it is not the rig's root — an Unreal
    ``pelvis`` under ``root``. Keying only a parentless bone's location leaves such a rig's
    body wherever the last key put it."""
    try:
        from . import posing

        b = posing.joint_bone(arm, "Hips")
    except Exception:                       # noqa: BLE001 — never break a key
        return ""
    return b.name if b is not None else ""


def is_rest_pose(arm) -> bool:
    """Whether the rig stands in its rest pose (a T-pose on most): every
    deform bone where its rest puts it."""
    from mathutils import Matrix
    ident = Matrix.Identity(4)
    for pb in arm.pose.bones:
        if not pb.bone.use_deform:
            continue
        m = pb.matrix_basis
        if any(abs(m[r][c] - ident[r][c]) > 1e-4 for r in range(4) for c in range(4)):
            return False
    return True


#: frames of the take under review the artist said to keep (the next Redo holds them)
_KEPT = "animatica_kept_frames"


def kept_frames(arm) -> list:
    return [int(f) for f in (arm.get(_KEPT) or [])] if arm is not None else []


def clear_kept(arm) -> None:
    if arm is not None and _KEPT in arm:
        del arm[_KEPT]


class ANIMATICA_OT_keep_frame(Operator):
    """Keep the take's pose at this frame as a key pose. The next Redo or
    Generate keeps this pose and remakes the rest"""
    bl_idname = "animatica.keep_frame"
    bl_label = "Keep This Frame"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _target(context) is not None

    def execute(self, context):
        from . import key_poses
        arm = _target(context)
        frame = int(context.scene.frame_current)
        action = _editing_action(arm)
        if action is None or not _write_pose_to_action(arm, action, frame):
            self.report({'WARNING'}, "Nothing to keep here")
            return {'CANCELLED'}
        arm[_KEPT] = sorted(set(kept_frames(arm)) | {frame})
        key_poses.flash_keyed(frame)
        key_poses.invalidate_plan()
        key_poses.request_rebuild()
        self.report({'INFO'}, f"Kept frame {frame}. The next take keeps this pose")
        return {'FINISHED'}


def _write_pose_to_action(arm, action, frame: int) -> int:
    """Write the rig's current pose into ``action`` at ``frame``."""
    return write_channels(action, frame, pose_channels(arm))


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _settings(context):
    return getattr(context.scene, "animatica", None)


def _target(context):
    from . import properties

    settings = _settings(context)
    if settings is None:
        return None
    return properties._live_armature(settings.target_armature)


class ANIMATICA_OT_set_key_pose(Operator):
    bl_idname = "animatica.set_key_pose"
    bl_label = "Set Key Pose"
    bl_description = (
        "Key the pose you see as your own key pose, so the next generation "
        "passes through it. Pressing I is different: Blender keeps the type of "
        "a keyframe you key over, so a pose keyed on top of a generated take "
        "would still count as generated and be left out of the request"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _target(context) is not None

    def execute(self, context):
        from . import key_poses, posing

        arm = _target(context)
        frame = int(context.scene.frame_current)

        action = _editing_action(arm)
        if action is None:
            # A rig with no action yet still deserves a first key pose.
            if arm.animation_data is None:
                arm.animation_data_create()
            action = bpy.data.actions.new(f"{arm.name}Action")
            arm.animation_data.action = action

        if is_rest_pose(arm):
            # the T-pose keyed is the worst first take: it is hit, exactly, arms out
            self.report({'WARNING'}, "That is the rest pose (T-pose). Pose the character first, "
                                     "or use Describe a pose")
            return {'CANCELLED'}
        written = _write_pose_to_action(arm, action, frame)
        if not written:
            # Never a silent success: a press that keys nothing must say so.
            self.report({'WARNING'}, f"Nothing could be keyed on “{action.name}”")
            return {'CANCELLED'}
        key_poses.flash_keyed(frame)
        posing.end_edit(context.scene)           # a ghost clicked to edit (the Autoposer's): done
        context.scene.frame_set(frame)
        key_poses.invalidate_plan()
        key_poses.request_rebuild()
        n = len(key_poses.take_keys(context.scene))
        if frame in key_poses.dropped_frames(context.scene):
            # a key the take would leave out: said here, where it was made
            self.report({'WARNING'}, f"Key pose set at frame {frame}, outside the take's blocks, so "
                                     "Generate leaves it out. Click the hint above the bar to stretch "
                                     "the block over it")
            return {'FINISHED'}
        self.report({'INFO'}, f"Key pose set at frame {frame}. Generate will pass through {n} key pose"
                              + ("s" if n != 1 else ""))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    ANIMATICA_OT_set_key_pose,
    ANIMATICA_OT_keep_frame,
)

def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
