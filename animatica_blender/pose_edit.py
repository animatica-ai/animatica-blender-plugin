# SPDX-License-Identifier: GPL-3.0-or-later
"""Click a ghost to edit the pose it stands for.

The ghosts show where the key poses are; this makes them the handle you grab
to change one. Clicking a ghost takes the playhead to its frame, puts the rig
in pose mode, and — where the Autoposer is driving that rig — hands the pose
over to it so the artist can push the body around with a handful of controls
instead of bone by bone. **Set Keyframe** writes the result back onto
that frame's keyframe, which is the whole point: the pose you edit is the
constraint the next generation is asked to hit.

Why a commit step exists at all
-------------------------------
The Autoposer has to *detach* the action to work (``autoposer.take_over``):
its solve and the action both write the same bones, and whichever runs last
wins, so the action is stashed while the artist poses. That means the edited
pose lives only in the pose bones, and re-attaching the action would wipe it.
Set Keyframe therefore reads the solved pose and writes it into the stashed
action's F-curves at the current frame, before handing the rig back.

The keys it writes are typed ``KEYFRAME``, not ``GENERATED``. On a rig
carrying a generated take that distinction is everything: Blender preserves a
keyframe's existing type when you key over one, so a pose keyed on top of a
bake stays typed as the model's own output and the request builder drops it.
See :func:`constraints_ui.authored_pose_frames`.

Without the Autoposer this is still useful: the click lands you on the frame,
in pose mode, on the right rig, and Set Keyframe keys whatever you posed by
hand.
"""

from __future__ import annotations

import bpy
from bpy.props import IntProperty
from bpy.types import Operator


# ---------------------------------------------------------------------------
# Autoposer interop
#
# Deliberately duck-typed. The Autoposer is a separate addon today and is
# expected to move into this one; everything here asks whether it is present
# and driving *this* rig, and degrades to plain pose-mode editing when it is
# not. No import, no hard dependency, no version check beyond the operators
# actually being there.
# ---------------------------------------------------------------------------

_AP_STASHED_ACTION = "ap_stashed_action"


def autoposer_available() -> bool:
    return hasattr(bpy.ops, "autoposer") and hasattr(bpy.ops.autoposer, "take_over")


def autoposer_drives(arm) -> bool:
    """True when the Autoposer is live on ``arm``."""
    if arm is None or not autoposer_available():
        return False
    scene = bpy.context.scene
    return str(getattr(scene, "ap_armature", "") or "") == arm.name


def autoposer_holds(arm) -> bool:
    """True when the Autoposer currently owns the rig's action."""
    return arm is not None and bool(arm.get(_AP_STASHED_ACTION))


def active_session(settings, arm) -> int | None:
    """The frame actually being edited, or None.

    A recorded frame is only a live session while the playhead is still on it,
    or while the Autoposer is holding the rig for it. Anything else is a
    leftover — the artist scrubbed away, an undo restored an old value, a file
    was loaded — and a leftover must never block a click or offer an Apply
    that would write somewhere the artist is not looking.

    Read-only: the panel calls this from ``draw``.
    """
    frame = int(getattr(settings, "editing_key_pose_frame", -1))
    if frame < 0:
        return None
    if autoposer_holds(arm):
        return frame
    scene = getattr(bpy.context, "scene", None)
    if scene is not None and frame == int(scene.frame_current):
        return frame
    return None


def _editing_action(arm):
    """The action an edit must be written into.

    While the Autoposer holds the rig its action is detached and stashed on
    the object, so the live ``animation_data.action`` is not the one the
    artist is editing.
    """
    stashed = arm.get(_AP_STASHED_ACTION)
    if stashed:
        action = bpy.data.actions.get(str(stashed))
        if action is not None:
            return action
    if arm.animation_data is None:
        arm.animation_data_create()
    return arm.animation_data.action


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


