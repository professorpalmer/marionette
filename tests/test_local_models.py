"""Hermetic tests for local-model domain: state, catalog, URL policy, specs."""
from __future__ import annotations

import json

import pytest

from harness import local_models as lm


def test_packaged_catalog_pins_official_digests():
    catalog = lm.load_catalog()
    runtime = catalog["runtime"]
    assert runtime["release"] == "b10442"
    assert runtime["binary"] == "llama-server"
    for key in (
        "macos-arm64", "macos-x64", "linux-x64", "linux-arm64",
        "windows-x64", "windows-arm64",
    ):
        asset = runtime["assets"][key]
        assert asset["url"].startswith(
            "https://github.com/ggml-org/llama.cpp/releases/download/b10442/"
        )
        assert len(asset["sha256"]) == 64
        assert asset["size"] > 0
    expected_hashes = {
        "macos-arm64": "10861c8a5405bf3a04889f1216505aafab6c48360446a5b73b9042e7bbe8d5b7",
        "macos-x64": "75b487d1c89048812a1d1c8fc99cfc0b75d1705cd6beddf6503e4edd1909bb71",
        "linux-x64": "a447495bdf503af09a1874ebbb450927171da2c84c68cc4eae27c9789ca37b0e",
        "linux-arm64": "a3f8deaf74111bf508d2e8a071b1964dcfc16e15ecb56aeeb1ace40c238c2a8f",
        "windows-x64": "67a5da01b254be88294bdb477f481b71bb482b838e8d7da013eef8b20a0cfa24",
        "windows-arm64": "5a1843d2995c106bcf7c796d89d3ed5f0a497aa641f0bf796c95ab868e8e43b8",
    }
    for key, digest in expected_hashes.items():
        assert runtime["assets"][key]["sha256"] == digest
    assert runtime["assets"]["macos-arm64"]["backend"] == "metal"
    assert runtime["assets"]["linux-x64"]["backend"] == "cpu"
    assert runtime["assets"]["windows-x64"]["backend"] == "cpu"
    model = catalog["models"][0]
    assert model["id"] == "qwen3-4b"
    assert model["name"] == "Qwen3 4B"
    assert model["filename"] == "Qwen3-4B-Q4_K_M.gguf"
    assert model["revision"] == "bc640142c66e1fdd12af0bd68f40445458f3869b"
    assert model["sha256"] == "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
    assert model["size"] == 2497280256
    assert model["context_length"] == 40960
    assert "Hob-forge" not in model["url"]
    assert model["url"].startswith("https://huggingface.co/Qwen/Qwen3-4B-GGUF/")


def test_detect_platform_key_normalizes_arch():
    assert lm.detect_platform_key("Darwin", "arm64") == "macos-arm64"
    assert lm.detect_platform_key("Linux", "x86_64") == "linux-x64"
    assert lm.detect_platform_key("Windows", "AMD64") == "windows-x64"


def test_runtime_asset_missing_platform():
    catalog = lm.load_catalog()
    assert lm.runtime_asset_for_platform(catalog, "plan9-x64") is None


