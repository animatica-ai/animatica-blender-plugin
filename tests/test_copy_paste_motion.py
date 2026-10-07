"""Copy Motion / Paste Motion: keys copied off one character and pasted onto another through
the server's ``POST /retarget``, end to end -- sampled off the source, sent, put into the
target's own animation.

* nothing selected, every key is copied; pasted on the playhead, each target's limbs follow the
  source's (the direction of every upper arm, forearm, thigh, shin and the spine, frame by frame,
  mean cosine), into its own action;
* the keys selected are what is copied -- three keys, or one (a pose) -- and pasted, a key on
  each, the first on the playhead, the pose on each the source's;
* the target's own keys outside the pasted stretch stay as they were, the ones inside go, and a
  joint the retarget did not reach (a finger) keeps its keys;
* a bone animated in Euler gets Euler keys, its rotation mode unchanged.

Source: a Mixamo animation (``SOURCE_FBX``). Targets: the Animatic character, a Mixamo Y Bot
(``MIXAMO_FBX``) and a Rigify control rig (``RIGIFY_BLEND``), each when it is on this machine.

Needs a motion server offering ``POST /retarget`` (``RETARGET_SERVER``, default
http://127.0.0.1:8766; the Modal app's URL needs ``--online-mode``), and the Animatic character
in Blender's data (read, never fetched)::

    BUR=$(mktemp -d); mkdir -p $BUR/datafiles
    ln -s "$HOME/Library/Application Support/Blender/5.1/datafiles/animatica" $BUR/datafiles/
    BLENDER_USER_RESOURCES=$BUR blender -b --factory-startup --python tests/test_copy_paste_motion.py
"""
from __future__ import annotations

import os
import sys
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=True)
from animatica_blender import canonical_skeleton, constraints_ui, retarget  # noqa: E402

SERVER = os.environ.get("RETARGET_SERVER", "http://127.0.0.1:8766")
SOURCE_FBX = os.environ.get("SOURCE_FBX", os.path.expanduser("~/proscenium/rigs/Scissor Kick.fbx"))
MIXAMO_FBX = os.environ.get("MIXAMO_FBX", os.path.expanduser("~/proscenium/rigs/Y Bot (2).fbx"))
RIGIFY_BLEND = os.environ.get("RIGIFY_BLEND", os.path.expanduser("~/motion-studio/autoposer-pro-ph/assets/rig_rigify.blend"))

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


#: the segments compared, per naming: (from, to) joint names
SEGMENTS = {
    "mixamo": [("{p}LeftArm", "{p}LeftForeArm"), ("{p}LeftForeArm", "{p}LeftHand"),
               ("{p}RightArm", "{p}RightForeArm"), ("{p}RightForeArm", "{p}RightHand"),
               ("{p}LeftUpLeg", "{p}LeftLeg"), ("{p}LeftLeg", "{p}LeftFoot"),
               ("{p}RightUpLeg", "{p}RightLeg"), ("{p}RightLeg", "{p}RightFoot"), ("{p}Hips", "{p}Neck")],
    "animatic": [("{p}LeftArm", "{p}LeftForeArm"), ("{p}LeftForeArm", "{p}LeftHand"),
                 ("{p}RightArm", "{p}RightForeArm"), ("{p}RightForeArm", "{p}RightHand"),
                 ("{p}LeftLeg", "{p}LeftShin"), ("{p}LeftShin", "{p}LeftFoot"),
                 ("{p}RightLeg", "{p}RightShin"), ("{p}RightShin", "{p}RightFoot"), ("{p}Hips", "{p}Neck1")],
    "rigify": [("DEF-upper_arm.L", "DEF-forearm.L"), ("DEF-forearm.L", "DEF-hand.L"),
               ("DEF-upper_arm.R", "DEF-forearm.R"), ("DEF-forearm.R", "DEF-hand.R"),
               ("DEF-thigh.L", "DEF-shin.L"), ("DEF-shin.L", "DEF-foot.L"),
               ("DEF-thigh.R", "DEF-shin.R"), ("DEF-shin.R", "DEF-foot.R"), ("DEF-spine", "DEF-spine.004")],
}


def bone(arm, name):
    b = arm.pose.bones.get(name)
    if b is None:                                  # a namespaced rig: match the name's end
        b = next((pb for pb in arm.pose.bones if pb.name.split(":")[-1] == name.split(":")[-1]), None)
    return b


def directions(arm, kind, prefix, frames):
    out = []
    for f in frames:
        bpy.context.scene.frame_set(f)
        bpy.context.view_layer.update()
        row = []
        for a, b in SEGMENTS[kind]:
            pa, pb = bone(arm, a.format(p=prefix)), bone(arm, b.format(p=prefix))
            if pa is None or pb is None:
                row.append(None)
                continue
            v = (arm.matrix_world @ pb.head) - (arm.matrix_world @ pa.head)
            row.append(v.normalized() if v.length > 1e-6 else None)
        out.append(row)
    return out