def write_channels(action, frame: int, channels) -> int:
    """Write ``(data_path, index, value)`` triples onto ``action`` at ``frame``.

    Goes through the F-curves directly rather than ``keyframe_insert`` for two
    reasons: it works while the action is detached — the state the Autoposer
    leaves the rig in — and it can key a frame the rig is not standing on,
    which is what the motion-curve drag needs.

    Keys are typed ``KEYFRAME``. On a rig carrying a generated take that is
    the whole difference between a pose the request sends and one it drops.
    """
    from . import constraints_ui

    written = 0
    for data_path, index, value in channels:
        fc = constraints_ui._ensure_fcurve(action, data_path, index)
        if fc is None:
            continue
        kp = next((k for k in fc.keyframe_points if int(round(k.co.x)) == frame), None)
        if kp is None:
            kp = fc.keyframe_points.insert(frame, value)
        else:
            kp.co.y = value
            kp.handle_left.y = value
            kp.handle_right.y = value
        kp.type = 'KEYFRAME'
        written += 1
    for fcurves in constraints_ui._iter_fcurve_collections(action):
        for fc in fcurves:
            fc.update()
    return written


def _write_pose_to_action(arm, action, frame: int) -> int:
    """Write the rig's current pose into ``action`` at ``frame``."""
    channels = []
    root = next((pb for pb in arm.pose.bones if pb.parent is None), None)
    for pb in _edited_bones(arm):
        path, values = _rotation_values(pb)
        channels += [(f'pose.bones["{pb.name}"].{path}', i, v) for i, v in enumerate(values)]
        if pb is root or pb.parent is None:
            # The root's placement is half the pose: without it the body's
            # rotation is keyed and its position is left to whatever else is
            # driving the rig.
            channels += [
                (f'pose.bones["{pb.name}"].location', i, v)
                for i, v in enumerate(pb.location)
            ]
    return write_channels(action, frame, channels)


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


class ANIMATICA_OT_edit_key_pose(Operator):
    bl_idname = "animatica.edit_key_pose"
    bl_label = "Edit Key Pose"
    bl_description = (
        "Go to this key pose and start editing it. Hands the pose to the "
        "Autoposer when it is driving this rig; Apply writes the result back "
        "onto this frame's keyframe"
    )
    bl_options = {'REGISTER', 'UNDO'}

    frame: IntProperty(name="Frame", default=1)

    @classmethod
    def poll(cls, context):
        return _target(context) is not None

    def execute(self, context):
        from . import key_poses

        arm = _target(context)
        settings = _settings(context)
        frame = int(self.frame)

        context.scene.frame_set(frame)

        # Pose mode on the right rig, or none of the rest means anything.
        if context.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except RuntimeError:
                pass
        for obj in context.view_layer.objects:
            obj.select_set(False)
        arm.select_set(True)
        context.view_layer.objects.active = arm
        try:
            bpy.ops.object.mode_set(mode='POSE')
        except RuntimeError:
            self.report({'WARNING'}, "Could not enter pose mode")
            return {'CANCELLED'}

        handed_over = False
        if autoposer_drives(arm) and not autoposer_holds(arm):
            # Seat the controls on the pose that is there before freezing it,
            # so the artist starts from their own key rather than from
            # wherever the controls were left.
            try:
                bpy.ops.autoposer.snap_controls()
                bpy.ops.autoposer.take_over()
                handed_over = True
            except RuntimeError as exc:
                self.report({'WARNING'}, f"Autoposer did not take the rig: {exc}")

        settings.editing_key_pose_frame = frame
        key_poses.tag_redraw()
        self.report(
            {'INFO'},
            f"Editing the pose at frame {frame}"
            + (" — Autoposer has the rig" if handed_over else ""),
        )
        return {'FINISHED'}


