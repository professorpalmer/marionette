from __future__ import annotations

"""Focused harness tests for the experimental compaction residual switch."""

from harness.compaction_mixin import (
    REASON_RESIDUAL_OFF,
    MIN_SUMMARY_SEED_CHARS,
)
from harness.compaction_residual import (
    CATALOG_HEADING,
    HYBRID_INDEX_HEADING,
    RESIDUAL_CATALOG,
    RESIDUAL_HYBRID,
    RESIDUAL_OFF,
    RESIDUAL_SUMMARY,
    build_catalog_residual,
    compaction_residual_mode,
    extract_handle_index,
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


_GOOD_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Compaction fixture summary with enough seed characters to pass guards.\n"
    "## Resolved\nPrior turns were compacted for the unit test.\n"
    "## Pending / Open Questions\nNone.\n"
    "## Key Facts / Decisions / Files\ntests/test_compaction_residual.py\n"
)


class MockPilot:
    name = "mock"

    def __init__(self, return_text=_GOOD_SUMMARY):
        self.return_text = return_text
        self.chat_calls = []
        self.complete_calls = []

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system))
        return type("Resp", (), {"text": self.return_text, "error": None, "tokens_out": 10})()

    def complete(self, prompt, system=None):
        self.complete_calls.append((prompt, system))
        return type("Resp", (), {"text": self.return_text, "error": None, "tokens_out": 10})()


def _fat_history(session: ConversationalSession, *, secret: str = "") -> None:
    session._history = [{"role": "system", "content": "sys"}]
    session._history.append({
        "role": "user",
        "content": "CONSTRAINT: keep src/billing/ledger_v3.py as the source of truth.",
    })
    session._history.append({
        "role": "assistant",
        "content": "reading",
        "tool_calls": [{
            "id": "call_r",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path": "src/billing/ledger_v3.py"}',
            },
        }],
    })
    tool_body = (
        "src/billing/ledger_v3.py contents. "
        "full: spill://sess-lab/result-omega artifact://job-omega/finding-0 "
        "job://job-omega"
    )
    if secret:
        tool_body += (
            f"\nERROR: failed with api_key={secret} token sk-abcdefghijklmnopqrstuvwx"
        )
    session._history.append({
        "role": "tool",
        "tool_call_id": "call_r",
        "content": tool_body,
        "_read_path": "src/billing/ledger_v3.py",
    })
    for i in range(8):
        session._history.append({
            "role": "user",
            "content": f"User message number {i}: " + ("A" * 150),
        })
        session._history.append({
            "role": "assistant",
            "content": f"Assistant response number {i}: " + ("B" * 150),
        })
    session._history.append({"role": "user", "content": "please continue"})
    session._history.append({"role": "assistant", "content": "continuing"})


def _session(tmp_path, monkeypatch, budget: int = 4000) -> ConversationalSession:
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    cfg = HarnessConfig(max_context_tokens=budget, state_dir=str(tmp_path))
    session = ConversationalSession(cfg)
    session.harness_session_id = "sess-residual"
    session.pilot = MockPilot()
    return session


def test_residual_mode_default_and_empty_never_off(monkeypatch):
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    assert compaction_residual_mode() == RESIDUAL_SUMMARY
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "")
    assert compaction_residual_mode() == RESIDUAL_SUMMARY
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "   ")
    assert compaction_residual_mode() == RESIDUAL_SUMMARY
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "nope")
    assert compaction_residual_mode() == RESIDUAL_SUMMARY
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "off")
    assert compaction_residual_mode() == RESIDUAL_OFF
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "CATALOG")
    assert compaction_residual_mode() == RESIDUAL_CATALOG


def test_default_summary_mode_unchanged(tmp_path, monkeypatch):
    """Unset residual switch keeps the existing LLM summary path (mode=llm)."""
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    session = _session(tmp_path, monkeypatch)
    _fat_history(session)
    events = list(session._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    assert events[-1].data.get("mode") == "llm"
    assert session.pilot.chat_calls
    assert session._history[1].get("_compressed_summary") is True
    assert "[Earlier conversation summarized to fit context]" in session._history[1]["content"]
    assert "Compaction fixture summary" in session._history[1]["content"]
    assert session._history[-1]["content"] == "continuing"


def test_off_is_no_compaction_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "off")
    session = _session(tmp_path, monkeypatch)
    _fat_history(session)
    original = list(session._history)
    events = list(session._maybe_compact_history(force=True))
    assert events == []
    assert session._history == original
    assert session._last_compaction_attempt.get("reason") == REASON_RESIDUAL_OFF
    assert not session.pilot.chat_calls