def test_state_migration_fills_v1(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    raw = {"managed": {"runtime": {"status": "ready", "path": "/bin/llama-server"}}}
    migrated = lm.migrate_state(raw)
    assert migrated["version"] == 1
    assert migrated["managed"]["runtime"]["status"] == "ready"
    assert migrated["managed"]["model"]["status"] == "absent"
    assert migrated["externals"] == []


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    state = lm.empty_state()
    state["active_spec"] = "local:managed/qwen3-4b"
    lm.save_state(state, str(tmp_path / "local-models"))
    loaded = lm.load_state(str(tmp_path / "local-models"))
    assert loaded["active_spec"] == "local:managed/qwen3-4b"


def test_normalize_localhost_to_ipv4_and_v1():
    assert lm.normalize_endpoint_url("http://localhost:11434") == "http://127.0.0.1:11434/v1"
    assert lm.normalize_endpoint_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"


def test_evaluate_url_blocks_metadata_and_public():
    blocked = lm.evaluate_endpoint_url("http://169.254.169.254/latest")
    assert blocked["ok"] is False
    assert blocked["kind"] == "metadata"
    public = lm.evaluate_endpoint_url("https://api.openai.com/v1")
    assert public["ok"] is False
    assert public["kind"] == "public"
    assert public["requires_remote"] is True


def test_evaluate_url_allows_https_public_with_accept_remote():
    denied = lm.evaluate_endpoint_url("https://proxy.runpod.net/v1")
    assert denied["ok"] is False
    accepted = lm.evaluate_endpoint_url(
        "https://proxy.runpod.net/v1", accept_remote=True,
    )
    assert accepted["ok"] is True
    assert accepted["kind"] == "public"
    assert accepted["normalized"].startswith("https://")
    http = lm.evaluate_endpoint_url("http://proxy.runpod.net/v1", accept_remote=True)
    assert http["ok"] is False
    assert http["kind"] == "public"


def test_evaluate_url_requires_explicit_lan():
    lan = lm.evaluate_endpoint_url("http://192.168.1.20:8080/v1")
    assert lan["ok"] is False
    assert lan["requires_lan"] is True
    accepted = lm.evaluate_endpoint_url("http://192.168.1.20:8080/v1", accept_lan=True)
    assert accepted["ok"] is True
    assert accepted["normalized"] == "http://192.168.1.20:8080/v1"
    remote_only = lm.evaluate_endpoint_url(
        "http://192.168.1.20:8080/v1", accept_remote=True,
    )
    assert remote_only["ok"] is False
    assert remote_only["requires_lan"] is True
    loopback = lm.evaluate_endpoint_url("http://127.0.0.1:8080/v1")
    assert loopback["ok"] is True


def test_canonical_spec_roundtrip():
    spec = lm.canonical_spec("managed", "qwen3-4b")
    assert spec == "local:managed/qwen3-4b"
    assert lm.parse_local_spec(spec) == ("managed", "qwen3-4b")
    assert lm.parse_local_spec("anthropic:claude") is None


def test_redact_mapping_strips_keys():
    out = lm.redact_mapping({"api_key": "sk-secret-value", "url": "http://127.0.0.1:8080/v1?token=abc"})
    assert "sk-secret-value" not in json.dumps(out)
    assert out["api_key"].startswith("••••")


def test_parse_command_rejects_unknown():
    with pytest.raises(ValueError):
        lm.parse_command({"type": "explode"})
    cmd = lm.parse_command({"type": "install", "target": "runtime"})
    assert cmd == {"type": "install", "target": "runtime", "model_id": ""}
    install = lm.parse_command({"type": "install", "target": "all", "model_id": "qwen3-4b"})
    assert install["model_id"] == "qwen3-4b"
    probe = lm.parse_command({
        "type": "probe",
        "url": "https://proxy.runpod.net/v1",
        "trust_remote": True,
    })
    assert probe["accept_remote"] is True
    verify = lm.parse_command({
        "type": "verify_tool_calling",
        "spec": "local:ollama-127-0-0-1-11434/llama3",
    })
    assert verify == {
        "type": "verify_tool_calling",
        "spec": "local:ollama-127-0-0-1-11434/llama3",
    }
    with pytest.raises(ValueError):
        lm.parse_command({"type": "verify_tool_calling"})


def test_extract_model_ids_and_context():
    ids = lm.extract_model_ids({"data": [{"id": "qwen"}, "other"]})
    assert ids == ["qwen", "other"]
    ctx = lm.extract_context_length({"data": [{"id": "qwen", "max_model_len": 8192}]})
    assert ctx == 8192


def test_unhealthy_external_is_not_usable():
    state = lm.empty_state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "selected_model": "llama3",
        "base_url": "http://127.0.0.1:11434/v1",
        "healthy": False,
    }]
    assert lm.usable_local_specs(state) == []
    state["externals"][0]["healthy"] = True
    assert lm.usable_local_specs(state) == ["local:ollama-127-0-0-1-11434/llama3"]


