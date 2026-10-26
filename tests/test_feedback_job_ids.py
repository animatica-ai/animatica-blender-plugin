"""The client notes the job the server made of each request: what a take's
verdict is reported against (see feedback.py).

A small HTTP server on this machine answers as Animatica Cloud does: a take
with ``X-Job-Id``, a ``202`` whose ``Location`` names the job, a batch whose
results carry one ``job_id`` each, and an answer with no job at all (a
self-hosted server).

Runs headless, without a server::

    blender -b --factory-startup --python tests/test_feedback_job_ids.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import addon_utils  # noqa: E402
import bpy  # noqa: E402

addon_utils.enable("animatica_blender", default_set=False)

from animatica_blender import mmcp_client  # noqa: E402

FAILS: list[str] = []
JOB = "3f0c9a52-6c1e-4f43-9a55-0b8a1f2f7a11"
GLTF = {"asset": {"version": "2.0"}, "animations": [{"name": "sample_0"}]}


def check(name, ok, info=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not ok:
        FAILS.append(name)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, headers=()):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        mode = self.path.strip("/").split("/")[0]
        if "requests" in body:
            results = [{"gltf": GLTF, "job_id": f"{JOB[:-1]}{i}"} for i in range(len(body["requests"]))]
            results[-1] = {"error": {"code": "invalid", "message": "no"}, "job_id": "not a job id!"}
            return self._send(200, {"results": results})
        if mode == "async":
            return self._send(202, {"job_id": JOB}, [("Location", f"/async/generate/jobs/{JOB}"),
                                                    ("Retry-After", "0")])
        if mode == "plain":
            return self._send(200, GLTF)
        return self._send(200, GLTF, [("X-Job-Id", JOB)])

    def do_GET(self):
        return self._send(200, GLTF)


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        client = mmcp_client.MmcpClient(f"{base}/cloud")
        client.generate({"protocol_version": "1.0"})
        check("a take: the job from X-Job-Id", client.last_job_id == JOB, client.last_job_id)

        client = mmcp_client.MmcpClient(f"{base}/async")
        client.generate({"protocol_version": "1.0"})
        check("a 202: the job from its Location", client.last_job_id == JOB, client.last_job_id)

        client = mmcp_client.MmcpClient(f"{base}/plain")
        client.generate({"protocol_version": "1.0"})
        check("a server that names no job: none", client.last_job_id == "", client.last_job_id)

        client = mmcp_client.MmcpClient(f"{base}/cloud")
        out = client.generate_batch([{"protocol_version": "1.0"}] * 3)
        check("a batch: each request's job, in order; nothing that is not a job id",
              client.last_job_ids == [f"{JOB[:-1]}0", f"{JOB[:-1]}1", ""]
              and isinstance(out[-1], mmcp_client.MmcpError), client.last_job_ids)
    except Exception:                                    # noqa: BLE001
        traceback.print_exc()
        FAILS.append("error")
    finally:
        server.shutdown()
    print(f"{'OK' if not FAILS else 'FAILED'}: {len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
