"""What the artist made of each take reaches the server, against the take's job,
in a real Blender window: the bar's buttons pressed the way a click presses
them (``animatica.bar_click``), the server stood in for by a stub that names a
job per request, the verdicts caught where they would go out.

* A take remembers its job (the server's ``X-Job-Id``) on its preview session,
  with the version showing and every version looked at.
* Redo of a block over a take nobody decided on: that take was ``regenerated``.
  Flipping to a version of the take before the Redo makes it that take again,
  and the Redo's take was regenerated in turn.
* Discard: ``rejected``, with the version showing, the versions seen, and the
  keys the artist changed first.
* The take kept by fine-tuning it (``keep_take``, which Marionette's first edit
  calls): ``kept``. Keep on the bar: ``accepted``. Keep All of a batch:
  ``accepted``, each character against its own job.
* Generate over a take nobody kept: that take was ``regenerated``.
* Nothing goes out with "Share how I judge takes" off, self-hosted, signed out,
  or from a duplicate's copy of another character's take.

Run (a window opens and closes itself; the report lands in
``tests/out/gui_feedback``)::

    blender --factory-startup --python tests/test_gui_feedback.py
"""

from __future__ import annotations

import copy
import json
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

import test_bake_object_rotation as synthetic  # noqa: E402
from test_gui_bar_batch import CAPS, TAKE, click, override, s, select, set_prompt  # noqa: E402

OUT = os.path.join(TESTS, "out", "gui_feedback")
os.makedirs(OUT, exist_ok=True)
WAIT_SECONDS = 60.0
KEEP_OPEN = "--keep-open" in sys.argv

JOBS: list[str] = []
SENT: list[dict] = []
REPORT: dict = {"checks": []}
FAILS: list = []


def _next_job() -> str:
    JOBS.append(f"job-{len(JOBS) + 1}")
    return JOBS[-1]


def stub_generate(client, body):
    """A job per request, as Animatica Cloud names one (``X-Job-Id``)."""
    time.sleep(0.3)
    client.last_job_id = _next_job()
    asked = int((body.get("options") or {}).get("num_samples") or 1)
    return copy.deepcopy(TAKE if asked > 1 else synthetic.GLTF)


def stub_generate_batch(client, bodies):
    out = [stub_generate(client, b) for b in bodies]
    client.last_job_ids = JOBS[-len(bodies):]
    return out


def stub_post(url, headers, body):
    """Where the verdict would go out: caught instead."""
    SENT.append({"url": url, "headers": headers, "body": json.loads(body)})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def prefs():
    return bpy.context.preferences.addons["animatica_blender"].preferences


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


def job_on(arm) -> dict | None:
    from animatica_blender import preview_session
    return (preview_session.get(arm) or {}).get("job")


def sent_since(n: int) -> list:
    return [(m["url"].rsplit("/jobs/", 1)[-1].split("/")[0], m["body"]) for m in SENT[n:]]


def generate():
    click("generate")
    yield from wait_for(lambda: not s().is_generating, "the take")


