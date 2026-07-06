import os
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
import pytest
import tempfile

def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    # GET now requires the auth token (centralized do_GET gate). Default it in.
    h = dict(headers or {})
    if "X-Harness-Token" not in h:
        import harness.server as _srv
        h["X-Harness-Token"] = _srv._TOKEN
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    # Create a temporary directory for tests
    tmp_dir = tempfile.mkdtemp()
    monkeypatch.setenv("HARNESS_STATE_DIR", tmp_dir)
    yield tmp_dir
    # Cleanup env
    if "OPENROUTER_API_KEY" in os.environ:
        del os.environ["OPENROUTER_API_KEY"]


def test_set_and_get_api_key_status():
    from harness.keys import set_api_key, get_api_key_status, get_keys_file_path
    
    fake_key = "sk-or-test-fakekey1234"
    set_api_key("openrouter", fake_key)
    
    # Check env is set
    assert os.environ.get("OPENROUTER_API_KEY") == fake_key
    
    # Check file exists and has permissions 600
    file_path = get_keys_file_path()
    assert os.path.exists(file_path)
    
    if os.name == "posix":
        # POSIX-only: Windows has no rwx permission bits (chmod only toggles
        # the read-only flag), so st_mode reports 0o666 regardless.
        mode = os.stat(file_path).st_mode & 0o777
        assert mode == 0o600
    
    # Check status and that full key is excluded
    status = get_api_key_status("openrouter")
    assert status["has_key"] is True
    assert status["masked"] == "....1234"
    assert fake_key not in status["masked"]
    assert len(status["masked"]) < len(fake_key)


def test_clear_api_key():
    from harness.keys import set_api_key, get_api_key_status, clear_api_key
    
    fake_key = "sk-or-test-fakekey1234"
    set_api_key("openrouter", fake_key)
    
    assert os.environ.get("OPENROUTER_API_KEY") == fake_key
    
    clear_api_key("openrouter")
    
    assert os.environ.get("OPENROUTER_API_KEY") is None
    status = get_api_key_status("openrouter")
    assert status["has_key"] is False
    assert status["masked"] == ""


def test_api_settings_endpoints_with_key():
    httpd, port, srv = _server()
    try:
        # GET settings initial check
        resp = _get(port, "/api/settings")
        data = json.loads(resp.read().decode())
        assert data["has_api_key"] is False
        assert data["api_key_masked"] == ""
        
        # POST with no token (403)
        try:
            _post(port, "/api/settings", {"api_key": "sk-or-test-fake1234"},
                  {"Content-Type": "application/json"})
            assert False, "Should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # POST with valid token
        post_resp = _post(port, "/api/settings", {"api_key": "sk-or-test-fake1234"},
                          {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert post_resp.status == 200
        post_data = json.loads(post_resp.read().decode())
        
        assert post_data["has_api_key"] is True
        assert post_data["api_key_masked"] == "....1234"
        assert post_data["masked"] == "....1234"
        assert "sk-or-test-fake1234" not in post_data["api_key_masked"]
        
        # Verify subsequent GET
        get_resp = _get(port, "/api/settings")
        get_data = json.loads(get_resp.read().decode())
        assert get_data["has_api_key"] is True
        assert get_data["api_key_masked"] == "....1234"
        assert get_data["key_env_var"] == "OPENROUTER_API_KEY"
        
        # Clear via POST
        clear_resp = _post(port, "/api/settings", {"clear_api_key": True},
                           {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert clear_resp.status == 200
        clear_data = json.loads(clear_resp.read().decode())
        assert clear_data["has_api_key"] is False
        assert clear_data["api_key_masked"] == ""
        
    finally:
        httpd.shutdown()


def test_legacy_keys_fallback_when_state_dir_empty(monkeypatch, tmp_path):
    """Upgraded installs with keys only in ~/.pmharness/keys.json stay readable."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy_file = tmp_path / "keys.json"
    legacy_file.write_text(json.dumps({"openrouter": "sk-or-legacy-1234"}))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(legacy_file))

    assert K.get_keys_file_path() == str(legacy_file)
    status = K.get_api_key_status("openrouter")
    assert status["has_key"] is True
    assert status["masked"] == "....1234"


def test_state_dir_keys_preferred_over_legacy(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "keys.json"
    state_file.write_text(json.dumps({"openrouter": "sk-or-state-key"}))
    legacy_file = tmp_path / "keys.json"
    legacy_file.write_text(json.dumps({"openrouter": "sk-or-legacy-key"}))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(legacy_file))

    assert K.get_keys_file_path() == str(state_file)
    status = K.get_api_key_status("openrouter")
    assert status["has_key"] is True
    assert status["masked"] == "....-key"


def test_legacy_disconnected_fallback(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy_file = tmp_path / "disconnected.json"
    legacy_file.write_text(json.dumps(["openrouter"]))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_DISCONNECTED_FILE", str(legacy_file))

    assert K._disconnected_file_path() == str(legacy_file)
    assert "openrouter" in K.get_disconnected()
