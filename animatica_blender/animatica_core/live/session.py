"""Playback clock, frame buffer and record marks for a live session.

Pure Python + numpy (no pyfbsdk, no Qt, no sockets) -- unit-tested outside
MotionBuilder.

Time model (plan D5): every frame carries a server timestamp ``t0 + i/fps``
(seconds since session start). The client anchors its wall clock on the
FIRST received packet plus a deliberate buffer delay of
``buffer_windows * horizon / fps`` -- the buffer absorbs generation jitter,
so the preview never starves right after start. The preview shows the
newest frame at-or-before the playback clock (late frames drop from the
preview only); recording always cuts from the full buffer, so a bake never
misses frames the preview skipped.
"""

from __future__ import annotations

import bisect
import time

import numpy as np


class LiveFrame:
    """One streamed frame: server time + pose."""

    __slots__ = ("t", "root_pos", "rotations")

    def __init__(self, t, root_pos, rotations):
        self.t = float(t)
        self.root_pos = root_pos          # (x, y, z) meters, Y-up
        self.rotations = rotations        # [J][4] xyzw local quaternions


def _slerp(q0, q1, t):
    """Shortest-arc slerp between two [J, 4] xyzw quaternion sets."""
    a = np.asarray(q0, dtype=np.float64)
    b = np.asarray(q1, dtype=np.float64)
    dot = (a * b).sum(axis=1, keepdims=True)
    b = np.where(dot < 0.0, -b, b)         # shortest arc
    dot = np.abs(dot).clip(0.0, 1.0)
    theta = np.arccos(dot)
    sin_theta = np.sin(theta)
    near = sin_theta < 1e-6                # parallel -> plain lerp
    safe = np.where(near, 1.0, sin_theta)
    w0 = np.where(near, 1.0 - t, np.sin((1.0 - t) * theta) / safe)
    w1 = np.where(near, t, np.sin(t * theta) / safe)
    out = w0 * a + w1 * b
    norm = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norm < 1e-12, 1.0, norm)


