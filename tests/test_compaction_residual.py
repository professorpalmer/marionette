from __future__ import annotations

"""Focused harness tests for the experimental compaction residual switch."""

from harness.compaction_mixin import (
    REASON_RESIDUAL_OFF,
    MIN_SUMMARY_SEED_CHARS,
)
from harness.compaction_residual import (
    CATALOG_HEADING,
    HYBRID_INDEX_HEADING,
    SELECTED_STORY_HEADING,
    RESIDUAL_CATALOG,
    RESIDUAL_HYBRID,
    RESIDUAL_OFF,
    RESIDUAL_SUMMARY,
    build_catalog_residual,
    build_hybrid_index,
    compaction_residual_mode,
    extract_handle_index,
    settings_residual_choice,
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
    assert compaction_residual_mode() == RESIDUAL_CATALOG
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "")
    assert compaction_residual_mode() == RESIDUAL_CATALOG
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "   ")
    assert compaction_residual_mode() == RESIDUAL_CATALOG
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "nope")
    assert compaction_residual_mode() == RESIDUAL_CATALOG
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "off")
    assert compaction_residual_mode() == RESIDUAL_OFF
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "CATALOG")
    assert compaction_residual_mode() == RESIDUAL_CATALOG
    assert settings_residual_choice() == RESIDUAL_CATALOG
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "hybrid")
    assert settings_residual_choice() == RESIDUAL_HYBRID


def test_default_catalog_mode_is_extractive(tmp_path, monkeypatch):
    """Unset residual switch uses the catalog path (mode=extractive)."""
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    session = _session(tmp_path, monkeypatch)
    _fat_history(session)
    events = list(session._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    assert events[-1].data.get("mode") == "extractive"
    assert not session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert session._history[1].get("_compressed_summary") is True
    assert "[Earlier conversation summarized to fit context]" in injected
    assert "compaction_generation=" in injected
    assert CATALOG_HEADING in injected
    assert "Compaction fixture summary" not in injected
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


def test_unwrap_peels_injected_generation_notice():
    """Re-compaction must not stack compaction_generation lines."""
    session = ConversationalSession(HarnessConfig())
    nested = (
        "[Earlier conversation summarized to fit context]\n"
        "compaction_generation=2. peek_history: omit expected_generation or pass this value.\n"
        "[Earlier conversation summarized to fit context]\n"
        "compaction_generation=1. peek_history: omit expected_generation or pass this value.\n"
        "inner summary body"
    )
    assert session._unwrap_prior_summary_content(nested) == "inner summary body"


def test_catalog_extractive_no_llm_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    session = _session(tmp_path, monkeypatch)
    secret = "supersecret-residual-key"
    _fat_history(session, secret=secret)
    events = list(session._maybe_compact_history(force=True))
    assert [e.kind for e in events] == ["compacting", "compaction"]
    # Catalog is extractive and must not call the summarizer.
    assert events[-1].data.get("mode") == "extractive"
    assert not session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert "compaction_generation=" in injected
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
    from harness.compaction_vault import retrieve_vault_chunks

    vault_hits = retrieve_vault_chunks(
        str(tmp_path),
        "sess-residual",
        "What is the source of truth for the billing ledger?",
    )
    assert any("ledger_v3.py" in hit for hit in vault_hits)


def test_hybrid_keeps_four_headings_and_handle_index(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "hybrid")
    session = _session(tmp_path, monkeypatch)
    _fat_history(session)
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data.get("mode") == "llm"
    assert session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert "compaction_generation=" in injected
    assert "## Historical Task Snapshot" in injected
    assert "## Resolved" in injected
    assert "## Pending / Open Questions" in injected
    assert "## Key Facts / Decisions / Files" in injected
    assert HYBRID_INDEX_HEADING in injected
    assert "- stems:" in injected
    assert "src/billing/ledger_v3.py" in injected
    assert "spill://sess-lab/result-omega" in injected
    assert "Compaction fixture summary" in injected
    assert SELECTED_STORY_HEADING in injected


def _compact_summary_case(tmp_path, monkeypatch, case_id, return_text=_GOOD_SUMMARY):
    from pmharness.compaction_residual_battery import cases_by_id

    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "summary")
    session = _session(tmp_path, monkeypatch)
    session.pilot = MockPilot(return_text=return_text)
    case = cases_by_id()[case_id]
    session._history = [dict(row) for row in case.transcript]
    session._history.append({"role": "user", "content": "please continue"})
    session._history.append({"role": "assistant", "content": "continuing"})
    events = list(session._maybe_compact_history(force=True))
    return session, events


