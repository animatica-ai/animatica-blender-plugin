# SPDX-License-Identifier: GPL-3.0-or-later
"""What a take waiting for Accept / Reject needs to put things back.

From the moment a take is baked onto a character until it is accepted or
rejected, the character carries a *session*: the action it had (and whether
that action had a fake user of its own), the pose every bone was in, the
scene's frame range, and -- when the take was written into the user's own
action -- a copy of that action as it was. Reject goes back to exactly that;
Accept lets it go. It lives on the armature as a custom property, so it
survives a save and reload, and a second character's take does not disturb
the first's.

Each take also records the value of every key it baked (the *baseline*). A
key the artist adds or changes while previewing differs from it, and that is
how Reject tells their edits (which stay) from the take's own keys (which go)
-- including a key typed over one of the take's, which Blender leaves typed
GENERATED.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib

import bpy
import numpy as np
from mathutils import Vector

from .constraints_ui import (
    _copy_keyframe_point,
    _ensure_fcurve,
    iter_action_fcurves,
)

_KEY = "animatica_preview_session"
_BASELINE = "animatica_baked"
#: A splice's copy of the user's action. The leading dot keeps it out of
#: Blender's action pickers.
_BACKUP_PREFIX = ".Animatica backup: "
_EPS = 1e-6


def get(arm) -> dict | None:
    """The session waiting on *arm*, or None."""
    if arm is None:
        return None
    try:
        raw = arm.get(_KEY)
    except ReferenceError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _put(arm, data: dict) -> None:
    arm[_KEY] = json.dumps(data)


def update(arm, **fields) -> None:
    data = get(arm)
    if data is not None:
        data.update(fields)
        _put(arm, data)


# --- the pose ---------------------------------------------------------------

def _pose_snapshot(arm) -> dict:
    return {
        pb.name: {
            "mode": pb.rotation_mode,
            "loc": list(pb.location),
            "quat": list(pb.rotation_quaternion),
            "euler": list(pb.rotation_euler),
            "aa": list(pb.rotation_axis_angle),
            "scale": list(pb.scale),
        }
        for pb in arm.pose.bones
    }


def _restore_pose(arm, pose: dict) -> None:
    """Every bone back as it was. Keyed channels are re-evaluated from the
    action straight after; this is what puts back the ones nothing keys,
    which a take moves and removing its curves does not move back. The
    rotation mode first: a bake sets every bone to quaternions, and setting
    the mode converts the values."""
    for pb in arm.pose.bones:
        p = pose.get(pb.name)
        if not p:
            continue
        try:
            pb.rotation_mode = p["mode"]
            pb.location = p["loc"]
            pb.rotation_quaternion = p["quat"]
            pb.rotation_euler = p["euler"]
            pb.rotation_axis_angle = p["aa"]
            pb.scale = p["scale"]
        except (KeyError, TypeError, ValueError):
            continue


# --- opening and closing ------------------------------------------------------

def begin(context, arm, source) -> bool:
    """Open a session on *arm* before its first take is baked. True if this
    call opened it (a regenerate or a variation finds one open already).

    *source* is the user's own action, restored by Reject. It is detached
    while a fresh take previews, which leaves it with no users: a save then
    dropped it and Reject had nothing to go back to. It gets a fake user for
    as long as the take waits, and its own flag back afterwards.
    """
    if get(arm) is not None:
        return False
    ad = arm.animation_data
    active = ad.action if ad is not None else None
    scene = context.scene
    data = {
        "source": source.name if source is not None else "",
        "source_fake": bool(source.use_fake_user) if source is not None else False,
        "active": active.name if active is not None else "",
        "pose": _pose_snapshot(arm),
        "frame_range": [int(scene.frame_start), int(scene.frame_end)],
        "backup": "",
    }
    if source is not None:
        source.use_fake_user = True
    _put(arm, data)
    return True


def backup_before_splice(arm, action) -> None:
    """Keep a copy of *action* as it is, before a take is written into it
    (once a session: a regenerate splices over the take, not the original)."""
    data = get(arm)
    if data is None:
        return
    if data.get("backup") and bpy.data.actions.get(data["backup"]) is not None:
        return
    copy = action.copy()
    copy.name = (_BACKUP_PREFIX + action.name)[:63]
    copy.use_fake_user = True
    if _BASELINE in copy:
        del copy[_BASELINE]
    data["backup"] = copy.name
    data["spliced"] = action.name
    _put(arm, data)


def backup_of(arm):
    data = get(arm)
    name = (data or {}).get("backup")
    return bpy.data.actions.get(name) if name else None


def source_of(arm):
    data = get(arm)
    name = (data or {}).get("source")
    return bpy.data.actions.get(name) if name else None


def _release(data: dict) -> None:
    source = bpy.data.actions.get(data.get("source") or "")
    if source is not None:
        source.use_fake_user = bool(data.get("source_fake"))
    backup = bpy.data.actions.get(data.get("backup") or "")
    if backup is not None:
        bpy.data.actions.remove(backup)


def finish(context, arm, *, accepted: bool) -> None:
    """Close the session. On Reject the pose and the frame range go back as
    they were; either way the user's action gets its own fake-user flag back
    and the splice copy goes."""
    data = get(arm)
    if data is None:
        return
    if not accepted:
        _restore_pose(arm, data.get("pose") or {})
        loop_range = data.get("loop_range")
        scene = context.scene
        if loop_range and [scene.frame_start, scene.frame_end] == list(loop_range):
            # The loop set the range to its cycle; the take is gone, so is that.
            scene.frame_start, scene.frame_end = data["frame_range"]
    _release(data)
    ad = arm.animation_data
    if ad is not None and ad.action is not None and _BASELINE in ad.action:
        del ad.action[_BASELINE]
    del arm[_KEY]


def abort(context, arm) -> None:
    """A first take whose bake failed: back to exactly where Generate began --
    the action that was active, the pose -- and whatever the bake had made
    so far removed."""
    data = get(arm)
    if data is None:
        return
    ad = arm.animation_data
    before = bpy.data.actions.get(data.get("active") or "")
    made = ad.action if ad is not None else None
    if backup_of(arm) is not None and made is not None and made.name == data.get("spliced"):
        before = restore_backup(arm) or before
        made = None
    if ad is not None:
        ad.action = before
    if (made is not None and made is not before and made.users == 0
            and not made.use_fake_user):
        bpy.data.actions.remove(made)
    finish(context, arm, accepted=False)


def _copy_keys(src_fc, dst_fc) -> None:
    """Make *dst_fc*'s keys exactly *src_fc*'s."""
    kps = dst_fc.keyframe_points
    kps.clear()
    kps.add(len(src_fc.keyframe_points))
    for d, k in zip(kps, src_fc.keyframe_points):
        d.co = k.co
        d.handle_left_type = k.handle_left_type
        d.handle_right_type = k.handle_right_type
        d.handle_left = k.handle_left
        d.handle_right = k.handle_right
        d.interpolation = k.interpolation
        d.easing = k.easing
        d.back = k.back
        d.amplitude = k.amplitude
        d.period = k.period
        d.type = k.type
    dst_fc.extrapolation = src_fc.extrapolation
    dst_fc.update()


def restore_backup(arm):
    """Put the user's action back as it was before the splice, from the copy:
    every curve's keys, no curve the take added, the addon's markers. In
    place, so the action stays the one datablock everything refers to.
    Returns it."""
    from .constraints_ui import _iter_fcurve_collections
    data = get(arm)
    backup = backup_of(arm)
    if data is None or backup is None:
        return None
    action = bpy.data.actions.get(data.get("spliced") or "")
    if action is None:
        return None
    saved = {(fc.data_path, fc.array_index): fc for fc in iter_action_fcurves(backup)}
    seen = set()
    for fcurves in _iter_fcurve_collections(action):
        for fc in list(fcurves):
            key = (fc.data_path, fc.array_index)
            if key in saved:
                _copy_keys(saved[key], fc)
                seen.add(key)
            else:
                fcurves.remove(fc)
    for key, src in saved.items():
        if key in seen:
            continue
        fc = _ensure_fcurve(action, *key)
        if fc is not None:
            _copy_keys(src, fc)
    for k in [k for k in action.keys() if k.startswith("animatica_")]:
        del action[k]
    for k in [k for k in backup.keys() if k.startswith("animatica_")]:
        v = backup[k]
        action[k] = v.to_dict() if hasattr(v, "to_dict") else v.to_list() if hasattr(v, "to_list") else v
    bpy.data.actions.remove(backup)
    data["backup"] = ""
    _put(arm, data)
    return action


# --- what the take baked, and what the artist changed -------------------------
#
# The record is one compressed blob, not an ID property per curve: a curve's
# key ("pose.bones[...].rotation_quaternion|3") is longer than the 63
# characters Blender allows an ID property's name on any rig with long bone
# names, and a dense take's thousands of per-curve arrays were megabytes of ID
# properties copied into every undo step.

#: a string (base64): an ID property holding bytes is cut short at a zero byte
_BLOB_MAGIC = "AMB1:"
_ROUND = 3


def _fc_key(fc) -> str:
    return f"{fc.data_path}|{fc.array_index}"


def _co(fc):
    """``(frames, values)`` of *fc*'s keys, as float32 arrays (what Blender
    stores them as, so a comparison is exact)."""
    n = len(fc.keyframe_points)
    co = np.empty(2 * n, dtype=np.float32)
    if n:
        fc.keyframe_points.foreach_get("co", co)
    return co[0::2], co[1::2]


def _frames(xs):
    return np.round(xs.astype(np.float64), _ROUND)


def _pack(base: dict) -> str:
    keys = list(base)
    counts = [int(len(base[k][0])) for k in keys]
    head = json.dumps([keys, counts]).encode()
    xs = np.concatenate([base[k][0] for k in keys]) if keys else np.empty(0, np.float32)
    ys = np.concatenate([base[k][1] for k in keys]) if keys else np.empty(0, np.float32)
    body = (struct.pack("<I", len(head)) + head
            + xs.astype("<f4").tobytes() + ys.astype("<f4").tobytes())
    return _BLOB_MAGIC + base64.b64encode(zlib.compress(body, 1)).decode("ascii")


def _unpack(blob: str) -> dict:
    body = zlib.decompress(base64.b64decode(blob[len(_BLOB_MAGIC):]))
    (hn,) = struct.unpack_from("<I", body)
    keys, counts = json.loads(body[4:4 + hn].decode())
    total = int(sum(counts))
    arr = np.frombuffer(body, dtype="<f4", offset=4 + hn, count=2 * total)
    xs, ys = arr[:total], arr[total:]
    out, at = {}, 0
    for k, n in zip(keys, counts):
        out[k] = (xs[at:at + n], ys[at:at + n])
        at += n
    return out


def _baseline(action):
    """What the take baked, ``{"path|index": (frames, values)}`` sorted by
    frame (frames rounded), or None when there is no record."""
    raw = action.get(_BASELINE) if action is not None else None
    if raw is None:
        return None
    try:
        if isinstance(raw, str) and raw.startswith(_BLOB_MAGIC):
            base = _unpack(raw)
        elif hasattr(raw, "to_dict"):         # a record written as ID properties (0.6.0-preview8)
            base = {}
            for k, v in raw.to_dict().items():
                co = np.asarray(list(v), dtype=np.float32)
                base[k] = (co[0::2], co[1::2])
        else:
            return None
    except Exception as exc:   # noqa: BLE001 -- a damaged record reads as none
        print(f"[animatica] preview: the take's key record is unreadable ({exc}); "
              "reading keys by type")
        return None
    out = {}
    for k, (xs, ys) in base.items():
        fr = _frames(xs)
        order = np.argsort(fr, kind="stable")
        out[k] = (fr[order], ys[order].astype(np.float64))
    return out


def record_baseline(action, *, exclude: dict | None = None) -> None:
    """Remember every key on *action* as the take's own.

    ``exclude`` (``{"path|index": {frame, ...}}``) leaves keys out of the
    record, so they go on reading as the artist's: see :class:`keeping_edits`.
    """
    if action is None:
        return
    base = {}
    for fc in iter_action_fcurves(action):
        xs, ys = _co(fc)
        skip = (exclude or {}).get(_fc_key(fc))
        if skip and len(xs):
            keep = ~np.isin(_frames(xs), np.fromiter(skip, dtype=np.float64))
            xs, ys = xs[keep], ys[keep]
        if len(xs):
            base[_fc_key(fc)] = (xs, ys)
    action[_BASELINE] = _pack(base)


def forget_baseline(action) -> None:
    if action is not None and _BASELINE in action:
        del action[_BASELINE]


def _log_failure(what: str) -> None:
    import traceback
    print(f"[animatica] preview: could not {what}; carrying on\n" + traceback.format_exc())


class keeping_edits:
    """``with keeping_edits(action):`` around addon code that rewrites keys of
    a take on show (In place, the hands, a block regenerated): what it changes
    is the take's, and what the artist had changed before stays theirs.

    Bookkeeping only: a failure in it is logged, never raised into the change
    it wraps.
    """

    def __init__(self, action):
        self.action = action
        try:
            self.tracked = action is not None and _BASELINE in action
        except ReferenceError:
            self.tracked = False
        self.mine = {}

    def __enter__(self):
        if self.tracked:
            try:
                states = edits(self.action)
                for path, index, state in states:
                    self.mine.setdefault(f"{path}|{index}", set()).add(
                        round(state["co"][0], _ROUND))
                promote_edits(self.action, states)
            except Exception:   # noqa: BLE001
                _log_failure("tell your edits from the take's")
                self.tracked = False
        return self

    def __exit__(self, *exc):
        if self.tracked:
            try:
                record_baseline(self.action, exclude=self.mine)
            except ReferenceError:
                pass
            except Exception:   # noqa: BLE001
                _log_failure("record the take's keys")
        return False


def _state(kp) -> dict:
    return {
        "co": (float(kp.co.x), float(kp.co.y)),
        "handle_left": (float(kp.handle_left.x), float(kp.handle_left.y)),
        "handle_right": (float(kp.handle_right.x), float(kp.handle_right.y)),
        "handle_left_type": kp.handle_left_type,
        "handle_right_type": kp.handle_right_type,
        "interpolation": kp.interpolation,
        "easing": kp.easing,
        "type": 'KEYFRAME' if kp.type == 'GENERATED' else kp.type,
    }


def edits(action) -> list:
    """The keys on *action* the artist added or changed since the take was
    baked, as ``(data_path, array_index, key state)``.

    A key the take baked and nobody touched is not one -- including the take's
    KEYFRAME-typed keys on the user's key frames, which hold the model's pose
    there, not the user's. A key the artist typed over one of the take's is,
    whatever its type. Every channel of a changed property at that frame goes
    together, so a rotation comes over whole rather than one component of it.

    With no record (a take baked by an older version) every key not typed
    GENERATED counts, which is how Reject used to decide.
    """
    if action is None:
        return []
    base = _baseline(action)
    by_path: dict = {}
    changed: set = set()
    for fc in iter_action_fcurves(action):
        xs, ys = _co(fc)
        fr = _frames(xs)
        by_path.setdefault(fc.data_path, []).append((fc, fr))
        if not len(fr):
            continue
        if base is None:
            new = np.ones(len(fr), dtype=bool)
        else:
            bfr, bys = base.get(_fc_key(fc), (None, None))
            if bfr is None or not len(bfr):
                new = np.ones(len(fr), dtype=bool)
            else:
                at = np.minimum(np.searchsorted(bfr, fr), len(bfr) - 1)
                found = bfr[at] == fr
                moved = found & (np.abs(bys[at] - ys.astype(np.float64)) > _EPS)
                for f in fr[moved]:
                    changed.add((fc.data_path, float(f)))
                new = ~found
        if new.any():
            kps = fc.keyframe_points
            for i in np.flatnonzero(new):
                if kps[int(i)].type != 'GENERATED':
                    changed.add((fc.data_path, float(fr[i])))
    out = []
    for path, f in sorted(changed):
        for fc, fr in by_path.get(path, ()):
            hit = np.flatnonzero(fr == f)
            if len(hit):
                out.append((path, fc.array_index, _state(fc.keyframe_points[int(hit[0])])))
    return out


def promote_edits(action, states=None) -> int:
    """Type the artist's changes on *action* as their keys (KEYFRAME), so the
    next generation keeps and follows them and nothing strips them.
    *states* is what :func:`edits` returned for *action*, if at hand."""
    if action is None:
        return 0
    if states is None:
        states = edits(action)
    wanted: dict = {}
    for p, i, s in states:
        wanted.setdefault((p, i), set()).add(round(s["co"][0], _ROUND))
    if not wanted:
        return 0
    n = 0
    for fc in iter_action_fcurves(action):
        frames = wanted.get((fc.data_path, fc.array_index))
        if not frames:
            continue
        fr = _frames(_co(fc)[0])
        for i in np.flatnonzero(np.isin(fr, np.fromiter(frames, dtype=np.float64))):
            kp = fc.keyframe_points[int(i)]
            if kp.type == 'GENERATED':
                kp.type = 'KEYFRAME'
                n += 1
    return n


class _Key:
    """A key state in the shape ``_copy_keyframe_point`` reads."""

    def __init__(self, s):
        self.co = Vector(s["co"])
        self.handle_left = s["handle_left"]
        self.handle_right = s["handle_right"]
        self.handle_left_type = s["handle_left_type"]
        self.handle_right_type = s["handle_right_type"]
        self.interpolation = s["interpolation"]
        self.easing = s["easing"]
        self.type = s["type"]


def apply_edits(states, action) -> int:
    """Write the artist's keys (from :func:`edits`) onto *action*."""
    if action is None:
        return 0
    touched = {}
    for path, index, s in states:
        fc = _ensure_fcurve(action, path, index)
        if fc is None:
            continue
        _copy_keyframe_point(_Key(s), fc)
        touched[(path, index)] = fc
    for fc in touched.values():
        fc.update()
    return len(states)


def fold_edits(arm, preview) -> int:
    """Before a take is replaced (Regenerate): the artist's changes on it go
    where Reject will restore from -- the user's action, or the splice's copy
    of it -- and are typed as theirs on the take, so the new request sends
    them and a splice keeps them."""
    states = edits(preview)
    if not states:
        return 0
    promote_edits(preview)
    data = get(arm) or {}
    backup = backup_of(arm)
    if backup is not None and preview is not None and preview.name == data.get("spliced"):
        target = backup
    else:
        target = bpy.data.actions.get(data.get("source") or "")
    if target is None or target is preview:
        return 0
    return apply_edits(states, target)
