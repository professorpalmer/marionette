"""Hermetic coverage for bench.cache_matrix (no live provider calls)."""
from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from bench.cache_matrix import (
    DEFAULT_STABLE_PREFIX_TOKENS,
    ModelMismatchError,
    arm_blocker,
    assert_model_match,
    build_native_codex_cmd,
    build_protocol,
    build_stable_system_prefix,
    cost_source_for_arm,
    cursor_fair_system,
    env_override,
    main,
    make_cursor_driver,
    models_compatible,
    negative_control_wire_check,
    nullable_cache_from_usage,
    parse_native_codex_jsonl,
    preflight_status,
    render_markdown_summary,
    run_dry,
    run_live_arm,
    run_marionette_driver_arm,
    run_native_codex_arm,
    turn_from_driver_response,
    warmup_excluded_hit_rate,
)


# ---------------------------------------------------------------------------
# Dry-run protocol
# ---------------------------------------------------------------------------


def test_dry_run_builds_deterministic_identical_protocol(tmp_path, monkeypatch):
    """Dry-run builds stable protocol turns and never touches network/subprocess."""
    calls: List[str] = []

    def boom(*_a, **_k):
        calls.append("subprocess")
        raise AssertionError("subprocess must not run in dry-run")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)

    # urllib should also stay quiet if accidentally imported by a driver.
    import urllib.request

    def url_boom(*_a, **_k):
        calls.append("urlopen")
        raise AssertionError("urlopen must not run in dry-run")

    monkeypatch.setattr(urllib.request, "urlopen", url_boom)

    out = tmp_path / "receipt.json"
    rc = main([
        "--dry-run",
        "--turns", "3",
        "--content-tokens", "32",
        "--stable-prefix-tokens", "64",
        "--output", str(out),
    ])
    assert rc == 0
    assert calls == []
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry-run"
    assert payload["schema_version"] == 1
    assert payload["dry_ok"] is True
    assert payload["stable_prefix_tokens"] == 64
    assert payload["protocol_fingerprint"]["stable_prefix_tokens"] == 64
    assert payload["dry_protocol"]["stable_prefix_tokens"] == 64

    proto_a = build_protocol(turns=3, content_tokens=32, stable_prefix_tokens=64)
    proto_b = build_protocol(turns=3, content_tokens=32, stable_prefix_tokens=64)
    assert proto_a["system"] == proto_b["system"]
    assert proto_a["tools"] == proto_b["tools"]
    assert proto_a["user_turns"] == proto_b["user_turns"]
    assert proto_a["stable_prefix_tokens"] == 64
    assert proto_a["system"] == payload["dry_protocol"]["system"]
    assert proto_a["user_turns"] == payload["dry_protocol"]["user_turns"]
    assert "tool_surface_limitation" in payload
    assert payload["claim_hygiene"]["no_plan_vs_api_usd_headline"] is True


def test_stable_system_prefix_deterministic_and_meets_requested_size():
    """Stable prefix is deterministic, labeled, and ~stable_prefix_tokens*4 chars."""
    a = build_stable_system_prefix(stable_prefix_tokens=DEFAULT_STABLE_PREFIX_TOKENS)
    b = build_stable_system_prefix(stable_prefix_tokens=DEFAULT_STABLE_PREFIX_TOKENS)
    assert a == b
    assert "marionette_cache_matrix_stable_v1" in a
    assert "Rules:" in a
    assert "Stable ballast" in a
    # Approximate token→char budget used by the bench (~4 chars/token).
    assert len(a) >= DEFAULT_STABLE_PREFIX_TOKENS * 4
    # Zero tokens keeps the rules/marker header without ballast.
    bare = build_stable_system_prefix(stable_prefix_tokens=0)
    assert "marionette_cache_matrix_stable_v1" in bare
    assert "Stable ballast" not in bare
    assert len(bare) < len(a)


def test_build_protocol_identical_across_builds_with_stable_prefix():
    """Same args → identical system/tools/manifest across arms' shared protocol."""
    proto_a = build_protocol(
        turns=2, content_tokens=64, stable_prefix_tokens=128,
    )
    proto_b = build_protocol(
        turns=2, content_tokens=64, stable_prefix_tokens=128,
    )
    assert proto_a == proto_b
    assert proto_a["stable_prefix_tokens"] == 128
    assert len(proto_a["system"]) >= 128 * 4
    assert proto_a["system"] in proto_a["native_prompts"][0]
    assert proto_a["tool_manifest_text"] in proto_a["native_prompts"][0]
    # Per-user-turn padding is independent of stable-prefix ballast.
    assert "cache ballast" in proto_a["user_turns"][0]
    assert "stable system ballast" in proto_a["system"]
    assert "Stable ballast" in proto_a["system"]


def test_dry_run_default_without_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError()))
    out = tmp_path / "default.json"
    rc = main(["--output", str(out), "--content-tokens", "8", "--turns", "2"])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry-run"
    assert payload["stable_prefix_tokens"] == DEFAULT_STABLE_PREFIX_TOKENS
    assert (
        payload["protocol_fingerprint"]["stable_prefix_tokens"]
        == DEFAULT_STABLE_PREFIX_TOKENS
    )
    assert len(payload["dry_protocol"]["system"]) >= DEFAULT_STABLE_PREFIX_TOKENS * 4


# ---------------------------------------------------------------------------
# Model mismatch
# ---------------------------------------------------------------------------


