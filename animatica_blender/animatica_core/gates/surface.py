"""Every name the GUI reaches for actually exists — checked without running it.

A plugin window reaches modules through a bridge and a vendored core, and both
resolve at CALL time. So a name that moved, or was never written, does not fail
when the plugin loads: it fails when a user presses the button, as a traceback
in a host console nobody is watching. That is a bad way to find out.

This is the check both plugins were doing with two hand-written copies. The
copies had already drifted -- one of them treated ``from package import
submodule`` as a missing name, which is legal Python and reported every such
import as broken. One implementation, parameterised by :class:`HostSurface`,
is how that stops happening.

What is NOT here is anything only one host can have: 3ds Max's
``_reports_exceptions`` decorator, for instance, guards entry points against a
Qt slot dying silently into the Listener, and MotionBuilder has no Listener.
Those stay in the host's own gate.

Nor is the PERSISTED_FIELDS check, for a different reason. It needs
``animatica_core.gui.window_state``, and core's purity rule forbids the non-GUI
half from importing the GUI half -- that half is what Blender consumes, and
Blender has no Qt. Four duplicated lines in each host gate cost less than a
hole in that rule, so the check stays there.

Everything here is `ast` and `importlib`. No DCC is touched, so a host can run
it against whatever scene the user has open.
"""

from __future__ import annotations

import ast
import importlib
import io
import os
from dataclasses import dataclass


@dataclass
class HostSurface:
    """Where one plugin keeps the things this module inspects."""

    root: str                 # repo root
    package: str              # "animatica_to_max"
    bridge_package: str       # "animatica_to_max.max_bridge"
    window_module: str        # "animatica_to_max.gui.tool_window"
    window_path: str          # .../gui/tool_window.py
    core_dir: str             # the VENDORED animatica_core
    core_repo: str = ""       # upstream checkout, if this machine has one
    window_class: str = "AnimaticaToolWindow"
    min_sections: int = 8
    #: How the vendored tree is refreshed, named in the failure message so the
    #: reader is told the fix rather than left to find it.
    resync_hint: str = "sync_core.py --write"


def _discover_sections(tree, min_sections):
    """``(sec_class, scaffold_mod, scaffold_fields)`` for a host window.

    Two variants of window construction, one map out:

    * hand-built — ``self.sec_x = XSection(...)`` assignments in the host
      window's own source (the pre-S3b shape);
    * scaffold — the window takes its cards from core's
      ``window_scaffold.build_sections()``, so the assignments above find
      NOTHING in the host file. Measured live in MoBu, where this check
      reported "0 sections found" against ten working cards. There the
      ``Sections`` NamedTuple is the authority: every field is a
      ``sec_<field>`` attribute on the window, typed by its annotation.

    The scaffold import is attempted only when the hand scan comes up short,
    and its failure (no Qt, pre-S3b vendor) leaves the hand-built verdict
    untouched.
    """
    sec_class = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for t in node.targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"
                        and t.attr.startswith("sec_")):
                    f = node.value.func
                    sec_class[t.attr] = (f.id if isinstance(f, ast.Name)
                                         else getattr(f, "attr", None))
    scaffold_mod, scaffold_fields = None, ()
    if len(sec_class) < min_sections:
        try:
            scaffold_mod = importlib.import_module(
                "animatica_core.gui.window_scaffold")
        except Exception:
            scaffold_mod = None
        if scaffold_mod is not None:
            anno = getattr(getattr(scaffold_mod, "Sections", None),
                           "__annotations__", {})
            scaffold_fields = tuple(f"sec_{field}" for field in anno)
            for field, cls_name in anno.items():
                if not isinstance(cls_name, str):
                    cls_name = getattr(cls_name, "__name__", None)
                sec_class.setdefault(f"sec_{field}", cls_name)
    return sec_class, scaffold_mod, scaffold_fields


def _scopes(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _bridge_aliases(scope, suffix):
    alias = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.ImportFrom) and node.module \
                and node.module.endswith(suffix):
            for a in node.names:
                alias[a.asname or a.name] = a.name
    return alias


