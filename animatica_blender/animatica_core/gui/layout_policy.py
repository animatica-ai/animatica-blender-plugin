"""Which parts of the shared tool window Compact mode folds away.

A persisted switch puts a window in a short column: the rarely-touched
containers inside each card are hidden and the workflow — pick a rig, write a
prompt, Generate — is what is left on screen. There is one such switch PER
WINDOW: ``AppState.ui_compact`` folds the tool column, ``ui_compact_settings``
folds the Settings window, and neither reaches into the other. The table
below is keyed by section either way. The rule this module states is the whole
of that policy, and it is stated here rather than inside the Qt sections for
two reasons.

The first is hosts. Every section registers its hideable containers under a
string key and answers ``set_compact(bool)``; *which* keys go away is asked
of :func:`hidden_parts`. A second host (3ds Max) that builds the same
sections therefore inherits Compact without reproducing a single decision.

The second is testability. A rule that lives inside a widget can only be
checked by building a widget, in a DCC that will not start on CI. Here it is
plain data over plain strings, so the table is a unit test and the keys are
pinned to the sections by an AST pass in ``tests/`` — every key below must
appear as a string constant in its own section's source, which is what stops
this table drifting into a private vocabulary nobody registers.

Compact does NOT restyle anything. The cards' chrome — header heights, body
padding, column spacing — is one tight size in both modes, fixed in
``CollapsibleSection`` / ``SubSection`` and ``styles``, so this table is the
whole difference between Compact and Full: what is on screen, never how much
room it takes.

The module reads no state of its own: ``compact`` arrives as an argument and
the hint takes the state it is handed. It never touches either flag.

Two properties are load-bearing:

* **Compact hides, it never resets.** Nothing here writes to the state. A
  window switched to Compact and back must present exactly the values it had,
  including the ones the user set through a control Compact took off screen.
* **Hidden is not gone.** Because a hidden control keeps working, a value the
  user cannot see can still change the generated result — so
  :func:`hint_text` names those under the Generate button, as a one-line
  reason to switch back to Full. ``COMPACT_HINT_FIELDS`` is deliberately the
  wire-and-apply subset: fields that only change what the window *shows*
  (namespace, the canonical-rig filter, the path-length and marker-name
  toggles, the custom take-name text) are excluded, because reporting them
  would train the operator to ignore the line.
"""

from __future__ import annotations

from animatica_core.core.prompt_model import AppState


#: The sections that answer ``set_compact``. The vocabulary is the section's,
#: not the host's: ``timeline_header`` is the strip inside the floating
#: timeline window, not a card in the column.
SECTIONS: tuple[str, ...] = (
    "model",
    "skeleton",
    "generate",
    "constraints",
    "pose",
    "live",
    "settings",
    "timeline_header",
)


#: Container keys each section hides in Compact. A section absent from a
#: value's set keeps that container on screen; an empty set means the whole
#: section is untouched.
#:
#: ``model`` is the first thing the workflow asks for and has nothing to
#: spare. ``live`` is likewise empty, but for the opposite reason: Live Drive
#: has no interior to fold, so the whole card is the unit — the scaffold
#: shows it on ``show_live_drive and not compact`` and this table stays out
#: of it.
COMPACT_HIDDEN: dict[str, frozenset[str]] = {
    "model": frozenset(),
    "skeleton": frozenset({
        "canonical_filter", "namespace", "auto_create",
        "adopt", "link_model_rig", "hik_status",
    }),
    "generate": frozenset({
        "adv_samples_cfg", "adv_heading", "adv_root_margin", "output_target",
    }),
    "constraints": frozenset({
        "convert_animkeys", "keyframes_row", "path_display", "nav",
    }),
    "pose": frozenset({"pose_use_xz", "auto_constraint", "key_pose"}),
    "live": frozenset(),
    # Keyboard Shortcuts stay in Compact (Matt, 2026-09-08).
    "settings": frozenset({"naming", "rigs", "debug", "extra"}),
    # The timeline dock is NOT compacted (Matt, 2026-09-08): the key stays
    # so the contract is uniform, the set is empty on purpose.
    "timeline_header": frozenset(),
}