def test_snapshot_exposes_catalog_models_without_recommendation():
    catalog = lm.load_catalog()
    snap = lm.snapshot_from_state(lm.empty_state(), catalog=catalog, hardware={
        "os": "Darwin",
        "arch": "arm64",
        "platform_key": "macos-arm64",
        "accelerator": "metal",
        "supported": True,
    })
    assert "recommendation" not in snap["hardware"]
    assert "recommended_ram_gb" not in snap["hardware"]
    assert snap["catalog"]["models"][0]["id"] == "qwen3-4b"
    assert snap["catalog"]["models"][0]["trust"] == "first-party"


def test_detect_hardware_has_no_recommendation():
    hw = lm.detect_hardware(catalog=lm.load_catalog())
    assert "recommendation" not in hw
    assert "recommended_ram_gb" not in hw


def test_detect_vendor_runpod():
    assert lm.detect_vendor_from_url("https://abc.proxy.runpod.net/v1") == "openai-compatible"
    assert lm.normalize_vendor("runpod") == "openai-compatible"


def test_catalog_rejects_missing_hash():
    with pytest.raises(ValueError):
        lm.parse_catalog({"runtime": {"assets": {"macos-arm64": {"url": "x", "sha256": "nope"}}}})


def test_catalog_omits_recommended_ram():
    catalog = lm.load_catalog()
    assert "recommended_ram_gb" not in catalog["models"][0]
    parsed = lm.parse_catalog({
        "models": [{
            "id": "x",
            "name": "X",
            "sha256": "a" * 64,
            "recommended_ram_gb": 8,
            "min_ram_gb": 4,
        }],
    })
    assert "recommended_ram_gb" not in parsed["models"][0]


def test_runpod_paths_get_distinct_ids():
    first = lm.endpoint_id_for_url(
        "https://api.runpod.ai/v2/aaa/openai/v1", "openai-compatible",
    )
    second = lm.endpoint_id_for_url(
        "https://api.runpod.ai/v2/bbb/openai/v1", "openai-compatible",
    )
    assert first != second
    assert first.startswith("openai-compatible-api-runpod-ai-")
    loop = lm.endpoint_id_for_url("http://127.0.0.1:11434/v1", "ollama")
    assert loop == "ollama-127-0-0-1-11434"


def test_resolve_refuses_unhealthy_external():
    state = lm.empty_state()
    state["externals"] = [{
        "id": "ollama-127-0-0-1-11434",
        "selected_model": "llama3",
        "base_url": "http://127.0.0.1:11434/v1",
        "healthy": False,
    }]
    assert lm.resolve_local_endpoint(state, "local:ollama-127-0-0-1-11434/llama3") is None
    state["externals"][0]["healthy"] = True
    resolved = lm.resolve_local_endpoint(state, "local:ollama-127-0-0-1-11434/llama3")
    assert resolved is not None
    assert resolved["base_url"] == "http://127.0.0.1:11434/v1"


def test_resolve_refuses_unhealthy_managed():
    state = lm.empty_state()
    state["managed"]["runtime"]["status"] = "ready"
    state["managed"]["model"]["status"] = "ready"
    state["managed"]["model"]["id"] = "qwen3-4b"
    state["managed"]["process"] = {
        "pid": 9, "port": 8080, "host": "127.0.0.1", "healthy": False,
    }
    assert lm.managed_usable(state) is False
    assert lm.resolve_local_endpoint(state, "local:managed/qwen3-4b") is None
    state["managed"]["process"]["healthy"] = True
    resolved = lm.resolve_local_endpoint(state, "local:managed/qwen3-4b")
    assert resolved is not None
    assert resolved["kind"] == "loopback"
    assert resolved["requires_key"] is False


