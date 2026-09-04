"""Qt worker thread for async MMCP server-availability probes.

Runs ``mmcp_client.probe_server()`` on a background ``QThread`` so the
MotionBuilder UI never blocks while checking whether the selected server
(local or Animatica Cloud) is reachable.

Mirrors ``generation_worker.GenerationWorker`` — only the network call runs
off-thread; the host applies the result back on the UI thread via signals.

Unlike generation, probes legitimately overlap (first-show, Test, and the
debounced backend/URL-change trigger can all be in flight, and an unreachable
host runs the full timeout). So:

* the custom completion signal is ``result`` — NOT ``finished`` — to leave the
  native ``QThread.finished`` free for the host's GC-safe cleanup, and
* the host tags each worker with a monotonic ``seq`` (+ a ``offer_recovery``
  intent flag) so a late result from a superseded probe is dropped instead of
  clobbering newer state.
"""

from .qt_compat import QtCore, Signal

from .. import mmcp_client
from ..animatica_auth import get_auth
from ..constants import ANIMATICA_MMCP_URL


_PROBE_TIMEOUT = 5.0  # short liveness check; matches the legacy Test path


class ConnectionProbeWorker(QtCore.QThread):
    """Async wrapper around ``mmcp_client.probe_server()``.

    Connect to ``result`` for the structured :class:`mmcp_client.ProbeResult`
    and ``failed`` for an unexpected exception (``probe_server`` itself never
    raises, so ``failed`` only fires on a programming error).

    ``mode`` uses the ``AppState.backend`` vocabulary — ``"local"`` (probe
    ``server_url`` with no auth) or ``"cloud"`` (probe ``ANIMATICA_MMCP_URL``
    with the Bearer token, retried once through ``auth.refresh()`` when the
    server reports the token expired). Note this differs from
    ``GenerationWorker``'s ``"local"``/``"remote"`` naming.

    ``seq`` and ``offer_recovery`` are set by the host before ``start()`` and
    travel with the worker so the result handler can match identity / suppress
    the recovery dialog without a side channel.
    """

    result = Signal(object)   # mmcp_client.ProbeResult
    failed = Signal(str)

    def __init__(self, server_url, mode="local", access_token=None, parent=None):
        super().__init__(parent)
        self._server_url = server_url
        self._mode = mode               # "local" | "cloud"
        self._access_token = access_token
        # Host-assigned just before start(); see class docstring.
        self.seq = 0
        self.offer_recovery = False

    def run(self):
        try:
            is_cloud = self._mode == "cloud"
            url = ANIMATICA_MMCP_URL if is_cloud else self._server_url
            token = self._access_token if is_cloud else None
            res = mmcp_client.probe_server(
                url, timeout=_PROBE_TIMEOUT, access_token=token,
            )
            # Cloud token may have expired mid-session — refresh once and retry,
            # mirroring GenerationWorker's auth recovery.
            if is_cloud and res.status == "auth_required":
                auth = get_auth()
                if auth.refresh():
                    self._access_token = auth.access_token
                    res = mmcp_client.probe_server(
                        url, timeout=_PROBE_TIMEOUT, access_token=auth.access_token,
                    )
            self.result.emit(res)
        except Exception as exc:
            self.failed.emit(str(exc))
