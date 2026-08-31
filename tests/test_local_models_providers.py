"""Local models appear in the picker only while usable, at zero price."""
from __future__ import annotations

import json
import os

from harness.local_model_manager import LocalModelError, LocalModelManager, reset_manager_for_tests
from harness.local_models import canonical_spec, local_secret_reach
from harness import model_visibility as mv
from harness import providers as prov


def test_local_provider_hidden_until_usable(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    assert prov.get_provider("local").name == "local"
    assert prov.get_provider("llama-cpp").name == "local"
    names = [p.name for p in prov.available_providers()]
    assert "local" not in names


def test_usable_local_spec_is_zero_price(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [{
            "id": "qwen-test",
            "name": "Qwen",
            "filename": "m.gguf",
            "url": "http://fixture/m.gguf",
            "revision": "x",
            "sha256": "b" * 64,
            "size": 1,
            "context_length": 1024,
            "min_ram_gb": 0,
            "recommended_ram_gb": 1,
            "min_disk_bytes": 1,
        }],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "context_length": 4096,
        "has_key": False,
        "healthy": True,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    monkeypatch.setattr(mv, "_store_path", lambda: str(tmp_path / "models.json"))
    spec = canonical_spec("ollama-127-0-0-1-11434", "llama3")
    assert spec in mgr.usable_specs()
    rows = mv.catalog(available_only=True)
    local_rows = [row for row in rows if row["provider"] == "local"]
    assert local_rows
    assert local_rows[0]["price_in"] == 0
    assert local_rows[0]["price_out"] == 0
    assert local_rows[0]["spec"] == spec
    restarted = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    resolved = restarted.resolve_spec(spec)
    assert resolved["base_url"] == "http://127.0.0.1:11434/v1"
    assert resolved["secret_reach"].startswith("local-")


def test_unhealthy_external_cannot_resolve_or_build_pilot(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "healthy": False,
    }]
    mgr._save(state)
    spec = canonical_spec("ollama-127-0-0-1-11434", "llama3")
    assert mgr.resolve_spec(spec) is None
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    try:
        prov.build_pilot(spec)
        raise AssertionError("unhealthy external must not build a driver")
    except (prov.ProviderError, LocalModelError):
        pass


def test_remote_key_survives_env_clear_into_driver(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_KEY_ENV", "OPENROUTER_API_KEY")
    reset_manager_for_tests()
    from harness.keys import get_api_key_status, get_env_var_for_reach
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    monkeypatch.setattr(
        "harness.local_model_manager.is_safe_url_pinned",
        lambda url, **k: (True, "", "1.2.3.4"),
    )

    def transport(url, **kwargs):
        return {"payload": {"data": [{"id": "qwen"}]}, "headers": {}, "status": 200}

    mgr = LocalModelManager(
        root=str(tmp_path / "local-models"),
        catalog=catalog,
        probe_transport=transport,
    )
    snap = mgr.save_external(
        "https://api.runpod.ai/v2/abc/openai/v1",
        accept_remote=True,
        model="qwen",
        api_key="sk-remote-secret-xyz",
    )
    row = snap["externals"][0]
    reach = local_secret_reach(row["id"])
    env_var = get_env_var_for_reach(reach)
    assert env_var != "OPENROUTER_API_KEY"
    assert os.environ.get(env_var) == "sk-remote-secret-xyz"
    os.environ.pop(env_var, None)
    assert not os.environ.get(env_var)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    driver = prov.build_pilot("local:%s/qwen" % row["id"])
    assert driver._key() == "sk-remote-secret-xyz"
    blob = json.dumps(mgr.snapshot())
    assert "sk-remote-secret-xyz" not in blob
    status = get_api_key_status(reach)
    assert status["has_key"] is True
    assert "sk-remote-secret-xyz" not in status["masked"]


def test_disconnected_local_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    from harness.keys import mark_disconnected, unmark_disconnected
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "name": "ollama",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["llama3"],
        "selected_model": "llama3",
        "healthy": True,
        "kind": "loopback",
        "requires_key": False,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    try:
        mark_disconnected("local")
        assert prov.get_provider("local").available is False
        try:
            prov.build_pilot("local:ollama-127-0-0-1-11434/llama3")
            raise AssertionError("disconnected local must not build a driver")
        except prov.ProviderError:
            pass
    finally:
        unmark_disconnected("local")


def test_keyless_lan_builds_and_public_requires_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "lan-box",
        "vendor": "ollama",
        "base_url": "http://192.168.1.20:8080/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": False,
        "kind": "lan",
        "requires_key": False,
        "lan_accepted": True,
    }, {
        "id": "runpod-box",
        "vendor": "openai-compatible",
        "base_url": "https://proxy.runpod.net/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": False,
        "kind": "public",
        "requires_key": True,
        "remote_accepted": True,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    lan = prov.build_pilot("local:lan-box/qwen")
    assert lan.allow_keyless is True
    assert lan._key() == "local"
    try:
        prov.build_pilot("local:runpod-box/qwen")
        raise AssertionError("public HTTPS without a key must fail closed")
    except prov.ProviderError as exc:
        assert "requires an API key" in str(exc)


def test_loopback_llama_cpp_builds_keyless(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "llama-loop",
        "vendor": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": False,
        "kind": "loopback",
        "requires_key": False,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    driver = prov.build_pilot("local:llama-loop/qwen")
    assert driver.allow_keyless is True
    assert driver._is_llama_cpp_host() is True
    assert driver._key() == "local"


def test_public_llama_cpp_stale_has_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "llama-public",
        "vendor": "llama.cpp",
        "base_url": "https://proxy.runpod.net/v1",
        "models": ["qwen"],
        "selected_model": "qwen",
        "healthy": True,
        "has_key": True,
        "kind": "public",
        "requires_key": True,
        "remote_accepted": True,
    }]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    from harness.local_models import local_secret_reach
    from harness.keys import get_env_var_for_reach
    monkeypatch.delenv(get_env_var_for_reach(local_secret_reach("llama-public")), raising=False)
    try:
        prov.build_pilot("local:llama-public/qwen")
        raise AssertionError("stale has_key must not open a public llama.cpp endpoint")
    except prov.ProviderError as exc:
        assert "requires an API key" in str(exc)


def test_first_activate_does_not_collapse_empty_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    reset_manager_for_tests()
    monkeypatch.setattr(mv, "_store_path", lambda: str(tmp_path / "models.json"))
    catalog = {
        "version": 1,
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}},
        "models": [],
    }
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog=catalog)
    state = mgr._state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "vendor": "ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "selected_model": "llama3",
        "healthy": True,
    }]
    mgr._save(state)
    assert mv.get_enabled() == []
    mgr.activate("local:ollama-127-0-0-1-11434/llama3")
    assert mv.get_enabled() == []
    mv.set_enabled(["openrouter:foo"])
    mgr.activate("local:ollama-127-0-0-1-11434/llama3")
    enabled = mv.get_enabled()
    assert "openrouter:foo" in enabled
    assert "local:ollama-127-0-0-1-11434/llama3" in enabled
