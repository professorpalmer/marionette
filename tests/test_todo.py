from __future__ import annotations

from harness.pilot import from_wire
from harness.send_loop_phases import LOCAL_ACTION_KINDS
from harness.todo import (
    apply_todo_op,
    export_todo_markdown,
    format_todo_tree,
    handle_todo_slash_command,
    import_todo_markdown,
    infer_todo_op,
    markdown_to_phases,
    next_actionable_task,
    phases_from_raw,
    phases_to_markdown,
    SessionTodoStore,
    todo_matches_any_description,
)


def _apply(phases, **kwargs):
    nxt, errors, op = apply_todo_op(phases, kwargs)
    return nxt, errors, op


def test_init_list_and_normalize_first_pending():
    phases, errors, op = _apply([], op="init", list=[
        {"phase": "WP-02", "items": ["hosted pari", "service invariants"]},
        {"phase": "WP-03", "items": ["cutover"]},
    ])
    assert errors == []
    assert op == "init"
    assert [p.name for p in phases] == ["WP-02", "WP-03"]
    assert phases[0].tasks[0].status == "in_progress"
    assert phases[0].tasks[1].status == "pending"
    assert next_actionable_task(phases).content == "hosted pari"


def test_init_flat_items_without_op():
    phases, errors, op = apply_todo_op([], {"items": ["one", "two"]})
    assert errors == []
    assert op == "init"
    assert phases[0].name == "Tasks"
    assert [t.content for t in phases[0].tasks] == ["one", "two"]


def test_start_keeps_one_in_progress():
    phases, _, _ = _apply([], op="init", list=[
        {"phase": "A", "items": ["first", "second"]},
    ])
    phases, errors, _ = _apply(phases, op="start", task="second")
    assert errors == []
    assert phases[0].tasks[0].status == "pending"
    assert phases[0].tasks[1].status == "in_progress"


def test_done_by_phase_then_next_advances():
    phases, _, _ = _apply([], op="init", list=[
        {"phase": "A", "items": ["first", "second"]},
    ])
    phases, errors, _ = _apply(phases, op="done", phase="A")
    assert errors == []
    assert all(t.status == "completed" for t in phases[0].tasks)
    assert next_actionable_task(phases) is None


def test_append_creates_phase_and_rejects_duplicates():
    phases, _, _ = _apply([], op="init", items=["alpha"], phase="Core")
    phases, errors, _ = _apply(phases, op="append", phase="UI", items=["button"])
    assert errors == []
    assert [p.name for p in phases] == ["Core", "UI"]
    phases, errors, _ = _apply(phases, op="append", phase="UI", items=["button"])
    assert errors == ['Task "button" already exists']


def test_block_unblock_and_drop():
    phases, _, _ = _apply([], op="init", items=["alpha", "beta"])
    phases, errors, _ = _apply(phases, op="block", task="beta", reason="needs key")
    assert errors == []
    blocked = [t for t in phases[0].tasks if t.content == "beta"][0]
    assert blocked.status == "blocked"
    assert blocked.blocker == "needs key"
    phases, _, _ = _apply(phases, op="unblock", task="beta")
    assert [t for t in phases[0].tasks if t.content == "beta"][0].status == "pending"
    phases, _, _ = _apply(phases, op="drop", task="beta")
    assert [t for t in phases[0].tasks if t.content == "beta"][0].status == "abandoned"


def test_infer_append_when_phase_and_items():
    assert infer_todo_op({"items": ["x"], "phase": "Later"}, True) == "append"
    assert infer_todo_op({"items": ["x"]}, False) == "init"
    assert infer_todo_op({"task": "x"}, True) is None


def test_format_tree_folds_overflow():
    items = ["T%02d" % i for i in range(1, 8)]
    phases, _, _ = _apply([], op="init", list=[{"phase": "WP-02", "items": items}])
    text = format_todo_tree(phases)
    assert "TODO 0/7" in text
    assert "I. WP-02 · 0/7" in text
    assert "[>] T01" in text
    assert "... 2 more todos" in text
    assert "Next: T01" in text


def test_dispatch_persists_and_formats(tmp_path):
    from types import SimpleNamespace

    from harness.pilot import PilotAction
    from harness.tool_dispatch import ToolDispatchMixin

    host = SimpleNamespace(
        config=SimpleNamespace(state_dir=str(tmp_path)),
        state_dir=str(tmp_path),
        _todo_store=None,
        _todo_phases=None,
    )
    host._get_todo_store = ToolDispatchMixin._get_todo_store.__get__(host)
    host.todo_snapshot = ToolDispatchMixin.todo_snapshot.__get__(host)
    act = PilotAction(
        kind="todo",
        arguments={"op": "init", "list": [{"phase": "WP-02", "items": ["hosted pari", "invariants"]}]},
    )
    ok, status, val = ToolDispatchMixin._do_todo(host, act)
    assert ok and status == "success"
    tree, payload = val
    assert "TODO 0/2" in tree
    assert payload["next"] == "hosted pari"
    assert host.todo_snapshot()["phases"][0]["name"] == "WP-02"
    reloaded = SessionTodoStore(str(tmp_path)).load()
    assert reloaded[0].tasks[0].content == "hosted pari"


