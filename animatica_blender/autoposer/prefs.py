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
    bl_description = ("Install onnxruntime into the addon's data folder (~75 MB). Needed once per "
                      "machine, unless this build of the addon already includes it")

    def execute(self, context):
        if engine.runtime_needs_network() and not engine.online():
            self.report({"ERROR"}, engine.offline_message())
            return {"CANCELLED"}
        d = engine.data_dir()
        try:
            apr.ortsetup.install(cache_dir=d)
        except Exception as e:                                 # noqa: BLE001
            self.report({"ERROR"}, f"Couldn't install the runtime: {e}")
            return {"CANCELLED"}
        if not apr.ortsetup.activate(cache_dir=d):
            self.report({"ERROR"}, "Installed, but onnxruntime won't load")
            return {"CANCELLED"}
        self.report({"INFO"}, "Inference runtime ready")
        return {"FINISHED"}


class AP_OT_get_model(bpy.types.Operator):
    bl_idname = "autoposer.get_model"
    bl_label = "Download Model"
    bl_description = ("Download the model from the selected source and check it against its "
                      "published checksums (~150 MB, once)")

    def execute(self, context):
        # In the background, like Download Autoposer: ~150 MB on the main thread
        # froze Blender for as long as the download took.
        src, _token = engine.source()
        if src and not str(src).startswith("hf://"):
            self.report({"INFO"}, f"The model comes from a folder ({src}), so there is nothing to download")
            return {"CANCELLED"}
        if not engine.online():
            self.report({"ERROR"}, engine.offline_message())
            return {"CANCELLED"}
        if engine.fetch_state()["running"]:
            self.report({"WARNING"}, "The model is already downloading")
            return {"CANCELLED"}
        engine.ensure_model(force=True, redownload=True)
        self.report({"INFO"}, "Downloading the model…")
        return {"FINISHED"}


class AP_OT_download(bpy.types.Operator):
    bl_idname = "autoposer.download"
    bl_label = "Download Autoposer"
    bl_description = ("Download what the Autoposer needs: the inference runtime (~75 MB) and "
                      "the model (~150 MB). This happens once, in the background. Nothing is "
                      "downloaded until you press this")

    @classmethod
    def poll(cls, context):
        if not engine.online():
            cls.poll_message_set(engine.offline_message())
            return False
        return True

    def execute(self, context):
        engine.download_all()
        self.report({"INFO"}, "Downloading the Autoposer…")
        return {"FINISHED"}


class AP_OT_check(bpy.types.Operator):
    bl_idname = "autoposer.check"
    bl_label = "Load and Self-Test"
    bl_description = ("Load the model and run the test problem it ships with, which has a known "
                      "answer. The runtime on some machines can miscompile the model and still "
                      "return wrong poses that look plausible, and this catches that")

    def execute(self, context):
        engine.unload()
        try:
            # Never a download from here: that ran on the main thread, and a
            # model that needs one is what the Download button is for.
            eng = engine.load(allow_install=False, allow_download=False)
        except engine.NotReady as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        ok, err = eng.selftest()
        m = engine.meta()
        self.report({"INFO" if ok else "ERROR"},
                    f"Self-test {'passed' if ok else 'FAILED'} ({err:.3f} cm), "
                    f"{m.get('njoints', '?')} joints, checkpoint step {m.get('step', '?')}")
        return {"FINISHED"}