def test_model_mismatch_fail_closed():
    with pytest.raises(ModelMismatchError):
        assert_model_match("gpt-5.6-luna", "gpt-4o", allow_mismatch=False)
    # Compatible dated variant is allowed.
    assert_model_match("gpt-5.6-luna", "gpt-5.6-luna-2026-03-01", allow_mismatch=False)
    # Override path.
    assert_model_match("gpt-5.6-luna", "gpt-4o", allow_mismatch=True)
    # Unknown served does not fail.
    assert_model_match("gpt-5.6-luna", None, allow_mismatch=False)


def test_model_compat_cursor_display_label_spacing():
    """Cursor-style spaced display labels share the requested slug family."""
    assert models_compatible("gpt-5.6-luna", "GPT-5.6 Luna 272K Medium")
    assert models_compatible("gpt-5.6-luna", "gpt_5.6_luna")
    assert models_compatible(
        "openai/gpt-5.6-luna", "GPT-5.6 Luna 272K Medium",
    )
    assert_model_match(
        "gpt-5.6-luna", "GPT-5.6 Luna 272K Medium", allow_mismatch=False,
    )


def test_model_compat_true_family_mismatch_fail_closed():
    """A genuinely different family (GPT-5.5) still fails closed."""
    assert not models_compatible("gpt-5.6-luna", "GPT-5.5")
    assert not models_compatible("gpt-5.6-luna", "GPT-5.5 Something")
    with pytest.raises(ModelMismatchError):
        assert_model_match("gpt-5.6-luna", "GPT-5.5", allow_mismatch=False)


def test_fake_driver_arm_fail_closed_on_served_mismatch():
    class FakeDriver:
        def chat(self, messages, *, tools=None, system=None, session_id=None):
            return SimpleNamespace(
                text="OK-1",
                tokens_in=100,
                tokens_out=5,
                error=None,
                model="cache-matrix-codex",
                meta={
                    "served_model": "totally-different-model",
                    "raw_usage": {"prompt_tokens": 100, "completion_tokens": 5},
                },
            )

    protocol = build_protocol(turns=1, content_tokens=8)
    with pytest.raises(ModelMismatchError):
        run_marionette_driver_arm(
            arm="codex",
            driver=FakeDriver(),
            protocol=protocol,
            requested_model="gpt-5.6-luna",
            session_id="sess-mismatch",
            allow_mismatch=False,
        )


# ---------------------------------------------------------------------------
# Native Codex JSONL parser + command shape
# ---------------------------------------------------------------------------


def test_native_codex_jsonl_parser_thread_and_cache_aliases():
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thr-abc"}),
        json.dumps({
            "type": "turn.completed",
            "model": "gpt-5.6-luna",
            "usage": {
                "input_tokens": 1200,
                "output_tokens": 12,
                "cached_input_tokens": 900,
                "cache_write_tokens": 40,
            },
        }),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "OK-1"},
        }),
    ]
    parsed = parse_native_codex_jsonl(lines)
    assert parsed["thread_id"] == "thr-abc"
    assert parsed["resume_id"] == "thr-abc"
    assert parsed["served_model"] == "gpt-5.6-luna"
    assert parsed["text"] == "OK-1"
    assert parsed["tokens_in"] == 1200
    assert parsed["tokens_out"] == 12
    assert parsed["cache_read"] == 900
    assert parsed["cache_write"] == 40


def test_native_codex_jsonl_missing_cache_fields_remain_null():
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "thr-1"}),
        json.dumps({
            "type": "event_msg",
            "msg": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 500,
                        "output_tokens": 3,
                    }
                },
            },
        }),
    ]
    parsed = parse_native_codex_jsonl(lines)
    assert parsed["thread_id"] == "thr-1"
    assert parsed["cache_read"] is None
    assert parsed["cache_write"] is None
    # Also via nullable helper directly.
    cr, cw = nullable_cache_from_usage({"input_tokens": 500, "output_tokens": 3})
    assert cr is None and cw is None


def test_native_codex_command_shape_exec_then_resume_no_secrets():
    first = build_native_codex_cmd(
        binary="/usr/bin/codex",
        model="gpt-5.6-luna",
        prompt="hello",
        thread_id=None,
        workspace="/repo",
    )
    assert first[:3] == ["/usr/bin/codex", "exec", "--json"]
    assert "--model" in first and "gpt-5.6-luna" in first
    assert "--sandbox" in first and "read-only" in first
    assert "--cd" in first and "/repo" in first
    assert first[-1] == "hello"
    assert not any("sk-" in p or "KEY=" in p for p in first)

    resume = build_native_codex_cmd(
        binary="/usr/bin/codex",
        model="gpt-5.6-luna",
        prompt="turn2",
        thread_id="thr-abc",
        workspace="/repo",
    )
    assert resume == [
        "/usr/bin/codex",
        "exec",
        "resume",
        "--json",
        "--model",
        "gpt-5.6-luna",
        "thr-abc",
        "turn2",
    ]
    # resume --help does not expose first-run --sandbox / --cd.
    assert "--sandbox" not in resume
    assert "--cd" not in resume
    assert "/repo" not in resume

    # Resume comes after first exec in the arm runner.
    calls: List[List[str]] = []
    call_kwargs: List[Dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        call_kwargs.append(kwargs)
        if len(calls) == 1:
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thr-xyz"}),
                json.dumps({
                    "type": "turn.completed",
                    "model": "gpt-5.6-luna",
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                }),
            ])
        else:
            stdout = json.dumps({
                "type": "turn.completed",
                "model": "gpt-5.6-luna",
                "usage": {"input_tokens": 11, "output_tokens": 1},
            })
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    protocol = build_protocol(turns=2, content_tokens=4)
    result = run_native_codex_arm(
        protocol=protocol,
        requested_model="gpt-5.6-luna",
        binary="codex",
        workspace="/repo",
        runner=fake_run,
    )
    assert len(calls) == 2
    assert calls[0][1:3] == ["exec", "--json"]
    assert "--sandbox" in calls[0] and "read-only" in calls[0]
    assert "--cd" in calls[0]
    assert calls[1][1:4] == ["exec", "resume", "--json"]
    assert "thr-xyz" in calls[1]
    assert "--sandbox" not in calls[1]
    assert "--cd" not in calls[1]
    # Workspace is still the subprocess cwd on resume (not an argv flag).
    assert call_kwargs[1].get("cwd") == "/repo"
    assert result["arm"] == "native_codex_cli"
    assert result["billing"] == "plan"
    # Logged cmd shapes never include secret-looking tokens.
    for shape in result["command_shapes"]:
        assert not any(str(p).startswith("sk-") for p in shape)
    # Prompt bodies are redacted in receipt command shapes.
    for shape in result["command_shapes"]:
        assert shape[-1] == "<prompt>"


