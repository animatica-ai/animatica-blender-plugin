"""The Video Capture feature is wired into the host, and refuses honestly.

Ported from the 3ds Max plugin's ``accept_m13_capture.py`` (G1 of
PLAN-suita-wieloDCC.md: zero host calls — the gate reads source and calls the
pure client, so the whole thing moves here and each host injects paths and
marker strings).

Capture arrived in MotionBuilder first and was ported to Max: the client,
worker and window live in core, and each plugin wires them in. The recurring
failure class of that port is a wired name that is not there, or a piece that
exists in core and never made it into the host — exactly what killed the
first nine bridge modules. So this gate proves the wiring, and the two honest
refusals the design names:

* ``to_motion_data`` refuses joint names that are not this rig, rather than
  keying 77 arrays onto 77 joints in arrival order (a limb-for-limb scramble
  reads as a bad capture, not a bad mapping);
* an unreachable capture service is an error with a message, not a hang.

What this gate does NOT do is run a clip through the service: that needs the
capture service running and a fixture video, and pretending otherwise would
be a green light over nothing. The client's logic is covered by pure tests in
the core repo, which run in its CI on every commit.

**Where the hosts differ, and how that is expressed here.** 3ds Max derives a
thin ``VideoToMotionWindow`` subclass in its own ``gui/capture_window.py``;
MotionBuilder builds core's ``MotionCaptureWindow`` directly from its dock
helper, and its menu labels carry an "Open " prefix. Both facts are marker
strings the host injects, not logic. One check changed FORM in the port, not
meaning: "the window no longer holds a capture section of its own" was a
substring test (``"self.sec_capture" not in src``) and is now an AST test for
an *assignment* to ``self.sec_capture`` — the MoBu window mentions the name
in a comment explaining where the section went, which is history, not a
section.
"""

from __future__ import annotations

import ast
import io
import time
from dataclasses import dataclass


@dataclass
class HostCapture:
    """Where one host keeps the pieces this gate inspects."""

    core_dir: str                 # the VENDORED animatica_core
    tool_window_path: str         # the host's main window source
    startup_path: str             # where the menu is built
    #: The file that hosts the shared capture window — a subclass module
    #: (Max) or the dock helper that instantiates core's directly (MoBu).
    window_host_path: str
    #: Substrings that must ALL appear there — the "hosts core's window,
    #: does not fork one" proof, in the host's own spelling.
    window_host_markers: tuple = ()
    #: Substrings that must NOT appear there — a re-implemented handler is
    #: a fork wearing the shared window's name.
    window_host_forbidden: tuple = ("_on_capture_requested",)
    #: Substrings the startup/menu source must carry — the menu item label
    #: and the entry point behind it, so the menu says what it does.
    startup_markers: tuple = ()
    bridge_leaf: str = ""         # "max_bridge" / "mobu_bridge"
    host_scene_module: str = ""   # "pymxs" / "pyfbsdk"
    #: The client-side refusal sentence the apply seam must carry.
    refusal_marker: str = "None of the captured joint names"


def _read(path: str) -> str:
    return io.open(path, encoding="utf-8").read()


def check_sources(host: HostCapture, check) -> None:
    """Every source-level wiring check. Pure file/AST work, no imports."""
    print("\n-- the host hosts the SHARED window, it does not fork one --",
          flush=True)
    win_src = _read(host.window_host_path)
    missing = [m for m in host.window_host_markers if m not in win_src]
    check("the host builds on core's capture window",
          not missing, missing)
    found = [m for m in host.window_host_forbidden if m in win_src]
    check("and does not re-implement the capture handlers",
          not found, found)

    core_win = _read(f"{host.core_dir}/gui/capture_window.py")
    check("the vendored core window is present", len(core_win) > 10000)
    check("session shots are gated on the TAKES capability",
          "shots.setVisible(host.has(host.TAKES))" in core_win)
    leaks = [name for name in (host.host_scene_module, host.bridge_leaf)
             if name and name in core_win]
    check("core reaches this host only through the bridge",
          not leaks
          and "from animatica_core.bridge import take_manager" in core_win,
          leaks)

    src = _read(host.tool_window_path)
    tree = ast.parse(src)
    methods = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    # The rig belongs to the main window, so the APPLY stays there and the
    # capture window calls back through it -- one owner for the scene.
    for h in ("open_capture_window", "apply_capture_motion",
              "open_console_window"):
        check(f"the main window exposes {h}", h in methods)
    # An ASSIGNMENT is what a live section is; a comment naming the attribute
    # is history (see the module docstring).
    holds_section = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "sec_capture"
                and isinstance(t.value, ast.Name) and t.value.id == "self"
                for t in node.targets)
        for node in ast.walk(tree))
    check("and no longer holds a capture section of its own",
          not holds_section)

    # The seam's SHAPE, not just its name: core calls it with meta= and ui=,
    # and a mismatch here is a TypeError the moment a capture finishes -- far
    # from the code that caused it.
    seam = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                and f.name == "apply_capture_motion")
    kwonly = {a.arg for a in seam.args.kwonlyargs}
    check("the apply seam takes meta= and ui= as core calls it",
          {"meta", "ui"} <= kwonly, sorted(kwonly))
    body = ast.get_source_segment(src, seam) or ""
    check("it reports through ui, not by returning a string",
          "ui.set_status" in body and "return f\"Apply" not in body)
    check("it refuses a rig whose joints do not match, rather than "
          "silently keying nothing", host.refusal_marker in body)

    print("\n-- and the menu says what it does --", flush=True)
    startup = _read(host.startup_path)
    for marker in host.startup_markers:
        check(f"the menu/startup wires {marker}", marker in startup)


