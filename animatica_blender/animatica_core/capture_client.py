"""Talk to the video-capture service, and hand the host a motion.

The capture service is separate from MMCP on purpose (see its own
docstring): minute-long batch work, large uploads, a different CUDA
stack, and a GPU peak that belongs to a process that exits. So it gets
its own client rather than another verb on ``mmcp_client``.

``urllib`` only, like ``mmcp_client``: MotionBuilder's embedded Python
ships neither ``requests`` nor ``httpx``, and a capture button is not
worth a vendored dependency.

**Why capture needs no retarget.** Generation returns a model skeleton
that differs from the user's, so a source rig is built, characterised,
and transferred through HIK. Capture does not: the service returns the
SOMA 77-joint rig, which IS ``somaskel77`` — the same names in the same
order. The joints can be keyed straight onto the user's rig, and
:func:`to_motion_data` produces exactly what ``animator.apply_animation``
already consumes.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.request
import uuid

import numpy as np

DEFAULT_CAPTURE_URL = os.environ.get("ANIMATICA_CAPTURE_URL",
                                     "http://localhost:8001")

#: The service refuses a second live job, so polling is the whole
#: protocol. Two seconds is slow enough to be free and fast enough that
#: a finished 20-second capture does not sit unnoticed.
POLL_SECONDS = 2.0


class CaptureError(RuntimeError):
    """The service said no, or could not be reached.

    ``status`` is the HTTP status when the service answered with one and
    ``None`` when it could not be reached at all. Callers that treat a
    single code as harmless — :func:`cancel` and its 409 — read that
    attribute instead of scraping the message text.
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class CaptureCancelled(CaptureError):
    """The job stopped because someone asked it to.

    A subclass rather than a return value so no caller can miss it, and
    an error rather than a plain result because the round trip did not
    produce a motion. It is still not a failure: a UI that styles this
    like a red error is telling the user their own click went wrong.
    """


