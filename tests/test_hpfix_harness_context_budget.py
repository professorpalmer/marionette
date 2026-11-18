from __future__ import annotations

from harness.context_budget import BudgetConfig, maybe_persist_result


def _config() -> BudgetConfig:
    return BudgetConfig(max_result_chars=2000, turn_budget_chars=48000, preview_chars=500)


def _log_with_marker() -> str:
    head = "line one\n... [omitted 12 characters] ...\nline three\n"
    return head + "".join(f"log line {i}\n" for i in range(2000))


def test_head_only_preview_with_marker_text_is_not_labelled_head_and_tail(tmp_path):
    out = maybe_persist_result(
        _log_with_marker(), "call_abc", str(tmp_path), _config(), tool_name="read_file"
    )
    assert "Preview (head and tail):" not in out
    assert "Preview (first " in out


def test_command_output_keeps_head_and_tail_label(tmp_path):
    out = maybe_persist_result(
        _log_with_marker(), "call_abc", str(tmp_path), _config(), tool_name="run_command"
    )
    assert "Preview (head and tail):" in out


def test_plain_read_file_keeps_first_chars_label(tmp_path):
    content = "".join(f"log line {i}\n" for i in range(2000))
    out = maybe_persist_result(
        content, "call_abc", str(tmp_path), _config(), tool_name="read_file"
    )
    assert "Preview (first " in out
