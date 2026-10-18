"""Copy Motion / Paste Motion on control rigs: the skeleton tree a control rig is sent as, and the
rotations copied off one.

An Auto-Rig Pro rig went out with its hands hanging off the spine, its feet off the root and the
tree upside down (``spine_01.x`` the root, ``root.x`` under it): its deform bones hang off
``c_traj`` and the mechanism chains, and every limb segment is split in two at its twist. The
server's HumanIK could not read that as a body and every paste onto it fell back to the learned
retargeter. And a rotation copied off a control rig was measured against the bone Blender hangs
it from, not the joint above it in the tree sent -- the server chains rotations down that tree,
so ARP's forearms came out 80 degrees off, Rigify's 30.

* ARP: one tree, its root ``root.x``; each arm shoulder -> upper arm -> forearm -> hand, each leg
  hips -> thigh -> shin -> foot -> toes, the neck and head on the spine; the second limb halves
  (``arm_stretch``, ``forearm_twist``, ``thigh_stretch``, ``leg_twist``) not sent, what hung off
  them on the first half; the skeleton sent is that tree;
* Rigify and Mixamo control rigs: the same chains as before;
* Copy off each, posed through its controls: the rotations sent, chained down the tree sent, put
  every joint where the rig has it;
* a server reachable (``RETARGET_SERVER``): a Mixamo clip pasted onto ARP is read by HumanIK, each
  slot on its joint, and the answer on ARP's deform skeleton follows the source's limbs.

Keying ARP's controls from the answer is not covered: the add-on has no Auto-Rig Pro baker yet.

Rigs (each when it is on this machine, read and never written): ``ARP_BLEND``, ``RIGIFY_BLEND``,
``MIXAMO_RIG_BLEND``; the clip ``SOURCE_FBX``. Runs headless; the server part needs
``--online-mode`` for a remote one::

    BLENDER_USER_RESOURCES=$(mktemp -d) blender -b --factory-startup --online-mode \\
        --python tests/test_copy_paste_control_rigs.py
"""
from __future__ import annotations

import math
import os
import random
import sys
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402
from mathutils import Euler, Quaternion, Vector  # noqa: E402

addon_utils.enable("animatica_blender", default_set=False)
from animatica_blender import gltf_to_blender, request_builder, retarget  # noqa: E402

SERVER = os.environ.get("RETARGET_SERVER", "http://127.0.0.1:8766")
SOURCE_FBX = os.environ.get("SOURCE_FBX", os.path.expanduser("~/proscenium/rigs/Scissor Kick.fbx"))
TESTS = os.path.expanduser("~/motion-studio/control-rig-test")
ARP_BLEND = os.environ.get("ARP_BLEND", os.path.join(TESTS, "arp_control_rig.blend"))
RIGIFY_BLEND = os.environ.get("RIGIFY_BLEND", os.path.join(TESTS, "rigify_control_rig.blend"))
MIXAMO_RIG_BLEND = os.environ.get("MIXAMO_RIG_BLEND", os.path.join(TESTS, "mixamo_control_rig.blend"))

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


