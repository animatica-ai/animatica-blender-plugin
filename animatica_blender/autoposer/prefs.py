# SPDX-License-Identifier: Apache-2.0
"""Preferences: where the model comes from, and the two buttons that fetch it.

The addon ships inference code and no weights, so a fresh install has exactly two things to
acquire — the onnxruntime that executes the graphs, and the model itself. Both are one-time,
both land in a directory the user can point elsewhere or delete, and both report what happened
in a sentence rather than a status code.

Three sources, because three situations are real: a Hugging Face repo (the default, and the
only one that works for a user with just an account), a folder on disk (a studio that stages
the model on a share, or an unreleased checkpoint), and Auto for a machine that has already
been provisioned — by a previous download, by IT, or by the environment.
"""
from __future__ import annotations

import bpy

from . import engine
from .vendor import autoposer_runtime as apr


class AP_OT_install_runtime(bpy.types.Operator):
    bl_idname = "autoposer.install_runtime"
    bl_label = "Install Runtime"
    bl_description = ("Put onnxruntime in the addon's data directory (~75 MB). Needed once per "
                      "machine, unless the addon was built with the wheels inside it")

    def execute(self, context):
        d = engine.data_dir()
        try:
            apr.ortsetup.install(cache_dir=d)
        except Exception as e:                                 # noqa: BLE001
            self.report({"ERROR"}, f"install failed: {e}")
            return {"CANCELLED"}
        if not apr.ortsetup.activate(cache_dir=d):
            self.report({"ERROR"}, "installed, but onnxruntime will not import")
            return {"CANCELLED"}
        self.report({"INFO"}, "inference runtime ready")
        return {"FINISHED"}


class AP_OT_get_model(bpy.types.Operator):
    bl_idname = "autoposer.get_model"
    bl_label = "Download Model"
    bl_description = ("Fetch the model for the selected source and verify it against its own "
                      "published checksums (~150 MB, once)")

    def execute(self, context):
        d = engine.data_dir()
        src, token = engine.source()
        wm = context.window_manager
        wm.progress_begin(0, 100)
        last = [0.0]

        def on_progress(name, done, total):
            if total:
                pct = 100.0 * done / total
                if pct - last[0] >= 1.0:
                    last[0] = pct
                    wm.progress_update(pct)

        try:
            b = apr.resolve(src, cache_dir=d, token=token, on_progress=on_progress)
        except apr.BundleError as e:
            wm.progress_end()
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        except Exception as e:                                 # noqa: BLE001
            wm.progress_end()
            self.report({"ERROR"}, f"download failed: {e}")
            return {"CANCELLED"}
        wm.progress_end()
        engine.unload()                       # the next solve picks the new model up
        self.report({"INFO"}, f"model ready ({b.path})")
        return {"FINISHED"}


class AP_OT_check(bpy.types.Operator):
    bl_idname = "autoposer.check"
    bl_label = "Load and Self-Test"
    bl_description = ("Load the model and run the stored IK problem it ships with. A graph can "
                      "be miscompiled by the runtime on this machine and still return "
                      "plausible-looking wrong poses, so this checks a known answer")

    def execute(self, context):
        engine.unload()
        try:
            eng = engine.load()
        except engine.NotReady as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        ok, err = eng.selftest()
        m = engine.meta()
        self.report({"INFO" if ok else "ERROR"},
                    f"self-test {'passed' if ok else 'FAILED'} ({err:.3f} cm) — "
                    f"{m.get('njoints', '?')} joints, checkpoint step {m.get('step', '?')}")
        return {"FINISHED"}


