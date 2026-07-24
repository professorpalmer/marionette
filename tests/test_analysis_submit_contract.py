"""Analysis workers must obey the swarm submit contract.

A run_parallel / run_implement analysis worker that only streams reasoning
must never report clean 'completed' with a truncated thought as the finding
headline. Structured findings still pass. Hermetic -- no network.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from harness.conversation import ConversationalSession, ConvEvent
from harness.worker import (
    ProviderWorker,
    _analysis_output_is_structured,
)
from pmharness.bridge import (
    _analysis_bridge_status,
    _analysis_instruction,
    _compact_artifact,
    _has_real_structured_findings,
    _looks_like_reasoning_fragment,
    _promote_degraded_prose,
    _worker_submitted_structure,
)


class _Artifact:
    def __init__(self, type, payload, confidence=None):
        self.type = type
        self.payload = payload
        self.confidence = confidence


def create_temp_git_repo():
    repo_dir = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repo_dir, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_dir,
        capture_output=True,
    )
    with open(os.path.join(repo_dir, "test.txt"), "w", encoding="utf-8") as f:
        f.write("hello\n")
    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"], cwd=repo_dir, capture_output=True
    )
    return repo_dir


def test_reasoning_fragment_detected():
    assert _looks_like_reasoning_fragment("Now let me look at the auth module...")
    assert _looks_like_reasoning_fragment("Let me check harness/worker.py next")
    assert not _looks_like_reasoning_fragment(
        "FINDING: harness/worker.py:680 empty-diff analysis accepts reasoning"
    )
    assert not _looks_like_reasoning_fragment(
        "Audit complete: no issues found in auth."
    )


def test_analysis_output_helper_rejects_reasoning_only():
    ok, reason = _analysis_output_is_structured(
        "Now let me look at the routing layer more carefully..."
    )
    assert ok is False
    assert "no structured findings" in reason
    assert "reasoning" in reason

    ok2, reason2 = _analysis_output_is_structured(
        "", halt_reason="no_tool_calls after 3 turns"
    )
    assert ok2 is False
    assert "no_tool_calls" in reason2

    ok3, reason3 = _analysis_output_is_structured(
        "FINDING: harness/keys.py:12 leaks the API key into logs"
    )
    assert ok3 is True
    assert reason3 == ""


def test_worker_reasoning_only_analysis_fails(monkeypatch):
    """expects_diff=False + reasoning-only last message => ok=False, no headline."""
    repo_dir = create_temp_git_repo()
    try:
        def mock_run_auto(self, objective, budget=None, require_codegraph=True, **kwargs):
            # Analysis brief must be used (not IMPLEMENT TASK).
            assert "ANALYSIS" in objective or "READ-ONLY" in objective
            assert "IMPLEMENT TASK" not in objective
            yield ConvEvent(
                "message",
                {"text": "Now let me look at the auth module more carefully..."},
            )
            yield ConvEvent("auto_halt", {"reason": "max turns"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        worker = ProviderWorker(
            repo=repo_dir,
            goal="Audit auth",
            expects_diff=False,
        )
        res = worker.run()
        assert res.ok is False
        assert "no structured findings" in (res.error or "")
        # Degrade label is the error; diagnostic prose may remain in summary
        # for the pilot, but must not be treated as a clean success headline.
        assert res.ok is False
        assert not (res.patch or "").strip()
    finally:
        shutil.rmtree(repo_dir)


def test_worker_structured_analysis_still_passes(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        def mock_run_auto(self, objective, budget=None, require_codegraph=True, **kwargs):
            yield ConvEvent(
                "message",
                {
                    "text": (
                        "FINDING: harness/worker.py:700 analysis empty-diff path "
                        "must reject reasoning-only output."
                    )
                },
            )
            yield ConvEvent("auto_halt", {"reason": "pilot reports objective met"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        worker = ProviderWorker(
            repo=repo_dir,
            goal="Audit worker analysis path",
            expects_diff=False,
        )
        res = worker.run()
        assert res.ok is True
        assert "FINDING:" in (res.summary or "")
        assert not (res.error or "").strip()
    finally:
        shutil.rmtree(repo_dir)


def test_promote_skips_reasoning_fragment():
    prose = "Now let me look at the cache eviction path and then report back..."
    compact = [
        {"type": "routing", "headline": "", "empty_headline": True},
        {
            "type": "verification",
            "headline": prose,
            "body": prose,
            "empty_headline": False,
            "failure": "empty_or_unstructured_agentic_result",
        },
    ]
    out = _promote_degraded_prose(compact)
    assert not any(a.get("type") == "finding" for a in out)
    assert not _has_real_structured_findings(out)


def test_promote_skips_no_tool_calls_stdout():
    prose = (
        "I was about to inspect several modules. Now let me look at server.py "
        "and then the keys module after that."
    )
    compact = [
        _compact_artifact(_Artifact("verification", {
            "stdout": prose,
            "failure": "no_tool_calls",
            "stop_reason": "no_tool_calls",
        })),
        _compact_artifact(_Artifact("risk", {
            "risk": "model x/y produced 3 turns of prose but never called any tool",
            "failure": "no_tool_calls",
        })),
    ]
    out = _promote_degraded_prose(compact)
    assert not any(
        a.get("type") == "finding" and a.get("promoted_from") == "verification"
        for a in out
    )


def test_bridge_status_fails_reasoning_only():
    compact = [
        {
            "type": "verification",
            "headline": "Now let me look at...",
            "body": "Now let me look at the auth code...",
            "empty_headline": False,
            "failure": "empty_or_unstructured_agentic_result",
        }
    ]
    status, summary = _analysis_bridge_status(
        compact, job_status="completed", summary="Now let me look at..."
    )
    assert status in ("failed", "degraded")
    assert "no structured findings" in summary.lower()
    assert "Now let me look" not in summary


def test_bridge_status_keeps_real_findings():
    compact = [
        {
            "type": "finding",
            "headline": "harness/keys.py:12 logs the API key",
            "body": "harness/keys.py:12 logs the API key in plaintext",
            "empty_headline": False,
            "failure": None,
        }
    ]
    status, summary = _analysis_bridge_status(
        compact, job_status="completed", summary="1 finding"
    )
    assert status == "completed"
    assert summary == "1 finding"
    assert _has_real_structured_findings(compact)


def test_honest_empty_submit_stays_clean():
    """submit_findings([]) with a clean verification must not be rewritten."""
    compact = [
        {
            "type": "verification",
            "headline": "audit auth",
            "body": "",
            "empty_headline": False,
            "failure": None,
        }
    ]
    assert _worker_submitted_structure(compact) is True
    status, summary = _analysis_bridge_status(
        compact, job_status="completed", summary="nothing to report"
    )
    assert status == "completed"
    assert summary == "nothing to report"


def test_native_analysis_brief_aligns_with_swarm_contract():
    inst = _analysis_instruction(
        "audit auth", "/repo", "explore", via_tool=False
    )
    assert "READ-ONLY" in inst
    assert "FINDING" in inst or "findings" in inst.lower()
    assert "submit_findings" not in inst
    assert "Now let me look" in inst  # negative example in the brief
    # Swarm tool brief still asks for submit_findings.
    tool_inst = _analysis_instruction("audit auth", "/repo", "explore", via_tool=True)
    assert "submit_findings" in tool_inst


def test_pick_analysis_message_prefers_structured_over_later_reasoning():
    from harness.worker import _pick_analysis_message

    events = [
        ConvEvent(
            "message",
            {
                "text": (
                    "FINDING: harness/worker.py:700 analysis empty-diff path "
                    "must reject reasoning-only output."
                )
            },
        ),
        ConvEvent(
            "message",
            {"text": "Now let me look at one more module..."},
        ),
        ConvEvent("auto_halt", {"reason": "stall: 5 steps with no new findings"}),
    ]
    text, halt = _pick_analysis_message(events)
    assert "FINDING:" in text
    assert "Now let me look" not in text
    assert "stall" in halt


def test_worker_degrade_keeps_diagnostic_summary(monkeypatch):
    """Contract fail keeps last message under summary for the pilot."""
    repo_dir = create_temp_git_repo()
    try:
        def mock_run_auto(self, objective, budget=None, require_codegraph=True, **kwargs):
            yield ConvEvent(
                "message",
                {"text": "Now let me look at the auth module more carefully..."},
            )
            yield ConvEvent("auto_halt", {"reason": "max turns"})

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)

        worker = ProviderWorker(
            repo=repo_dir,
            goal="Audit auth",
            expects_diff=False,
        )
        res = worker.run()
        assert res.ok is False
        assert "no structured findings" in (res.error or "")
        assert "Now let me look" in (res.summary or "")
    finally:
        shutil.rmtree(repo_dir)


def test_analysis_mode_skips_no_swarm_early_halt(monkeypatch):
    """Leaf analysis must not halt after one idle no-swarm cycle."""
    from harness.autobudget import AutoBudget
    from harness.config import HarnessConfig

    cfg = HarnessConfig()
    cfg.swarm_adapter = "demo"
    cfg.repo = ""
    session = ConversationalSession(cfg)
    cycles: list[str] = []

    def fake_send(self, msg):
        cycles.append(msg)
        if len(cycles) == 1:
            yield ConvEvent(
                "message",
                {"text": "Now let me look at the routing layer..."},
            )
            yield ConvEvent("assistant_done", {"turns": 1})
        else:
            yield ConvEvent(
                "message",
                {
                    "text": (
                        "FINDING: harness/worker.py:700 empty-diff analysis "
                        "must reject reasoning-only output."
                    )
                },
            )
            yield ConvEvent("assistant_done", {"turns": 2})

    monkeypatch.setattr(ConversationalSession, "send", fake_send)
    budget = AutoBudget(
        max_tokens=100000, max_seconds=60, max_swarms=2, max_idle_steps=5,
    )
    events = list(
        session.run_auto(
            "audit auth",
            budget=budget,
            require_codegraph=False,
            analysis_mode=True,
        )
    )
    halt = [e for e in events if e.kind == "auto_halt"][-1]
    assert "findings submitted" in (halt.data.get("reason") or "")
    assert len(cycles) >= 2


def test_implement_mode_still_halts_on_no_swarm(monkeypatch):
    from harness.autobudget import AutoBudget
    from harness.config import HarnessConfig

    cfg = HarnessConfig()
    cfg.swarm_adapter = "demo"
    cfg.repo = ""
    session = ConversationalSession(cfg)
    cycles: list[str] = []

    def fake_send(self, msg):
        cycles.append(msg)
        yield ConvEvent("message", {"text": "done looking"})
        yield ConvEvent("assistant_done", {"turns": 1})

    monkeypatch.setattr(ConversationalSession, "send", fake_send)
    budget = AutoBudget(
        max_tokens=100000, max_seconds=60, max_swarms=2, max_idle_steps=5,
    )
    events = list(
        session.run_auto(
            "implement foo",
            budget=budget,
            require_codegraph=False,
            analysis_mode=False,
        )
    )
    halt = [e for e in events if e.kind == "auto_halt"][-1]
    assert "objective met" in (halt.data.get("reason") or "")
    assert len(cycles) == 1


def test_analysis_mode_leaf_tools_do_not_burn_swarm_ceiling(monkeypatch):
    """read_file x3 must not trip max_swarms=2; FINDING summary can still land."""
    from harness.autobudget import AutoBudget
    from harness.config import HarnessConfig

    cfg = HarnessConfig()
    cfg.swarm_adapter = "demo"
    cfg.repo = ""
    session = ConversationalSession(cfg)
    cycles: list[str] = []

    def fake_send(self, msg):
        cycles.append(msg)
        if len(cycles) == 1:
            for i in range(3):
                yield ConvEvent("action_start", {
                    "id": f"r{i}", "kind": "read_file", "goal": f"f{i}.py",
                })
                yield ConvEvent("action_result", {
                    "id": f"r{i}", "kind": "read_file", "goal": f"f{i}.py",
                    "status": "complete",
                })
            yield ConvEvent(
                "message",
                {"text": "Now let me synthesize the three files..."},
            )
            yield ConvEvent("assistant_done", {"turns": 1})
        else:
            yield ConvEvent(
                "message",
                {
                    "text": (
                        "FINDING: harness/worker.py:700 empty-diff analysis "
                        "must reject reasoning-only output."
                    )
                },
            )
            yield ConvEvent("assistant_done", {"turns": 2})

    monkeypatch.setattr(ConversationalSession, "send", fake_send)
    budget = AutoBudget(
        max_tokens=100000, max_seconds=60, max_swarms=2, max_idle_steps=5,
    )
    events = list(
        session.run_auto(
            "audit three files",
            budget=budget,
            require_codegraph=False,
            analysis_mode=True,
        )
    )
    halt = [e for e in events if e.kind == "auto_halt"][-1]
    reason = halt.data.get("reason") or ""
    assert "swarm ceiling" not in reason
    assert "findings submitted" in reason
    assert budget.swarms_used == 0
    assert len(cycles) >= 2
