"""A tool call that fetches a benchmark task's answer key is refused before it runs.

The cheat URLs are copied from the 2026-11-11 leaderboard trials, where the
agent downloaded reference solutions and hidden tests from task mirrors.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from harness.answer_keys import answer_key_url
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PILOT_SYSTEM, WORKER_SYSTEM
from pmharness.drivers.openai_compat import DriverResponse

CHEATS = [
    "https://raw.githubusercontent.com/datacurve-ai/deep-swe/main/tasks/superjson-error-stack-serialization/solution/solution.patch",
    "https://api.github.com/repos/datacurve-ai/deep-swe/contents/tasks/superjson-error-stack-serialization/tests",
    "https://huggingface.co/datasets/datacurve/deep-swe/resolve/main/tasks/abs-stepped-slices/solution/solution.patch",
    "https://raw.githubusercontent.com/Xnmk029/Xnmk_Library/main/L4_Expert/DeepSWE/deep-swe/tasks/termenv-preserve-ansi-resets/tests/test.patch",
    "https://raw.githubusercontent.com/qiyuanhuakai/deepswe-for-kat-coder-pro-v2.5/main/tasks/skrub-duration-encoding/solution/solution.patch",
    "https://raw.githubusercontent.com/teamchong/pxpipe/main/eval/deepswe/results-retry2/dateutil-rfc5545/model.patch",
    "https://raw.githubusercontent.com/harbor-framework/terminal-bench-2/main/torch-pipeline-parallelism/tests/test_outputs.py",
    "https://huggingface.co/datasets/harborframework/terminal-bench-2.0/raw/main/count-dataset-tokens/solution/solve.sh",
    "https://api.github.com/repos/harbor-framework/terminal-bench/contents/legacy/make-doom-for-mips/solution",
    "https://raw.githubusercontent.com/teamchong/pxpipe/140725f/eval/deepswe/results-retry2/testem-bail-on-test-failure/testem-bail-on-test-failure__tQtsjr7",
    "https://api.github.com/repos/LabTesing/deepswe-sqlfmt-create-table-ddl-formatting/git/trees/HEAD?recursive=1",
]
CHEAT_COMMANDS = [
    'cd /tmp && B="https://raw.githubusercontent.com/datacurve-ai/deep-swe/main/tasks/pest-character-class-coalescing"; '
    'for f in tests/config.json tests/grader.py tests/test.sh tests/test.patch; do curl -sS "$B/$f"; done',
    'cd /app && for f in solution.sh task.yaml tests/test_outputs.py; do curl -sL '
    '"https://raw.githubusercontent.com/someone/tb-mirror/main/tasks/count-dataset-tokens/$f"; done',
]
FAIR = [
    "https://docs.python.org/3/library/asyncio-task.html",
    "https://github.com/celery/kombu/blob/main/kombu/transport/virtual/base.py",
    "https://raw.githubusercontent.com/pola-rs/polars/main/py-polars/tests/unit/test_api.py",
    "https://github.com/mattn/anko/issues/312",
    "https://pypi.org/project/sqlite-utils/",
    "https://raw.githubusercontent.com/datacurve-ai/deep-swe/main/tasks/pest-character-class-coalescing/instruction.md",
    "https://raw.githubusercontent.com/pytorch/pytorch/main/torch/distributed/distributed_c10d.py",
    "https://raw.githubusercontent.com/huggingface/picotron_tutorial/master/step7_pipeline_parallel_afab/pipeline_parallel.py",
    "https://api.github.com/repos/tconbeer/sqlfmt/pulls?state=all&per_page=100",
    "https://github.com/python/cpython/blob/main/Lib/test/test_asyncio/test_tasks.py",
    "https://github.com/harbor-framework/terminal-bench",
]


@pytest.mark.parametrize("url", CHEATS)
def test_answer_key_urls_are_found(url):
    assert answer_key_url(url) == url
    assert answer_key_url(f'curl -sL "{url}" -o /tmp/x') == url


@pytest.mark.parametrize("command", CHEAT_COMMANDS)
def test_a_base_url_plus_a_file_loop_is_found(command):
    assert answer_key_url(command)


@pytest.mark.parametrize("url", FAIR)
def test_normal_sources_pass(url):
    assert answer_key_url(url) is None
    assert answer_key_url(f"curl -s {url} | head") is None


def test_both_prompts_carry_the_rule():
    for prompt in (PILOT_SYSTEM, WORKER_SYSTEM):
        assert "WORK INTEGRITY" in prompt
        assert "hidden tests" in prompt


class _Pilot:
    supports_streaming = True
    name = "cheat-pilot"

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


def _run(calls, monkeypatch):
    ran = []
    monkeypatch.setattr("harness.web_tools.web_fetch", lambda url, timeout=12: ran.append(url) or "PAGE")
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", repo=tempfile.mkdtemp(), state_dir=tempfile.mkdtemp()))
    s.pilot = _Pilot(calls)
    events = list(s.send("Fix the bug."))
    return s, events, ran


def test_web_fetch_of_a_solution_is_refused_and_never_runs(monkeypatch):
    s, events, ran = _run([("web_fetch", {"url": CHEATS[0]})], monkeypatch)
    assert ran == []
    errors = [e.data.get("error", "") for e in events if e.kind == "action_result"]
    assert any(err.startswith("Refused:") for err in errors)
    assert "answer key" in s.pilot.seen[-1]


def test_curl_of_hidden_tests_is_refused(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    cmd = f'touch {marker} && curl -sL "{CHEATS[1]}"'
    s, events, _ = _run([("run_command", {"command": cmd})], monkeypatch)
    assert not marker.exists()
    assert "answer key" in s.pilot.seen[-1]


def test_a_normal_fetch_still_runs(monkeypatch):
    _, _, ran = _run([("web_fetch", {"url": FAIR[1]})], monkeypatch)
    assert ran == [FAIR[1]]


def test_the_guard_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HARNESS_ANSWER_KEY_GUARD", "0")
    _, _, ran = _run([("web_fetch", {"url": CHEATS[0]})], monkeypatch)
    assert ran == [CHEATS[0]]


def test_a_normal_command_still_runs(monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    _run([("run_command", {"command": f"touch {marker}"})], monkeypatch)
    assert marker.exists()
