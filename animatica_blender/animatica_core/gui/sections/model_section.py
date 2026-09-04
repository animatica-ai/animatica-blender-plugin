"""00 · Model — which model generates, and the knobs only it has.

Split out of Text to Motion so the choice comes first, before anything
that depends on it: the model decides the canonical skeleton, the frame
rate, how long a single clip may be, and whether a trajectory has to be
supplied. Settings shared by every model (diffusion steps, seed,
duration, output target) stay in their own sections.

Read-only against ``AppState``; the host feeds it the server's model list
through :meth:`set_models` and it reports choices back through
``on_patch``.
"""

from __future__ import annotations

from ..qt_compat import QtWidgets
from ..widgets import CollapsibleSection, Combo, Field, NumberInput, Pill

#: Models that steer from a root trajectory rather than from text alone.
#: Mirrors ``core.request_builder._TRAJECTORY_DRIVEN_MODELS`` — the walk
#: speed below only means anything for these.
_TRAJECTORY_DRIVEN = frozenset({"ardy-core-rp"})


class ModelSection(QtWidgets.QWidget):
    """Numbered workflow card 00."""

    def __init__(self, state, on_patch, parent=None):
        super().__init__(parent)
        self._state = state
        self._on_patch = on_patch
        self._caps_by_id: dict = {}

        self._pill = Pill("—", tone="info")
        self._section = CollapsibleSection(
            "Model", step=0, icon="spark", right=self._pill, open=True,
        )
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._section)
        body = self._section.body_layout

        self._combo = Combo([(state.model, state.model)], value=state.model)
        self._combo.valueChanged.connect(self._on_model_changed)
        body.addWidget(Field("Model", self._combo))

        # What the server says about the chosen model. Facts, not settings:
        # they change with the dropdown and explain why the same prompt
        # behaves differently between models.
        self._facts = QtWidgets.QLabel("")
        self._facts.setObjectName("field_hint")
        self._facts.setWordWrap(True)
        body.addWidget(self._facts)

        # -- Model-specific controls ---------------------------------------
        # Shown only for the models they apply to; a knob that does nothing
        # for the current model is worse than no knob.
        self._walk_row = QtWidgets.QWidget()
        walk_layout = QtWidgets.QVBoxLayout(self._walk_row)
        walk_layout.setContentsMargins(0, 0, 0, 0)
        self._walk_speed = NumberInput(
            value=float(getattr(state, "walk_speed_ms", 1.4)),
            minimum=0.1, maximum=5.0, step=0.1,
        )
        self._walk_speed.valueChanged.connect(
            lambda v: self._on_patch({"walk_speed_ms": float(v)}))
        walk_layout.addWidget(Field(
            "Default walk speed (m/s)", self._walk_speed,
            hint="Used when no Path is drawn."))
        body.addWidget(self._walk_row)

        self.refresh()

    # ------------------------------------------------------------------

    def _on_model_changed(self, value):
        self._on_patch({"model": value})
        self.refresh()

    def set_models(self, ids: list) -> None:
        """Populate the dropdown from the server's advertised model ids.

        No-op on an empty list so the offline default seeded at
        construction survives a failed probe.
        """
        if not ids:
            return
        prev = self._combo.value() or self._state.model
        self._combo.blockSignals(True)
        self._combo.set_items([(i, i) for i in ids])
        self._combo.blockSignals(False)
        if prev in ids:
            self._combo.setValue(prev)
        elif self._state.model in ids:
            self._combo.setValue(self._state.model)
        else:
            self._combo.setValue(ids[0])
            self._on_patch({"model": ids[0]})
        self.refresh()

    def set_capabilities(self, caps) -> None:
        """Remember ``/capabilities`` so the facts line can quote it."""
        models = (caps or {}).get("models") or []
        self._caps_by_id = {m.get("id"): m for m in models
                            if isinstance(m, dict) and m.get("id")}
        self.refresh()

    def refresh(self) -> None:
        model_id = self._combo.value() or self._state.model or ""
        info = self._caps_by_id.get(model_id) or {}

        limits = info.get("limits") or {}
        fps = info.get("fps")
        joints = len((info.get("canonical_skeleton") or {}).get("joints") or [])
        max_seconds = limits.get("max_duration_seconds")
        bits = []
        if fps:
            bits.append(f"{float(fps):g} fps")
        if joints:
            bits.append(f"{joints}-joint skeleton")
        if max_seconds:
            bits.append(f"up to {float(max_seconds):g}s per request")
        if model_id in _TRAJECTORY_DRIVEN:
            bits.append("steers from a trajectory")
        self._facts.setText(
            " · ".join(bits) if bits
            else "Connect to a server to read this model's capabilities.")

        self._pill.set_text(model_id.split("-")[0] if model_id else "—")
        self._walk_row.setVisible(model_id in _TRAJECTORY_DRIVEN)
