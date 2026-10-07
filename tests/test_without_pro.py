"""Animatica on its own makes motion and shows it; editing it by hand is Animatica Autoposer
Pro's. Without Pro:

* none of the editing tools is here -- no operator, no setting, no click on a ghost;
* the bar has the onion skin and no editing buttons (the trail, the zoetrope, the reach of an
  edit, Smooth, Lock in Place, the Autopose tool);
* each place that asks Pro gets nothing back and carries on (the bar, the panels, the bake pass,
  In place's root path, a take's locks);
* the onion skin's frames are its own Before and After, Step apart (and round a loop's seam);
* every panel and popover draws, naming only settings and operators that are here.

Headless, with the Animatic character in Blender's data (read, never fetched)::

    BUR=$(mktemp -d); mkdir -p $BUR/datafiles
    ln -s "$HOME/Library/Application Support/Blender/5.1/datafiles/animatica" $BUR/datafiles/
    BLENDER_USER_RESOURCES=$BUR blender -b --factory-startup --python tests/test_without_pro.py
"""
from __future__ import annotations

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=True)
from animatica_blender import canonical_skeleton, key_poses, posing, toolbar  # noqa: E402

FAILS: list[str] = []

#: the editing operators that moved to Pro (as Animatica named them)
MOVED_OPS = ("lock_joint", "unlock_joint", "lock_range", "smooth_trail", "drag_motion_curve",
             "wormhole_drag", "reach_drag", "edit_key_pose", "pick_ghost", "give_back_rig",
             "edit_root_trajectory", "reset_root_trajectory", "toolbar_wormhole")
#: the settings that moved to Pro's
MOVED_PROPS = ("trail_radius", "edit_strength", "onion_wormhole", "wormhole_step", "wormhole_spacing",
               "key_pose_trail", "key_pose_trail_hips", "key_pose_trail_head", "key_pose_trail_hands",
               "key_pose_trail_feet", "editing_key_pose_frame")
#: the bar's editing buttons, all Pro's
EDIT_BUTTONS = {"autopose", "picker", "reset_controls", "wormhole", "trail", "reach", "smooth", "lock"}


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


def op_exists(name) -> bool:
    try:
        getattr(bpy.ops.animatica, name).get_rna_type()
        return True
    except (KeyError, AttributeError):
        return False


class Checked:
    """A panel's layout that checks each row it is asked for: a setting by its name on the data
    given, an operator by its id."""

    seen = 0                               # rows checked, by every layout

    def __init__(self, bad):
        self.bad = bad
        self.active = self.enabled = True
        self.use_property_split = self.use_property_decorate = False
        self.scale_y = self.scale_x = 1.0
        self.alignment = 'EXPAND'

    def prop(self, data, name, **_k):
        Checked.seen += 1
        if data is None or name not in data.bl_rna.properties:
            self.bad.append(f"{type(data).__name__}.{name}")
        return self

    def operator(self, idname, **_k):
        Checked.seen += 1
        mod, _, op = idname.partition(".")
        try:
            getattr(getattr(bpy.ops, mod), op).get_rna_type()
        except (KeyError, AttributeError):
            self.bad.append(idname)
        return self

    def __getattr__(self, name):           # row(), column(), box(), label(), separator() ...
        return lambda *a, **k: self


def draw_panels(ctx, bad):
    """Every panel and popover of the add-on's, drawn onto a checked layout: ``{name: error}``
    for the ones that raised."""
    from animatica_blender import panels, retarget
    classes = [c for m in (panels, toolbar, retarget) for c in vars(m).values()
               if isinstance(c, type) and issubclass(c, bpy.types.Panel) and c.__name__.startswith("ANIMATICA_PT")]
    errors = {}
    for cls in classes:
        attrs = {}
        for base in reversed(cls.__mro__):
            if base.__module__.startswith(("bpy", "builtins")):
                continue
            attrs.update({k: v for k, v in vars(base).items() if not k.startswith("__")})
        panel = type("Drawn", (), attrs)()
        panel.layout = Checked(bad)
        panel.bl_label = getattr(cls, "bl_label", "")
        try:
            for fn in ("draw_header", "draw"):
                if fn in attrs:
                    getattr(panel, fn)(ctx)
        except Exception as exc:                        # noqa: BLE001
            errors[cls.__name__] = f"{type(exc).__name__}: {exc}"
    return classes, errors


