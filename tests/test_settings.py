"""Tests for settings GET/POST endpoints."""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
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


def _post_and_check_persisted(port, srv, state_dir, key, value, env_var, env_value):
    """POST one setting, then check the response, GET, env var, and env_settings.json."""
    import os

    post_resp = _post(
        port,
        "/api/settings",
        {key: value},
        {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
    )
    assert post_resp.status == 200
    posted = json.loads(post_resp.read().decode())[key]
    assert posted == value and type(posted) is type(value)
    fetched = json.loads(_get(port, "/api/settings").read().decode())[key]
    assert fetched == value and type(fetched) is type(value)
    assert os.environ.get(env_var) == env_value
    env_path = os.path.join(str(state_dir), "env_settings.json")
    assert os.path.exists(env_path)
    with open(env_path, encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted[env_var] == env_value
    return env_path


def test_settings_get_returns_expected_shape(monkeypatch):
    # Live developer shells often pin HARNESS_CODEX_REASONING_EFFORT=max;
    # this shape test asserts the factory default, not the host preference.
    monkeypatch.delenv("HARNESS_CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("HARNESS_SWARM_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("HARNESS_BROWSER_REAL_PROFILE", raising=False)
    monkeypatch.delenv("HARNESS_JEV", raising=False)
    httpd, port, srv = _server()
    try:
        resp = _get(port, "/api/settings")
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        
        # Verify keys
        assert "driver" in data
        assert "reach" in data
        assert "budget" in data
        assert "models" in data
        assert "auto_distill" in data
        assert "hash_edit_enabled" in data
        assert data["jev_enabled"] is False
        assert data["jev_ready"] is False
        assert "wiki_auto" in data
        assert "reasoning_effort" in data
        assert data["reasoning_effort"] == "low"
        assert "swarm_reasoning_effort" in data
        assert data["swarm_reasoning_effort"] == "medium"
        assert "compactionResidual" in data
        assert data["compactionResidual"] == "catalog"
        assert data["maxOutputTokens"] == "unlimited"
        assert data["autoMaxTokens"] == "500000"
        assert data["workerTokenBudget"] == "250000"
        assert "browserRealProfile" in data
        assert data["browserRealProfile"] is False
        assert "state_dir" in data
        assert "repo" in data
    finally:
        httpd.shutdown()


def test_settings_post_rejected_without_token():
    httpd, port, srv = _server()
    try:
        try:
            _post(port, "/api/settings", {"budget": 10},
                  {"Content-Type": "application/json"})
            assert False, "should have been rejected with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_settings_post_updates_settings_successfully():
    httpd, port, srv = _server()
    try:
        # Check initial budget & auto_distill
        resp = _get(port, "/api/settings")
        initial_data = json.loads(resp.read().decode())
        initial_budget = initial_data["budget"]
        initial_auto_distill = initial_data["auto_distill"]
        initial_hash_edit = initial_data["hash_edit_enabled"]

        # Modify values
        target_budget = 7 if initial_budget != 7 else 12
        target_auto_distill = not initial_auto_distill
        target_hash_edit = not initial_hash_edit

        # Post update
        post_resp = _post(port, "/api/settings",
                          {"budget": target_budget, "auto_distill": target_auto_distill,
                           "hash_edit_enabled": target_hash_edit},
                          {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN})
        assert post_resp.status == 200
        post_data = json.loads(post_resp.read().decode())
        
        assert post_data["budget"] == target_budget
        assert post_data["auto_distill"] is target_auto_distill
        assert post_data["hash_edit_enabled"] is target_hash_edit

        # Verify via subsequent GET
        get_resp2 = _get(port, "/api/settings")
        get_data2 = json.loads(get_resp2.read().decode())
        assert get_data2["budget"] == target_budget
        assert get_data2["auto_distill"] is target_auto_distill
        assert get_data2["hash_edit_enabled"] is target_hash_edit

        # Restore hash_edit so later tests in this process see default-off behavior.
        restore_resp = _post(
            port,
            "/api/settings",
            {"hash_edit_enabled": initial_hash_edit},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert restore_resp.status == 200
    finally:
        httpd.shutdown()


def test_settings_jev_opt_in_default_off_and_persists(monkeypatch):
    monkeypatch.delenv("HARNESS_JEV", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    httpd, port, srv = _server()
    try:
        initial = json.loads(_get(port, "/api/settings").read().decode())
        assert initial["jev_enabled"] is False
        assert initial["jev_ready"] is False

        post_resp = _post(
            port,
            "/api/settings",
            {"jev_enabled": True},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert post_resp.status == 200
        post_data = json.loads(post_resp.read().decode())
        assert post_data["jev_enabled"] is True
        assert post_data["jev_ready"] is False

        again = json.loads(_get(port, "/api/settings").read().decode())
        assert again["jev_enabled"] is True
        assert again["jev_ready"] is False

        restore = _post(
            port,
            "/api/settings",
            {"jev_enabled": False},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert restore.status == 200
        assert json.loads(restore.read().decode())["jev_enabled"] is False
    finally:
        httpd.shutdown()


@pytest.mark.parametrize(
    ("key", "env_var", "default", "value", "restore"),
    [
        pytest.param(
            "reasoning_effort", "HARNESS_CODEX_REASONING_EFFORT", None, "xhigh", "low",
            id="reasoning_effort",
        ),
        pytest.param(
            "swarm_reasoning_effort", "HARNESS_SWARM_REASONING_EFFORT", "medium", "low", "medium",
            id="swarm_reasoning_effort",
        ),
    ],
)
def test_settings_post_persists_reasoning_effort(
    tmp_path, monkeypatch, key, env_var, default, value, restore,
):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    if default is not None:
        monkeypatch.delenv(env_var, raising=False)
    httpd, port, srv = _server()
    try:
        if default is not None:
            assert json.loads(_get(port, "/api/settings").read().decode())[key] == default

        _post_and_check_persisted(port, srv, tmp_path, key, value, env_var, value)

        config = json.loads(_get(port, "/api/config").read().decode())
        assert config[key] == value

        restore_resp = _post(
            port,
            "/api/settings",
            {key: restore},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert restore_resp.status == 200
    finally:
        httpd.shutdown()


def test_settings_post_persists_compaction_residual(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    httpd, port, srv = _server()
    try:
        assert json.loads(_get(port, "/api/settings").read().decode())[
            "compactionResidual"
        ] == "catalog"

        _post_and_check_persisted(
            port, srv, tmp_path,
            "compactionResidual", "hybrid", "HARNESS_COMPACTION_RESIDUAL", "hybrid",
        )

        catalog_resp = _post(
            port,
            "/api/settings",
            {"compactionResidual": "catalog"},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert catalog_resp.status == 200
        assert json.loads(catalog_resp.read().decode())["compactionResidual"] == "catalog"

        try:
            _post(
                port,
                "/api/settings",
                {"compactionResidual": "off"},
                {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            )
            assert False, "off must stay env-only"
        except urllib.error.HTTPError as e:
            assert e.code == 400

        restore_resp = _post(
            port,
            "/api/settings",
            {"compactionResidual": "summary"},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert restore_resp.status == 200
        assert json.loads(restore_resp.read().decode())["compactionResidual"] == "summary"
    finally:
        httpd.shutdown()


def test_settings_post_persists_browser_real_profile(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("HARNESS_BROWSER_REAL_PROFILE", raising=False)
    monkeypatch.setattr(
        "harness.browser_real_profile.Path.home",
        classmethod(lambda cls: tmp_path / "home"),
    )
    cleaned = []
    real_cleanup = __import__(
        "harness.browser_real_profile", fromlist=["cleanup_real_profile_snapshots"]
    ).cleanup_real_profile_snapshots

    def _cleanup():
        cleaned.append(True)
        real_cleanup()

    monkeypatch.setattr(
        "harness.browser_real_profile.cleanup_real_profile_snapshots",
        _cleanup,
    )
    httpd, port, srv = _server()
    try:
        assert json.loads(_get(port, "/api/settings").read().decode())[
            "browserRealProfile"
        ] is False

        env_path = _post_and_check_persisted(
            port, srv, tmp_path,
            "browserRealProfile", True, "HARNESS_BROWSER_REAL_PROFILE", "1",
        )

        off_resp = _post(
            port,
            "/api/settings",
            {"browserRealProfile": False},
            {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
        )
        assert off_resp.status == 200
        assert json.loads(off_resp.read().decode())["browserRealProfile"] is False
        assert os.environ.get("HARNESS_BROWSER_REAL_PROFILE") == "0"
        assert cleaned == [True]
        with open(env_path, encoding="utf-8") as f:
            persisted = json.load(f)
        assert persisted["HARNESS_BROWSER_REAL_PROFILE"] == "0"
    finally:
        httpd.shutdown()
