"""The A/B comparators — request canonicalization and FBX-dump diffing.

Stage P1/P2 of PLAN-testy-ab.md. Two layers, two comparators:

* **Layer 1 — request parity.** A generation request is a function of scene
  state, and scene state after an apply carries measurement noise (a live
  MoBu run showed the hip-height anchor at 1.000 m vs 1.003 m between two
  runs of the same prompt). So "exact" means exact AFTER canonicalization:
  volatile fields dropped, MEASURED fields rounded to a precision the P0(d)
  measurement will set, AUTHORED fields (prompt texts, frames, waypoints,
  seed) untouched — an authored value that drifts is a real bug, and
  rounding it away would be silent softening.

* **Layer 2 — semantic FBX diff.** We compare dumps, not FBX bytes (FBX
  carries timestamps and unstable IDs). A dump is what
  ``animatica_core/tools/blender_dump_fbx.py`` writes: hierarchy, rest
  heads, per-frame world transforms in meters / Y-up, key counts. The
  comparator's verdicts name the bone, the frame, the axis and the numbers
  — a red line a human cannot locate from is not a verdict.

Both comparators return a **list of worded difference lines; empty means
equal**. They never print and never soften: a NaN is its own failure, a
dump that is not stamped meters/Y-up fails on the stamp instead of
reddening every position line after it.

The profile (a plain dict) is the one place a host declares what is
volatile, what is measured, and which top-level sections are host-specific
BY DESIGN (the 3ds Max plugin ships its rig as-is after server-decides;
MoBu ships the canonical block — request parity must not demand equality
of the skeleton section, PLAN-testy-ab §1 last row). Unknown profile keys
are refused: a typo in ``"volatile"`` that silently dropped nothing would
soften layer 1 without anyone noticing.

The one rule every profile lever obeys: canonicalization removes
REPRESENTATION differences, never SEMANTIC ones. Two hosts can spell the
same request differently — one materializes a default the other leaves
implicit, one repeats ``options.seed`` on every segment, one emits a
transition length for a single segment that has nothing to transition into
(measured on the Blender host vs Max/MoBu, 2026-09-02). Those spellings
generate the same motion, so ``"defaults"`` and ``"equivalences"`` fold
them; a value that would change generation (a present-but-different
default, a per-segment seed that differs from ``options.seed``) is left
exactly as it is and diffs.
"""

from __future__ import annotations

import hashlib
import json
import math

#: The one home of the layer-2 thresholds (PLAN-testy-ab §7: thresholds live
#: in one file, each value with its provenance; changing one takes a commit
#: with a numeric justification). Two axes, two sets: same-host regression
#: (axis A) and cross-host parity (axis B) are different questions with
#: different measured floors.
REGRESSION_THRESHOLDS = {
    # Measured (Max path, 2026-09-01): re-running the same scenario in the
    # host, re-running the Blender reader on the same FBX, and re-exporting
    # the same scene all produced 0.0 deviation EXACTLY; server generation
    # with a fixed seed is byte-identical (P0a, CPU). With a zero noise
    # floor, anything above the dump's own 1e-6 rounding is a real change:
    # 1e-4 m. STILL UNMEASURED: the MoBu path (P0b/d need a live MoBu) —
    # for MoBu axis A this value stays PROVISIONAL until that run.
    "position_mm": 0.1,
    # Same zero-noise measurement; with max == p95 the p95 binding only
    # fires when the max does, which is honest for a zero-noise axis.
    "position_p95_mm": 0.1,
    # PROVISIONAL — rotation noise was not measured directly; the measured
    # zero position noise implies stable rotations, and 0.01 deg absorbs
    # the dump's 1e-6 quaternion rounding (~2e-4 deg) with margin.
    "rotation_deg": 0.01,
    # PROVISIONAL — same rationale as rotation_deg.
    "rotation_p95_deg": 0.01,
}
CROSS_HOST_THRESHOLDS = {
    # Axis-B contract bound. MEASURED twice: 2026-09-01 max<->mobu on the
    # same replayed canonical response, scene fingerprints, worst 2.66 mm
    # -- and 2026-09-02 that whole 2.66 mm turned out to be ONE TICK on the
    # Max side (keys sat 1/160 frame before the integer frame: key time
    # computed through seconds, then truncated by addNewKey); after Max
    # snapped keys to the tick grid the real cross-host parity is
    # 0.001-0.002 mm (c2 2.662->0.001, c3 1.977->0.001, c4 2.310->0.002).
    # The 5 mm contract bound stays; the margin is now three orders, not 2x.
    "position_mm": 5.0,
    # PROVISIONAL — half the accepted max bound, pending a measured
    # cross-host distribution (fingerprint spec: "one runaway frame" and
    # "everything shifted" must read differently).
    "position_p95_mm": 2.5,
    # PROVISIONAL — no cross-host rotation bound was decided; 0.5 deg is a
    # guess scaled from the 5 mm bound (5 mm at a ~0.5 m limb).
    "rotation_deg": 0.5,
    # PROVISIONAL — same rationale, p95 binding.
    "rotation_p95_deg": 0.25,
}
#: The deploy gate's default is the regression axis (a deploy compares a
#: host against its own golden; cross-host runs name their axis).
DEFAULT_THRESHOLDS = REGRESSION_THRESHOLDS

_PROFILE_KEYS = frozenset(
    ("volatile", "measured", "measured_decimals", "host_sections",
     "defaults", "equivalences"))


def _check_profile(profile):
    unknown = sorted(set(profile) - _PROFILE_KEYS)
    if unknown:
        raise ValueError(
            f"unknown profile key(s) {unknown} — known keys are "
            f"{sorted(_PROFILE_KEYS)}. A misspelled key would silently "
            "canonicalize nothing.")
    if profile.get("measured") and "measured_decimals" not in profile:
        raise ValueError(
            "profile lists measured fields but no 'measured_decimals' — "
            "the precision comes from the P0(d) measurement, not a default.")
    defaults = profile.get("defaults", {})
    if not isinstance(defaults, dict) or not all(
            isinstance(p, str) and p and "" not in p.split(".")
            for p in defaults):
        raise ValueError(
            "profile 'defaults' must be a mapping {dotted.path: value} with "
            f"non-empty path segments, got {defaults!r}.")
    equivalences = profile.get("equivalences", ())
    if isinstance(equivalences, str):
        raise ValueError(
            f"profile 'equivalences' must be a list of names, not the bare "
            f"string {equivalences!r}.")
    unknown = sorted(set(equivalences) - set(_EQUIVALENCES))
    if unknown:
        raise ValueError(
            f"unknown equivalence name(s) {unknown} — known equivalences "
            f"are {sorted(_EQUIVALENCES)}. A misspelled name would "
            "silently normalize nothing.")