def test_summary_skips_ack_and_pins_last_wins_story(tmp_path, monkeypatch):
    """Ack-only 'Reversed.' must not reach the summarizer or undo last-wins."""
    session, events = _compact_summary_case(tmp_path, monkeypatch, "unprefixed_reversal")
    assert events[-1].data.get("mode") == "llm"
    assert session.pilot.chat_calls
    prompt = session.pilot.chat_calls[0][0][0]["content"]
    system = session.pilot.chat_calls[0][1]
    assert "Reversed." not in prompt
    assert "Later decisions replace" in system
    injected = session._history[1]["content"]
    assert SELECTED_STORY_HEADING in injected
    assert "write to the live ledger now" in injected.lower()
    assert "don't write" not in injected.lower()


def test_summary_lying_paragraph_still_pins_later_policy(tmp_path, monkeypatch):
    """A rollback paragraph must not hide the extractive last-wins story."""
    lying = (
        _GOOD_SUMMARY
        + "Current policy: do not write to the live ledger because it was reversed.\n"
    )
    session, events = _compact_summary_case(
        tmp_path, monkeypatch, "unprefixed_reversal", return_text=lying
    )
    assert events[-1].data.get("mode") == "llm"
    injected = session._history[1]["content"]
    assert SELECTED_STORY_HEADING in injected
    assert "write to the live ledger now" in injected.lower()
    story = injected.split(SELECTED_STORY_HEADING, 1)[1]
    assert "don't write" not in story.lower()
    assert "do not write" not in story.lower()


def test_summary_obligation_keeps_first_policy(tmp_path, monkeypatch):
    session, events = _compact_summary_case(
        tmp_path, monkeypatch, "unprefixed_obligation"
    )
    assert events[-1].data.get("mode") == "llm"
    injected = session._history[1]["content"]
    assert SELECTED_STORY_HEADING in injected
    assert "don't write to the live ledger" in injected.lower()
    assert "east replica is the only sink" in injected.lower()
    assert "retired" not in injected.lower()


def test_hybrid_degenerate_pilot_falls_back_extractively(tmp_path, monkeypatch):
    """Failed / degenerate hybrid summarizer uses extractive + handle index."""
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "hybrid")
    session = _session(tmp_path, monkeypatch)
    session.pilot = MockPilot(return_text="too short")
    _fat_history(session)
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data.get("mode") == "extractive"
    assert session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert "too short" not in injected
    assert "## Historical Task Snapshot" in injected
    assert "## Resolved" in injected
    assert "## Pending / Open Questions" in injected
    assert "## Key Facts / Decisions / Files" in injected
    assert HYBRID_INDEX_HEADING in injected
    assert "src/billing/ledger_v3.py" in injected
    assert "spill://sess-lab/result-omega" in injected
    assert "middle messages compressed to task facts" in injected


def test_hybrid_error_pilot_falls_back_extractively(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "hybrid")
    session = _session(tmp_path, monkeypatch)

    class ErrorPilot(MockPilot):
        def chat(self, messages, tools=None, system=None):
            self.chat_calls.append((messages, system))
            return type("Resp", (), {"text": "", "error": "simulated hybrid fail", "tokens_out": 0})()

    session.pilot = ErrorPilot()
    _fat_history(session)
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data.get("mode") == "extractive"
    assert session.pilot.chat_calls
    injected = session._history[1]["content"]
    assert HYBRID_INDEX_HEADING in injected
    assert "## Historical Task Snapshot" in injected
    assert "src/billing/ledger_v3.py" in injected


