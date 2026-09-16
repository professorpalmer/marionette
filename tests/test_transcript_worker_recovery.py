from harness.conversation_jobs import (
    _empty_implement_recovery_objective,
    _worker_provenance_text,
)
from harness.implement_guards import check_oversized_single_file_rewrite
from harness.pilot import PILOT_SYSTEM
from harness.todo import apply_todo_op


def test_recovery_does_not_promote_unrelated_dirty_paths_to_edit_targets():
    goal = "Fix widget.py rendering"
    text = _empty_implement_recovery_objective(goal)
    assert text.startswith(goal)
    assert "unrelated/private-note.md" not in text
    assert "were seeded" not in text
    assert "mandatory" not in text
    assert "original objective" in text


def test_specific_failure_precedes_generic_diff_and_dirty_details():
    text = _worker_provenance_text({
        "error": "agentic_orchestrator_failed",
        "failure_reason": "No model in registry has all required tags ['vision']",
        "worktree_diff_empty": True,
        "managed_worktree_path": "/long/managed/worktree/path",
        "live_dirty_paths_before": ["unrelated-note.md"],
    })
    assert "required tags ['vision']" in text[:200]
    assert text.index("required tags") < text.index("no changes")


def test_todo_accepts_unambiguous_init_shorthand_from_transcript():
    phases, errors, op = apply_todo_op([], {
        "init": [{"phase": "Verify", "items": ["Run tests"]}],
    })
    assert not errors
    assert op == "init"
    assert phases[0].tasks[0].content == "Run tests"


def test_large_file_does_not_force_multiple_writers_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_IMPLEMENT_FANOUT_GUARD", raising=False)
    (tmp_path / "module.py").write_text("line\n" * 300)
    assert check_oversized_single_file_rewrite("Rewrite module.py", str(tmp_path)) is None


def test_prompt_matches_supported_native_codex_pin():
    assert "only the agentic adapter" not in PILOT_SYSTEM
    assert "The only worker adapter" not in PILOT_SYSTEM
    assert "codex/gpt-6-astra" in PILOT_SYSTEM
