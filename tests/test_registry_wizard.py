"""Tests for Registry & Provider wizard backend endpoints."""
import os
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def test_server(tmp_path, monkeypatch):
    # Set environment variables so tests do not touch real paths
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(tmp_path / "models.json"))
    
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd, port, srv
    httpd.shutdown()


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=5)


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=5)


def test_get_providers_requires_token(test_server):
    httpd, port, srv = test_server
    # Without token -> 403
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/api/providers")
    assert exc.value.code == 403

    # With bad token -> 403
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/api/providers", {"X-Harness-Token": "bad"})
    assert exc.value.code == 403

    # With header token -> 200
    resp = _get(port, "/api/providers", {"X-Harness-Token": srv._TOKEN})
    assert resp.status == 200
    data = json.loads(resp.read().decode())
    assert isinstance(data, list)
    assert len(data) > 0
    # verify shape
    p0 = data[0]
    assert "name" in p0
    assert "env_var" in p0
    assert "base_url" in p0
    assert "has_key" in p0
    assert "api_mode" in p0


def test_providers_probe_no_key_static_fallback(test_server):
    httpd, port, srv = test_server
    # Unauthenticated -> 403
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(port, "/api/providers/probe", {"provider": "openrouter"}, {"Content-Type": "application/json"})
    assert exc.value.code == 403

    # Authenticated, no key set -> returns 200 with source: static and clean error
    resp = _post(port, "/api/providers/probe", {"provider": "openrouter"},
                 {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
    assert resp.status == 200
    data = json.loads(resp.read().decode())
    assert data["provider"] == "openrouter"
    assert data["source"] == "static"
    assert "error" in data
    assert isinstance(data["models"], list)
    assert len(data["models"]) > 0


def test_registry_get_set_roundtrip(test_server, tmp_path):
    httpd, port, srv = test_server
    
    # 1. GET empty registry -> should return {"models": []}
    resp = _get(port, "/api/registry", {"X-Harness-Token": srv._TOKEN})
    assert resp.status == 200
    data = json.loads(resp.read().decode())
    assert data == {"models": []}

    # 2. POST invalid registry (e.g. missing adapter or bad score) -> 400
    bad_model = {"id": "test/model", "adapter": 123, "capability_score": 90}
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(port, "/api/registry", {"models": [bad_model]},
              {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
    assert exc.value.code == 400

    # 3. POST valid models list -> 200
    good_models = [
        {"id": "test/model-1", "adapter": "test-adapter", "capability_score": 85, "tags": ["test"], "notes": "hello"},
        {"id": "test/model-2", "adapter": "test-adapter", "capability_score": 150, "tags": ["test2"]} # 150 will be clamped to 100
    ]
    post_resp = _post(port, "/api/registry", {"models": good_models},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
    assert post_resp.status == 200
    post_data = json.loads(post_resp.read().decode())
    assert post_data["ok"] is True
    assert post_data["models"][0]["id"] == "test/model-1"
    assert post_data["models"][1]["capability_score"] == 100 # clamped!

    # 4. GET again -> should return the updated models list
    get_resp = _get(port, "/api/registry", {"X-Harness-Token": srv._TOKEN})
    assert get_resp.status == 200
    get_data = json.loads(get_resp.read().decode())
    assert len(get_data["models"]) == 2
    assert get_data["models"][0]["id"] == "test/model-1"
    assert get_data["models"][1]["capability_score"] == 100


def test_roles_get_set_roundtrip(test_server):
    httpd, port, srv = test_server

    # 1. GET default roles
    resp = _get(port, "/api/roles", {"X-Harness-Token": srv._TOKEN})
    assert resp.status == 200
    data = json.loads(resp.read().decode())
    assert "roles" in data
    assert "policies" in data
    assert "routing_policy" in data
    assert data["routing_policy"] == "balanced"
    # verify standard role score exists
    assert data["roles"]["explore"] == 50

    # 2. POST role overrides and policy -> 200
    overrides = {"explore": 42, "implement": 99}
    post_resp = _post(port, "/api/roles", {"overrides": overrides, "routing_policy": "cheap"},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
    assert post_resp.status == 200
    post_data = json.loads(post_resp.read().decode())
    assert post_data["ok"] is True
    assert post_data["overrides"]["explore"] == 42
    assert post_data["routing_policy"] == "cheap"

    # 3. GET again and verify they are loaded
    resp2 = _get(port, "/api/roles", {"X-Harness-Token": srv._TOKEN})
    assert resp2.status == 200
    data2 = json.loads(resp2.read().decode())
    assert data2["routing_policy"] == "cheap"
    assert data2["roles"]["explore"] == 42
    assert data2["roles"]["implement"] == 99


def test_pilot_validate_endpoint(test_server):
    httpd, port, srv = test_server

    # 1. Reject bogus id
    resp1 = _post(port, "/api/pilot/validate", {"driver": "bogus-provider:bogus-model"},
                  {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
    assert resp1.status == 200
    data1 = json.loads(resp1.read().decode())
    assert data1["valid"] is False
    assert data1["resolved_model_id"] is None

    # 2. Accept known catalog short-name (e.g. "glm-5.2")
    resp2 = _post(port, "/api/pilot/validate", {"driver": "glm-5.2"},
                  {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
    assert resp2.status == 200
    data2 = json.loads(resp2.read().decode())
    assert data2["valid"] is True
    assert data2["resolved_model_id"] == "z-ai/glm-5.2"
    assert data2["provider"] == "openrouter"


def test_registry_recommend_endpoint(test_server):
    httpd, port, srv = test_server

    resp = _get(port, "/api/registry/recommend", {"X-Harness-Token": srv._TOKEN})
    assert resp.status == 200
    data = json.loads(resp.read().decode())
    assert "pilot" in data
    assert "pilot_driver" in data
    assert data["pilot"] == data["pilot_driver"]
    assert data["pilot_driver"] in {"qwen3-coder-30b", "glm-4.7-flash"}
    assert "roles" in data
    assert isinstance(data["roles"], dict)
    assert "explore" in data["roles"]
    assert "architect" in data["roles"]
