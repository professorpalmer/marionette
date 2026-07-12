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


def test_bedrock_bearer_save_load_and_env_injection(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for ev in (
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN", "AWS_REGION", "BEDROCK_REGION", "BEDROCK_MODEL_ID",
    ):
        monkeypatch.delenv(ev, raising=False)

    from harness.keys import (
        set_bedrock_credentials, get_bedrock_status, clear_bedrock_credentials,
        load_api_keys_on_startup, get_api_key_status, get_keys_file_path,
        BEDROCK_ENV_FIELDS,
    )

    status = set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "bedrock-bearer-token-xyz9",
        "AWS_REGION": "us-west-2",
        "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    })
    assert status["configured"] is True
    assert status["has_key"] is True
    assert status["auth_mode"] == "bearer"
    assert status["masked"] == "....xyz9"
    assert status["region"] == "us-west-2"
    assert status["model_id"].startswith("us.anthropic.")
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "bedrock-bearer-token-xyz9"
    assert os.environ.get("AWS_REGION") == "us-west-2"
    assert os.environ.get("BEDROCK_MODEL_ID") == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    keys_path = get_keys_file_path()
    assert os.path.exists(keys_path)
    stored = json.loads(open(keys_path, encoding="utf-8").read())
    assert isinstance(stored["bedrock"], dict)
    assert stored["bedrock"]["AWS_BEARER_TOKEN_BEDROCK"].endswith("xyz9")

    # Simulate restart: scrub env, then load from keyfile.
    for ev in BEDROCK_ENV_FIELDS:
        os.environ.pop(ev, None)
    load_api_keys_on_startup("openrouter")
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "bedrock-bearer-token-xyz9"
    assert os.environ.get("AWS_REGION") == "us-west-2"
    assert get_api_key_status("bedrock")["has_key"] is True

    cleared = clear_bedrock_credentials()
    assert cleared["configured"] is False
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") is None
    assert get_api_key_status("bedrock")["has_key"] is False


def test_bedrock_access_key_pair_required(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for ev in (
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(ev, raising=False)

    from harness.keys import (
        set_bedrock_credentials, get_bedrock_status, bedrock_auth_present,
        _normalize_bedrock_creds,
    )
    from harness.providers import get_provider

    # Access key alone is not enough.
    set_bedrock_credentials({"AWS_ACCESS_KEY_ID": "AKIATESTACCESSKEY1"})
    assert get_bedrock_status()["configured"] is False
    assert bedrock_auth_present(_normalize_bedrock_creds({
        "AWS_ACCESS_KEY_ID": "AKIATESTACCESSKEY1",
    })) is False

    status = set_bedrock_credentials({
        "AWS_ACCESS_KEY_ID": "AKIATESTACCESSKEY1",
        "AWS_SECRET_ACCESS_KEY": "secretsecretsecret12",
        "AWS_SESSION_TOKEN": "session-token-value",
        "BEDROCK_REGION": "eu-west-1",
    })
    assert status["configured"] is True
    assert status["auth_mode"] == "access_key"
    assert status["has_session_token"] is True
    assert status["region"] == "eu-west-1"
    assert os.environ.get("AWS_ACCESS_KEY_ID") == "AKIATESTACCESSKEY1"
    assert os.environ.get("AWS_SECRET_ACCESS_KEY") == "secretsecretsecret12"
    assert os.environ.get("AWS_SESSION_TOKEN") == "session-token-value"
    assert os.environ.get("BEDROCK_REGION") == "eu-west-1"

    p = get_provider("bedrock")
    assert p is not None
    assert p.available is True
    assert p.key_env() == "AWS_ACCESS_KEY_ID"


def test_bedrock_set_api_key_bearer_shortcut(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    from harness.keys import set_api_key, get_api_key_status, clear_api_key

    set_api_key("bedrock", "bearer-via-set-api-key-99")
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "bearer-via-set-api-key-99"
    assert get_api_key_status("bedrock")["has_key"] is True
    assert get_api_key_status("bedrock")["masked"] == "....y-99"

    clear_api_key("bedrock")
    assert get_api_key_status("bedrock")["has_key"] is False


def test_bedrock_api_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for ev in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_REGION"):
        monkeypatch.delenv(ev, raising=False)

    httpd, port, srv = _server()
    try:
        resp = _get(port, "/api/bedrock")
        data = json.loads(resp.read().decode())
        assert data["configured"] is False

        post = _post(port, "/api/bedrock", {
            "AWS_BEARER_TOKEN_BEDROCK": "endpoint-bearer-tok4",
            "AWS_REGION": "us-east-1",
        }, {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert post.status == 200
        body = json.loads(post.read().decode())
        assert body["ok"] is True
        assert body["configured"] is True
        assert body["auth_mode"] == "bearer"
        assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "endpoint-bearer-tok4"

        settings = json.loads(_get(port, "/api/settings").read().decode())
        assert settings["bedrock"]["configured"] is True

        clear = _post(port, "/api/bedrock", {"clear": True},
                      {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        clear_body = json.loads(clear.read().decode())
        assert clear_body["configured"] is False
    finally:
        httpd.shutdown()


def test_doctor_reports_bedrock(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    from harness import cli
    from harness.keys import set_bedrock_credentials

    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "bedrock" in out
    assert "not configured" in out

    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "doctor-bearer-token-1",
        "AWS_REGION": "us-east-1",
    })
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "bearer auth" in out
    assert "us-east-1" in out
