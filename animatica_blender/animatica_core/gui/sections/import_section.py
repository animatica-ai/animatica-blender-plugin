"""02 · Motion Import — file picker for .npz / .bvh / .glb."""

from ..qt_compat import QtWidgets, Signal
from ..widgets import CollapsibleSection, Pill, Field, Btn, TextInput


class ImportSection(QtWidgets.QWidget):
    browse_requested = Signal()
    load_requested = Signal()

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)
        self._state = state

        self._pill = Pill("", tone="info")
        self._pill.setVisible(False)
        self._section = CollapsibleSection(
            "Motion Import", step=2, icon_ex="import_tray", right=self._pill,
        )

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)
        body = self._section.body_layout

        path = TextInput(state.motion_file, placeholder="path/to/motion.npz", mono=True)
        path.textChanged.connect(lambda v: on_patch({"motion_file": v}))
        self._path_input = path

        row = QtWidgets.QHBoxLayout()
        row.addWidget(path, 1)
        browse = Btn("Browse", icon="folder", variant="surface", size="sm")
        browse.clicked.connect(self.browse_requested.emit)
        row.addWidget(browse, 0)

        body.addWidget(Field("Motion file", None,
                              hint="NumPy archive of pose / joint data — .npz / .bvh / .glb"))
        body.addLayout(row)

        load = Btn("Load Motion", icon="wand", variant="solid")
        load.clicked.connect(self.load_requested.emit)
        body.addWidget(load)

        self.refresh()

    def set_path(self, path: str) -> None:
        self._path_input.setText(path)

    def refresh(self) -> None:
        loaded = bool(self._state.motion_file)
        self._pill.setVisible(loaded)
        if loaded:
            ext = self._state.motion_file.rsplit(".", 1)[-1].lower()
            self._pill.set_text(f".{ext} loaded")