def test_todo_is_a_local_action_and_from_wire():
    assert "todo" in LOCAL_ACTION_KINDS
    act = from_wire("todo", {"op": "view"})
    assert act.kind == "todo"
    assert act.arguments.get("op") == "view"


def test_store_round_trip(tmp_path):
    store = SessionTodoStore(str(tmp_path))
    phases, _, _ = _apply([], op="init", items=["one"])
    store.save(phases)
    loaded = store.load()
    assert phases_from_raw([p.to_dict() for p in loaded])[0].tasks[0].content == "one"


def test_markdown_round_trip_preserves_blockers():
    phases, _, _ = _apply([], op="init", list=[
        {"phase": "WP-02", "items": ["hosted pari", "cutover"]},
    ])
    phases, _, _ = _apply(phases, op="block", task="cutover", reason="needs key")
    md = phases_to_markdown(phases)
    assert "# WP-02" in md
    assert "- [/] hosted pari" in md
    assert "- [!] cutover <!-- blocker: needs key -->" in md
    parsed, errors = markdown_to_phases(md)
    assert errors == []
    assert parsed[0].tasks[1].status == "blocked"
    assert parsed[0].tasks[1].blocker == "needs key"


def test_markdown_markers_and_unknown_syntax():
    parsed, errors = markdown_to_phases(
        "# A\n- [x] done\n- [>] live\n- [-] dropped\n- [?] bad\nnot a task\n"
    )
    assert [t.status for t in parsed[0].tasks] == ["completed", "in_progress", "abandoned"]
    assert any("unknown status marker" in err for err in errors)
    assert any("unrecognized syntax" in err for err in errors)


def test_export_import_stays_workspace_confined(tmp_path):
    phases, _, _ = _apply([], op="init", items=["alpha"])
    _abs_path, rel = export_todo_markdown(phases, str(tmp_path), "notes/TODO.md")
    assert rel == "notes/TODO.md"
    assert (tmp_path / "notes" / "TODO.md").is_file()
    imported, errors, rel2 = import_todo_markdown(str(tmp_path), "notes/TODO.md")
    assert errors == []
    assert rel2 == "notes/TODO.md"
    assert imported[0].tasks[0].content == "alpha"
    try:
        export_todo_markdown(phases, str(tmp_path), "../escape.md")
        raise AssertionError("escaped write")
    except ValueError as exc:
        assert "escapes workspace" in str(exc)


def test_slash_view_done_and_fuzzy_start(tmp_path):
    phases, _, _ = _apply([], op="init", list=[
        {"phase": "WP-02", "items": ["hosted pari", "service invariants"]},
    ])
    viewed = handle_todo_slash_command("/todo", phases)
    assert viewed.ok and "TODO 0/2" in viewed.tree
    started = handle_todo_slash_command("/todo start invariants", phases)
    assert started.ok and started.mutated
    assert started.phases[0].tasks[1].status == "in_progress"
    done = handle_todo_slash_command("/todo done hosted", started.phases)
    assert done.phases[0].tasks[0].status == "completed"
    exported = handle_todo_slash_command("/todo export TODO.md", done.phases, str(tmp_path))
    assert exported.ok and exported.path == "TODO.md"
    (tmp_path / "TODO.md").write_text("# Later\n- [ ] imported task\n", encoding="utf-8")
    imported = handle_todo_slash_command("/todo import", done.phases, str(tmp_path))
    assert imported.ok and imported.mutated
    assert imported.phases[0].tasks[0].content == "imported task"
    denied = handle_todo_slash_command("/todo export ../x.md", done.phases, str(tmp_path))
    assert not denied.ok


def test_todo_matches_live_job_label():
    assert todo_matches_any_description("Sonnet #2: bug scan", ["Sonnet #2"]) is True
    assert todo_matches_any_description("fix", ["fixture loader"]) is False


def test_mixin_slash_persists(tmp_path):
    from types import SimpleNamespace

    from harness.tool_dispatch import ToolDispatchMixin

    host = SimpleNamespace(
        config=SimpleNamespace(state_dir=str(tmp_path), repo=str(tmp_path)),
        state_dir=str(tmp_path),
        _todo_store=None,
        _todo_phases=None,
    )
    host._get_todo_store = ToolDispatchMixin._get_todo_store.__get__(host)
    host.handle_todo_slash = ToolDispatchMixin.handle_todo_slash.__get__(host)
    first = host.handle_todo_slash("/todo append WP-02 hosted pari", workspace_root=str(tmp_path))
    assert first["ok"] and first["mutated"]
    second = host.handle_todo_slash("/todo export TODO.md", workspace_root=str(tmp_path))
    assert second["ok"]
    assert (tmp_path / "TODO.md").is_file()
    reloaded = SessionTodoStore(str(tmp_path)).load()
    assert reloaded[0].tasks[0].content == "hosted pari"
