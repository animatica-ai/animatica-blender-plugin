"""Modal gates a tool window puts in front of a generation, in any host.

Three questions the user has to answer before some runs can proceed — regenerate
this one block with which boundaries pinned, export to Story without a character,
bake the rig over the whole take or just the applied span. None of them reads a
scene: each takes what it needs as arguments and hands back a verdict, so the
window keeps every decision about what to do with the answer.

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b, where they
sat between eight thousand lines of host-specific orchestration.
"""

from __future__ import annotations

from .qt_compat import QtWidgets


def _exec(box):
    """Run a modal, spanning the PySide2/PySide6 rename of ``exec_``."""
    return box.exec_() if hasattr(box, "exec_") else box.exec()


class GenerateSelectedDialog(QtWidgets.QDialog):
    """Confirm + configure boundary auto-pin for a single-block regeneration.

    Shows the block's prompt + frame range and two "Keep pose" checkboxes; the
    caller reads :meth:`values` on accept and injects fullbody ``pose_keyframe``
    anchors at the block boundaries. Defaults come from ``AppState`` (persisted).

    A checked boundary is pinned unconditionally — its anchor overrides a user
    Full Body pin on the same frame.
    """

    def __init__(self, parent, block, fps: float,
                 pin_start: bool = True, pin_end: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Generate Selected Prompt")
        layout = QtWidgets.QVBoxLayout(self)

        start_f = int(block.start_frame)
        end_f = int(block.end_frame)
        seconds = (end_f - start_f) / max(1.0, float(fps))
        text = (getattr(block, "text", "") or "").strip()
        layout.addWidget(QtWidgets.QLabel(f"Prompt: {text}"))
        layout.addWidget(QtWidgets.QLabel(
            f"Frames {start_f}–{end_f}  ({seconds:.2f}s)"
        ))

        self.pin_start_cb = QtWidgets.QCheckBox("Keep start pose")
        self.pin_start_cb.setToolTip(
            "Lock the regenerated motion's first frame to the pose currently "
            "at the block's first frame, so the boundary with the previous "
            "prompt stays smooth. Overrides a Full Body constraint sitting on "
            "that exact frame (it is not sent)."
        )
        self.pin_end_cb = QtWidgets.QCheckBox("Keep end pose")
        self.pin_end_cb.setToolTip(
            "Lock the regenerated motion's last frame to the pose currently at "
            "the block's end frame, so the boundary with the next prompt stays "
            "smooth. Overrides a Full Body constraint sitting on that exact "
            "frame (it is not sent)."
        )
        self.pin_start_cb.setChecked(bool(pin_start))
        self.pin_end_cb.setChecked(bool(pin_end))
        layout.addWidget(self.pin_start_cb)
        layout.addWidget(self.pin_end_cb)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText("Generate")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def values(self) -> dict:
        return {
            "pin_start": self.pin_start_cb.isChecked(),
            "pin_end": self.pin_end_cb.isChecked(),
        }


def ask_hik_required(parent, skel_name: str) -> str:
    """Modal gate before a Story export when the skeleton has no HIK character.

    Returns one of ``"create_hik"``, ``"use_anim"``, ``"cancel"``. Default
    button is Cancel — the user must explicitly opt in either way.
    """
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(QtWidgets.QMessageBox.Warning)
    box.setWindowTitle("HIK required for Story export")
    box.setText(
        f"No HIK character is mapped to '{skel_name}'.\n\n"
        "Story playback works best with a characterized skeleton "
        "(Character track). Without HIK, the clip falls back to an "
        "Animation track on the raw skeleton.\n\n"
        "HIK characterization reads the skeleton's CURRENT joint "
        "transforms — make sure the skeleton is in its neutral T-pose "
        "before creating."
    )
    btn_create = box.addButton("Create HIK", QtWidgets.QMessageBox.AcceptRole)
    btn_anim = box.addButton("Use animation track", QtWidgets.QMessageBox.ActionRole)
    btn_cancel = box.addButton(QtWidgets.QMessageBox.Cancel)
    box.setDefaultButton(btn_cancel)
    _exec(box)
    clicked = box.clickedButton()
    if clicked is btn_create:
        return "create_hik"
    if clicked is btn_anim:
        return "use_anim"
    return "cancel"


def ask_widen_scoped_bake(parent) -> bool:
    """Pre-flight widen-or-continue box for a run whose bake will be scoped.

    Returns True when the user chose to bake the whole range, so the caller
    flips the persisted setting before the request ships. Asked BEFORE the
    request so the modal never opens mid-apply — a box raised inside the apply
    path would pump a nested event loop against half-applied state.
    """
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle("Bake to Control Rig")
    box.setText(
        "\"Bake whole time range\" is off — after this run, only the "
        "applied span is plotted onto the Control Rig, and rig keys "
        "outside it are not updated.\n\n"
        "Bake the whole take instead, or continue with the scoped bake."
    )
    whole_btn = box.addButton("Bake whole range", QtWidgets.QMessageBox.AcceptRole)
    box.addButton("Continue anyway", QtWidgets.QMessageBox.RejectRole)
    box.setDefaultButton(whole_btn)
    _exec(box)
    return box.clickedButton() is whole_btn


def ask_story_path_missing(parent) -> bool:
    """Warn that a Story run has no export path. True = continue anyway.

    With no path set the clip is still created in-scene, but no FBX is
    exported — so this is a warning with a default of No, not a blocker.
    """
    resp = QtWidgets.QMessageBox.warning(
        parent,
        "No Story export path",
        "No Story export path is set.\n\n"
        "A Story clip will be created in-scene only — no FBX file "
        "will be exported.\n\nContinue anyway?",
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        QtWidgets.QMessageBox.No,
    )
    return resp == QtWidgets.QMessageBox.Yes