def _materialize_defaults(value, defaults):
    """Fill every ABSENT ``dotted.path`` of *defaults* with its value.

    Absent-vs-materialized is a representation difference: a host that
    omits ``timing`` and one that writes ``{"fps": 30}`` asked for the same
    clock. A PRESENT key is never touched, whatever its value — present
    and different is a real difference and stays one. Paths address dicts
    only (intermediate dicts are created as needed); a path that passes
    through a list or a scalar is refused with words rather than guessing
    a per-element rule.
    """
    for path, default in defaults.items():
        parts = path.split(".")
        node = value
        for i, part in enumerate(parts[:-1]):
            if not isinstance(node, dict):
                raise ValueError(
                    f"defaults path {path!r} passes through a "
                    f"{type(node).__name__} at {'.'.join(parts[:i])!r} — "
                    "defaults address dict paths only.")
            node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ValueError(
                f"defaults path {path!r} passes through a "
                f"{type(node).__name__} at {'.'.join(parts[:-1])!r} — "
                "defaults address dict paths only.")
        if parts[-1] not in node:
            node[parts[-1]] = default
    return value


def _segment_seed_lifts_to_options(request):
    """``segments[*].seed`` equal to ``options.seed`` is dropped.

    Representation: a host that stamps ``options.seed`` onto every segment
    (Blender) and one that states it once (Max, MoBu) hand the server the
    same seed for every segment — the generator cannot tell the two apart.
    Semantics: a per-segment seed that DIFFERS from ``options.seed``
    changes what that segment generates, so it is kept and diffs; with no
    ``options.seed`` at all there is nothing to lift to, and every segment
    seed stays.
    """
    options = request.get("options")
    if not isinstance(options, dict) or "seed" not in options:
        return request
    for segment in request.get("segments") or ():
        if isinstance(segment, dict) and "seed" in segment \
                and segment["seed"] == options["seed"]:
            del segment["seed"]
    return request


def _single_segment_transition_frames_is_noise(request):
    """``options.transition_frames`` is dropped when there is ONE segment.

    Representation: a transition length only acts BETWEEN segments; with a
    single segment there is no boundary for it to blend, so whether a host
    emits ``options.transition_frames`` (Blender writes 0; Max and MoBu
    omit it — measured on the real c1 manifests) cannot change the motion.
    Semantics: with two or more segments the value shapes the blend, so it
    is kept and compared like any authored field.
    """
    segments = request.get("segments")
    options = request.get("options")
    if isinstance(segments, list) and len(segments) == 1 \
            and isinstance(options, dict):
        options.pop("transition_frames", None)
    return request


def _is_frame_zero_anchor(constraint):
    return (isinstance(constraint, dict)
            and constraint.get("type") == "root_path"
            and constraint.get("frames") == [0]
            and isinstance(constraint.get("positions_xz"), list)
            and len(constraint["positions_xz"]) == 1
            and len(constraint["positions_xz"][0]) == 2)


def _whole_constraint_set_translates_to_frame_zero_anchor(request, profile=None):
    """Every XZ position in the constraint set is shifted so the frame-0
    root anchor sits at the origin.

    Representation: the server is translation-equivariant on a constant
    shift of the ENTIRE constraint set — measured 2026-09-02 (Max, live
    server, c2 raw requests: anchor [0,0] vs anchor = the rig's scene root
    r0): every local rotation bitwise identical, the root at frame 0 and at
    the last frame moved by exactly r0. A host that anchors frame 0 "where
    the character stands" (Blender, Max) and one that sends the canonical
    rest anchor and reseats on apply (MoBu) hand the user the same clip;
    the shift is where the request's origin sits, not what the motion is.
    Semantics: only a shift that is CONSTANT across the set is folded — the
    anchor and every authored position move together, so the relative
    geometry (anchor -> waypoint -> pin) is untouched. A request whose
    anchor moved while its waypoints stayed (a different start toward the
    same targets) keeps a different relative geometry and still diffs.
    Applies only with exactly one single-frame root_path at frame 0; a
    multi-frame trajectory through frame 0 or no anchor at all is left as
    it is. The shifted floats are re-rounded to the profile's
    ``measured_decimals`` (when it has one), so a float32 anchor does not
    smear its last bits over every authored coordinate.
    """
    constraints = request.get("constraints")
    if not isinstance(constraints, list):
        return request
    anchors = [c for c in constraints if _is_frame_zero_anchor(c)]
    if len(anchors) != 1:
        return request
    ax, az = (float(v) for v in anchors[0]["positions_xz"][0])
    if ax == 0.0 and az == 0.0:
        return request
    decimals = (int(profile["measured_decimals"])
                if isinstance(profile, dict) and "measured_decimals" in profile
                else None)

    def _shift(x, z):
        x, z = float(x) - ax, float(z) - az
        if decimals is not None:
            x, z = round(x, decimals), round(z, decimals)
        return (0.0 if x == 0 else x), (0.0 if z == 0 else z)

    for c in constraints:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("positions_xz"), list):
            c["positions_xz"] = [list(_shift(p[0], p[1])) + list(p[2:])
                                 for p in c["positions_xz"]]
        if isinstance(c.get("positions"), list):
            out = []
            for p in c["positions"]:
                x, z = _shift(p[0], p[2])
                out.append([x, p[1], z] + list(p[3:]))
            c["positions"] = out
        rp = c.get("root_position")
        if isinstance(rp, list) and len(rp) >= 3:
            x, z = _shift(rp[0], rp[2])
            c["root_position"] = [x, rp[1], z] + list(rp[3:])
    return request


