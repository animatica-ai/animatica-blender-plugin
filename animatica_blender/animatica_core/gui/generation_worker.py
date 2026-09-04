"""Qt worker thread for async MMCP generation calls.

Runs ``mmcp_client.generate()`` on a background ``QThread`` so the
MotionBuilder UI stays responsive during the 60-120 s generation time.

Two server modes are supported:
- ``"local"``  — plain HTTP to a local MMCP server (no auth)
- ``"remote"`` — HTTPS to Animatica Cloud with a Bearer access token
"""

import time

from .qt_compat import QtCore, Signal

from .. import mmcp_client
from ..animatica_auth import get_auth
from ..constants import ANIMATICA_MMCP_URL
from ..gltf_parser import parse_gltf_samples


class GenerationWorker(QtCore.QThread):
    """Async wrapper around ``mmcp_client.generate()`` + ``parse_gltf()``.

    Connect to ``result`` for success and ``failed`` for any exception.
    ``progress`` carries status-line strings emitted during the run.
    ``result`` carries ``list[dict]`` — one ``motion_data`` per sample
    (``num_samples`` variants); a single-sample run emits a one-element list.

    The success signal is ``result`` — NOT ``finished`` — to leave the native
    ``QThread.finished`` free for the host's GC-safe cleanup (see
    ``connection_worker.ConnectionProbeWorker``).
    """

    result = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, server_url, request_body, mode="local", access_token=None,
                 debug_session_dir=None, skeleton_source=None, parent=None):
        super().__init__(parent)
        self._server_url = server_url
        self._request_body = request_body
        self._mode = mode              # "local" | "remote"
        self._access_token = access_token
        self._debug_session_dir = debug_session_dir
        # Which skeleton block the request carried ("canonical" | "wire"), for
        # the ground.json provenance — threaded from the caller's selection
        # branch the same way _debug_session_dir is. None when unknown.
        self._skeleton_source = skeleton_source

    def run(self):
        # Everything is inside the try, imports included. A QThread swallows
        # whatever escapes run(): Qt prints it to stderr and the thread simply
        # ends, so a failure here reads in the UI as a generation that started
        # and then never said anything again. Two lines used to sit outside.
        t0 = time.time()

        def _emit(msg):
            self.progress.emit(f"[+{time.time() - t0:.3f}s] {msg}")

        debug_io = None
        try:
            # First thing the thread does, so the UI can distinguish "the
            # thread never ran" from "the thread ran and stalled".
            self.progress.emit("[+0.000s] Generation thread started…")
            from ..core import debug_io, ground_measure
            _emit("Sending request to server…")
            url = ANIMATICA_MMCP_URL if self._mode == "remote" else self._server_url
            token = self._access_token if self._mode == "remote" else None
            debug_io.breadcrumb("generate:request", mode=self._mode)
            # Persists through the blocking POST + server compute; on the async
            # (202-poll) path the poll loop's "Generating… Ns" takes over, on the
            # sync path it stays until the response returns and parsing begins.
            _emit("Waiting for server…")
            t_wait = time.time()
            try:
                gltf = mmcp_client.generate(
                    url, self._request_body, access_token=token,
                    on_progress=_emit,
                )
            except mmcp_client.MmcpError as exc:
                if exc.code == "auth_required" and self._mode == "remote":
                    auth = get_auth()
                    if auth.refresh():
                        self._access_token = auth.access_token
                        token = auth.access_token
                        gltf = mmcp_client.generate(
                            url, self._request_body, access_token=token,
                            on_progress=_emit,
                        )
                    else:
                        self.failed.emit("Session expired. Please log in to Animatica Cloud again.")
                        return
                else:
                    raise
            debug_io.write_json(self._debug_session_dir, "response.json", gltf)
            _emit(f"Server responded in {time.time() - t_wait:.3f}s — parsing motion data…")
            debug_io.breadcrumb("generate:parse")
            t_parse = time.time()
            samples = parse_gltf_samples(gltf)
            debug_io.write_json(
                self._debug_session_dir, "motion.json",
                debug_io.summarize_motion(samples[0]),
            )
            # Skeleton provenance rides on every sample so the apply side can
            # gate the canonical-only ground correction straight off the
            # motion_data — computed once at request time, consumed at apply
            # (tool_window._ground_correction_m). summarize_motion allowlists
            # its keys, so the extra string is never serialized by mistake.
            for s in samples:
                s["skeleton_source"] = self._skeleton_source
            # Ground-offset probe: logged, captured, and stashed on the
            # samples. Observational here — nothing in the worker moves; the
            # stashed ground_summary feeds the default-OFF, std-gated ground
            # correction at apply time. measure_ground_offset is a pure-numpy
            # function of the parsed response and never raises.
            ground = ground_measure.measure_ground_offset(samples[0])
            if ground is not None:
                for s in samples:
                    s["ground_offset_m"] = ground["offset_m"]
                    s["ground_summary"]  = ground   # carries std_m + contact_source
                _emit(
                    "Ground offset: {offset_m:+.4f} m "
                    "({joint}, {contact_source})".format(**ground)
                )
            debug_io.write_json(
                self._debug_session_dir, "ground.json",
                {"skeleton_source": self._skeleton_source,
                 **(ground if ground is not None else {"offset_m": None})},
            )
            _emit(
                f"Parsed {len(samples)} sample(s) in {time.time() - t_parse:.3f}s "
                f"— sending to MotionBuilder…"
            )
            self.result.emit(samples)
        except Exception as exc:
            # debug_io is None when the failure was its own import.
            if debug_io is not None:
                try:
                    debug_io.write_error(self._debug_session_dir, exc)
                except Exception:
                    pass
            import traceback
            print("[animatica] generation worker raised:" + chr(10)
                  + traceback.format_exc(), flush=True)
            self.failed.emit(f"{type(exc).__name__}: {exc}")
