"""Luna Max routing, http_status:400 fail-loud, and Home run_swarm guard."""
from __future__ import annotations

import json
from types import SimpleNamespace

from pmharness.bridge import (
    BridgeResult,
    _analysis_bridge_status,
    _is_no_structure_failure,
    _promote_degraded_prose,
    _provider_http_reject_note,
)


def test_http_status_400_is_no_structure_failure():
    assert _is_no_structure_failure("http_status:400")
    assert _is_no_structure_failure("http_status:429")
    assert not _is_no_structure_failure("")
    assert _is_no_structure_failure("empty_or_unstructured_agentic_result")  # listed; promotion uses _is_nonpromotable_failure exception


def test_http_status_400_never_promotes_and_fails_bridge_status():
    compact = [
        {
            "type": "verification",
            "headline": "ok",
            "body": "not supported when using Codex with a ChatGPT account",
            "detail": "not supported when using Codex with a ChatGPT account",
            "failure": "http_status:400",
            "empty_headline": False,
        }
    ]
    promoted = _promote_degraded_prose(compact)
    assert not any(str(a.get("type")) == "finding" for a in promoted)
    status, summary = _analysis_bridge_status(
        compact, job_status="complete", summary="completed without structured findings",
    )
    assert status in ("failed", "degraded", "error")
    assert "400" in summary or "provider reject" in summary.lower() or "no structured" in summary.lower()
    note = _provider_http_reject_note(compact)
    assert "http_status:400" in note
    assert "Codex" in note or "provider reject" in note


def test_all_workers_http_400_badge_not_green(monkeypatch):
    monkeypatch.setattr("harness.edit_engines.workers_ready", lambda: True)
    """Sync swarm with only http_status:400 plumbing must not green-complete."""
    import tempfile
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from pmharness.drivers.openai_compat import DriverResponse

    def _bad_result(*_a, **_k):
        return BridgeResult(
            job_id="job_http400_abc",
            status="complete",
            mode="analysis",
            num_artifacts=2,
            artifact_types=["verification"],
            summary="completed without structured findings",
            artifacts=[
                {
                    "type": "verification",
                    "headline": "provider reject (HTTP 400): not supported when using Codex",
                    "body": "not supported when using Codex with a ChatGPT account",
                    "detail": "stderr: model gpt-5.6-luna-pro not supported when using Codex with a ChatGPT account",
                    "failure": "http_status:400",
                    "empty_headline": False,
                },
                {
                    "type": "verification",
                    "headline": "provider reject (HTTP 400): not supported when using Codex",
                    "body": "not supported when using Codex with a ChatGPT account",
                    "detail": "stderr: model gpt-5.6-luna-pro not supported when using Codex with a ChatGPT account",
                    "failure": "http_status:400",
                    "empty_headline": False,
                },
            ],
            adapter="agentic",
        )

    class _SwarmOncePilot:
        name = "swarm-once"

        def __init__(self):
            self.n = 0

        def complete(self, prompt, *, system=None, tools=None):
            self.n += 1
            if self.n == 1:
                return DriverResponse(
                    text=(
                        '{"say":"auditing",'
                        '"actions":[{"kind":"run_swarm","goal":"Audit the repo",'
                        '"roles":["explore","conflict-auditor"]}]}'
                    ),
                    tokens_out=8,
                    latency_ms=1.0,
                )
            return DriverResponse(
                text='{"say":"done.","actions":[]}',
                tokens_out=4,
                latency_ms=1.0,
            )

    monkeypatch.setattr(
        "harness.send_loop_phases.execute_intent",
        lambda intent, **kw: _bad_result(),
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _SwarmOncePilot()
    events = list(s.send("please audit"))
    results = [e for e in events if e.kind == "swarm_result"]
    assert results
    badge = results[0].data["result"]
    assert badge["applied"] is False
    blob = f"{badge.get('summary') or ''}\n{badge.get('error') or ''}".lower()
    assert "400" in blob or "provider reject" in blob or "codex" in blob
    assert badge.get("error")


def test_home_workspace_swarm_error_nudge():
    from harness.send_loop_dispatch import (
        _home_workspace_swarm_error,
        _looks_like_home_workspace,
        _non_git_workspace_error,
    )

    home = "/Users/cary/.pmharness/state/home"
    assert _looks_like_home_workspace(home)
    err = _home_workspace_swarm_error(home)
    assert err
    assert "Home" in err
    assert "Projects" in err or "repo=" in err
    non_git = _non_git_workspace_error(home)
    assert non_git
    assert "Home" in non_git


def test_home_run_swarm_refuses_before_dispatch(monkeypatch, tmp_path):
    from harness.pilot import PilotAction
    import harness.send_loop_dispatch as dispatch

    home = tmp_path / "state" / "home"
    home.mkdir(parents=True)
    # Make path look like pmharness home for the heuristic.
    pm_home = tmp_path / ".pmharness" / "state" / "home"
    pm_home.mkdir(parents=True)

    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(pm_home)),
        _validate_target_repo=lambda p: (p, ""),
        _append_action_result=lambda *a, **k: None,
        _cancel=SimpleNamespace(is_set=lambda: False),
    )
    act = PilotAction(kind="run_swarm", goal="audit home", roles=["explore"])
    events = list(
        dispatch.dispatch_swarm_action(
            session, act, "a1", True, counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
        )
    )
    assert events
    assert any(
        getattr(e, "kind", "") == "action_result"
        and "Home" in str((getattr(e, "data", {}) or {}).get("error") or "")
        for e in events
    )


def test_openai_codex_live_catalog_wins_over_static_pro(monkeypatch):
    from harness import model_visibility as mv
    from harness.providers import get_provider

    p = get_provider("openai-codex")
    assert "gpt-5.6-luna-pro" not in p.pilot_models

    class _P:
        name = "openai-codex"
        pilot_models = ("gpt-5.6-luna", "gpt-5.6-luna-pro", "gpt-5.6-sol")

        def key(self):
            return "test-codex-token"

    monkeypatch.setattr(
        "harness.model_fetch.fetch_models",
        lambda *_a, **_k: ["gpt-5.6-luna", "gpt-5.5"],
    )
    live = mv.provider_models(_P(), force=True)
    assert live == ["gpt-5.6-luna", "gpt-5.5"]
    assert "gpt-5.6-luna-pro" not in live