def test_detect_linux_libc_fail_closed(monkeypatch):
    assert lm.detect_linux_libc(
        confstr=lambda _name: "glibc 2.39",
        maps_text="",
        lib_names=[],
    ) == "glibc"

    def _no_confstr(_name):
        raise ValueError("not glibc")

    assert lm.detect_linux_libc(
        confstr=_no_confstr,
        maps_text="/lib/ld-musl-x86_64.so.1",
        lib_names=[],
    ) == "musl"
    assert lm.detect_linux_libc(
        confstr=_no_confstr,
        maps_text="",
        lib_names=["ld-musl-aarch64.so.1"],
    ) == "musl"
    assert lm.detect_linux_libc(
        confstr=_no_confstr,
        maps_text="",
        lib_names=["libfoo.so"],
    ) == "unknown"


def test_musl_linux_does_not_claim_ubuntu_runtime():
    assert lm.detect_platform_key("Linux", "x86_64", libc="musl") == "linux-x64-musl"
    catalog = lm.load_catalog()
    assert lm.runtime_asset_for_platform(catalog, "linux-x64-musl") is None
    assert lm.runtime_asset_for_platform(catalog, "linux-x64")["backend"] == "cpu"
    assert lm.runtime_asset_for_platform(catalog, "macos-arm64")["backend"] == "metal"


def test_runtime_offload_layers_follow_backend():
    assert lm.runtime_offload_layers({"backend": "cpu"}) == "0"
    assert lm.runtime_offload_layers({"backend": "metal"}) == "99"
    assert lm.runtime_offload_layers({"backend": "cuda"}) == "99"
    assert lm.runtime_offload_layers({}) == "0"


def test_classify_tool_calling_payload_semantics():
    verified, reason = lm.classify_tool_calling_payload({
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": lm.TOOL_CALLING_FUNCTION_NAME,
                        "arguments": "{\"cmd\":\"rm -rf /\"}",
                    },
                }],
            },
        }],
    })
    assert verified == "verified"
    assert "tool call" in reason
    unsupported, _ = lm.classify_tool_calling_payload({
        "choices": [{"message": {"content": "I cannot call tools."}}],
    })
    assert unsupported == "unsupported"
    malformed, _ = lm.classify_tool_calling_payload({"choices": [{"message": {"tool_calls": "nope"}}]})
    assert malformed == "error"
    empty, _ = lm.classify_tool_calling_payload({"id": "cmpl"})
    assert empty == "error"


def test_classify_tool_calling_requires_requested_function():
    other, reason = lm.classify_tool_calling_payload({
        "choices": [{
            "message": {
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "some_other_tool",
                        "arguments": "{\"cmd\":\"rm -rf /\"}",
                    },
                }],
            },
        }],
    })
    assert other == "unsupported"
    assert "different function" in reason
    assert "rm -rf" not in reason
    malformed, _ = lm.classify_tool_calling_payload({
        "choices": [{"message": {"tool_calls": [{"function": {"name": ""}}]}}],
    })
    assert malformed == "error"


def test_normalize_tool_calling_defaults_unverified():
    row = lm._normalize_external({
        "id": "ollama-127-0-0-1-11434",
        "base_url": "http://127.0.0.1:11434/v1",
        "selected_model": "llama3",
        "healthy": True,
    })
    assert row["tool_calling"] == {
        "status": "unverified", "reason": "", "checked_at": None,
    }


def test_tool_calling_request_is_non_streaming():
    body = lm.tool_calling_request_body("llama3")
    assert body["stream"] is False
    assert body["tool_choice"]["function"]["name"] == lm.TOOL_CALLING_FUNCTION_NAME


def test_trusted_lan_is_keyless_public_requires_key():
    lan = lm._normalize_external({
        "id": "lan-box",
        "base_url": "http://192.168.1.20:8080/v1",
        "selected_model": "qwen",
        "healthy": True,
        "kind": "lan",
    })
    assert lan["requires_key"] is False
    public = lm._normalize_external({
        "id": "runpod",
        "base_url": "https://proxy.runpod.net/v1",
        "selected_model": "qwen",
        "healthy": True,
        "kind": "public",
    })
    assert public["requires_key"] is True
