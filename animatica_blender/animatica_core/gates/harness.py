"""Running a suite of acceptance gates, minus the part that starts one.

Extracted from the 3ds Max plugin's ``run_all_gates.py``, which was the only
suite that existed. Measured before the split: of its 383 lines, 118 were
launcher work (finding ``3dsmaxbatch``, writing its stdout-capture runner,
spawning it, retrying its ADP hiccup) and the rest was not about 3ds Max at
all. This is that rest.

What a host supplies is one callable::

    def launch(gate_file: str, timeout_s: int, log_dir: str) -> GateRun

and this module does the rest: order the gates, probe the server once, read
each gate's verdict, print the table, pick the exit code.

**Verdicts are three, not two.** SKIP is a real answer -- no FBX plugin, no
core checkout, no server -- and collapsing it into FAIL trains people to
ignore red. A SKIP with no reason is worse than useless, though, so the reason
is carried into the table; a gate that skips silently is indistinguishable
from one that quietly does nothing.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

#: A gate reports its own verdict on its last matching line, e.g.
#: ``M8 REAPPLY: PASS`` or ``S1 RESULT: SKIP -- no FBX plugin``.
VERDICT_RE = re.compile(
    r"\b[A-Z0-9][A-Z0-9 _.-]*:\s*(PASS|FAIL|SKIP)\b(?:\s*[-–—]{1,2}\s*(?P<why>.*))?$")

#: ``===== 13/13 checks passed =====``
CHECKS_RE = re.compile(r"=====\s*(\d+)\s*/\s*(\d+)\s+checks passed\s*=====")


@dataclass
class GateRun:
    """What a launcher hands back: the gate's captured output, and how it went."""

    text: str = ""
    returncode: int = 0
    duration: float = 0.0
    timed_out: bool = False
    launch_error: str = ""


@dataclass
class GateResult:
    name: str
    status: str                    # PASS, FAIL, or SKIP
    checks: str | None             # "13/13", or None if the gate reports none
    duration: float
    note: str = ""


@dataclass
class Suite:
    """One host's suite: where the gates are, and how to start one."""

    scripts_dir: str
    launch: object                 # (gate_file, timeout_s, log_dir) -> GateRun
    order: tuple = ()              # names pinned to the front, in this order
    tail: tuple = ()               # names pinned to the end, in this order
    pattern: str = r"accept_.*\.py$"
    server: str = ""
    server_fallback: frozenset = field(default_factory=frozenset)


def natural_key(filename: str):
    """Sort by milestone number, not lexicographically.

    Plain sorting puts ``accept_m10_`` before ``accept_m2_``. The filename is
    the tiebreaker within one milestone, so the four ``accept_m2_*`` gates keep
    a stable order among themselves.
    """
    m = re.search(r"_m(\d+)_", filename)
    return (int(m.group(1)) if m else 0, filename)


def discover(suite: Suite) -> list[str]:
    """Gate filenames in run order: pinned head, matched middle, pinned tail."""
    names = [f for f in suite.order
             if os.path.isfile(os.path.join(suite.scripts_dir, f))]
    names.extend(sorted(
        (f for f in os.listdir(suite.scripts_dir) if re.match(suite.pattern, f)),
        key=natural_key))
    names.extend(f for f in suite.tail
                 if os.path.isfile(os.path.join(suite.scripts_dir, f)))
    return names


def needs_server(suite: Suite, gate_file: str) -> bool:
    """Whether a gate talks to the MMCP server.

    Read out of the gate rather than listed here, because a list drifts from
    what it describes. The bare word "capabilities" appears in unrelated checks
    (a host-capability assertion, say), so the match is on the call signals --
    the SERVER env var or the literal ``/capabilities`` path -- not the word.

    ``server_fallback`` covers gates that reach the server through a helper
    and so carry neither signal. It is a documented safety net, not the
    primary path.
    """
    path = os.path.join(suite.scripts_dir, gate_file)
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        text = ""
    if re.search(r"\bANIMATICA_SERVER\b", text) or re.search(r"\bSERVER\s*=", text) \
            or "/capabilities" in text:
        return True
    return gate_file in suite.server_fallback


