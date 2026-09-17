# SPDX-License-Identifier: GPL-3.0-or-later
"""Posing with the controls keys the pose, so nothing has to be detached.

The Autoposer writes ``matrix_basis`` directly, which is transient: with an
action bound, the next animation evaluation re-applies the keys over the solve
and the pose appears to be thrown away. Its answer was **Take Over Rig** —
detach the action, hold the pose, hand it back later — which is a mode the
artist has to know about, remember they are in, and get out of. It also
invited the worst failure this addon has had: generate while held, and giving
back would swap the result for the action from before.

The motion-curve drag never had any of that, because it never poses the rig:
it solves, writes keys at the frame, and lets the action carry the pose. This
does the same for the controls. After a solve, the result is keyed at the
current frame — debounced, so a live drag at eighty solves a second writes
once it settles — and the action reproduces it on every evaluation afterwards.

The keys are typed as the artist's own, so a pose made this way is a key pose
like any other: it ghosts, it sits on the timeline, and the next generation is
asked to hit it.
"""

from __future__ import annotations

import time

import bpy


#: Quiet needed after the last solve before the pose is written. A live drag
#: solves continuously; keying each one would write a few hundred channels per
#: tick for a pose that is still moving.
KEY_DEBOUNCE = 0.25

_pending: dict = {"at": None, "frame": -1, "channels": None}


def _settings(scene):
    return getattr(scene, "animatica", None)


def _target(scene):
    from . import properties

    settings = _settings(scene)
    if settings is None:
        return None
    return properties._live_armature(settings.target_armature)


def on_solved(context) -> None:
    """Hook for :data:`poser.AFTER_SOLVE`: capture now, write when it settles.

    The capture cannot wait. A solve lives in ``matrix_basis``, and the next
    animation evaluation puts the action's pose back over it — so a write
    scheduled for a quarter of a second later would read the pose the artist
    was trying to change and key that instead. Measured before this split:
    434 cm of drift, the solve gone and the old pose keyed over it.

    Writing can wait, and should: a live drag solves eighty times a second and
    each write is a few hundred F-curve points for a pose that is still moving.
    """
    from . import pose_edit

    scene = getattr(context, "scene", None)
    arm = _target(scene) if scene is not None else None
    if arm is None:
        return
    _pending["channels"] = pose_edit.pose_channels(arm)
    _pending["frame"] = int(scene.frame_current)
    _pending["at"] = time.monotonic()
    if not bpy.app.timers.is_registered(_write_timer):
        bpy.app.timers.register(_write_timer, first_interval=KEY_DEBOUNCE)


def _write_timer():
    at = _pending["at"]
    if at is None:
        return None
    waited = time.monotonic() - at
    if waited < KEY_DEBOUNCE:
        return KEY_DEBOUNCE - waited     # still moving
    _pending["at"] = None
    # The captured pose is cleared by write_captured once it has been used.
    # Clearing it here threw the pose away a line before it was needed, and
    # every auto-key through this timer silently wrote nothing.
    try:
        write_captured(bpy.context)
    except Exception as exc:             # noqa: BLE001 — a timer must not raise
        print(f"[Animatica] keying the solved pose failed: {exc}")
    return None


def write_captured(context) -> int:
    """Write the captured pose into the action, at the frame it was made for.

    The frame is the one the solve happened on, not whatever the playhead
    reads now: the two differ if the artist scrubbed while the debounce was
    running, and the pose belongs where it was made.
    """
    from . import key_poses, pose_edit

    scene = context.scene
    arm = _target(scene)
    channels = _pending["channels"]
    if arm is None or not channels:
        return 0
    frame = int(_pending["frame"])
    _pending["channels"] = None

    action = pose_edit._editing_action(arm)
    if action is None:
        if arm.animation_data is None:
            arm.animation_data_create()
        action = bpy.data.actions.new(f"{arm.name}Action")
        arm.animation_data.action = action

    written = pose_edit.write_channels(action, frame, channels)
    if written:
        # The same thing the motion-curve drag says, in the same place: posing
        # a frame makes it one of yours, however you posed it. Without this a
        # control drag committed in silence and the only way to know was to
        # scrub off and back.
        key_poses.flash_keyed(frame)
        scene.ap_status = f"keyed at frame {frame}"
        key_poses.invalidate_plan()
        key_poses.request_rebuild()
    return written


# ---------------------------------------------------------------------------
# Controls follow the frame
# ---------------------------------------------------------------------------

def _on_frame_change(scene, _depsgraph=None) -> None:
    """Re-seat the controls on the pose at the new frame.

    A control is a handle on a joint, and the joint moves with the animation —
    but the controls are free bones that stay where they were last put. Scrub
    away and they are left behind: measured on a walk, 1.7 to 2.5 metres from
    the joints they drive. Grab one there and the solve does what it is told,
    which is to pull the body back to where the handle is. That is why posing
    "did not work" at any frame but the one the controls were seated on.

    Seating them on every frame change costs about 2 ms and means the
    Autoposer is simply available wherever the playhead is.
    """
    from . import key_poses
    from .autoposer import poser

    # Not while something else is stepping the playhead: the ghost bake walks
    # a hundred frames, and a generation samples frame by frame. Both would
    # snap the controls once per frame and leave them wherever they stopped.
    if key_poses._baking or poser._BUSY:
        return
    settings = _settings(scene)
    if settings is None or settings.is_generating:
        return
    arm = _target(scene)
    if arm is None or not poser.has_controls(arm):
        return
    try:
        poser._snap(arm, bpy.context)
        # The controls just moved, and nobody moved them. Take that as the new
        # baseline, or the live timer reads a scrub as a drag and solves.
        poser.sync_state(arm)
    except Exception as exc:                # noqa: BLE001 — a handler must not raise
        print(f"[Animatica] could not seat the controls: {exc}")


def reseat_controls(scene=None) -> None:
    """Put the controls back on their joints at the current frame.

    Called after anything that walked the playhead with the frame handler
    muted — a ghost bake steps a hundred frames and restores, and the controls
    would otherwise be left describing a pose from the middle of that walk.
    """
    _on_frame_change(scene or bpy.context.scene)


_HANDLER_NAME = "_on_frame_change"


def _purge(handlers) -> None:
    for h in list(handlers):
        if getattr(h, "__name__", None) == _HANDLER_NAME:
            handlers.remove(h)


def register() -> None:
    from .autoposer import poser

    poser.AFTER_SOLVE = on_solved
    _purge(bpy.app.handlers.frame_change_post)
    bpy.app.handlers.frame_change_post.append(_on_frame_change)


def unregister() -> None:
    from .autoposer import poser

    poser.AFTER_SOLVE = None
    _purge(bpy.app.handlers.frame_change_post)
    if bpy.app.timers.is_registered(_write_timer):
        bpy.app.timers.unregister(_write_timer)
    _pending["at"] = None
