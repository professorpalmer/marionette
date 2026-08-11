"""GET /api/spill/read — operator peek of spill:// via spill_registry."""
from __future__ import annotations

import os
from types import SimpleNamespace

from harness.api.files import FileServices, get_spill_read
from harness.spill_registry import register_spill


def _svc(state_dir: str, repo: str | None = None) -> FileServices:
    return FileServices(
        cfg=SimpleNamespace(state_dir=state_dir, repo=repo),
        sessions=None,
        upload_dir="",
    )


def _seed_text_spill(state_dir: str, session_id: str, tool_call_id: str, body: str) -> None:
    results_dir = os.path.join(state_dir, "pmharness-results")
    os.makedirs(results_dir, exist_ok=True)
    path = os.path.join(results_dir, f"{tool_call_id}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    assert register_spill(state_dir, session_id, tool_call_id, path, len(body))


def test_get_spill_read_returns_full_content(tmp_path):
    state = str(tmp_path)
    body = "line one\n" * 500
    _seed_text_spill(state, "sess1", "call_peek", body)

    status, payload = get_spill_read("spill://sess1/call_peek", _svc(state))
    assert status == 200
    assert payload["ok"] is True
    assert payload["uri"] == "spill://sess1/call_peek"
    assert payload["content"] == body
    assert payload["chars"] == len(body)
    assert payload["truncated"] is False


def test_get_spill_read_rejects_non_spill_and_missing(tmp_path):
    state = str(tmp_path)
    svc = _svc(state)

    status, payload = get_spill_read("artifact://x/y", svc)
    assert status == 400
    assert "spill://" in payload["error"]

    status, payload = get_spill_read("spill://sess1/missing", svc)
    assert status == 404
    assert "not found" in payload["error"].lower()

    status, payload = get_spill_read("", svc)
    assert status == 400


def test_get_spill_read_requires_state_dir():
    svc = FileServices(cfg=SimpleNamespace(state_dir="", repo=None), sessions=None, upload_dir="")
    status, payload = get_spill_read("spill://sess1/call_a", svc)
    assert status == 503
    assert "state_dir" in payload["error"]


def test_get_spill_read_rejects_directory_listing(tmp_path):
    state = str(tmp_path)
    _seed_text_spill(state, "sess1", "call_1", "x" * 200)

    status, payload = get_spill_read("spill://sess1", _svc(state))
    assert status == 400
    assert "directory" in payload["error"].lower()
