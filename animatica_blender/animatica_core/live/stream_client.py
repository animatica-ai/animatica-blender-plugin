"""Network client for the ``/stream/ardy/*`` live channel.

``urllib`` only -- same zero-dependency rule as :mod:`..mmcp_client` (the
MotionBuilder interpreter ships no ``requests``/``websocket-client`` and we
do not want the installer to grow a pip step). The frame stream is NDJSON
over a long-lived chunked HTTP response, which ``urllib`` reads
incrementally via ``readline()``.

No pyfbsdk, no Qt: everything here is plain ``threading`` + ``queue`` so it
runs (and is tested) outside MotionBuilder. The reader thread pushes parsed
packets into :attr:`StreamClient.packets`; the GUI tick drains that queue on
the UI thread.

Wire messages (see server ``stream_ardy.py``):

    {"t0": <sec>, "frames": [{"root_pos": [x,y,z],
                              "rotations": [[x,y,z,w] * J]} * horizon]}
    {"event": "end", "error": <str|null>}    -- final line of a stream
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request

CONTROL_RETRY_BACKOFF = 1.0  # s -- pause after a failed control post
_HTTP_TIMEOUT = 30.0

#: Sentinel put on :attr:`StreamClient.packets` when the stream ends.
#: ``error`` is None for a clean end (server stop / session teardown).
class EndOfStream:
    def __init__(self, error=None):
        self.error = error

    def __repr__(self):
        return f"EndOfStream(error={self.error!r})"


class StreamClientError(RuntimeError):
    """Transport or server-side failure of the live channel."""


def _prefer_ipv4_loopback(url):
    """Rewrite a ``localhost`` host to ``127.0.0.1``.

    On Windows ``localhost`` resolves to ``::1`` first. A server bound to
    127.0.0.1 only (the usual uvicorn --host 127.0.0.1) never answers on
    IPv6, so every request first waits out the IPv6 connect timeout:
    measured 2040 ms per control POST against 2-5 ms for the same POST to
    127.0.0.1. That single stall was the bulk of the live input latency.
    """
    return url.replace("//localhost:", "//127.0.0.1:", 1) \
        if url.startswith(("http://localhost:", "https://localhost:")) else url


def read_ndjson(fobj, out_queue, stop_event):
    """Pump NDJSON lines from a file-like object into *out_queue*.

    Shared by the HTTP reader thread and file replay (tests, offline debug):
    anything with ``readline()`` works. Ends -- pushing :class:`EndOfStream`
    -- on EOF, on an ``{"event": "end"}`` line, on *stop_event*, or on a
    parse/socket error.
    """
    try:
        while not stop_event.is_set():
            line = fobj.readline()
            if not line:
                out_queue.put(EndOfStream())
                return
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "event" in msg:
                out_queue.put(EndOfStream(error=msg.get("error")))
                return
            out_queue.put(msg)
        out_queue.put(EndOfStream())
    except Exception as exc:
        if stop_event.is_set():
            # forced close() from stop() surfaces as a socket error -- clean
            out_queue.put(EndOfStream())
        else:
            out_queue.put(EndOfStream(error=f"{type(exc).__name__}: {exc}"))


class StreamClient:
    """One live session against the MMCP server's stream extension.

    Lifecycle::

        c = StreamClient("http://localhost:8000")
        hs = c.start(seed=42)          # handshake: fps / horizon / skeleton
        c.open_frames()                # reader thread -> c.packets
        c.send_control((0, 1), 1.4)    # any time, any rate (coalesced)
        ...
        c.stop()                       # server stop + threads joined
    """

    def __init__(self, server_url, access_token=None):
        self.server_url = _prefer_ipv4_loopback(server_url.rstrip("/"))
        self.access_token = access_token
        self.handshake = None
        self.session_id = None
        self.packets = queue.Queue()

        self._stop_evt = threading.Event()
        self._reader = None
        self._response = None

        # Control state: send_control() stores the latest desired state;
        # flush_control() POSTs it from the CALLER's thread. No background
        # sender: inside MotionBuilder, background Python threads are
        # GIL-starved by the host for SECONDS at a time (measured: the
        # session-start control arrived 8.6 s late, a keypress 2.4 s late,
        # while the same POST takes 3-14 ms when issued directly), so
        # control must ride the UI tick, which demonstrably runs at 30 Hz.
        self._pending_control = None
        self._sent_control = None
        self._flush_blocked_until = 0.0

    # -- HTTP helpers ----------------------------------------------------

    def _headers(self, body=False):
        h = {}
        if body:
            h["Content-Type"] = "application/json"
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _post(self, path, body, timeout=_HTTP_TIMEOUT):
        req = urllib.request.Request(
            self.server_url + path, data=json.dumps(body).encode("utf-8"),
            headers=self._headers(body=True), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}")
                message = detail.get("error", {}).get("message", str(exc))
            except Exception:
                message = str(exc)
            raise StreamClientError(f"POST {path}: {message}") from exc
        except Exception as exc:
            raise StreamClientError(f"POST {path} failed: {exc}") from exc

    # -- probe / handshake ----------------------------------------------

    def info(self, timeout=5.0):
        """``GET /stream/ardy/info`` -> dict, or None when unavailable.

        Never raises: the GUI uses this to decide whether to show the Live
        section at all.
        """
        req = urllib.request.Request(
            self.server_url + "/stream/ardy/info", headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def start(self, model_variant=None, seed=None, takeover=False,
              prompts=None):
        """``POST /start`` -> handshake dict (fps, horizon_frames, skeleton).

        ``takeover=True`` replaces a session another client left behind
        (crashed client, second MotionBuilder instance) instead of failing.

        ``prompts`` declares every text the session may switch to later
        (the blocks on the tool's timeline). The server encodes them once
        here -- a text encoder running on CPU needs ~2 s per text, which
        would otherwise stall generation the moment the playhead crosses
        into a new block.
        """
        body = {}
        if model_variant:
            body["model_variant"] = model_variant
        if seed is not None:
            body["seed"] = seed
        if takeover:
            body["takeover"] = True
        if prompts:
            body["prompts"] = list(prompts)
        # first start may lazy-load the model server-side -- generous timeout
        hs = self._post("/stream/ardy/start", body, timeout=120.0)
        self.handshake = hs
        self.session_id = hs["session_id"]
        return hs

    # -- frame stream ----------------------------------------------------

    def open_frames(self):
        """Open ``GET /frames`` and spawn the reader thread."""
        if self.session_id is None:
            raise StreamClientError("start() must succeed before open_frames()")
        url = (self.server_url
               + f"/stream/ardy/frames?session={self.session_id}")
        req = urllib.request.Request(url, headers=self._headers())
        try:
            self._response = urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)
        except Exception as exc:
            raise StreamClientError(f"GET /frames failed: {exc}") from exc
        self._reader = threading.Thread(
            target=read_ndjson,
            args=(self._response, self.packets, self._stop_evt),
            name="ardy-live-reader", daemon=True)
        self._reader.start()

    # -- control ---------------------------------------------------------

    def send_control(self, move_dir, speed, facing=None, flush=False,
                     prompt=None):
        """Record the desired control state; POSTed by flush_control().

        Call as often as the UI likes -- only the newest state is kept and
        duplicate states are never re-sent.

        ``flush=True`` posts it right here instead of waiting for the next
        tick. Use it for direct operator input: waiting cost 1.5 s of
        input latency in a measurement (the preview tick does not always
        run at its nominal rate inside MotionBuilder), while the POST
        itself takes ~10 ms on localhost.
        """
        state = {
            "session_id": self.session_id,
            "move_dir": list(move_dir),
            "speed": float(speed),
        }
        if facing is not None:
            state["facing"] = list(facing)
        if prompt:
            state["prompt"] = prompt
        self._pending_control = state
        if flush:
            self.flush_control(timeout=0.6)

    def flush_control(self, timeout=1.5):
        """POST the pending control state from the calling thread.

        Call once per UI tick. No-op when nothing changed. A failed post
        keeps the state pending and backs off ``CONTROL_RETRY_BACKOFF``
        so a wedged server cannot stall the UI every tick.
        """
        state = self._pending_control
        if state is None or state == self._sent_control:
            return
        if time.monotonic() < self._flush_blocked_until:
            return
        try:
            self._post("/stream/ardy/control", state, timeout=timeout)
            self._sent_control = state
        except StreamClientError:
            # retried next tick (after the backoff); a dead server
            # surfaces through the reader's EndOfStream
            self._flush_blocked_until = (
                time.monotonic() + CONTROL_RETRY_BACKOFF)

    # -- teardown --------------------------------------------------------

    def stop(self):
        """Stop the server session and join the reader thread."""
        self._stop_evt.set()
        if self._response is not None:
            try:
                self._response.close()      # unblocks readline()
            except Exception:
                pass
        if self.session_id is not None:
            try:
                self._post("/stream/ardy/stop",
                           {"session_id": self.session_id}, timeout=10.0)
            except StreamClientError:
                pass                        # server may already be gone
            self.session_id = None
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=3.0)
        self._reader = None


def replay_ndjson_file(path, out_queue=None):
    """Feed an NDJSON capture into a queue -- offline stand-in for a server.

    Returns the queue (creates one when *out_queue* is None). Used by tests
    and for debugging the preview path without a GPU server.
    """
    q = out_queue if out_queue is not None else queue.Queue()
    stop = threading.Event()
    with open(path, "r", encoding="utf-8") as f:
        read_ndjson(f, q, stop)
    return q
