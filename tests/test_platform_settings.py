"""Tests for platform adapters lock GET/POST settings and first-run defaults."""
import json
import os
import tempfile
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture
def temp_platform_json():
    # Create a temporary platform.json path
    tmp_dir = tempfile.mkdtemp()
    json_path = os.path.join(tmp_dir, "platform.json")
    os.environ["TEST_PLATFORM_JSON_PATH"] = json_path
    yield json_path
    # Cleanup
    if os.path.exists(json_path):
        os.remove(json_path)
    os.rmdir(tmp_dir)
    if "TEST_PLATFORM_JSON_PATH" in os.environ:
        del os.environ["TEST_PLATFORM_JSON_PATH"]


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def test_first_run_sensible_default(temp_platform_json):
    # platform.json does not exist. Call _init_platform_lock() to verify first-run default
    import harness.server as srv
    srv._init_platform_lock()

    assert os.path.exists(temp_platform_json)
    with open(temp_platform_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data.get("harness_initialized") is True
    # Standalone default: only the built-in 'agentic' adapter is enabled out of
    # the box (it runs directly on the user's provider key with no external CLI).
    # Every CLI adapter -- including hermes -- is disabled so a fresh install is
    # fully self-contained.
    assert "agentic" not in data["disabled"]
    assert "hermes" in data["disabled"]
    assert "cursor" in data["disabled"]
    assert "claude-code" in data["disabled"]
    assert "codex" in data["disabled"]
    assert "openai" in data["disabled"]


def test_get_platform_adapters(temp_platform_json):
    # Set up mock platform.json with specific disabled state
    mock_data = {
        "disabled": ["cursor", "openai"],
        "other_key": "preserve_me",
        "harness_initialized": True
    }
    with open(temp_platform_json, "w", encoding="utf-8") as f:
        json.dump(mock_data, f)

    httpd, port, srv = _server()
    try:
        resp = _get(port, "/api/platform")
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        
        adapters = data["adapters"]
        assert len(adapters) == 6

        # The standalone 'agentic' adapter is present and implement-capable.
        agentic = next(a for a in adapters if a["name"] == "agentic")
        assert agentic["enabled"] is True
        assert agentic["implement_capable"] is True

        # Verify disabled/enabled state from file
        cursor = next(a for a in adapters if a["name"] == "cursor")
        assert cursor["enabled"] is False
        assert cursor["implement_capable"] is True

        hermes = next(a for a in adapters if a["name"] == "hermes")
        assert hermes["enabled"] is True
        assert hermes["implement_capable"] is True

        openai = next(a for a in adapters if a["name"] == "openai")
        assert openai["enabled"] is False
        assert openai["implement_capable"] is False
    finally:
        httpd.shutdown()


def test_post_platform_adapters_updates_lock(temp_platform_json):
    # Prepare initial platform.json
    mock_data = {
        "disabled": ["claude-code", "openai"],
        "other_key": "preserve_me",
        "harness_initialized": True
    }
    with open(temp_platform_json, "w", encoding="utf-8") as f:
        json.dump(mock_data, f)

    httpd, port, srv = _server()
    try:
        # Disable "cursor" (enabled initially)
        resp = _post(port, "/api/platform", {"name": "cursor", "enabled": False},
                     {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp.status == 200
        
        # Verify the file is updated correctly and other keys are preserved
        with open(temp_platform_json, "r", encoding="utf-8") as f:
            updated_data = json.load(f)
        
        assert "cursor" in updated_data["disabled"]
        assert "claude-code" in updated_data["disabled"]
        assert "openai" in updated_data["disabled"]
        assert updated_data["other_key"] == "preserve_me"
        assert updated_data["harness_initialized"] is True

        # Enable "claude-code" (disabled initially)
        resp2 = _post(port, "/api/platform", {"name": "claude-code", "enabled": True},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert resp2.status == 200

        with open(temp_platform_json, "r", encoding="utf-8") as f:
            updated_data2 = json.load(f)
        
        assert "claude-code" not in updated_data2["disabled"]
        assert "cursor" in updated_data2["disabled"]
        assert "openai" in updated_data2["disabled"]
        assert updated_data2["other_key"] == "preserve_me"
        assert updated_data2["harness_initialized"] is True
    finally:
        httpd.shutdown()


def test_post_platform_invalid_adapter_or_type(temp_platform_json):
    httpd, port, srv = _server()
    try:
        # Invalid name
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _post(port, "/api/platform", {"name": "invalid-adapter", "enabled": True},
                  {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert exc_info.value.code == 400

        # Invalid enabled type
        with pytest.raises(urllib.error.HTTPError) as exc_info2:
            _post(port, "/api/platform", {"name": "cursor", "enabled": "not-a-bool"},
                  {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert exc_info2.value.code == 400
    finally:
        httpd.shutdown()
