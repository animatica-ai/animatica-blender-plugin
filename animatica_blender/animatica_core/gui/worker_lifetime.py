"""Keeping a QThread's Python wrapper alive until the C++ thread really ends.

Dropping the last reference to a ``QThread`` subclass while the native thread is
still running aborts the process — "QThread: Destroyed while thread is still
running" — and inside a DCC that is a hard crash with the user's scene in it.
Identity bookkeeping (``self._worker``, ``self._login_worker``) is *not* a
lifetime mechanism: those are reassigned the moment the next run starts.

So every launched worker goes into a set before ``start()`` and is discarded
only when its native ``finished`` signal fires. The discard lambda runs pure
Python under the GIL, and the ``w=worker`` default plus the sender-held
connection form a reference cycle, so the final decref never lands inline on the
worker thread — cyclic GC collects the wrapper later, after the thread has
truly finished.

Extracted from the MotionBuilder ``gui/tool_window.py`` in phase S3b; the crash
it prevents is documented in that plugin's ``doc/bugs/mobu_crash/ANALYSIS.md``.
"""

from __future__ import annotations


class WorkerRetentionMixin:
    """Holds every in-flight worker until its native thread reports finished."""

    def init_worker_retention(self) -> None:
        """Create the retention set. Call from ``__init__``, before any start()."""
        self._gen_workers: set = set()

    def _retain_worker(self, worker) -> None:
        """Keep *worker* referenced until its native ``QThread.finished`` fires.

        Wire this BEFORE ``worker.start()`` — a worker that finishes between
        ``start()`` and this call would never be discarded, and one that is
        never retained can be collected mid-run.
        """
        self._gen_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._gen_workers.discard(w))