class ANIMATICA_OT_pick_ghost(Operator):
    bl_idname = "animatica.pick_ghost"
    bl_label = "Pick Key Pose Ghost"
    bl_description = "Click a ghosted key pose to edit it"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event):
        from . import key_poses

        # This runs on every left click in the 3D view, so it has two jobs
        # before anything else: be cheap, and never consume a click it did not
        # mean to. Anything unexpected passes the click on untouched — a
        # broken overlay must not break selection.
        try:
            if context.area is None or context.area.type != 'VIEW_3D':
                return {'PASS_THROUGH'}
            settings = _settings(context)
            if settings is None or not settings.show_key_poses:
                return {'PASS_THROUGH'}
            # A point on a motion curve wins over the ghost behind it: it is
            # the smaller target and the more specific intent.
            from . import curve_edit

            grabbed = curve_edit.pick_point(
                context, event.mouse_region_x, event.mouse_region_y,
            )
            if grabbed is not None:
                bone, curve_frame, _world = grabbed
                bpy.app.driver_namespace["animatica_last_ghost_pick"] = {
                    "xy": (event.mouse_region_x, event.mouse_region_y),
                    "hit": f"curve {bone}@{curve_frame}",
                }
                return bpy.ops.animatica.drag_motion_curve(
                    'INVOKE_DEFAULT', bone=bone, frame=curve_frame,
                )
            frame = key_poses.pick_frame(
                context, event.mouse_region_x, event.mouse_region_y,
            )
            # Leave a trace of what the last click decided. Clicking a ghost
            # and getting nothing is indistinguishable from the binding never
            # firing, and this is the only way to tell them apart afterwards.
            bpy.app.driver_namespace["animatica_last_ghost_pick"] = {
                "xy": (event.mouse_region_x, event.mouse_region_y),
                "hit": frame,
            }
            if frame is not None:
                # Switching poses mid-hand-over would strand the edit inside
                # the Autoposer, so that one case is refused — out loud, and
                # still passing the click on so selection behaves normally.
                held = active_session(settings, _target(context))
                if held is not None and held != frame and autoposer_holds(_target(context)):
                    self.report(
                        {'WARNING'},
                        f"Apply or Cancel the pose at frame {held} first",
                    )
                    return {'PASS_THROUGH'}
        except Exception as exc:            # noqa: BLE001 — never eat a click
            print(f"[Animatica] ghost pick failed: {exc}")
            return {'PASS_THROUGH'}
        if frame is None:
            return {'PASS_THROUGH'}
        return bpy.ops.animatica.edit_key_pose('INVOKE_DEFAULT', frame=frame)


class ANIMATICA_OT_set_key_pose(Operator):
    bl_idname = "animatica.set_key_pose"
    bl_label = "Set Keyframe"
    bl_description = (
        "Key the pose you are looking at as one of yours, so the next "
        "generation is asked to hit it. Not the same as pressing I: Blender "
        "keeps a keyframe's existing type when you key over one, so a pose "
        "set on top of a generated take would otherwise read as the model's "
        "own output and be left out of the request"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _target(context) is not None

    def execute(self, context):
        from . import key_poses

        arm = _target(context)
        settings = _settings(context)
        frame = int(context.scene.frame_current)

        action = _editing_action(arm)
        if action is None:
            # A rig with no action yet still deserves a first key pose.
            if arm.animation_data is None:
                arm.animation_data_create()
            action = bpy.data.actions.new(f"{arm.name}Action")
            arm.animation_data.action = action

        written = _write_pose_to_action(arm, action, frame)

        # Hand the rig back before anything re-evaluates: the pose now lives
        # in the action, so re-attaching reproduces what was just keyed.
        if autoposer_holds(arm):
            try:
                bpy.ops.autoposer.release()
            except RuntimeError as exc:
                self.report({'WARNING'}, f"Autoposer did not release: {exc}")

        settings.editing_key_pose_frame = -1
        context.scene.frame_set(frame)
        key_poses.invalidate_plan()
        key_poses.request_rebuild()
        self.report({'INFO'}, f"Key pose set at frame {frame} ({written} channels)")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    ANIMATICA_OT_edit_key_pose,
    ANIMATICA_OT_pick_ghost,
    ANIMATICA_OT_set_key_pose,
)

_keymaps: list = []


def _register_keymap() -> None:
    """Bind a plain left click in the 3D view to the ghost pick.

    An addon keymap entry is consulted before Blender's own, and the operator
    returns ``PASS_THROUGH`` whenever the click is not on a ghost — so
    selection, tools and gizmos behave exactly as they did everywhere else.
    """
    wm = bpy.context.window_manager
    config = getattr(wm.keyconfigs, "addon", None)
    if config is None:
        return
    km = config.keymaps.new(name="3D View", space_type='VIEW_3D')
    kmi = km.keymap_items.new(
        ANIMATICA_OT_pick_ghost.bl_idname, 'LEFTMOUSE', 'PRESS',
    )
    _keymaps.append((km, kmi))


def _unregister_keymap() -> None:
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except (RuntimeError, ReferenceError):
            pass
    _keymaps.clear()


def register() -> None:
    for cls in _classes:
        bpy.utils.register_class(cls)
    _register_keymap()


def unregister() -> None:
    _unregister_keymap()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
