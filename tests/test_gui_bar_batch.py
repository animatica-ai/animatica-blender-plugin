"""Several characters selected, made from the floating bar, in a real Blender
window: every button pressed the way a click presses it (``animatica.bar_click``
on the hovered button), the server stood in for by a stub.

* One character selected: the bar's Generate makes one take, as before.
* Three selected, one without a prompt: Generate says "Generate 2 of 3", a
  Shared switch sits beside it, and the hint offers the typed prompt to all.
  The hint's click turns Shared on, and Generate makes all three.
* Generate on the bar starts the batch: the working button counts the
  characters back; then Keep All / Redo All / the versions / Discard All
  replace Generate, and the hint says what each does.
* Redo All makes them again; a version button flips every character;
  Discard All puts back what each had; Keep All keeps each take as that
  character's own action, its blocks locked.
* Every tooltip of every state draws.

Run (a window opens and closes itself; the report and screenshots land in
``tests/out/gui_bar_batch``)::

    blender --factory-startup --python tests/test_gui_bar_batch.py
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

OUT = os.path.join(TESTS, "out", "gui_bar_batch")
os.makedirs(OUT, exist_ok=True)
WAIT_SECONDS = 60.0
KEEP_OPEN = "--keep-open" in sys.argv

CAPS = {
    "protocol_version": "1.0",
    "rotation_format": "quaternion_xyzw",
    "coordinate_system": "right_handed_y_up",
    "units": "meters",
    "response_formats": ["gltf_2.0_json"],
    "models": [{
        "id": "kimodo-soma-rp", "model_name": "kimodo-soma-rp", "version": "1.0.0", "fps": 30.0,
        "supports_retargeting": True, "supports_async": False, "supports_segment_seed": True,
        "supports_loop": True, "supports_batch": True, "supports_trajectory": False,
        "supported_constraints": ["root_path", "effector_target", "pose_keyframe"],
        "supported_segments": ["text", "unconditioned", "pose"],
        "supported_guidance_types": ["nocfg", "regular", "separated"],
        "predicted_contact_joints": [], "native_clip_seconds": 10.0, "chunking": "none",
        "recommended_max_duration_seconds": 12.0,
        "limits": {"max_duration_seconds": 30.0, "max_num_samples": 16, "max_constraints_per_request": 64,
                   "max_prompt_length": 1000, "max_request_bytes": 1048576, "max_batch_size": 16},
        "canonical_skeleton": {"joints": [], "coordinate_system": "right_handed_y_up", "units": "meters"},
    }],
}

REQUESTS: list = []
REPORT: dict = {"checks": [], "screenshots": []}
FAILS: list = []


def two_versions(take: dict) -> dict:
    """The synthetic take with a second version (the same motion, so the
    check is which version shows, not what it looks like)."""
    out = copy.deepcopy(take)
    second = copy.deepcopy(out["animations"][0])
    second["name"] = "sample_1"
    out["animations"].append(second)
    ext = out["extensions"]["MMCP_motion"]
    ext["samples"] = ext["samples"] + [copy.deepcopy(ext["samples"][0])]
    return out


TAKE = two_versions(synthetic.GLTF)


def stub_generate(_client, body):
    """As many versions as the request asks for (one or two)."""
    REQUESTS.append(body)
    time.sleep(0.4)
    asked = int((body.get("options") or {}).get("num_samples") or 1)
    return copy.deepcopy(TAKE if asked > 1 else synthetic.GLTF)


def stub_generate_batch(client, bodies):
    return [stub_generate(client, b) for b in bodies]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def s():
    return bpy.context.scene.animatica


def override() -> dict:
    win = bpy.context.window_manager.windows[0]
    area = next(a for a in win.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return {"window": win, "screen": win.screen, "area": area, "region": region}


def check(name, ok, info="") -> None:
    REPORT["checks"].append({"name": name, "ok": bool(ok), "info": str(info)})
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def bar() -> dict:
    """The bar's buttons by id, as the bar draws them now."""
    from animatica_blender import toolbar
    toolbar._cache["at"] = 0.0             # what the bar asks, asked afresh
    toolbar._batch_cache["at"] = 0.0
    with bpy.context.temp_override(**override()):
        return {it.id: it for it in toolbar.items(bpy.context)}


def tips_draw() -> str:
    """Every button's tooltip, as hovering shows it; the first that raised, or ''."""
    from animatica_blender import toolbar
    with bpy.context.temp_override(**override()):
        for it in toolbar.items(bpy.context):
            try:
                toolbar.tip(bpy.context, it)
            except Exception:              # noqa: BLE001
                return f"{it.id}: {traceback.format_exc().splitlines()[-1]}"
    return ""


def tip(ident) -> str:
    from animatica_blender import toolbar
    with bpy.context.temp_override(**override()):
        it = next(i for i in toolbar.items(bpy.context) if i.id == ident)
        return toolbar.tip(bpy.context, it)