class _Layout:
    """Stands in for a panel's layout: records every row asked of it."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*a, **k):
            self.calls.append(name)
            return self
        return call


def test_nothing_of_editing_is_here():
    check("Pro is not here", not posing.present() and "animatica_autoposer" not in sys.modules)
    there = [n for n in MOVED_OPS if op_exists(n)]
    check("no editing operator", not there, ", ".join(there))
    rna = bpy.types.Scene.bl_rna.properties["animatica"].fixed_type.properties
    there = [n for n in MOVED_PROPS if n in rna]
    check("no editing setting", not there, ", ".join(there))
    kc = bpy.context.window_manager.keyconfigs.addon
    picks = [kmi.idname for km in (kc.keymaps if kc else ()) if km.space_type == 'VIEW_3D'
             for kmi in km.keymap_items if kmi.idname.startswith("animatica.") and kmi.type == 'LEFTMOUSE']
    check("no click taken in the 3D view (a ghost is not a handle)", not picks, ", ".join(picks))
    for mod in ("carry", "curve_edit", "joint_lock", "reach_widget", "root_edit", "wormhole", "autopose_sync"):
        check(f"no {mod} module", f"animatica_blender.{mod}" not in sys.modules
              and not os.path.exists(os.path.join(ROOT, "animatica_blender", mod + ".py")))


def setup():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    ctx = bpy.context
    arm = canonical_skeleton.load_character(ctx)[0]
    s = ctx.scene.animatica
    s.target_armature = arm
    hips = next(pb for pb in arm.pose.bones if pb.name.split(":")[-1] == "Hips")
    for i, f in enumerate((1, 13, 25, 37)):
        hips.location = (0.0, 0.0, 0.3 * i)
        hips.keyframe_insert("location", frame=f)
    ctx.scene.frame_start, ctx.scene.frame_end = 1, 48
    ctx.scene.frame_set(25)
    s.key_pose_overlay = True
    s.key_pose_ghosts = True
    return arm


def test_bar(arm):
    ctx = bpy.context
    ids = [it.id for it in toolbar.items(ctx)]
    check("the bar has the onion skin", "onion" in ids, ", ".join(ids))
    check("...and the key, Generate's field and Options", {"set_key", "prompt", "options"} <= set(ids))
    there = sorted(EDIT_BUTTONS & set(ids))
    check("no editing button on the bar", not there, ", ".join(there))
    check("the fold order is this add-on's own", toolbar._fold_order(toolbar.items(ctx))[:1] == ["copy_paste"]
          and not (EDIT_BUTTONS & set(toolbar._fold_order(toolbar.items(ctx)))))
    s = ctx.scene.animatica
    s.key_pose_ghosts = False
    bpy.ops.animatica.toolbar_overlay(part='GHOSTS')
    check("the onion skin button switches it on", s.key_pose_ghosts and s.key_pose_overlay)


def test_hooks_answer_nothing(arm):
    ctx = bpy.context
    check("no buttons from Pro", posing.bar_items(ctx, arm, "pose") == [] and posing.bar_items(ctx, arm, "edit") == [])
    check("no sampler in the bake pass", posing.samplers() == [])
    check("no ghost being edited", posing.editing_frame(ctx.scene) == -1)
    check("no edit's hint", posing.hint(ctx) is None)
    check("no onion layout of Pro's", posing.onion_layout() is None)
    check("nothing dragging", posing.dragging() is False)
    spans = [{"first": 1, "last": 37}]
    check("In place's root path left as fitted", posing.path_overlay(arm.animation_data.action, spans, 24) is spans
          and posing.path_edited(arm.animation_data.action) is False)
    posing.path_discard_all()
    posing.after_take(arm, arm.animation_data.action, 1, 37)
    posing.reseat(ctx.scene)
    posing.end_edit(ctx.scene)
    lay = _Layout()
    drew = [k for k in ("pose", "overlay", "options", "options_onion", "options_trail", "popover",
                        "popover_onion", "review") if posing.draw_panel(k, lay, ctx)]
    check("no panel rows of Pro's", not drew and not lay.calls, ", ".join(drew))
    check("Set Key Pose still keys (it ends no edit of Pro's)",
          bpy.ops.animatica.set_key_pose() == {'FINISHED'})


def test_onion_frames(arm):
    sc = bpy.context.scene
    s = sc.animatica
    s.onion_mode = 'FRAMES'
    s.onion_before, s.onion_after, s.onion_step = 2, 3, 3
    sc.frame_set(25)
    got = key_poses.onion_frames(sc, s)
    check("Frames: Before and After, Step apart", got == [19, 22, 28, 31, 34], f"{got}")
    s.onion_before, s.onion_after, s.onion_step = 0, 1, 1
    got = key_poses.onion_frames(sc, s)
    check("...none before, one after", got == [26], f"{got}")


def test_panels(arm):
    bad = []
    classes, errors = draw_panels(bpy.context, bad)
    check("every panel draws", len(classes) >= 8 and not errors,
          f"{len(classes)} panels; " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    check("...naming only settings and operators that are here", Checked.seen >= 40 and not bad,
          f"{Checked.seen} rows; " + ", ".join(sorted(set(bad))))


def main():
    test_nothing_of_editing_is_here()
    arm = setup()
    for t in (test_bar, test_hooks_answer_nothing, test_onion_frames, test_panels):
        try:
            t(arm)
        except Exception:                               # noqa: BLE001
            traceback.print_exc()
            FAILS.append(t.__name__)
    print(f"\n{len(FAILS)} failed" + (": " + ", ".join(FAILS) if FAILS else ""))
    sys.stdout.flush()
    os._exit(1 if FAILS else 0)


main()