class AP_OT_forget_model(bpy.types.Operator):
    bl_idname = "autoposer.forget_model"
    bl_label = "Delete Cached Model"
    bl_description = "Remove the downloaded model from this machine (it can be fetched again)"

    def execute(self, context):
        import shutil
        from pathlib import Path
        engine.unload()
        d = Path(engine.data_dir()) / "current"
        shutil.rmtree(d, ignore_errors=True)
        self.report({"INFO"}, f"removed {d}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Preferences
#
# These were an AddonPreferences of their own. Ported into Animatica, which
# already has one — an addon gets exactly one — they become fields on it
# (merged in ``properties.py``) and a draw function it calls for its Autoposer
# section. The property names and semantics are unchanged, so a machine that
# already fetched the model through the standalone addon keeps using it.
# ---------------------------------------------------------------------------

PROPERTIES = {
    'auto_install_runtime': bpy.props.BoolProperty(
        name="Install runtime automatically",
        default=True,
        description="Fetch the inference runtime in the background the first "
                    "time it is missing, instead of waiting to be asked. "
                    "About 75 MB, once per machine"),
    'model_source': bpy.props.EnumProperty(
        name="Model from",
        items=[("HF", "Hugging Face", "Download from a Hugging Face repo (private repos "
                                      "need an access token)"),
               ("LOCAL", "Folder", "A bundle directory holding poser.onnx, ik.onnx and "
                                   "meta.json"),
               ("AUTO", "Already installed", "Whatever this machine already has: the "
                                             "cached download, or a location named by "
                                             "the environment")],
        default="HF"),
    'hf_repo': bpy.props.StringProperty(name="Repo", default="Animatica-ai/autoposer"),
    'hf_subfolder': bpy.props.StringProperty(name="Subfolder", default="onnx"),
    'hf_revision': bpy.props.StringProperty(name="Revision", default="main"),
    'hf_token': bpy.props.StringProperty(
        name="Access token", default="", subtype="PASSWORD",
        description="A Hugging Face token with read access. Leave empty to use a token you have "
                    "already set up on this machine ($HF_TOKEN, or huggingface-cli login)"),
    'local_path': bpy.props.StringProperty(
        name="Bundle folder", default="", subtype="DIR_PATH",
        description="A directory holding poser.onnx, ik.onnx and meta.json"),
    'cache_dir': bpy.props.StringProperty(
        name="Data folder", default="", subtype="DIR_PATH",
        description="Where the model and the runtime are kept. Empty = Blender's own user data "
                    "directory, under the addon's name"),
    'threads': bpy.props.IntProperty(
        name="Threads", default=0, min=0, max=32,
        description="Threads onnxruntime may use per solve. 0 lets it decide; 1 or 2 keeps a "
                    "drag responsive on a busy machine"),
}


def draw(layout, prefs, context):
    lay = layout
    st = engine.status()

    box = lay.box()
    row = box.row()
    row.label(text="Inference runtime",
              icon="CHECKMARK" if st["runtime"] else "ERROR")
    installing = engine.install_state()
    if st["runtime"]:
        row.label(text="ready")
    elif installing["running"]:
        row.label(text="installing…")
    else:
        row.label(text="not installed")
    if not st["runtime"]:
        if installing["running"]:
            box.label(text="fetching onnxruntime in the background, ~75 MB",
                      icon="SORTTIME")
        else:
            if installing["error"]:
                err = box.row()
                err.alert = True
                err.label(text=installing["error"][:70], icon="ERROR")
            box.operator("autoposer.install_runtime", icon="IMPORT")
            box.label(text="onnxruntime, ~75 MB, once per machine", icon="INFO")
    box.prop(prefs, "auto_install_runtime")

    box = lay.box()
    row = box.row()
    row.label(text="Model", icon="CHECKMARK" if st["model"] else "ERROR")
    row.label(text=(f"step {st['step']}" if st["step"] else "not downloaded"))
    col = box.column(align=True)
    col.prop(prefs, "model_source", expand=True)
    if prefs.model_source == "HF":
        col.prop(prefs, "hf_repo")
        r = col.row(align=True)
        r.prop(prefs, "hf_subfolder")
        r.prop(prefs, "hf_revision")
        col.prop(prefs, "hf_token")
    elif prefs.model_source == "LOCAL":
        col.prop(prefs, "local_path")
    row = box.row(align=True)
    row.operator("autoposer.get_model", icon="IMPORT")
    row.operator("autoposer.check", icon="CHECKMARK")
    if st["model"]:
        box.label(text=st["model_dir"], icon="FILE_FOLDER")
        if st["model_source"]:
            box.label(text=f"from {st['model_source']}")
        box.operator("autoposer.forget_model", icon="TRASH")

    box = lay.box()
    box.label(text="Storage")
    box.prop(prefs, "cache_dir")
    box.prop(prefs, "threads")
    box.label(text=st["data_dir"], icon="FILE_FOLDER")
    hint = engine.env_hint()
    if hint:
        box.label(text=f"environment in use: {hint}", icon="INFO")
    lay.label(text="Nothing is sent anywhere: every solve runs in this process.",
              icon="LOCKED")



CLASSES = (AP_OT_install_runtime, AP_OT_get_model, AP_OT_check, AP_OT_forget_model)