class LiveSession:
    """Frame buffer with a buffered playback clock and record marks."""

    def __init__(self, fps, horizon_frames, buffer_windows=1):
        self.fps = float(fps)
        self.horizon = int(horizon_frames)
        self.buffer_delay = buffer_windows * self.horizon / self.fps
        # Extra backlog (seconds) tolerated on top of the buffer before the
        # clock jumps forward. Covers the start-up burst (the server
        # generates during handshake + skeleton build) and UI stalls;
        # without it the backlog becomes permanent control latency. Must
        # exceed one window: frames arrive in window-sized bursts, so the
        # backlog naturally oscillates by that much — a smaller slack would
        # trigger a (visible) clock jump on every packet.
        self.catchup_slack = 1.2 * self.horizon / self.fps

        # How far back the buffer is kept once nothing is recording. Sized
        # by the longest look-back any reader has: the viz trail's 10 s
        # window, plus room for the playback clock's own lag and a late
        # packet. Recording overrides it (see _trim).
        self.keep_seconds = 20.0

        self.frames = []           # LiveFrame, ascending t
        self._times = []           # parallel list of t for bisect
        self._anchor_mono = None   # wall clock at first packet
        self._anchor_t0 = None     # server t of first packet
        self._record_start = None  # playback-clock position of Record press
        # Diagnostics (read by scripts/dev_profile.py): an underrun holds
        # the newest pose for a moment, which the eye reads as a stutter —
        # indistinguishable from a dropped preview frame without a count.
        self.underruns = 0
        self.catchups = 0
        self._starved = False      # inside an underrun episode right now

    # -- ingest ----------------------------------------------------------

    def ingest_packet(self, packet, now=None):
        """Append one wire packet ({"t0": ..., "frames": [...]})."""
        t0 = float(packet["t0"])
        if self._anchor_mono is None:
            self._anchor_mono = time.monotonic() if now is None else now
            self._anchor_t0 = t0
        step = 1.0 / self.fps
        for i, f in enumerate(packet["frames"]):
            t = t0 + i * step
            self.frames.append(LiveFrame(t, f["root_pos"], f["rotations"]))
            self._times.append(t)
        self._trim()

    def _trim(self):
        """Drop frames nobody can still ask for.

        The buffer used to be append-only: an hour-long session held every
        frame of it (measured: 3656 frames after three minutes), and the
        trail redraw scans the whole list. Nothing reads further back than
        :attr:`keep_seconds`, EXCEPT a recording in progress -- it cuts
        from this buffer when it stops, so while ``recording`` the record
        start is the hard floor and the buffer grows for the take's
        duration. That is intended: a bake must not lose frames.
        """
        if not self._times:
            return
        floor = self._times[-1] - self.keep_seconds
        if self._record_start is not None:
            floor = min(floor, self._record_start)
        cut = bisect.bisect_left(self._times, floor)
        if cut > 0:
            del self.frames[:cut]
            del self._times[:cut]

    # -- playback clock --------------------------------------------------

    def playback_time(self, now=None):
        """Current playback position in server time, or None before anchor.

        Self-correcting: when the newest buffered frame runs more than
        ``buffer_delay + catchup_slack`` ahead of the playback position, the
        anchor shifts forward so the preview skips the backlog and returns
        to one buffer of lag (the preview may visibly jump; recordings cut
        from the full buffer either way).
        """
        if self._anchor_mono is None:
            return None
        mono = time.monotonic() if now is None else now
        pos = self._anchor_t0 + (mono - self._anchor_mono) - self.buffer_delay
        if self._times:
            newest = self._times[-1]
            backlog = newest - pos
            excess = backlog - self.buffer_delay - self.catchup_slack
            if excess > 0:
                shift = backlog - self.buffer_delay
                self._anchor_t0 += shift
                pos += shift
                self.catchups += 1
            elif pos > newest:
                # Underrun: the wall clock outran the stream (a slow or
                # stalled server). Clamp to the newest frame instead of
                # drifting permanently ahead — otherwise the preview would
                # only ever show the newest arrival, and playback would
                # stay ahead by the whole outage even after recovery.
                shift = newest - pos
                self._anchor_t0 += shift
                pos = newest
                # Count EPISODES, not ticks: the clock is re-clamped on
                # every tick until the next packet lands, so counting ticks
                # reports the tick rate rather than how often the preview
                # actually ran dry.
                if not self._starved:
                    self.underruns += 1
                    self._starved = True
            else:
                self._starved = False
        return pos

    def frame_for_now(self, now=None):
        """Newest frame at-or-before the playback clock (None = hold)."""
        pos = self.playback_time(now)
        if pos is None:
            return None
        idx = bisect.bisect_right(self._times, pos) - 1
        if idx < 0:
            return None
        return self.frames[idx]

    def pose_for_now(self, now=None):
        """Pose at the playback clock, INTERPOLATED between stream frames.

        The stream runs at 20 fps while the preview tick runs faster, so
        showing the nearest frame lands poses on an irregular beat (some
        33 ms apart, some 66 ms) — which reads as judder even though the
        data is fine. Interpolating (slerp on the joint quaternions, lerp
        on the root) gives one smooth pose per tick. Recording is
        untouched: takes are baked from the raw frames.
        """
        pos = self.playback_time(now)
        if pos is None:
            return None
        idx = bisect.bisect_right(self._times, pos) - 1
        if idx < 0:
            return None
        f0 = self.frames[idx]
        if idx + 1 >= len(self.frames):
            return f0                      # newest: hold until more arrive
        f1 = self.frames[idx + 1]
        span = f1.t - f0.t
        if span <= 0:
            return f0
        a = (pos - f0.t) / span
        if a <= 1e-4:
            return f0
        root = [f0.root_pos[i] + (f1.root_pos[i] - f0.root_pos[i]) * a
                for i in range(3)]
        return LiveFrame(pos, root, _slerp(f0.rotations, f1.rotations, a))

    def lag_frames(self, now=None):
        """How far generation runs ahead of playback (buffer health)."""
        pos = self.playback_time(now)
        if pos is None or not self._times:
            return 0
        return max(0, int(round((self._times[-1] - pos) * self.fps)))

    # -- recording -------------------------------------------------------

    @property
    def recording(self):
        return self._record_start is not None

    def start_record(self, now=None):
        """Mark the record start at the CURRENT playback position."""
        pos = self.playback_time(now)
        # before the anchor (no frame shown yet) recording starts at t=0
        self._record_start = pos if pos is not None else 0.0

    def stop_record(self, now=None):
        """Return the recorded slice [record_start, playback_now] and clear.

        Cut on playback positions -- exactly the stretch the operator saw --
        but from the FULL buffer, so preview drops never lose frames.
        """
        if self._record_start is None:
            return []
        start = self._record_start
        stop = self.playback_time(now)
        self._record_start = None
        if stop is None:
            return []
        lo = bisect.bisect_left(self._times, start - 1e-9)
        hi = bisect.bisect_right(self._times, stop + 1e-9)
        return self.frames[lo:hi]


def to_motion_data(frames, fps, joint_names):
    """Recorded frames -> the ``motion_data`` dict the animator consumes.

    Matches the contract of :func:`..bridge.animator.apply_animation`:
    ``local_rot_mats`` (T, J, 3, 3) from the streamed XYZW quaternions (the
    same conversion the batch glTF path uses) and ``posed_joints`` (T, J, 3)
    world meters -- the animator only reads index 0 (the root) for the root
    translation, which is exactly what the stream carries.
    """
    if not frames:
        raise ValueError("no frames recorded")
    from ..gltf_parser import _xyzw_to_rotmat

    quats = np.asarray([f.rotations for f in frames], dtype=np.float64)
    if quats.ndim != 3 or quats.shape[2] != 4:
        raise ValueError(f"bad rotations shape {quats.shape}")
    n_frames, n_joints = quats.shape[0], quats.shape[1]
    if n_joints != len(joint_names):
        raise ValueError(
            f"{n_joints} rotation tracks vs {len(joint_names)} joint names")

    local_rot_mats = np.stack(
        [_xyzw_to_rotmat(quats[i]) for i in range(n_frames)])

    posed_joints = np.zeros((n_frames, n_joints, 3), dtype=np.float64)
    posed_joints[:, 0, :] = np.asarray(
        [f.root_pos for f in frames], dtype=np.float64)

    return {
        "local_rot_mats": local_rot_mats,
        "posed_joints": posed_joints,
        "joint_names": list(joint_names),
        "fps": float(fps),
        "num_frames": n_frames,
    }


def preview_pose_data(frame, joint_names):
    """One streamed frame -> the 1-frame dict ``apply_single_pose`` reads."""
    from ..gltf_parser import _xyzw_to_rotmat

    quats = np.asarray(frame.rotations, dtype=np.float64)
    return {
        "local_rot_mats": _xyzw_to_rotmat(quats)[None, ...],
        "joint_names": list(joint_names),
    }