#: The closed set of named equivalences a profile may enable by name. Each
#: is explicit code with its representation-vs-semantics argument in its
#: docstring — there is no generic "ignore this path" lever on purpose.
#: Each takes ``(request, profile)``; the profile is there for the ones
#: whose fold has to re-round (see the anchor translation).
_EQUIVALENCES = {
    "segment_seed_lifts_to_options":
        lambda request, profile=None: _segment_seed_lifts_to_options(request),
    "single_segment_transition_frames_is_noise":
        lambda request, profile=None:
            _single_segment_transition_frames_is_noise(request),
    "whole_constraint_set_translates_to_frame_zero_anchor":
        _whole_constraint_set_translates_to_frame_zero_anchor,
}


def _round_floats(value, decimals):
    """Round every float in *value*'s subtree; leave everything else alone."""
    if isinstance(value, float):
        v = round(value, decimals)
        return 0.0 if v == 0 else v          # -0.0 must not diff against 0.0
    if isinstance(value, dict):
        return {k: _round_floats(v, decimals) for k, v in value.items()}
    if isinstance(value, list):
        return [_round_floats(v, decimals) for v in value]
    return value


def _canon(value, volatile, measured, decimals):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in volatile:
                continue
            if k in measured:
                # volatile names are dropped INSIDE measured subtrees too
                # (an anchor's own timestamp is still a timestamp), then
                # the remaining floats are rounded.
                out[k] = _round_floats(
                    _canon(v, volatile, measured, decimals), decimals)
            else:
                out[k] = _canon(v, volatile, measured, decimals)
        return out
    if isinstance(value, list):
        return [_canon(v, volatile, measured, decimals) for v in value]
    if isinstance(value, float) and value == 0.0:
        # The sign of zero is never semantics -- measured 2026-09-02: after
        # every other fold the ONE remaining Max<->Blender c1 difference was
        # 0.0 vs -0.0 in the root read (float32). Folded everywhere, not
        # only in measured subtrees, so an authored coordinate does not
        # have to be declared 'measured' (and rounded) to absorb it.
        return 0.0
    return value


def canonicalize_request(request, profile):
    """The request as layer 1 compares (and the cassette hashes) it.

    * ``profile["volatile"]`` — key names dropped wherever they appear
      (timestamps, session ids: volatile is a property of the name, not of
      one position in the tree).
    * ``profile["measured"]`` — key names whose subtree's floats are rounded
      to ``profile["measured_decimals"]`` places (anchors, hip heights —
      scene-state reads that carry measurement noise).
    * ``profile["defaults"]`` — ``{dotted.path: value}`` materialized where
      the path is ABSENT (dict paths only); a present key is never
      overwritten, so present-and-different still diffs.
    * ``profile["equivalences"]`` — names from the closed set
      :data:`_EQUIVALENCES`, applied in the order listed, after volatile
      dropping, rounding and defaults. Unknown names are refused.
    * Everything else is AUTHORED and passes through untouched.

    Key order is not part of the canonical form — serialize with
    :func:`canonical_request_json` (sorted keys) before hashing or diffing.
    """
    _check_profile(profile)
    canon = _canon(request,
                   frozenset(profile.get("volatile", ())),
                   frozenset(profile.get("measured", ())),
                   int(profile.get("measured_decimals", 0)))
    _materialize_defaults(canon, profile.get("defaults", {}))
    for name in profile.get("equivalences", ()):
        canon = _EQUIVALENCES[name](canon, profile)
    return canon


def canonical_request_json(request, profile):
    """The canonical serialized form — the cassette hashes exactly this."""
    return json.dumps(canonicalize_request(request, profile),
                      sort_keys=True, indent=1)


def canonical_request_hash(request, profile):
    """SHA-256 of :func:`canonical_request_json` — THE cassette key.

    One definition, used by :mod:`animatica_core.gates.ab_cassette` and by
    nothing host-side directly: float jitter that must not change the key is
    absorbed here (by the profile's measured rounding), in one place, so a
    tolerance change is one commit and every host inherits it.
    """
    return hashlib.sha256(
        canonical_request_json(request, profile).encode("utf-8")).hexdigest()


def _walk_diff(a, b, path, out):
    if type(a) is not type(b) and not (
            isinstance(a, (int, float)) and isinstance(b, (int, float))
            and not isinstance(a, bool) and not isinstance(b, bool)):
        out.append(f"{path}: A is {type(a).__name__}, B is {type(b).__name__}")
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            sub = f"{path}.{k}" if path else str(k)
            if k not in b:
                out.append(f"{sub}: only in A (A={a[k]!r})")
            elif k not in a:
                out.append(f"{sub}: only in B (B={b[k]!r})")
            else:
                _walk_diff(a[k], b[k], sub, out)
        return
    if isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} in A, {len(b)} in B")
        for i in range(min(len(a), len(b))):
            _walk_diff(a[i], b[i], f"{path}[{i}]", out)
        return
    if a != b:
        out.append(f"{path}: A={a!r} B={b!r}")


def compare_requests(a, b, profile, axis="golden"):
    """Worded differences between two canonicalized requests; empty = equal.

    *axis* names which A/B axis is being compared (PLAN-testy-ab §0):

    * ``"golden"`` — same host, candidate vs recorded golden. EVERYTHING in
      the canonical form is compared, host-specific sections included: the
      golden axis exists to catch drift in this host's own request.
    * ``"cross_host"`` — two hosts, same scenario. The top-level sections in
      ``profile["host_sections"]`` are excluded from both sides: they differ
      BY DESIGN (§1 last row), and a diff on them would be noise dressed as
      red.
    """
    if axis not in ("golden", "cross_host"):
        raise ValueError(
            f"unknown axis {axis!r} — 'golden' or 'cross_host'")
    ca = canonicalize_request(a, profile)
    cb = canonicalize_request(b, profile)
    if axis == "cross_host":
        for section in profile.get("host_sections", ()):
            ca.pop(section, None)
            cb.pop(section, None)
    out = []
    _walk_diff(ca, cb, "", out)
    return out


# ---------------------------------------------------------------------------
# Layer 2 — dump comparison
# ---------------------------------------------------------------------------

