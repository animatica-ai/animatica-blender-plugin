"""Modeless chooser for the shipped example prompt files.

Lists every ``*.json`` in ``resources.example_prompts_dir()``, rescanned on
every open (``reload_listing``) so files added to the folder appear without a MoBu
restart. Each row shows the filename plus its prompt-block count (cheap
metadata read via ``prompt_store_json.load_from_file``).

The dialog is *modeless*: the owner (``tool_window.open_example_prompt_chooser``)
holds one persistent instance and connects ``accepted`` to the load, so the
viewport and timeline stay live while it is up -- which matters because
``tool_window._load_example_prompts`` inserts at the CURRENT playhead, so the
user can scrub to the insertion point with the window open.
"""

from __future__ import annotations

import os

from .qt_compat import QtCore, QtWidgets


class ExamplePromptsDialog(QtWidgets.QDialog):
    """Pick one example prompt file; styled by the parent's stylesheet cascade."""

    def __init__(self, parent=None, prompts_dir: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Load Example Prompt")
        self.setMinimumWidth(380)
        # Explicit: this dialog is shown with show(), never exec_() -- a modal
        # run would block MoBu's viewport and timeline while it is up.
        self.setModal(False)

        if prompts_dir is None:
            from .. import resources
            prompts_dir = resources.example_prompts_dir()
        self._dir = prompts_dir

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(
            "Pick an example — its prompt blocks are inserted at the playhead,\n"
            "keeping any blocks already on the timeline."
        ))

        self._list = QtWidgets.QListWidget()
        self._list.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self._list)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText("Load")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._populate()

    def reload_listing(self) -> None:
        """Rescan the examples folder. Called by the owner on every open so the
        persistent instance never shows a stale listing. Deliberately NOT named
        ``refresh`` -- that name already means "reconcile the viz branch"
        (``constraint_viz.refresh``) and "repaint a section" (``sec_*.refresh``).
        """
        self._populate()

    def _populate(self) -> None:
        from .timeline.prompt_store_json import load_from_file
        self._list.clear()
        try:
            names = sorted(
                n for n in os.listdir(self._dir) if n.lower().endswith(".json")
            )
        except OSError:
            names = []
        for name in names:
            path = os.path.join(self._dir, name)
            try:
                count = len(load_from_file(path))
            except Exception:
                count = 0
            plural = "" if count == 1 else "s"
            item = QtWidgets.QListWidgetItem(
                f"{os.path.splitext(name)[0]}   ({count} prompt{plural})"
            )
            item.setData(QtCore.Qt.UserRole, path)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)

    def chosen_path(self) -> str | None:
        item = self._list.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else None