def _open(url, *, data=None, headers=None, method="GET", timeout=30.0) -> bytes:
    """Do the request, return the raw response body.

    Shared by :func:`_request` (which decodes it as JSON) and
    :func:`fetch_frame` (which wants the bytes untouched) so the
    HTTPError/URLError handling lives in exactly one place.
    """
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise CaptureError(f"{method} {url} -> {exc.code}: {detail}",
                           status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise CaptureError(
            f"cannot reach the capture service at {url}: {exc.reason}. "
            f"Start it with: uvicorn capture.service:app --port 8001") from exc


def _request(url, *, data=None, headers=None, method="GET", timeout=30.0):
    body = _open(url, data=data, headers=headers, method=method, timeout=timeout)
    return json.loads(body.decode("utf-8"))


def health(base_url=DEFAULT_CAPTURE_URL, timeout=5.0) -> dict:
    return _request(f"{base_url}/health", timeout=timeout)


def upload(video_path, base_url=DEFAULT_CAPTURE_URL, timeout=300.0) -> str:
    """POST a video as multipart/form-data, built by hand.

    ``urllib`` has no multipart encoder, and the alternative — a
    dependency, or asking the operator to install one — costs more than
    twenty lines of boundary assembly.
    """
    with open(video_path, "rb") as handle:
        payload = handle.read()
    name = os.path.basename(video_path)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    boundary = f"----animatica{uuid.uuid4().hex}"

    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        .encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    result = _request(
        f"{base_url}/capture/upload", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Content-Length": str(len(body))},
        timeout=timeout)
    return result["upload_id"]


def start(upload_id=None, *, upload_ids=None, camera="static",
          target_fps=None, people=None, props=None,
          base_url=DEFAULT_CAPTURE_URL) -> str:
    """Queue a capture job. *people* is ``"single"`` or ``"all"``.

    Omitted from the request when unset, exactly as *target_fps* is: the
    service's own default is a single subject, and a service from before
    multi-person has never heard of the field.

    *props* is a list of COCO class NAMES to also track as objects
    (``["sports ball", "chair"]``). Same rule, and it matters more here:
    an empty list is left out of the request entirely, so a request that
    asks for no objects is byte-identical to the one that shipped before
    objects existed. A name outside COCO's 80 comes back as a 422 naming
    every name that does work — the service refuses rather than silently
    capturing nothing.

    *upload_ids* is several clips of ONE motion, filmed at the same
    moment from different angles — not a batch, which is several takes
    and several jobs. The FIRST id is the reference camera: the fused
    take keeps its world, its scale and its root, so the phone that saw
    the performer best goes first. The service runs them one after
    another on its single GPU, so the job costs N times a single
    capture; it then synchronises them, fuses them, and cleans the
    result up. When the clips cannot be placed on one clock it returns
    the reference camera's capture alone and says so in the job summary
    — see ``multiview_note``.

    One id, whichever field carries it, is the single-clip path: the
    request is the request that shipped, field for field.
    """
    ids = [str(i) for i in (upload_ids or [])]
    if ids and upload_id:
        raise CaptureError("start() takes upload_id or upload_ids, not both")
    if len(ids) > 1:
        return _start(
            {"upload_ids": ids, "camera": camera}, target_fps=target_fps,
            people=people, props=props, base_url=base_url)
    upload_id = upload_id or (ids[0] if ids else None)
    if not upload_id:
        raise CaptureError("start() needs an upload_id")
    return _start({"upload_id": upload_id, "camera": camera},
                  target_fps=target_fps, people=people, props=props,
                  base_url=base_url)


def _start(request, *, target_fps, people, props, base_url) -> str:
    """POST the request both forms of :func:`start` end up building.

    The optional fields are added here, in this order, so the single-clip
    request is the same bytes it has always been.
    """
    if target_fps:
        request["target_fps"] = float(target_fps)
    if people:
        request["people"] = str(people)
    if props:
        request["props"] = [str(p) for p in props]
    result = _request(f"{base_url}/capture/start",
                      data=json.dumps(request).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
    return result["id"]


def multiview_note(summary) -> str | None:
    """The one line a multi-camera job is worth putting in front of a user.

    ``None`` for a single-clip capture, which has nothing multi-camera to
    say. The sentence itself is the SERVICE's — it is the only side that
    knows how many cameras made it into the take, which one the world
    came from, and how convinced the synchronisation was — so this reads
    it out of the poll summary rather than assembling one from the
    numbers. A client that wrote its own could say "3 cameras fused"
    about a job that fused none.
    """
    if not isinstance(summary, dict):
        return None
    verdict = summary.get("multiview")
    if not isinstance(verdict, dict):
        return None
    note = verdict.get("note")
    return str(note) if note else None


def wait(job_id, *, base_url=DEFAULT_CAPTURE_URL, on_progress=None,
         timeout_s=3600.0) -> dict:
    """Poll until the job settles. Raises on failure, with the tail.

    Three terminal states, not two. A cancelled job carries no ``error``
    and no ``result``, so without a case of its own it matched neither
    branch and this loop kept polling a finished job until *timeout_s* —
    an hour, by default — expired.
    """
    deadline = time.time() + timeout_s
    while True:
        job = _request(f"{base_url}/capture/jobs/{job_id}")
        state = job.get("state")
        if state == "done":
            return job
        if state == "cancelled":
            raise CaptureCancelled("capture cancelled")
        if state == "failed":
            raise CaptureError(job.get("error") or "capture failed")
        if time.time() > deadline:
            raise CaptureError(
                f"capture still {state} after {timeout_s:.0f}s; the job "
                f"keeps running server-side — poll {job_id} later")
        if on_progress:
            on_progress(job.get("progress") or state)
        time.sleep(POLL_SECONDS)


def cancel(job_id, base_url=DEFAULT_CAPTURE_URL, timeout=10.0):
    """Ask the service to stop a queued or running job.

    Returns immediately with the job as it stands — cancellation is
    cooperative, so a running job is still ``running`` (with progress
    ``cancelling``) for a poll or two before it settles as ``cancelled``.
    The terminal state is learned from :func:`wait`, not from here, which
    is why this is cheap enough to call on the UI thread.

    A 409 means the job was already terminal when the click landed:
    there is nothing to stop and nothing worth reporting, so it returns
    ``None`` instead of raising. Every other status — 404 for a job the
    service never had, included — raises :class:`CaptureError`.
    """
    try:
        return _request(f"{base_url}/capture/jobs/{job_id}/cancel",
                        data=b"", method="POST", timeout=timeout)
    except CaptureError as exc:
        if exc.status == 409:
            return None
        raise


def fetch_motion(job_id, base_url=DEFAULT_CAPTURE_URL, timeout=300.0) -> dict:
    return _request(f"{base_url}/capture/jobs/{job_id}/motion", timeout=timeout)


def upload_info(upload_id, base_url=DEFAULT_CAPTURE_URL, timeout=10.0) -> dict:
    """fps/frame count/duration/size for an uploaded clip, for the preview.

    Raises :class:`CaptureError` (with the service's 404 in it) for an
    ``upload_id`` the service does not know.
    """
    return _request(f"{base_url}/capture/uploads/{upload_id}/info",
                    timeout=timeout)


def fetch_frame(upload_id, index, max_px=480, base_url=DEFAULT_CAPTURE_URL,
                timeout=30.0) -> bytes:
    """One preview frame as raw JPEG bytes.

    Unlike the rest of this client the response is not JSON, so it goes
    through :func:`_open` directly rather than :func:`_request`. An
    out-of-range ``index`` raises :class:`CaptureError` with the
    service's 422 in it; an unknown ``upload_id`` raises with its 404.
    """
    url = (f"{base_url}/capture/uploads/{upload_id}/frame"
          f"?index={index}&max_px={max_px}")
    return _open(url, timeout=timeout)


def quaternions_to_matrices(quaternions) -> np.ndarray:
    """(..., 4) xyzw to (..., 3, 3) rotation matrices.

    The service speaks quaternions because JSON does; ``apply_animation``
    wants matrices. Normalising first is not defensive padding — JSON
    round-trips float32 through decimal text, and an un-normalised
    quaternion produces a matrix that scales as well as rotates, which
    reads in the viewport as limbs that grow.
    """
    q = np.asarray(quaternions, dtype=np.float64)
    if q.shape[-1] != 4:
        raise ValueError(f"expected xyzw quaternions, got {q.shape}")
    q = q / np.linalg.norm(q, axis=-1, keepdims=True)
    x, y, z, w = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], -1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], -1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], -1),
    ], axis=-2)


