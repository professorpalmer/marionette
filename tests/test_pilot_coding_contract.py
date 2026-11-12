"""The default pilot prompt is a short coding contract.

Frontier harnesses on the same model send about 2.4k chars of prompt and a
small tool set. Marionette sent 22k chars and 40 tools (25.6k chars of
schema) on every request. Session plumbing is now on demand, and the
Marionette self-edit notes load only in Marionette's own checkout.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import MARIONETTE_REPO_SYSTEM, PILOT_SYSTEM, pilot_system
from harness.tool_discovery import ToolCatalog
from pmharness.drivers.openai_compat import DriverResponse

# Prompt plus the default tool schemas, in chars. It was 47.8k.
FIRST_REQUEST_BUDGET = 24_000


def test_prompt_and_default_tools_fit_the_budget():
    catalog = ToolCatalog()
    catalog.refresh(mcp_tools=[], browser_enabled=False)
    schema = catalog.visible_schema(profile="standard")
    assert len(PILOT_SYSTEM) + len(json.dumps(schema)) <= FIRST_REQUEST_BUDGET


def test_tests_take_their_oracle_from_the_request_and_use_unique_names():
    assert "from the request text, not from your own code" in PILOT_SYSTEM
    assert "unique prefix" in PILOT_SYSTEM


def test_self_edit_notes_load_only_in_a_marionette_checkout(tmp_path):
    assert pilot_system(str(tmp_path)) == PILOT_SYSTEM
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "cli.py").write_text("")
    comps = tmp_path / "webapp" / "src" / "components"
    comps.mkdir(parents=True)
    (comps / "Conversation.tsx").write_text("")
    assert pilot_system(str(tmp_path)) == PILOT_SYSTEM + MARIONETTE_REPO_SYSTEM


class _Pilot:
    supports_streaming = True
    name = "recall-pilot"

    def __init__(self, calls):
        self.calls = list(calls)
        self.seen = []

    def chat(self, messages, **_k):
        raise AssertionError("chat() must not run when streaming is available")

    def chat_stream(self, messages, *, tools=None, system=None, on_delta, **_k):
        self.seen.append(json.dumps(messages, default=str))
        if self.calls:
            name, args = self.calls.pop(0)
            call = {"id": f"c{len(self.seen)}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}
            return DriverResponse(text="", meta={"tool_calls": [call], "finish_reason": "tool_calls",
                                                 "stream_terminal": "tool_calls"})
        on_delta("Done.")
        return DriverResponse(text="Done.", tokens_out=2, meta={
            "tool_calls": [], "finish_reason": "stop", "stream_terminal": "stop", "stream_started": True})


def test_a_first_call_to_an_on_demand_tool_runs():
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", repo=tempfile.mkdtemp(),
                                            state_dir=tempfile.mkdtemp()))
    s.pilot = _Pilot([("search_state", {"query": "earlier swarm"})])
    events = list(s.send("Find the earlier swarm job."))
    results = [e.data for e in events if e.kind == "action_result"]
    assert results, "the call must dispatch"
    assert "search_state" in (results[0].get("types") or [])
    assert "INVALID" not in json.dumps(results[0])
