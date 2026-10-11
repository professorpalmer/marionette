"""A long local tool loop keeps the user's own request across repeated compactions."""
from __future__ import annotations

import json

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.working_set import carried_requests, requests_block, requests_for_residual

REQUEST = (
    "Port the billing exporter to the new ledger schema.\n"
    "Keep the CSV column order. Do not touch tests/legacy/. Report the diff."
)


def _tool_round(history, i):
    call_id = "call_%d" % i
    history.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": "run_command", "arguments": json.dumps({"command": "grep -n ledger src/m%d.py" % i})}}],
    })
    history.append({"role": "tool", "tool_call_id": call_id, "content": ("line %d " % i) * 400})


def _session(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.compaction_mixin.MIN_COMPACTABLE_TOKENS", 0)
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "catalog")
    cfg = HarnessConfig(max_context_tokens=1_000_000, state_dir=str(tmp_path))
    cfg.repo = str(tmp_path)
    s = ConversationalSession(cfg)
    s._history.append({"role": "user", "content": REQUEST, "_user_request": True})
    s._edited_paths = {"src/exporter.py"}
    return s


def _real_user_messages(history):
    return [m for m in history[1:] if m.get("role") == "user" and not m.get("_compressed_summary")]


def test_request_survives_two_mid_turn_compactions(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    for i in range(30):
        _tool_round(s._history, i)
    list(s._maybe_compact_history(force=True))
    for i in range(30, 60):
        _tool_round(s._history, i)
    list(s._maybe_compact_history(force=True))

    text = "\n".join(str(m.get("content") or "") for m in s._history[1:])
    assert REQUEST in text
    assert text.count(REQUEST) == 1
    # The file list is reference data inside the residual, not a user turn.
    assert _real_user_messages(s._history) == []
    residual = s._history[1]
    assert residual.get("_compressed_summary") is True
    assert "edited: src/exporter.py" in residual["content"]


def test_newer_request_replaces_carried_one(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch)
    for i in range(30):
        _tool_round(s._history, i)
    list(s._maybe_compact_history(force=True))
    s._history.append({"role": "user", "content": "Stop the port. Revert src/exporter.py instead.", "_user_request": True})
    for i in range(30, 60):
        _tool_round(s._history, i)
    list(s._maybe_compact_history(force=True))

    assert carried_requests(s._history[1]["content"]) == [
        REQUEST, "Stop the port. Revert src/exporter.py instead.",
    ]


def test_injected_user_rows_are_not_requests():
    middle = [
        {"role": "user", "content": REQUEST, "input_ids": ["in_1"]},
        {"role": "user", "content": "[auto-verify] The project check failed"},
        {"role": "user", "content": "Working set still tracked after compact:\n- edited: a.py"},
        {"role": "user", "content": "steer: use the v2 schema", "_user_request": True},
    ]
    assert requests_for_residual(middle) == [REQUEST, "steer: use the v2 schema"]


def test_request_budget_keeps_the_newest_whole():
    old, new = "o" * 11000, "n" * 5000
    block = requests_block([old, new])
    kept = carried_requests(block)
    assert kept[-1] == new
    assert kept[0].endswith("[request clipped]")
