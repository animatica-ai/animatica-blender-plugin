"""Transport contract of ``animatica_blender.mmcp_client`` over the vendored
motionmcp SDK client (``animatica_blender.vendor.motionmcp.client.http``).

Every request goes through the addon's gated opener (``mmcp_client.urlopen``),
installed into the SDK module; the SDK owns the wire format (Accept, GLB,
202 polling, error envelope) and ``mmcp_client`` maps its errors onto
``MmcpError`` and owns the cloud session (Bearer, refresh, expiry).

Each test checks one observable behaviour against a real socket on 127.0.0.1
(``fake_server``), or against an address nothing listens on.
"""

from __future__ import annotations

import base64
import importlib
import json

import pytest

from animatica_blender import mmcp_client
from fake_server import GLTF_DOC

_http = importlib.import_module("animatica_blender.vendor.motionmcp.client.http")

REQUEST = {"protocol_version": "1.0", "model": "fake-model"}

#: Loopback to TCP, but not to ``is_loopback`` -- and nothing listens there.
NOT_LOOPBACK_URL = "http://127.0.0.2:1"


@pytest.fixture(autouse=True)
def _signed_out(monkeypatch):
    """No stored session unless a test says so (the real getters need bpy.context)."""
    monkeypatch.setattr(mmcp_client, "get_access_token", lambda: "")
    monkeypatch.setattr(mmcp_client, "get_refresh_token", lambda: "")


def _client(url, timeout=5):
    return mmcp_client.MmcpClient(url, timeout=timeout)


def _posts(server):
    return [r for r in server.requests if r.method == "POST"]


def test_gate_installed_in_sdk_module():
    assert _http.urllib.request.urlopen is mmcp_client.urlopen


def test_generate_json_carries_client_headers_and_capabilities_does_not(fake_server):
    fake_server.enqueue_json(200, {"protocol_version": "1.0", "models": []})
    fake_server.enqueue_gltf()
    client = _client(fake_server.url)

    client.capabilities(refresh=True)
    doc = client.generate(REQUEST)

    assert doc == GLTF_DOC
    [get] = [r for r in fake_server.requests if r.method == "GET"]
    [post] = _posts(fake_server)
    assert get.path == "/capabilities"
    assert post.path == "/generate"
    expected = mmcp_client.client_headers()
    assert expected["X-Animatica-Client"] == "blender"
    for name, value in expected.items():
        assert post.header(name) == value, name
        assert get.header(name) is None, name


def test_generate_glb_becomes_gltf_document(fake_server):
    bin_chunk = b"\x01\x02\x03\x04\x05"
    doc = {**GLTF_DOC, "buffers": [{"byteLength": len(bin_chunk)}]}
    fake_server.enqueue_glb(doc, bin_chunk)

    out = _client(fake_server.url).generate(REQUEST)

    uri = out["buffers"][0]["uri"]
    prefix = "data:application/octet-stream;base64,"
    assert uri.startswith(prefix)
    assert base64.b64decode(uri[len(prefix):]) == bin_chunk
    assert out["extensions"]["MMCP_motion"] == GLTF_DOC["extensions"]["MMCP_motion"]


def test_offline_gate_and_unreachable_mapping(fake_server, bpy_app, monkeypatch):
    # (a) online access off, not loopback: refused before any socket -> offline
    monkeypatch.setattr(bpy_app, "online_access", False)
    with pytest.raises(mmcp_client.MmcpError) as exc_a:
        _client(NOT_LOOPBACK_URL, timeout=0.5).generate(REQUEST)
    assert exc_a.value.code == "offline"

    # (b) same URL, online access on: nothing listens -> model_unavailable
    monkeypatch.setattr(bpy_app, "online_access", True)
    with pytest.raises(mmcp_client.MmcpError) as exc_b:
        _client(NOT_LOOPBACK_URL, timeout=0.5).generate(REQUEST)
    assert exc_b.value.code == "model_unavailable"

    # (c) online access off, but 127.0.0.1 is this machine -> allowed
    monkeypatch.setattr(bpy_app, "online_access", False)
    fake_server.enqueue_gltf()
    assert _client(fake_server.url).generate(REQUEST) == GLTF_DOC