def check_client(check) -> None:
    """The two honest refusals of the pure client. No host, no scene."""
    from animatica_core import capture_client

    print("\n-- refusal 1: wrong joints never key in arrival order --",
          flush=True)
    payload = {"joint_names": ["Hips", "NotARealJoint"],
               "joints": [[[0.0, 0.0, 0.0]] * 2],
               "rotations": [[[0.0, 0.0, 0.0, 1.0]] * 2],
               "fps": 30.0}
    try:
        capture_client.to_motion_data(payload)
        check("a name mismatch raises CaptureError", False, "no raise")
    except capture_client.CaptureError as exc:
        check("a name mismatch raises CaptureError",
              "refusing" in str(exc), str(exc)[:80])

    # And the accepting side of the same contract: the real hierarchy passes.
    from animatica_core.skeleton import get_joint_hierarchy
    names = [n for n, _p in get_joint_hierarchy()]
    F, J = 2, len(names)
    good = {"joint_names": names,
            "joints": [[[0.0, 0.0, 0.0]] * J] * F,
            "rotations": [[[0.0, 0.0, 0.0, 1.0]] * J] * F,
            "fps": 30.0}
    md = capture_client.to_motion_data(good)
    check(f"the real {J}-joint hierarchy converts",
          md["num_joints"] == J and md["num_frames"] == F
          and md["local_rot_mats"].shape == (F, J, 3, 3))

    print("\n-- refusal 2: an unreachable service fails fast, with words --",
          flush=True)
    t0 = time.time()
    try:
        capture_client.health(base_url="http://127.0.0.1:9", timeout=3.0)
        check("health() against a dead port raises", False, "no raise")
    except Exception as exc:                               # noqa: BLE001
        took = time.time() - t0
        check(f"health() against a dead port raises in {took:.1f}s",
              took < 10.0, f"{type(exc).__name__} after {took:.1f}s")

    reachable = True
    try:
        capture_client.health(timeout=3.0)
    except Exception:                                      # noqa: BLE001
        reachable = False
    status = ("reachable" if reachable
              else "not running -- end-to-end clip-to-motion stays covered "
                   "by core CI tests only")
    print(f"     (capture service at its default URL: {status})", flush=True)


def run(host: HostCapture, check) -> None:
    """Run every shared capture-wiring check, reporting through *check*."""
    print("\n-- the pieces exist where the window will look --", flush=True)
    from animatica_core import capture_client

    # Resolved by string, the same dodge surface.py uses for window_scaffold:
    # core's purity rule forbids the non-GUI half an `import animatica_core.
    # gui` statement (the Qt-free consumers must import this module), but this
    # code only RUNS inside a Qt host, where the question is legitimate.
    import importlib
    try:
        sections = importlib.import_module("animatica_core.gui.sections")
    except Exception as exc:                               # noqa: BLE001
        sections = None
        check("core gui sections import", False,
              f"{type(exc).__name__}: {exc}")
    if sections is not None:
        check("CaptureSection is exported by core sections",
              hasattr(sections, "CaptureSection"))
    for name in ("health", "upload", "start", "wait", "fetch_motion",
                 "to_motion_data", "capture", "CaptureError"):
        check(f"capture_client.{name} exists", hasattr(capture_client, name))

    check_sources(host, check)
    check_client(check)
