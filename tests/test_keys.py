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


def _startup_mutated_env_vars():
    """Env vars :func:`load_api_keys_on_startup` may set or clear."""
    from harness.keys import BEDROCK_ENV_FIELDS
    from harness.providers import PROVIDERS

    names = set(BEDROCK_ENV_FIELDS)
    names.add("ANTHROPIC_MODEL")
    for provider in PROVIDERS:
        for env_var in provider.env_vars or ():
            names.add(env_var)
    return sorted(names)


@pytest.fixture(autouse=True)
def setup_env(request, monkeypatch):
    prior = {name: os.environ.get(name) for name in _startup_mutated_env_vars()}

    # Register early so restore runs after per-test monkeypatch undo (LIFO).
    def _restore_provider_env():
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    request.addfinalizer(_restore_provider_env)

    for name in prior:
        monkeypatch.delenv(name, raising=False)

    tmp_dir = tempfile.mkdtemp()
    monkeypatch.setenv("HARNESS_STATE_DIR", tmp_dir)
    try:
        from harness.credential_pool import clear_pools_for_tests
        clear_pools_for_tests()
    except Exception:
        pass
    yield tmp_dir
    try:
        from harness.credential_pool import clear_pools_for_tests
        clear_pools_for_tests()
    except Exception:
        pass


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