#: per rig: the file, the armature, the chains (a joint and the ones above it, in order, up to
#: the root), the joints not sent, and the controls a test pose turns (an FK switch set to FK)
RIGS = {
    "Auto-Rig Pro": dict(
        blend=ARP_BLEND, armature="rig", root="root.x",
        chains=[[f"hand.{s}", f"forearm_stretch.{s}", f"c_arm_twist_offset.{s}", f"shoulder.{s}",
                 "spine_02.x", "spine_01.x", "root.x"] for s in "lr"]
        + [[f"toes_01.{s}", f"foot.{s}", f"leg_stretch.{s}", f"thigh_twist.{s}", "root.x"] for s in "lr"]
        + [["head.x", "neck.x", "spine_02.x"]],
        not_sent=[f"{n}.{s}" for n in ("arm_stretch", "forearm_twist", "thigh_stretch", "leg_twist") for s in "lr"],
        switch=("ik_fk_switch", 1.0),
        controls=["c_root_master.x", "c_spine_01.x", "c_spine_02.x", "c_neck.x"]
        + [f"c_{n}_fk.{s}" for n in ("arm", "forearm", "hand", "thigh", "leg", "foot") for s in "lr"]
        + ["c_index1.l", "c_thumb2.r"]),
    "Rigify": dict(
        blend=RIGIFY_BLEND, armature=None, root="DEF-spine",
        chains=[[f"DEF-hand.{s}", f"DEF-forearm.{s}", f"DEF-upper_arm.{s}", f"DEF-shoulder.{s}"] for s in "LR"]
        + [[f"DEF-toe.{s}", f"DEF-foot.{s}", f"DEF-shin.{s}", f"DEF-thigh.{s}", "DEF-spine"] for s in "LR"],
        not_sent=[f"DEF-{n}.{s}.001" for n in ("upper_arm", "forearm", "thigh", "shin") for s in "LR"],
        switch=("IK_FK", 1.0),
        controls=["torso", "spine_fk.001", "neck", "head"]
        + [f"{n}_fk.{s}" for n in ("upper_arm", "forearm", "hand", "thigh", "shin", "foot") for s in "LR"]),
    "Mixamo control rig": dict(
        blend=MIXAMO_RIG_BLEND, armature="Mixamo Rig", root="mixamorig1:Hips",
        chains=[[f"mixamorig1:{s}Hand", f"mixamorig1:{s}ForeArm", f"mixamorig1:{s}Arm", f"mixamorig1:{s}Shoulder"]
                for s in ("Left", "Right")]
        + [[f"mixamorig1:{s}ToeBase", f"mixamorig1:{s}Foot", f"mixamorig1:{s}Leg", f"mixamorig1:{s}UpLeg",
            "mixamorig1:Hips"] for s in ("Left", "Right")],
        not_sent=[], switch=("ik_fk_switch", 1.0),
        controls=["Ctrl_Master", "Ctrl_Hips", "Ctrl_Spine", "Ctrl_Spine1", "Ctrl_Neck", "Ctrl_Head"]
        + [f"Ctrl_{n}_FK_{s}" for n in ("Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot") for s in ("Left", "Right")]
        + ["Ctrl_Index1_Left"]),
}

#: a Mixamo clip's segments and the ARP joints that carry them
SEGMENTS = [("LeftArm", "LeftForeArm", "c_arm_twist_offset.l", "forearm_stretch.l"),
            ("LeftForeArm", "LeftHand", "forearm_stretch.l", "hand.l"),
            ("RightArm", "RightForeArm", "c_arm_twist_offset.r", "forearm_stretch.r"),
            ("RightForeArm", "RightHand", "forearm_stretch.r", "hand.r"),
            ("LeftUpLeg", "LeftLeg", "thigh_twist.l", "leg_stretch.l"),
            ("LeftLeg", "LeftFoot", "leg_stretch.l", "foot.l"),
            ("RightUpLeg", "RightLeg", "thigh_twist.r", "leg_stretch.r"),
            ("RightLeg", "RightFoot", "leg_stretch.r", "foot.r"),
            ("LeftFoot", "LeftToeBase", "foot.l", "toes_01.l"),
            ("Hips", "Neck", "root.x", "neck.x")]


def load_rig(spec):
    """The rig's objects linked into this scene from its file (read, never written)."""
    with bpy.data.libraries.load(spec["blend"]) as (src, dst):
        dst.objects = list(src.objects)
    arm = None
    for o in dst.objects:
        if o is None or o.type in ('CAMERA', 'LIGHT'):
            continue
        bpy.context.scene.collection.objects.link(o)
        if o.type == 'ARMATURE' and (o.name == spec["armature"] or (spec["armature"] is None and "rig_id" in o.data)):
            arm = o
    return arm


