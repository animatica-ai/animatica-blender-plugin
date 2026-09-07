"""Standalone widget-preview window — runs outside MotionBuilder.

Usage::

    python -m animatica_core.gui.preview

Spawns a QApplication, shows one ``CollapsibleSection`` per design section
populated with each atomic widget so the Ember palette can be reviewed
visually without launching MoBu.
"""

import sys

from .qt_compat import QtWidgets
from . import styles
from .widgets import (
    CollapsibleSection, SubSection,
    Pill, Field, Btn, IconBtn, Segment, Check, Toggle, NumberInput, TextInput,
)


def build_preview() -> QtWidgets.QWidget:
    root = QtWidgets.QWidget()
    root.setWindowTitle("Animatica — Widget Preview (Ember)")
    root.resize(560, 820)
    root.setStyleSheet(styles.complete_stylesheet())

    scroll = QtWidgets.QScrollArea(root)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)

    content = QtWidgets.QWidget()
    scroll.setWidget(content)
    col = QtWidgets.QVBoxLayout(content)
    col.setContentsMargins(16, 16, 16, 16)
    col.setSpacing(10)

    # -- 01 Skeleton --------------------------------------------------------
    s1 = CollapsibleSection("Skeleton", step=1, icon="bone",
                            right=Pill("ready", tone="success", dot=True))
    s1.body_layout.addWidget(Field("Namespace", TextInput("animatica", mono=True)))
    row = QtWidgets.QHBoxLayout()
    row.addWidget(Btn("Create Skeleton", icon="plus", variant="solid"))
    row.addWidget(Btn("HIK Skeleton", icon="check", variant="surface"))
    row.addWidget(Btn("Delete", icon="trash", variant="danger"))
    s1.body_layout.addLayout(row)

    sub = SubSection("Constraints", right=Pill("3 active", tone="muted"))
    sub.body_layout.addWidget(Check("Keep constraint keyframes", checked=True))
    sub.body_layout.addWidget(
        Field("Constraint type",
              Segment([("fb", "Full Body"), ("lb", "Lower Body"),
                       ("ub", "Upper Body")], value="fb"))
    )
    sub_row = QtWidgets.QHBoxLayout()
    sub_row.addWidget(Btn("Create Constraint", icon="plus"))
    sub_row.addWidget(Btn("From Animation Curves", icon="folder"))
    sub.body_layout.addLayout(sub_row)
    s1.body_layout.addWidget(sub)
    col.addWidget(s1)

    # -- 02 Motion Import ---------------------------------------------------
    s2 = CollapsibleSection("Motion Import", step=2, icon="folderOpen")
    path_row = QtWidgets.QHBoxLayout()
    path_row.addWidget(TextInput("", placeholder="path/to/motion.npz", mono=True))
    path_row.addWidget(Btn("Browse", icon="folder", variant="surface", size="sm"))
    s2.body_layout.addWidget(Field("Motion file", None))
    s2.body_layout.addLayout(path_row)
    s2.body_layout.addWidget(Btn("Load Motion", icon="wand", variant="solid"))
    col.addWidget(s2)

    # -- 03 Text to Motion --------------------------------------------------
    s3 = CollapsibleSection("Text to Motion", step=3, icon="spark",
                            right=Pill("idle", tone="muted", dot=True))
    s3.body_layout.addWidget(
        Field("Backend",
              Segment([("server", "Server")], value="server"))
    )
    s3.body_layout.addWidget(Field("Server URL", TextInput("http://localhost:8000", mono=True)))
    gen_sub = SubSection("General",
                         right=Toggle(checked=False))
    grid = QtWidgets.QHBoxLayout()
    grid.addWidget(Field("Diffusion steps", NumberInput(100, minimum=1, maximum=500)))
    grid.addWidget(Field("Seed", NumberInput(2056164011)))
    gen_sub.body_layout.addLayout(grid)
    gen_sub.body_layout.addWidget(Check("Random seed", checked=True))
    s3.body_layout.addWidget(gen_sub)
    s3.body_layout.addWidget(Btn("Generate Motion", icon="spark",
                                 variant="solid", size="lg"))
    col.addWidget(s3)

    # -- 04 Single Pose -----------------------------------------------------
    s4 = CollapsibleSection("Single Pose", step=4, icon="bone")
    s4.body_layout.addWidget(Field("Pose prompt",
                                    TextInput("", placeholder="A guarded fighting stance.")))
    s4.body_layout.addWidget(Check("Use current position", checked=True,
                                    sublabel="Key the pose at the rig's scene XZ + facing"))
    s4.body_layout.addWidget(Check("Keep current height", checked=True,
                                    sublabel="On: keep the rig's hip height. Off: seat on the ground plane"))
    s4.body_layout.addWidget(Check("Auto apply as constraint", checked=False))
    s4.body_layout.addWidget(Check("Key pose", checked=True,
                                    sublabel="Bake keyframe at current frame"))
    s4.body_layout.addWidget(Btn("Generate Pose at Current Frame",
                                  icon="wand", variant="soft"))
    col.addWidget(s4)

    # -- 05 Console (stub) --------------------------------------------------
    s5 = CollapsibleSection("Console", step=5, icon="terminal",
                            right=Pill("2 entries", tone="muted"))
    log = QtWidgets.QTextEdit()
    log.setReadOnly(True)
    log.setObjectName("status_field")
    log.setPlainText("13:27  info  Ready.\n13:27  ok    Connected to localhost:8000.")
    log.setMinimumHeight(110)
    s5.body_layout.addWidget(log)
    row = QtWidgets.QHBoxLayout()
    row.addStretch(1)
    row.addWidget(IconBtn("check", title="Copy"))
    row.addWidget(IconBtn("trash", title="Clear", danger=True))
    s5.body_layout.addLayout(row)
    col.addWidget(s5)

    outer = QtWidgets.QVBoxLayout(root)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.addWidget(scroll)
    return root


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = build_preview()
    w.show()
    return app.exec() if hasattr(app, "exec") else app.exec_()


if __name__ == "__main__":
    sys.exit(main())