def click(ident) -> set:
    """Press a button on the bar as the mouse does: hovered, then clicked."""
    from animatica_blender import toolbar
    ov = override()
    toolbar._cache["at"] = 0.0
    toolbar._batch_cache["at"] = 0.0
    toolbar._hover[ov["area"].as_pointer()] = ident
    with bpy.context.temp_override(**ov):
        return bpy.ops.animatica.bar_click('INVOKE_DEFAULT')


def hint() -> dict:
    from animatica_blender import guidance
    with bpy.context.temp_override(**override()):
        return guidance.next_step(bpy.context) or {}


def select(objs, active) -> None:
    for o in bpy.context.scene.objects:
        o.select_set(o in objs)
    bpy.context.view_layer.objects.active = active
    # Follow Selection makes the active one the character, as a click does
    s().target_armature = active
    bpy.context.view_layer.update()


def set_prompt(arm, prompt: str) -> None:
    from animatica_blender import properties
    st = s()
    st.target_armature = arm
    st.prompt_blocks.clear()
    b = st.prompt_blocks.add()
    b.prompt, b.frame_start, b.frame_end, b.enabled, b.seed = prompt, synthetic.FRAMES[0], synthetic.FRAMES[-1], True, 0
    st.active_block_index = 0
    properties.save_blocks_to_armature(arm, st)


def screenshot(tag: str) -> None:
    with bpy.context.temp_override(**override()):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        path = os.path.join(OUT, f"{tag}.png")
        bpy.ops.screen.screenshot(filepath=path)
    REPORT["screenshots"].append(path)


def push(message="Animatica") -> None:
    """The undo step a click on the bar records (bar_click is an UNDO operator;
    called from a script, it records none of its own)."""
    with bpy.context.temp_override(**override()):
        bpy.ops.ed.undo_push(message=message)


def undo() -> None:
    with bpy.context.temp_override(**override()):
        bpy.ops.ed.undo()


def wait_for(pred, what: str):
    t0 = time.time()
    while not pred():
        if time.time() - t0 > WAIT_SECONDS:
            raise TimeoutError(f"gave up waiting for {what}")
        yield


# ---------------------------------------------------------------------------
# The scenario
# ---------------------------------------------------------------------------