def test_cloud_401_refreshes_once_then_expires(fake_server, monkeypatch):
    monkeypatch.setattr(mmcp_client, "is_cloud_url", lambda url: True)
    token = {"access": "old-token"}
    monkeypatch.setattr(mmcp_client, "get_access_token", lambda: token["access"])
    monkeypatch.setattr(mmcp_client, "get_refresh_token", lambda: "refresh-token")
    refreshes, expiries = [], []
    refresh_ok = {"value": True}

    def fake_refresh():
        refreshes.append(1)
        if refresh_ok["value"]:
            token["access"] = "new-token"
        return refresh_ok["value"]

    monkeypatch.setattr(mmcp_client, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(mmcp_client, "expire_session",
                        lambda *a, **k: expiries.append((a, k)))

    # refresh succeeds: exactly one refresh, the retry carries the new Bearer
    fake_server.enqueue_error(401, "unauthorized", "token expired")
    fake_server.enqueue_gltf()
    assert _client(fake_server.url).generate(REQUEST) == GLTF_DOC
    assert len(refreshes) == 1
    first, second = _posts(fake_server)
    assert first.header("Authorization") == "Bearer old-token"
    assert second.header("Authorization") == "Bearer new-token"
    assert expiries == []

    # refresh fails: the session is expired and the 401 surfaces as auth_required
    refreshes.clear()
    refresh_ok["value"] = False
    token["access"] = "old-token"
    fake_server.enqueue_error(401, "unauthorized", "token expired")
    with pytest.raises(mmcp_client.MmcpError) as exc:
        _client(fake_server.url).generate(REQUEST)
    assert exc.value.code == "auth_required"
    assert len(refreshes) == 1
    assert len(expiries) == 1


def test_429_quota_exceeded_carries_status_and_retry_after(fake_server):
    fake_server.enqueue_error(429, "quota_exceeded", "monthly quota used up",
                              headers={"Retry-After": "30"})

    with pytest.raises(mmcp_client.MmcpError) as exc:
        _client(fake_server.url).generate(REQUEST)

    assert exc.value.code == "quota_exceeded"
    assert exc.value.status == 429
    assert isinstance(exc.value.details.get("retry_after"), (int, float))
    assert exc.value.details["retry_after"] == 30


def test_generate_batch_returns_documents_and_errors_in_order(fake_server):
    fake_server.enqueue_json(200, {"results": [
        {"gltf": GLTF_DOC},
        {"error": {"code": "constraint_conflict", "message": "keys overlap"}},
    ]})

    out = _client(fake_server.url).generate_batch([REQUEST, REQUEST])

    [post] = _posts(fake_server)
    sent = json.loads(post.body)
    assert sent["protocol_version"] == "1.0" and len(sent["requests"]) == 2
    assert len(out) == 2
    assert out[0] == GLTF_DOC
    assert isinstance(out[1], mmcp_client.MmcpError)
    assert out[1].code == "constraint_conflict"


def test_async_202_is_polled_through_the_gate(fake_server, monkeypatch):
    fake_server.enqueue(202, {"Location": "/jobs/abc", "Retry-After": "0"})
    fake_server.enqueue_gltf()
    passes = []
    gate = _http.urllib.request.urlopen
    assert gate is mmcp_client.urlopen  # the addon's gate, not stdlib

    def counting_urlopen(req, *args, **kwargs):
        passes.append((req.get_method(), req.full_url))
        return gate(req, *args, **kwargs)

    monkeypatch.setattr(_http.urllib.request, "urlopen", counting_urlopen)

    assert _client(fake_server.url).generate(REQUEST) == GLTF_DOC
    assert [method for method, _ in passes] == ["POST", "GET"]
    assert [(r.method, r.path) for r in fake_server.requests] == [
        ("POST", "/generate"), ("GET", "/jobs/abc")]


def test_bearer_only_for_cloud(fake_server, monkeypatch):
    monkeypatch.setattr(mmcp_client, "is_cloud_url", lambda url: False)
    monkeypatch.setattr(mmcp_client, "get_access_token", lambda: "stored-token")
    fake_server.enqueue_gltf()

    assert _client(fake_server.url).generate(REQUEST) == GLTF_DOC

    [post] = _posts(fake_server)
    assert post.header("Authorization") is None


def test_cloud_401_while_polling_resumes_the_job(fake_server, monkeypatch):
    """A token that expires mid-job must not cost a second generation: after
    the refresh the client polls the same Location, and never POSTs again."""
    monkeypatch.setattr(mmcp_client, "is_cloud_url", lambda url: True)
    token = {"access": "old-token"}
    monkeypatch.setattr(mmcp_client, "get_access_token", lambda: token["access"])
    monkeypatch.setattr(mmcp_client, "get_refresh_token", lambda: "refresh-token")
    refreshes, expiries = [], []

    def fake_refresh():
        refreshes.append(1)
        token["access"] = "new-token"
        return True

    monkeypatch.setattr(mmcp_client, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(mmcp_client, "expire_session",
                        lambda *a, **k: expiries.append((a, k)))

    fake_server.enqueue(202, {"Location": "/jobs/abc", "Retry-After": "0"})
    fake_server.enqueue_error(401, "unauthorized", "token expired")
    fake_server.enqueue_gltf()

    assert _client(fake_server.url).generate(REQUEST) == GLTF_DOC
    assert len(refreshes) == 1 and expiries == []
    wire = [(r.method, r.path, r.header("Authorization")) for r in fake_server.requests]
    assert wire == [
        ("POST", "/generate", "Bearer old-token"),
        ("GET", "/jobs/abc", "Bearer old-token"),
        ("GET", "/jobs/abc", "Bearer new-token"),
    ]
