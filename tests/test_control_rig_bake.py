"""A take on a control rig, keyed on its controls: Generate, Discard, a key pose read off it,
Generate Pose and Regenerate Block, on Rigify, Auto-Rig Pro and a Mixamo Rig.

A take used to reach the screen on a Mixamo Rig only. On Rigify it went through the metarig,
whose limb bones Rigify rolls differently (the arms came out held out in front, 61 cm off), and
with the metarig hidden -- as it usually is -- nothing was baked: the character stood still. On
Auto-Rig Pro, read as Rigify, nothing was keyed but its deform bones: the hips walked 5 m and the
hands and feet stayed where they started. Now (control_bake) the answer is put on the rig's FK
controls frame by frame, each limb's IK matched to the same pose.

Each server answer is recorded (``REPLIES``: one per rig, a walk); what it means is read off a
plain copy of the rig's deform skeleton -- the tree the request sent, nothing driving it -- with
the answer baked on as on any plain rig. Per rig:

* Generate (the take Generate makes: ``operators.bake_take``): every joint sent lands within
  1 cm of the answer on every frame looked at, and travels as far; a hand pose the animator gave
  the finger controls stays (the answer's fingers are at rest: the model has none); a rig with a profile
  (Rigify, Auto-Rig Pro) has keys on its controls only, its bones' rotation modes untouched,
  each limb in FK over the take and back in the mode it had either side of it where the rig's
  own animation goes on; a limb's IK
  matched (Rigify's): switched to IK mid-take, the hand or foot stays put;
* a key pose read off the take: each joint's rotation against its parent in the tree sent
  is the answer's own (it was read against Blender's parent: ARP's arms 80-90 degrees off);
* Discard: the switches back as they were;
* Generate Pose on a frame of no take: that frame's pose, exactly; Set Key Pose sees a pose there
  (not the rest pose) and keys the controls;
* Regenerate Block: the block's frames the new answer's, the frames either side the take's;
* In place: the travel out through the rig's top control, the body's motion kept, and back;
* Loop (the server's cycle): made a cycle on the controls, held on the spot by In place, and
  walking on a cycle's travel a cycle with it off;
* Paste Motion (the server's answer as if retargeted): each copied key's pose on the controls
  (Auto-Rig Pro's finger controls are deform bones too, and were skipped), the limbs' mode
  before the pasted stretch as it was, and no key after it where the action has nothing.

Rigs and answers, each when it is on this machine (read, never written): ``RIGIFY_BLEND``,
``ARP_BLEND``, ``ARP_HERO_BLEND``, ``MIXAMO_RIG_BLEND``, ``REPLIES``. Runs headless::

    BLENDER_USER_RESOURCES=$(mktemp -d) blender -b --factory-startup \\
        --python tests/test_control_rig_bake.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=False)
from animatica_blender import (constraints_ui, gltf_to_blender, operators, pose_edit,  # noqa: E402
                               preview_session, request_builder)

TESTS = os.path.expanduser("~/motion-studio/control-rig-test")
REPLIES = os.environ.get("REPLIES", os.path.join(TESTS, "choreo", "replies"))
RIGS = {
    "Rigify": dict(blend=os.environ.get("RIGIFY_BLEND", os.path.join(TESTS, "rigify_control_rig.blend")),
                   reply="rigify.json", profile="Rigify", ik_matched=True,
                   ends=["DEF-hand.L", "DEF-foot.R"], tol=0.01,
                   spine=[f"DEF-spine.{i:03d}" for i in range(1, 7)] + ["DEF-spine"], finger="f_index.02.L",
                   ik_hand="hand_ik.L", loop_reply="rigify-loop.json"),
    "Auto-Rig Pro": dict(blend=os.environ.get("ARP_BLEND", os.path.join(TESTS, "arp_control_rig.blend")),
                         reply="arp.json", profile="Auto-Rig Pro", ik_matched=True,
                         ends=["hand.l", "foot.r"], tol=0.005, finger="c_index2.l", ik_hand="c_hand_ik.l",
                         loop_reply="arp-loop.json"),
    "Auto-Rig Pro (Animatica's character)": dict(
        blend=os.environ.get("ARP_HERO_BLEND", os.path.join(TESTS, "arp_hero.blend")),
        reply="arp-hero.json", profile="Auto-Rig Pro", ik_matched=True, ends=["hand.l", "foot.r"], tol=0.005,
        ik_hand="c_hand_ik.l"),
    "Mixamo Rig": dict(blend=os.environ.get("MIXAMO_RIG_BLEND", os.path.join(TESTS, "mixamo_control_rig.blend")),
                       reply="mixamo.json", profile=None, ik_matched=False,
                       ends=["mixamorig1:LeftHand", "mixamorig1:RightFoot"], tol=0.02),
}

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)


def load_rig(path):
    """The file's objects linked into this scene; the armature its skin follows."""
    with bpy.data.libraries.load(path) as (src, dst):
        dst.objects = list(src.objects)
    for o in dst.objects:
        if o is not None and o.type not in ('CAMERA', 'LIGHT'):
            bpy.context.scene.collection.objects.link(o)
    skinned = {m.object for o in dst.objects if o is not None and o.type == 'MESH'
               for m in o.modifiers if m.type == 'ARMATURE' and m.object is not None}
    arm = max(skinned, key=lambda a: len(a.data.bones))
    if arm.animation_data is not None:
        arm.animation_data.action = None
    return arm


