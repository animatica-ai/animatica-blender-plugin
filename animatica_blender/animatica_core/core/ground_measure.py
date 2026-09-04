"""Signed ground offset of a parsed server response — the observational probe.

Measures how far the returned motion sits above (+) or below (−) the ground
implied by the skeleton we sent, as a **pure function of the parsed response**
(``motion_data`` from :mod:`animatica_core.gltf_parser`). The comparison is
one joint against itself — the lowest-rest joint's contact-frame world Y minus
its own rest Y — which cancels anatomy by construction (research §1.1).

Runs on the ``GenerationWorker`` thread, so: numpy only — no pyfbsdk, no Qt —
and it must never raise (mirrors ``debug_io``'s "debug capture must not be able
to abort a generate run" discipline; any missing or malformed input degrades to
``None``). Purely observational — nothing here moves the applied motion; the
gated correction that consumes this number is a separate, default-OFF setting.

Two more helpers live here for the CAPTURE grounding step, which measures the
same quantity from the other side: :func:`contact_joints_per_frame` says which
joints the service marked planted on which frames, and :func:`rig_ground_offset`
turns the heights the host then reads OFF THE USER'S RIG into an offset plus a
trust verdict. The split is deliberate — the probe above FKs the SERVICE's
skeleton, and the vertical error a capture actually suffers from is precisely
the difference between that skeleton and the user's, so one measurement cannot
answer both questions.

FK convention: world rotations/positions accumulate root→leaf with the local
offset taken as the world-space rest delta (``rest[child] − rest[parent]``) —
exact when node rest rotations are identity, which holds on every capture and
on the canonical ``somaskel30`` path (all PreRotations identity, so the rest
conjugation is a no-op; see plan "Key Discoveries").
"""

from __future__ import annotations

from typing import Any

import numpy as np


# Fallback contact selection when the server reported no contacts for the
# measured joint: the lowest 1/5 of its per-frame world Y (research §1.1's
# method for the 77-joint captures).
_QUINTILE = 5

# Contact-frame Y spread above which the measured offset is treated as
# unreliable and the gated correction must not act on it. Research §1.1
# measured std = 0.000025 m over 60 planted frames, so 5 mm is ~200× any
# trustworthy plant — past it the "planted" joint was moving.
MAX_TRUSTED_STD_M = 0.005

# The same gate for a CAPTURE, where 5 mm is the wrong number by an order of
# magnitude. Generation's contacts come out of a model that was asked for a
# plant and produces one to numerical precision; a capture's come out of a
# geometric detector run over a monocular fit of real footage, so a genuine
# plant still breathes. Measured std of the per-frame lowest contact foot,
# on the user's rig, over the three eval clips (Q1 calibration, 2026-08-27):
#
#     matt_walking_01   1.19 cm   (11 contact frames of 55)
#     matt_walking_02   1.34 cm   (13 contact frames of 63)
#     tennis-static     2.16 cm   (141 contact frames of 312)
#
# 4 cm is set above the worst of the three with room to spare, and well below
# the spread a clip with no real plant produces (the airborne
# `handheld-tricking` reference sits far above it). It gates SANITY, not
# quality: the number it protects is a median over the contact frames, which
# a 2 cm spread barely moves, whereas a clip whose "contacts" are a detector
# hallucinating through a jump would move it by a lot. Pairing a 5 mm gate
# with capture data would simply have refused every clip we have.
CAPTURE_MAX_TRUSTED_STD_M = 0.04


def measure_ground_offset(motion_data: dict) -> dict[str, Any] | None:
    """Measure the signed vertical ground offset of one ``motion_data`` sample.

    Returns a summary dict, or ``None`` on any missing/malformed input:

        ``offset_m``            – float, median contact-frame world Y of the
                                  measured joint minus its rest Y (meters,
                                  signed; + = floats above the ground)
        ``joint``               – str, the measured joint (lowest rest Y)
        ``contact_source``      – ``"server_contacts"`` | ``"lowest_quintile"``
        ``num_contact_frames``  – int, frames the median was taken over
        ``std_m``               – float, std-dev of the contact-frame Y values;
                                  a large value means the offset is unreliable
                                  and must not be acted on
        ``rest_y_m``            – float, the joint's world rest Y (diagnostic)

    Never raises.
    """
    try:
        return _measure(motion_data)
    except Exception:
        return None