def test_hybrid_redacts_secrets_in_body_and_index(tmp_path, monkeypatch):
    """Hybrid LLM body and handle index must redact secrets like catalog mode."""
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "hybrid")
    session = _session(tmp_path, monkeypatch)
    secret = "supersecret-hybrid-residual-key"
    leaky = (
        _GOOD_SUMMARY
        + f"\napi_key={secret} token sk-abcdefghijklmnopqrstuvwx\n"
    )
    session.pilot = MockPilot(return_text=leaky)
    _fat_history(session, secret=secret)
    session._history[1]["content"] = (
        "CONSTRAINT: keep src/billing/ledger_v3.py; "
        f"api_key={secret} token sk-zyxwvutsrqponmlkjihgfedcba"
    )
    events = list(session._maybe_compact_history(force=True))
    assert events[-1].data.get("mode") == "llm"
    assert session.pilot.chat_calls
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


def _washout_middle() -> list[dict]:
    """Raw middle with the catalog-washout tokens from the residual lab."""
    return [
        {
            "role": "user",
            "content": "Decision: use SQLite instead of Redis",
        },
        {
            "role": "user",
            "content": "never write to production.db; keep scratch.sqlite as the scratch store.",
        },
        {
            "role": "assistant",
            "content": "inspect src/billing/ledger_v3.py",
            "tool_calls": [{
                "id": "call_ledger",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path": "src/billing/ledger_v3.py"}',
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_ledger",
            "content": (
                "ERROR: ConfigMissing: /etc/marionette/secret-policy.yaml "
                "not found (code E-7721) spill://sess-lab/result-omega"
            ),
            "_read_path": "scratch.sqlite",
        },
        {
            "role": "tool",
            "content": "bare production pointer",
            "_read_path": "production.db",
        },
        {
            "role": "tool",
            "content": "active auth module",
            "_read_path": "auth_current_v2.py",
        },
        {
            "role": "tool",
            "content": (
                "Plain measurement only: omega-cache-token-9f3a observed "
                "on shard-omega-p95."
            ),
        },
    ]


def _assert_closed_loop(first: dict, second: dict, keys: tuple[str, ...]) -> None:
    for key in keys:
        missing = [item for item in first[key] if item not in second[key]]
        assert missing == [], f"{key} washed out on re-extract: {missing}"


def _index_blob(index: dict) -> str:
    return "\n".join(
        list(index["files"])
        + list(index["tools"])
        + list(index["handles"])
        + list(index["stems"])
        + list(index.get("facts") or [])
    )


def test_stem_and_error_regexes_accept_markdown_bullets():
    """Catalog-style '- STEM' bullets must still extract without inventing IDs."""
    middle = [{
        "role": "assistant",
        "content": (
            "- Decision: use SQLite instead of Redis\n"
            "- never write to production.db\n"
            "- error: ConfigMissing secret-policy.yaml (code E-7721)\n"
        ),
    }]
    index = extract_handle_index(middle)
    blob = _index_blob(index)
    assert "Decision: use SQLite instead of Redis" in blob
    assert "never write to production.db" in blob
    assert "E-7721" in blob
    assert "turn-" not in blob


def test_catalog_reextraction_preserves_stems_and_bare_files():
    """Re-extracting a catalog residual must not wash out stems or bare files."""
    middle = _washout_middle()
    first = extract_handle_index(middle)
    first_blob = _index_blob(first)
    assert "Decision: use SQLite instead of Redis" in first_blob
    assert "never write to production.db" in first_blob
    assert "production.db" in first_blob
    assert "scratch.sqlite" in first_blob
    assert "src/billing/ledger_v3.py" in first["files"]
    assert "spill://sess-lab/result-omega" in first["handles"]
    assert "E-7721" in first_blob or "secret-policy.yaml" in first_blob

    catalog = build_catalog_residual(middle, char_budget=4000)
    assert CATALOG_HEADING in catalog
    second = extract_handle_index([{
        "role": "assistant",
        "content": catalog,
        "_compressed_summary": True,
    }])
    _assert_closed_loop(first, second, ("files", "tools", "handles", "stems", "facts"))
    second_blob = _index_blob(second)
    assert "omega-cache-token-9f3a" in second["facts"]
    assert "shard-omega-p95" in second["facts"]
    assert "Decision: use SQLite instead of Redis" in second_blob
    assert "never write to production.db" in second_blob
    assert "production.db" in second_blob
    assert "scratch.sqlite" in second_blob
    assert "E-7721" in second_blob or "secret-policy.yaml" in second_blob
    assert "auth_current_v2.py" in second["files"]
    assert "(no file pointers found)" not in second["files"]
    assert "(no error/decision/constraint stems)" not in second["stems"]


def test_hybrid_stems_line_keeps_inner_semicolons():
    """A stems line must split only at a new stem prefix, not every '; '."""
    residual = (
        f"{HYBRID_INDEX_HEADING}\n"
        "- files (0): (none)\n"
        "- tools: (none)\n"
        "- uris: (none)\n"
        "- stems: Decision: keep Redis; never write to production.db; "
        "error: failed: timeout; retry later\n"
    )
    index = extract_handle_index([{
        "role": "assistant",
        "content": residual,
        "_compressed_summary": True,
    }])
    assert index["stems"] == [
        "Decision: keep Redis",
        "never write to production.db",
        "error: failed: timeout; retry later",
    ]


def test_hybrid_index_reextraction_preserves_classified_handles():
    """Re-extracting a hybrid appendix must recover files / tools / URIs / stems."""
    middle = _washout_middle()
    first = extract_handle_index(middle)
    hybrid = build_hybrid_index(middle)
    assert HYBRID_INDEX_HEADING in hybrid
    assert "- stems:" in hybrid
    assert "- facts:" in hybrid
    second = extract_handle_index([{
        "role": "assistant",
        "content": hybrid,
        "_compressed_summary": True,
    }])
    _assert_closed_loop(first, second, ("files", "tools", "handles", "stems", "facts"))
    assert "production.db" in second["files"]
    assert "scratch.sqlite" in second["files"]
    assert "auth_current_v2.py" in second["files"]
    assert "src/billing/ledger_v3.py" in second["files"]
    assert "read_file" in second["tools"]
    assert "spill://sess-lab/result-omega" in second["handles"]
    second_blob = _index_blob(second)
    assert "Decision: use SQLite instead of Redis" in second_blob
    assert "never write to production.db" in second_blob
    assert "(none)" not in second["files"]
    assert "(none)" not in second["tools"]
    assert "(none)" not in second["handles"]
    assert "(none)" not in second["stems"]
    assert "(none)" not in second["facts"]
    assert "omega-cache-token-9f3a" in second["facts"]
    assert "shard-omega-p95" in second["facts"]


def test_file_stem_lookalikes_are_not_facts():
    """auth_legacy_v1 must not leak into facts from distractor prose."""
    middle = [
        {"role": "user", "content": "Ignore auth_legacy_v1.py; it is the retired twin."},
        {"role": "user", "content": "Read auth_current_v2.py — that is the active auth module."},
    ]
    index = extract_handle_index(middle)
    blob = " ".join(index["facts"])
    assert "auth_legacy_v1" not in blob
    assert "auth_current_v2" not in blob


def test_hybrid_index_keeps_newest_stems_under_cap():
    """Hybrid appendix must take the stem tail, not the oldest six."""
    middle = []
    for i in range(12):
        middle.append({"role": "user", "content": f"never touch filler-file-{i}-zz."})
    middle.append({
        "role": "user",
        "content": "Decision: use SQLite instead of Redis for the session store.",
    })
    hybrid = build_hybrid_index(middle)
    assert "SQLite instead of Redis" in hybrid


def test_last_wins_stems_keep_late_decision_after_cap():
    middle = []
    for i in range(12):
        middle.append({"role": "user", "content": f"never touch filler-file-{i}-zz."})
    middle.append({
        "role": "user",
        "content": "Decision: use SQLite instead of Redis for the session store.",
    })
    index = extract_handle_index(middle)
    blob = " ".join(index["stems"]).lower()
    assert "sqlite instead of redis" in blob
    assert len(index["stems"]) <= 10


def test_version_and_ticket_harvest():
    middle = [
        {
            "role": "user",
            "content": "Pin marionette to 0.9.187 and see ticket E-7721.",
        },
    ]
    index = extract_handle_index(middle)
    assert "0.9.187" in index["facts"]
    assert "E-7721" in index["facts"]


def test_obligation_harvest_keeps_later_reversal():
    middle = [
        {
            "role": "user",
            "content": (
                "please don't write to the live ledger; "
                "the east replica is the only sink."
            ),
        },
        {
            "role": "user",
            "content": (
                "go ahead and write to the live ledger now; "
                "the east replica is retired."
            ),
        },
    ]
    index = extract_handle_index(middle)
    blob = " ".join(index["stems"] + index["story"]).lower()
    assert "write to the live ledger now" in blob
    assert "east replica is retired" in blob
    assert "don't write" not in blob
    assert "only sink" not in blob


def test_obligation_harvest_folds_unicode_apostrophes():
    middle = [{
        "role": "user",
        "content": "please don\u2019t write to the live ledger.",
    }]
    index = extract_handle_index(middle)
    blob = " ".join(index["stems"]).lower()
    assert "live ledger" in blob


def test_obligation_harvest_keeps_unprefixed_policy_lines():
    """Unprefixed don't / the-only lines enter stems without a Decision: prefix."""
    middle = [
        {
            "role": "user",
            "content": (
                "please don't write to the live ledger; "
                "the east replica is the only sink."
            ),
        },
    ]
    index = extract_handle_index(middle)
    blob = " ".join(index["stems"]).lower()
    assert "live ledger" in blob
    assert "east replica" in blob
    assert "don't write" in blob


def test_fact_harvest_keeps_measurement_nonces_not_paths():
    """Hyphenated measurement tokens enter facts; file stems do not."""
    middle = [
        {
            "role": "tool",
            "content": (
                "Plain measurement only: omega-cache-token-9f3a observed "
                "on shard-omega-p95. Also read auth_current_v2.py."
            ),
            "_read_path": "auth_current_v2.py",
        },
    ]
    index = extract_handle_index(middle)
    assert "omega-cache-token-9f3a" in index["facts"]
    assert "shard-omega-p95" in index["facts"]
    assert "auth_current_v2.py" in index["files"]
    assert "auth_current_v2" not in index["facts"]
    catalog = build_catalog_residual(middle, char_budget=2000)
    assert "### Facts" in catalog
    assert "omega-cache-token-9f3a" in catalog


def test_catalog_placeholders_are_not_ingested_as_handles():
    empty = build_catalog_residual([], char_budget=2000)
    index = extract_handle_index([{
        "role": "assistant",
        "content": empty,
        "_compressed_summary": True,
    }])
    assert index["files"] == []
    assert index["tools"] == []
    assert index["handles"] == []
    assert index["stems"] == []
    assert index["facts"] == []
    assert index["story"] == []
    blob = _index_blob(index)
    assert "(no file pointers found)" not in blob
    assert "(none)" not in blob
    assert "(no selected story)" not in blob


def test_selected_story_survives_catalog_reextract():
    """Last-N story must re-enter the next catalog from ### Selected story."""
    from harness.compaction_residual import SELECTED_STORY_HEADING

    middle = [
        {"role": "user", "content": "The canary now ships to the spare region."},
        {"role": "assistant", "content": "Recorded the replacement ship plan."},
        {"role": "user", "content": "Please continue the current docs pass."},
    ]
    first = extract_handle_index(middle)
    assert any("spare region" in line.lower() for line in first["story"])
    catalog = build_catalog_residual(middle, char_budget=4000)
    assert SELECTED_STORY_HEADING in catalog
    assert "spare region" in catalog.lower()
    second = extract_handle_index([{
        "role": "user",
        "content": (
            "[Earlier conversation summarized to fit context]\n" + catalog
        ),
        "_compressed_summary": True,
    }])
    assert any("spare region" in line.lower() for line in second["story"])