def chain_up(tree, joint):
    out = [joint]
    while tree.get(out[-1]) is not None and len(out) < 64:
        out.append(tree[out[-1]])
    return out


def pose_controls(arm, spec, frames, seed=3):
    """Key the rig's controls (FK) at random turns on ``frames``, as an animator would."""
    key, value = spec["switch"]
    for pb in arm.pose.bones:
        if key in pb.keys():
            pb[key] = value
    ad = arm.animation_data or arm.animation_data_create()
    ad.action = bpy.data.actions.new(f"{arm.name} posed")
    rng = random.Random(seed)
    for f in frames:
        for n in spec["controls"]:
            pb = arm.pose.bones[n]
            e = Euler([math.radians(rng.uniform(-35, 35)) for _ in range(3)])
            if pb.rotation_mode == 'QUATERNION':
                pb.rotation_quaternion = e.to_quaternion()
                pb.keyframe_insert("rotation_quaternion", frame=f)
            else:
                pb.rotation_euler = e
                pb.keyframe_insert("rotation_euler", frame=f)
    return ad.action


def chained_miss(clip):
    """The furthest any joint lands from where the rig has it, the rotations chained down the
    tree from the root's position (metres), and that joint."""
    joints = clip["skeleton"]["joints"]
    idx = {j["name"]: i for i, j in enumerate(joints)}
    worst, at = 0.0, None
    for rots, pos in zip(clip["rotations"], clip["joint_positions"]):
        world = {}
        for j in joints:                                # parents come first
            x, y, z, w = rots[idx[j["name"]]]
            r = Quaternion((w, x, y, z)).to_matrix()
            if j["parent"] is None:
                world[j["name"]] = (r, Vector(pos[idx[j["name"]]]))
            else:
                pr, pp = world[j["parent"]]
                world[j["name"]] = (pr @ r, pp + pr @ Vector(j["rest_translation"]))
            d = (world[j["name"]][1] - Vector(pos[idx[j["name"]]])).length
            if d > worst:
                worst, at = d, j["name"]
    return worst, at


def deform_copy(arm, tree):
    """ARP's deform skeleton alone, as the request sent it: the tree's bones, parented that way,
    nothing driving them -- what the server's answer is keyed on."""
    plain = arm.copy()
    plain.data = arm.data.copy()
    plain.name = f"{arm.name} deform"
    bpy.context.scene.collection.objects.link(plain)
    plain.animation_data_clear()
    plain.data.animation_data_clear()
    for pb in plain.pose.bones:
        for c in list(pb.constraints):
            pb.constraints.remove(c)
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    plain.select_set(True)
    bpy.context.view_layer.objects.active = plain
    bpy.ops.object.mode_set(mode='EDIT')
    eb = plain.data.edit_bones
    for b in eb:
        if b.name in tree:
            b.use_connect = False
            b.parent = eb.get(tree[b.name]) if tree[b.name] else None
    for b in [b for b in eb if b.name not in tree]:
        eb.remove(b)
    bpy.ops.object.mode_set(mode='OBJECT')
    for b in plain.data.bones:                         # ARP's feet do not inherit their parent's turn
        b.use_inherit_rotation = True
        b.inherit_scale = 'FULL'
    return plain


def suffix(arm, name):
    return next(pb for pb in arm.pose.bones if pb.name.split(":")[-1] == name)