def answer_copy(arm, tree, gltf):
    """The rig's deform skeleton alone, as the request sent it -- the tree's bones, hung that
    way, plain inheritance (the server chains every turn down the tree: Auto-Rig Pro's feet
    inherit none), nothing driving them -- with the answer baked on as on any plain rig."""
    plain = arm.copy()
    plain.data = arm.data.copy()
    plain.name = f"{arm.name} answer"
    bpy.context.scene.collection.objects.link(plain)
    plain.animation_data_clear()
    plain.data.animation_data_clear()
    for pb in plain.pose.bones:
        for c in list(pb.constraints):
            pb.constraints.remove(c)
    select(plain)
    bpy.ops.object.mode_set(mode='EDIT')
    eb = plain.data.edit_bones
    for b in eb:
        b.use_connect = False
        b.use_inherit_rotation = True
        b.inherit_scale = 'FULL'
        b.use_local_location = True
    for b in eb:
        if b.name in tree:
            b.parent = eb.get(tree[b.name]) if tree[b.name] else None
    for b in [b for b in eb if b.name not in tree]:
        eb.remove(b)
    bpy.ops.object.mode_set(mode='OBJECT')
    gltf_to_blender.bake_gltf_to_armature(gltf, plain, action_name="answer", start_frame=1)
    plain.hide_set(True)
    return plain


def select(ob):
    for o in bpy.context.view_layer.objects:
        o.select_set(o is ob)
    bpy.context.view_layer.objects.active = ob


def heads(ob, names):
    bpy.context.view_layer.update()
    return {n: ob.matrix_world @ ob.pose.bones[n].head for n in names if n in ob.pose.bones}


def miss(arm, ref, names, frames, ref_frame=lambda f: f):
    """The furthest any of ``names`` lands from the answer over ``frames`` (metres), and where."""
    worst, at = 0.0, None
    scene = bpy.context.scene
    for f in frames:
        scene.frame_set(ref_frame(f))
        want = heads(ref, names)
        scene.frame_set(f)
        got = heads(arm, names)
        for n in want:
            d = (got[n] - want[n]).length
            if d > worst:
                worst, at = d, f"{n} at {f}"
    return worst, at


def keyed_bones(action, arm):
    return {fc.data_path.split('"')[1] for fc in gltf_to_blender._bake_common.action_fcurves(action, arm) or ()
            if fc.data_path.startswith('pose.bones["')}


