from types import SimpleNamespace

from harness.conversation import ConversationalSession


class _Catalog:
    activated = set()

    def visible_schema(self, **_kwargs):
        return [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "run_swarm"}},
            {"type": "function", "function": {"name": "run_implement"}},
            {"type": "function", "function": {"name": "run_parallel"}},
        ]


def _schema_session(worker_available):
    return SimpleNamespace(
        _worker_delegation_available=lambda: worker_available,
        _tool_catalog=_Catalog(),
        _mcp=None,
        _tools_schema_snapshot=None,
        _invalidate_tools_schema=lambda: None,
        _task_profile="STANDARD",
        config=SimpleNamespace(no_delegation=False, browser_enabled=True),
    )


def test_unavailable_worker_verbs_are_absent_from_tool_schema():
    session = _schema_session(False)
    schema = ConversationalSession._build_visible_tools_schema(session)
    names = {item["function"]["name"] for item in schema}
    assert names == {"read_file"}


def test_available_worker_verbs_remain_in_tool_schema():
    session = _schema_session(True)
    schema = ConversationalSession._build_visible_tools_schema(session)
    names = {item["function"]["name"] for item in schema}
    assert names == {"read_file", "run_swarm", "run_implement", "run_parallel"}


def test_no_delegation_remains_leaf_role_not_pilot_identity(monkeypatch):
    monkeypatch.setattr("harness.edit_engines.workers_ready", lambda: True)
    leaf = SimpleNamespace(config=SimpleNamespace(no_delegation=True))
    pilot = SimpleNamespace(config=SimpleNamespace(no_delegation=False))
    assert ConversationalSession._worker_delegation_available(leaf) is False
    assert ConversationalSession._worker_delegation_available(pilot) is True


def test_system_prompt_does_not_force_every_multifile_task_to_workers():
    from harness.pilot import PILOT_SYSTEM
    assert "Own the task directly" in PILOT_SYSTEM
    assert "Native edit tools are available for coherent multi-file work" in PILOT_SYSTEM
    assert "Swarms are the product" not in PILOT_SYSTEM
    assert "SWARM FIRST for broad work" not in PILOT_SYSTEM
