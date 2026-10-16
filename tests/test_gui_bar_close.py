"""Closing the floating bar and opening it again, in a real Blender window, with real (simulated)
mouse events: the pointer moved onto the bar's cross and its tab where the bar draws them and
clicked, the viewport's View menu opened and its "Animatica Bar" entry chosen.

* The cross is the bar's last button, in setup (no character yet) and with a character.
* A click on it folds the bar down to a tab where it was; a click on the tab opens it again.
* Closing is one undo step: Ctrl+Z opens the bar, Ctrl+Shift+Z closes it again.
* The View menu's first entry, "Animatica Bar", hides the bar altogether (the tab too), and
  brings it back open. The menu opens wherever it fits round the pointer, so the entry is chosen
  from the keyboard (Home, Return).

Run (a window opens and closes itself; the report and screenshots land in
``tests/out/gui_bar_close``)::

    blender --factory-startup --enable-event-simulate --python tests/test_gui_bar_close.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
for p in (ROOT, TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

import test_bake_object_rotation as synthetic  # noqa: E402

OUT = os.path.join(TESTS, "out", "gui_bar_close")
os.makedirs(OUT, exist_ok=True)
KEEP_OPEN = "--keep-open" in sys.argv
REPORT: dict = {"checks": [], "screenshots": []}
FAILS: list = []


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


def screenshot(tag: str) -> None:
    with bpy.context.temp_override(**override()):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        path = os.path.join(OUT, f"{tag}.png")
        bpy.ops.screen.screenshot(filepath=path)
    REPORT["screenshots"].append(path)


def bar_ids() -> list:
    from animatica_blender import toolbar
    with bpy.context.temp_override(**override()):
        return [it.id for it in toolbar.items(bpy.context)]


def drawn(ident):
    """Where the bar draws a button, in window pixels (its centre), or None."""
    from animatica_blender import toolbar
    ov = override()
    with bpy.context.temp_override(**ov):
        _bar, rects, _d = toolbar.layout(bpy.context, ov["area"], ov["region"])
    for it, (x0, y0, x1, y1) in rects:
        if it.id == ident:
            return int(ov["region"].x + (x0 + x1) / 2), int(ov["region"].y + (y0 + y1) / 2)
    return None


def shown() -> bool:
    from animatica_blender import toolbar
    with bpy.context.temp_override(**override()):
        return toolbar.shown(bpy.context)


def mouse(x, y):
    bpy.context.window_manager.windows[0].event_simulate('MOUSEMOVE', 'NOTHING', x=x, y=y)


def press(kind, x, y, **mods):
    win = bpy.context.window_manager.windows[0]
    win.event_simulate(kind, 'PRESS', x=x, y=y, **mods)
    win.event_simulate(kind, 'RELEASE', x=x, y=y, **mods)


def view_menu_entry(x, y):
    """Choose the View menu's first entry, as the keyboard does."""
    with bpy.context.temp_override(**override()):
        bpy.ops.wm.call_menu(name="VIEW3D_MT_view")
    for _ in range(3):
        yield
    press('HOME', x, y)
    yield
    press('RET', x, y)
    for _ in range(4):
        yield


def click_at(ident, tag):
    """Hover a button where it is drawn, then click it."""
    from animatica_blender import toolbar
    x, y = drawn(ident)
    mouse(x, y)
    for _ in range(3):
        yield
    check(f"the mouse over {ident} lights it", toolbar.hovered(override()["area"]) == ident,
          toolbar.hovered(override()["area"]))
    screenshot(tag)
    press('LEFTMOUSE', x, y)
    for _ in range(3):
        yield


def scenario():
    from animatica_blender import toolbar
    # no character yet: the way in, and the cross at the end
    ids = bar_ids()
    check("setup: the cross is the bar's last button", ids and ids[-1] == "close_bar", ids)
    synthetic.reset_scene()
    s().target_armature = synthetic.make_rig("Rig")
    bpy.ops.object.mode_set(mode='OBJECT')
    yield
    ids = [i for i in bar_ids() if not i.startswith("hint")]
    check("with a character: the cross is the bar's last button", ids and ids[-1] == "close_bar", ids)
    with bpy.context.temp_override(**override()):
        it = next(i for i in toolbar.items(bpy.context) if i.id == "close_bar")
        text = toolbar.tip(bpy.context, it)
    check("its tooltip says what it does and how to hide it all", "tab" in text and "View" in text, text)
    screenshot("1_open")
    yield from click_at("close_bar", "2_close_hover")
    check("a click folds the bar to a tab", s().toolbar_collapsed and s().show_toolbar and shown()
          and bar_ids() == ["open_bar"], bar_ids())
    check("the tab is drawn, the bar's buttons are not", drawn("open_bar") and not drawn("close_bar"))
    screenshot("3_tab")
    yield from click_at("open_bar", "4_tab_hover")
    check("a click on the tab opens the bar", not s().toolbar_collapsed and drawn("close_bar"), bar_ids()[-3:])
    screenshot("5_opened")
    # Ctrl+Z: closing was one undo step
    yield from click_at("close_bar", "6_close_again")
    x, y = drawn("open_bar")
    cx, cy = x, y + 300
    mouse(cx, cy)
    yield
    press('Z', cx, cy, ctrl=True)
    for _ in range(3):
        yield
    check("Ctrl+Z opens it", not s().toolbar_collapsed and drawn("close_bar"))
    press('Z', cx, cy, ctrl=True, shift=True)
    for _ in range(3):
        yield
    check("Ctrl+Shift+Z closes it again", s().toolbar_collapsed)
    # the View menu: off hides it all, the tab too; on brings it back open
    yield from view_menu_entry(cx, cy)
    check("View > Animatica Bar off hides it altogether, the tab too", not s().show_toolbar and not shown())
    screenshot("7_hidden")
    yield from view_menu_entry(cx, cy)
    check("View > Animatica Bar on brings it back open", s().show_toolbar and not s().toolbar_collapsed
          and drawn("close_bar"))
    screenshot("8_back")


def run():
    bpy.context.preferences.use_preferences_save = False
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
        return 0.3

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
