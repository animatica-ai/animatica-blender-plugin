"""Keep, in a real Blender window: a take is judged on the floating bar with
Keep, Redo and Discard, and Keep sets it in stone -- its motion stays, its
blocks lock, and the next Generate leaves them alone.

The server is stood in for by a stub whose every take moves differently, so a
block made again shows it: its keys change, and a kept block's don't.

* A take waiting for review: Keep (the main button), Redo, Discard, and no
  Generate; the hint names the three; Keep's tooltip says how many blocks lock.
* Generate while the take waits makes it again and keeps nothing.
* Keep: the take is the character's own action, its blocks lock, nothing is
  left to generate.
* A kept block unlocked and Generated again: only it is made again; the kept
  block's keys are exactly as kept.
* Redo of a kept block does nothing.

Run (a window opens and closes itself; the report lands in
``tests/out/gui_keep``)::

    blender --factory-startup --python tests/test_gui_keep.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
for p in (ROOT, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import addon_utils  # noqa: E402
import bpy  # noqa: E402
from mathutils import Quaternion  # noqa: E402

import test_bake_object_rotation as synthetic  # noqa: E402
from test_gui_bar_batch import CAPS, bar, click, override, s, select, tip  # noqa: E402

OUT = os.path.join(TESTS, "out", "gui_keep")
os.makedirs(OUT, exist_ok=True)
WAIT_SECONDS = 60.0
KEEP_OPEN = "--keep-open" in sys.argv

A = (1, 6)          # the first block's frames
B = (7, 12)         # the second's
REQUESTS: list = []
REPORT: dict = {"checks": []}
FAILS: list = []


def take(n_frames: int, phase: float) -> dict:
    """A take of *n_frames* that moves differently for every *phase*."""
    rot = {j: [] for j in synthetic.JOINTS}
    pos = {"Hips": []}
    q = synthetic.to_xyzw
    for t in range(n_frames):
        rot["Hips"].append(q(Quaternion((0.0, 1.0, 0.0), 0.4 + phase + 0.05 * t)))
        rot["Spine"].append(q(Quaternion((0.0, 0.0, 1.0), 0.3 * math.sin(0.5 * t + phase))))
        swing = 0.3 * math.sin(0.6 * t + phase)
        rot["LeftUpLeg"].append(q(Quaternion((1.0, 0.0, 0.0), -0.4 + swing)))
        rot["LeftLeg"].append(q(Quaternion((1.0, 0.0, 0.0), 0.7)))
        rot["LeftFoot"].append(q(Quaternion((1.0, 0.0, 0.0), -0.2)))
        rot["RightUpLeg"].append(q(Quaternion((1.0, 0.0, 0.0), -0.4 - swing)))
        rot["RightLeg"].append(q(Quaternion((1.0, 0.0, 0.0), 0.5)))
        rot["RightFoot"].append(q(Quaternion((0.0, 1.0, 0.0), 0.1)))
        pos["Hips"].append((0.05 * t + phase, 1.0, 0.03 * t))
    return synthetic.make_gltf(rot, pos)


def stub_generate(_client, body):
    """As many frames as asked for, moving differently every time."""
    REQUESTS.append(body)
    time.sleep(0.3)
    n = int(body.get("duration_frames") or sum(seg.get("duration_frames", 0)
                                               for seg in body.get("segments") or ()))
    return take(max(n, 2), 0.7 * len(REQUESTS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check(name, ok, info="") -> None:
    REPORT["checks"].append({"name": name, "ok": bool(ok), "info": str(info)})
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def wait_for(pred, what: str):
    t0 = time.time()
    while not pred():
        if time.time() - t0 > WAIT_SECONDS:
            raise TimeoutError(f"gave up waiting for {what}")
        yield


def set_blocks(arm) -> None:
    from animatica_blender import properties
    st = s()
    st.target_armature = arm
    st.prompt_blocks.clear()
    for (lo, hi), prompt in ((A, "walks"), (B, "waves")):
        b = st.prompt_blocks.add()
        b.prompt, b.frame_start, b.frame_end, b.enabled, b.seed = prompt, lo, hi, True, 0
    st.active_block_index = 0
    properties.save_blocks_to_armature(arm, st)


def locks() -> list:
    return [bool(b.locked) for b in s().prompt_blocks]


def sample(arm, frames) -> dict:
    """Every channel of the character's action at *frames*."""
    from animatica_blender.constraints_ui import iter_action_fcurves
    action = arm.animation_data.action
    return {(fc.data_path, fc.array_index, f): fc.evaluate(f)
            for fc in iter_action_fcurves(action) for f in frames}


def biggest_change(before: dict, after: dict) -> float:
    return max((abs(after[k] - v) for k, v in before.items() if k in after), default=0.0)


def hint() -> dict:
    from animatica_blender import guidance
    with bpy.context.temp_override(**override()):
        return guidance.next_step(bpy.context) or {}


def generate(how: str = "bar"):
    if how == "bar":
        click("generate")
    else:                                   # the bar's operator, called as a key or F3 would
        with bpy.context.temp_override(**override()):
            bpy.ops.animatica.toolbar_generate('INVOKE_DEFAULT')
    yield from wait_for(lambda: not s().is_generating, "the take")


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