def test_native_codex_resume_sends_user_turn_without_stable_prefix():
    """First exec includes stable prefix; resume sends user-only prompts."""
    protocol = build_protocol(turns=2, content_tokens=8)
    assert "native_user_prompts" in protocol
    assert protocol["native_user_prompts"] == protocol["user_turns"]
    stable = "marionette_cache_matrix_stable_v1"
    assert stable in protocol["native_prompts"][0]
    assert stable not in protocol["native_user_prompts"][1]

    prompts_seen: List[str] = []

    def fake_run(cmd, **_kwargs):
        prompts_seen.append(cmd[-1])
        if len(prompts_seen) == 1:
            stdout = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "thr-resume"}),
                json.dumps({
                    "type": "turn.completed",
                    "model": "gpt-5.6-luna",
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                }),
            ])
        else:
            stdout = json.dumps({
                "type": "turn.completed",
                "model": "gpt-5.6-luna",
                "usage": {"input_tokens": 11, "output_tokens": 1},
            })
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    run_native_codex_arm(
        protocol=protocol,
        requested_model="gpt-5.6-luna",
        binary="codex",
        workspace="/repo",
        runner=fake_run,
    )
    assert len(prompts_seen) == 2
    assert stable in prompts_seen[0]
    assert "# Tool manifest" in prompts_seen[0]
    assert prompts_seen[0] == protocol["native_prompts"][0]
    assert stable not in prompts_seen[1]
    assert "# Tool manifest" not in prompts_seen[1]
    assert prompts_seen[1] == protocol["native_user_prompts"][1]
    assert prompts_seen[1] == protocol["user_turns"][1]


def test_codex_arm_blocker_rejects_oauth_store_alone_without_reading(
    tmp_path, monkeypatch,
):
    """auth.json is diagnostic-only; Marionette Codex needs env or pool token."""
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth = codex_home / "auth.json"
    # Marker content must never appear in preflight / blocker strings.
    auth.write_text('{"access_token":"sk-secret-must-not-leak"}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    status = preflight_status(arms=["codex"], load_keys=False)
    assert status["codex_oauth_store"]["present"] is True
    assert status["openai_codex_token"]["oauth_store_present"] is True
    # auth.json alone is not Marionette Codex driver readiness.
    assert status["openai_codex_token"]["present"] is False
    assert status["openai_codex_token"]["env_present"] is False
    assert status["openai_codex_token"]["credential_pool_present"] is False
    assert arm_blocker("codex", status) is not None

    dumped = json.dumps(status)
    assert "sk-secret" not in dumped
    assert "access_token" not in dumped

    # OPENAI_API_KEY alone also does not satisfy the Codex driver seam.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-count")
    status_openai = preflight_status(arms=["codex"], load_keys=False)
    assert status_openai["openai_codex_token"]["present"] is False
    assert arm_blocker("codex", status_openai) is not None
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Without store or env token, Marionette Codex is blocked.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # Point home at empty dir so ~/.codex is not accidentally used.
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(empty))
    # Also override CODEX_HOME to a dir without auth.json.
    bare = tmp_path / "bare-codex"
    bare.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(bare))
    status2 = preflight_status(arms=["codex"], load_keys=False)
    assert status2["codex_oauth_store"]["present"] is False
    assert status2["openai_codex_token"]["present"] is False
    assert arm_blocker("codex", status2) is not None

    # OpenRouter / Cursor checks remain independent of Codex OAuth store.
    assert arm_blocker(
        "openrouter",
        {"openrouter_api_key": {"present": False}, "codex_oauth_store": {"present": True}},
    ) == "missing OPENROUTER_API_KEY"
    assert arm_blocker(
        "cursor",
        {"cursor_agent": {"present": False}, "codex_oauth_store": {"present": True}},
    ) == "Cursor Agent CLI binary not found"