def test_api_settings_endpoints_with_key(monkeypatch):
    # Keep rebuild on clear off a live openrouter pilot (ambient workspace
    # driver) so clearing the key cannot 500 the settings handler.
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    httpd, port, srv = _server()
    try:
        # GET settings initial check
        resp = _get(port, "/api/settings")
        data = json.loads(resp.read().decode())
        assert data["has_api_key"] is False
        assert data["api_key_masked"] == ""
        
        # POST with no token (403)
        try:
            _post(port, "/api/settings", {"api_key": "sk-or-live-fake1234"},
                  {"Content-Type": "application/json"})
            assert False, "Should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # POST with valid token
        post_resp = _post(port, "/api/settings", {"api_key": "sk-or-live-fake1234"},
                          {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert post_resp.status == 200
        post_data = json.loads(post_resp.read().decode())
        
        assert post_data["has_api_key"] is True
        assert post_data["api_key_masked"] == "....1234"
        assert post_data["masked"] == "....1234"
        assert "sk-or-live-fake1234" not in post_data["api_key_masked"]
        
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
    home = tmp_path / ".pmharness"
    home.mkdir()
    state_dir = home / "state"
    state_dir.mkdir()
    legacy_file = home / "keys.json"
    legacy_file.write_text(
        json.dumps({"openrouter": "sk-or-legacy-1234"}), encoding="utf-8"
    )
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(legacy_file))

    # Writes stay in the state dir; the legacy file is only folded in on read.
    assert K.get_keys_file_path() == str(state_dir / "keys.json")
    assert K.legacy_keys_file_path() == str(legacy_file)
    status = K.get_api_key_status("openrouter")
    assert status["has_key"] is True
    assert status["masked"] == "....1234"


def test_legacy_and_state_keys_merge_per_provider(monkeypatch, tmp_path):
    """Legacy openrouter + state anthropic must both survive; state wins ties."""
    home = tmp_path / ".pmharness"
    home.mkdir()
    state_dir = home / "state"
    state_dir.mkdir()
    legacy_file = home / "keys.json"
    legacy_file.write_text(
        json.dumps({"openrouter": "sk-or-legacy-openrouter-1", "gemini": "legacy-loses"}),
        encoding="utf-8",
    )
    state_file = state_dir / "keys.json"
    state_file.write_text(
        json.dumps({"anthropic": "sk-ant-state-key-2", "gemini": "state-wins-9999"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for ev in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(ev, raising=False)

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(legacy_file))
    monkeypatch.setattr(K, "_DISCONNECTED_FILE", str(home / "disconnected.json"))

    merged = K._read_keys()
    assert merged["openrouter"] == "sk-or-legacy-openrouter-1"
    assert merged["anthropic"] == "sk-ant-state-key-2"
    assert merged["gemini"] == "state-wins-9999"

    assert K.get_api_key_status("openrouter")["has_key"] is True
    assert K.get_api_key_status("anthropic")["has_key"] is True

    K.load_api_keys_on_startup("openrouter")
    assert os.environ.get("OPENROUTER_API_KEY") == "sk-or-legacy-openrouter-1"

    # Legacy-only providers are migrated into the state file; conflicts are not.
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert stored["openrouter"] == "sk-or-legacy-openrouter-1"
    assert stored["anthropic"] == "sk-ant-state-key-2"
    assert stored["gemini"] == "state-wins-9999"
    # Legacy file is left untouched (one-way, non-destructive migration).
    assert json.loads(legacy_file.read_text(encoding="utf-8"))["gemini"] == "legacy-loses"


def test_legacy_merge_then_api_settings_endpoints_order(monkeypatch, tmp_path):
    """Order-sensitive pair: legacy merge must not leak keys into settings GET."""
    home = tmp_path / ".pmharness"
    home.mkdir()
    state_dir = home / "state"
    state_dir.mkdir()
    legacy_file = home / "keys.json"
    legacy_file.write_text(
        json.dumps({"openrouter": "sk-or-legacy-openrouter-1", "gemini": "legacy-loses"}),
        encoding="utf-8",
    )
    state_file = state_dir / "keys.json"
    state_file.write_text(
        json.dumps({"anthropic": "sk-ant-state-key-2", "gemini": "state-wins-9999"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(legacy_file))
    monkeypatch.setattr(K, "_DISCONNECTED_FILE", str(home / "disconnected.json"))

    K.load_api_keys_on_startup("openrouter")
    assert os.environ.get("OPENROUTER_API_KEY") == "sk-or-legacy-openrouter-1"

    # Simulate autouse teardown between the two order-sensitive tests.
    for name in _startup_mutated_env_vars():
        os.environ.pop(name, None)
    fresh_state = tempfile.mkdtemp()
    monkeypatch.setenv("HARNESS_STATE_DIR", fresh_state)

    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    httpd, port, srv = _server()
    try:
        resp = _get(port, "/api/settings")
        data = json.loads(resp.read().decode())
        assert data["has_api_key"] is False
        assert data["api_key_masked"] == ""
    finally:
        httpd.shutdown()


def test_sibling_prefix_home_does_not_read_legacy_keys(monkeypatch, tmp_path):
    """~/.pmharness_shadow must not inherit real ~/.pmharness/keys.json."""
    shadow_home = tmp_path / ".pmharness_shadow"
    shadow_home.mkdir()
    state_dir = shadow_home / "state"
    state_dir.mkdir()
    real_home = tmp_path / ".pmharness"
    real_home.mkdir()
    real_keys = real_home / "keys.json"
    real_keys.write_text(
        json.dumps({"openrouter": "sk-or-real-leak-from-sibling"}), encoding="utf-8"
    )
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(real_keys))

    assert K.legacy_keys_file_path() == ""
    assert K.migrate_legacy_keys_into_state() == []
    assert K._read_keys() == {}


def test_sibling_prefix_home_does_not_read_legacy_disconnected(monkeypatch, tmp_path):
    """~/.pmharness_shadow must not inherit real ~/.pmharness/disconnected.json."""
    shadow_home = tmp_path / ".pmharness_shadow"
    shadow_home.mkdir()
    state_dir = shadow_home / "state"
    state_dir.mkdir()
    real_home = tmp_path / ".pmharness"
    real_home.mkdir()
    real_disconnected = real_home / "disconnected.json"
    real_disconnected.write_text(json.dumps(["openrouter", "anthropic"]), encoding="utf-8")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_DISCONNECTED_FILE", str(real_disconnected))

    assert K._disconnected_file_path() == str(state_dir / "disconnected.json")
    assert K.get_disconnected() == set()


def test_ephemeral_state_dir_has_no_legacy_keys_path(monkeypatch, tmp_path):
    """Temp / test state dirs must never resolve a legacy keys path."""
    state_dir = tmp_path / "ephemeral-state"
    state_dir.mkdir()
    legacy_file = tmp_path / "keys.json"
    legacy_file.write_text(json.dumps({"openrouter": "sk-or-legacy-leak"}))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(legacy_file))

    assert K.legacy_keys_file_path() == ""
    assert K.migrate_legacy_keys_into_state() == []
    assert K._read_keys() == {}


def test_ephemeral_state_dir_ignores_legacy_keys(monkeypatch, tmp_path):
    """Temp / test HARNESS_STATE_DIR must not read or write ~/.pmharness/keys.json."""
    state_dir = tmp_path / "ephemeral-state"
    state_dir.mkdir()
    legacy_file = tmp_path / "keys.json"
    legacy_file.write_text(
        json.dumps({"openrouter": "sk-or-legacy-leak"}), encoding="utf-8"
    )
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_KEYS_FILE", str(legacy_file))

    assert K.get_keys_file_path() == str(state_dir / "keys.json")
    assert K.get_api_key_status("openrouter")["has_key"] is False


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
    """Legacy ~/.pmharness/disconnected.json still applies when state is under
    that home tree and the state-dir file is missing."""
    home = tmp_path / ".pmharness"
    home.mkdir()
    state_dir = home / "state"
    state_dir.mkdir()
    legacy_file = home / "disconnected.json"
    legacy_file.write_text(json.dumps(["openrouter"]))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_DISCONNECTED_FILE", str(legacy_file))

    assert K._disconnected_file_path() == str(legacy_file)
    assert "openrouter" in K.get_disconnected()


def test_ephemeral_state_dir_ignores_legacy_disconnected(monkeypatch, tmp_path):
    """Temp / test HARNESS_STATE_DIR must not inherit ~/.pmharness disconnects."""
    state_dir = tmp_path / "ephemeral-state"
    state_dir.mkdir()
    legacy_file = tmp_path / "disconnected.json"
    legacy_file.write_text(json.dumps(["anthropic", "openrouter"]))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))

    import importlib
    from harness import keys as K
    importlib.reload(K)
    monkeypatch.setattr(K, "_DISCONNECTED_FILE", str(legacy_file))

    assert K._disconnected_file_path() == str(state_dir / "disconnected.json")
    assert K.get_disconnected() == set()


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
        "AWS_SESSION_TOKEN", "AWS_REGION", "BEDROCK_REGION", "AWS_DEFAULT_REGION",
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
        "AWS_BEARER_TOKEN_BEDROCK": "live-bearer-token-for-doctor-1",
        "AWS_REGION": "us-east-1",
    })
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "bearer auth" in out
    assert "us-east-1" in out


