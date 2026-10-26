# SPDX-License-Identifier: GPL-3.0-or-later
"""How each take ends, sent to Animatica Cloud to help improve the service.

Every take waits to be judged, and only the add-on sees how it ends. It is
sent against the job the server named for the take (``X-Job-Id``), where the
request and the result already are: ``POST /mmcp/generate/jobs/{id}/feedback``.

* ``accepted`` -- Keep (on the bar or in the sidebar), or Keep All of a batch.
* ``kept`` -- kept by fine-tuning it: Marionette's first edit on a take keeps
  it (see ``operators.keep_take``), a softer yes than Keep.
* ``rejected`` -- Discard.
* ``regenerated`` -- another take replaced it before anyone decided: Redo, of
  the whole take or of one block, Generate over a take nobody kept, or a
  version of the take from before a block Redo shown again.

With it go the variation showing, every variation the artist looked at (only
those lose to the one kept), and how many keys they changed on the take first.

The job rides on the take's preview session (see preview_session), so it
survives a save and reload and comes back with Ctrl+Z like the rest of the
take; a duplicate's copy of another character's session sends nothing.

Sent only to Animatica Cloud, signed in, with Blender's online access on and
"Share how I judge takes" on (the default, under Privacy in the preferences). Never in the artist's way: nothing
here raises, and the request goes from a daemon thread whose failure is
dropped.
"""

from __future__ import annotations

import json
import threading
import traceback
from urllib.request import Request

VERDICTS = ("accepted", "kept", "rejected", "regenerated")

#: the take's job, in its preview session:
#: ``{"id", "samples", "sample": showing, "seen": [looked at]}``
_FIELD = "job"

_TIMEOUT = 10.0


def enabled() -> bool:
    """May a verdict go out now? Main thread."""
    from . import mmcp_client
    prefs = mmcp_client._addon_prefs()
    if prefs is None or not getattr(prefs, "share_feedback", True):
        return False
    url = mmcp_client.get_mmcp_url()
    return (mmcp_client.is_cloud_url(url) and mmcp_client.may_connect(url)
            and bool(mmcp_client.get_access_token()))


def _job(arm) -> dict | None:
    from . import preview_session
    data = preview_session.get(arm)
    if data is None or preview_session.is_copy(arm):
        return None
    job = data.get(_FIELD)
    return job if isinstance(job, dict) and job.get("id") else None


def note_job(arm, job_id: str, result=None, *, samples: int | None = None,
             sample: int = 0) -> None:
    """A take from job *job_id* (answered with *result*) now shows on *arm*,
    variation *sample* of it. Main thread, after the bake.

    The take it replaced in the same session was never decided on: the artist
    asked again instead, and that is its verdict."""
    from . import preview_session, variations
    try:
        if samples is None:
            samples = variations.sample_count(result)
        take = variations.take_of(arm)
        if job_id and take is not None and result is not None and take.result is result:
            take.job_id = job_id                        # its versions are this job's
        before = _job(arm)
        if before is not None and before["id"] != job_id:
            send(_review(arm, before, edits=False), "regenerated")
        if preview_session.get(arm) is not None:
            preview_session.update(arm, **{_FIELD: {
                "id": job_id, "samples": int(samples), "sample": int(sample),
                "seen": [int(sample)],
            } if job_id else None})
    except Exception:                                   # noqa: BLE001 — never in the way
        traceback.print_exc()


def shown(arm, index: int, *, job_id: str = "", samples: int = 1) -> None:
    """Variation *index* of job *job_id* now shows on *arm*. Main thread.

    Of another job than the take's -- the versions of a take whose block was
    redone since -- the take is that job's again, and the redo regenerated."""
    from . import preview_session
    try:
        job = _job(arm)
        if job_id and (job is None or job["id"] != job_id):
            note_job(arm, job_id, samples=samples, sample=index)
            return
        if job is None:
            return
        job["sample"] = int(index)
        job["seen"] = sorted({int(i) for i in job.get("seen") or ()} | {int(index)})
        preview_session.update(arm, **{_FIELD: job})
    except Exception:                                   # noqa: BLE001 — never in the way
        traceback.print_exc()


def review(arm) -> dict | None:
    """What a verdict on the take on *arm* would say, taken before Accept or
    Reject changes anything: they close the session and fold the edits in.
    None when there is nothing to judge. Main thread."""
    try:
        job = _job(arm)
        return _review(arm, job) if job is not None else None
    except Exception:                                   # noqa: BLE001 — never in the way
        traceback.print_exc()
        return None


def _review(arm, job: dict, *, edits: bool = True) -> dict:
    out = {"job_id": job["id"], "sample_index": job.get("sample"),
           "seen_samples": job.get("seen")}
    if edits:
        out["edited_keys"] = _edited_keys(arm)
    return out


def _edited_keys(arm) -> int | None:
    """Keys the artist added or changed on the take: one per channel group and
    frame, so a rotation keyed once is one key, not four."""
    from . import preview_session
    ad = arm.animation_data
    action = ad.action if ad is not None else None
    if action is None:
        return None
    try:
        states = preview_session.edits(action)
    except Exception:                                   # noqa: BLE001
        return None
    return len({(path, round(state["co"][0], 3)) for path, _index, state in states})


def send(review: dict | None, verdict: str) -> threading.Thread | None:
    """Send *verdict* on the take *review* describes, in the background. Main
    thread. Returns the thread, or None when nothing goes out."""
    if review is None or verdict not in VERDICTS:
        return None
    try:
        if not enabled():
            return None
        from . import mmcp_client
        url = f"{mmcp_client.get_mmcp_url()}/generate/jobs/{review['job_id']}/feedback"
        body = {"verdict": verdict}
        body.update({k: v for k, v in review.items() if k != "job_id" and v is not None})
        # the headers here, on the main thread: they read the preferences
        headers = mmcp_client._auth_headers(url, {
            "Content-Type": "application/json; charset=utf-8",
            **mmcp_client.client_headers(),
        })
        thread = threading.Thread(target=_post, args=(url, headers, json.dumps(body).encode()),
                                  daemon=True, name="animatica-feedback")
        thread.start()
        return thread
    except Exception:                                   # noqa: BLE001 — never in the way
        traceback.print_exc()
        return None


def _post(url: str, headers: dict, body: bytes) -> None:
    """POST it once. Worker thread; a failure is dropped.

    A session that ran out is not renewed from here: a refresh rotates the
    refresh token, and racing a generation's own refresh would sign the
    artist out over a verdict."""
    from . import mmcp_client
    try:
        req = Request(url, data=body, method="POST", headers=headers)
        with mmcp_client.urlopen(req, timeout=_TIMEOUT):
            pass
    except Exception as exc:                            # noqa: BLE001 — dropped
        print(f"[animatica] feedback not sent: {exc}")
