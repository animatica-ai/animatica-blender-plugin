"""04 · Single Pose — pose prompt + flags + Generate Pose button."""

from ..qt_compat import QtWidgets, Signal
from ..widgets import CollapsibleSection, Field, Btn, TextInput, Check


class PoseSection(QtWidgets.QWidget):
    generate_pose_requested = Signal()

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)

        self._section = CollapsibleSection(
            "Single Pose", step=4, icon_ex="pose_figure", open=False)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)
        body = self._section.body_layout

        prompt = TextInput(state.pose_prompt,
                           placeholder="e.g. A person in a guarded fighting stance.")
        prompt.textChanged.connect(lambda v: on_patch({"pose_prompt": v}))
        self._prompt_input = prompt
        body.addWidget(Field("Pose prompt", prompt))

        body.addWidget(Field("Root / hip placement", None))
        c1 = Check("Use current position", checked=state.pose_use_xz,
                   sublabel="Key the pose at the rig's scene XZ + facing")
        c1.toggled.connect(lambda v: on_patch({"pose_use_xz": v}))
        body.addWidget(c1)
        ch = Check("Keep current height", checked=state.pose_keep_height,
                   sublabel="On: keep the rig's hip height. Off: seat on the ground plane")
        ch.toggled.connect(lambda v: on_patch({"pose_keep_height": v}))
        body.addWidget(ch)
        c2 = Check("Auto apply as constraint", checked=state.auto_constraint)
        c2.toggled.connect(lambda v: on_patch({"auto_constraint": v}))
        body.addWidget(c2)
        c3 = Check("Key pose", checked=state.key_pose,
                   sublabel="Bake keyframe at current frame")
        c3.toggled.connect(lambda v: on_patch({"key_pose": v}))
        body.addWidget(c3)

        btn = Btn("Generate Pose at Current Frame", icon="wand", variant="soft")
        btn.clicked.connect(self.generate_pose_requested.emit)
        body.addWidget(btn)

    def set_prompt(self, text: str) -> None:
        """Reflect *text* in the prompt field without re-emitting a patch.

        Used by the timeline-header pose control to mirror its inline prompt
        into this side-panel field. Signal-guarded so it does not re-enter
        ``on_patch`` (and not driven from ``refresh()``, which fires on every
        patch and would jump the caret while the user types here).
        """
        self._prompt_input.blockSignals(True)
        self._prompt_input.setText(text or "")
        self._prompt_input.blockSignals(False)

    def refresh(self) -> None:  # nothing dynamic yet
        pass