def test_codex_and_openrouter_arm_blocker_accepts_credential_pool(
    tmp_path, monkeypatch,
):
    """Pool runtime token unblocks Marionette arms without exposing token text."""
    from harness import credential_pool as cp

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    cp.clear_pools_for_tests()

    secret_codex = "sk-codex-pool-secret-must-not-leak"
    secret_or = "sk-openrouter-pool-secret-must-not-leak"
    try:
        cp.add_oauth_entry(
            "openai-codex",
            access_token=secret_codex,
            label="cache-matrix-codex",
        )
        cp.add_api_key(
            "openrouter",
            secret_or,
            label="cache-matrix-openrouter",
        )
        # add_* mirrors into env; clear so present comes from the pool seam.
        monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        # Dry-run must not consult the pool (resolver-free).
        dry = preflight_status(arms=["codex", "openrouter"], load_keys=False)
        assert dry["openai_codex_token"]["credential_pool_present"] is False
        assert dry["openai_codex_token"]["present"] is False
        assert dry["openrouter_api_key"]["credential_pool_present"] is False
        assert dry["openrouter_api_key"]["present"] is False
        assert arm_blocker("codex", dry) is not None
        assert arm_blocker("openrouter", dry) is not None

        status = preflight_status(arms=["codex", "openrouter"], load_keys=True)
        assert status["openai_codex_token"]["env_present"] is False
        assert status["openai_codex_token"]["credential_pool_present"] is True
        assert status["openai_codex_token"]["present"] is True
        assert status["openrouter_api_key"]["env_present"] is False
        assert status["openrouter_api_key"]["credential_pool_present"] is True
        assert status["openrouter_api_key"]["present"] is True
        assert arm_blocker("codex", status) is None
        assert arm_blocker("openrouter", status) is None

        dumped = json.dumps(status)
        assert secret_codex not in dumped
        assert secret_or not in dumped
        assert "sk-codex-pool" not in dumped
        assert "sk-openrouter-pool" not in dumped
    finally:
        cp.clear_pools_for_tests()


# ---------------------------------------------------------------------------
# Warmup-excluded hit rate
# ---------------------------------------------------------------------------


def test_warmup_excluded_hit_rate_ignores_warmup_and_unknown():
    turns = [
        {"turn": 1, "tokens_in": 1000, "cache_read": 0},       # warmup
        {"turn": 2, "tokens_in": 1000, "cache_read": 800},
        {"turn": 3, "tokens_in": 1000, "cache_read": 900},
    ]
    rate = warmup_excluded_hit_rate(turns, warmup_turns=1)
    assert rate == pytest.approx((800 + 900) / 2000.0)

    # Unknown denominator when post-warmup cache_read is all null.
    unknown = [
        {"turn": 1, "tokens_in": 1000, "cache_read": 50},
        {"turn": 2, "tokens_in": 1000, "cache_read": None},
        {"turn": 3, "tokens_in": 1000, "cache_read": None},
    ]
    assert warmup_excluded_hit_rate(unknown, warmup_turns=1) is None

    # Missing evidence must not become zero hit rate.
    assert warmup_excluded_hit_rate([], warmup_turns=1) is None


# ---------------------------------------------------------------------------
# Cost source / claim hygiene
# ---------------------------------------------------------------------------


def test_plan_api_cost_source_separation_and_no_usd_headline():
    assert cost_source_for_arm(billing="api", provider_cost_usd=0.12) == "provider"
    assert cost_source_for_arm(billing="api", provider_cost_usd=None) == "unknown"
    assert cost_source_for_arm(billing="plan", provider_cost_usd=None) == "plan_estimated"
    assert cost_source_for_arm(
        billing="plan", provider_cost_usd=None, plan_estimated_usd=1.5,
    ) == "plan_estimated"

    md = render_markdown_summary(
        {
            "created_at": "t",
            "protocol": "gpt56-luna-cache-matrix-v1",
            "mode": "dry-run",
            "schema_version": 1,
            "warmup_turns": 1,
            "requested_models": {"codex": "gpt-5.6-luna", "openrouter": "openai/gpt-5.6-luna"},
            "billing_policy": {"codex": "plan", "openrouter": "api"},
            "arms": {
                "codex": {
                    "status": "ok",
                    "billing": "plan",
                    "cost_source": "plan_estimated",
                    "warmup_excluded_hit_rate": 0.5,
                    "totals": {"errors": 0},
                },
                "openrouter": {
                    "status": "ok",
                    "billing": "api",
                    "cost_source": "provider",
                    "warmup_excluded_hit_rate": 0.4,
                    "totals": {"errors": 0, "provider_cost_usd": 0.2},
                },
            },
            "tool_surface_limitation": "limit",
        }
    )
    low = md.lower()
    assert "plan-vs-api" in low or "never a plan-vs-api" in low
    assert "cheaper than" not in low
    assert "% cheaper" not in low
    assert "savings headline" in low or "usd savings" in low


# ---------------------------------------------------------------------------
# Negative control env restore + wire markers
# ---------------------------------------------------------------------------


def test_negative_control_env_restored_and_breakpoint_absent(monkeypatch):
    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "1")
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "auto")
    before_pc = os.environ.get("HARNESS_PROMPT_CACHE")
    before_bp = os.environ.get("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT")

    result = negative_control_wire_check(
        model="gpt-5.6-luna",
        session_id="neg-sess-1",
    )
    assert result["ok"] is True
    assert result["has_prompt_cache_extensions"] is False
    assert result["has_prompt_cache_options"] is False
    assert result["has_prompt_cache_key"] is False
    assert result["assertion"] == "wire_marker_absence_only"
    assert (result.get("apply_detail") or {}).get("reason") == "cache_disabled"

    assert os.environ.get("HARNESS_PROMPT_CACHE") == before_pc
    assert os.environ.get("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT") == before_bp

    # Context manager restores clears too.
    with env_override({"HARNESS_PROMPT_CACHE": "0"}):
        assert os.environ.get("HARNESS_PROMPT_CACHE") == "0"
    assert os.environ.get("HARNESS_PROMPT_CACHE") == before_pc


