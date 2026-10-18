"""The body shape Paste Motion sends (``body_shape``): spheres inside the character's skin, each on
a joint the request carries, so the server can keep the limbs out of the body.

* on a Mixamo character and a Rigify control rig: every sphere is on a joint the request sends,
  its centre is inside the skin and its radius at least the smallest sent; the torso, head, arms
  and legs all have some; fitting takes under two seconds (about half a second on an idle Mac);
* the request carries ``target.body`` for a skinned character, none for a bare armature;
* a server that refuses the field (a schema error naming it) gets the request again without it,
  and an unrelated error still comes through.

Runs headless, without a server::

    BLENDER_USER_RESOURCES=$(mktemp -d) blender -b --factory-startup --python tests/test_body_shape.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402

addon_utils.enable("animatica_blender", default_set=False)
from animatica_blender import body_shape, coords, mmcp_client, request_builder, retarget  # noqa: E402

MIXAMO_FBX = os.environ.get("MIXAMO_FBX", os.path.expanduser("~/proscenium/rigs/Y Bot (2).fbx"))
RIGIFY_BLEND = os.environ.get("RIGIFY_BLEND", os.path.expanduser("~/motion-studio/control-rig-test/rigify_control_rig.blend"))

FAILS: list[str] = []


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def clear():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)


def mixamo():
    bpy.ops.import_scene.fbx(filepath=MIXAMO_FBX)
    arm = max((o for o in bpy.data.objects if o.type == 'ARMATURE'), key=lambda o: len(o.data.bones))
    if arm.animation_data:
        arm.animation_data.action = None
    return arm


def rigify():
    with bpy.data.libraries.load(RIGIFY_BLEND) as (src, dst):
        dst.objects = list(src.objects)
    arm = None
    for o in dst.objects:
        if o is None or o.type in ('CAMERA', 'LIGHT'):
            continue
        bpy.context.scene.collection.objects.link(o)
        if o.type == 'ARMATURE' and "rig_id" in o.data:
            arm = o
    return arm


def parts(names):
    def kind(n):
        n = n.lower()
        if "hand" in n or "forearm" in n or "arm" in n:
            return "arm"
        if "leg" in n or "thigh" in n or "shin" in n or "foot" in n:
            return "leg"
        if "head" in n or "neck" in n or n.endswith(("spine.005", "spine.006")):   # Rigify's neck, head
            return "head"
        return "torso"
    return {kind(n) for n in names}


def spheres_are_right(label, arm):
    skel = request_builder.armature_to_skeleton(arm)
    sent = {j["name"] for j in skel["joints"]}
    t0 = time.time()
    sph = body_shape.body_spheres(arm, skel)
    secs = time.time() - t0
    check(f"{label}: spheres on the body", len(sph) >= 40, f"{len(sph)} on {len({s['joint'] for s in sph})} joints")
    check(f"{label}: every sphere on a joint the request sends", all(s["joint"] in sent for s in sph))
    check(f"{label}: radii at least {body_shape.MIN_RADIUS} m", all(s["radius"] >= body_shape.MIN_RADIUS for s in sph))
    check(f"{label}: torso, head, arms and legs all have spheres",
          parts({s["joint"] for s in sph}) >= {"arm", "leg", "head", "torso"}, f"{sorted(parts({s['joint'] for s in sph}))}")
    check(f"{label}: fitted in under two seconds", secs < 2.0, f"{secs:.2f} s")
    # each centre inside the skin, in the rest pose
    meshes = body_shape.skinned_meshes(arm)
    _verts, bvhs = body_shape._rest_vertices(arm, meshes, with_bvh=True)
    inside = body_shape._Inside(meshes, bvhs)
    mw = arm.matrix_world
    outside = []
    for s in sph:
        head = mw @ arm.data.bones[s["joint"]].head_local
        a, b, c = s["center"]                      # MMCP (x, y, z) -> Blender (x, -z, y)
        if not inside(head + Vector((a, -c, b))):
            outside.append(s["joint"])
    check(f"{label}: every centre inside the skin", not outside, f"{len(outside)} outside: {outside[:4]}")
    return sph


def main():
    if os.path.exists(MIXAMO_FBX):
        try:
            clear()
            arm = mixamo()
            spheres_are_right("Mixamo", arm)
            req = retarget.build_retarget_request({"skeleton": {"joints": []}}, arm)
            check("the request carries the body", len(req["target"].get("body", {}).get("spheres", [])) >= 40)
            plain = retarget.build_retarget_request({"skeleton": {"joints": []}}, arm, body=False)
            check("...and not when asked without", "body" not in plain["target"])
            # a bare armature: no skin, no body
            for o in [o for o in bpy.data.objects if o.type == 'MESH']:
                bpy.data.objects.remove(o, do_unlink=True)
            bare = retarget.build_retarget_request({"skeleton": {"joints": []}}, arm)
            check("a bare armature goes without a body", "body" not in bare["target"])
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append("Mixamo: raised")
    else:
        print(f"SKIP Mixamo: no {MIXAMO_FBX}")
    if os.path.exists(RIGIFY_BLEND):
        try:
            clear()
            spheres_are_right("Rigify", rigify())
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append("Rigify: raised")
    else:
        print(f"SKIP Rigify: no {RIGIFY_BLEND}")

    # the fallback: a server from before body shapes refuses the field
    sent = []
    real = mmcp_client.MmcpClient.retarget

    def refusing(self, body):
        sent.append("body" in body["target"])
        if "body" in body["target"]:
            raise mmcp_client.MmcpError(code="schema_validation", message="invalid retarget request", status=400,
                                        details={"errors": [{"loc": ["target", "body"], "type": "extra_forbidden"}]})
        return {"ok": True}

    def failing(self, body):
        sent.append("body" in body["target"])
        raise mmcp_client.MmcpError(code="internal_error", message="boom", status=500)

    req = {"target": {"skeleton": {}, "body": {"spheres": [{"joint": "x", "center": [0, 0, 0], "radius": 0.1}]}}}
    try:
        mmcp_client.MmcpClient.retarget = refusing
        out = retarget.send_retarget("http://127.0.0.1:9", req)
        check("a server refusing the body gets the request again without it", out == {"ok": True} and sent == [True, False],
              f"sent {sent}")
        sent.clear()
        mmcp_client.MmcpClient.retarget = failing
        try:
            retarget.send_retarget("http://127.0.0.1:9", req)
            check("an unrelated error still comes through", False, "no error")
        except mmcp_client.MmcpError as exc:
            check("an unrelated error still comes through", exc.code == "internal_error" and sent == [True], f"sent {sent}")
    finally:
        mmcp_client.MmcpClient.retarget = real
    print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