#: The names the service calls "the bottom of a leg". Verbatim from the
#: server's own ``capture/adapters/base.py`` -- ``resolve_feet`` is what
#: decides how many columns the ``contacts`` array has, so the two lists have
#: to be the same list. (Two lists that must agree are a bug with a delay on
#: it; here the delay is a network hop, so the guard below never trusts the
#: match blindly.)
_FOOT_NAME = re.compile(r"(ankle|foot|heel|toe)", re.I)
_NOT_FOOT  = re.compile(r"(forearm|together|footer)", re.I)


def foot_columns(joint_names) -> list:
    """Joint indices the service's ``contacts`` COLUMNS correspond to.

    The service emits one contact column per foot joint, in joint order --
    six for SOMA-77, not 77. Everything downstream (``core.ground_measure``,
    ``gltf_parser``'s documented ``[T, J]`` shape) indexes contacts BY JOINT,
    so somebody has to widen them; this is the one place that knows both.
    """
    return [i for i, n in enumerate(joint_names)
            if _FOOT_NAME.search(n) and not _NOT_FOOT.search(n)]


def contacts_by_joint(contacts, joint_names):
    """Service ``contacts`` -> ``(F, J)`` bool aligned to *joint_names*.

    ``None`` when the payload carried no contacts, or when the column count
    matches neither the joint count nor the foot count — an unrecognised
    layout must read as "no contacts", never as contacts on the wrong joints.
    A payload that already speaks per-joint passes straight through, so an
    older or a future service both work.
    """
    if contacts is None:
        return None
    arr = np.asarray(contacts)
    if arr.ndim != 2 or arr.shape[0] == 0:
        return None
    arr = arr.astype(bool)
    if arr.shape[1] == len(joint_names):
        return arr
    feet = foot_columns(joint_names)
    if not feet or arr.shape[1] != len(feet):
        return None
    wide = np.zeros((arr.shape[0], len(joint_names)), dtype=bool)
    wide[:, feet] = arr
    return wide