# ---------------------------------------------------------------------------
# Fake Marionette driver arm receipt fields
# ---------------------------------------------------------------------------


def test_fake_marionette_driver_arm_captures_session_and_receipt_fields():
    seen: Dict[str, Any] = {}

    class FakeDriver:
        def chat(self, messages, *, tools=None, system=None, session_id=None):
            seen["session_id"] = session_id
            seen["system"] = system
            seen["tools"] = tools
            seen["messages"] = list(messages)
            n = sum(1 for m in messages if m.get("role") == "user")
            return SimpleNamespace(
                text=f"OK-{n}",
                tokens_in=1000 if n == 1 else 1100,
                tokens_out=4,
                error=None,
                model="cache-matrix-codex",
                meta={
                    "billing": "plan",
                    "requested_model": "gpt-5.6-luna",
                    "served_model": "gpt-5.6-luna",
                    "prompt_cache_key": "11111111-1111-1111-1111-111111111111",
                    "raw_usage": {
                        "prompt_tokens": 1000 if n == 1 else 1100,
                        "completion_tokens": 4,
                        "prompt_tokens_details": {
                            "cached_tokens": 0 if n == 1 else 800,
                        },
                    },
                    "cache_read_tokens": 0 if n == 1 else 800,
                },
            )

    protocol = build_protocol(turns=3, content_tokens=16)
    result = run_marionette_driver_arm(
        arm="codex",
        driver=FakeDriver(),
        protocol=protocol,
        requested_model="gpt-5.6-luna",
        session_id="sess-fixed-001",
        warmup_turns=1,
        billing="plan",
    )
    assert seen["session_id"] == "sess-fixed-001"
    assert seen["system"] == protocol["system"]
    assert seen["tools"] == protocol["tools"]
    assert result["session_id"] == "sess-fixed-001"
    assert result["billing"] == "plan"
    assert result["cost_source"] == "plan_estimated"
    assert result["driver"] == "CodexResponsesDriver"
    assert result["requested_model"] == "gpt-5.6-luna"
    assert len(result["turns"]) == 3
    assert result["turns"][0]["prompt_cache_key"]
    assert result["turns"][1]["cache_read"] == 800
    assert result["warmup_excluded_hit_rate"] is not None
    assert result["warmup_excluded_hit_rate"] > 0


def test_openrouter_fake_arm_api_cost_source():
    class FakeOR:
        def chat(self, messages, *, tools=None, system=None, session_id=None):
            return SimpleNamespace(
                text="OK-1",
                tokens_in=200,
                tokens_out=2,
                error=None,
                model="cache-matrix-openrouter",
                meta={
                    "provider_cost_usd": 0.0012,
                    "served_model": "openai/gpt-5.6-luna",
                    "raw_usage": {
                        "prompt_tokens": 200,
                        "completion_tokens": 2,
                        "cost": 0.0012,
                        "prompt_tokens_details": {"cached_tokens": 50},
                    },
                    "cache_read_tokens": 50,
                },
            )

    protocol = build_protocol(turns=1, content_tokens=4)
    result = run_marionette_driver_arm(
        arm="openrouter",
        driver=FakeOR(),
        protocol=protocol,
        requested_model="openai/gpt-5.6-luna",
        billing="api",
    )
    assert result["billing"] == "api"
    assert result["cost_source"] == "provider"
    assert result["turns"][0]["provider_cost_usd"] == pytest.approx(0.0012)


def test_run_dry_helper_includes_fingerprint():
    protocol = build_protocol(
        turns=2, content_tokens=8, stable_prefix_tokens=96,
    )
    models = {
        "codex": "gpt-5.6-luna",
        "openrouter": "openai/gpt-5.6-luna",
        "cursor": "gpt-5.6-luna",
        "native_codex": "gpt-5.6-luna",
    }
    payload = run_dry(
        protocol=protocol,
        models=models,
        arms=["codex", "openrouter"],
        warmup_turns=1,
        allow_mismatch=False,
        cursor_agent_bin=None,
        native_codex_bin="codex",
    )
    assert payload["protocol_fingerprint"]["tools_count"] == 2
    assert payload["protocol_fingerprint"]["stable_prefix_tokens"] == 96
    assert payload["stable_prefix_tokens"] == 96
    assert payload["dry_protocol"]["stable_prefix_tokens"] == 96
    assert payload["dry_protocol"]["system"] == protocol["system"]
    assert "openrouter_api_key" in payload["preflight"]
    # Preflight must not expose secret values.
    assert "sk-" not in json.dumps(payload["preflight"])


# ---------------------------------------------------------------------------
# Live arm env forcing + Cursor fair protocol + error cache evidence
# ---------------------------------------------------------------------------