def test_catalog_extractive_no_llm_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    session = _session(tmp_path, monkeypatch)
    secret = "supersecret-residual-key"
    _fat_history(session, secret=secret)
    events = list(session._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    # Deterministic catalog/hybrid paths emit mode=extractive (not llm).
    assert events[-1].data.get("mode") == "extractive"
    assert not session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert CATALOG_HEADING in injected
    assert "src/billing/ledger_v3.py" in injected
    assert "read_file" in injected
    assert "spill://sess-lab/result-omega" in injected
    assert "artifact://job-omega/finding-0" in injected
    assert "job://job-omega" in injected
    assert "never invent turn" not in injected.lower()
    assert secret not in injected
    assert "sk-abcdefghijklmnopqrstuvwx" not in injected
    assert "REDACTED" in injected
    assert session._history[-1]["content"] == "continuing"


def test_hybrid_keeps_four_headings_and_handle_index(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "hybrid")
    session = _session(tmp_path, monkeypatch)
    _fat_history(session)
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data.get("mode") == "extractive"
    assert not session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert "## Historical Task Snapshot" in injected
    assert "## Resolved" in injected
    assert "## Pending / Open Questions" in injected
    assert "## Key Facts / Decisions / Files" in injected
    assert HYBRID_INDEX_HEADING in injected
    assert "src/billing/ledger_v3.py" in injected
    assert "spill://sess-lab/result-omega" in injected


def test_hybrid_redacts_secrets_in_body_and_index(tmp_path, monkeypatch):
    """Hybrid body and handle index must redact secrets like catalog mode."""
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "hybrid")
    session = _session(tmp_path, monkeypatch)
    secret = "supersecret-hybrid-residual-key"
    _fat_history(session, secret=secret)
    session._history[-2]["content"] = (
        f"please continue with api_key={secret} token sk-zyxwvutsrqponmlkjihgfedcba"
    )
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data.get("mode") == "extractive"
    assert not session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert "## Historical Task Snapshot" in injected
    assert HYBRID_INDEX_HEADING in injected
    assert "src/billing/ledger_v3.py" in injected
    assert secret not in injected
    assert "sk-abcdefghijklmnopqrstuvwx" not in injected
    assert "sk-zyxwvutsrqponmlkjihgfedcba" not in injected
    assert "REDACTED" in injected


def test_catalog_extraction_is_unique_handle_not_per_message():
    middle = []
    for i in range(12):
        middle.append({
            "role": "user",
            "content": f"again src/billing/ledger_v3.py mention {i}",
        })
        middle.append({
            "role": "assistant",
            "content": "read_file again spill://sess-lab/result-omega",
            "tool_calls": [{
                "id": f"c{i}",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        })
    index = extract_handle_index(middle)
    assert index["files"] == ["src/billing/ledger_v3.py"]
    assert index["tools"] == ["read_file"]
    assert index["handles"] == ["spill://sess-lab/result-omega"]
    catalog = build_catalog_residual(middle, char_budget=2000)
    assert catalog.count("src/billing/ledger_v3.py") <= 3
    assert len(catalog) >= MIN_SUMMARY_SEED_CHARS


def test_repeated_catalog_compaction_stays_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    session = _session(tmp_path, monkeypatch, budget=8000)
    _fat_history(session)
    list(session._maybe_compact_history(force=True))
    first = session._history[1]["content"]
    first_len = len(first)
    for wave in range(3):
        for i in range(6):
            session._history.append({
                "role": "user",
                "content": f"wave-{wave} again src/billing/ledger_v3.py " + ("Z" * 120),
            })
            session._history.append({
                "role": "assistant",
                "content": "still spill://sess-lab/result-omega " + ("Y" * 120),
            })
        list(session._maybe_compact_history(force=True))
    later = session._history[1]["content"]
    assert CATALOG_HEADING in later
    # Unique-handle residual must not grow with message count / re-compaction.
    assert later.count("src/billing/ledger_v3.py") <= 4
    assert later.count("spill://sess-lab/result-omega") <= 4
    assert len(later) < first_len * 3