def probe_server(server: str) -> tuple[bool, str]:
    """Ask once, up front.

    Otherwise every server-dependent gate burns its own multi-minute timeout
    discovering the same thing.
    """
    try:
        urllib.request.urlopen(f"{server}/capabilities", timeout=10)
        return True, ""
    except Exception as exc:                               # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def read_verdict(run: GateRun, gate_file: str, timeout_s: int) -> GateResult:
    """Turn a launcher's output into a verdict. The parsing, all in one place."""
    if run.launch_error:
        return GateResult(gate_file, "FAIL", None, run.duration,
                          f"could not launch: {run.launch_error}")
    if run.timed_out:
        return GateResult(gate_file, "FAIL", None, run.duration,
                          f"exceeded {timeout_s}s timeout, killed")

    checks = None
    m = CHECKS_RE.search(run.text)
    if m:
        checks = f"{m.group(1)}/{m.group(2)}"

    for line in reversed(run.text.splitlines()):
        vm = VERDICT_RE.search(line.strip())
        if vm:
            # A skip that does not say why is indistinguishable from a gate
            # that quietly does nothing, so the reason rides into the table.
            return GateResult(gate_file, vm.group(1), checks, run.duration,
                              (vm.group("why") or "").strip())

    tail = (run.text.strip().splitlines()[-1] if run.text.strip()
            else f"exit {run.returncode}, no output captured")
    return GateResult(gate_file, "FAIL", checks, run.duration,
                      f"no PASS/FAIL/SKIP line in output -- {tail}")


def print_table(results: list[GateResult]) -> None:
    if not results:
        print("no gates ran")
        return
    name_w = max(len(r.name) for r in results)
    checks_w = max([len(r.checks or "-") for r in results] + [len("CHECKS")])
    print()
    print(f"{'GATE':<{name_w}}  {'STATUS':<6}  {'CHECKS':<{checks_w}}  "
          f"{'TIME':>7}  NOTE")
    for r in results:
        print(f"{r.name:<{name_w}}  {r.status:<6}  {(r.checks or '-'):<{checks_w}}  "
              f"{r.duration:>6.1f}s  {r.note}")


def write_suite_verdicts(path: str, suite: str, host: str,
                         results: list[GateResult], reason: str = "") -> dict:
    """Write the cross-host ``suite-verdicts.json`` contract; returns it.

    ONE schema for every host's sweep (PLAN-raport-kampanii, P1b/c): the Max
    harness and the MoBu telnet launcher write the same file so the campaign
    report reads one shape and never re-parses logs::

        {"suite": <suite>, "host": <host>, "reason": "",
         "gates": [{"id", "title", "verdict": "PASS|FAIL|SKIP",
                    "detail", "checks", "time"}]}

    * ``gates`` keeps RUN ORDER — deterministic, like the printed table;
    * ``id`` is the gate filename without ``.py``; ``title`` the filename as
      run (hosts have no richer name at this layer, and inventing one here
      would drift from the file the verdict came from);
    * ``detail`` is the table's NOTE column (a SKIP's reason, a FAIL's
      words), ``checks`` the ``"13/13"`` string or ``""`` when the gate
      reports none, ``time`` wall seconds rounded to 0.1 (wall clock is the
      one field exempt from byte determinism);
    * an early exit (unreachable console, refused scene, nothing matched)
      still writes the file: empty ``gates`` plus the worded ``reason`` —
      a sweep that never ran must leave a record saying so, not no file.
    """
    payload = {
        "suite": suite,
        "host": host,
        "reason": reason,
        "gates": [{
            "id": r.name[:-3] if r.name.endswith(".py") else r.name,
            "title": r.name,
            "verdict": r.status,
            "detail": r.note,
            "checks": r.checks or "",
            "time": round(r.duration, 1),
        } for r in results],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return payload


def summarise(results: list[GateResult]) -> tuple[int, int, int]:
    """``(passed, failed, skipped)``."""
    return (sum(1 for r in results if r.status == "PASS"),
            sum(1 for r in results if r.status == "FAIL"),
            sum(1 for r in results if r.status == "SKIP"))


def run(suite: Suite, gates: list[str], timeout_s: int, log_dir: str,
        skip_reason_for=None) -> list[GateResult]:
    """Run *gates* through the suite's launcher, in order.

    *skip_reason_for* is an optional ``(gate_file) -> str | None``; a reason
    turns into a SKIP without the gate being started at all.
    """
    results = []
    for gate_file in gates:
        reason = skip_reason_for(gate_file) if skip_reason_for else None
        if reason:
            print(f"skipping {gate_file} -- {reason}", flush=True)
            results.append(GateResult(gate_file, "SKIP", None, 0.0, reason))
            continue
        print(f"running {gate_file} ...", flush=True)
        started = time.time()
        run_out = suite.launch(gate_file, timeout_s, log_dir)
        if not run_out.duration:
            run_out.duration = time.time() - started
        res = read_verdict(run_out, gate_file, timeout_s)
        print(f"  {res.status} ({res.checks or '-'}, {res.duration:.1f}s)"
              f"{' -- ' + res.note if res.note else ''}", flush=True)
        results.append(res)
    return results