def scenario():
    from animatica_blender import batch, variations

    bpy.context.scene.frame_start, bpy.context.scene.frame_end = synthetic.FRAMES[0], synthetic.FRAMES[-1]
    ann = synthetic.make_rig("Ann")
    bob = synthetic.make_rig("Bob")
    cy = synthetic.make_rig("Cy")
    bob.location.x += 1.5
    cy.location.x += 3.0
    for arm, prompt in ((ann, "walks"), (bob, "waves"), (cy, "")):
        set_prompt(arm, prompt)

    # -- one character: the bar is as it was
    select([ann], ann)
    items = bar()
    check("one selected: Generate makes one take", "generate" in items and "batch_shared" not in items
          and items["generate"].op == "animatica.toolbar_generate" and "Characters" not in items["generate"].label,
          items.get("generate") and items["generate"].label)

    # -- three selected, Cy with no prompt
    select([ann, bob, cy], ann)
    s().batch_direction = 'OWN'
    items = bar()
    gen = items.get("generate")
    check("three selected: Generate counts who goes", gen is not None and gen.label == "Generate 2 of 3"
          and gen.enabled and gen.primary, gen and gen.label)
    check("three selected: Shared switch beside Generate, off", "batch_shared" in items
          and not items["batch_shared"].on)
    line = tip("generate")
    check("Generate's tooltip names who is left out and why", "Cy (no prompt)" in line and "Shared" in line, line)
    h = hint()
    check("hint offers Ann's prompt to the others", h.get("id") == "batch_shared" and "Cy" in h.get("text", ""),
          h.get("text"))
    check("tooltips draw (selection)", not tips_draw(), tips_draw())
    screenshot("1_three_selected_own")

    click("hint")
    check("the hint's click turns Shared on", s().batch_direction == 'SHARED')
    items = bar()
    check("Shared: all three go", items["generate"].label == "Generate 3 Characters" and items["batch_shared"].on,
          items["generate"].label)
    h = hint()
    check("Shared: the ready hint names whose prompts", h.get("id") == "batch_ready" and "Ann's" in h.get("text", ""),
          h.get("text"))
    screenshot("2_three_selected_shared")

    click("batch_shared")
    check("the Shared button turns it off again", s().batch_direction == 'OWN')

    # -- Generate from the bar: the batch
    s().variations = 2
    before = {a.name: (a.animation_data.action if a.animation_data else None) for a in (ann, bob, cy)}
    REQUESTS.clear()
    ret = click("generate")
    check("Generate on the bar starts the batch", s().is_generating and s().batch_total == 2, sorted(ret))
    items = bar()
    check("working: says how many it makes (none back yet, so no “0 of 2”)",
          "working" in items and items["working"].label.startswith("2 characters"),
          items.get("working") and items["working"].label)
    check("tooltips draw (working)", not tips_draw(), tips_draw())
    yield from wait_for(lambda: not s().is_generating, "the batch")
    check("two requests, one each", len(REQUESTS) == 2, len(REQUESTS))
    check("Ann and Bob wait for review, Cy left out",
          sorted(batch.pending(s())) == ["Ann", "Bob"] and any(f.startswith("Cy:") for f in batch.failures(s())),
          f"{batch.pending(s())} / {batch.failures(s())}")

    items = bar()
    check("review: Keep All, Redo All, Discard All instead of Generate",
          {"keep_all", "redo_all", "discard_all"} <= set(items) and "generate" not in items
          and items["keep_all"].label == "Keep All" and items["keep_all"].primary,
          sorted(items))
    check("review: Redo All takes the failed one along",
          sorted(json.loads(items["redo_all"].props["characters"])) == ["Ann", "Bob", "Cy"],
          items["redo_all"].props)
    check("review: a button per version, for everyone", "var_0" in items and "var_1" in items
          and items["var_0"].on and items["var_1"].props.get("character") == "*")
    check("review: no Shared switch", "batch_shared" not in items)
    h = hint()
    check("review hint says what each button does", h.get("id") == "batch_review" and "Keep All" in h.get("text", ""),
          h.get("text"))
    check("tooltips draw (review)", not tips_draw(), tips_draw())
    screenshot("3_batch_review")

    # selecting one character while the takes wait keeps the batch's buttons
    select([bob], bob)
    items = bar()
    check("one selected while they wait: still the batch's buttons", "keep_all" in items and "generate" not in items,
          sorted(items))
    select([ann, bob, cy], ann)

    # -- a version for everyone
    click("var_1")
    check("version 2 shows on every character",
          all(variations.take_of(a) is not None and variations.take_of(a).index == 1 for a in (ann, bob)),
          [(a.name, variations.take_of(a) and variations.take_of(a).index) for a in (ann, bob)])
    check("the bar lights version 2", bar()["var_1"].on)

    # -- Redo All
    REQUESTS.clear()
    click("redo_all")
    yield from wait_for(lambda: not s().is_generating, "Redo All")
    check("Redo All: made again", len(REQUESTS) == 2 and sorted(batch.pending(s())) == ["Ann", "Bob"],
          f"{len(REQUESTS)} requests, {batch.pending(s())}")

    # -- Discard All
    click("discard_all")
    after = {a.name: (a.animation_data.action if a.animation_data else None) for a in (ann, bob, cy)}
    check("Discard All: no takes waiting", not batch.pending(s()) and not batch.failures(s()))
    check("Discard All: each has what it had", after == before, f"{before} -> {after}")
    items = bar()
    check("after Discard: Generate again", items.get("generate") is not None
          and items["generate"].label == "Generate 2 of 3", items.get("generate") and items["generate"].label)

    # -- Ctrl+Z, then one character: the batch's count must not come back (it said "0 of 2"
    #    on a single take). The undo steps are the ones the clicks record
    push("before")
    click("generate")
    push()                                          # the Generate click: the batch just started
    yield from wait_for(lambda: not s().is_generating, "the batch to undo")
    click("discard_all")
    push()                                          # the Discard All click
    undo()
    check("Ctrl+Z after Discard All: the takes are back",
          sorted(batch.pending(s())) == ["Ann", "Bob"] and not s().is_generating,
          f"{batch.pending(s())}, generating={s().is_generating}")
    undo()
    items = bar()
    check("Ctrl+Z to the Generate click: not stuck generating",
          not s().is_generating and s().batch_total == 0 and "working" not in items,
          f"generating={s().is_generating} total={s().batch_total} {sorted(items)}")
    ann, bob, cy = (bpy.data.objects[n] for n in ("Ann", "Bob", "Cy"))   # undo remade them
    select([ann], ann)
    click("generate")
    items = bar()
    check("then one character: no batch count on its take",
          s().is_generating and "working" in items and "characters" not in items["working"].label
          and " of " not in items["working"].label, items.get("working") and items["working"].label)
    yield from wait_for(lambda: not s().is_generating, "the single take")
    click("reject")
    select([ann, bob, cy], ann)

    # -- Generate, then Keep All
    s().variations = 1
    click("generate")
    yield from wait_for(lambda: not s().is_generating, "the batch, again")
    check("one version: no version buttons", "var_0" not in bar())
    click("keep_all")
    check("Keep All: no takes waiting", not batch.pending(s()) and not batch.failures(s()))
    for a in (ann, bob):
        act = a.animation_data.action if a.animation_data else None
        check(f"Keep All: {a.name}'s take is its own action",
              act is not None and not act.name.startswith("Animatica_Motion"), act and act.name)
    from animatica_blender import properties
    for a in (ann, bob):
        blocks = json.loads(a.get(properties._BLOCKS_KEY) or "[]")
        check(f"Keep All: {a.name}'s block is locked", blocks and all(b.get("locked") for b in blocks), blocks)
    screenshot("4_kept")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run():
    from animatica_blender import mmcp_client
    bpy.context.preferences.use_preferences_save = False
    mmcp_client.MmcpClient.generate = stub_generate
    mmcp_client.MmcpClient.generate_batch = stub_generate_batch
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