def test_run_live_arm_forces_enabled_env_and_restores(monkeypatch):
    """Positive Marionette arms force cache-on markers; ambient kill-switch restored."""
    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "0")
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    seen_env = {}

    class FakeDriver:
        def chat(self, messages, *, tools=None, system=None, session_id=None):
            seen_env["HARNESS_PROMPT_CACHE"] = os.environ.get("HARNESS_PROMPT_CACHE")
            seen_env["HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT"] = os.environ.get(
                "HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT"
            )
            return SimpleNamespace(
                text="OK-1",
                tokens_in=10,
                tokens_out=1,
                error=None,
                model="cache-matrix-codex",
                meta={"served_model": "gpt-5.6-luna"},
            )

    protocol = build_protocol(turns=1, content_tokens=4)
    models = {
        "codex": "gpt-5.6-luna",
        "openrouter": "openai/gpt-5.6-luna",
        "cursor": "gpt-5.6-luna",
        "native_codex": "gpt-5.6-luna",
    }
    preflight = {
        "openai_codex_token": {"present": True},
        "codex_oauth_store": {"present": False},
        "openrouter_api_key": {"present": True},
        "cursor_agent": {"present": True, "binary": "/fake/agent"},
        "native_codex_cli": {"present": True, "binary": "/fake/codex"},
    }
    result = run_live_arm(
        "codex",
        protocol=protocol,
        models=models,
        workspace=None,
        openrouter_base="https://openrouter.ai/api/v1",
        cursor_agent_bin=None,
        native_codex_bin=None,
        max_output_tokens=64,
        warmup_turns=0,
        idle_seconds=0.0,
        allow_mismatch=False,
        skip_unavailable=False,
        preflight=preflight,
        driver_factory=lambda _arm: FakeDriver(),
    )
    assert result["status"] == "ok"
    assert seen_env["HARNESS_PROMPT_CACHE"] == "1"
    assert seen_env["HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT"] == "auto"
    # Ambient kill-switch restored after the arm.
    assert os.environ.get("HARNESS_PROMPT_CACHE") == "0"
    assert os.environ.get("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT") == "off"


def test_run_live_arm_negative_control_overrides_off_and_restores(monkeypatch):
    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "1")
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "auto")
    seen_env = {}

    class FakeDriver:
        def chat(self, messages, *, tools=None, system=None, session_id=None):
            seen_env["HARNESS_PROMPT_CACHE"] = os.environ.get("HARNESS_PROMPT_CACHE")
            seen_env["HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT"] = os.environ.get(
                "HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT"
            )
            # Provider may still report cache_read under the kill switch;
            # negative_control must not treat that as a headline hit rate.
            return SimpleNamespace(
                text="OK-1",
                tokens_in=1000,
                tokens_out=1,
                error=None,
                model="cache-matrix-negative",
                meta={
                    "served_model": "gpt-5.6-luna",
                    "cache_read_tokens": 800,
                    "raw_usage": {
                        "prompt_tokens": 1000,
                        "completion_tokens": 1,
                        "prompt_tokens_details": {"cached_tokens": 800},
                    },
                },
            )

    protocol = build_protocol(turns=2, content_tokens=4)
    models = {
        "codex": "gpt-5.6-luna",
        "openrouter": "openai/gpt-5.6-luna",
        "cursor": "gpt-5.6-luna",
        "native_codex": "gpt-5.6-luna",
    }
    preflight = {
        "openai_codex_token": {"present": True},
        "codex_oauth_store": {"present": False},
        "openrouter_api_key": {"present": True},
        "cursor_agent": {"present": True},
        "native_codex_cli": {"present": True},
    }
    result = run_live_arm(
        "negative_control",
        protocol=protocol,
        models=models,
        workspace=None,
        openrouter_base="https://openrouter.ai/api/v1",
        cursor_agent_bin=None,
        native_codex_bin=None,
        max_output_tokens=64,
        warmup_turns=0,
        idle_seconds=0.0,
        allow_mismatch=False,
        skip_unavailable=False,
        preflight=preflight,
        driver_factory=lambda _arm: FakeDriver(),
    )
    assert result["cache_claim"] == "wire_marker_absence_only"
    # Never headline provider cache reads as a negative-control hit rate.
    assert result["warmup_excluded_hit_rate"] is None
    assert result["wire_check"]["has_prompt_cache_key"] is False
    assert seen_env["HARNESS_PROMPT_CACHE"] == "0"
    assert seen_env["HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT"] == "off"
    assert os.environ.get("HARNESS_PROMPT_CACHE") == "1"
    assert os.environ.get("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT") == "auto"


def test_turn_retains_nested_prompt_cache_diagnostics():
    resp = SimpleNamespace(
        text="OK-1",
        tokens_in=100,
        tokens_out=2,
        error=None,
        model="cache-matrix-codex",
        meta={
            "served_model": "gpt-5.6-luna",
            "prompt_cache_key": "11111111-1111-1111-1111-111111111111",
            "prompt_cache": {
                "reason": "explicit_breakpoint",
                "breakpoint": True,
                "prompt_cache_key": "11111111-1111-1111-1111-111111111111",
                "initial": {
                    "prompt_cache_key_present": True,
                    "prompt_cache_key": "11111111-1111-1111-1111-111111111111",
                    "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
                    "explicit_breakpoint": True,
                },
                "final": {
                    "prompt_cache_key_present": True,
                    "prompt_cache_key": "11111111-1111-1111-1111-111111111111",
                    "prompt_cache_options": None,
                    "explicit_breakpoint": False,
                },
            },
            "raw_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
            "cache_read_tokens": 40,
        },
    )
    turn = turn_from_driver_response(
        resp,
        turn_index=1,
        requested_model="gpt-5.6-luna",
        wall_s=0.1,
        session_id="sess-diag",
    )
    diag = turn["prompt_cache_diagnostics"]
    assert diag["initial"]["explicit_breakpoint"] is True
    assert diag["final"]["explicit_breakpoint"] is False
    assert diag["final"]["prompt_cache_options"] is None
    assert diag["reason"] == "explicit_breakpoint"
    assert turn["prompt_cache_key"] == "11111111-1111-1111-1111-111111111111"


