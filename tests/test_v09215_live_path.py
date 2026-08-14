"""Live-path validation for v0.9.215 kernel features.

These go through ConversationalSession.send / the session-control API — not
just the isolated helpers — so a disconnected module cannot go green.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from harness.api.session_control import SessionControlServices, post_session_steer
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.mcp_client import McpTool
from harness.task_profile import MICRO, STANDARD
from harness.task_receipt import JSONL_FILENAME, load_receipts, prompt_hash
from harness.task_transaction import as_dict, context_block
from harness.tool_requirement import SoftToolRequirement
from harness.wiki import WikiClient


class _FakePilotWithActions:
    name = "fake_actions"

    def __init__(self, actions):
        self.actions = actions
        self.calls = 0

    def chat(self, messages, tools=None, system=None):
        from pmharness.drivers.openai_compat import DriverResponse

        self.calls += 1
        if self.calls == 1:
            txt = json.dumps({"say": "Executing actions.", "actions": self.actions})
        else:
            txt = json.dumps({"say": "Done.", "actions": []})
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def _session(tmp_path, *, repo=None, auto_verify=False, task_profile="auto"):
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path / "state"),
        repo=str(repo or (tmp_path / "repo")),
        auto_verify=auto_verify,
        task_profile=task_profile,
    )
    (tmp_path / "repo").mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    s = ConversationalSession(cfg)
    s.harness_session_id = "live-215"
    return s


def test_send_writes_compact_receipt_for_micro_prose(tmp_path):
    s = _session(tmp_path)
    s.pilot = _FakePilotWithActions([])
    events = list(s.send("typo in README.md"))
    kinds = [e.kind for e in events]
    assert "task_profile" in kinds
    assert "assistant_done" in kinds
    prof = next(e for e in events if e.kind == "task_profile")
    assert prof.data["profile"] == MICRO

    rows = load_receipts(s.config.state_dir, limit=5)
    assert len(rows) == 1
    rec = rows[0]
    assert rec["task_id"] == "live-215"
    assert rec["profile"] == MICRO
    assert rec["prompt_hash"] == prompt_hash("typo in README.md")
    assert rec["adapter"] == "stub-oracle-v2"
    assert (tmp_path / "state" / JSONL_FILENAME).is_file()


def test_send_write_notes_transaction_reminds_and_receipt(tmp_path):
    s = _session(tmp_path, auto_verify=False)
    s.pilot = _FakePilotWithActions([
        {"kind": "write_file", "path": "note.txt", "content": "hello"},
    ])
    events = list(s.send("typo in README.md"))
    written = tmp_path / "repo" / "note.txt"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "hello"

    tx = as_dict(s._task_tx)
    assert any(str(p).endswith("note.txt") for p in tx.get("files") or [])
    assert tx["phase"] in ("acting", "verifying", "done")

    remind = SoftToolRequirement.remind_message()
    assert any(
        h.get("role") == "user" and remind in (h.get("content") or "")
        for h in s._history
    )

    rows = load_receipts(s.config.state_dir, limit=5)
    assert rows
    rec = rows[-1]
    assert rec["profile"] == MICRO
    assert rec.get("changed_files")
    assert rec.get("verification") in ("skipped", "unverified")


def test_send_write_plus_command_skips_remind_and_marks_pass(tmp_path):
    s = _session(tmp_path, auto_verify=False)
    s.pilot = _FakePilotWithActions([
        {"kind": "write_file", "path": "note.txt", "content": "hello"},
        {"kind": "run_command", "command": "echo hi"},
    ])
    list(s.send("typo in README.md"))
    remind = SoftToolRequirement.remind_message()
    assert not any(
        h.get("role") == "user" and remind in (h.get("content") or "")
        for h in s._history
    )
    assert s._turn_ran_command is True
    rec = load_receipts(s.config.state_dir, limit=1)[-1]
    assert rec.get("verification") == "pass"
    assert rec.get("changed_files")


def test_send_micro_skips_wiki_search(tmp_path, monkeypatch):
    s = _session(tmp_path)
    s._wiki = WikiClient(base_url="https://wiki.example.com", token="tok")
    s.pilot = _FakePilotWithActions([])

    def boom(query, *, limit=5):
        raise AssertionError("wiki search must not run on MICRO send")

    monkeypatch.setattr(s._wiki, "search_pages", boom)
    list(s.send("typo in README.md"))


def test_send_standard_wiki_uses_tighter_budget(tmp_path, monkeypatch):
    s = _session(tmp_path)
    s._wiki = WikiClient(base_url="https://wiki.example.com", token="tok")
    s.pilot = _FakePilotWithActions([])
    captured = {}

    def fake_search(query, *, limit=5):
        captured["limit"] = limit
        return [
            {"title": f"Hit {i}", "slug": f"hit-{i}", "snippet": "x" * 200}
            for i in range(limit)
        ]

    monkeypatch.setattr(s._wiki, "search_pages", fake_search)
    events = list(s.send("add OAuth support"))
    prof = next(e for e in events if e.kind == "task_profile")
    assert prof.data["profile"] == STANDARD
    assert captured.get("limit") == 3


def test_send_micro_compacts_live_catalog_descriptions(tmp_path):
    long_desc = (
        "First sentence about creating GitHub issues with a title and body. "
        + ("Additional trailing detail about labels milestones. " * 8)
    )
    assert len(long_desc) > 160
    tool = McpTool(
        server="github",
        name="create_issue",
        description=long_desc,
        input_schema={"type": "object", "properties": {}},
    )
    s = _session(tmp_path)
    s._mcp = SimpleNamespace(discovered_tools=lambda: [tool])
    s.pilot = _FakePilotWithActions([])
    list(s.send("typo in README.md"))
    entry = next(e for e in s._tool_catalog._entries.values() if e.source == "mcp")
    assert len(entry.description) <= 160
    assert entry.description.startswith("First sentence about creating GitHub issues")
    assert "Additional trailing detail" not in entry.description


def test_steer_inject_includes_task_transaction_block(tmp_path):
    s = _session(tmp_path, auto_verify=False)
    s.pilot = _FakePilotWithActions([
        {"kind": "write_file", "path": "note.txt", "content": "hello"},
    ])
    list(s.send("typo in README.md"))
    block = context_block(s._task_tx)
    assert "## Task transaction" in block
    s._history.append({"role": "user", "content": "(tool result)"})
    s.enqueue_steer("course correct")
    events = list(s._check_and_inject_steer())
    assert any(e.kind == "steer" for e in events)
    injected = s._history[-1]["content"]
    assert "course correct" in injected
    assert "## Task transaction" in injected
    assert "note.txt" in injected


def test_session_steer_interrupt_delivery_stops_then_queues():
    class _Pilot:
        def __init__(self):
            self.interrupts = 0
            self.prompts = []
            self.steers = []

        def is_turn_busy(self):
            return True

        def interrupt(self):
            self.interrupts += 1

        def enqueue_prompt(self, text, images=None, model=None):
            item = {"id": "p1", "text": text, "images": images or []}
            self.prompts.append(item)
            return item

        def enqueue_steer(self, text):
            self.steers.append(text)

    p = _Pilot()
    svc = SessionControlServices(
        cfg=SimpleNamespace(driver="m1", state_dir=None, max_context_tokens=96000),
        get_pilot=lambda: p,
        get_runners=lambda: SimpleNamespace(get=lambda sid: None),
        gate_active_pilot_ready=lambda: None,
        stash_put=lambda msg, imgs: "mid1",
        save_active_transcript=lambda: None,
        upload_dir="/uploads",
        diag=lambda *a: None,
        get_sessions=lambda: SimpleNamespace(active=None),
        save_transcript=lambda *a, **k: None,
        set_resume_latch=lambda *a, **k: None,
        persist_boot_usage=lambda **k: None,
        peek_resume_pending=lambda idle, session_id="": False,
        consume_resume_pending=lambda idle, session_id="": False,
        checkpoint_transcript=lambda: None,
        context_at=lambda *a: None,
    )
    code, payload = post_session_steer(
        {"text": "stop and do this", "delivery_mode": "interrupt"},
        svc,
    )
    assert code == 200
    assert payload["ok"] is True
    assert payload["action"] == "interrupt_then_queue"
    assert payload.get("interrupted") is True
    assert p.interrupts == 1
    assert p.prompts[0]["text"] == "stop and do this"
    assert p.steers == []
