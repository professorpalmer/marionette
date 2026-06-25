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
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
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