def test_negative_control_markdown_hit_rate_is_unknown():
    payload = {
        "created_at": "2026-08-01T00:00:00Z",
        "protocol": "cache-matrix-v1",
        "mode": "live",
        "schema_version": 1,
        "warmup_turns": 1,
        "tool_surface_limitation": "test",
        "requested_models": {"codex": "gpt-5.6-luna"},
        "billing_policy": {"negative_control": "plan"},
        "arms": {
            "negative_control": {
                "status": "ok",
                "billing": "plan",
                "cost_source": "plan_estimated",
                "cache_claim": "wire_marker_absence_only",
                # Even if a rate sneaks in, markdown must not headline it.
                "warmup_excluded_hit_rate": 0.99,
                "totals": {"errors": 0},
            }
        },
    }
    md = render_markdown_summary(payload)
    assert "| negative_control |" in md
    assert "0.9900" not in md
    assert "unknown" in md


def test_make_cursor_driver_constructs_with_ask_mode(monkeypatch):
    """Cache-matrix Cursor arm must pin mode=ask for CLI versions that reject agent."""
    captured: Dict[str, Any] = {}

    class CapturingCursorCliDriver:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.CursorCliDriver",
        CapturingCursorCliDriver,
    )
    driver = make_cursor_driver(
        "gpt-5.6-luna",
        256,
        agent_binary="/tmp/fake-agent",
        workspace="/tmp/cache-matrix-ws",
    )
    assert isinstance(driver, CapturingCursorCliDriver)
    assert captured["mode"] == "ask"
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["agent_binary"] == "/tmp/fake-agent"
    assert captured["cwd"] == "/tmp/cache-matrix-ws"
    assert captured["name"] == "cache-matrix-cursor"
    assert captured["max_tokens"] == 256


def test_cursor_arm_passes_system_with_stable_prefix_and_manifest():
    seen = {}

    class FakeCursor:
        def chat(self, messages, *, tools=None, system=None, session_id=None):
            seen["system"] = system
            seen["tools"] = tools
            return SimpleNamespace(
                text="OK-1",
                tokens_in=20,
                tokens_out=1,
                error=None,
                model="cache-matrix-cursor",
                meta={"served_model": "gpt-5.6-luna", "billing": "plan"},
            )

    protocol = build_protocol(turns=1, content_tokens=8)
    result = run_marionette_driver_arm(
        arm="cursor",
        driver=FakeCursor(),
        protocol=protocol,
        requested_model="gpt-5.6-luna",
        warmup_turns=0,
        billing="plan",
    )
    expected = cursor_fair_system(protocol)
    assert seen["system"] == expected
    assert "marionette_cache_matrix_stable_v1" in seen["system"]
    assert "# Tool manifest (schema only; native loop differs)" in seen["system"]
    assert protocol["tool_manifest_text"] in seen["system"]
    # Codex/OpenRouter still receive structured tools; Cursor gets them too
    # (driver ignores host tools) while fair text lives in system.
    assert seen["tools"] == protocol["tools"]
    assert result["arm"] == "cursor"


def test_turn_from_driver_response_preserves_cache_on_error():
    resp = SimpleNamespace(
        text="",
        tokens_in=120,
        tokens_out=0,
        error="HTTP 500: boom",
        model="cache-matrix-openrouter",
        meta={
            "served_model": "openai/gpt-5.6-luna",
            "provider_cost_usd": 0.002,
            "cache_read_tokens": 40,
            "cache_write_tokens": 0,
            "raw_usage": {
                "prompt_tokens": 120,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 40},
                "cacheWriteInputTokens": 0,
                "cost": 0.002,
            },
        },
    )
    turn = turn_from_driver_response(
        resp,
        turn_index=2,
        requested_model="openai/gpt-5.6-luna",
        wall_s=0.5,
        session_id="s1",
    )
    assert turn["error"] == "HTTP 500: boom"
    assert turn["tokens_in"] == 120
    assert turn["cache_read"] == 40
    assert turn["cache_write"] == 0
    assert turn["provider_cost_usd"] == pytest.approx(0.002)
    assert turn["served_model"] == "openai/gpt-5.6-luna"

    # Absent cache fields stay null — never invented zeros.
    resp2 = SimpleNamespace(
        text="",
        tokens_in=0,
        tokens_out=0,
        error="network down",
        model="cache-matrix-openrouter",
        meta={"raw_usage": {"prompt_tokens": 5, "completion_tokens": 0}},
    )
    turn2 = turn_from_driver_response(
        resp2,
        turn_index=1,
        requested_model="openai/gpt-5.6-luna",
        wall_s=0.1,
    )
    assert turn2["error"]
    assert turn2["cache_read"] is None
    assert turn2["cache_write"] is None


def test_turn_from_driver_response_mismatch_on_error_fails_closed():
    resp = SimpleNamespace(
        text="",
        tokens_in=1,
        tokens_out=0,
        error="partial failure",
        model="cache-matrix-codex",
        meta={"served_model": "gpt-other-model"},
    )
    with pytest.raises(ModelMismatchError):
        turn_from_driver_response(
            resp,
            turn_index=1,
            requested_model="gpt-5.6-luna",
            wall_s=0.1,
            allow_mismatch=False,
        )


def test_dry_preflight_makes_no_subprocess_calls(monkeypatch):
    calls = []

    def boom(*_a, **_k):
        calls.append("subprocess")
        raise AssertionError("subprocess must not run when load_keys=False")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    status = preflight_status(arms=["cursor", "native-codex"], load_keys=False)
    assert calls == []
    assert "auth" not in status.get("cursor_agent", {})
    assert "auth" not in status.get("native_codex_cli", {})
    # Env presence is recorded without probing; never a secret value.
    assert status["cursor_agent"]["api_key_present"] is False
    assert status["cursor_agent"]["api_key_env"] == "CURSOR_API_KEY"
    # Binary absence still blocks; missing auth probe does not.
    assert arm_blocker(
        "cursor",
        {"cursor_agent": {"present": True}},
    ) is None