def to_motion_data(payload: dict) -> dict:
    """The service's JSON as ``animator.apply_animation`` expects it.

    Refuses a payload whose joint names are not the rig we key onto.
    Silence here would be expensive: 77 arrays would land on 77 joints in
    whatever order they arrived, and a limb-for-limb scramble looks like
    a bad capture rather than a bad mapping.

    Carries ``hierarchy`` and ``rest_positions`` as well, because the two
    consumers of a capture's contacts both need them and neither can invent
    them: ``core.ground_measure`` refuses a sample without them, and the
    plugin's grounding step walks the chain to find the lowest foot. They are
    the canonical SOMA-77 values -- which is not a guess, since the name check
    directly above has just established that this payload IS ``somaskel77``.
    """
    from .constants import DEFAULT_HIP_HEIGHT
    from .skeleton import get_joint_hierarchy, get_neutral_positions

    names = list(payload.get("joint_names") or [])
    # Names come from the HIERARCHY, which is (name, parent) pairs.
    # SOMA77_NEUTRAL_POSITIONS is 77 coordinate triples and carries no
    # names at all -- reading it as a name list produced a check that
    # compared joint names against x-coordinates and rejected every
    # correct capture. The unit test missed it by building its fixture
    # from the same wrong source, so it agreed with the bug.
    expected = [name for name, _parent in get_joint_hierarchy()]
    if names != expected:
        extra = set(names) - set(expected)
        missing = set(expected) - set(names)
        raise CaptureError(
            f"capture returned {len(names)} joints that are not this rig "
            f"(missing {sorted(missing)[:4]}, unexpected {sorted(extra)[:4]}); "
            f"refusing to key them in arrival order")

    joints = np.asarray(payload["joints"], dtype=np.float32)
    rotations = quaternions_to_matrices(payload["rotations"]).astype(np.float32)
    contacts = payload.get("contacts")
    return {
        "posed_joints": joints,
        # Parent-relative already: the adapter converts them once, on the
        # server, where the bone topology lives.
        "local_rot_mats": rotations,
        "global_rot_mats": None,
        # Widened from the service's per-FOOT columns to the per-JOINT shape
        # every consumer documents. Before this they were carried verbatim and
        # silently consumed by nothing.
        "foot_contacts": contacts_by_joint(contacts, names),
        "fps": float(payload.get("fps") or 30.0),
        "num_frames": int(joints.shape[0]),
        "num_joints": int(joints.shape[1]),
        "joint_names": names,
        "hierarchy": get_joint_hierarchy(),
        "rest_positions": get_neutral_positions(hip_height=DEFAULT_HIP_HEIGHT),
    }


def to_subject_motions(payload: dict) -> list:
    """Every person in *payload*, each as one ``motion_data`` dict.

    One ``if``, on purpose. A service that captured several people lists
    them under ``subjects``; one that did not IS the single subject —
    the older shape is the same fields at the top level, and the new one
    mirrors ``subjects[0]`` there for exactly that reason. So the list
    to walk is ``subjects`` or the payload itself, and everything
    downstream stops caring which server answered.

    Two per-subject facts ride along beside the arrays, because the
    apply needs them and ``to_motion_data`` knows nothing about people:

    * ``track_id`` — whose motion this is, as the tracker named them;
    * ``frame_offset`` — the clip sample this subject's first sample sits
      on, so sample *k* of a subject is sample ``frame_offset + k`` of
      the clip. Someone who walks in halfway is keyed halfway in rather
      than at the head of the take.
    """
    subjects = payload.get("subjects") or [payload]
    motions = []
    for subject in subjects:
        motion = to_motion_data(subject)
        motion["track_id"] = subject.get("track_id")
        motion["frame_offset"] = int(subject.get("frame_offset") or 0)
        motions.append(motion)
    return motions


def props_from_payload(payload: dict):
    """``(props, stride)`` — the clip's objects and the clock they use.

    Two values because one is useless without the other. A prop's
    ``frames`` are SOURCE video frame indices while the payload's
    ``fps`` is the SAMPLING rate (``source_fps / stride``), so placing a
    prop in time needs the stride that separates the two grids — the
    host's ``bridge.props`` derives the arithmetic in full.

    ``([], 1)`` for every payload without props: a service that never
    heard of objects, and a request that asked for none, are the same
    answer here and neither is an error. The stride falls back to 1 for
    the same reason a missing ``props`` list falls back to empty — an
    older payload has no ``meta["stride"]`` and also has nothing to
    place with it.
    """
    props = payload.get("props") or []
    meta = payload.get("meta") or {}
    try:
        stride = max(1, int(meta.get("stride") or 1))
    except (TypeError, ValueError):
        stride = 1
    return list(props), stride


def capture(video_path, *, camera="static", target_fps=None,
            base_url=DEFAULT_CAPTURE_URL, on_progress=None) -> dict:
    """Upload, run, wait, fetch — the whole round trip as motion_data."""
    if on_progress:
        on_progress("uploading")
    upload_id = upload(video_path, base_url=base_url)
    job_id = start(upload_id, camera=camera, target_fps=target_fps,
                   base_url=base_url)
    wait(job_id, base_url=base_url, on_progress=on_progress)
    if on_progress:
        on_progress("fetching motion")
    return to_motion_data(fetch_motion(job_id, base_url=base_url))
