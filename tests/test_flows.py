"""run_flow / flow_control: graph boundary, session runs, wakes and the drain."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from harness import flows
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction, build_tools_schema, from_wire

SID = "sess-flow"
ALLOW = {
    "allowed_adapters": ["agentic"],
    "prefer_plan_billed": False,
    "primary_adapter": "agentic",
    "allowed_model_ids": ["or/model-a"],
}


@pytest.fixture
def routing(monkeypatch):
    import harness.marionette_registry as registry
    import harness.swarm_worker_allowlist as allowlist
    import pmharness.bridge as bridge

    monkeypatch.setattr(allowlist, "resolve_swarm_worker_allowlist", lambda **_k: dict(ALLOW))
    monkeypatch.setattr(registry, "boot_marionette_registry", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "_sync_agentic_credential_env", lambda: None)


@pytest.fixture
def pins(monkeypatch):
    """Pins named ok-* resolve; anything else is demoted."""
    import harness.swarm_model_pin as pin_mod

    def fake(pin, *, allowed_adapters=None):
        if pin.startswith("ok-"):
            return {
                "pin_fields": {"pinned_model": f"agentic/{pin}", "model": pin, "auto_route": False},
                "auto_route": False, "requested": pin, "resolved": f"agentic/{pin}",
                "demoted": False, "reason": "exact", "adapter": "agentic",
            }
        return {"pin_fields": {}, "auto_route": True, "requested": pin, "resolved": "",
                "demoted": True, "reason": f"pin {pin!r} not in keyed worker registry", "adapter": ""}

    monkeypatch.setattr(pin_mod, "resolve_swarm_model_pin", fake)


def _graph(**extra):
    graph = {
        "id": "fix-and-review",
        "entry": "fix",
        "nodes": [
            {"id": "fix", "kind": "agent", "task": "{{input}}"},
            {"id": "review", "kind": "judge", "task": "Review {{input}}"},
        ],
        "edges": [
            {"from": "fix", "to": "review", "when": "ok"},
            {"from": "review", "to": "fix", "when": "FAIL", "max": 2},
        ],
    }
    graph.update(extra)
    return graph


def _prepare(workspace, raw, *, guard=False, state_dir=""):
    return flows.prepare_graph(
        raw, workspace=str(workspace), session_id=SID,
        full_auto_guard=guard, state_dir=state_dir or str(workspace),
    )


def _refused(workspace, raw, **kw) -> str:
    with pytest.raises(flows.FlowCallError) as info:
        _prepare(workspace, raw, **kw)
    return str(info.value)


# --------------------------------------------------------------------------
# prepare_graph


def test_cwd_defaults_to_workspace_and_relative_joins(tmp_path, routing):
    (tmp_path / "sub").mkdir()
    assert _prepare(tmp_path, _graph())["cwd"] == os.path.realpath(tmp_path)
    assert _prepare(tmp_path, _graph(cwd="sub"))["cwd"] == os.path.join(os.path.realpath(tmp_path), "sub")
    graph = _prepare(tmp_path, json.dumps(_graph()))
    assert graph["id"] == "fix-and-review"


def test_cwd_outside_workspace_is_refused(tmp_path, routing):
    assert "outside the workspace" in _refused(tmp_path, _graph(cwd=".."))
    assert "outside the workspace" in _refused(tmp_path, _graph(cwd=os.path.dirname(str(tmp_path))))


def test_shell_cwd_must_stay_inside_workspace(tmp_path, routing):
    outside = os.path.dirname(os.path.realpath(tmp_path))
    graph = _graph(nodes=[{"id": "sh", "kind": "shell", "command": "echo hi", "cwd": outside}], entry="sh", edges=[])
    assert "node 'sh' cwd" in _refused(tmp_path, graph)
    graph["nodes"][0]["cwd"] = "../.."
    assert "node 'sh' cwd" in _refused(tmp_path, graph)
    graph["nodes"][0]["cwd"] = "."
    _prepare(tmp_path, graph)


def test_pilot_adapter_and_payload_are_rejected_everywhere(tmp_path, routing):
    graph = _graph(defaults={"adapter": "codex"})
    assert "defaults sets 'adapter'" in _refused(tmp_path, graph)
    graph = _graph()
    graph["nodes"][0]["payload"] = {"allowed_model_ids": ["*"]}
    assert "node 'fix' sets 'payload'" in _refused(tmp_path, graph)
    template = {"id": "each", "kind": "agent", "role": "explore", "task": "{{item}}", "adapter": "claude-code"}
    mapped = {"id": "m", "entry": "fan", "nodes": [{"id": "fan", "kind": "map", "items": ["a"], "node": template}]}
    assert "template node 'each' sets 'adapter'" in _refused(tmp_path, mapped)
    child = {
        "defaults": {"payload": {"read_only": False}},
        "entry": "one",
        "nodes": [{"id": "one", "kind": "agent", "role": "explore", "task": "x", "payload": {}}],
    }
    mapped = {"id": "m", "entry": "fan", "nodes": [{"id": "fan", "kind": "map", "items": ["a"], "graph": child}]}
    problems = _refused(tmp_path, mapped)
    assert "graph defaults sets 'payload'" in problems
    assert "graph node 'one' sets 'payload'" in problems


def test_defaults_carry_marionette_routing_and_session_stamp(tmp_path, routing):
    graph = _prepare(tmp_path, _graph())
    defaults = graph["defaults"]
    assert defaults["adapter"] == "agentic"
    payload = defaults["payload"]
    assert payload["auto_route"] is True
    assert payload["allowed_adapters"] == ["agentic"]
    assert payload["allowed_model_ids"] == ["or/model-a"]
    assert payload["routing_policy"] == "balanced"
    assert payload["prefer_plan_billed"] is False
    assert isinstance(payload["token_budget"], int)
    assert payload["session_id"] == SID
    assert payload["origin"] == "marionette"
    assert payload["cwd"] == os.path.realpath(tmp_path)
    for key in ("read_only", "no_edit", "dry_run"):
        assert key not in payload


def test_node_and_global_pins_resolve(tmp_path, routing, pins):
    graph = _graph(defaults={"model": "ok-global", "timeout_seconds": 60})
    graph["nodes"][1]["model"] = "ok-judge"
    out = _prepare(tmp_path, graph)
    assert "model" not in out["defaults"]
    assert out["defaults"]["payload"]["pinned_model"] == "agentic/ok-global"
    assert out["defaults"]["payload"]["auto_route"] is False
    assert "allowed_model_ids" not in out["defaults"]["payload"]
    judge = out["nodes"][1]
    assert "model" not in judge
    assert judge["adapter"] == "agentic"
    assert judge["payload"]["pinned_model"] == "agentic/ok-judge"
    assert judge["payload"]["auto_route"] is False


def test_map_template_pin_resolves_and_demoted_pin_fails_closed(tmp_path, routing, pins):
    template = {"id": "each", "kind": "agent", "role": "explore", "task": "{{item}}", "model": "ok-small"}
    mapped = {"id": "m", "entry": "fan", "nodes": [{"id": "fan", "kind": "map", "items": ["a"], "node": template}]}
    out = _prepare(tmp_path, mapped)
    assert out["nodes"][0]["node"]["payload"]["pinned_model"] == "agentic/ok-small"
    graph = _graph()
    graph["nodes"][0]["model"] = "gone-model"
    message = _refused(tmp_path, graph)
    assert "node 'fix' model 'gone-model'" in message
    assert "not in keyed worker registry" in message
    child = {"defaults": {"model": "gone-too"}, "entry": "one",
             "nodes": [{"id": "one", "kind": "agent", "role": "explore", "task": "x"}]}
    mapped = {"id": "m", "entry": "fan", "nodes": [{"id": "fan", "kind": "map", "items": ["a"], "graph": child}]}
    assert "graph defaults model 'gone-too'" in _refused(tmp_path, mapped)


def test_full_auto_shell_guard_checks_allowlist(tmp_path, routing, monkeypatch):
    import harness.command_allowlist as allowlist_mod

    command = "rm -rf /"
    child = {"entry": "wipe", "nodes": [{"id": "wipe", "kind": "shell", "command": command}]}
    graph = {"id": "danger", "entry": "fan", "nodes": [{"id": "fan", "kind": "map", "items": ["a"], "graph": child}]}
    _prepare(tmp_path, graph, guard=False)
    assert "blocked by the full-auto guard" in _refused(tmp_path, graph, guard=True)
    seen = {}

    def hit(cmd, state_dir=None, workspace_root="", command_hash=""):
        seen.update(cmd=cmd, state_dir=state_dir, hash=command_hash)
        return True

    monkeypatch.setattr(allowlist_mod, "allowlist_contains", hit)
    _prepare(tmp_path, graph, guard=True, state_dir="/state")
    assert seen == {"cmd": command, "state_dir": "/state",
                    "hash": hashlib.sha256(command.encode("utf-8")).hexdigest()}


def test_validate_graph_problems_are_surfaced(tmp_path, routing):
    message = _refused(tmp_path, _graph(id="Not_Kebab", edges=[{"from": "fix", "to": "nowhere"}]))
    assert "id must be kebab-case" in message
    assert "edge to 'nowhere' is not a node" in message


def test_schema_example_graph_is_valid(tmp_path, routing):
    run_flow = next(t["function"] for t in build_tools_schema() if t["function"]["name"] == "run_flow")
    example = json.loads(run_flow["description"].split("Minimal example: ", 1)[1])
    _prepare(tmp_path, example)


# --------------------------------------------------------------------------
# launch / control


class _Session:
    def __init__(self, tmp_path):
        self.state_dir = str(tmp_path / "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.workspace = tmp_path / "ws"
        self.workspace.mkdir(exist_ok=True)
        self.config = SimpleNamespace(repo=str(self.workspace))
        self.harness_session_id = SID
        self._flow_runs = {}
        self._session_job_ids = []
        self._display_transcript = []


@pytest.fixture
def calls(monkeypatch):
    log = []
    responses = {}

    def fake(state_dir, action, params):
        log.append((state_dir, action, params))
        reply = responses.get(action)
        if callable(reply):
            return reply(params)
        if isinstance(reply, list):
            return reply.pop(0)
        return reply or {"run_id": "flow_000000000001", "status": "running", "next_since": 0, "steps": []}

    monkeypatch.setattr(flows, "flow_call", fake)
    return SimpleNamespace(log=log, responses=responses)


def _launch(session, **kw):
    params = dict(graph=_graph(), flow_input="fix the bug", continue_from="", goal="", repo="")
    params.update(kw)
    return flows.launch(session, **params)


def test_launch_records_the_run_in_the_session_store(tmp_path, routing, calls):
    session = _Session(tmp_path)
    run_id, summary, objective = _launch(session)
    state_dir, action, params = calls.log[-1]
    assert (state_dir, action) == (session.state_dir, "run")
    assert params["input"] == "fix the bug"
    assert params["continue_from"] is None
    assert params["graph"]["defaults"]["payload"]["session_id"] == SID
    assert params["cwd"] == os.path.realpath(session.workspace)
    assert objective == "Flow fix-and-review: fix the bug"
    assert session._flow_runs[run_id] == {"objective": objective, "delivered": "", "since": 0, "status": "running"}
    assert session._session_job_ids == [run_id]
    assert session._display_transcript[-1]["job_ids"] == [run_id]
    assert session._display_transcript[-1]["status"] == "running"
    assert session._display_transcript[-1]["session_id"] == SID


def test_continue_from_must_be_a_run_of_this_session(tmp_path, routing, calls):
    session = _Session(tmp_path)
    with pytest.raises(flows.FlowCallError, match="not a flow run of this session"):
        _launch(session, continue_from="flow_aaaaaaaaaaaa")
    session._flow_runs["flow_aaaaaaaaaaaa"] = {"objective": "x", "delivered": "done:2:", "since": 2, "status": "done"}
    _launch(session, continue_from="flow_aaaaaaaaaaaa")
    assert calls.log[-1][2]["continue_from"] == "flow_aaaaaaaaaaaa"


def test_active_flow_cap(tmp_path, routing, calls, monkeypatch):
    monkeypatch.setenv("HARNESS_MAX_ACTIVE_FLOWS", "2")
    session = _Session(tmp_path)
    for n in range(2):
        session._flow_runs[f"flow_00000000000{n}"] = {"status": "running"}
    with pytest.raises(flows.FlowCallError, match="already has 2 active flows"):
        _launch(session)
    session._flow_runs["flow_000000000000"]["status"] = "done"
    _launch(session)


def test_control_maps_to_flow_actions(tmp_path, calls):
    session = _Session(tmp_path)
    with pytest.raises(flows.FlowCallError, match="not a flow run"):
        flows.control(session, "flow_bbbbbbbbbbbb", "stop")
    session._flow_runs["flow_bbbbbbbbbbbb"] = {"objective": "o", "delivered": "waiting:3:ask", "since": 3, "status": "waiting"}
    calls.responses["resume"] = {"status": "running"}
    calls.responses["stop"] = {"status": "stopped", "reason": "stopped by request"}
    calls.responses["cut"] = {"status": "running"}
    flows.control(session, "flow_bbbbbbbbbbbb", "answer", answer="yes")
    assert calls.log[-1][1:] == ("resume", {"run_id": "flow_bbbbbbbbbbbb", "answer": "yes", "background": True})
    assert session._flow_runs["flow_bbbbbbbbbbbb"]["status"] == "running"
    flows.control(session, "flow_bbbbbbbbbbbb", "resume")
    assert calls.log[-1][1:] == ("resume", {"run_id": "flow_bbbbbbbbbbbb", "background": True})
    flows.control(session, "flow_bbbbbbbbbbbb", "cut", reason="too slow")
    assert calls.log[-1][1:] == ("cut", {"run_id": "flow_bbbbbbbbbbbb", "reason": "too slow"})
    line = flows.control(session, "flow_bbbbbbbbbbbb", "stop")
    assert calls.log[-1][1:] == ("stop", {"run_id": "flow_bbbbbbbbbbbb"})
    assert "status stopped" in line


def test_cancel_job_routes_flow_ids_to_stop(tmp_path, calls):
    session = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    session._flow_runs["flow_cccccccccccc"] = {"objective": "o", "delivered": "", "since": 0, "status": "running"}
    calls.responses["stop"] = {"status": "stopped"}
    ok, status, text = session._do_cancel_job(PilotAction(kind="cancel_job", arguments={"job_id": "flow_cccccccccccc"}))
    assert (ok, status) == (True, "success")
    assert calls.log[-1][1:] == ("stop", {"run_id": "flow_cccccccccccc"})


# --------------------------------------------------------------------------
# wakes


def _status(status, next_since, **extra):
    body = {"run_id": "flow_dddddddddddd", "status": status, "reason": "", "steps": [], "next_since": next_since, "usage": {}}
    body.update(extra)
    return body


def test_due_wakes_dedupes_by_key_and_rewakes_after_answer(tmp_path, calls):
    session = _Session(tmp_path)
    session._flow_runs["flow_dddddddddddd"] = {"objective": "o", "delivered": "", "since": 0, "status": "running"}
    gate = {"node": "ask", "question": "Ship it?", "options": ["yes", "no"]}
    calls.responses["status"] = lambda params: _status("waiting", 3, gate=gate)
    wakes = flows.due_wakes(session)
    assert [(w.status, w.key) for w in wakes] == [("waiting", "waiting:3:ask")]
    flows.mark_delivered(session, wakes[0])
    assert session._flow_runs["flow_dddddddddddd"]["since"] == 3
    assert flows.due_wakes(session) == []
    assert calls.log[-1][2] == {"run_id": "flow_dddddddddddd", "since": 3}
    calls.responses["resume"] = {"status": "running"}
    flows.control(session, "flow_dddddddddddd", "answer", answer="yes")
    calls.responses["status"] = lambda params: _status("done", 5)
    wakes = flows.due_wakes(session)
    assert [(w.status, w.key) for w in wakes] == [("done", "done:5:")]
    flows.mark_delivered(session, wakes[0])
    count = len(calls.log)
    assert flows.due_wakes(session) == []
    assert len(calls.log) == count  # a delivered terminal run is not polled again


def test_due_wakes_interrupted_and_read_failures(tmp_path, calls):
    session = _Session(tmp_path)
    session._flow_runs["flow_eeeeeeeeeeee"] = {"objective": "o", "delivered": "", "since": 0, "status": "running"}
    session._flow_runs["flow_ffffffffffff"] = {"objective": "p", "delivered": "", "since": 0, "status": "running"}

    def reply(params):
        if params["run_id"] == "flow_ffffffffffff":
            raise flows.FlowCallError("no flow run flow_ffffffffffff")
        return _status("interrupted", 1, reason="the walker exited before the run finished")

    calls.responses["status"] = reply
    wakes = flows.due_wakes(session)
    assert [(w.run_id, w.status) for w in wakes] == [("flow_eeeeeeeeeeee", "interrupted")]
    flows.mark_delivered(session, wakes[0])
    calls.responses["resume"] = {"status": "running"}
    flows.control(session, "flow_eeeeeeeeeeee", "resume")
    # Re-interrupted with no new steps: same key, but a new event after resume.
    assert [w.status for w in flows.due_wakes(session)] == ["interrupted"]


def test_continuation_text_per_status():
    def wake(status, **summary):
        return flows.FlowWake(run_id="flow_x", status=status, key=status, summary=summary)

    assert "take the next step" in flows.continuation_text([wake("done")])
    assert "continue_from=flow_x" in flows.continuation_text([wake("failed")])
    assert "continue_from=flow_x" in flows.continuation_text([wake("stuck")])
    assert '"control": "resume"' in flows.continuation_text([wake("interrupted")])
    assert "Acknowledge" in flows.continuation_text([wake("stopped")])
    text = flows.continuation_text([wake("waiting", gate={"node": "ask", "question": "Ship?", "options": ["yes", "no"]})])
    assert "Ship?" in text and "yes | no" in text
    assert "ask them first" in text and '"control": "answer"' in text


def test_format_wake_is_compact():
    summary = {
        "status": "failed", "reason": "judge said FAIL",
        "steps": [{"node": "fix", "visit": 1, "ok": True}, {"node": "review", "visit": 1, "ok": True, "verdict": "FAIL", "reason": "missing test"}],
        "last_problem": {"node": "review", "verdict": "FAIL"},
        "usage": {"cost_usd": 0.12},
    }
    text = flows.format_wake(flows.FlowWake("flow_x", "failed", "k", summary), "o")
    assert text.splitlines()[0] == "status: failed (judge said FAIL)"
    assert "- fix#1 ok" in text
    assert "- review#1 ok FAIL: missing test" in text
    assert "last problem: node=review, verdict=FAIL" in text
    assert '"cost_usd": 0.12' in text


# --------------------------------------------------------------------------
# drain integration


RUN = "flow_aaaaaaaaaaaa"


@pytest.fixture
def session(tmp_path):
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    s.harness_session_id = SID
    s._flow_runs[RUN] = {"objective": "Flow g: ship", "delivered": "", "since": 0, "status": "running"}
    return s


def _drain(session):
    return [(ev.kind, ev.data) for ev in session.drain_swarm_results()]


def test_drain_running_to_done_wakes_once(session, calls):
    calls.responses["status"] = lambda params: _status("running", 1)
    assert session.has_pending_swarms() is True
    assert _drain(session) == []
    calls.responses["status"] = lambda params: _status(
        "done", 2, steps=[{"node": "fix", "visit": 1, "ok": True}],
    )
    before = len(session._history)
    events = _drain(session)
    kinds = [k for k, _ in events]
    assert kinds == ["swarm_result", "pilot_resume"]
    result = events[0][1]["result"]
    assert result["analysis_ok"] is True and result["error"] is None
    assert events[1][1]["job_ids"] == [RUN]
    record, continuation = session._history[before:]
    assert record["role"] == "assistant"
    assert record["content"].startswith(f"[flow {RUN} done for: Flow g: ship]")
    assert continuation["role"] == "user"
    assert "take the next step" in continuation["content"]
    assert session.has_pending_swarms() is False
    assert _drain(session) == []
    pill = [r for r in session._display_transcript if r.get("type") == "swarm_pending"][-1]
    assert pill["status"] == "done"


def test_drain_waiting_emits_waiting_pill_and_gate_guidance(session, calls):
    gate = {"node": "ask", "question": "Deploy now?", "options": ["yes", "no"]}
    calls.responses["status"] = lambda params: _status("waiting", 1, gate=gate)
    events = _drain(session)
    assert events[0] == ("swarm_pending", {"job_ids": [RUN], "objective": "Flow g: ship", "status": "waiting"})
    assert events[-1][0] == "pilot_resume"
    assert "Deploy now?" in session._history[-1]["content"]
    assert "ask them first" in session._history[-1]["content"]
    pill = [r for r in session._display_transcript if r.get("type") == "swarm_pending"][-1]
    assert pill["status"] == "waiting" and pill["session_id"] == SID
    assert session.has_pending_swarms() is False


def test_drain_after_stop_suppresses_resume(session, calls):
    session._stop_holds_idle = True
    calls.responses["status"] = lambda params: _status("failed", 2, reason="boom")
    before = len(session._history)
    events = _drain(session)
    assert [k for k, _ in events] == ["swarm_result"]
    assert events[0][1]["result"]["error"] == "boom"
    assert [m["role"] for m in session._history[before:]] == ["assistant"]
    assert _drain(session) == []


def test_drain_coalesces_flow_wake_with_swarm_result(session, calls):
    calls.responses["status"] = lambda params: _status("done", 2)
    session._swarm_results.put({
        "job_id": "local-abc",
        "objective": "patch it",
        "result": {"applied": True, "files": ["a.py"], "summary": "patched a.py", "error": None},
    })
    session._history.append({"role": "assistant", "content": "working"})
    before = len(session._history)
    events = _drain(session)
    resumes = [d for k, d in events if k == "pilot_resume"]
    assert len(resumes) == 1
    assert resumes[0]["job_ids"] == ["local-abc", RUN]
    users = [m for m in session._history[before:] if m["role"] == "user"]
    assert len(users) == 1
    assert "[background job local-abc finished]" in users[0]["content"]
    assert f"[flow {RUN} done]" in users[0]["content"]
    roles = [m["role"] for m in session._history[1:]]
    assert all(not (a == b == "user") for a, b in zip(roles, roles[1:]))


def test_flow_runs_round_trip_through_transcript(session, tmp_path):
    session._flow_runs[RUN].update(delivered="waiting:3:ask", since=3, status="waiting")
    data = json.loads(json.dumps(session.export_transcript_data()))
    assert data["flow_runs"][RUN]["delivered"] == "waiting:3:ask"
    restored = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    restored.load_history(data)
    assert restored._flow_runs == session._flow_runs


def test_flow_node_jobs_are_session_stamped_for_the_tracker(tmp_path, routing):
    from harness.job_scoping import filter_store_jobs_with_tasks, parse_job_session_id

    graph = _prepare(tmp_path, _graph())
    # Puppetmaster's _spec merges defaults.payload under the node payload.
    payload = {**graph["defaults"]["payload"], **(graph["nodes"][0].get("payload") or {}),
               "sandbox": "workspace-write", "flow": {"run_id": RUN, "node": "fix", "visit": 1}}
    task = SimpleNamespace(job_id="job_123", payload=payload)
    store = SimpleNamespace(
        list_tasks_for_jobs=lambda ids: [task],
        list_jobs=lambda: [SimpleNamespace(id="job_123", label="flow fix-and-review: fix")],
    )
    jobs = [{"id": "job_123", "label": "flow fix-and-review: fix", "session_id": RUN, "status": "running"}]
    assert parse_job_session_id("flow fix-and-review: fix", [task]) == SID
    visible, _tasks = filter_store_jobs_with_tasks(
        jobs, store, active_session_id=SID, repo_root=str(tmp_path),
    )
    assert [row["session_id"] for row in visible] == [SID]
    assert visible[0]["origin"] == "marionette"


# --------------------------------------------------------------------------
# pilot surface


def _schema_fn(name, **kw):
    for entry in build_tools_schema(**kw):
        fn = entry.get("function") or {}
        if fn.get("name") == name:
            return fn
    return None


def test_schema_entries():
    run_flow = _schema_fn("run_flow")
    assert run_flow["parameters"]["required"] == ["graph"]
    assert set(run_flow["parameters"]["properties"]) == {"graph", "input", "continue_from", "goal", "repo"}
    control = _schema_fn("flow_control")
    assert control["parameters"]["required"] == ["run_id", "control"]
    assert control["parameters"]["properties"]["control"]["enum"] == ["answer", "resume", "cut", "stop"]
    assert _schema_fn("run_flow", no_delegation=True) is None
    assert _schema_fn("flow_control", no_delegation=True) is None


def test_worker_catalog_excludes_flow_tools():
    from harness.tool_discovery import ToolCatalog

    catalog = ToolCatalog()
    catalog.refresh(no_delegation=True)
    names = {t["function"]["name"] for t in catalog.visible_schema(no_delegation=True)}
    assert "run_flow" not in names and "flow_control" not in names
    catalog = ToolCatalog()
    catalog.refresh()
    names = {t["function"]["name"] for t in catalog.visible_schema(profile="standard")}
    assert {"run_flow", "flow_control"} <= names


def test_micro_profile_has_no_flow_tools():
    from harness.tool_discovery import core_visible_names

    names = core_visible_names(profile="micro")
    assert "run_flow" not in names and "run_swarm" not in names


def test_plan_mode_lists_match_and_block_flow_tools():
    from harness.send_loop_phases import PLAN_SKIP_KINDS
    from harness.tool_capabilities import MUTATING_KINDS, plan_mode_blocks

    assert MUTATING_KINDS == PLAN_SKIP_KINDS
    assert plan_mode_blocks("run_flow") and plan_mode_blocks("flow_control")


def test_delegation_kinds_include_run_flow():
    from harness.pilot_guards import DELEGATION_KINDS

    assert DELEGATION_KINDS == {"run_swarm", "run_implement", "run_parallel", "run_flow"}


def test_from_wire_parses_flow_actions():
    act = from_wire("run_flow", {"graph": json.dumps(_graph()), "input": "go"})
    assert act.graph["id"] == "fix-and-review"
    assert act.flow_input == "go"
    assert act.goal == "Flow fix-and-review: go"
    with pytest.raises(ValueError, match="requires a 'graph'"):
        from_wire("run_flow", {"graph": "not json"})
    act = from_wire("flow_control", {"run_id": "flow_x", "control": "Answer", "answer": "yes"})
    assert (act.run_id, act.control, act.answer) == ("flow_x", "answer", "yes")
    with pytest.raises(ValueError, match="requires 'answer'"):
        from_wire("flow_control", {"run_id": "flow_x", "control": "answer"})
    with pytest.raises(ValueError, match="control must be"):
        from_wire("flow_control", {"run_id": "flow_x", "control": "action"})


def test_run_flow_fingerprint_dedupes_identical_graphs():
    from harness.pilot_guards import dedupe_dispatch_actions, normalize_action_args

    one = from_wire("run_flow", {"graph": _graph(), "input": "go"})
    same = from_wire("run_flow", {"graph": json.loads(json.dumps(_graph())), "input": "go"})
    other = from_wire("run_flow", {"graph": _graph(), "input": "stop"})
    assert dedupe_dispatch_actions([one, same, other]) == [one, other]
    assert normalize_action_args("run_flow", one) == normalize_action_args("run_flow", same)
    assert normalize_action_args("run_flow", one) != normalize_action_args("run_flow", other)


# --------------------------------------------------------------------------
# offline E2E: a real detached walker


def test_real_flow_runs_shell_node_and_wakes_done(tmp_path, routing, monkeypatch):
    from puppetmaster.flow import walker_alive

    monkeypatch.delenv("PUPPETMASTER_WORKER", raising=False)
    session = _Session(tmp_path)
    command = f'"{sys.executable}" -c "print(\'VERDICT: PASS - ok\')"'
    graph = {"id": "smoke", "entry": "check", "nodes": [{"id": "check", "kind": "shell", "command": command}]}
    run_id, _summary, _objective = flows.launch(
        session, graph=graph, flow_input="", continue_from="", goal="smoke", repo="",
    )
    try:
        deadline = time.monotonic() + 60
        wakes = []
        while time.monotonic() < deadline:
            wakes = flows.due_wakes(session)
            if wakes:
                break
            time.sleep(0.5)
        assert [w.status for w in wakes] == ["done"], wakes
        steps = wakes[0].summary["steps"]
        assert steps[-1]["node"] == "check" and steps[-1]["verdict"] == "PASS"
        flows.mark_delivered(session, wakes[0])
        assert flows.active_flow_ids(session) == []
    finally:
        marker = os.path.join(session.state_dir, "flows", "runs", run_id, "walker.pid")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if os.name != "nt":
                # Reap the detached walker this process spawned so it never lingers.
                try:
                    with open(marker, encoding="utf-8") as handle:
                        os.waitpid(int(json.load(handle)["pid"]), os.WNOHANG)
                except (OSError, ValueError, KeyError):
                    pass
            if not walker_alive(session.state_dir, run_id):
                break
            time.sleep(0.2)
        assert not walker_alive(session.state_dir, run_id)