#: Provenance fields on ``dump["meta"]`` that make two dumps COMPARABLE at
#: all. A judge upgrade (new Blender import behavior), a dump-format change,
#: a scenario edit or a different retarget regime (``none | hik | server``
#: — comparing a none-run against a hik-run is a category error) each
#: shifts numbers legitimately — chasing those numbers as if they were
#: regressions is the §7 "upgrade reddens every golden" failure. So does a
#: different model or backend (measured 2026-09-01: the cloud backend's
#: canonical skeleton has 30 joints, the local one differs — C0 goldens
#: from two backends are incomparable BY DEFINITION, and provenance defends
#: that instead of golden-directory naming discipline). Mismatch here is
#: NOT-COMPARABLE, not red.
PROVENANCE_FIELDS = ("generator", "blender_version", "dump_format_version",
                     "scenario_version", "retarget_regime", "model_id",
                     "backend")

#: The dump-format generation this comparator speaks. The judge script
#: (tools/blender_dump_fbx.py) carries its own DUMP_FORMAT_VERSION — it
#: cannot import this module (it runs in Blender's interpreter) — and the
#: CI test ties the two constants together, so a version bump on one side
#: without the other fails loudly instead of drifting. v2: rotations are
#: pose-relative-to-rest (the importer's bone-roll inference cannot vote).
EXPECTED_DUMP_FORMAT_VERSION = 2


def _provenance_faults(golden, candidate):
    faults = []
    gm = golden.get("meta", {})
    cm = candidate.get("meta", {})
    for field in PROVENANCE_FIELDS:
        gv, cv = gm.get(field), cm.get(field)
        if gv != cv:
            faults.append(
                f"NOT COMPARABLE: meta.{field} differs — golden {gv!r}, "
                f"candidate {cv!r}. The judge or the dump format changed, "
                "not (necessarily) the motion: re-bless the golden, do not "
                "chase numbers.")
    return faults