def _attribute_sweep(tree, surface, importer, suffix, origin):
    """Every ``mod.attr`` where *mod* came from a bridge import.

    ``seen`` is a SET, not a counter. A module-level alias is visible both when
    walking the module tree (which contains the function bodies) and when
    walking each function, so counting occurrences reported every access twice
    -- "all 8" where four exist. The number is in the label a reader trusts.
    """
    missing, seen = set(), set()
    for scope in [tree, *_scopes(tree)]:
        alias = _bridge_aliases(scope, suffix)
        if not alias:
            continue
        for node in ast.walk(scope):
            if not (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)):
                continue
            mod_name = alias.get(node.value.id)
            if mod_name is None:
                continue
            mod = importer(mod_name)
            if isinstance(mod, Exception):
                missing.add(f"{mod_name} does not import: {mod}")
                continue
            where = origin(scope, node)
            seen.add(f"{mod_name}.{node.attr} @ {where}")
            if not hasattr(mod, node.attr):
                missing.add(f"{mod_name}.{node.attr} <- {where}")
    return missing, seen


def run(surface: HostSurface, check) -> None:
    """Run every host-neutral surface check, reporting through *check*.

    *check* is the host gate's own ``(label, ok, detail)`` reporter, so the
    output reads the same as the rest of that gate.
    """
    src = io.open(surface.window_path, encoding="utf-8").read()
    tree = ast.parse(src)
    cache: dict = {}

    def bridge(name):
        key = f"{surface.bridge_package}.{name}"
        if key not in cache:
            try:
                cache[key] = importlib.import_module(key)
            except Exception as exc:                       # noqa: BLE001
                cache[key] = exc
        return cache[key]

    bridge_leaf = surface.bridge_package.rsplit(".", 1)[-1]

    print("\n-- every attribute read off a bridge module --", flush=True)
    missing, seen = _attribute_sweep(
        tree, surface, bridge, bridge_leaf,
        lambda scope, node: (f"{getattr(scope, 'name', '<module>')}() "
                             f"line {node.lineno}"))
    check(f"all {len(seen)} bridge attributes resolve", not missing,
          sorted(missing))

    print("\n-- every bridge symbol CORE names, resolved against this host --",
          flush=True)
    # Core reaches the host through `animatica_core.bridge`, forwarded at call
    # time. A symbol core expects and this host never wrote raises only when a
    # user clicks the thing, deep inside a shared window.
    core_missing, core_seen = set(), set()
    for dirpath, dirs, files in os.walk(surface.core_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                ctree = ast.parse(io.open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            rel = os.path.relpath(path, surface.root)
            m, s = _attribute_sweep(
                ctree, surface, bridge, "animatica_core.bridge",
                lambda scope, node, rel=rel: f"{rel}:{node.lineno}")
            core_missing |= m
            core_seen |= s
    check(f"all {len(core_seen)} bridge symbols core names exist in "
          f"{bridge_leaf}",
          not core_missing, sorted(core_missing))

    print("\n-- every name imported from animatica_core --", flush=True)
    bad, names = set(), set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ImportFrom) and node.module
                and node.module.startswith("animatica_core")):
            continue
        try:
            mod = importlib.import_module(node.module)
        except Exception as exc:                           # noqa: BLE001
            bad.add(f"{node.module} (line {node.lineno}): "
                    f"{type(exc).__name__}: {exc}")
            continue
        for a in node.names:
            if a.name == "*":
                continue
            names.add(f"{node.module}.{a.name}")
            if hasattr(mod, a.name):
                continue
            # `from package import submodule` is legal even though the
            # submodule is not an attribute until something imports it.
            # Asking hasattr alone calls every such import broken.
            try:
                importlib.import_module(f"{node.module}.{a.name}")
            except Exception:                              # noqa: BLE001
                bad.add(f"{node.module}.{a.name} (line {node.lineno})")
    check(f"all {len(names)} core imports resolve", not bad, sorted(bad))

    print("\n-- every self.<method> the window calls actually resolves --",
          flush=True)
    # The sweeps above see module attributes, not methods on self. When a
    # method moves out to a core mixin, nothing else notices whether the mixin
    # was inherited. Resolved through the MRO, so inherited counts as defined.
    host_window_mod = importlib.import_module(surface.window_module)
    window = getattr(host_window_mod, surface.window_class)
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and isinstance(node.func.value, ast.Name)
              and node.func.value.id == "self"}
    unresolved = sorted(a for a in called if not hasattr(window, a))
    check(f"all {len(called)} self.<method> calls resolve on the class",
          not unresolved, unresolved)

    print("\n-- every method called on a GUI section --", flush=True)
    sec_class, scaffold_mod, scaffold_fields = _discover_sections(
        tree, surface.min_sections)

    def _sec_cls(name):
        return (getattr(host_window_mod, name or "", None)
                or getattr(scaffold_mod, name or "", None))

    bad_methods, seen_methods = set(), 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "self"
                and node.value.attr in sec_class):
            continue
        cls = _sec_cls(sec_class[node.value.attr])
        if cls is None:
            continue
        seen_methods += 1
        if not hasattr(cls, node.attr):
            bad_methods.add(f"{sec_class[node.value.attr]}.{node.attr} "
                            f"(self.{node.value.attr}, line {node.lineno})")
    scaffold_complete = bool(scaffold_fields) and all(
        name in sec_class for name in scaffold_fields)
    check(f"{len(sec_class)} sections found"
          + (" (via window_scaffold.Sections)" if scaffold_fields else ""),
          len(sec_class) >= surface.min_sections or scaffold_complete,
          sec_class)
    check(f"all {seen_methods} section calls resolve",
          not bad_methods, sorted(bad_methods))

    check_vendored(surface, check)


