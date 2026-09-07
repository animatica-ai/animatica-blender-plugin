"""Scene events reach Python, and the UI guards hold (gate m6, shared half).

Ported from the 3ds Max plugin's ``accept_m6_host_events.py`` (final gate
tranche of PLAN-suita-wieloDCC.md). The story it pins: the GUI subscribed to
the host's File New/Open events, bracketed bulk edits with
``ui_updates_paused``, and polled idle -- and every one of those seams once
died with ``ModuleNotFoundError`` in a second host because the calls named
the wrong DCC. This gate drives the CONTRACT half of those seams against a
real scene: handlers that actually fire, removal that detaches exactly what
its registry names, an install that skips unknown event names, the paused
context that re-enables on the exception path, and an idle pair that is
honest in BOTH of its allowed shapes.

Reset topology (the decided design, cleanup-before-re-reset): this gate
calls ``scene_api.new_scene()`` three times -- to fire the New handler, to
prove partial removal, and to prove full removal -- and it may, because it
puts NOTHING in the scene: handlers are application state, the saved file is
filesystem state, and the scene is empty at every reset. The one file it
writes is its own temp copy of an empty scene (``save_scene`` /
``load_scene`` -- this gate is the suite's only consumer of the pair), and
it deletes that file itself on the way out. The verb keeps its full Q1(b)
refusal; there is nothing here to refuse over.

What the host injects (:class:`HostHostEvents`): the native scene-file
suffix (Max ``.max``, MoBu ``.fbx``) -- ``save_scene`` hands the path to the
host verbatim, and a host asked to save a scene under a foreign extension is
allowed to refuse.

What stays in the MAX wrapper, deliberately (the conservatism rule -- these
assert Max-shaped facts, not contract facts):

* the take-name round-trip section -- it pins Max's take SHIM ("Max has no
  takes, so the name must still round-trip"); MoBu's takes are real and
  answer differently by design;
* the busy-counter re-entrancy (``busy_begin``/``busy_end``/``busy()``) --
  declared Max extras; MoBu's ``is_busy`` reads live recorder/plotter state
  and has no counter to exercise;
* ``is_character_track(...) is False`` and the ``apply_to_target
  (mode='story')`` refusal -- both mean "Max has no Story", and MoBu has;
* ``installed_events`` and ``remove_scene_handlers(None)`` -- declared
  extras outside the contract (the contract registry is opaque, so the
  shared gate observes removal behaviourally instead);
* the ``tool_window.py`` source scans (no reachable pyfbsdk import, QTimer
  follower) -- they pin Max's own GUI file.

One deliberate direction change, test-pinned: the Max gate proved partial
removal by handing ``remove_scene_handlers`` a LIST OF NAMES and reading
``installed_events`` back. Both are Max extras -- the contract registry is
opaque ("hand it back", possibly job ids). The same meaning ("removal
detaches ONLY what the caller's registry names, never a module-wide
teardown") is proven here by installing the two events as two separate
registries, removing one, and watching which handler still fires.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass


@dataclass
class HostHostEvents:
    """What one host tells this gate about its scene files."""

    #: The host's native scene-file suffix, dot included (".max" / ".fbx").
    scene_suffix: str


def run(host: HostHostEvents, check):
    """Run the shared m6 checks, reporting through *check*.

    Cleans up after itself completely -- handlers removed, temp file gone --
    even when a check body raises, so the wrapper has nothing to undo.
    """
    from animatica_core.bridge import scene_events, time_bridge
    from animatica_core.gates import scene_api

    scene_api.new_scene()

    print("\n-- scene evaluation, and busy outside any bulk operation --",
          flush=True)
    time_bridge.evaluate()
    check("evaluate() does not raise", True)
    check("is_busy reports idle outside any bulk operation",
          time_bridge.is_busy() is False, time_bridge.is_busy())

    fired = []
    reg_new = reg_open = None
    tmp_dir = tempfile.mkdtemp(prefix="animatica_m6_")
    try:
        print("\n-- scene events actually fire --", flush=True)
        # Two SEPARATE registries on purpose: removal is proven per-registry
        # below, and the contract keeps each registry opaque.
        reg_new = scene_events.install_scene_handlers({
            "OnFileNewCompleted": lambda: fired.append("new"),
            "NotAnEvent": lambda: fired.append("bogus"),
        })
        reg_open = scene_events.install_scene_handlers({
            "OnFileOpenCompleted": lambda: fired.append("open"),
        })
        check("install skips unknown event names (1 of 2 attached)",
              len(reg_new) == 1, reg_new)
        check("and attaches what it can (the Open registry)",
              len(reg_open) == 1, reg_open)

        # The scene is still empty -- this gate has created nothing -- so the
        # verb's Q1(b) discriminator passes honestly at every re-reset.
        scene_api.new_scene()
        check("a scene reset reaches the Python handler",
              "new" in fired, fired)
        check("and no unknown-event handler ever fires",
              "bogus" not in fired, fired)

        scene_path = scene_api.save_scene(
            os.path.join(tmp_dir, "animatica_events_probe"
                         + host.scene_suffix))
        fired.clear()
        scene_api.load_scene(scene_path)
        check("opening a file reaches the Python handler",
              "open" in fired, fired)

        print("\n-- removal detaches ONLY what its registry names --",
              flush=True)
        scene_events.remove_scene_handlers(reg_new)
        reg_new = None
        fired.clear()
        scene_api.new_scene()
        check("the removed New handler stays silent", "new" not in fired,
              fired)
        scene_api.load_scene(scene_path)
        check("the other registry's Open handler still fires",
              "open" in fired, fired)

        scene_events.remove_scene_handlers(reg_open)
        reg_open = None
        fired.clear()
        scene_api.new_scene()
        scene_api.load_scene(scene_path)
        check("full uninstall really detaches", fired == [], fired)
    finally:
        # Teardown must not depend on the checks above having survived.
        for reg in (reg_new, reg_open):
            if reg:
                try:
                    scene_events.remove_scene_handlers(reg)
                except Exception:                           # noqa: BLE001
                    pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # Leave an empty scene, not the loaded temp copy of one.
        scene_api.new_scene()

    print("\n-- the UI guards hold --", flush=True)
    # ui_updates_paused is contract-MUST: it must round-trip without wedging
    # the viewport, exception path included.
    with scene_events.ui_updates_paused():
        pass
    try:
        with scene_events.ui_updates_paused():
            raise ValueError("boom")
    except ValueError:
        pass
    check("ui_updates_paused survives use and re-enables on exception", True)

    # The idle pair is contract-MAY-STUB with exactly two honest shapes: both
    # calls work, or both refuse with a reason. A mixed answer -- add works
    # but remove refuses, or the reverse -- would leak handlers or refuse a
    # cleanup, and a silent half is the one forbidden behaviour. Max refuses
    # (its wrapper pins that specifically); MoBu serves.
    probe = lambda: None                                    # noqa: E731
    answers = []
    for fn in (scene_events.add_idle_handler,
               scene_events.remove_idle_handler):
        try:
            fn(probe)
            answers.append("served")
        except RuntimeError:
            answers.append("refused")
    check("the idle pair answers as one (both served or both refuse)",
          answers[0] == answers[1], answers)