def main():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    made = {}
    for label, spec in RIGS.items():
        if not os.path.exists(spec["blend"]):
            print(f"SKIP {label}: no {spec['blend']}")
            continue
        try:
            arm = load_rig(spec)
            made[label] = arm
            tree = request_builder.request_tree(arm)
            roots = [n for n, p in tree.items() if p is None]
            check(f"{label}: one tree, its root {spec['root']}", roots == [spec["root"]], f"roots {roots}")
            for chain in spec["chains"]:
                got = chain_up(tree, chain[0])[:len(chain)]
                check(f"{label}: {' <- '.join(chain)}", got == chain, f"got {' <- '.join(got)}")
            sent = [n for n in spec["not_sent"] if n in tree]
            check(f"{label}: the second limb halves are not sent", not sent, f"{sent}")
            skel = {j["name"]: j["parent"] for j in request_builder.armature_to_skeleton(arm)["joints"]}
            check(f"{label}: the skeleton sent is that tree", skel == tree, f"{len(skel)} joints")

            frames = (1, 6, 11)
            action = pose_controls(arm, spec, frames)
            clip = retarget.sample_motion(arm, action, (frames[0], frames[-1]))
            miss, at = chained_miss(clip)
            check(f"{label}: copied off its controls, the rotations chained down the tree put every joint "
                  f"where the rig has it", miss < 1e-3, f"furthest {miss * 100:.2f} cm ({at})")
            arm.animation_data.action = None
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append(f"{label}: raised")

    arp = made.get("Auto-Rig Pro")
    try:
        urllib.request.urlopen(f"{SERVER}/health", timeout=10).read()
        online = True
    except Exception as e:                              # noqa: BLE001
        print(f"SKIP the server part: no motion server at {SERVER} ({e})")
        online = False
    if arp is not None and online and os.path.exists(SOURCE_FBX):
        try:
            before = set(bpy.data.objects)
            bpy.ops.import_scene.fbx(filepath=SOURCE_FBX)
            src = max((o for o in bpy.data.objects if o not in before and o.type == 'ARMATURE'),
                      key=lambda o: len(o.data.bones))
            src.location.x = -2.0
            bpy.context.view_layer.objects.active = src
            copied = retarget.copy(src)
            gltf = retarget.send_retarget(SERVER, retarget.build_retarget_request(copied["clip"], arp))
            report = retarget.retarget_report(gltf)
            check("pasted onto ARP: HumanIK reads it", report.get("method") == "hik", f"method {report.get('method')}")
            mapping = {s.split(":")[-1]: t for s, t in (report.get("mapping") or {}).items()}
            want = {a: ta for a, _b, ta, _tb in SEGMENTS} | {b: tb for _a, b, _ta, tb in SEGMENTS}
            wrong = {s: (mapping.get(s), t) for s, t in want.items() if mapping.get(s) != t}
            check("...each slot on its joint", not wrong, f"{wrong}" if wrong else f"{len(want)} joints")

            keys = sorted(copied["keys"])
            frames = list(range(keys[0], keys[-1] + 1, max(1, (keys[-1] - keys[0]) // 40)))
            plain = deform_copy(arp, request_builder.request_tree(arp))
            gltf_to_blender.bake_gltf_to_armature(gltf, plain, action_name="answer", start_frame=keys[0])
            cos = {}
            for f in frames:
                bpy.context.scene.frame_set(f)
                bpy.context.view_layer.update()
                for a, b, ta, tb in SEGMENTS:
                    u = suffix(src, b).head - suffix(src, a).head
                    v = plain.pose.bones[tb].head - plain.pose.bones[ta].head
                    u, v = src.matrix_world.to_3x3() @ u, plain.matrix_world.to_3x3() @ v
                    cos.setdefault(f"{a}>{b}", []).append(u.normalized().dot(v.normalized()))
            means = {k: sum(v) / len(v) for k, v in cos.items()}
            arms = [means[k] for k in list(means)[:4]]
            check("...the answer's arms follow the source's", min(arms) > 0.98,
                  ", ".join(f"{k} {v:.3f}" for k, v in list(means.items())[:4]))
            # the legs a little less: ARP's root turns at the spine base, a hand's width above the hips
            check("...and its legs, feet and spine", min(means.values()) > 0.9,
                  ", ".join(f"{k} {v:.3f}" for k, v in list(means.items())[4:]))
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append("server: raised")
    print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