def test_live_preflight_auth_blocker_authenticated_and_unauthenticated(monkeypatch, tmp_path):
    agent = tmp_path / "agent"
    agent.write_text("#!/bin/sh\n", encoding="utf-8")
    agent.chmod(0o755)
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    codex.chmod(0o755)

    def fake_run(argv, **_kwargs):
        cmd = list(argv)
        if cmd and str(cmd[0]).endswith("agent") and "status" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"isAuthenticated": False, "status": "unauthenticated"}),
                stderr="",
            )
        if cmd and str(cmd[0]).endswith("codex") and "login" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"authenticated": True, "status": "authenticated"}),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.resolve_agent_binary",
        lambda: str(agent),
    )
    monkeypatch.setattr(
        "bench.cache_matrix.resolve_native_codex_binary",
        lambda explicit=None: str(codex),
    )
    # Isolate browser-store path: empty state dir + no env key so load_keys
    # cannot re-inject CURSOR_API_KEY from the developer keys.json.
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_KEY_FILE", raising=False)

    status = preflight_status(
        arms=["cursor", "native-codex"],
        cursor_agent_bin=str(agent),
        native_codex_bin=str(codex),
        load_keys=True,
    )
    dumped = json.dumps(status)
    assert "sk-" not in dumped
    assert "@" not in dumped  # no emails/secrets
    assert status["cursor_agent"]["api_key_present"] is False
    assert status["cursor_agent"]["auth"]["authenticated"] is False
    assert status["cursor_agent"]["auth"]["status"] == "unauthenticated"
    assert status["native_codex_cli"]["auth"]["authenticated"] is True
    assert arm_blocker("cursor", status) == "Cursor Agent CLI not authenticated"
    assert arm_blocker("native-codex", status) is None

    # Flip Cursor to authenticated — blocker clears.
    def fake_run_ok(argv, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"loggedIn": True, "email": "secret@example.com"}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run_ok)
    status2 = preflight_status(
        arms=["cursor"],
        cursor_agent_bin=str(agent),
        load_keys=True,
    )
    assert status2["cursor_agent"]["auth"]["authenticated"] is True
    assert status2["cursor_agent"]["auth"]["status"] == "authenticated"
    assert "secret@example.com" not in json.dumps(status2)
    assert arm_blocker("cursor", status2) is None


def test_cursor_api_key_unblocks_browser_unauthenticated_cli(monkeypatch, tmp_path):
    """CURSOR_API_KEY presence unblocks cursor even when agent status is unauthenticated.

    Receipt stays honest: browser auth remains false; only api_key_present is True.
    The secret value must never appear in the serialized status.
    """
    agent = tmp_path / "agent"
    agent.write_text("#!/bin/sh\n", encoding="utf-8")
    agent.chmod(0o755)
    secret = "sk-cursor-secret-value-do-not-leak"

    def fake_run(argv, **_kwargs):
        cmd = list(argv)
        if cmd and str(cmd[0]).endswith("agent") and "status" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"isAuthenticated": False, "status": "unauthenticated"}
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "pmharness.drivers.cursor_cli.resolve_agent_binary",
        lambda: str(agent),
    )
    # Hermetic key store: load_keys must not pull developer CURSOR_API_KEY.
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_KEY_FILE", raising=False)

    # Absent key + browser unauthenticated → fail closed.
    status_blocked = preflight_status(
        arms=["cursor"],
        cursor_agent_bin=str(agent),
        load_keys=True,
    )
    assert status_blocked["cursor_agent"]["api_key_present"] is False
    assert status_blocked["cursor_agent"]["auth"]["authenticated"] is False
    assert status_blocked["cursor_agent"]["auth"]["status"] == "unauthenticated"
    assert arm_blocker("cursor", status_blocked) == (
        "Cursor Agent CLI not authenticated"
    )

    # Key present + browser still unauthenticated → arm unblocked; receipt
    # does not claim browser auth succeeded.
    monkeypatch.setenv("CURSOR_API_KEY", secret)
    status_ok = preflight_status(
        arms=["cursor"],
        cursor_agent_bin=str(agent),
        load_keys=True,
    )
    dumped = json.dumps(status_ok)
    assert secret not in dumped
    assert "sk-cursor" not in dumped
    assert status_ok["cursor_agent"]["api_key_present"] is True
    assert status_ok["cursor_agent"]["api_key_env"] == "CURSOR_API_KEY"
    assert status_ok["cursor_agent"]["auth"]["authenticated"] is False
    assert status_ok["cursor_agent"]["auth"]["status"] == "unauthenticated"
    assert arm_blocker("cursor", status_ok) is None

    # Presence-only unit path: api_key_present alone clears the blocker.
    assert (
        arm_blocker(
            "cursor",
            {
                "cursor_agent": {
                    "present": True,
                    "api_key_present": True,
                    "auth": {
                        "checked": True,
                        "authenticated": False,
                        "status": "unauthenticated",
                    },
                },
            },
        )
        is None
    )
    assert (
        arm_blocker(
            "cursor",
            {
                "cursor_agent": {
                    "present": True,
                    "api_key_present": False,
                    "auth": {
                        "checked": True,
                        "authenticated": False,
                        "status": "unauthenticated",
                    },
                },
            },
        )
        == "Cursor Agent CLI not authenticated"
    )
