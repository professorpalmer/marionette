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
    coerce_unlabeled_analysis_prose,
    parse_analysis_signal_rows,
)
from pmharness.bridge import (
    _analysis_bridge_status,
    _analysis_instruction,
    _compact_artifact,
    _has_real_structured_findings,
    _looks_like_reasoning_fragment,
    _promote_degraded_prose,
    _worker_submitted_structure,
    looks_like_reasoning_fragment,
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
    assert looks_like_reasoning_fragment("Now let me look at the auth module...")
    assert looks_like_reasoning_fragment("Let me check harness/worker.py next")
    assert not looks_like_reasoning_fragment(
        "FINDING: harness/worker.py:680 empty-diff analysis accepts reasoning"
    )
    assert not looks_like_reasoning_fragment(
        "Audit complete: no issues found in auth."
    )
    # Private alias stays wired to the public contract.
    assert _looks_like_reasoning_fragment is looks_like_reasoning_fragment


def test_worker_and_bridge_agree_on_reasoning_openers():
    """Worker structured gate and bridge helper share the full prefix list."""
    longer_list_only = (
        "Let me examine the auth module next",
        "Hmm, this looks like a race in the queue",
        "Okay, let me dig into the retry path",
    )
    for opener in longer_list_only:
        assert looks_like_reasoning_fragment(opener) is True
        ok, reason = _analysis_output_is_structured(opener)
        assert ok is False, opener
        assert "reasoning" in reason, opener
        # Soft-rescue must not wrap reasoning openers as FINDING rows.
        assert coerce_unlabeled_analysis_prose(opener) == opener


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

    # Outer gate failures fail closed (never paint structured on crash).
    ok4, reason4 = _analysis_output_is_structured(object())  # type: ignore[arg-type]
    assert ok4 is False
    assert "no structured findings" in reason4


def test_analysis_output_rejects_unlabeled_prose():
    """Non-reasoning free text without FINDING/RISK/DECISION labels must fail."""
    ok, reason = _analysis_output_is_structured(
        "The auth module looks fine overall."
    )
    assert ok is False
    assert "missing FINDING/RISK/DECISION" in reason

    ok_risk, reason_risk = _analysis_output_is_structured(
        "RISK: sessions never expire after logout in harness/auth.py:42"
    )
    assert ok_risk is True
    assert reason_risk == ""


def test_parse_analysis_signal_rows_extracts_typed_labels():
    rows = parse_analysis_signal_rows(
        "Preface.\n"
        "FINDING: token refresh skips expiry checks in harness/auth.py:42\n"
        "risk: cookie jar is shared across workers\n"
        "DECISION: prefer typed labels over prose\n"
        "Still unlabeled trailing prose."
    )
    assert [r["type"] for r in rows] == ["finding", "risk", "decision"]
    assert "token refresh" in rows[0]["headline"]
    assert "cookie jar" in rows[1]["headline"]
    assert "typed labels" in rows[2]["headline"]
    # Single-line signals: body matches the uncapped first-line content.
    assert rows[0]["body"] == rows[0]["headline"]
    assert rows[1]["body"] == rows[1]["headline"]
    assert "typed labels" in rows[2]["body"]
    assert parse_analysis_signal_rows("The auth module looks fine overall.") == []


def test_parse_analysis_signal_rows_keeps_multiline_coerced_finding_body():
    """coerce→parse must not drop paragraphs after the FINDING first line."""
    paragraph_1 = (
        "harness/worker.py:483 coerce_unlabeled_analysis_prose prefixes "
        "FINDING onto multi-line prose."
    )
    paragraph_2 = (
        "parse_analysis_signal_rows was line-anchored and kept only the "
        "first line as the finding headline."
    )
    paragraph_3 = (
        "Job artifacts must retain paragraphs 2 and 3 in the signal body "
        "and payload.report so the full audit text is not truncated."
    )
    prose = f"{paragraph_1}\n\n{paragraph_2}\n\n{paragraph_3}"
    coerced = coerce_unlabeled_analysis_prose(prose)
    assert coerced.startswith("FINDING: ")
    assert paragraph_2 in coerced and paragraph_3 in coerced

    rows = parse_analysis_signal_rows(coerced)
    assert len(rows) == 1
    assert rows[0]["type"] == "finding"
    assert paragraph_1 in rows[0]["headline"]
    assert "\n" not in rows[0]["headline"]
    assert paragraph_2 in rows[0]["body"]
    assert paragraph_3 in rows[0]["body"]
    assert rows[0]["body"].startswith(paragraph_1)


def test_parse_analysis_signal_rows_accepts_last_assistant_message_wrapper():
    text = (
        "Last assistant message: FINDING: first paragraph cites "
        "harness/auth.py:42\n"
        "\n"
        "Second paragraph elaborates the expiry race.\n"
        "\n"
        "Third paragraph recommends a typed label contract."
    )
    rows = parse_analysis_signal_rows(text)
    assert len(rows) == 1
    assert rows[0]["type"] == "finding"
    assert "first paragraph" in rows[0]["headline"]
    assert "Second paragraph" in rows[0]["body"]
    assert "Third paragraph" in rows[0]["body"]


def test_analysis_signal_rows_for_job_puts_body_in_report():
    """WorkerResult.findings body must land in job artifact payload.report."""
    from harness.conversation_jobs import _analysis_signal_rows_for_job

    class _Res:
        findings = [
            {
                "type": "finding",
                "headline": "paragraph one cites harness/worker.py:483",
                "body": (
                    "paragraph one cites harness/worker.py:483\n\n"
                    "paragraph two explains the truncate bug.\n\n"
                    "paragraph three is the remediation note."
                ),
            }
        ]

    rows = _analysis_signal_rows_for_job(_Res(), summary_text="")
    assert len(rows) == 1
    assert rows[0]["headline"] == "paragraph one cites harness/worker.py:483"
    assert "paragraph two" in rows[0]["body"]
    assert "paragraph three" in rows[0]["body"]

    # Mirror the job-artifact mapping used when expects_diff=False succeeds.
    artifacts = [
        {
            "type": row["type"],
            "payload": {
                "claim": row.get("headline") or "",
                "report": row.get("body") or row.get("headline") or "",
            },
        }
        for row in rows
    ]
    report = artifacts[0]["payload"]["report"]
    assert artifacts[0]["payload"]["claim"] == rows[0]["headline"]
    assert "paragraph two" in report
    assert "paragraph three" in report


def test_analysis_signal_rows_for_job_fallback_parse_preserves_body():
    from harness.conversation_jobs import _analysis_signal_rows_for_job

    class _Res:
        findings = []

    summary = (
        "FINDING: first line of coerced finding at harness/keys.py:12\n"
        "\n"
        "Second paragraph remains visible in the job report.\n"
        "\n"
        "Third paragraph must not be dropped by the summary fallback path."
    )
    rows = _analysis_signal_rows_for_job(_Res(), summary_text=summary)
    assert len(rows) == 1
    assert "first line" in rows[0]["headline"]
    assert "Second paragraph" in rows[0]["body"]
    assert "Third paragraph" in rows[0]["body"]


def test_analysis_mode_gate_crash_does_not_early_halt(monkeypatch):
    """run_auto analysis must not halt as findings-submitted if the gate raises."""
    from harness.autobudget import AutoBudget
    from harness.config import HarnessConfig
    import harness.worker as worker_mod

    cfg = HarnessConfig()
    cfg.swarm_adapter = "demo"
    cfg.repo = ""
    session = ConversationalSession(cfg)
    cycles: list[str] = []

    def fake_send(self, msg):
        cycles.append(msg)
        if len(cycles) == 1:
            yield ConvEvent("message", {"text": "non-empty mid-thought prose"})
            yield ConvEvent("assistant_done", {"turns": 1})
        else:
            yield ConvEvent(
                "message",
                {
                    "text": (
                        "FINDING: harness/conversation.py:2955 fail-closed "
                        "structured gate on exception."
                    )
                },
            )
            yield ConvEvent("assistant_done", {"turns": 2})

    # First cycle: gate raises (must not early-halt). Later: real gate so FINDING lands.
    real_gate = _analysis_output_is_structured
    calls = {"n": 0}

    def gate_then_real(last_message, *, halt_reason=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gate exploded")
        return real_gate(last_message, halt_reason=halt_reason)

    monkeypatch.setattr(worker_mod, "_analysis_output_is_structured", gate_then_real)
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
    assert calls["n"] >= 2


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
    assert "REQUIRED OUTPUT FORMAT" in inst
    assert "FINDING: path/to/file.py:123" in inst
    assert "RISK: path/to/file.py:45" in inst
    assert "DECISION: keep X because Y" in inst
    # Swarm tool brief still asks for submit_findings + same format block.
    tool_inst = _analysis_instruction("audit auth", "/repo", "explore", via_tool=True)
    assert "submit_findings" in tool_inst
    assert "REQUIRED OUTPUT FORMAT" in tool_inst
    assert "FINDING: path/to/file.py:123" in tool_inst
    assert "type finding/risk/decision" in tool_inst


def test_coerce_unlabeled_substantive_prose_becomes_structured():
    prose = (
        "harness/worker.py:700 empty-diff analysis accepts unlabeled "
        "substantive prose that cites a concrete path:line locus."
    )
    coerced = coerce_unlabeled_analysis_prose(prose)
    assert coerced.startswith("FINDING: ")
    assert prose in coerced
    ok, reason = _analysis_output_is_structured(coerced)
    assert ok is True
    assert reason == ""
    rows = parse_analysis_signal_rows(coerced)
    assert len(rows) == 1
    assert rows[0]["type"] == "finding"


def test_coerce_leaves_reasoning_and_labelled_unchanged():
    reasoning = "Now let me look at the auth module more carefully..."
    assert coerce_unlabeled_analysis_prose(reasoning) == reasoning
    ok, reason = _analysis_output_is_structured(reasoning)
    assert ok is False
    assert "reasoning" in reason

    labelled = "FINDING: harness/keys.py:12 leaks the API key into logs"
    assert coerce_unlabeled_analysis_prose(labelled) == labelled

    thin = "The auth module looks fine overall."
    assert coerce_unlabeled_analysis_prose(thin) == thin


def test_analysis_degrade_label_prefers_token_ceiling():
    from harness.worker import _analysis_degrade_label

    label = _analysis_degrade_label(
        "no structured findings",
        "token ceiling reached (257173/250000)",
    )
    assert "token ceiling reached" in label
    assert "no structured findings" in label


def test_worker_token_ceiling_surfaces_in_error(monkeypatch):
    repo_dir = create_temp_git_repo()
    try:
        def mock_run_auto(self, objective, budget=None, require_codegraph=True, **kwargs):
            yield ConvEvent(
                "message",
                {"text": "Now let me look at a few more modules..."},
            )
            yield ConvEvent(
                "auto_halt",
                {"reason": "token ceiling reached (257173/250000)"},
            )

        monkeypatch.setattr(ConversationalSession, "run_auto", mock_run_auto)
        worker = ProviderWorker(
            repo=repo_dir, goal="broad audit", expects_diff=False,
        )
        res = worker.run()
        assert res.ok is False
        assert "token ceiling reached" in (res.error or "")
    finally:
        shutil.rmtree(repo_dir)


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