def test_placeholder_bedrock_credential_rejected(monkeypatch, tmp_path):
    """doctor-/test-/placeholder tokens must not count as configured Bedrock auth."""
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for ev in (
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(ev, raising=False)

    from harness.keys import (
        is_placeholder_credential,
        set_bedrock_credentials,
        bedrock_credential_token,
        resolve_usable_bedrock_credentials,
        get_bedrock_status,
    )
    from harness.providers import get_provider
    from harness.registry_wizard import get_provider_key as wizard_key

    assert is_placeholder_credential("doctor-bearer-token-1") is True
    assert is_placeholder_credential("test-fake-key") is True
    assert is_placeholder_credential("dummy_secret") is True
    assert is_placeholder_credential("placeholder-key") is True
    assert is_placeholder_credential("EXAMPLE-token") is True
    # Mid-string "placeholder" / unrelated live tokens are not rejected.
    assert is_placeholder_credential("my-placeholder-key") is False
    assert is_placeholder_credential("live-bearer-token-xyz9") is False

    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "doctor-bearer-token-1",
        "AWS_REGION": "us-east-1",
    })
    assert bedrock_credential_token() is None
    assert resolve_usable_bedrock_credentials() is None
    assert get_bedrock_status()["configured"] is False
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") is None

    p = get_provider("bedrock")
    assert p is not None
    assert p.key() is None
    assert wizard_key(p) is None


def test_disconnect_bedrock_scrubs_env_in_process(monkeypatch, tmp_path):
    """Disabling bedrock must scrub AWS_* from os.environ without a restart."""
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for ev in (
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(ev, raising=False)

    from harness.keys import (
        set_bedrock_credentials,
        set_provider_enabled,
        get_disconnected,
        bedrock_credential_token,
    )
    from harness.api.providers import post_providers_key
    from unittest.mock import MagicMock

    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "live-bedrock-bearer-abc1",
    })
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "live-bedrock-bearer-abc1"

    set_provider_enabled("bedrock", False)
    assert "bedrock" in get_disconnected()
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") is None
    assert bedrock_credential_token() is None

    # Re-enable and clear via the HTTP handler path (also scrubs + resyncs).
    set_provider_enabled("bedrock", True)
    set_bedrock_credentials({
        "AWS_BEARER_TOKEN_BEDROCK": "live-bedrock-bearer-abc1",
    })
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") == "live-bedrock-bearer-abc1"

    svc = MagicMock()
    svc.cfg.driver = "stub"
    svc.driver_provider_available.return_value = True
    code, body = post_providers_key(
        {"provider": "bedrock", "action": "disable"}, svc
    )
    assert code == 200
    assert body.get("disconnected") is True
    assert os.environ.get("AWS_BEARER_TOKEN_BEDROCK") is None
    assert "bedrock" in get_disconnected()


def test_persist_env_api_keys_imports_openrouter_once(monkeypatch, tmp_path):
    """Login-shell OpenRouter must land in keys.json for the next cold start."""
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fresh-install-test-key-99")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    from harness.keys import (
        persist_env_api_keys,
        load_api_keys_on_startup,
        get_keys_file_path,
        mark_disconnected,
        get_disconnected,
    )

    imported = persist_env_api_keys()
    assert "openrouter" in imported
    stored = json.loads(open(get_keys_file_path(), encoding="utf-8").read())
    assert stored["openrouter"].endswith("99")

    # Second pass is a no-op (does not overwrite).
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-should-not-overwrite")
    assert persist_env_api_keys() == []
    stored2 = json.loads(open(get_keys_file_path(), encoding="utf-8").read())
    assert stored2["openrouter"].endswith("99")

    # Explicit disconnect wins over shell env on startup load.
    mark_disconnected("openrouter")
    assert "openrouter" in get_disconnected()
    os.environ.pop("OPENROUTER_API_KEY", None)
    # Re-export after disconnect scrub path: load must not resurrect into env
    # when disconnected (scrub_disconnected_env clears it).
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-fresh-install-test-key-99")
    load_api_keys_on_startup("openrouter")
    assert os.environ.get("OPENROUTER_API_KEY") in (None, "")