def check_vendored(surface: HostSurface, check) -> None:
    """The vendored core must be a copy of the PINNED core commit, not a fork.

    One comparison, one authority: :func:`animatica_core.vendoring.compare`
    reads the same pin ``sync_core`` enforces, so this check can no longer
    disagree with it by comparing against whatever the SDK working tree
    happens to hold (a vendored tree that matches its pin used to read as
    "differs" the moment core grew a newer commit).
    """
    print("\n-- the vendored core must be a copy, not a fork --", flush=True)
    if not surface.core_repo or not os.path.isdir(surface.core_repo):
        # "Cannot tell" is not "fine". A check that passes on cannot-tell is
        # worse than one that says so.
        print(f"  SKIP the vendored tree cannot be compared -- no core "
              f"checkout at {surface.core_repo or '<unset>'}", flush=True)
        return

    from animatica_core import vendoring
    cfg = vendoring.VendorConfig(surface.root, surface.core_repo)
    cfg.dst = surface.core_dir
    try:
        mismatched, missing, extra = vendoring.compare(cfg)
        stale, edited = (vendoring.classify(cfg, mismatched)
                         if mismatched else ([], []))
    except Exception as exc:  # noqa: BLE001 -- no git / bad pin: say so
        print(f"  SKIP the vendored tree cannot be compared -- {exc}",
              flush=True)
        return

    # Stale and edited both read as "differs" and call for OPPOSITE fixes:
    # stale is cured by re-vendoring, edited means someone changed core through
    # the copy and the change has to go upstream first. Saying only "edited"
    # cost three commands to disprove the last time it fired.
    parts = []
    if stale:
        parts.append(f"STALE -- re-vendor ({surface.resync_hint}): "
                     f"{[rel for rel, _ in stale][:5]}")
    if edited:
        parts.append(f"EDITED IN PLACE -- the change must reach core first: "
                     f"{edited[:5]}")
    check(f"the vendored core matches the pinned commit "
          f"({len(mismatched)} differ)", not mismatched, "; ".join(parts))
    check(f"no core file is missing from the vendored tree ({len(missing)})",
          not missing, missing[:5])
    check(f"nothing vendored that core no longer has ({len(extra)})",
          not extra, extra[:5])
