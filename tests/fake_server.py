# SPDX-License-Identifier: Apache-2.0
"""A fake MMCP server on a real socket, for wire-level tests of ``mmcp_client``.

Adapted from ``tests/fake_server.py`` of the motionmcp repository at tag
v0.8.0 (Apache-2.0): same queue-of-responses + request-log design, plus the
response builders these tests need (glTF JSON, a minimal GLB, the MMCP error
envelope, a batch result).

A stdlib ``ThreadingHTTPServer`` listens on 127.0.0.1, port 0, in a daemon
thread. A test queues the responses it wants, in order (one per request,
whatever the path), and reads back ``requests``: method, path and headers of
every request the client actually put on the wire.

``python tests/fake_server.py`` runs a self-smoke: one scripted 200, fetched
from itself, then exit 0.
"""

from __future__ import annotations

import collections
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: A glTF document small enough to compare whole, carrying the MMCP extension
#: the addon reads motion metadata from.
GLTF_DOC = {
    "asset": {"version": "2.0", "generator": "fake_server"},
    "extensionsUsed": ["MMCP_motion"],
    "extensions": {"MMCP_motion": {"fps": 30.0, "frame_count": 2, "model": "fake-model"}},
}


def error_envelope(code: str, message: str, details: dict | None = None) -> dict:
    """The MMCP error envelope ``{"error": {"code", "message", "details"}}``."""
    err = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    return {"error": err}


def make_glb(doc: dict, bin_chunk: bytes) -> bytes:
    """A minimal valid GLB: 12-byte header, JSON chunk (space-padded to 4),
    BIN chunk (zero-padded to 4). ``doc["buffers"][0]`` must describe the BIN
    chunk and carry no ``uri``."""
    js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    bn = bin_chunk + b"\x00" * (-len(bin_chunk) % 4)
    body = (struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(bn), 0x004E4942) + bn)
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


class RecordedRequest:
    """One request as it arrived: method, path (with query), headers, body."""

    def __init__(self, method, path, headers, body):
        self.method = method
        self.path = path
        self.headers = headers  # dict, keys spelled as they came on the wire
        self.body = body  # bytes, b"" when there was none

    def header(self, name, default=None):
        """Case-insensitive header lookup."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default

    def __repr__(self):
        return f"RecordedRequest({self.method} {self.path})"


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0: one request per connection, so nothing lingers on keep-alive.
    protocol_version = "HTTP/1.0"

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        fake = self.server.fake  # type: ignore[attr-defined]
        status, headers, out = fake._take(
            RecordedRequest(self.command, self.path, dict(self.headers.items()), body))
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() != "content-length":
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Connection", "close")
        self.end_headers()
        if out:
            self.wfile.write(out)

    do_GET = _serve
    do_POST = _serve

    def log_message(self, format, *args):
        pass


class FakeMmcpServer:
    """Queue of canned responses + log of received requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._responses = collections.deque()
        self.requests: list[RecordedRequest] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.fake = self  # type: ignore[attr-defined]
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def enqueue(self, status=200, headers=None, body=b""):
        """Queue one raw response. *body* is bytes or str (UTF-8)."""
        if isinstance(body, str):
            body = body.encode("utf-8")
        with self._lock:
            self._responses.append((status, dict(headers or {}), body))

    def enqueue_json(self, status, obj, headers=None, content_type="application/json"):
        """Queue *obj* as a JSON body with the given Content-Type."""
        self.enqueue(status, {"Content-Type": content_type, **(headers or {})},
                     json.dumps(obj))

    def enqueue_gltf(self, doc=None):
        """Queue a 200 ``model/gltf+json`` (defaults to :data:`GLTF_DOC`)."""
        self.enqueue_json(200, GLTF_DOC if doc is None else doc,
                          content_type="model/gltf+json")

    def enqueue_glb(self, doc, bin_chunk):
        """Queue a 200 ``model/gltf-binary`` built by :func:`make_glb`."""
        self.enqueue(200, {"Content-Type": "model/gltf-binary"}, make_glb(doc, bin_chunk))

    def enqueue_error(self, status, code, message, headers=None):
        """Queue a non-2xx response carrying the MMCP error envelope."""
        self.enqueue_json(status, error_envelope(code, message), headers=headers)

    def _take(self, request):
        with self._lock:
            self.requests.append(request)
            if self._responses:
                return self._responses.popleft()
        msg = (f"fake_server: no response queued for {request.method} {request.path} "
               f"(request #{len(self.requests)})")
        return 500, {"Content-Type": "text/plain; charset=utf-8"}, msg.encode("utf-8")

    def start(self):
        self._thread.start()

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _self_smoke() -> int:
    import urllib.request

    fake = FakeMmcpServer()
    fake.start()
    try:
        fake.enqueue_gltf()
        # No proxy: the smoke must talk to 127.0.0.1 directly.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(fake.url + "/generate", data=b"{}", timeout=5) as resp:
            status = resp.status
            doc = json.loads(resp.read())
    finally:
        fake.stop()
    ok = (status == 200 and doc == GLTF_DOC and len(fake.requests) == 1
          and fake.requests[0].method == "POST" and fake.requests[0].path == "/generate")
    print(f"fake_server self-smoke: {'ok' if ok else 'FAILED'} "
          f"(status={status}, requests={fake.requests})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_self_smoke())