def _scan_nan(value, path, out):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            out.append(path)
    elif isinstance(value, dict):
        for k in sorted(value):
            _scan_nan(value[k], f"{path}.{k}", out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_nan(v, f"{path}[{i}]", out)


def _stamp_faults(dump, label):
    meta = dump.get("meta", {})
    faults = []
    if meta.get("units") != "meters":
        faults.append(f"{label} dump is stamped units={meta.get('units')!r}, "
                      "not 'meters' — the dumper must normalize units")
    if meta.get("axes") != "right_handed_y_up":
        faults.append(f"{label} dump is stamped axes={meta.get('axes')!r}, "
                      "not 'right_handed_y_up' — the dumper must normalize "
                      "axes, or this diff compares conventions, not motion")
    return faults


def _quat_angle_deg(qa, qb):
    """Angular distance in degrees; q and -q are the SAME rotation.

    The fingerprint spec calls the sign out explicitly: the |dot| absorbs a
    flipped sign, so a dump that stored -q for q measures zero degrees away.
    """
    dot = abs(sum(float(x) * float(y) for x, y in zip(qa, qb)))
    return math.degrees(2.0 * math.acos(min(1.0, dot)))


def _percentile(sorted_vals, q):
    """Nearest-rank percentile of an already-sorted list (q in 0..100)."""
    if not sorted_vals:
        return 0.0
    rank = max(1, int(math.ceil(q / 100.0 * len(sorted_vals))))
    return sorted_vals[rank - 1]


def _stats(devs):
    """(p50, p95, max) of a deviation list — the fingerprint spec's trio."""
    vals = sorted(d[0] for d in devs)
    return (_percentile(vals, 50), _percentile(vals, 95),
            vals[-1] if vals else 0.0)


def _dev_stats(devs, scale):
    """The run-summary stats blob of ``(deviation, joint, frame, ...)`` tuples.

    The same nearest-rank percentiles the verdict lines print, scaled into
    the reporting unit (mm for positions, degrees as-is). ``None`` for an
    empty list — an absent channel must read as absent, not as a zero.
    """
    if not devs:
        return None
    p50, p95, mx = _stats(devs)
    worst = max(devs)
    return {"p50": p50 * scale, "p95": p95 * scale, "max": mx * scale,
            "worst_joint": worst[1], "worst_frame": worst[2]}


def _compare_armature(g, c, name, thresholds, out, pos_all=None, rot_all=None):
    pos_m = float(thresholds["position_mm"]) / 1000.0
    rot_deg = float(thresholds["rotation_deg"])

    g_bones = {b["name"]: b for b in g.get("bones", [])}
    c_bones = {b["name"]: b for b in c.get("bones", [])}
    for missing in sorted(set(g_bones) - set(c_bones)):
        out.append(f"{name}: bone '{missing}' only in golden dump "
                   "(missing from candidate)")
    for extra in sorted(set(c_bones) - set(g_bones)):
        out.append(f"{name}: bone '{extra}' only in candidate dump")
    common = sorted(set(g_bones) & set(c_bones))
    for bone in common:
        gp, cp = g_bones[bone].get("parent"), c_bones[bone].get("parent")
        if gp != cp:
            out.append(f"{name}: bone '{bone}' is parented to {cp!r} in the "
                       f"candidate, {gp!r} in the golden (reparented)")

    # The TIME STRUCTURE gate: fps or frame-range disagreement makes every
    # per-frame number meaningless (measured: a 24-vs-30 fps skew alone
    # reads as 49 mm of "motion drift" on the last frame). Collect the
    # worded structural faults now; below, their presence suppresses the
    # numeric comparison entirely — clock skew must never be dressed up as
    # motion regression.
    time_faults = []
    for key in ("fps", "frame_start", "frame_end"):
        if g.get(key) != c.get(key):
            time_faults.append(
                f"{name}: {key} differs — golden {g.get(key)!r}, "
                f"candidate {c.get(key)!r}")
    out.extend(time_faults)

    for bone in common:
        gr = g_bones[bone].get("rest_head_m")
        cr = c_bones[bone].get("rest_head_m")
        if gr is None or cr is None:
            if gr != cr:
                out.append(f"{name}: rest head of '{bone}' present in only "
                           "one dump")
            continue
        for ax, axis_name in enumerate("xyz"):
            d = abs(float(gr[ax]) - float(cr[ax]))
            if d > pos_m:
                out.append(
                    f"{name}: rest head of '{bone}' axis {axis_name}: "
                    f"golden {float(gr[ax]):.6f} m, candidate "
                    f"{float(cr[ax]):.6f} m, |d| {d * 1000:.3f} mm exceeds "
                    f"the {pos_m * 1000:.3f} mm rest threshold (the max "
                    "position bound)")

    if time_faults:
        out.append(f"{name}: the time structure differs, so the key-count "
                   "and per-frame comparisons are not attempted — clock "
                   "skew must never read as motion drift")
        return

    gk = g.get("key_counts", {})
    ck = c.get("key_counts", {})
    for bone in common:
        if gk.get(bone, 0) != ck.get(bone, 0):
            out.append(f"{name}: key count on '{bone}' differs — golden "
                       f"{gk.get(bone, 0)}, candidate {ck.get(bone, 0)} "
                       "(counts are exact, no threshold)")

    # Per-frame comparison, reported as a DISTRIBUTION (fingerprint spec:
    # p50 / p95 / max plus the worst location -- "one runaway frame" and
    # "everything shifted 2 mm" are different failures and must read
    # differently). Deviations gather per (bone, frame) across the whole
    # armature; the verdict binds max AND p95, each against its own
    # threshold.
    pos_p95_m = float(thresholds["position_p95_mm"]) / 1000.0
    rot_p95_deg = float(thresholds["rotation_p95_deg"])
    fstart = int(g.get("frame_start", 0))
    pos_devs = []                  # (deviation_m, bone, frame, axis)
    rot_devs = []                  # (deviation_deg, bone, frame)
    for bone in common:
        gb, cb = g.get("world", {}).get(bone), c.get("world", {}).get(bone)
        if gb is None or cb is None:
            if gb != cb:
                out.append(f"{name}: world samples for '{bone}' present in "
                           "only one dump")
            continue
        gpos, cpos = gb.get("pos_m", []), cb.get("pos_m", [])
        if len(gpos) != len(cpos):
            out.append(f"{name}: '{bone}' has {len(gpos)} position samples "
                       f"in the golden, {len(cpos)} in the candidate")
        else:
            for f in range(len(gpos)):
                for ax, axis_name in enumerate("xyz"):
                    d = abs(float(gpos[f][ax]) - float(cpos[f][ax]))
                    pos_devs.append((d, bone, fstart + f, axis_name))
        gq, cq = gb.get("quat_xyzw", []), cb.get("quat_xyzw", [])
        if len(gq) != len(cq):
            out.append(f"{name}: '{bone}' has {len(gq)} rotation samples "
                       f"in the golden, {len(cq)} in the candidate")
        else:
            for f in range(len(gq)):
                rot_devs.append(
                    (_quat_angle_deg(gq[f], cq[f]), bone, fstart + f))

    if pos_all is not None:
        pos_all.extend(pos_devs)
    if rot_all is not None:
        rot_all.extend(rot_devs)

    if pos_devs:
        p50, p95, mx = _stats(pos_devs)
        worst = max(pos_devs)
        loc = (f"'{worst[1]}' frame {worst[2]} axis {worst[3]}")
        stats_txt = (f"position deviation p50 {p50 * 1000:.3f} / "
                     f"p95 {p95 * 1000:.3f} / max {mx * 1000:.3f} mm, "
                     f"worst at {loc}")
        if mx > pos_m:
            offenders = sorted({d[1] for d in pos_devs if d[0] > pos_m})
            out.append(
                f"{name}: {stats_txt} — max exceeds the {pos_m * 1000:.3f} "
                f"mm position threshold; offending bone(s): "
                f"{', '.join(offenders[:5])}"
                + (f" (+{len(offenders) - 5} more)"
                   if len(offenders) > 5 else ""))
        elif p95 > pos_p95_m:
            out.append(
                f"{name}: {stats_txt} — p95 exceeds the "
                f"{pos_p95_m * 1000:.3f} mm p95 position threshold "
                "(a broad shift, not one runaway frame)")
    if rot_devs:
        p50, p95, mx = _stats(rot_devs)
        worst = max(rot_devs)
        stats_txt = (f"rotation deviation p50 {p50:.3f} / p95 {p95:.3f} / "
                     f"max {mx:.3f} deg, worst at '{worst[1]}' "
                     f"frame {worst[2]}")
        if mx > rot_deg:
            offenders = sorted({d[1] for d in rot_devs if d[0] > rot_deg})
            out.append(
                f"{name}: {stats_txt} — max exceeds the {rot_deg:.3f} deg "
                f"rotation threshold; offending bone(s): "
                f"{', '.join(offenders[:5])}"
                + (f" (+{len(offenders) - 5} more)"
                   if len(offenders) > 5 else ""))
        elif p95 > rot_p95_deg:
            out.append(
                f"{name}: {stats_txt} — p95 exceeds the {rot_p95_deg:.3f} "
                f"deg p95 rotation threshold (a broad drift, not one "
                "runaway frame)")


def compare_dumps(golden, candidate, thresholds):
    """Worded differences between two FBX dumps; empty = equal enough.

    Hierarchy (names + parents), key counts, fps and frame ranges are exact.
    Per-frame deviations are judged as a DISTRIBUTION: the max binds against
    ``thresholds["position_mm"]`` / ``["rotation_deg"]``, the p95 against
    ``["position_p95_mm"]`` / ``["rotation_p95_deg"]``, and every verdict
    line reports p50/p95/max plus the worst location (bone, frame, axis).
    Rotations measure quaternion angular distance with the sign normalized
    (q and -q are the same rotation). Rest heads bind per-axis against the
    max position threshold.

    Three classes stop the comparison instead of reddening everything after
    them: a NaN/Inf anywhere (the numbers past it mean nothing); mismatched
    :data:`PROVENANCE_FIELDS` (the dumps are NOT COMPARABLE — a different
    judge or format moved the numbers legitimately, so the verdict says
    "re-bless the golden", not "regression"); and a dump not stamped
    meters / Y-up (past it the diff would compare axis conventions, not
    motion).
    """
    return compare_dumps_with_stats(golden, candidate, thresholds)[0]


def compare_dumps_with_stats(golden, candidate, thresholds):
    """:func:`compare_dumps`, but returning ``(problems, stats)``.

    *stats* carries the deviation distributions ALSO when nothing exceeded a
    threshold — the campaign report (PLAN-raport-kampanii P1) needs the
    numbers of a green run, not only of a red one::

        {"pos_mm": {"p50", "p95", "max", "worst_joint", "worst_frame"},
         "rot_deg": {"p50", "p95", "max", "worst_joint", "worst_frame"}}

    Values are floats (mm / degrees), ``worst_joint`` the bone name and
    ``worst_frame`` the absolute frame index, aggregated across armatures.
    Either key is absent when that channel gathered no samples; *stats* is
    ``None`` when the numeric comparison never ran at all (NaN, NOT
    COMPARABLE, a stamp fault, or a time-structure short circuit) — a number
    from a comparison that did not happen would be worse than no number.
    """
    out = []
    for label, dump in (("golden", golden), ("candidate", candidate)):
        hits = []
        _scan_nan(dump, label, hits)
        for path in hits[:8]:
            out.append(f"{label} dump carries NaN/Inf at {path} — no number "
                       "past this one is trustworthy")
        if len(hits) > 8:
            out.append(f"{label} dump carries NaN/Inf at {len(hits) - 8} "
                       "further location(s)")
    if out:
        return out, None

    out.extend(_provenance_faults(golden, candidate))
    if out:
        return out, None

    for label, dump in (("golden", golden), ("candidate", candidate)):
        out.extend(_stamp_faults(dump, label))
    if out:
        return out, None

    g_arms = {a["name"]: a for a in golden.get("armatures", [])}
    c_arms = {a["name"]: a for a in candidate.get("armatures", [])}
    for missing in sorted(set(g_arms) - set(c_arms)):
        out.append(f"armature '{missing}' only in golden dump")
    for extra in sorted(set(c_arms) - set(g_arms)):
        out.append(f"armature '{extra}' only in candidate dump")
    pos_all, rot_all = [], []
    for name in sorted(set(g_arms) & set(c_arms)):
        _compare_armature(g_arms[name], c_arms[name], name, thresholds, out,
                          pos_all, rot_all)
    stats = None
    if pos_all or rot_all:
        stats = {}
        if pos_all:
            stats["pos_mm"] = _dev_stats(pos_all, 1000.0)
        if rot_all:
            stats["rot_deg"] = _dev_stats(rot_all, 1.0)
    return out, stats


def compare_scene_dumps(golden, candidate, thresholds):
    """Worded differences between two SCENE fingerprints; empty = equal enough.

    The scene fingerprint is the D5 arbiter: world positions read through a
    host's own bridge, no FBX (and no FBX judge) in the loop. Measured need:
    the Blender importer does not reproduce a MoBu FBX's position
    articulation faithfully (~1 m of apparent divergence at a real 2.66 mm),
    so axis B for pairs involving MoBu binds on THIS comparison while FBX
    dumps stay the same-host axis-A artifact.

    Schema per side: ``{"meta": {"axes", "units", "fps", "frames", ...},
    "world": {joint: [[x, y, z] per frame]}}`` — as written by the hosts'
    scene-dump emitters. Time structure (fps, frames) and axis/unit stamps
    gate the numerics exactly like :func:`compare_dumps` (clock skew must
    never read as motion drift); the joint SET compares on the shared subset
    with one-sided extras reported as their own worded fault (hosts may
    legitimately differ in registry size). Positions are judged as a
    distribution: max against ``thresholds["position_mm"]``, p95 against
    ``["position_p95_mm"]``, verdict lines report p50/p95/max and the worst
    (joint, frame, axis).
    """
    return compare_scene_dumps_with_stats(golden, candidate, thresholds)[0]


def compare_scene_dumps_with_stats(golden, candidate, thresholds):
    """:func:`compare_scene_dumps`, but returning ``(problems, stats)``.

    *stats* is ``{"pos_mm": {"p50", "p95", "max", "worst_joint",
    "worst_frame"}}`` — the scene comparison is positions-only, so there is
    never a ``rot_deg`` key. Present ALSO on a clean pass (the campaign
    report needs a green run's numbers, PLAN-raport-kampanii P1), with the
    exact values the verdict line would print; ``None`` when the numeric
    comparison never ran (stamp or time-structure short circuit, NaN, no
    shared joints).
    """
    out = []
    gm, cm = golden.get("meta") or {}, candidate.get("meta") or {}
    for field in ("axes", "units"):
        if gm.get(field) != cm.get(field):
            out.append(
                f"scene: {field} stamp differs -- golden {gm.get(field)!r}, "
                f"candidate {cm.get(field)!r}; past this the diff would "
                f"compare conventions, not motion")
    if out:
        return out, None
    for field in ("fps", "frames"):
        if gm.get(field) != cm.get(field):
            out.append(
                f"scene: {field} differs -- golden {gm.get(field)!r}, "
                f"candidate {cm.get(field)!r}")
    if out:
        out.append("scene: the time structure differs, so per-frame "
                   "comparisons are not attempted -- clock skew must never "
                   "read as motion drift")
        return out, None

    gw, cw = golden.get("world") or {}, candidate.get("world") or {}
    only_g = sorted(set(gw) - set(cw))
    only_c = sorted(set(cw) - set(gw))
    if only_g:
        out.append(f"scene: {len(only_g)} joint(s) only in the golden "
                   f"(first: {only_g[0]})")
    if only_c:
        out.append(f"scene: {len(only_c)} joint(s) only in the candidate "
                   f"(first: {only_c[0]})")
    shared = sorted(set(gw) & set(cw))
    if not shared:
        out.append("scene: no shared joints -- nothing to compare")
        return out, None

    deltas = []
    worst = None
    for name in shared:
        ga, ca = gw[name], cw[name]
        frames = min(len(ga), len(ca))
        for f in range(frames):
            for axis_i, axis in enumerate("xyz"):
                a, b = float(ga[f][axis_i]), float(ca[f][axis_i])
                if math.isnan(a) or math.isnan(b)                         or math.isinf(a) or math.isinf(b):
                    return (out + [f"scene: NaN/Inf at {name} frame {f} "
                                   f"axis {axis} -- the numbers past it "
                                   f"mean nothing"], None)
                d = abs(a - b)
                deltas.append(d)
                if worst is None or d > worst[0]:
                    worst = (d, name, f, axis)
    if worst is None:
        out.append("scene: no overlapping frames -- nothing to compare")
        return out, None
    deltas.sort()
    n = len(deltas)
    p50 = deltas[n // 2] * 1000.0
    p95 = deltas[int(n * 0.95)] * 1000.0 if n else 0.0
    mx = worst[0] * 1000.0
    stats = {"pos_mm": {"p50": p50, "p95": p95, "max": mx,
                        "worst_joint": worst[1], "worst_frame": worst[2]}}
    line = (f"position deviation p50 {p50:.3f} / p95 {p95:.3f} / "
            f"max {mx:.3f} mm, worst at {worst[1]!r} frame {worst[2]} "
            f"axis {worst[3]}")
    if mx > float(thresholds["position_mm"]):
        out.append(f"scene: {line} -- max exceeds the "
                   f"{thresholds['position_mm']} mm position threshold")
    elif p95 > float(thresholds.get("position_p95_mm",
                                    thresholds["position_mm"])):
        out.append(f"scene: {line} -- p95 exceeds the "
                   f"{thresholds.get('position_p95_mm')} mm bound")
    return out, stats


# ---------------------------------------------------------------------------
# Skeleton integrity — the BROKEN-RIG gate
# ---------------------------------------------------------------------------

#: The verdict class a non-empty :func:`check_scene_integrity` result maps
#: to. The orchestrator (run.py, the Max session's lane) files it NEXT TO
#: PASS/FAIL/NOT-COMPARABLE, never inside them: a broken rig must be named a
#: broken rig, not a 1.2 m motion regression. Freeze refuses on it like on
#: FAIL (PLAN-raport-kampanii §B).
BROKEN_RIG = "BROKEN-RIG"

#: Every integrity fault line starts with exactly this prefix — THE stable
#: contract for mapping lines to the :data:`BROKEN_RIG` verdict. A consumer
#: needs no parsing beyond "the list is non-empty"; the prefix exists so a
#: fault line quoted out of context still names its class.
BROKEN_RIG_PREFIX = "broken-rig: "

#: Integrity thresholds, with provenance (PLAN-testy-ab §7 discipline).
INTEGRITY_THRESHOLDS = {
    # Measured 2026-09-01 on the real scene fingerprints (max + mobu, c0):
    # worst frame-0 deviation from the canonical rest world accumulated out
    # of the manifest's rest_translation chain was 0.0006 mm — an effective
    # 0.00 mm base (the fingerprints round at 1e-6 m). 1.0 mm keeps three
    # orders of margin while a genuinely different rig measures whole
    # centimeters (the judge's non-anatomical world sits ~1 m off at Head).
    "rest_mm": 1.0,
    # Measured 2026-09-01 on the same artifacts, every checkpoint c0–c4,
    # every frame: worst bone-length deviation from canonical 0.0013 mm —
    # the same 0.00 mm base as the plan's max/c1 measurement. Same 1.0 mm
    # bound; bone stretching in a real fault is millimeters to meters.
    "bone_mm": 1.0,
}


def accumulate_rest_world(skeleton):
    """Canonical rest WORLD positions from a manifest's skeleton block.

    ``skeleton["joints"][*].rest_translation`` (meters, parent-relative) is
    summed down the ``parent`` chain. Rest rotations are identity by the
    request contract (d51d46c: a rest capture is identity everywhere), and a
    joint that violates that is refused with words — accumulating plain
    translations under a rotated rest frame would produce a silently wrong
    canon, the one failure mode this gate exists to prevent.
    """
    joints = skeleton.get("joints") or []
    by = {j["name"]: j for j in joints}
    for j in joints:
        q = j.get("rest_rotation") or (0.0, 0.0, 0.0, 1.0)
        if max(abs(float(q[0])), abs(float(q[1])), abs(float(q[2]))) > 1e-6 \
                or abs(abs(float(q[3])) - 1.0) > 1e-6:
            raise ValueError(
                f"joint {j['name']!r} carries a non-identity rest_rotation "
                f"{list(q)!r} — the rest-capture contract stamps identity "
                "everywhere, and accumulating bare translations under a "
                "rotated rest frame would build a silently wrong canon")
    world = {}

    def _world(name, trail):
        if name in world:
            return world[name]
        if name in trail:
            raise ValueError(f"the skeleton's parent chain loops at {name!r}")
        j = by[name]
        t = j.get("rest_translation") or (0.0, 0.0, 0.0)
        parent = j.get("parent")
        if parent is None:
            w = [float(t[0]), float(t[1]), float(t[2])]
        else:
            if parent not in by:
                raise ValueError(
                    f"joint {name!r} names parent {parent!r}, which the "
                    "skeleton block does not carry — a malformed manifest, "
                    "not a broken rig")
            p = _world(parent, trail | {name})
            w = [p[i] + float(t[i]) for i in range(3)]
        world[name] = w
        return w

    for j in joints:
        _world(j["name"], frozenset())
    return world


def _resolve_skeleton(manifest, skeleton):
    if skeleton is None:
        skeleton = (manifest.get("request") or {}).get("skeleton")
    if not skeleton or not skeleton.get("joints"):
        raise ValueError(
            "no canonical skeleton: the manifest carries no request.skeleton "
            "and none was passed. c0 manifests carry no request BY DESIGN "
            "(measured on the real goldens) — pass the skeleton block of a "
            "sibling checkpoint's manifest (same host, same backend; the "
            "canonical rig is identical across one run's checkpoints). A "
            "missing canon is a wiring fault, not a rig verdict.")
    return skeleton


def check_scene_integrity(scene, manifest, thresholds=INTEGRITY_THRESHOLDS,
                          skeleton=None):
    """Skeleton-integrity faults of a SCENE fingerprint; empty = intact.

    A non-empty result is the :data:`BROKEN_RIG` class: every line starts
    with :data:`BROKEN_RIG_PREFIX`, and the orchestrator maps any such line
    to its BROKEN-RIG verdict. By design this check SHORT-CIRCUITS before
    the pose comparison, exactly like the fps gate suppresses per-frame
    numbers: a broken rig must be named a broken rig, not dressed up as a
    1.2 m motion regression.

    Scene fingerprints ONLY, never judge dumps — the judge's world is
    non-anatomical by format (``meta.pos_semantics``, 7ac8799), so judging
    it here would red every healthy run. Two assertions, both against the
    canon accumulated by :func:`accumulate_rest_world` from *skeleton*
    (default: ``manifest["request"]["skeleton"]``; see
    :func:`_resolve_skeleton` for the c0 sibling rule):

    * **i-rest** — only when the fingerprint is the bare rig (the manifest
      marks checkpoint c0, or the scene holds exactly one frame) AND the
      regime is ``none``: frame-0 world positions must sit within
      ``thresholds["rest_mm"]`` of the canonical rest world, and every
      canonical joint must be present.
    * **i-bones** — every checkpoint: per-frame child–parent distances.
      Regime ``none`` binds them to the canonical bone lengths; regimes
      ``hik``/``server`` retarget onto a user rig whose lengths differ
      legitimately, so the assertion weakens to CONSTANT across frames
      (bone stretching is always a failure), both within
      ``thresholds["bone_mm"]``. Bone pairing comes from the skeleton's
      parent map restricted to joints the fingerprint carries.

    The regime is read from ``manifest["meta"]["retarget_regime"]`` and must
    be one of ``none | hik | server`` — an unknown regime silently choosing
    the weaker assertion would soften the gate, so it is refused instead.
    """
    rest_mm = float(thresholds["rest_mm"])
    bone_mm = float(thresholds["bone_mm"])
    skeleton = _resolve_skeleton(manifest, skeleton)
    regime = (manifest.get("meta") or {}).get("retarget_regime")
    if regime not in ("none", "hik", "server"):
        raise ValueError(
            f"unknown retarget regime {regime!r} — 'none', 'hik' or "
            "'server'. Guessing would silently pick the weaker assertion.")

    faults = []
    meta = scene.get("meta") or {}
    if meta.get("units") != "meters":
        faults.append(
            f"{BROKEN_RIG_PREFIX}scene fingerprint is stamped "
            f"units={meta.get('units')!r}, not 'meters' — the canonical "
            "world is meters, so integrity cannot pass unjudged")
    if meta.get("axes") != "right_handed_y_up":
        faults.append(
            f"{BROKEN_RIG_PREFIX}scene fingerprint is stamped "
            f"axes={meta.get('axes')!r}, not 'right_handed_y_up' — past "
            "this the check would compare conventions, not anatomy")
    if faults:
        return faults

    world = scene.get("world") or {}
    hits = []
    _scan_nan(world, "world", hits)
    for path in hits[:8]:
        faults.append(f"{BROKEN_RIG_PREFIX}NaN/Inf at {path} — no number "
                      "past this one is trustworthy")
    if len(hits) > 8:
        faults.append(f"{BROKEN_RIG_PREFIX}NaN/Inf at {len(hits) - 8} "
                      "further location(s)")
    if faults:
        return faults

    rest_world = accumulate_rest_world(skeleton)

    # --- i-rest: the bare rig must BE the canonical rest world -----------
    is_rest = (manifest.get("checkpoint") == "c0" or meta.get("frames") == 1)
    if is_rest and regime == "none":
        missing = sorted(set(rest_world) - set(world))
        if missing:
            faults.append(
                f"{BROKEN_RIG_PREFIX}i-rest: {len(missing)} canonical "
                f"joint(s) missing from the bare rig (first: {missing[0]}) "
                "— the host built a different rig")
        offenders = []
        for name in sorted(set(rest_world) & set(world)):
            if not world[name]:
                continue
            got = world[name][0]
            d = max(abs(float(got[i]) - rest_world[name][i])
                    for i in range(3))
            if d * 1000.0 > rest_mm:
                offenders.append((d, name))
        for d, name in sorted(offenders, reverse=True)[:8]:
            faults.append(
                f"{BROKEN_RIG_PREFIX}i-rest: joint '{name}' sits "
                f"{d * 1000.0:.3f} mm from the canonical rest world — "
                f"exceeds the {rest_mm:.3f} mm threshold (the host built "
                "a different or broken rig)")
        if len(offenders) > 8:
            faults.append(
                f"{BROKEN_RIG_PREFIX}i-rest: {len(offenders) - 8} further "
                "joint(s) beyond the threshold")

    # --- i-bones: bone lengths, canonical or at least constant -----------
    by = {j["name"]: j for j in skeleton.get("joints") or []}
    offenders = []
    for name in sorted(world):
        parent = by.get(name, {}).get("parent")
        if not parent or parent not in world:
            continue
        child_rows, parent_rows = world[name], world[parent]
        frames = min(len(child_rows), len(parent_rows))
        if not frames:
            continue
        lengths = []
        for f in range(frames):
            a, b = child_rows[f], parent_rows[f]
            lengths.append(math.sqrt(sum(
                (float(a[i]) - float(b[i])) ** 2 for i in range(3))))
        if regime == "none":
            t = by[name].get("rest_translation") or (0.0, 0.0, 0.0)
            canon = math.sqrt(sum(float(x) ** 2 for x in t))
            worst_f = max(range(frames),
                          key=lambda f: abs(lengths[f] - canon))
            d = abs(lengths[worst_f] - canon)
            if d * 1000.0 > bone_mm:
                offenders.append((
                    d,
                    f"{BROKEN_RIG_PREFIX}i-bones: bone '{parent}'->'{name}' "
                    f"measures {lengths[worst_f] * 1000.0:.3f} mm at frame "
                    f"{worst_f}, canonical {canon * 1000.0:.3f} mm — |d| "
                    f"{d * 1000.0:.3f} mm exceeds the {bone_mm:.3f} mm "
                    "threshold"))
        else:
            f_min = min(range(frames), key=lambda f: lengths[f])
            f_max = max(range(frames), key=lambda f: lengths[f])
            spread = lengths[f_max] - lengths[f_min]
            if spread * 1000.0 > bone_mm:
                offenders.append((
                    spread,
                    f"{BROKEN_RIG_PREFIX}i-bones: bone '{parent}'->'{name}' "
                    f"stretches {spread * 1000.0:.3f} mm between frames "
                    f"{f_min} and {f_max} — under regime '{regime}' a user "
                    "rig's lengths differ from canon legitimately, but a "
                    "bone's length must be CONSTANT; exceeds the "
                    f"{bone_mm:.3f} mm threshold"))
    for _d, line in sorted(offenders, reverse=True)[:8]:
        faults.append(line)
    if len(offenders) > 8:
        faults.append(f"{BROKEN_RIG_PREFIX}i-bones: {len(offenders) - 8} "
                      "further bone(s) beyond the threshold")
    return faults
