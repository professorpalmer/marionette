from __future__ import annotations

from harness.pilot import from_wire
from harness.send_loop_phases import LOCAL_ACTION_KINDS
from harness.todo import (
    apply_successful_landing,
    apply_successful_verification,
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
    should_fold_todo_landing,
    should_fold_todo_verification,
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
    host._todo_session_id = ToolDispatchMixin._todo_session_id.__get__(host)
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


def test_todo_dispatch_does_not_leak_across_sessions(tmp_path):
    from types import SimpleNamespace

    from harness.pilot import PilotAction
    from harness.tool_dispatch import ToolDispatchMixin

    def _host(session_id: str):
        host = SimpleNamespace(
            config=SimpleNamespace(state_dir=str(tmp_path)),
            state_dir=str(tmp_path),
            harness_session_id=session_id,
            _todo_store=None,
            _todo_phases=None,
        )
        host._todo_session_id = ToolDispatchMixin._todo_session_id.__get__(host)
        host._get_todo_store = ToolDispatchMixin._get_todo_store.__get__(host)
        host.reload_session_todos = ToolDispatchMixin.reload_session_todos.__get__(host)
        host.todo_snapshot = ToolDispatchMixin.todo_snapshot.__get__(host)
        return host

    arena = _host("sess-arena")
    act = PilotAction(
        kind="todo",
        arguments={"op": "init", "items": ["arena leftover"]},
    )
    ok, status, _val = ToolDispatchMixin._do_todo(arena, act)
    assert ok and status == "success"

    marionette = _host("sess-marionette")
    marionette.reload_session_todos()
    assert marionette.todo_snapshot()["phases"] == []


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


def test_store_isolates_todos_by_session_id(tmp_path):
    store = SessionTodoStore(str(tmp_path))
    arena, _, _ = _apply([], op="init", items=["beyblade wave"])
    marionette, _, _ = _apply([], op="init", items=["marionette only"])
    store.save(arena, session_id="sess-arena")
    store.save(marionette, session_id="sess-marionette")
    assert [t.content for t in store.load("sess-arena")[0].tasks] == ["beyblade wave"]
    assert [t.content for t in store.load("sess-marionette")[0].tasks] == ["marionette only"]
    assert store.load("sess-other") == []
    assert store.load() == []


def test_store_does_not_attach_legacy_phases_to_a_session(tmp_path):
    store = SessionTodoStore(str(tmp_path))
    phases, _, _ = _apply([], op="init", items=["stale other repo"])
    store.save(phases)
    assert store.load("sess-marionette") == []
    assert store.load()[0].tasks[0].content == "stale other repo"


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


def test_todo_slash_clear_removes_persisted_checklist():
    phases, _, _ = _apply([], op="init", list=[
        {"phase": "Release", "items": ["stale step"]},
    ])

    cleared = handle_todo_slash_command("/todo clear", phases)

    assert cleared.ok
    assert cleared.mutated
    assert cleared.phases == []
    assert cleared.public_dict()["todos"]["phases"] == []


def test_todo_matches_live_job_label():
    assert todo_matches_any_description("Sonnet #2: bug scan", ["Sonnet #2"]) is True
    assert todo_matches_any_description("fix", ["fixture loader"]) is False


def test_containment_misses_wave_landing_labels():
    wave = (
        "Implement versioned ruleset validator for part legality "
        "(BX vs CX, banlists, duplicate parts check)"
    )
    parser = (
        "Create src/lib/rulesets/parser.ts with a pure ruleset definition and "
        "validator for Beyblade X and deck legality constraints."
    )
    assert todo_matches_any_description(wave, [parser]) is False


def test_should_fold_todo_landing_requires_applied_files():
    assert should_fold_todo_landing(
        True, ["src/lib/rulesets/parser.ts"], failed=False, analysis_ok=False,
    )
    assert not should_fold_todo_landing(
        False, [], failed=True, error="agentic_no_diff",
    )
    assert not should_fold_todo_landing(
        False, [], analysis_ok=True,
    )


def test_successful_landing_completes_matching_wave_item():
    phases, _, _ = _apply([], op="init", list=[
        {"phase": "Wave 1 — Station & Stadium Operations", "items": [
            "Add stadium/station schema (SQLite & Postgres) + migration and station service logic",
            "Implement station management server actions and UI controls (station list, queue dispatch, match call)",
            "Validate Wave 1 with typecheck, lint, unit/integration tests, and build",
        ]},
        {"phase": "Wave 3 — Deck Legality & Ruleset Engine", "items": [
            "Implement versioned ruleset validator for part legality (BX vs CX, banlists, duplicate parts check)",
            "Validate Wave 3 with domain unit tests, typecheck, lint, and build",
        ]},
    ])
    phases[0].tasks[0].status = "completed"
    phases[0].tasks[1].status = "in_progress"

    parser = (
        "Create src/lib/rulesets/parser.ts with a pure ruleset definition and "
        "validator for Beyblade X and deck legality constraints."
    )
    nxt, hit = apply_successful_landing(phases, [parser, "src/lib/rulesets/parser.ts"])
    assert hit == phases[1].tasks[0].content
    assert nxt[1].tasks[0].status == "completed"
    assert nxt[1].tasks[1].status == "pending"
    assert nxt[0].tasks[1].status == "in_progress"

    scheduler = (
        "STATION MANAGEMENT SLICE — Create src/lib/stations/scheduler.ts "
        "for queueing and match allocation."
    )
    same, missed = apply_successful_landing(nxt, [scheduler, "src/lib/stations/scheduler.ts"])
    assert missed is None
    assert same[0].tasks[1].status == "in_progress"
    assert same[0].tasks[2].status == "pending"

    legality = "Create src/lib/deck/legality-rules.ts and src/lib/deck/legality-rules.test.ts"
    fresh, _, _ = _apply([], op="init", list=[
        {"phase": "Wave 3 — Deck Legality & Ruleset Engine", "items": [
            "Implement versioned ruleset validator for part legality (BX vs CX, banlists, duplicate parts check)",
            "Validate Wave 3 with domain unit tests, typecheck, lint, and build",
        ]},
    ])
    done, hit = apply_successful_landing(
        fresh, [legality, "src/lib/deck/legality-rules.ts"],
    )
    assert hit == fresh[0].tasks[0].content
    assert done[0].tasks[0].status == "completed"
    assert done[0].tasks[1].status != "completed"


def _session_8cc8_waves():
    phases, _, _ = _apply([], op="init", list=[
        {"phase": "Wave 1 — Station & Stadium Operations", "items": [
            "Add stadium/station schema (SQLite & Postgres) + migration and station service logic",
            "Implement station management server actions and UI controls (station list, queue dispatch, match call)",
            "Validate Wave 1 with typecheck, lint, unit/integration tests, and build",
        ]},
        {"phase": "Wave 2 — Live Spectator & Display Surfaces", "items": [
            "Implement dedicated live spectator TV / Kiosk display and stream overlay routes (/tournaments/[slug]/live and /overlay)",
            "Validate Wave 2 with typecheck, lint, and build",
        ]},
        {"phase": "Wave 3 — Deck Legality & Ruleset Engine", "items": [
            "Implement versioned ruleset validator for part legality (BX vs CX, banlists, duplicate parts check)",
            "Validate Wave 3 with domain unit tests, typecheck, lint, and build",
        ]},
    ])
    phases[0].tasks[0].status = "completed"
    phases[0].tasks[1].status = "completed"
    phases[0].tasks[2].status = "in_progress"
    phases[2].tasks[0].status = "completed"
    return phases


def test_should_fold_todo_verification_requires_ok_verify_command():
    assert should_fold_todo_verification("npm test", 0, "ok")
    assert should_fold_todo_verification("npm run typecheck && npm run lint", 0, "ok")
    assert should_fold_todo_verification("npx tsc --noEmit", 0, "success")
    assert should_fold_todo_verification("node scripts/tests/run.mjs unit", 0, "ok")
    assert should_fold_todo_verification("git status -s && npm test", 0, "ok")
    assert not should_fold_todo_verification("npm test", 1, "ok")
    assert not should_fold_todo_verification("npm test", 0, "error")
    assert not should_fold_todo_verification("git status", 0, "ok")
    assert not should_fold_todo_verification("echo test", 0, "ok")
    assert not should_fold_todo_verification(
        "git commit -am 'complete wave 2 adjudication'", 0, "ok",
    )


def test_successful_verification_completes_in_progress_validate_only():
    phases = _session_8cc8_waves()
    nxt, hit = apply_successful_verification(phases, ["npm test"])
    assert hit == phases[0].tasks[2].content
    assert nxt[0].tasks[2].status == "completed"
    assert nxt[1].tasks[0].status == "in_progress"
    assert nxt[1].tasks[1].status == "pending"
    assert nxt[2].tasks[1].status == "pending"

    same, missed = apply_successful_verification(phases, ["git status"])
    assert missed is None
    assert same[0].tasks[2].status == "in_progress"


def test_mixin_verification_persists_validate_gate(tmp_path):
    from types import SimpleNamespace

    from harness.pilot import PilotAction
    from harness.tool_dispatch import ToolDispatchMixin

    host = SimpleNamespace(
        config=SimpleNamespace(state_dir=str(tmp_path), repo=str(tmp_path)),
        state_dir=str(tmp_path),
        harness_session_id="8cc8a1c2281d",
        _todo_store=None,
        _todo_phases=None,
    )
    host._todo_session_id = ToolDispatchMixin._todo_session_id.__get__(host)
    host._get_todo_store = ToolDispatchMixin._get_todo_store.__get__(host)
    host._do_todo = ToolDispatchMixin._do_todo.__get__(host)
    host.apply_todo_verification = ToolDispatchMixin.apply_todo_verification.__get__(host)
    ok, status, _val = host._do_todo(PilotAction(kind="todo", arguments={
        "op": "init",
        "list": [
            {"phase": "Wave 1 — Station & Stadium Operations", "items": [
                "Add stadium/station schema (SQLite & Postgres) + migration and station service logic",
                "Validate Wave 1 with typecheck, lint, unit/integration tests, and build",
            ]},
        ],
    }))
    assert ok and status == "success"
    host._todo_phases[0].tasks[0].status = "completed"
    host._todo_phases[0].tasks[1].status = "in_progress"
    snap = host.apply_todo_verification("npm test", 0, "ok")
    assert snap and snap["op"] == "done"
    assert snap["phases"][0]["tasks"][1]["status"] == "completed"
    stored = SessionTodoStore(str(tmp_path)).load("8cc8a1c2281d")
    assert stored[0].tasks[1].status == "completed"


def test_mixin_slash_persists(tmp_path):
    from types import SimpleNamespace

    from harness.tool_dispatch import ToolDispatchMixin

    host = SimpleNamespace(
        config=SimpleNamespace(state_dir=str(tmp_path), repo=str(tmp_path)),
        state_dir=str(tmp_path),
        _todo_store=None,
        _todo_phases=None,
    )
    host._todo_session_id = ToolDispatchMixin._todo_session_id.__get__(host)
    host._get_todo_store = ToolDispatchMixin._get_todo_store.__get__(host)
    host.handle_todo_slash = ToolDispatchMixin.handle_todo_slash.__get__(host)
    first = host.handle_todo_slash("/todo append WP-02 hosted pari", workspace_root=str(tmp_path))
    assert first["ok"] and first["mutated"]
    second = host.handle_todo_slash("/todo export TODO.md", workspace_root=str(tmp_path))
    assert second["ok"]
    assert (tmp_path / "TODO.md").is_file()
    reloaded = SessionTodoStore(str(tmp_path)).load()
    assert reloaded[0].tasks[0].content == "hosted pari"


def test_mixin_landing_persists_matching_wave(tmp_path):
    from types import SimpleNamespace

    from harness.pilot import PilotAction
    from harness.tool_dispatch import ToolDispatchMixin

    host = SimpleNamespace(
        config=SimpleNamespace(state_dir=str(tmp_path), repo=str(tmp_path)),
        state_dir=str(tmp_path),
        harness_session_id="8cc8a1c2281d",
        _todo_store=None,
        _todo_phases=None,
    )
    host._todo_session_id = ToolDispatchMixin._todo_session_id.__get__(host)
    host._get_todo_store = ToolDispatchMixin._get_todo_store.__get__(host)
    host._do_todo = ToolDispatchMixin._do_todo.__get__(host)
    host.apply_todo_landing = ToolDispatchMixin.apply_todo_landing.__get__(host)
    ok, status, _val = host._do_todo(PilotAction(kind="todo", arguments={
        "op": "init",
        "list": [
            {"phase": "Wave 3 — Deck Legality & Ruleset Engine", "items": [
                "Implement versioned ruleset validator for part legality (BX vs CX, banlists, duplicate parts check)",
                "Validate Wave 3 with domain unit tests, typecheck, lint, and build",
            ]},
        ],
    }))
    assert ok and status == "success"
    snap = host.apply_todo_landing(
        "Create src/lib/rulesets/parser.ts with a ruleset validator and deck legality constraints.",
        ["src/lib/rulesets/parser.ts"],
    )
    assert snap and snap["op"] == "done"
    assert snap["phases"][0]["tasks"][0]["status"] == "completed"
    assert snap["phases"][0]["tasks"][1]["status"] != "completed"
    stored = SessionTodoStore(str(tmp_path)).load("8cc8a1c2281d")
    assert stored[0].tasks[0].status == "completed"


def test_drain_swarm_result_includes_todo_landing(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.pilot import PilotAction

    session = ConversationalSession(
        HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)),
    )
    session.harness_session_id = "8cc8a1c2281d"
    session.reload_session_todos()
    ok, status, _val = session._do_todo(PilotAction(kind="todo", arguments={
        "op": "init",
        "list": [
            {"phase": "Wave 3 — Deck Legality & Ruleset Engine", "items": [
                "Implement versioned ruleset validator for part legality (BX vs CX, banlists, duplicate parts check)",
                "Validate Wave 3 with domain unit tests, typecheck, lint, and build",
            ]},
        ],
    }))
    assert ok and status == "success"
    session._swarm_results.put({
        "job_id": "local-parser",
        "objective": (
            "Create src/lib/rulesets/parser.ts with a ruleset validator "
            "and deck legality constraints."
        ),
        "result": {
            "applied": True,
            "files": ["src/lib/rulesets/parser.ts"],
            "summary": "ok",
        },
    })
    events = list(session.drain_swarm_results())
    result_ev = next(event for event in events if event.kind == "swarm_result")
    assert result_ev.data["session_id"] == "8cc8a1c2281d"
    assert result_ev.data["todos"]["phases"][0]["tasks"][0]["status"] == "completed"
    assert result_ev.data["todos"]["phases"][0]["tasks"][1]["status"] != "completed"
    display = next(
        row for row in session._display_transcript
        if isinstance(row, dict) and row.get("type") == "swarm_result"
    )
    assert display["todos"]["phases"][0]["tasks"][0]["status"] == "completed"
