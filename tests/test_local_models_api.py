"""HTTP command/event surface for local models."""
from __future__ import annotations

import json
from types import SimpleNamespace

from harness.api.local_models import (
    LocalModelServices,
    get_local_model_events,
    get_local_models,
    post_local_models,
    stream_local_model_events,
)
from harness.local_model_manager import LocalModelManager


def _svc(tmp_path, **kwargs):
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [{
            "id": "qwen-test",
            "name": "Qwen",
            "filename": "m.gguf",
            "url": "http://fixture/m.gguf",
            "revision": "x",
            "sha256": "a" * 64,
            "size": 1,
            "context_length": 1024,
            "min_ram_gb": 0,
            "recommended_ram_gb": 1,
            "min_disk_bytes": 1,
        }],
    }
    mgr = LocalModelManager(root=str(tmp_path / "lm"), catalog=catalog, **kwargs)
    cfg = SimpleNamespace(driver="stub:ok", repo=str(tmp_path))
    rebuilt = {"n": 0}
    return LocalModelServices(
        manager=mgr,
        cfg=cfg,
        rebuild_pilot_and_session=lambda: rebuilt.__setitem__("n", rebuilt["n"] + 1),
        save_workspace_driver=lambda *_a: None,
        resync_driver_after_model_curation=lambda: {},
    ), rebuilt


def test_get_snapshot(tmp_path):
    svc, _ = _svc(tmp_path)
    status, payload = get_local_models(svc)
    assert status == 200
    assert "hardware" in payload
    assert "managed" in payload
    assert payload["externals"] == []
    assert payload["catalog"]["models"][0]["id"] == "qwen-test"
    assert "recommendation" not in payload["hardware"]


def test_post_unknown_command(tmp_path):
    svc, _ = _svc(tmp_path)
    status, payload = post_local_models({"type": "explode"}, svc)
    assert status == 400
    assert "error" in payload


def test_post_probe_and_events(tmp_path):
    def transport(url, **kwargs):
        return {
            "payload": {"data": [{"id": "phi", "context_length": 2048}]},
            "headers": {"Server": "llama.cpp"},
            "status": 200,
        }

    svc, _ = _svc(tmp_path, probe_transport=transport)
    status, payload = post_local_models({
        "type": "probe",
        "url": "http://127.0.0.1:8080/v1",
        "api_key": "secret-key",
    }, svc)
    assert status == 200
    assert payload["models"] == ["phi"]
    assert "secret-key" not in str(payload)

    status, replay = get_local_model_events(svc, "0")
    assert status == 200
    assert replay["ok"] is True
    assert isinstance(replay["events"], list)


def test_activate_rebuilds_pilot(tmp_path):
    svc, rebuilt = _svc(tmp_path)
    state = svc.manager._state()
    state["managed"]["runtime"] = {"status": "ready", "path": "/x"}
    state["managed"]["model"] = {"status": "ready", "id": "qwen-test", "path": "/m"}
    state["managed"]["process"] = {
        "pid": 1, "port": 9, "host": "127.0.0.1", "healthy": True,
        "alias": "marionette-x", "nonce": "x", "exe": "llama-server",
        "model_path": "qwen-test",
    }
    svc.manager._save(state)
    svc.manager.reconcile_process = lambda: svc.manager._state()
    status, payload = post_local_models({
        "type": "activate",
        "spec": "local:managed/qwen-test",
    }, svc)
    assert status == 200
    assert payload["active_spec"] == "local:managed/qwen-test"
    assert rebuilt["n"] == 1
    assert svc.cfg.driver == "local:managed/qwen-test"


def test_post_install_requires_model_id(tmp_path):
    svc, _ = _svc(tmp_path)
    status, payload = post_local_models({"type": "install", "target": "all"}, svc)
    assert status == 400
    assert payload.get("code") == "model_id"
    status, payload = post_local_models({
        "type": "install", "target": "all", "model_id": "missing",
    }, svc)
    assert status == 400
    assert payload.get("code") == "unknown_model"


def test_post_runpod_requires_accept_remote(tmp_path):
    def transport(url, **kwargs):
        return {"payload": {"data": []}, "headers": {}, "status": 200}

    svc, _ = _svc(tmp_path, probe_transport=transport)
    status, payload = post_local_models({
        "type": "save_external",
        "url": "https://abc.proxy.runpod.net/v1",
        "model": "qwen3-4b",
    }, svc)
    assert status == 400
    assert payload.get("code") == "public"


