"""Qt worker thread for video capture.

A capture is minutes, not seconds — the estimator runs about one second
per frame and a 10-second clip at 15 fps is 150 of them — so it cannot
share the generation worker's assumptions about a job that finishes
while the user waits. Same shape, different timescale: ``result`` for
success, ``failed`` for any exception, ``progress`` for the status line.

``QThread.finished`` is deliberately left alone here as it is in
``generation_worker``: the host connects to it for GC-safe cleanup, and
overloading it as a success signal is how a worker gets collected
mid-emit.

Both workers publish the server-side ``job_id`` as a plain attribute
rather than through ``result``. The signal stays ``result(motion_data)``
so one host handler can serve a fresh capture and a re-fetch of the same
job; the attribute is written before the emit, and the emit crosses
threads as a queued event, which orders the write before the host's
read.
"""

from __future__ import annotations

import time

from .qt_compat import QtCore, Signal

from .. import capture_client


class UploadWorker(QtCore.QThread):
    """Send a clip to the service and read back its info, off the UI thread.

    The clip is capped at 200 MB but is routinely tens of them, so this
    cannot run on the UI thread even though the preview it feeds is the
    only reason it runs early. ``result`` carries everything the section
    needs to describe the clip — id, info, and the path it came from, so
    a result that lands after the user retyped the path can be matched
    against what the field holds now.
    """

    result = Signal(object)                # {"upload_id", "info", "path"}
    failed = Signal(str)

    def __init__(self, video_path, *, base_url=None, parent=None):
        super().__init__(parent)
        self._base_url = base_url or capture_client.DEFAULT_CAPTURE_URL
        #: The clip being uploaded, for the host's pending/label bookkeeping.
        self.video_path = video_path

    def run(self) -> None:
        try:
            upload_id = capture_client.upload(self.video_path,
                                              base_url=self._base_url)
            info = capture_client.upload_info(upload_id,
                                              base_url=self._base_url)
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.result.emit({"upload_id": upload_id, "info": info,
                          "path": self.video_path})


