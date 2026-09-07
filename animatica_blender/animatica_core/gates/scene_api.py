"""The eleven scene verbs a shared gate is allowed to speak.

PLAN-suita-wieloDCC.md §2: the "product behaviour" gate family needs eleven
scene operations, and a gate that named ``rt.resetMaxFile`` or
``FBApplication().FileNew()`` would stop being shared. This module is the
vocabulary instead — every verb reaches the host through
``animatica_core.bridge``, so a gate imports ONE module and no host.

**Deliberately NOT named ``hostapi``**: ``animatica_core.host`` already
exists and means something else (host capabilities — ``TAKES``,
``CONTROL_RIG``…). Two modules with confusably close names in one package is
a ready-made import mistake.

Six verbs were already in the bridge contract and are wrapped here so gates
have one import site: ``current_fps``, the take range, the playhead trio,
units, a world-transform read, ``is_alive``. Five groups are new, and their
host halves live in each bridge's ``scene_ops`` module: ``new_scene``,
``save_scene``/``load_scene``, the selection trio, ``key_times``/
``key_count``, ``frame_viewport``.

**``new_scene`` carries Q1 variant (b), and the semantics live in the VERB,
not in gates.** In ``3dsmaxbatch`` the scene starts empty in a throwaway
process and a reset is free — the Max gates call it 27 times. In
MotionBuilder gates run inside the LIVE application, where the open scene is
the user's work and a ``FileNew()`` would delete it without asking. So under
MoBu ``scene_ops.new_scene`` MUST refuse — raising :class:`SceneNotEmptyError`
— unless the scene is empty by the property-based discriminators of
:func:`scene_content_findings`; the user opens an empty scene (File > New)
when they want scene gates to run. A batch host whose scene is born empty may
reset unconditionally. Shared gate code never learns which host it is on.

Every bridge import below is function-local, per core's purity rule
(``tests/test_core_purity.py`` enforces it): registration happens at plugin
startup, after this module can already be imported.
"""

from __future__ import annotations

#: The two properties :func:`key_times` / :func:`key_count` speak about.
#: Validated HERE so every host refuses an unknown property the same way,
#: before any scene is touched.
KEY_PROPS = ("translation", "rotation")


class SceneNotEmptyError(RuntimeError):
    """``new_scene`` refused: the open scene holds user content (Q1 b).

    Raised by a host whose gates run inside the live application. The message
    names what was found and the fix (open an empty scene), because a refusal
    that does not say why reads as a bug in the refusal.
    """


def scene_content_findings(*, user_cams, foreign_roots, take_count,
                           character_count) -> list:
    """What in a probed scene is USER content — by properties, not counts.

    The one implementation of the Q1(b) discriminator, shared by the MoBu
    launcher (``run_gates_mobu.py``, probing over the console) and the MoBu
    bridge's ``scene_ops.new_scene`` (probing in-process) so the two cannot
    drift. A raw component count is useless here: an empty default
    MotionBuilder scene reports 121 components (7 Producer cameras, keying
    groups, tools…, measured live), and the plugin adds its own scaffolding
    at startup. Each discriminator was verified on a live scene:

    * a non-system camera is user content (all seven defaults report
      ``SystemCamera == True`` — a clean discriminator);
    * a scene root NOT named ``Animatica*`` is user content (the plugin's
      own scaffolding must not make every scene "dirty");
    * more than one take, or any Character, is user content.

    Returns the list of findings — empty means the scene is safe to reset.
    """
    findings = []
    user_cams = list(user_cams)
    foreign_roots = list(foreign_roots)
    if user_cams:
        findings.append(f"non-system cameras: {user_cams[:3]}")
    if foreign_roots:
        findings.append(f"root models: {foreign_roots[:3]}")
    if take_count != 1:
        findings.append(f"{take_count} takes")
    if character_count:
        findings.append(f"{character_count} character(s)")
    return findings


# ---------------------------------------------------------------------------
# The five new groups — host half in <bridge>/scene_ops.py
# ---------------------------------------------------------------------------

def new_scene() -> None:
    """Reset to an empty scene — refusing where the scene is the user's work.

    Q1 variant (b) — see the module docstring. Raises
    :class:`SceneNotEmptyError` on a live host whose open scene holds user
    content; a batch host resets unconditionally. Never a silent no-op.
    """
    from animatica_core.bridge import scene_ops
    scene_ops.new_scene()