def test_post_verify_tool_calling(tmp_path):
    def transport(url, **kwargs):
        if str(url).endswith("/chat/completions"):
            return {
                "payload": {
                    "choices": [{
                        "message": {
                            "tool_calls": [{
                                "function": {"name": "marionette_capability_probe", "arguments": "{}"},
                            }],
                        },
                    }],
                },
                "headers": {},
                "status": 200,
            }
        return {"payload": {"data": [{"id": "llama3"}]}, "headers": {}, "status": 200}

    svc, _ = _svc(tmp_path, probe_transport=transport)
    state = svc.manager._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "healthy": True,
        "kind": "loopback",
        "requires_key": False,
    }]
    svc.manager._save(state)
    status, payload = post_local_models({
        "type": "verify_tool_calling",
        "spec": "local:ollama-127-0-0-1-11434/llama3",
    }, svc)
    assert status == 200
    row = payload["externals"][0]
    assert row["tool_calling"]["status"] == "verified"
    assert row["healthy"] is True


def test_post_verify_public_without_key_is_error(tmp_path):
    called = []

    def transport(url, **kwargs):
        called.append(url)
        return {"payload": {}, "headers": {}, "status": 200}

    svc, _ = _svc(tmp_path, probe_transport=transport)
    state = svc.manager._state()
    state["externals"] = [{
        "id": "runpod-box",
        "vendor": "openai-compatible",
        "base_url": "https://proxy.runpod.net/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "kind": "public",
        "requires_key": True,
        "remote_accepted": True,
        "has_key": False,
    }]
    svc.manager._save(state)
    status, payload = post_local_models({
        "type": "verify_tool_calling",
        "spec": "local:runpod-box/qwen",
    }, svc)
    assert status == 200
    assert payload["externals"][0]["tool_calling"]["status"] == "error"
    assert payload["externals"][0]["tool_calling"]["reason"] == (
        "This public endpoint requires an API key."
    )
    assert payload["externals"][0]["healthy"] is True
    assert called == []


def test_post_verify_redacts_secret(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "harness.keys.hydrate_reach_key",
        lambda _reach: True,
    )
    monkeypatch.setenv("LOCAL_RUNPOD_BOX_API_KEY", "sk-secret-value")
    monkeypatch.setattr(
        "harness.keys.get_env_var_for_reach",
        lambda _reach: "LOCAL_RUNPOD_BOX_API_KEY",
    )
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )

    def transport(url, **kwargs):
        assert "sk-secret-value" in (kwargs.get("headers") or {}).get("Authorization", "")
        return {
            "payload": {
                "choices": [{
                    "message": {
                        "tool_calls": [{"function": {"name": "marionette_capability_probe"}}],
                    },
                }],
            },
            "headers": {},
            "status": 200,
        }

    svc, _ = _svc(tmp_path, probe_transport=transport)
    state = svc.manager._state()
    state["externals"] = [{
        "id": "runpod-box",
        "vendor": "openai-compatible",
        "base_url": "https://proxy.runpod.net/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "kind": "public",
        "requires_key": True,
        "remote_accepted": True,
        "has_key": True,
    }]
    svc.manager._save(state)
    status, payload = post_local_models({
        "type": "verify_tool_calling",
        "spec": "local:runpod-box/qwen",
    }, svc)
    assert status == 200
    blob = json.dumps(payload)
    assert "sk-secret-value" not in blob
    assert payload["externals"][0]["tool_calling"]["status"] == "verified"


def test_stream_events_snapshot_then_wait(tmp_path):
    frames = []
    waits = []

    class _WFile:
        def write(self, data):
            frames.append(data)
            if len(frames) > 2:
                raise BrokenPipeError()
            return len(data)

        def flush(self):
            return None

    class _Handler:
        def __init__(self):
            self.wfile = _WFile()
            self.headers = []

        def send_response(self, _code):
            return None

        def send_header(self, *a):
            self.headers.append(a)

        def _cors(self):
            return None

        def end_headers(self):
            return None

    svc, _ = _svc(tmp_path)

    def wait_since(cursor, timeout):
        waits.append(timeout)
        return []

    svc.manager.wait_events_since = wait_since
    try:
        stream_local_model_events(_Handler(), svc, "0")
    except BrokenPipeError:
        pass
    assert frames
    first = json.loads(frames[0].decode("utf-8").split("data: ", 1)[1])
    assert first["kind"] == "snapshot"
    assert "snapshot" in first
    assert waits
    assert all(value >= 2.0 for value in waits)
