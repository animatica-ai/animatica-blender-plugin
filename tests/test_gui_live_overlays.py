"""The ghosts (the onion skin) follow the keys, live, in a real Blender window: keys deleted in
the Dope Sheet show on them within moments -- deleted while nothing else is going on, and deleted
while the onion skin is still filling in after the last edit (the case that left them stale: an
edit then was taken for the overlay's own re-evaluation and dropped). Without Marionette (the
motion trail is its own, and tested there), so the onion skin's frames are its Before and After,
Step apart.

"Right" is measured, not assumed: after each edit every onion ghost's joints are compared with
the rig stepped to its frame now.

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
    s.onion_mode = 'FRAMES'
    s.onion_before, s.onion_after, s.onion_step = 3, 2, 4
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


def truth(arm, frames, bones):
    """Where the ghosts' joints are at each frame, the rig stepped there now (the overlay's
    change handler muted meanwhile, as its own bakes mute it)."""
    ctx = bpy.context
    sc = ctx.scene
    saved = sc.frame_current
    key_poses._baking = True
    out = {}
    try:
        for f in frames:
            sc.frame_set(f)
            ctx.view_layer.update()
            out[f] = key_poses.capture_joints(arm, bones, ctx.evaluated_depsgraph_get())
    finally:
        sc.frame_set(saved)
        ctx.view_layer.update()
        key_poses._baking = False
    return out


def far(a, b):
    return max(abs(x - y) for x, y in zip(a, b))


def compare(arm, label):
    """The onion ghosts against the rig now."""
    sc = bpy.context.scene
    shown = key_poses.onion_frames(sc, sc.animatica)
    c = sc.frame_current
    want = [c - 12, c - 8, c - 4, c + 4, c + 8]             # 3 before, 2 after, 4 frames apart
    check(f"{label}: the onion skin shows Before and After, Step apart", shown == want, f"{shown}")
    bones = key_poses.end_bones(arm)
    real = truth(arm, sorted(set(shown) | set(key_poses._onion["cache"])), bones)
    worst, missing = 0.0, []
    for f in shown:
        e = key_poses.onion_entry(f)
        if e is None or f not in key_poses._onion["cache"]:
            missing.append(f)
            continue
        for name, p in (e.get("joints") or {}).items():
            if name in real.get(f, {}):
                worst = max(worst, far(p, real[f][name]))
    check(f"{label}: every ghost on show is the pose now", worst < TOL and not missing,
          f"off by {worst * 100:.2f} cm at worst, {len(missing)} of {len(shown)} not captured again")


def settled():
    return (key_poses._rebuild_requested_at is None and not key_poses._onion["pending"]
            and not key_poses._onion["dirty"])


def wait_settled(timeout=8.0):
    t0 = time.monotonic()
    while not settled() and time.monotonic() - t0 < timeout:
        yield
    # and a beat more: a refill the last draw asked for
    for _ in range(6):
        yield
    while not settled() and time.monotonic() - t0 < timeout:
        yield


def steps():
    arm = setup()
    yield from wait_settled()
    check("the overlay bakes at the start", bool(key_poses._onion["cache"]),
          f"{len(key_poses._onion['cache'])} ghosts")
    compare(arm, "start")

    # 1. a key deleted with nothing else going on
    r, left = delete_keys_at(arm, 33)
    check("Delete in the Dope Sheet removes frame 33's keys", r == {'FINISHED'} and 33 not in left, f"{r} keys at {left}")
    yield from wait_settled()
    compare(arm, "after deleting 33")

    # 2. a key deleted just after the onion skin captured a few frames, while it fills in after
    #    the last edit: its own re-evaluation is ignored for a moment then, and the edit with it
    r, left = delete_keys_at(arm, 9)
    t0 = time.monotonic()
    seen, caught = len(key_poses._onion["cache"]), False
    while time.monotonic() - t0 < 4.0:
        n = len(key_poses._onion["cache"])
        if key_poses._onion["pending"] and n > seen:      # it has just captured some
            caught = True
            break
        seen = n
        yield
    check("caught the onion skin mid-fill, just after a capture", caught)
    r, left = delete_keys_at(arm, 57)               # the last key: the motion ends sooner
    check("Delete removes frame 57's keys mid-fill", r == {'FINISHED'} and 57 not in left, f"{r} keys at {left}")
    yield from wait_settled()
    compare(arm, "after deleting 57 mid-fill")

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
