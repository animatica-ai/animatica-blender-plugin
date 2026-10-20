"""Redo of a block with Loop and In place on.

A looped take is one block, so its Redo is the whole take's Generate
(``request_builder.redo_makes_loop``). A block Redo of an in-place take puts
the travel back before the request, and takes it out again after the splice
with original keys of the new take; with no splice, the take is left as it
was. Generate again over a take on a rig with no action of its own sends the
artist's edits alone, not the old take's keys.

A SOMA30 rig built from the bundled joints, its Hips keyed travelling, stands
in for the take's preview; the splice is stood in for by keys written into
the block.

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_redo_loop_in_place.py

``--factory-startup`` keeps an installed copy of the addon from shadowing
the checkout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=False)

from animatica_blender import inplace, operators, request_builder  # noqa: E402
from animatica_blender import timeline_operators as T  # noqa: E402

FAILS: list[str] = []
LOOPS = {"supports_loop": True}


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def clear_scene():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)


# --- 1. redo_makes_loop -----------------------------------------------------

def set_blocks(s, specs):
    """Real prompt-block items, so blocks are told apart by their pointers."""
    s.prompt_blocks.clear()
    for prompt, fs, fe in specs:
        b = s.prompt_blocks.add()
        b.prompt, b.frame_start, b.frame_end = prompt, fs, fe
        b.enabled = True


def loop_decision():
    scene = bpy.context.scene
    s = scene.animatica

    def makes(idx, caps=LOOPS):
        return request_builder.redo_makes_loop(s, s.prompt_blocks, scene, caps, idx)

    s.loop = True
    set_blocks(s, [("walks", 1, 60)])
    check("loop, one block, the model loops: Redo is the take's", makes(0) is True)

    set_blocks(s, [("walks", 1, 30), ("runs", 31, 60)])
    check("two blocks with text: Redo is the block's", makes(0) is False and makes(1) is False)

    set_blocks(s, [("walks", 1, 30), ("", 31, 60)])
    check("the block with text, next to an empty one, is the loop", makes(0) is True)
    check("...the empty one is not", makes(1) is False)

    set_blocks(s, [("walks", 1, 60)])
    s.loop = False
    check("Loop off: Redo is the block's", makes(0) is False)
    s.loop = True
    check("the model doesn't loop: Redo is the block's", makes(0, caps={}) is False)
    check("no model: Redo is the block's", makes(0, caps=None) is False)

    s.prompt_blocks[0].enabled = False
    check("the block is disabled: Redo is the block's", makes(0) is False)
    check("a block index out of range", makes(5) is False)

    s.prompt_blocks.clear()
    s.loop = False


# --- 2./3. In place round trip --------------------------------------------

def build_take():
    """A SOMA30 rig, its Hips travelling 1 m/s over frames 1-60, In place on;
    two prompt blocks (1-30, 31-60), as a preview would have them."""
    clear_scene()
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 60
    s = scene.animatica
    s.inplace = True
    r = bpy.ops.animatica.import_canonical_skeleton(source='DEFAULT', with_body=False)
    assert r == {'FINISHED'}, r
    arm = next(o for o in scene.objects if o.type == 'ARMATURE')
    top = next(pb for pb in arm.pose.bones if pb.parent is None)
    act = bpy.data.actions.new("Animatica_Motion_test")
    arm.animation_data_create()
    arm.animation_data.action = act
    arm["animatica_pending_block_ranges"] = json.dumps([[1, 30], [31, 60]])
    key_travel(top, 1, 60, speed=1.0, start=0.0)
    bpy.context.view_layer.update()
    operators._apply_inplace_constraint(arm, enabled=True)
    return scene, arm, top, act


def key_travel(top, lo, hi, speed, start):
    """Hips location keys: forward along the bone's local X and Z (both on the
    floor for an upright Hips), a small bob on its Y."""
    fps = 30.0
    for f in range(lo, hi + 1):
        t = (f - lo) / fps
        d = start + speed * t
        top.location = (d * 0.8, 0.02 * ((f % 10) / 10.0), d * 0.6)
        top.keyframe_insert("location", frame=f)
        top.keyframe_insert("rotation_quaternion", frame=f)


def root_keys(action, bone):
    return {f"{p}|{i}": [tuple(k.co) for k in fc.keyframe_points]
            for (p, i), fc in inplace._root_curves(action, bone).items()}


def max_diff(a, b):
    if set(a) != set(b):
        return float("inf")
    m = 0.0
    for k in a:
        if len(a[k]) != len(b[k]):
            return float("inf")
        for (x0, y0), (x1, y1) in zip(a[k], b[k]):
            m = max(m, abs(x0 - x1), abs(y0 - y1))
    return m


def hips_drift(scene, arm, top, lo, hi):
    """How far the Hips wander on the floor over frames lo..hi, in metres."""
    keep = scene.frame_current
    xy = []
    for f in range(lo, hi + 1):
        scene.frame_set(f)
        p = arm.matrix_world @ top.head
        xy.append((p.x, p.y))
    scene.frame_set(keep)
    x0, y0 = xy[0]
    return max(((x - x0) ** 2 + (y - y0) ** 2) ** 0.5 for x, y in xy)


def round_trip_spliced():
    scene, arm, top, act = build_take()
    s = scene.animatica
    check("In place is on the take before the Redo", inplace.applied_mode(act) == "in_place")
    act["animatica_loop"] = {"start": 1.0, "cut": 60.0}

    was = T._regen_travel_back(arm, act)
    check("the travel goes back before the request", was is True and inplace.applied_mode(act) is None)

    # The splice, stood in for: block 31-60 comes back travelling twice as
    # fast, from where the take was at frame 30.
    at30 = 29 / 30.0
    key_travel(top, 31, 60, speed=2.0, start=at30 + 1 / 30.0)
    new_travel = root_keys(act, top.name)

    T._regen_after(arm, act, s, spliced=True)
    check("In place is on the new take", inplace.applied_mode(act) == "in_place")
    drift = hips_drift(scene, arm, top, 31, 60)
    check("...the new block stays in place (Hips drift < 5 cm)", drift < 0.05, f"{drift * 100:.2f} cm")
    saved = {k: [(p[0], p[1]) for p in v]
             for k, v in json.loads(act[inplace.BACKUP_KEY])["keys"].items()}
    d = max_diff({k: new_travel[k] for k in saved}, saved) if saved else float("inf")
    check("...its original keys are the new travel", d < 1e-6, f"{d:.2e}")
    check("the loop marker is dropped after the splice", "animatica_loop" not in act)


def round_trip_failed():
    scene, arm, top, act = build_take()
    s = scene.animatica
    act["animatica_loop"] = {"start": 1.0, "cut": 60.0}
    before = root_keys(act, top.name)

    was = T._regen_travel_back(arm, act)
    T._regen_after(arm, act, s, spliced=False)
    d = max_diff(before, root_keys(act, top.name))
    check("no splice: In place was taken off", was is True)
    check("...and is back on", inplace.applied_mode(act) == "in_place")
    check("...the root keys are as they were", d < 1e-5, f"{d:.2e}")
    check("...the loop marker stays", "animatica_loop" in act)


# --- 4. Generate again over a take on a rig with no action of its own ------

def own_take_scratch():
    """The request for a take replaced whole is made from the artist's edits
    on it alone (operators._own_take_request_action): none of the take's keys,
    cycle or In place go into it."""
    from animatica_blender import constraints_ui, preview_session
    scene, arm, top, act = build_take()
    act["animatica_loop"] = {"start": 1.0, "cut": 60.0}
    for fc in constraints_ui.iter_action_fcurves(act):
        fc.modifiers.new('CYCLES')
        for kp in fc.keyframe_points:
            kp.type = 'GENERATED'
    preview_session.record_baseline(act)

    states = preview_session.travel_edits(arm, act)
    check("no edits on the take: nothing of the artist's to send",
          operators._own_take_request_action(act, states) is None)

    pb = next(p for p in arm.pose.bones if p.parent is not None)
    pb.rotation_mode = 'QUATERNION'
    scene.frame_set(20)
    pb.rotation_quaternion = (0.9, 0.3, 0.2, 0.1)
    pb.keyframe_insert("rotation_quaternion", frame=20)
    keys_before = sum(len(fc.keyframe_points) for fc in constraints_ui.iter_action_fcurves(act))
    states = preview_session.travel_edits(arm, act)
    scratch = operators._own_take_request_action(act, states)
    check("an edit on the take: a scratch action", scratch is not None)
    if scratch is None:
        return
    kps = [(fc.data_path, int(round(kp.co.x)), kp.type)
           for fc in constraints_ui.iter_action_fcurves(scratch) for kp in fc.keyframe_points]
    check("...holding the edit alone", kps and all(f == 20 and pb.name in p for p, f, _ in kps),
          f"{len(kps)} keys")
    check("...typed the artist's", all(t != 'GENERATED' for _, _, t in kps))
    check("...with none of the take's cycle, record or In place",
          not any(fc.modifiers for fc in constraints_ui.iter_action_fcurves(scratch))
          and not any(k in scratch for k in ("animatica_loop", inplace.BACKUP_KEY, inplace.APPLIED_KEY)))
    keys_after = sum(len(fc.keyframe_points) for fc in constraints_ui.iter_action_fcurves(act))
    check("...and the take itself untouched", keys_after == keys_before
          and inplace.applied_mode(act) == "in_place" and "animatica_loop" in act)
    bpy.data.actions.remove(scratch)


def main():
    loop_decision()
    round_trip_spliced()
    round_trip_failed()
    own_take_scratch()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        FAILS.append("exception")
    print("FAILED: " + ", ".join(FAILS) if FAILS else "all passed")
    sys.exit(1 if FAILS else 0)
