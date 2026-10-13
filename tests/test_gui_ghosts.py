"""The key-pose ghosts -- the motion plan -- in a real Blender window, through the add-on's own
operators (the server is stood in for by the stub of test_gui_object_rotation.py):

* every pose keyed shows as a ghost: two keys, two ghosts, each where the rig stands then
  (the onion skin is Animatica Marionette's; these are not it);
* after Generate and Discard, the ghosts are baked again, of the motion that is back -- also
  when the take was spliced into the artist's own action, which is rewritten in place and
  tells the change handler nothing.

A window opens and closes itself::

    blender --factory-startup --python tests/test_gui_ghosts.py
"""

from __future__ import annotations

import math
import os
import sys
import time
import traceback

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for p in (ROOT, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import addon_utils  # noqa: E402
import bpy  # noqa: E402
from mathutils import Euler  # noqa: E402

addon_utils.enable("animatica_blender", default_set=True)
import test_gui_object_rotation as R  # noqa: E402  (the stub server, the synthetic rig and take)
from animatica_blender import key_poses  # noqa: E402

FAILS: list[str] = []
TOL = 1e-4
_rebuilds: list = []                 # (time, the ghost frames) of every bake


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def counted_rebuild(fn):
    def wrapper(*a, **k):
        n = fn(*a, **k)
        _rebuilds.append((time.time(), list(key_poses._ghosts["frames"])))
        return n
    return wrapper


def settled():
    return key_poses._rebuild_requested_at is None and not key_poses._ghosts["dirty"]


def wait_settled(timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for a in bpy.context.window_manager.windows[0].screen.areas:
            a.tag_redraw()
        if settled():
            break
        yield
    for _ in range(6):
        yield


def roots_vs_rig(arm, frames):
    """How far each ghost's root is from where the rig's root is at its frame now."""
    sc = bpy.context.scene
    saved = sc.frame_current
    root_pb = next(pb for pb in arm.pose.bones if pb.parent is None)
    worst, missing = 0.0, []
    key_poses._baking = True
    try:
        for f in frames:
            got = key_poses._ghosts["roots"].get(f)
            if got is None:
                missing.append(f)
                continue
            sc.frame_set(f)
            bpy.context.view_layer.update()
            eva = arm.evaluated_get(bpy.context.evaluated_depsgraph_get())
            want = eva.matrix_world @ eva.pose.bones[root_pb.name].head
            worst = max(worst, (got[0] - want).length)
    finally:
        sc.frame_set(saved)
        bpy.context.view_layer.update()
        key_poses._baking = False
    return worst, missing


def key_pose(arm, frame, angle):
    """Key the whole rig at ``frame``, a turn of ``angle`` on its first child bones (a pose)."""
    for pb in arm.pose.bones:
        pb.rotation_mode = 'XYZ'
        pb.rotation_euler = Euler((angle, 0.0, angle * 0.5)) if pb.parent is not None and pb.parent.parent is None \
            else Euler((0.0, 0.0, 0.0))
        pb.keyframe_insert("rotation_euler", frame=frame)
        pb.keyframe_insert("location", frame=frame)


def steps():
    subject, take, frames, yaw, _ref = R.setup()
    s = bpy.context.scene.animatica
    s.key_pose_overlay = s.key_pose_ghosts = s.key_pose_auto_refresh = True

    # --- two keys, two ghosts
    arm = R.copy_rig(subject, "Ghosts", yaw)
    R.select_only([arm])
    R.set_blocks(arm, "walks", frames)
    f1, f2 = frames[0] + 4, frames[0] + 16
    arm.animation_data_create()
    arm.animation_data.action = bpy.data.actions.new("Ghosts keys")
    key_pose(arm, f1, 0.0)
    key_pose(arm, f2, 0.8)
    bpy.context.scene.frame_set(frames[-1])         # the playhead on neither
    key_poses.request_rebuild()
    yield from wait_settled()
    shown = [f for f, _e in key_poses._visible_poses(bpy.context.scene, key_poses.plan(bpy.context.scene))]
    check("two keys: two ghosts, drawn", key_poses._ghosts["frames"] == [f1, f2] and shown == [f1, f2]
          and all(key_poses._ghosts["ghosts"][f]["tris"] or key_poses._ghosts["ghosts"][f]["lines"] for f in (f1, f2)),
          f"baked {key_poses._ghosts['frames']}, drawn {shown}")
    worst, missing = roots_vs_rig(arm, [f1, f2])
    check("...each where the rig stands at its frame", worst < TOL and not missing, f"{worst * 100:.3f} cm")

    # --- Generate, then Discard, spliced into the artist's action (it is rewritten in place)
    arm2 = R.copy_rig(subject, "Discard", yaw)
    arm2.animation_data_create()
    action = bpy.data.actions.new("Discard keys")
    arm2.animation_data.action = action
    before, after = frames[0] - 1, frames[-1] + 20
    for f in (before, after):
        key_pose(arm2, f, 0.0)
    key_pose(arm2, f1, 0.5)
    key_pose(arm2, f2, -0.5)
    R.select_only([arm2])
    R.set_blocks(arm2, "walks", frames)
    R.Stub.responder = lambda req: take
    key_poses.request_rebuild()
    yield from wait_settled()
    with bpy.context.temp_override(**R.override()):
        ret = bpy.ops.animatica.generate('INVOKE_DEFAULT')
    yield from R.wait_for(lambda: not s.is_generating, "Generate")
    check("Generate made a take, spliced into the artist's action",
          s.is_previewing and arm2.animation_data.action is action, f"{ret}")
    yield from wait_settled()
    bpy.context.scene.frame_set(frames[-1])
    with bpy.context.temp_override(**R.override()):
        ret = bpy.ops.animatica.reject()
    t_reject = time.time()
    check("Discard", ret == {'FINISHED'} and not s.is_previewing, f"{ret}")
    yield from wait_settled()
    baked_after = [fr for t, fr in _rebuilds if t > t_reject]
    check("after Discard the ghosts are baked again", bool(baked_after), f"{len(baked_after)} bakes since")
    want = sorted({f for f in (before, f1, f2, after)})
    got = key_poses._ghosts["frames"]
    worst, missing = roots_vs_rig(arm2, got)
    check("...of the motion that is back: the artist's keys, where the rig stands",
          got == want and worst < TOL and not missing, f"ghosts {got} (keys {want}), {worst * 100:.3f} cm")


def run():
    key_poses.rebuild = counted_rebuild(key_poses.rebuild)
    gen = steps()

    def tick():
        try:
            next(gen)
            return 0.05
        except StopIteration:
            pass
        except Exception:                                   # noqa: BLE001
            traceback.print_exc()
            FAILS.append("raised")
        print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ""))
        sys.stdout.flush()
        os._exit(1 if FAILS else 0)

    bpy.app.timers.register(tick, first_interval=1.0)


if __name__ == "__main__":
    bpy.context.preferences.view.show_splash = False
    run()