def save_scene(path) -> str:
    """Save the scene to *path* (never the user's own file). Returns the path."""
    from animatica_core.bridge import scene_ops
    return scene_ops.save_scene(path)


def load_scene(path) -> None:
    """Open the scene file at *path*, replacing the current scene.

    No emptiness check here — mid-gate the scene holds the gate's own
    content, which is the point of loading. The protection for the user's
    work is ``new_scene``'s refusal plus the launcher's suite-level check.
    """
    from animatica_core.bridge import scene_ops
    scene_ops.load_scene(path)


def selection() -> list:
    """The currently selected models, as host node objects."""
    from animatica_core.bridge import scene_ops
    return scene_ops.selection()


def select(nodes) -> None:
    """Make exactly *nodes* the selection (replaces, never additive)."""
    from animatica_core.bridge import scene_ops
    scene_ops.select(nodes)


def clear_selection() -> None:
    """Deselect everything."""
    from animatica_core.bridge import scene_ops
    scene_ops.clear_selection()


def key_times(node, prop) -> list:
    """Sorted unique key times, in SECONDS, on *node*'s *prop*.

    *prop* is one of :data:`KEY_PROPS`. Seconds, not frames, because that is
    how every key this plugin writes is placed (contract §1: key times are
    computed from seconds so they survive a scene frame-rate change). The
    union across the property's axis curves — a key shared by three axes is
    one time. ``[]`` when the property is not animated.
    """
    if prop not in KEY_PROPS:
        raise ValueError(f"unknown property {prop!r}; expected one of "
                         f"{KEY_PROPS}")
    from animatica_core.bridge import scene_ops
    return scene_ops.key_times(node, prop)


def key_count(node, prop) -> int:
    """``len(key_times(node, prop))`` — see there for the semantics."""
    if prop not in KEY_PROPS:
        raise ValueError(f"unknown property {prop!r}; expected one of "
                         f"{KEY_PROPS}")
    from animatica_core.bridge import scene_ops
    return scene_ops.key_count(node, prop)


def frame_viewport() -> None:
    """Frame the viewport on the selection (or everything, if none).

    The demo gate's single host coupling (select + zoomext + redraw in Max).
    A host that cannot frame — no viewport, no framing API — must raise with
    a reason, never silently no-op: a gate that "framed" nothing would
    screenshot an empty corner and call it a demo.
    """
    from animatica_core.bridge import scene_ops
    scene_ops.frame_viewport()


# ---------------------------------------------------------------------------
# The six verbs the contract already had — wrapped so gates import ONE module
# ---------------------------------------------------------------------------

def current_fps() -> float:
    """Real frames-per-second, never an enum ordinal (contract §3.4)."""
    from animatica_core.bridge import time_bridge
    return time_bridge.current_fps()


def take_range() -> tuple:
    """``(start, end)`` frame span of the current take/animation."""
    from animatica_core.bridge import time_bridge
    return time_bridge.take_range()


def set_take_range(start, stop) -> None:
    from animatica_core.bridge import time_bridge
    time_bridge.set_take_range(start, stop)


def current_frame() -> int:
    from animatica_core.bridge import time_bridge
    return time_bridge.current_frame()


def goto_frame(frame) -> None:
    from animatica_core.bridge import time_bridge
    time_bridge.goto_frame(frame)


def seek_and_evaluate(frame) -> None:
    """Seek AND force an evaluation — required before any per-frame read."""
    from animatica_core.bridge import time_bridge
    time_bridge.seek_and_evaluate(frame)


def units() -> str:
    """``"meters"`` — a constant, by contract §1, not a host question.

    The bridge boundary is meters in every host; conversions live INSIDE
    each bridge. The verb exists so a gate can assert the boundary without
    knowing any host's native unit.
    """
    return "meters"


def is_alive(model) -> bool:
    """True iff the wrapper's underlying host object still exists."""
    from animatica_core.bridge import skeleton
    return skeleton.is_alive(model)


def world_position_m(joint_map, joint_name, frame) -> tuple:
    """World position of a joint at *frame*, in meters (the transform read).

    Routed through ``constraint_capture.sample_effector_position`` — the
    contract's existing world-read, which seeks and evaluates first.
    """
    from animatica_core.bridge import constraint_capture
    value = constraint_capture.sample_effector_position(
        joint_map, joint_name, frame)
    return tuple(value["position"])