class AP_OT_forget_model(bpy.types.Operator):
    bl_idname = "autoposer.forget_model"
    bl_label = "Delete Cached Model"
    bl_description = "Remove the downloaded model from this machine. You can download it again"

    def execute(self, context):
        import shutil
        from pathlib import Path
        engine.unload()
        d = Path(engine.data_dir()) / "current"
        shutil.rmtree(d, ignore_errors=True)
        self.report({"INFO"}, f"Removed {d}")
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
        name="Set the poser up automatically",
        default=False,
        description="Download the inference runtime and the model in the "
                    "background the first time they are missing, instead of "
                    "waiting for Download Autoposer. About 225 MB in total, "
                    "once per machine. Needs online access"),
    'model_source': bpy.props.EnumProperty(
        name="Model from",
        items=[("HF", "Hugging Face", "Download from a Hugging Face repo (private repos "
                                      "need an access token)"),
               ("LOCAL", "Folder", "A folder holding poser.onnx, ik.onnx and "
                                   "meta.json"),
               ("AUTO", "Already installed", "Whatever this machine already has: the "
                                             "cached download, or a location set by an "
                                             "environment variable")],
        default="HF"),
    'hf_repo': bpy.props.StringProperty(name="Repo", default="Animatica-ai/autoposer"),
    'hf_subfolder': bpy.props.StringProperty(name="Subfolder", default="onnx"),
    'hf_revision': bpy.props.StringProperty(
        name="Revision", default="",
        description="A commit of the repo. Leave empty for the one this addon build is pinned "
                    "to, whose checksums it carries. Any other commit is checked only against "
                    "the repo's own meta.json"),
    'hf_token': bpy.props.StringProperty(
        name="Access token", default="", subtype="PASSWORD",
        description="A Hugging Face token with read access, sent only to huggingface.co. Leave "
                    "it empty for the public model. A token already set up on this machine "
                    "($HF_TOKEN, or huggingface-cli login) is used only if the repo refuses "
                    "access without one"),
    'local_path': bpy.props.StringProperty(
        name="Bundle folder", default="", subtype="DIR_PATH",
        description="A folder holding poser.onnx, ik.onnx and meta.json"),
    'cache_dir': bpy.props.StringProperty(
        name="Data folder", default="", subtype="DIR_PATH",
        description="Where the model and the runtime are kept. Leave empty to use Blender's "
                    "user data folder, under the addon's name"),
    'threads': bpy.props.IntProperty(
        name="Threads", default=0, min=0, max=32,
        description="Threads onnxruntime may use per solve. 0 lets it decide. 1 or 2 keeps "
                    "dragging responsive on a busy machine"),
}


def draw(layout, prefs, context):
    """The poser, in one line — and everything it took to get there, folded.

    Both halves install themselves now, so the page's job is no longer to hand
    someone the buttons for that. It is to say whether the poser is ready, and
    to keep the tools for when it is not within reach but out of the way.
    """
    st = engine.status()
    installing = engine.install_state()
    fetching = engine.fetch_state()

    box = layout.box()
    row = box.row(align=True)
    if st["runtime"] and st["model"]:
        row.label(text="Ready" + (f"  ·  model step {st['step']}" if st["step"] else ""),
                  icon='CHECKMARK')
    elif fetching["running"]:
        row.label(text=f"Downloading the model…  {engine.fetch_percent():.0f}%", icon='SORTTIME')
    elif installing["running"]:
        row.label(text="Installing the inference runtime…", icon='SORTTIME')
    elif installing["error"] or fetching["error"]:
        row.alert = True
        row.label(text=(installing["error"] or fetching["error"])[:60], icon='ERROR')
    elif st.get("model_note"):
        row.label(text=st["model_note"][:60], icon='INFO')
    else:
        row.label(text="Not downloaded yet", icon='INFO')
    if not (st["runtime"] and st["model"]) and not (fetching["running"] or installing["running"]):
        if engine.online():
            box.operator("autoposer.download", icon='IMPORT', text=engine.download_label())
        else:
            from ..mmcp_client import draw_offline
            draw_offline(box)
    box.prop(prefs, "auto_install_runtime")

    header, body = layout.panel("animatica_prefs_poser_advanced", default_closed=True)
    header.label(text="Advanced")
    if body is None:
        return

    # Where the model comes from. Only meaningful when someone is overriding
    # the default, which is the whole reason this is behind a fold.
    col = body.column(align=True)
    col.prop(prefs, "model_source", expand=True)
    if prefs.model_source == "HF":
        col.prop(prefs, "hf_repo")
        r = col.row(align=True)
        r.prop(prefs, "hf_subfolder")
        r.prop(prefs, "hf_revision")
        col.prop(prefs, "hf_token")
    elif prefs.model_source == "LOCAL":
        col.prop(prefs, "local_path")

    if st["model"]:
        where = body.column(align=True)
        where.active = False
        where.label(text=st["model_dir"], icon='FILE_FOLDER')
        if st["model_source"]:
            where.label(text=f"from {st['model_source']}")

    row = body.row(align=True)
    row.operator("autoposer.get_model", icon='IMPORT',
                 text="Download" if not st["model"] else "Re-download")
    row.operator("autoposer.check", icon='CHECKMARK', text="Self-test")
    row = body.row(align=True)
    if not st["runtime"]:
        row.operator("autoposer.install_runtime", icon='IMPORT')
    if st["model"]:
        row.operator("autoposer.forget_model", icon='TRASH')

    body.separator()
    body.prop(prefs, "cache_dir")
    body.prop(prefs, "threads")


CLASSES = (AP_OT_install_runtime, AP_OT_get_model, AP_OT_download, AP_OT_check,
           AP_OT_forget_model)
