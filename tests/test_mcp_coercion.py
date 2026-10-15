from __future__ import annotations

from harness.conversation import _mcp_result_text
from harness.mcp_client import McpTool
from harness.mcp_manager import McpManager
from harness.tool_discovery import coerce_mcp_arguments

SCHEMA = {
    "type": "object",
    "properties": {
        "filter": {"type": "object"},
        "ids": {"type": "array"},
        "query": {"type": "string"},
        "limit": {"type": "integer"},
    },
}


def test_json_string_parsed_for_object_and_array():
    args = {"filter": '{"a": 1}', "ids": "[1, 2]", "limit": 5}
    fixed, notes = coerce_mcp_arguments(SCHEMA, args)
    assert fixed == {"filter": {"a": 1}, "ids": [1, 2], "limit": 5}
    assert len(notes) == 2
    assert args["filter"] == '{"a": 1}'


def test_object_serialized_for_string():
    fixed, notes = coerce_mcp_arguments(SCHEMA, {"query": {"q": "x"}})
    assert fixed == {"query": '{"q": "x"}'}
    assert notes == ["query (object -> JSON string)"]


def test_all_or_nothing_when_one_property_fails_proof():
    args = {"filter": '{"a": 1}', "ids": '{"not": "a list"}'}
    assert coerce_mcp_arguments(SCHEMA, args) == (args, [])
    args = {"filter": '{"a": 1}', "ids": "not json"}
    assert coerce_mcp_arguments(SCHEMA, args) == (args, [])


def test_matching_shapes_unchanged():
    args = {"filter": {"a": 1}, "query": "x", "unknown": "[1]"}
    assert coerce_mcp_arguments(SCHEMA, args) == (args, [])


class _Client:
    alive = True

    def __init__(self):
        self.sent = []

    def call_tool(self, name, arguments, timeout=120.0):
        self.sent.append(arguments)
        return {"content": [{"type": "text", "text": "done"}]}


def _mgr(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    mgr = McpManager(config_path=str(tmp_path / "mcp.json"))
    client = _Client()
    mgr._clients["srv"] = client
    mgr._tools["srv.t"] = McpTool(server="srv", name="t", description="", input_schema=SCHEMA)
    return mgr, client


def test_manager_sends_coerced_args_and_notes_the_model(tmp_path, monkeypatch):
    mgr, client = _mgr(tmp_path, monkeypatch)
    out = mgr.call("srv.t", {"filter": '{"a": 1}'})
    assert client.sent == [{"filter": {"a": 1}}]
    text = _mcp_result_text(out)
    assert text.startswith("Note: Marionette converted arguments")
    assert "filter (JSON string -> object)" in text
    assert text.endswith("done")


def test_manager_sends_original_args_without_note_when_proof_fails(tmp_path, monkeypatch):
    mgr, client = _mgr(tmp_path, monkeypatch)
    args = {"filter": '{"a": 1}', "ids": "oops"}
    out = mgr.call("srv.t", args)
    assert client.sent == [args]
    assert _mcp_result_text(out) == "done"