def correction_from_summary(summary: "dict[str, Any] | None", *,
                            skeleton_source: "str | None",
                            enabled: bool,
                            max_std_m: float = MAX_TRUSTED_STD_M) -> float:
    """The ground correction (meters) apply may subtract from the root Y.

    Pure gate for the default-OFF "correct ground offset" setting: returns
    ``summary["offset_m"]`` only when ALL of these hold, else ``0.0``:

    * *enabled* — the user opted in (``AppState.ground_correction_enabled``);
    * *skeleton_source* == ``"canonical"`` — the retargeted (wire) path's sink
      is variable with no identified mechanism (research Open Question 6), so
      only canonical-skeleton responses are corrected;
    * *summary* is a :func:`measure_ground_offset` result whose ``std_m`` is at
      most *max_std_m* — a large spread means the measured joint was moving,
      so the number must not be acted on.

    ``0.0`` keeps ``apply_animation``'s root Y byte-identical to the
    uncorrected path (its ``ground_offset_m`` default). Never raises.
    """
    try:
        if not enabled or skeleton_source != "canonical":
            return 0.0
        if not isinstance(summary, dict):
            return 0.0
        offset = summary.get("offset_m")
        std    = summary.get("std_m")
        if not isinstance(offset, (int, float)) or not isinstance(std, (int, float)):
            return 0.0
        if float(std) > float(max_std_m):
            return 0.0
        return float(offset)
    except Exception:
        return 0.0


def contact_joints_per_frame(motion_data: dict) -> "dict[int, tuple]":
    """``{frame_index: (joint_name, ...)}`` for every frame the server marked.

    The half of the capture grounding step that needs no scene: which joints,
    on which frames, the service says were touching the floor. The host reads
    those joints' world Y at those frames off the RIG (which is why this
    cannot simply reuse :func:`measure_ground_offset` — that one FKs the
    SERVICE's skeleton, and the whole vertical error being corrected here is
    the difference between the two).

    Empty when the payload carries no usable contacts. Never raises.
    """
    try:
        names = motion_data.get("joint_names")
        contacts = motion_data.get("foot_contacts")
        if not isinstance(names, list) or not names:
            return {}
        if not isinstance(contacts, np.ndarray) or contacts.ndim != 2:
            return {}
        if contacts.shape[1] != len(names):
            return {}
        flags = contacts.astype(bool)
        out = {}
        for frame in np.flatnonzero(flags.any(axis=1)):
            out[int(frame)] = tuple(names[j]
                                    for j in np.flatnonzero(flags[frame]))
        return out
    except Exception:
        return {}


def rig_ground_offset(lowest_y_m, *, ground_y_m: float = 0.0,
                      max_std_m: float = CAPTURE_MAX_TRUSTED_STD_M):
    """Summarise per-contact-frame lowest-foot heights read off the rig.

    *lowest_y_m* is one world Y per contact frame, in metres: the LOWEST foot
    joint that frame reported in contact. *ground_y_m* is where the scene's
    floor is. Returns

        ``offset_m``            – median(lowest) − ground; subtract it from the
                                  applied root Y and the planted foot lands on
                                  the floor
        ``std_m``               – spread of the contact heights
        ``num_contact_frames``  – how many frames the median was taken over
        ``trusted``             – ``std_m <= max_std_m``

    or ``None`` when there is nothing to measure. The caller decides what to
    do with an untrusted measurement; this function only refuses to hide it.
    Never raises.
    """
    try:
        values = np.asarray(list(lowest_y_m), dtype=np.float64)
        if values.size == 0 or not np.all(np.isfinite(values)):
            return None
        return {
            "offset_m":           float(np.median(values) - float(ground_y_m)),
            "std_m":              float(np.std(values)),
            "num_contact_frames": int(values.size),
            "trusted":            bool(float(np.std(values)) <= float(max_std_m)),
        }
    except Exception:
        return None