def edit_one_key(arm) -> None:
    """The artist moves one key of the take: one bone's rotation on one frame."""
    action = arm.animation_data.action
    from animatica_blender.constraints_ui import iter_action_fcurves
    fc = next(fc for fc in iter_action_fcurves(action) if fc.data_path.endswith("rotation_quaternion"))
    kp = fc.keyframe_points[len(fc.keyframe_points) // 2]
    kp.co.y += 0.2
    kp.handle_left.y += 0.2
    kp.handle_right.y += 0.2
    fc.update()


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

def scenario():
    from animatica_blender import batch, feedback, operators

    bpy.context.scene.frame_start, bpy.context.scene.frame_end = synthetic.FRAMES[0], synthetic.FRAMES[-1]
    ann = synthetic.make_rig("Ann")
    bob = synthetic.make_rig("Bob")
    bob.location.x += 1.5
    for arm, prompt in ((ann, "walks"), (bob, "waves")):
        set_prompt(arm, prompt)
    select([ann], ann)

    # -- when a verdict may go out at all
    check("signed in, on Animatica Cloud, sharing on: verdicts go out", feedback.enabled())
    prefs().share_feedback = False
    check("Share how I judge takes off: nothing goes out", not feedback.enabled())
    prefs().share_feedback = True
    prefs().self_hosted = True
    check("self-hosted: nothing goes out", not feedback.enabled())
    prefs().self_hosted = False
    token, prefs().access_token = prefs().access_token, ""
    check("signed out: nothing goes out", not feedback.enabled())
    prefs().access_token = token

    # -- a take remembers its job, the version showing and the versions seen
    s().variations = 2
    yield from generate()
    check("the take remembers its job", job_on(ann) == {"id": "job-1", "samples": 2, "sample": 0, "seen": [0]},
          job_on(ann))
    check("nothing is judged yet", not SENT, SENT)
    click("var_1")
    check("a version looked at is remembered", job_on(ann) == {"id": "job-1", "samples": 2, "sample": 1,
                                                              "seen": [0, 1]}, job_on(ann))

    # -- Redo of the block over it: job-1 was regenerated
    bpy.context.scene.frame_set(synthetic.FRAMES[len(synthetic.FRAMES) // 2])
    n = len(SENT)
    # the bar's Redo asks for the block's seed in a dialog; its OK runs this
    with bpy.context.temp_override(**override()):
        bpy.ops.animatica.regenerate_block('EXEC_DEFAULT', block_index=0)
    yield from wait_for(lambda: not s().is_generating, "the block Redo")
    yield from wait_for(lambda: len(SENT) > n, "the verdict on the take redone")
    check("Redo of a block: the take it replaced was regenerated",
          sent_since(n) == [("job-1", {"verdict": "regenerated", "sample_index": 1, "seen_samples": [0, 1]})],
          sent_since(n))
    check("the Redo's job is the take now", (job_on(ann) or {}).get("id") == "job-2", job_on(ann))
    msg = SENT[n]
    check("it goes to the job's feedback route, signed in, attributed",
          msg["url"].endswith("/mmcp/generate/jobs/job-1/feedback")
          and msg["headers"].get("Authorization") == "Bearer test-token"
          and msg["headers"].get("X-Animatica-Client") == "blender", msg)

    # -- a version of the take before the Redo: that take again, the Redo's regenerated
    n = len(SENT)
    click("var_0")
    yield from wait_for(lambda: len(SENT) > n, "the verdict on the Redo's take")
    check("a version of the take before the Redo: the Redo's take was regenerated",
          sent_since(n) == [("job-2", {"verdict": "regenerated", "sample_index": 0, "seen_samples": [0]})],
          sent_since(n))
    check("and the take is job-1's again", job_on(ann) == {"id": "job-1", "samples": 2, "sample": 0,
                                                            "seen": [0]}, job_on(ann))

    # -- Discard, after moving a key: rejected, with the edit counted
    edit_one_key(ann)
    n = len(SENT)
    click("reject")
    yield from wait_for(lambda: len(SENT) > n, "the Discard verdict")
    check("Discard: rejected, with the version, the versions seen and the key moved",
          sent_since(n) == [("job-1", {"verdict": "rejected", "sample_index": 0, "seen_samples": [0],
                                       "edited_keys": 1})], sent_since(n))
    check("Discard: the take's job goes with its session", job_on(ann) is None, job_on(ann))

    # -- kept by fine-tuning it (Marionette's first edit calls keep_take)
    s().variations = 1
    yield from generate()
    n = len(SENT)
    with bpy.context.temp_override(**override()):
        kept = operators.keep_take(bpy.context)
    yield from wait_for(lambda: len(SENT) > n, "the keep verdict")
    check("kept by fine-tuning it: kept, not accepted",
          kept and sent_since(n) == [("job-3", {"verdict": "kept", "sample_index": 0, "seen_samples": [0],
                                                "edited_keys": 0})], sent_since(n))

    # -- Keep on the bar: accepted
    set_prompt(ann, "walks")                    # keeping locked the block: a fresh one
    yield from generate()
    n = len(SENT)
    click("keep_take")
    yield from wait_for(lambda: len(SENT) > n, "the Keep verdict")
    check("Keep on the bar: accepted",
          sent_since(n) == [("job-4", {"verdict": "accepted", "sample_index": 0, "seen_samples": [0],
                                       "edited_keys": 0})], sent_since(n))

    # -- Generate over a take nobody kept makes it again: that take was regenerated
    set_prompt(ann, "walks")
    yield from generate()
    n = len(SENT)
    with bpy.context.temp_override(**override()):
        bpy.ops.animatica.toolbar_generate('INVOKE_DEFAULT')
    yield from wait_for(lambda: not s().is_generating, "the take made again")
    yield from wait_for(lambda: len(SENT) > n, "the verdict on the take replaced")
    check("Generate over a take nobody kept: regenerated",
          sent_since(n) == [("job-5", {"verdict": "regenerated", "sample_index": 0, "seen_samples": [0]})],
          sent_since(n))
    check("…and the new take is the one waiting", (job_on(ann) or {}).get("id") == "job-6", job_on(ann))

    # -- a duplicate of a character whose take waits sends nothing for the copy
    copy_ = ann.copy()
    copy_.name = "Ann copy"
    bpy.context.scene.collection.objects.link(copy_)
    check("a duplicate's copy of the take is not one to judge",
          feedback.review(copy_) is None and feedback.review(ann) is not None)
    bpy.data.objects.remove(copy_, do_unlink=True)

    # -- sharing off: Discard sends nothing
    prefs().share_feedback = False
    n = len(SENT)
    click("reject")
    yield from wait_for(lambda: not s().is_previewing, "the Discard")
    yield from wait_for(lambda: True, "a tick")
    check("sharing off: Discard sends nothing", len(SENT) == n, sent_since(n))
    prefs().share_feedback = True

    # -- a batch: each character against its own job, Keep All accepted
    select([ann, bob], ann)
    n = len(SENT)
    click("generate")
    yield from wait_for(lambda: not s().is_generating, "the batch")
    jobs = {a.name: (job_on(a) or {}).get("id") for a in (ann, bob)}
    check("a batch: each character remembers its own job",
          sorted(batch.pending(s())) == ["Ann", "Bob"] and len(set(jobs.values())) == 2
          and all(j and j.startswith("job-") for j in jobs.values()), jobs)
    click("keep_all")
    yield from wait_for(lambda: len(SENT) >= n + 2, "the Keep All verdicts")
    got = sorted(sent_since(n))
    check("Keep All: accepted, each against its own job",
          got == sorted((jobs[name], {"verdict": "accepted", "sample_index": 0, "seen_samples": [0],
                                      "edited_keys": 0}) for name in ("Ann", "Bob")), got)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run():
    from animatica_blender import feedback, mmcp_client
    bpy.context.preferences.use_preferences_save = False
    mmcp_client.MmcpClient.generate = stub_generate
    mmcp_client.MmcpClient.generate_batch = stub_generate_batch
    mmcp_client.store_capabilities(CAPS)
    mmcp_client.needs_sign_in = lambda: False
    # Online access as an artist has it on; nothing leaves this process (the
    # verdicts are caught, the generations stubbed).
    mmcp_client.may_connect = lambda url: True
    feedback._post = stub_post
    prefs().access_token = "test-token"
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
    REPORT["sent"] = SENT
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
