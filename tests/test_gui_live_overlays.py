"""The ghosts of the key poses -- the motion plan -- follow the keys, live, in a real Blender
window: keys deleted in the Dope Sheet show on them within moments, deleted while nothing else
is going on and while the animation plays. (The onion skin and the trail are Animatica
Marionette's, and tested there.)

"Right" is measured, not assumed: after each edit the ghosts are the keyed frames, each where
the rig's root stands at its frame now.

Needs the Animatic character in Blender's data (read, never fetched). A window opens and closes
itself::

    BUR=$(mktemp -d); mkdir -p $BUR/datafiles
    ln -s "$HOME/Library/Application Support/Blender/5.1/datafiles/animatica" $BUR/datafiles/
    BLENDER_USER_RESOURCES=$BUR blender --factory-startup --python tests/test_gui_live_overlays.py
"""

from __future__ import annotations

import math
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=True)
from animatica_blender import canonical_skeleton, constraints_ui, key_poses  # noqa: E402

FAILS: list[str] = []
LOG: list[str] = []
KEYS = (1, 9, 17, 25, 33, 41, 49, 57)
TOL = 1e-4


def check(name, ok, info=""):
    line = ("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else "")
    print(line)
    LOG.append(line)
    if not ok:
        FAILS.append(name)


def areas():
    win = bpy.context.window_manager.windows[0]
    v3d = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
    dope = next(a for a in win.screen.areas if a.type == 'DOPESHEET_EDITOR')
    return win, v3d, dope


def setup():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    ctx = bpy.context
    arm = canonical_skeleton.load_character(ctx)[0]
    s = ctx.scene.animatica
    s.target_armature = arm
    # a motion: the hips travel and sway, the left arm swings
    bones = arm.pose.bones
    hips = next(pb for pb in bones if pb.name.split(":")[-1] == "Hips")
    larm = next(pb for pb in bones if pb.name.split(":")[-1] == "LeftArm")
    larm.rotation_mode = 'XYZ'
    for i, f in enumerate(KEYS):
        hips.location = (0.25 * math.sin(i * 1.3), 0.0, 0.4 * i)
        hips.keyframe_insert("location", frame=f)
        larm.rotation_euler = (0.0, 0.0, 0.9 * math.sin(i * 1.7))
        larm.keyframe_insert("rotation_euler", frame=f)
    ctx.scene.frame_start, ctx.scene.frame_end = 1, 64
    ctx.scene.frame_set(25)
    s.key_pose_overlay = True
    s.key_pose_ghosts = True
    s.key_pose_auto_refresh = True
    # the Timeline a Dope Sheet, its keys deletable as an artist deletes them
    _win, _v3d, dope = areas()
    dope.ui_type = 'DOPESHEET'
    dope.spaces.active.dopesheet.show_only_selected = False
    for o in ctx.view_layer.objects:
        o.select_set(o is arm)
    ctx.view_layer.objects.active = arm
    return arm


def delete_keys_at(arm, frame):
    """Select the keys on ``frame`` and press Delete in the Dope Sheet."""
    action = arm.animation_data.action
    for fc in constraints_ui.iter_action_fcurves(action):
        for kp in fc.keyframe_points:
            on = int(round(kp.co.x)) == frame
            kp.select_control_point = kp.select_left_handle = kp.select_right_handle = on
    win, _v3d, dope = areas()
    region = next(r for r in dope.regions if r.type == 'WINDOW')
    with bpy.context.temp_override(window=win, area=dope, region=region):
        r = bpy.ops.action.delete(confirm=False)
    left = sorted({int(round(kp.co.x)) for fc in constraints_ui.iter_action_fcurves(action)
                   for kp in fc.keyframe_points})
    return r, left


def keyed(arm):
    action = arm.animation_data.action
    return sorted({int(round(kp.co.x)) for fc in constraints_ui.iter_action_fcurves(action)
                   for kp in fc.keyframe_points if kp.type != 'GENERATED'})


def compare(arm, label):
    """The ghosts against the keys and the rig now."""
    want = keyed(arm)
    got = list(key_poses._ghosts["frames"])
    check(f"{label}: a ghost for every key pose, and no other", got == want, f"ghosts {got}, keys {want}")
    sc = bpy.context.scene
    saved = sc.frame_current
    root = next(pb for pb in arm.pose.bones if pb.parent is None)
    worst = 0.0
    key_poses._baking = True
    try:
        for f in got:
            sc.frame_set(f)
            bpy.context.view_layer.update()
            eva = arm.evaluated_get(bpy.context.evaluated_depsgraph_get())
            r = key_poses._ghosts["roots"].get(f)
            if r is not None:
                worst = max(worst, (r[0] - eva.matrix_world @ eva.pose.bones[root.name].head).length)
    finally:
        sc.frame_set(saved)
        bpy.context.view_layer.update()
        key_poses._baking = False
    check(f"{label}: each ghost where the rig stands at its frame", worst < TOL, f"{worst * 100:.3f} cm")


def settled():
    return key_poses._rebuild_requested_at is None and not key_poses._ghosts["dirty"]


def wait_settled(timeout=8.0):
    t0 = time.monotonic()
    while not settled() and time.monotonic() - t0 < timeout:
        yield
    for _ in range(6):
        yield
    while not settled() and time.monotonic() - t0 < timeout:
        yield


def steps():
    arm = setup()
    yield from wait_settled()
    check("the ghosts bake at the start", bool(key_poses._ghosts["frames"]),
          f"{len(key_poses._ghosts['frames'])} ghosts")
    compare(arm, "start")

    # 1. a key deleted with nothing else going on
    r, left = delete_keys_at(arm, 33)
    check("Delete in the Dope Sheet removes frame 33's keys", r == {'FINISHED'} and 33 not in left, f"{r} keys at {left}")
    yield from wait_settled()
    compare(arm, "after deleting 33")

    # 2. the last key: the motion ends sooner
    r, left = delete_keys_at(arm, 57)
    check("Delete removes frame 57's keys", r == {'FINISHED'} and 57 not in left, f"{r} keys at {left}")
    yield from wait_settled()
    compare(arm, "after deleting 57")

    # 3. a key deleted while the animation plays
    win, v3d, _dope = areas()
    region = next(rg for rg in v3d.regions if rg.type == 'WINDOW')
    with bpy.context.temp_override(window=win, area=v3d, region=region):
        bpy.ops.screen.animation_play()
    for _ in range(15):
        yield
    check("the animation is playing", key_poses.playing())
    r, left = delete_keys_at(arm, 41)
    check("Delete removes frame 41's keys during playback", r == {'FINISHED'} and 41 not in left, f"{r} keys at {left}")
    for _ in range(25):
        yield
    with bpy.context.temp_override(window=win, area=v3d, region=region):
        bpy.ops.screen.animation_cancel(restore_frame=True)
    yield from wait_settled()
    compare(arm, "after deleting 41 during playback")


def run():
    gen = steps()

    def tick():
        try:
            next(gen)
            return 0.02
        except StopIteration:
            pass
        except Exception:                                   # noqa: BLE001
            traceback.print_exc()
            FAILS.append("raised")
        print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ""))
        out = os.environ.get("AP_GUI_REPORT")
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write("\n".join(LOG + [f"{len(FAILS)} failed"]) + "\n")
        sys.stdout.flush()
        os._exit(1 if FAILS else 0)

    bpy.app.timers.register(tick, first_interval=1.0)


if __name__ == "__main__":
    bpy.context.preferences.view.show_splash = False
    run()