def _measure(motion_data: dict) -> dict[str, Any] | None:
    joint_names = motion_data.get("joint_names")
    hierarchy   = motion_data.get("hierarchy")
    rest        = motion_data.get("rest_positions")
    local_rot   = motion_data.get("local_rot_mats")
    posed       = motion_data.get("posed_joints")

    if not isinstance(joint_names, list) or not joint_names:
        return None
    if not isinstance(hierarchy, list) or len(hierarchy) != len(joint_names):
        return None
    if not isinstance(rest, dict):
        return None
    if not isinstance(local_rot, np.ndarray) or local_rot.ndim != 4:
        return None
    if not isinstance(posed, np.ndarray) or posed.ndim != 3:
        return None

    num_frames = int(local_rot.shape[0])
    num_joints = len(joint_names)
    if num_frames < 2 or posed.shape[0] != num_frames:
        return None
    if local_rot.shape[1] != num_joints or posed.shape[1] != num_joints:
        return None
    if any(name not in rest for name in joint_names):
        return None

    # 1. The measured joint: lowest world rest Y (ties → first in joint order).
    rest_y = [float(rest[name][1]) for name in joint_names]
    target = int(np.argmin(rest_y))

    # 2. FK the target's world Y per frame along its root→target chain.
    y = _fk_world_y(target, joint_names, hierarchy, rest, local_rot, posed)
    if y is None:
        return None

    # 3. Contact frames: the server's contacts column for that joint, falling
    #    back to its lowest-Y quintile when absent or all-False.
    contact_y, source = _contact_frame_y(motion_data, y, target, num_frames)

    # 4. Anatomy cancels: the planted joint should return to its own rest
    #    height, so the median excess IS the ground float.
    return {
        "offset_m":           float(np.median(contact_y) - rest_y[target]),
        "joint":              joint_names[target],
        "contact_source":     source,
        "num_contact_frames": int(contact_y.shape[0]),
        "std_m":              float(np.std(contact_y)),
        "rest_y_m":           rest_y[target],
    }


def _fk_world_y(target: int, joint_names: list, hierarchy: list, rest: dict,
                local_rot: np.ndarray, posed: np.ndarray) -> "np.ndarray | None":
    """World Y of joint *target* per frame, FK'd down its root→target chain.

    ``hierarchy`` parents are joint *names* (the parser already remapped glTF
    node indices — joint ``j`` ↔ node ``sorted_nodes[j]`` — into name space).
    Returns ``None`` when the parent chain doesn't terminate at a root.
    """
    index_of  = {name: j for j, name in enumerate(joint_names)}
    parent_of = {name: parent for name, parent in hierarchy}

    chain: list[int] = []                    # target → … → root, joint indices
    name: "str | None" = joint_names[target]
    while name is not None:
        j = index_of.get(name)
        if j is None or j in chain:          # unknown parent / cycle
            return None
        chain.append(j)
        name = parent_of.get(name)
    chain.reverse()                          # root → … → target

    root = chain[0]
    world_p = posed[:, root].astype(np.float64)       # root world translation
    world_R = local_rot[:, root].astype(np.float64)   # root local == world
    for parent, j in zip(chain, chain[1:]):
        offset  = (np.asarray(rest[joint_names[j]],      dtype=np.float64)
                   - np.asarray(rest[joint_names[parent]], dtype=np.float64))
        world_p = world_p + world_R @ offset
        world_R = world_R @ local_rot[:, j].astype(np.float64)
    return world_p[:, 1]


def _contact_frame_y(motion_data: dict, y: np.ndarray, target: int,
                     num_frames: int) -> "tuple[np.ndarray, str]":
    """The Y values to take the median over, plus which source supplied them."""
    contacts = motion_data.get("foot_contacts")
    if (isinstance(contacts, np.ndarray) and contacts.ndim == 2
            and contacts.shape[0] == num_frames and target < contacts.shape[1]):
        col = contacts[:, target].astype(bool)
        if col.any():
            return y[col], "server_contacts"
    k = max(1, num_frames // _QUINTILE)
    return np.sort(y)[:k], "lowest_quintile"