def hidden_parts(section: str, compact: bool) -> frozenset[str]:
    """The container keys *section* hides right now.

    ``compact=False`` is an empty set for every section — Full mode hides
    nothing, and saying so here spares each caller the same ``if``.

    An unknown *section* is a wiring mistake (a card that registered under a
    name this table has never heard of), so it raises rather than quietly
    hiding nothing and leaving the host looking merely unhelpful.
    """
    try:
        parts = COMPACT_HIDDEN[section]
    except KeyError:
        raise KeyError(
            f"no Compact policy for section {section!r}; the sections this "
            f"module knows are: {', '.join(SECTIONS)}"
        ) from None
    return parts if compact else frozenset()


#: ``(attribute, label)`` for every option whose control Compact hides AND
#: whose value changes the generated (or applied) result. Order is the order
#: the hint reads them out in; labels are the on-screen control's own words,
#: so "switch to Full" leads somewhere the operator recognises.
#:
#: ``use_local_skeleton`` is deliberately absent: it has no control in any
#: section, so naming it here would send the operator to a Full window that
#: does not show it either.
#:
#: ``pose_use_xz`` carries a section prefix because its control says "Use
#: current position", word for word the same as ``use_hip_pos`` over in
#: Generate; the labels are what the hint prints, so they have to tell the
#: two apart.
COMPACT_HINT_FIELDS: tuple[tuple[str, str], ...] = (
    ("animation_mode", "Output target"),
    ("num_samples", "Num samples"),
    ("cfg_type", "CFG type"),
    ("heading_deg", "Initial heading"),
    ("post_processing", "Server post-processing"),
    ("use_hip_pos", "Use current position"),
    ("preserve_height", "Preserve height"),
    ("ground_offset_enabled", "Apply ground offset"),
    ("base_layer_only", "Generate on base layer only"),
    ("bake_to_control_rig", "Bake to Control Rig after import"),
    ("story_passthrough", "Passthrough"),
    ("story_overwrite_fbx", "Replace current Clip"),
    ("auto_take_name", "Auto-name from first prompt"),
    ("bake_whole_range", "Bake whole time range"),
    ("skip_root_joint", "Skip top joint when sending hierarchy"),
    ("compensate_group_scale", "Compensate group scale"),
    ("match_scene_fps", "Match scene FPS"),
    ("ground_correction_enabled", "Correct ground offset"),
    ("reorder_same_frame_waypoints",
     "Path waypoint wins over same-frame hand/foot pins"),
    ("duplicate_seam_waypoints", "Duplicate path waypoints at block seams"),
    ("debug_capture", "Debug: save request / response JSON"),
    ("debug_omit_timing", "Debug: omit timing block"),
    ("debug_omit_root_anchor", "Debug: omit frame-0 root anchor"),
    ("debug_send_effector_rotations", "Debug: send effector rotations"),
    ("debug_send_scene_fps", "Debug: send scene FPS in timing"),
    ("keep_keyframes", "Keep constraint keyframes"),
    ("pose_use_xz", "Pose: use current position"),
    ("auto_constraint", "Auto apply as constraint"),
    ("key_pose", "Key pose"),
)


# The yardstick every hint value is measured against. Built once at import:
# a fresh AppState is cheap but not free, and the hint is recomputed on every
# patch. Never mutated — it is read through getattr and nothing else.
_DEFAULT = AppState()


def _render(value: object) -> str:
    """One value as the hint shows it.

    Booleans read as on/off — a checkbox reported as ``True`` says what the
    field holds, not what the user did. Floats drop their trailing zeros so
    an untouched-looking ``0.04`` does not arrive as ``0.04000000000000001``.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def active_hidden_options(state) -> list[tuple[str, object]]:
    """``(label, value)`` for every hint field *state* has moved off default.

    Compared with ``!=`` against the module's cached default state, so a
    float matches by value and a mode string by content.
    """
    return [
        (label, getattr(state, field))
        for field, label in COMPACT_HINT_FIELDS
        if getattr(state, field) != getattr(_DEFAULT, field)
    ]


def hint_text(state, max_items: int = 3) -> str:
    """The one-line note under Generate, or ``""`` when there is nothing to say.

    Silence is the common case and the right default: an operator who never
    left the defaults should see no line at all, which is what makes the line
    worth reading when it does appear.
    """
    differing = active_hidden_options(state)
    if not differing:
        return ""

    shown = differing[:max_items]
    body = ", ".join(f"{label} = {_render(value)}" for label, value in shown)
    if len(differing) > len(shown):
        body += f", +{len(differing) - len(shown)} more"

    count = len(differing)
    head = ("1 hidden option differs" if count == 1
            else f"{count} hidden options differ")
    return f"{head} from defaults: {body} — switch to Full to review"
