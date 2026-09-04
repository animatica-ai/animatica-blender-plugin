"""The server cassette — recorded MMCP responses, replayed by request hash.

Stage P2 of PLAN-testy-ab.md (§2.3). Generation checkpoints run by default
on a RECORDED server response: GPU generation is not deterministic even with
a seed (to be confirmed by P0(a)), and a plugin deploy must not need a live
server. The key is :func:`animatica_core.gates.ab_compare.canonical_request_hash`
— one definition, shared with layer 1, so the float jitter a profile absorbs
(measured-field rounding) is absorbed for the cassette too, in one place.

The store is one JSON file per entry in a directory: ``{key}.json`` holding
``{"request": <canonical>, "profile": <the profile it was canonicalized under>, "response": ..., "recorded_at": ..., "meta": ...}``.
One file per entry so a golden refresh diffs entry-by-entry in review, and
a partial re-record does not rewrite the neighbours' bytes.

A hash miss during replay means the plugin changed its request. That is a
layer-1 verdict, not a stack trace: :class:`CassetteMiss` carries a readable
diff of the canonicalized request against the NEAREST stored entry (fewest
differing lines) and the instruction to re-record — never a bare
``KeyError`` (§2.3: "czytelny FAIL z diffem i instrukcją, nie tajemniczy
wyjątek").

Wall-clock time is injected (``clock=``), and only :meth:`Cassette.record`
ever calls it — nothing time-shaped can leak into the hashing path.
"""

from __future__ import annotations

import difflib
import json
import os
import time

from animatica_core.gates.ab_compare import (canonical_request_hash,
                                             canonical_request_json,
                                             canonicalize_request)

#: What a miss tells a human to do (asserted verbatim by the teeth tests
#: and printed verbatim by the P4 orchestrator's docs).
MISS_INSTRUCTION = "intended change? re-record with --record-golden"


class CassetteMiss(LookupError):
    """No recorded response for this request — with the diff that says why.

    Deliberately NOT a ``KeyError``: a bare KeyError at replay time reads as
    a bug in the harness, when what actually happened is that the plugin now
    builds a different request than the one the golden was recorded on.
    """


class Cassette:
    """Record/replay store for one directory of ``{key}.json`` entries."""

    def __init__(self, directory, profile, clock=None):
        self.directory = directory
        self.profile = profile
        # The clock feeds ONLY `recorded_at` metadata. Injected so tests are
        # deterministic; never consulted by key_for/replay.
        self._clock = clock if clock is not None else time.time

    def key_for(self, request):
        return canonical_request_hash(request, self.profile)

    def _path(self, key):
        return os.path.join(self.directory, f"{key}.json")

    def record(self, request, response, meta=None):
        """Store *response* under the request's canonical hash; return key."""
        key = self.key_for(request)
        entry = {
            "request": canonicalize_request(request, self.profile),
            # The profile the stored request was canonicalized under. A
            # profile change migrates the cassette by re-keying (measured
            # 2026-09-02: 24 entries, three hosts, zero re-recording), and
            # an entry with no authored copy on the tree can only be re-keyed
            # safely when the OLD profile is known to compose with the new
            # one (its measured/volatile sets a subset). Without this stamp
            # that check is a guess; with it, the migration can refuse with
            # words.
            "profile": self.profile,
            "response": response,
            "recorded_at": self._clock(),
            "meta": meta or {},
        }
        os.makedirs(self.directory, exist_ok=True)
        with open(self._path(key), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(entry, fh, sort_keys=True, indent=1)
            fh.write("\n")
        return key

    def keys(self):
        """Stored entry keys, sorted — the review-diff order."""
        if not os.path.isdir(self.directory):
            return []
        return sorted(fn[:-len(".json")] for fn in os.listdir(self.directory)
                      if fn.endswith(".json"))

    def _load(self, key):
        with open(self._path(key), encoding="utf-8") as fh:
            return json.load(fh)

    def replay(self, request):
        """The recorded response, or a :class:`CassetteMiss` that says why."""
        key = self.key_for(request)
        path = self._path(key)
        if os.path.isfile(path):
            return self._load(key)["response"]
        raise CassetteMiss(self._miss_message(request, key))

    def _miss_message(self, request, key):
        want = canonical_request_json(request, self.profile).splitlines()
        keys = self.keys()
        if not keys:
            return (f"cassette miss in {self.directory}: no entry for "
                    f"request {key[:12]}, and the cassette is EMPTY — "
                    f"nothing was ever recorded here. {MISS_INSTRUCTION}")
        # Nearest = fewest differing lines against the canonical JSON. Linear
        # scan; a cassette holds a handful of checkpoints, not thousands.
        best_key, best_diff, best_cost = None, [], None
        for other in keys:
            stored = json.dumps(self._load(other)["request"],
                                sort_keys=True, indent=1).splitlines()
            diff = list(difflib.unified_diff(
                stored, want, fromfile=f"recorded {other[:12]}",
                tofile="this request", lineterm="", n=1))
            cost = sum(1 for ln in diff
                       if ln[:1] in "+-" and ln[:3] not in ("+++", "---"))
            if best_cost is None or cost < best_cost:
                best_key, best_diff, best_cost = other, diff, cost
        shown = "\n".join(best_diff[:60])
        if len(best_diff) > 60:
            shown += f"\n... ({len(best_diff) - 60} more diff lines)"
        return (f"cassette miss in {self.directory}: no entry for request "
                f"{key[:12]}. The plugin now builds a different request than "
                f"the recorded golden. Nearest entry {best_key[:12]} differs "
                f"on {best_cost} line(s):\n{shown}\n{MISS_INSTRUCTION}")
