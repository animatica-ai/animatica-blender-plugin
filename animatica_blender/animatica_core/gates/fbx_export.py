"""The FBX export surface keeps its shape — and a stub host keeps refusing.

Ported from the 3ds Max plugin's ``accept_m12_export.py`` (G1 of
PLAN-suita-wieloDCC.md: zero host calls, so the whole gate moves here and each
host injects its constants).

The two hosts are NOT in the same situation, and the port must not flatten
that difference:

* **3ds Max** ships ``fbx_exporter`` as a documented post-v1 stub
  (BRIDGE-CONTRACT module 14). There the gate's verdict is **SKIP by
  design** — the SKIP *is* the content: both entry points must keep refusing
  with the sentence naming the workaround, and neither refusal may leave a
  half-written file behind. A change that quietly made either function
  "succeed" without producing a real export is exactly the regression this
  catches.
* **MotionBuilder** ships a real exporter. Driving it needs a scene with a
  rig and a take, which this gate deliberately does not touch — so there the
  gate certifies the callable surface and the documented signatures, and its
  verdict is PASS on that scope.

What is identical in both hosts, and therefore lives here: the entry points
exist, are callable, and keep the signatures ``tool_window.py`` calls them
with positionally — a drift there breaks the call site silently until someone
presses Export.
"""

from __future__ import annotations

import importlib
import inspect
import os
from dataclasses import dataclass

#: The documented signatures (BRIDGE-CONTRACT.md §3.14) — identical in every
#: host, which is what makes this check shareable at all.
RANGE_SIGNATURE = ["root_model", "start_frame", "end_frame", "export_dir"]
TAKE_SIGNATURE = ["root_model", "take_name", "export_dir"]

#: The verdict a stub host must report. Not PASS: there is no working export
#: to certify as working.
STUB_SKIP_REASON = (
    "fbx_exporter is a documented post-v1 stub (BRIDGE-CONTRACT module 14); "
    "confirmed it refuses honestly instead of claiming a fake success")


@dataclass
class HostExport:
    """What one host tells this gate about its export path."""

    bridge_package: str        # "animatica_to_max.max_bridge"
    #: True where the host's exporter is a documented refusal. Drives the
    #: refusal checks AND the verdict: a stub host's correct verdict is SKIP.
    stub: bool
    #: The sentence a stub's refusal must name (the workaround).
    refusal_marker: str = "Export Selected"
    #: Where a refusal must not leave files behind. Empty = TEMP.
    scratch_dir: str = ""


def run(host: HostExport, check) -> tuple[str, str]:
    """Run the shared checks, reporting through *check*.

    *check* is the host gate's own ``(label, ok, detail)`` reporter. Returns
    ``(status, reason)`` for the non-FAIL case — ``("SKIP", …)`` on a stub
    host, ``("PASS", "")`` on a real one; whether any check FAILed is the
    wrapper's own ledger to consult.
    """
    fbx_exporter = importlib.import_module(f"{host.bridge_package}.fbx_exporter")

    print("\n-- documented surface: both entry points still exist --",
          flush=True)
    check("export_range_fbx is present and callable",
          callable(getattr(fbx_exporter, "export_range_fbx", None)))
    check("export_fbx is present and callable",
          callable(getattr(fbx_exporter, "export_fbx", None)))

    # tool_window.py calls these positionally; a signature drift here breaks
    # that call site silently until someone actually presses Export.
    sig_range = list(inspect.signature(fbx_exporter.export_range_fbx).parameters)
    check("export_range_fbx keeps its documented signature",
          sig_range == RANGE_SIGNATURE, sig_range)
    sig_take = list(inspect.signature(fbx_exporter.export_fbx).parameters)
    check("export_fbx keeps its documented signature",
          sig_take == TAKE_SIGNATURE, sig_take)

    if not host.stub:
        # A real exporter is not driven from here: it needs a scene with a
        # rig and a take, and pretending otherwise would be a green light
        # over nothing. Surface + signature is this gate's honest scope.
        print("\n-- this host's exporter is real; driving it needs a scene, "
              "which this gate does not touch --", flush=True)
        return "PASS", ""

    print("\n-- both refuse honestly rather than half-exporting --",
          flush=True)
    export_dir = host.scratch_dir or os.environ.get("TEMP", ".")
    before = set(os.listdir(export_dir))

    try:
        fbx_exporter.export_range_fbx(None, 0, 10, export_dir)
        check("export_range_fbx refuses rather than claiming success",
              False, "no raise")
    except NotImplementedError as exc:
        check("export_range_fbx refuses rather than claiming success",
              host.refusal_marker in str(exc), str(exc))

    try:
        fbx_exporter.export_fbx(None, "Take001", export_dir)
        check("export_fbx refuses rather than claiming success",
              False, "no raise")
    except NotImplementedError as exc:
        check("export_fbx refuses rather than claiming success",
              host.refusal_marker in str(exc), str(exc))

    after = set(os.listdir(export_dir))
    check("neither refusal left a file behind",
          after == before, sorted(after - before))

    return "SKIP", STUB_SKIP_REASON