def scenario():
    from animatica_blender import request_builder

    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = A[0], B[1]
    ann = synthetic.make_rig("Ann")
    set_blocks(ann)
    select([ann], ann)

    # -- a take waiting: Keep, Redo, Discard, no Generate
    scene.frame_set(3)
    yield from generate()
    check("the take waits for review", s().is_previewing and locks() == [False, False], locks())
    items = bar()
    check("review: Keep is the main button, with Redo and Discard",
          "keep_take" in items and items["keep_take"].primary and items["keep_take"].label == "Keep"
          and {"redo", "reject"} <= set(items), sorted(items))
    check("review: no Generate until the take is kept", "generate" not in items, sorted(items))
    h = hint()
    check("the hint names Keep, Redo and Discard",
          h.get("id") == "review" and h.get("text", "").startswith("Judge the take: Keep keeps it"), h.get("text"))
    check("the hint no longer says there is nothing to accept", "nothing to accept" not in h.get("why", ""),
          h.get("why"))
    line = tip("keep_take")
    check("Keep's tooltip says how many blocks lock", "2 blocks lock" in line, line)

    # -- Generate while it waits makes it again, keeping nothing
    first = sample(ann, range(A[0], B[1] + 1))
    n = len(REQUESTS)
    yield from generate("operator")
    check("Generate while the take waits makes it again", len(REQUESTS) == n + 1 and s().is_previewing,
          f"{len(REQUESTS) - n} request(s), previewing={s().is_previewing}")
    check("…and keeps nothing: the blocks stay unlocked", locks() == [False, False], locks())
    check("…with new motion", biggest_change(first, sample(ann, range(A[0], B[1] + 1))) > 1e-3)

    # -- Keep: the take is the character's own action, its blocks locked
    click("keep_take")
    act = ann.animation_data.action
    check("Keep ends the review", not s().is_previewing)
    check("Keep: the take is the character's own action",
          act is not None and not act.name.startswith("Animatica_Motion"), act and act.name)
    check("Keep: both blocks lock", locks() == [True, True], locks())
    check("Keep: nothing is left for Generate",
          request_builder.generation_blocks(s().prompt_blocks, scene) == [])
    h = hint()
    check("on the kept take, the hint says the block is kept",
          h.get("id") == "finetune" and "kept take" in h.get("text", ""), h.get("text"))

    # -- unlock B and Generate: only B is made again, A stays exactly as kept
    kept_a = sample(ann, range(A[0], A[1]))           # A's frames, short of the seam
    kept_b = sample(ann, range(B[0] + 1, B[1] + 1))
    s().prompt_blocks[1].locked = False
    scene.frame_set(9)
    n = len(REQUESTS)
    yield from generate()
    body = REQUESTS[-1] if len(REQUESTS) > n else {}
    check("Generate after unlocking B asks for B alone",
          len(REQUESTS) == n + 1 and int(body.get("duration_frames") or 0) <= B[1] - B[0] + 2,
          body.get("duration_frames"))
    check("B's take waits for review, A stays kept", s().is_previewing and locks() == [True, False], locks())
    drift = biggest_change(kept_a, sample(ann, range(A[0], A[1])))
    check("the kept block's motion is exactly as kept", drift < 1e-5, f"max change {drift:.2e}")
    check("B was made again", biggest_change(kept_b, sample(ann, range(B[0] + 1, B[1] + 1))) > 1e-3)

    click("keep_take")
    check("Keep B: both kept again", not s().is_previewing and locks() == [True, True], locks())

    # -- with everything kept: no Redo, and Generate says why it waits
    scene.frame_set(3)
    items = bar()
    with bpy.context.temp_override(**override()):
        can_redo = bpy.ops.animatica.regenerate_block.poll()
    check("a kept take offers no Redo", "redo" not in items and not can_redo, sorted(items))
    gen = items.get("generate")
    check("Generate waits, saying every block is kept",
          gen is not None and not gen.enabled and "Every block is kept" in tip("generate"),
          gen and (gen.enabled, tip("generate")))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run():
    from animatica_blender import mmcp_client
    bpy.context.preferences.use_preferences_save = False
    mmcp_client.MmcpClient.generate = stub_generate
    mmcp_client.store_capabilities(CAPS)
    mmcp_client.needs_sign_in = lambda: False
    s().model_id = "kimodo-soma-rp"
    synthetic.reset_scene()
    gen = scenario()

    def tick():
        try:
            next(gen)
        except StopIteration:
            return finish()
        except Exception:                  # noqa: BLE001
            FAILS.append("error")
            REPORT["error"] = traceback.format_exc()
            print(traceback.format_exc())
            return finish()
        return 0.2

    bpy.app.timers.register(tick, first_interval=1.0)


def finish():
    REPORT["summary"] = {"passed": sum(c["ok"] for c in REPORT["checks"]), "failed": len(FAILS)}
    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(REPORT, f, indent=1, default=str)
    print(f"{REPORT['summary']['passed']} passed, {len(FAILS)} failed -> {OUT}")
    if not KEEP_OPEN:
        bpy.ops.wm.quit_blender()
    return None


if __name__ == "__main__":
    bpy.context.preferences.view.show_splash = False
    addon_utils.enable("animatica_blender", default_set=True)
    run()