class CaptureWorker(QtCore.QThread):
    """Upload a clip, wait for the service, hand back the captured people.

    ``result`` carries a LIST of ``motion_data`` dicts — one per person in
    the clip, and a list of one for the ordinary single-subject capture,
    so the host has a single shape to apply rather than two.
    """

    result = Signal(object)
    failed = Signal(str)
    #: A stop the user asked for, kept off ``failed`` on purpose: the host
    #: styles failures as errors, and a deliberate cancel arriving in red
    #: tells the user their own click went wrong.
    cancelled = Signal()
    progress = Signal(str)

    def __init__(self, video_path, *, base_url=None, camera="static",
                 target_fps=None, upload_id=None, people=None, props=None,
                 extra_paths=None, parent=None):
        """*upload_id* skips the upload: the clip is already on the
        service, put there when the user picked it or opened the preview.
        Without it the upload happens here, as it always did.

        *people* is ``"single"`` (or ``None``) for one performer and
        ``"all"`` for everyone the tracker finds.

        *props* is the list of COCO class names to track as objects, or
        empty for none — the path that shipped.

        *extra_paths* are the OTHER cameras that filmed the same motion
        (Q3). Empty — the default — is the single-clip capture, request
        and all. They are uploaded here rather than through the window's
        one-clip-at-a-time upload slot: the slot exists to feed the
        preview of the clip in the field, and these clips are not in the
        field, are not previewed, and are only ever wanted by the job
        that is starting anyway. ``video_path`` stays the reference
        camera and stays first in the request, which is what makes it
        the camera the fused take's world comes from."""
        super().__init__(parent)
        self._video_path = video_path
        self._extra_paths = [str(p) for p in (extra_paths or [])]
        self._base_url = base_url or capture_client.DEFAULT_CAPTURE_URL
        self._camera = camera
        self._target_fps = target_fps
        self._upload_id = upload_id
        self._people = people
        self._props = list(props or [])
        #: Read by the host once the job exists, so a finished capture can
        #: be re-fetched without paying for the estimator again.
        self.job_id = None
        #: The clip this job came from, for the host's "last capture" label.
        self.video_path = video_path
        #: Wall-clock seconds the estimator took, written before ``result``.
        #: The host divides it by the frame count to price the next clip;
        #: the upload is excluded because it scales with megabytes, not
        #: with frames.
        self.elapsed_s = None
        #: The service's own quality summary for the finished job (vertex
        #: error, contact frames, …), written before ``result`` like the
        #: other attributes. ``None`` until the job is done.
        self.summary = None
        #: The clip's tracked objects and the stride their frame indices
        #: are on — same attribute idiom as ``summary``, because
        #: ``result`` carries the PEOPLE and props belong to the clip,
        #: not to any of them. Empty for a job that asked for none.
        self.props = []
        self.props_stride = 1

    def run(self) -> None:
        # The round trip is spelled out rather than delegated to
        # ``capture_client.capture``: that helper returns motion_data
        # only, and the job id — the one thing worth keeping after the
        # minutes are spent — never leaves it.
        try:
            upload_id = self._upload_id
            if not upload_id:
                self.progress.emit("uploading")
                upload_id = capture_client.upload(self._video_path,
                                                  base_url=self._base_url)
            upload_ids = [upload_id]
            for index, path in enumerate(self._extra_paths, 2):
                self.progress.emit(
                    f"uploading camera {index}/{len(self._extra_paths) + 1}")
                upload_ids.append(capture_client.upload(
                    path, base_url=self._base_url))
            # ``upload_ids`` only when there is more than one: the client
            # sends the single-clip request, field for field, for one.
            job_id = capture_client.start(
                upload_id if len(upload_ids) == 1 else None,
                upload_ids=None if len(upload_ids) == 1 else upload_ids,
                camera=self._camera, target_fps=self._target_fps,
                people=self._people, props=self._props,
                base_url=self._base_url)
            self.job_id = job_id
            started = time.monotonic()
            job = capture_client.wait(job_id, base_url=self._base_url,
                                      on_progress=self.progress.emit)
            self.elapsed_s = time.monotonic() - started
            self.summary = job.get("result") if isinstance(job, dict) else None
            self.progress.emit("fetching motion")
            payload = capture_client.fetch_motion(job_id,
                                                  base_url=self._base_url)
            motion = capture_client.to_subject_motions(payload)
            self.props, self.props_stride = (
                capture_client.props_from_payload(payload))
        except capture_client.CaptureCancelled:
            # Caught BEFORE the blanket handler below, which would report
            # a stop the user asked for as a capture that broke.
            self.cancelled.emit()
            return
        except Exception as exc:                       # noqa: BLE001
            # Every failure mode here is the operator's to act on — the
            # service is down, the clip has no person in it, the job
            # timed out — so none of them should reach a traceback in
            # the host's console.
            self.failed.emit(str(exc))
            return
        self.result.emit(motion)


class ReapplyWorker(QtCore.QThread):
    """Re-fetch a finished job and hand back the same people it captured.

    Seconds, not minutes — nothing is estimated again — but the payload
    is megabytes of JSON, which is long enough to freeze the UI thread,
    so it gets a thread like the capture it repeats.

    The store behind the job keeps only the most recent jobs and does not
    survive a service restart, so a 404 here is ordinary, not a fault.
    """

    result = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, job_id, *, base_url=None, video_path=None, parent=None):
        super().__init__(parent)
        self.job_id = job_id
        self.video_path = video_path
        self._base_url = base_url or capture_client.DEFAULT_CAPTURE_URL
        #: Read off the same payload as the people, for the same reason
        #: the capture worker keeps them: a reapply puts back everything
        #: the job produced, objects included.
        self.props = []
        self.props_stride = 1

    def run(self) -> None:
        try:
            self.progress.emit("fetching motion")
            payload = capture_client.fetch_motion(self.job_id,
                                                  base_url=self._base_url)
            motion = capture_client.to_subject_motions(payload)
            self.props, self.props_stride = (
                capture_client.props_from_payload(payload))
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.result.emit(motion)