def switches(arm, prof):
    from animatica_blender import control_bake
    return control_bake.switches(arm, prof) if prof is not None else {}


def generate(arm, gltf, first, last, source=None):
    s = bpy.context.scene.animatica
    s.target_armature = arm
    s.prompt_blocks.clear()
    b = s.prompt_blocks.add()
    b.prompt, b.frame_start, b.frame_end = "a person walks forward", first, last
    select(arm)
    bpy.ops.object.mode_set(mode='POSE')
    # as Generate bakes it: the take's keys typed GENERATED (no key poses in it), the rig's own
    # action its source
    action, _skipped = operators.bake_take(bpy.context, s, arm, gltf, prompt_blocks=s.prompt_blocks,
                                           gen_start=first, gen_end=last, anchor_frames=set(),
                                           source_action_name=source.name if source else "")
    return action


def run(label, spec):
    clear()
    scene = bpy.context.scene
    arm = load_rig(spec["blend"])
    gltf = json.load(open(os.path.join(REPLIES, spec["reply"])))
    n = gltf_to_blender.sample_frame_count(gltf) or 90
    first, last = 1, n
    tree = request_builder.request_tree(arm)
    ref = answer_copy(arm, tree, gltf)
    prof = gltf_to_blender.control_profile(arm)
    # the body: the answer has no fingers, and a rig's own hand pose stays on its finger controls
    names = list(tree)
    if prof is not None:
        from animatica_blender import control_bake
        ride = control_bake.riders(arm, prof, tree)
        hand = {ride[b][0] for side in ("Left", "Right") for chain in prof.finger_chains(arm, side)
                for b in chain if b in ride}
        names = [n for n in names if n not in hand]
    check(f"{label}: its profile", (prof.name if prof else None) == spec["profile"],
          f"{prof.name if prof else None}")
    modes = {pb.name: pb.rotation_mode for pb in arm.pose.bones}
    before = switches(arm, prof)
    # a hand pose on a finger control, unkeyed, as an animator leaves one
    finger = spec.get("finger")
    if finger:
        from mathutils import Euler
        fpb = arm.pose.bones[finger]
        fpb.rotation_mode = 'XYZ'
        fpb.rotation_euler = Euler((0.5, 0.0, 0.0))
        modes[finger] = 'XYZ'
        curl = fpb.matrix_basis.copy()

    # the rig's own animation, either side of where the take goes: an IK hand keyed in IK
    own = None
    if spec.get("ik_hand"):
        own = bpy.data.actions.new(f"{arm.name} own")
        arm.animation_data_create()
        arm.animation_data.action = own
        hpb = arm.pose.bones[spec["ik_hand"]]
        for f in (first - 10, last + 20):
            hpb.keyframe_insert("location", frame=f)

    # Generate
    action = generate(arm, gltf, first, last, own)
    frames = list(range(first, last + 1, 3))
    worst, at = miss(arm, ref, names, frames)
    check(f"{label}: Generate puts every joint where the answer has it", worst < spec["tol"],
          f"furthest {worst * 100:.2f} cm ({at})")
    root = next(n for n, p in tree.items() if p is None)
    scene.frame_set(first)
    a0, r0 = heads(arm, [root])[root], heads(ref, [root])[root]
    scene.frame_set(last)
    a1, r1 = heads(arm, [root])[root], heads(ref, [root])[root]
    check(f"{label}: ...and it travels as far", abs((a1 - a0).length - (r1 - r0).length) < 0.01,
          f"{(a1 - a0).length:.2f} m vs {(r1 - r0).length:.2f} m")
    if prof is not None:
        controls = {c for c, _b in prof.controls(arm)}
        stray = sorted(keyed_bones(action, arm) & (set(tree) - controls))
        check(f"{label}: ...keyed on its controls, none on its deform bones", not stray, f"{stray[:6]}")
        changed = sorted(n for n, m in modes.items() if arm.pose.bones[n].rotation_mode != m)
        check(f"{label}: ...its bones' rotation modes untouched", not changed, f"{changed[:6]}")
        if finger:
            scene.frame_set((first + last) // 2)
            off = max(abs(a - b) for ra, rb in zip(fpb.matrix_basis, curl) for a, b in zip(ra, rb))
            check(f"{label}: ...the hand pose on its finger controls kept (the answer has no fingers)",
                  off < 1e-4, f"{finger} {off:.5f}")
        scene.frame_set((first + last) // 2)
        mid = switches(arm, prof)
        check(f"{label}: ...each limb in FK over the take",
              all(abs(v - prof._switch(arm, *k)[2]) < 0.5 for k, v in mid.items()) and bool(mid), f"{mid}")
        for f in (first - 1, last + 1):
            scene.frame_set(f)
            out = switches(arm, prof)
            check(f"{label}: ...in the mode it had either side of it, where its own animation is (frame {f})",
                  all(abs(out[k] - before[k]) < 1e-6 for k in before), f"{out} vs {before}")
        if spec["ik_matched"]:
            f = (first + last) // 2
            scene.frame_set(f)
            fk = heads(arm, spec["ends"])
            for limb, side in prof.limbs(arm):
                prof.set_ik(arm, limb, side, True)
            ik = heads(arm, spec["ends"])
            gap = max((ik[n] - fk[n]).length for n in fk)
            check(f"{label}: ...each limb's IK matched: switched to IK mid-take, nothing moves", gap < 0.01,
                  f"{gap * 100:.2f} cm")
            scene.frame_set(f + 1)

        # In place: the travel out through the rig's top control, the body's own motion kept
        hips = root
        foot = spec["ends"][1]
        scene.frame_set(first)
        start = heads(arm, [hips])[hips]
        scene.frame_set(last)
        walked = heads(arm, [hips, foot])
        operators._apply_inplace_constraint(arm, enabled=True)
        scene.frame_set(last)
        stayed = heads(arm, [hips, foot])
        drift = ((stayed[hips] - start).to_2d()).length
        check(f"{label}: In place takes the travel out", drift < 0.3,
              f"the hips {drift * 100:.0f} cm from where they began, walked {((walked[hips] - start).to_2d()).length:.2f} m")
        shape = abs((stayed[foot] - stayed[hips]).length - (walked[foot] - walked[hips]).length)
        check(f"{label}: ...the body's own motion kept", shape < 0.01, f"{shape * 100:.2f} cm")
        operators._apply_inplace_constraint(arm, enabled=False)
        worst, at = miss(arm, ref, names, range(first, last + 1, 9))
        check(f"{label}: ...and off again, the travel back", worst < spec["tol"], f"furthest {worst * 100:.2f} cm ({at})")

        # a key pose read off the take: each turn against the joint above it in the tree sent
        f = (first + last) // 2
        scene.frame_set(f)
        bpy.context.view_layer.update()
        parents = constraints_ui._request_parents(arm)
        pose = constraints_ui._request_pose(arm)
        turns = {}
        for n in names:
            got = constraints_ui._evaluated_local_basis(arm.pose.bones[n], parents[n], pose).to_quaternion()
            want = ref.pose.bones[n].matrix_basis.to_quaternion()
            turns[n] = math.degrees(got.rotation_difference(want).angle)
        # Rigify's FK spine pivots off its joints: it shows the pose a few mm and degrees short
        spine = {n: d for n, d in turns.items() if n in spec.get("spine", ())}
        rest = {n: d for n, d in turns.items() if n not in spine}
        at = max(rest, key=rest.get)
        check(f"{label}: a key pose read off the take is the answer's own turns", rest[at] < 1.0,
              f"furthest {rest[at]:.2f} deg ({at})")
        if spine:
            at = max(spine, key=spine.get)
            check(f"{label}: ...its FK spine's within 5 degrees", spine[at] < 5.0, f"furthest {spine[at]:.2f} deg ({at})")

        # Regenerate Block: the block's frames from another stretch of an answer, the rest kept
        w0, w1, shift = first + 30, first + 50, 25
        scene.frame_set(w0 - 5)
        keep = heads(arm, names)
        gltf_to_blender.splice_gltf_into_action(gltf, arm, action, request_start_frame=first - shift,
                                                target_range=(w0, w1))
        worst, at = miss(arm, ref, names, range(w0, w1 + 1, 2), ref_frame=lambda f: f + shift)
        check(f"{label}: Regenerate Block: the block's frames are the new answer's", worst < spec["tol"],
              f"furthest {worst * 100:.2f} cm ({at})")
        scene.frame_set(w0 - 5)
        now = heads(arm, names)
        moved = max((now[n] - keep[n]).length for n in keep)
        check(f"{label}: ...the frames before it as they were", moved < 1e-4, f"{moved * 100:.3f} cm")
        scene.frame_set((w0 + w1) // 2)
        mid = switches(arm, prof)
        check(f"{label}: ...its limbs still in FK", all(abs(v - prof._switch(arm, *k)[2]) < 0.5 for k, v in mid.items()),
              f"{mid}")

    # Discard: the rig as it was
    preview_session.finish(bpy.context, arm, accepted=False)
    if prof is not None:
        back = switches(arm, prof)
        check(f"{label}: Discard puts the switches back", all(abs(back[k] - before[k]) < 1e-6 for k in before),
              f"{back} vs {before}")

    # Generate Pose on a frame of no take, then Set Key Pose there
    if prof is not None:
        ad = arm.animation_data
        ad.action = bpy.data.actions.new(f"{arm.name} poses")
        target = 20
        gltf_to_blender.bake_single_pose(gltf, arm, source_frame=40, target_frame=target, root_translation="full")
        worst, at = miss(arm, ref, names, [target], ref_frame=lambda f: 41)
        check(f"{label}: Generate Pose puts that frame's pose on it", worst < spec["tol"],
              f"furthest {worst * 100:.2f} cm ({at})")
        scene.frame_set(target)
        check(f"{label}: ...Set Key Pose sees a pose there, not the rest pose", not pose_edit.is_rest_pose(arm))
        paths = {p.split('"')[1] for p, _i, _v in pose_edit.pose_channels(arm)}
        controls = {c for c, _b in prof.controls(arm)}
        check(f"{label}: ...and keys the controls", bool(paths) and not (paths & (set(tree) - controls)),
              f"{len(paths)} bones, {sorted(paths & (set(tree) - controls))[:4]}")

        # Paste Motion: a clip's keys (every 6th frame) pasted from frame 101, the answer as if
        # the server had retargeted it onto this rig
        from animatica_blender import retarget
        keys = list(range(first, last + 1, 6))
        copied = {"sampled": list(range(first, last + 1)), "keys": {f: 'KEYFRAME' for f in keys}}
        shift = 100
        scene.frame_set(first)
        before = switches(arm, prof)
        done = retarget.bake_result(copied, gltf, arm, first + shift)
        worst, at = miss(arm, ref, names, [f + shift for f in keys[1:-1]], ref_frame=lambda f: f - shift)
        check(f"{label}: Paste Motion puts every copied key's pose on its controls", worst < spec["tol"],
              f"furthest {worst * 100:.2f} cm ({at}); {done['joints']} bones written")
        fingers = {c for c, _b in prof.controls(arm) if c in tree}
        if fingers:
            got = keyed_bones(done["action"], arm) & fingers
            check(f"{label}: ...its finger controls too", got == fingers, f"{len(got)} of {len(fingers)}")
        scene.frame_set(first + shift - 1)
        out = switches(arm, prof)
        check(f"{label}: ...the limbs in the mode they had before the paste (frame {first + shift - 1})",
              all(abs(out[k] - before[k]) < 1e-6 for k in before), f"{out} vs {before}")
        frames_keyed = sorted({round(kp.co.x) for fc in gltf_to_blender._bake_common.action_fcurves(done["action"], arm)
                               for kp in fc.keyframe_points})
        check(f"{label}: ...and no key after it, where the action has nothing to play",
              frames_keyed[-1] == done["frames"][-1], f"last key {frames_keyed[-1]}, last pasted {done['frames'][-1]}")


def run_loop(label, spec):
    """A looped take (the server's cycle): made a cycle on the controls, held on the spot by In
    place, and travelling a cycle's length a cycle with it off."""
    clear()
    scene = bpy.context.scene
    arm = load_rig(spec["blend"])
    gltf = json.load(open(os.path.join(REPLIES, spec["loop_reply"])))
    s = scene.animatica
    s.target_armature = arm
    s.loop = True                       # and In place with it, as Loop sets it
    s.inplace = True
    s.prompt_blocks.clear()
    b = s.prompt_blocks.add()
    b.prompt, b.frame_start, b.frame_end = "a person walks forward", 1, 60
    select(arm)
    bpy.ops.object.mode_set(mode='POSE')
    action, _ = operators.bake_take(bpy.context, s, arm, gltf, prompt_blocks=s.prompt_blocks, gen_start=1,
                                    gen_end=60, anchor_frames=set(), server_looped=True)
    looped = dict(action.get("animatica_loop") or {})
    check(f"{label}: a looped take is made a cycle", bool(looped), f"{looped}")
    if not looped:
        return
    first, cut = int(looped["start"]), int(looped["cut"])
    tree = request_builder.request_tree(arm)
    names = [n for n in tree if not any(k in n for k in ("f_", "thumb", "palm", "index", "middle", "ring", "pinky"))]
    hips = next(n for n, p in tree.items() if p is None)
    scene.frame_set(first)
    a = heads(arm, names)
    scene.frame_set(cut)
    c = heads(arm, names)
    seam = max(((c[n] - c[hips]) - (a[n] - a[hips])).length for n in names)
    check(f"{label}: ...its last frame is its first again (the pose)", seam < 0.03, f"{seam * 100:.1f} cm")
    held = (c[hips] - a[hips]).to_2d().length
    check(f"{label}: ...held on the spot by In place", held < 0.2, f"{held * 100:.0f} cm over a cycle")
    s.inplace = False
    operators._apply_inplace_constraint(arm, enabled=False)
    scene.frame_set(first)
    a = heads(arm, [hips])[hips]
    scene.frame_set(cut)
    c = heads(arm, [hips])[hips]
    scene.frame_set(cut + (cut - first))
    d = heads(arm, [hips])[hips]
    one, two = (c - a).to_2d().length, (d - a).to_2d().length
    check(f"{label}: ...and with In place off it walks on, cycle after cycle", one > 1.0 and abs(two - 2 * one) < 0.2,
          f"{one:.2f} m a cycle, {two:.2f} m two")


def main():
    if not os.path.isdir(REPLIES):
        print(f"SKIP: no answers in {REPLIES}")
        return 0
    for label, spec in RIGS.items():
        if not os.path.exists(spec["blend"]) or not os.path.exists(os.path.join(REPLIES, spec["reply"])):
            print(f"SKIP {label}: no {spec['blend']} or its answer")
            continue
        try:
            run(label, spec)
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append(f"{label}: raised")
        if spec.get("loop_reply") and os.path.exists(os.path.join(REPLIES, spec["loop_reply"])):
            try:
                run_loop(label, spec)
            except Exception:                           # noqa: BLE001
                traceback.print_exc()
                FAILS.append(f"{label}: loop raised")
    print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