def import_fbx(path, keep_anim):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.fbx(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    arm = max((o for o in new if o.type == 'ARMATURE'), key=lambda o: len(o.data.bones))
    if not keep_anim:
        for o in new:
            if o.animation_data is not None:
                o.animation_data_clear()
    return arm


def rigify_rig():
    with bpy.data.libraries.load(RIGIFY_BLEND) as (src, dst):
        dst.objects = [n for n in src.objects]
    arm = None
    for o in dst.objects:
        if o is None or o.type in ('CAMERA', 'LIGHT'):
            continue
        bpy.context.scene.collection.objects.link(o)
        if o.type == 'ARMATURE' and o.name.startswith("rigify_rig"):
            arm = o
    return arm


def keys_of(arm, action=None):
    """Every keyed frame on the rig's action."""
    action = action or arm.animation_data.action
    return sorted({int(round(kp.co.x)) for fc in constraints_ui.iter_action_fcurves(action, arm)
                   for kp in fc.keyframe_points})


def select_keys(arm, frames):
    """Select exactly the keys on ``frames`` (none, for an empty list), as a click in the Timeline does."""
    for fc in constraints_ui.iter_action_fcurves(arm.animation_data.action, arm):
        for kp in fc.keyframe_points:
            on = int(round(kp.co.x)) in frames
            kp.select_control_point = kp.select_left_handle = kp.select_right_handle = on


def follows(src, want_frames, arm, kind, prefix, got_frames):
    want = directions(src, "mixamo", "mixamorig:", want_frames)
    got = directions(arm, kind, prefix, got_frames)
    cos = [w.dot(g) for rw, rg in zip(want, got) for w, g in zip(rw, rg) if w is not None and g is not None]
    return sum(cos) / max(1, len(cos)), len(cos)


def curve(arm, path, index=0):
    return next((fc for fc in constraints_ui.iter_action_fcurves(arm.animation_data.action, arm)
                 if fc.data_path == path and fc.array_index == index), None)


def main():
    try:
        urllib.request.urlopen(f"{SERVER}/health", timeout=10).read()
    except Exception as e:                              # noqa: BLE001
        print(f"SKIP: no motion server at {SERVER} ({e})")
        return 0
    if not os.path.exists(SOURCE_FBX):
        print(f"SKIP: no source animation at {SOURCE_FBX}")
        return 0
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    scene = bpy.context.scene
    src = import_fbx(SOURCE_FBX, keep_anim=True)
    src.location.x = -2.0                                # stood apart: the target is placed where it is
    src_keys = keys_of(src)
    f0, f1 = src_keys[0], src_keys[-1]
    frames = list(range(f0, f1 + 1, max(1, (f1 - f0) // 24)))
    bpy.context.view_layer.objects.active = src
    select_keys(src, [])
    copied = retarget.copy(src)
    check("nothing selected: every key is copied", sorted(copied["keys"]) == src_keys and copied["whole"],
          f"{len(copied['keys'])} keys, {len(copied['clip']['rotations'])} frames sampled, "
          f"{len(copied['clip']['skeleton']['joints'])} joints")

    # a take's keys all selected (as a bake leaves them), Pose mode, one bone selected, and a click
    # on one frame in the Timeline -- which shows that bone's keys only, and changes only those
    from animatica_blender._bake_common import bone_of
    select_keys(src, src_keys)
    bpy.ops.object.mode_set(mode='POSE')
    head = next(pb.name for pb in src.pose.bones if pb.name.endswith(":Head"))
    for pb in src.pose.bones:
        pb.select = pb.name == head
    for fc in constraints_ui.iter_action_fcurves(src.animation_data.action, src):
        if bone_of(fc.data_path) == head:
            for kp in fc.keyframe_points:
                kp.select_control_point = int(round(kp.co.x)) == src_keys[20]
    keys, picked = retarget.selected_keys(src, src.animation_data.action)
    check("a Timeline click on one frame copies that frame, not the take's other selected keys",
          sorted(keys) == [src_keys[20]] and picked, f"{len(keys)} frames")
    bpy.ops.object.mode_set(mode='OBJECT')

    targets = [("Animatic", "animatic", "animatica:", lambda: canonical_skeleton.load_character(bpy.context)[0])]
    if os.path.exists(MIXAMO_FBX):
        targets.append(("Mixamo Y Bot", "mixamo", "mixamorig:", lambda: import_fbx(MIXAMO_FBX, keep_anim=False)))
    if os.path.exists(RIGIFY_BLEND):
        targets.append(("Rigify", "rigify", "", rigify_rig))
    made = {}
    for label, kind, prefix, make in targets:
        try:
            arm = make()
            arm.location.x += 1.0
            made[label] = (arm, kind, prefix)
            had = bpy.data.actions.new(f"{label} own")
            arm.animation_data_create()
            arm.animation_data.action = had
            scene.frame_set(f0)
            done = retarget.paste(arm, SERVER)
            mean, n = follows(src, frames, arm, kind, prefix, frames)
            check(f"{label}: the limbs follow the source", mean > 0.93, f"mean cos {mean:.3f} over {n} segment-frames")
            got = keys_of(arm)
            check(f"{label}: a key on each copied key's frame", got == src_keys, f"{got[:1]}..{got[-1:]} ({len(got)})")
            check(f"{label}: into its own action", arm.animation_data.action == had and done["action"] == had)
            print(f"     {label}: {done['joints']} joints written, {done['kept']} kept their own")
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append(f"{label}: raised")

    # the selection: three keys, pasted at frame 200 onto the Animatic, over keys of its own
    arm, kind, prefix = made.get("Animatic", (None, None, None))
    if arm is not None:
        try:
            picked = [src_keys[10], src_keys[30], src_keys[50]]
            select_keys(src, picked)
            bpy.context.view_layer.objects.active = src
            retarget.copy(src)
            label = retarget.clipboard_label()
            check("three keys selected: those are copied", sorted(retarget.clipboard()["keys"]) == picked, label)
            mine = bpy.data.actions.new("Animatic blocking")
            arm.animation_data.action = mine
            hips = next(pb for pb in arm.pose.bones if pb.name.split(":")[-1] == "Hips")
            finger = next(pb for pb in arm.pose.bones if pb.name.split(":")[-1] == "LeftHandIndex1")
            for f, x in ((150, 0.1), (210, 0.2), (300, 0.3)):   # its own keys: before, inside, after
                hips.location = (x, 0.0, 0.0)
                hips.keyframe_insert("location", frame=f)
                hips.keyframe_insert("rotation_quaternion", frame=f)
            finger.rotation_mode = 'QUATERNION'
            finger.rotation_quaternion = (0.9, 0.0, 0.0, 0.43)
            finger.keyframe_insert("rotation_quaternion", frame=210)
            done = retarget.paste(arm, SERVER, at_frame=200)
            d = [p - picked[0] for p in picked]
            want = [200 + x for x in d]
            check("pasted with a key on each, the first on the playhead", done["frames"] == want, f"{done['frames']}")
            hw = curve(arm, hips.path_from_id("rotation_quaternion"), 0)
            got = sorted(int(round(kp.co.x)) for kp in hw.keyframe_points) if hw else []
            check("its own keys outside the stretch stay, the one inside goes (the hips)",
                  got == [150] + want + [300], f"keys {got}")
            hx = curve(arm, hips.path_from_id("location"), 0)
            check("...unchanged", hx is not None and abs(hx.evaluate(150) - 0.1) < 1e-6 and abs(hx.evaluate(300) - 0.3) < 1e-6,
                  f"{hx.evaluate(150):.3f}, {hx.evaluate(300):.3f}" if hx else "no curve")
            fq = curve(arm, finger.path_from_id("rotation_quaternion"), 0)
            check("a finger the retarget did not reach keeps its keys", fq is not None and
                  any(int(round(kp.co.x)) == 210 for kp in fq.keyframe_points), f"{done['kept']} joints kept")
            mean, n = follows(src, picked, arm, kind, prefix, want)
            check("the pose on each pasted key is the source's", mean > 0.93, f"mean cos {mean:.3f} over {n}")
            check("the action is still its own", arm.animation_data.action == mine)

            # one key: a pose
            select_keys(src, [src_keys[40]])
            retarget.copy(src)
            done = retarget.paste(arm, SERVER, at_frame=400)
            check("one key selected: one pose pasted, on the playhead", done["frames"] == [400],
                  retarget.clipboard_label())
            mean, n = follows(src, [src_keys[40]], arm, kind, prefix, [400])
            check("...the source's pose", mean > 0.93, f"mean cos {mean:.3f} over {n}")
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append("selection: raised")

    # a rig animated in Euler: Euler keys, its rotation mode kept
    arm, kind, prefix = made.get("Mixamo Y Bot", (None, None, None))
    if arm is not None:
        try:
            for pb in arm.pose.bones:
                pb.rotation_mode = 'XYZ'
            arm.animation_data.action = bpy.data.actions.new("Y Bot euler")
            select_keys(src, [])
            retarget.copy(src)
            done = retarget.paste(arm, SERVER, at_frame=f0)
            modes = {pb.rotation_mode for pb in arm.pose.bones}
            paths = {fc.data_path.rsplit(".", 1)[-1] for fc in constraints_ui.iter_action_fcurves(arm.animation_data.action, arm)}
            check("Euler bones: rotation mode kept", modes == {'XYZ'}, f"{modes}")
            check("...keyed in Euler", "rotation_euler" in paths and "rotation_quaternion" not in paths, f"{sorted(paths)}")
            mean, n = follows(src, frames, arm, kind, prefix, frames)
            check("...the limbs follow the source", mean > 0.93, f"mean cos {mean:.3f} over {n}")
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append("euler: raised")
    print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
