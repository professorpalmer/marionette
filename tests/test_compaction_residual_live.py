from __future__ import annotations

"""Hermetic tests for the Layer-1 live compaction residual runner."""

import inspect
import json
import os
from types import SimpleNamespace

from harness.compaction_residual import (
    CATALOG_HEADING,
    HYBRID_INDEX_HEADING,
    RESIDUAL_CATALOG,
    RESIDUAL_SUMMARY,
    compaction_residual_mode,
)
from pmharness.compaction_residual_battery import (
    ABSENT_LIVE_TOKEN,
    LIVE_HOLDOUT_CASES,
    NONCE_ALLOWED_WRITE,
    NONCE_FORBIDDEN_WRITE,
    RESIDUAL_CASES,
    ResidualCase,
    cases_by_id,
    live_cases,
)
from pmharness.compaction_residual_bench import score_residual_text
from pmharness.compaction_residual_live import (
    ALL_ARMS,
    ARM_A,
    ARM_B,
    ARM_C,
    ARM_D,
    LIVE_ARM_RESIDUAL,
    RECEIPT_SCHEMA,
    SCORER_VERSION,
    InstrumentedPilot,
    UsageRecorder,
    aggregate_rows,
    apply_lab_visible_tools,
    case_has_durable_handles,
    dry_run_plan,
    evaluate_live_gates,
    final_assistant_prose,
    lab_tool_names,
    lab_visible_tool_schema,
    main,
    peek_metrics_from_events_and_history,
    rescore_receipt_rows,
    run_compaction_residual_live,
    run_compaction_rounds,
    run_live_arm,
    score_end_task_text,
    sum_usage,
    usage_from_response,
)


class FakeEvent:
    def __init__(self, kind: str, data: dict | None = None) -> None:
        self.kind = kind
        self.data = data or {}


class FakePilot:
    name = "fake-live"
    supports_streaming = False

    def __init__(self, text: str = "ok", **resp: object) -> None:
        self.text = text
        self.resp = resp
        self.chat_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []

    def _response(self):
        return SimpleNamespace(
            text=self.text,
            error=None,
            tokens_in=self.resp.get("tokens_in", 11),
            tokens_out=self.resp.get("tokens_out", 5),
            latency_ms=self.resp.get("latency_ms", 3.0),
            model=self.resp.get("model", "fake-model"),
            meta=self.resp.get("meta", {}),
        )

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system, tools))
        return self._response()

    def complete(self, prompt, system=None):
        self.complete_calls.append((prompt, system))
        return self.chat([{"role": "user", "content": prompt}], system=system)


class FakeStreamingPilot:
    """Keyless streaming inner pilot. Returns usage on the synchronous DriverResponse."""

    name = "fake-streaming"
    supports_streaming = True

    def __init__(self, text: str = "ok", **resp: object) -> None:
        self.text = text
        self.resp = resp
        self.chat_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []
        self.chat_stream_calls: list[tuple] = []

    def _response(self):
        return SimpleNamespace(
            text=self.text,
            error=None,
            tokens_in=self.resp.get("tokens_in", 17),
            tokens_out=self.resp.get("tokens_out", 6),
            latency_ms=self.resp.get("latency_ms", 4.0),
            model=self.resp.get("model", "fake-stream-model"),
            meta=self.resp.get("meta", {}),
        )

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system, tools))
        return self._response()

    def complete(self, prompt, system=None):
        self.complete_calls.append((prompt, system))
        return self.chat([{"role": "user", "content": prompt}], system=system)

    def chat_stream(
        self,
        messages,
        *args,
        tools=None,
        system=None,
        on_delta=None,
        session_id=None,
        on_reasoning_delta=None,
        on_tool_hint=None,
        **kwargs,
    ):
        self.chat_stream_calls.append(
            (messages, system, tools, on_delta, session_id, args, kwargs)
        )
        if on_delta is not None:
            on_delta(self.text)
        if on_reasoning_delta is not None:
            on_reasoning_delta("")
        return self._response()


class FakeSession:
    def __init__(
        self,
        *,
        compact_mode: str = "llm",
        abort: bool = False,
        answer: str = "",
        compact_residual: str = "",
        peek_events: list[FakeEvent] | None = None,
        peek_history_rows: list[dict] | None = None,
        raise_on_send: Exception | None = None,
    ) -> None:
        self.config = SimpleNamespace(driver="fake:driver")
        self.state_dir = ""
        self.harness_session_id = ""
        self._history: list[dict] = []
        self.pilot = FakePilot(text=answer or "assistant")
        self.compact_calls: list[bool] = []
        self.send_calls: list[str] = []
        self.saved: list[tuple] = []
        self.compact_mode = compact_mode
        self.abort = abort
        self.answer = answer
        self.compact_residual = compact_residual
        self.peek_events = peek_events or []
        self.peek_history_rows = peek_history_rows or []
        self.raise_on_send = raise_on_send
        self._do_peek_history_calls = 0

    def _estimate_context_tokens(self) -> int:
        return 120 if not self.compact_calls else 40

    def _maybe_compact_history(self, force: bool = False):
        self.compact_calls.append(force)
        if self.compact_mode == "llm" and hasattr(self.pilot, "chat"):
            self.pilot.chat(
                [{"role": "user", "content": "summarize residual"}],
                system="compact",
            )
        yield FakeEvent("compacting", {"message": "Summarizing chat context"})
        yield FakeEvent("compaction", {
            "before_tokens": 120,
            "after_tokens": 40,
            "mode": self.compact_mode,
            "aborted": self.abort,
            "reason": "ok" if not self.abort else "summary_rejected",
        })
        if not self.abort:
            self._history = [
                self._history[0] if self._history else {"role": "system", "content": "sys"},
                {
                    "role": "user",
                    "content": "[Earlier conversation summarized to fit context]\n"
                    + (
                        (self.compact_residual + "\n")
                        if self.compact_residual
                        else ""
                    )
                    + (
                        "## Historical Task Snapshot\ncompacted\n"
                        "## Resolved\nnone\n"
                        "## Pending / Open Questions\nnone\n"
                        "## Key Facts / Decisions / Files\nfile\n"
                    ),
                    "_compressed_summary": True,
                },
            ]

    def send(self, prompt: str):
        self.send_calls.append(prompt)
        if self.raise_on_send is not None:
            raise self.raise_on_send
        can_stream = (
            getattr(self.pilot, "supports_streaming", False) is True
            and callable(getattr(self.pilot, "chat_stream", None))
        )
        if can_stream:
            self.pilot.chat_stream(
                [{"role": "user", "content": prompt}],
                on_delta=lambda _delta: None,
                session_id=self.harness_session_id or None,
            )
        elif hasattr(self.pilot, "chat"):
            self.pilot.chat([{"role": "user", "content": prompt}])
        for event in self.peek_events:
            yield event
        for row in self.peek_history_rows:
            self._history.append(row)
        yield FakeEvent("message", {"role": "assistant", "text": self.answer})
        yield FakeEvent("assistant_done", {"turns": 1})

    def export_transcript_data(self) -> dict:
        return {
            "history": [row for row in self._history if row.get("role") != "system"],
            "display": [],
            "job_ids": [],
        }

    def _do_peek_history(self, act):
        self._do_peek_history_calls += 1
        raise AssertionError("live runner must not script peek_history")

    def _build_visible_tools_schema(self) -> list:
        return [{"type": "function", "function": {"name": "read_file"}}]


def _factory_for(session: FakeSession):
    def factory(driver: str, state_dir: str, max_context_tokens: int) -> FakeSession:
        session.state_dir = state_dir
        session.config = SimpleNamespace(driver=driver, max_context_tokens=max_context_tokens)
        return session

    return factory


def _case(case_id: str = "early_constraint") -> ResidualCase:
    return cases_by_id()[case_id]


def test_four_arm_map_is_summary_hybrid_catalog_off():
    assert LIVE_ARM_RESIDUAL == {
        ARM_A: "summary",
        ARM_B: "hybrid",
        ARM_C: "catalog",
        ARM_D: "off",
    }
    assert ALL_ARMS == (ARM_A, ARM_B, ARM_C, ARM_D)
    assert RECEIPT_SCHEMA == "compaction_residual_live/v2"
    assert SCORER_VERSION == "end_task/v2"


def test_dry_run_never_builds_a_provider(monkeypatch):
    def boom(spec: str, **kwargs):
        raise AssertionError(f"dry-run must not build provider {spec}")

    monkeypatch.setattr("harness.providers.build_pilot", boom)
    monkeypatch.setattr(
        "harness.conversation.ConversationalSession",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run built a session")),
    )
    result = run_compaction_residual_live(
        live=False,
        driver="openrouter:should-not-build",
        case_ids=["early_constraint"],
        arms=[ARM_A],
    )
    assert result["dry_run"] is True
    assert result["schema"] == RECEIPT_SCHEMA
    assert result["protocol"] == "compaction_residual_live"
    assert result["arms"] == [ARM_A]
    assert result["arm_residual"][ARM_A] == "summary"
    assert result["cases"] == ["early_constraint"]
    assert "winner" not in result


def test_cli_dry_run_default_and_validation(capsys, tmp_path):
    assert main([]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dry_run"] is True
    assert printed["schema"] == RECEIPT_SCHEMA
    assert printed["protocol"] == "compaction_residual_live"
    assert printed["suite"] == "core"
    assert printed["cases"] == [case.id for case in RESIDUAL_CASES]
    assert printed["arms"] == list(ALL_ARMS)

    assert main(["--rounds", "2"]) == 2
    rounds_payload = json.loads(capsys.readouterr().out)
    assert rounds_payload["end_task_success"] is False
    assert "--rounds" in rounds_payload["failure"]

    assert main(["--repeats", "2"]) == 2
    repeats_payload = json.loads(capsys.readouterr().out)
    assert "--repeats" in repeats_payload["failure"]

    assert main(["--live"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == RECEIPT_SCHEMA
    assert payload["end_task_success"] is False
    assert "winner" not in payload
    assert "driver" in payload["failure"]


def test_cli_provider_error_is_honest_failure(monkeypatch, capsys):
    from harness.providers import ProviderError

    monkeypatch.setattr(
        "pmharness.compaction_residual_live._verify_driver",
        lambda driver, fn: (_ for _ in ()).throw(ProviderError("no provider key")),
    )
    code = main(["--live", "--driver", "openrouter:missing", "--case", "early_constraint", "--arm", "A"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == RECEIPT_SCHEMA
    assert payload["status"] == "provider_error"
    assert payload["end_task_success"] is False
    assert "winner" not in payload


def test_abc_repeat_rounds_and_d_never_compacts(tmp_path):
    case = _case()
    saves: list[str] = []

    def save(state_dir, session_id, data):
        saves.append(session_id)

    sessions: dict[str, FakeSession] = {}

    for arm in ALL_ARMS:
        isolated = tmp_path / arm
        isolated.mkdir()

        def factory(driver, state_dir, max_context_tokens, current=arm):
            session = FakeSession(
                compact_mode="extractive" if current == ARM_C else "llm",
                answer="never write to production.db; use scratch.sqlite only.",
            )
            sessions[current] = session
            session.state_dir = state_dir
            return session

        row = run_live_arm(
            case,
            arm,
            driver="fake:driver",
            rounds=3,
            seed=7,
            repeat_index=0,
            state_dir=str(isolated),
            session_factory=factory,
            save_transcript_fn=save,
        )
        assert row["schema"] == RECEIPT_SCHEMA
        assert row["residual_mode"] == LIVE_ARM_RESIDUAL[arm]
        assert row["driver"] == "fake:driver"
        if arm == ARM_D:
            assert sessions[arm].compact_calls == []
            assert row["compact_rounds"] == []
        else:
            assert sessions[arm].compact_calls == [True, True, True]
            assert len(row["compact_rounds"]) == 3
            assert [item["round"] for item in row["compact_rounds"]] == [1, 2, 3]
        assert sessions[arm].send_calls == [case.probe_prompt]
        assert sessions[arm]._do_peek_history_calls == 0

    assert any(sid.endswith("-A-0") for sid in saves)
    assert any(sid.endswith("-B-0") for sid in saves)
    assert any(sid.endswith("-C-0") for sid in saves)
    assert not any(sid.endswith("-D-0") for sid in saves)


def test_probe_goes_through_send_and_never_scripts_peek():
    source = inspect.getsource(
        __import__("pmharness.compaction_residual_live", fromlist=["run_live_arm"])
    )
    assert "_do_peek_history" not in source
    assert "_peek_windows" not in source
    assert "session.send(case.probe_prompt)" in source


def test_autonomous_peek_metrics_from_send_events(tmp_path):
    case = _case()
    peek_body = (
        "(peek_history returned)\n"
        "never write to production.db; use scratch.sqlite only."
    )
    session = FakeSession(
        answer="I need to look that up.",
        peek_events=[
            FakeEvent("action_result", {
                "id": "p1",
                "types": ["peek_history"],
            }),
            FakeEvent("action_result", {
                "id": "p2",
                "error": "stale: expected_generation=1 current compaction_generation=2",
            }),
        ],
        peek_history_rows=[
            {"role": "tool", "content": peek_body},
            {
                "role": "tool",
                "content": (
                    "(peek_history failed: stale: expected_generation=1 "
                    "current compaction_generation=2)"
                ),
            },
        ],
    )
    row = run_live_arm(
        case,
        ARM_A,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    assert row["peek_calls"] == 2
    assert row["peek_success"] == 1
    assert row["peek_stale"] == 1
    assert row["stale_generation"] is True
    assert row["peek_tokens"] > 0
    assert row["peek_diagnostic_recall"] is True
    assert session._do_peek_history_calls == 0


def test_hybrid_fallback_is_not_summarizer_ok(tmp_path):
    case = _case()
    session = FakeSession(
        compact_mode="extractive",
        answer="never write to production.db; use scratch.sqlite only.",
    )
    row = run_live_arm(
        case,
        ARM_B,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    assert row["residual_mode"] == "hybrid"
    assert row["summarizer_ok"] is False
    assert all(not item["summarizer_ok"] for item in row["compact_rounds"])
    assert row["end_task_success"] is False
    assert row["task_recall"] is True


def test_receipt_schema_and_nullable_cost_truth(tmp_path):
    case = _case()
    session = FakeSession(
        answer="never write to production.db; use scratch.sqlite only.",
    )
    session.pilot = FakePilot(
        text="ok",
        tokens_in=20,
        tokens_out=4,
        meta={"raw_usage": {"prompt_tokens": 20, "completion_tokens": 4}},
    )
    row = run_live_arm(
        case,
        ARM_A,
        driver="fake:driver",
        rounds=3,
        seed=3,
        repeat_index=2,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    required = {
        "schema", "arm", "case_id", "template", "residual_mode", "driver",
        "model", "seed", "repeat_index", "compact_rounds", "prompt_tokens",
        "output_tokens", "cache_read_tokens", "cache_write_tokens",
        "provider_cost_usd", "estimated_cost_usd", "latency_ms",
        "tokens_in_basis",
        "peek_calls", "peek_success", "peek_stale", "stale_generation",
        "peek_tokens", "task_recall", "false_recall", "stale_recall",
        "residual_recall", "residual_recall_round1", "residual_text",
        "event_kinds", "failure", "status",
        "final_answer", "final_answer_preview", "scorer_version",
        "end_task_success",
    }
    assert required <= set(row)
    assert row["schema"] == RECEIPT_SCHEMA
    assert row["scorer_version"] == SCORER_VERSION
    assert row["provider_cost_usd"] is None
    assert row["tokens_in_basis"] == "provider"
    assert row["status"] == "ok"
    assert "winner" not in row

    recorded = usage_from_response(
        SimpleNamespace(
            text="x",
            tokens_in=8,
            tokens_out=2,
            latency_ms=9.0,
            model="priced",
            error=None,
            meta={"provider_cost_usd": 0.0, "cache_read_tokens": 3, "raw_usage": {"cost": 0.0}},
        ),
        phase="end_task",
        error=None,
        latency_ms=1.0,
        driver="fake:driver",
    )
    assert recorded["provider_cost_usd"] == 0.0
    assert recorded["tokens_in_basis"] == "provider"
    unknown = usage_from_response(
        SimpleNamespace(
            text="x",
            tokens_in=0,
            tokens_out=0,
            latency_ms=1.0,
            model="",
            error=None,
            meta={},
        ),
        phase="compaction",
        error=None,
        latency_ms=1.0,
        driver="unknown-slug/that-has-no-price",
    )
    assert unknown["provider_cost_usd"] is None
    assert unknown["estimated_cost_usd"] is None
    assert unknown["tokens_in_basis"] == "unknown"


def test_env_restored_and_state_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_RESIDUAL", "summary")
    monkeypatch.setenv("HARNESS_COMPACTION_MODEL", "ambient-summarizer")
    case = _case()
    seen_env: list[str] = []
    seen_model: list[str] = []
    seen_dirs: list[str] = []

    def factory(driver, state_dir, max_context_tokens):
        seen_env.append(os.environ.get("HARNESS_COMPACTION_RESIDUAL", ""))
        seen_model.append(os.environ.get("HARNESS_COMPACTION_MODEL"))
        seen_dirs.append(state_dir)
        session = FakeSession(answer="never write to production.db; use scratch.sqlite")
        session.state_dir = state_dir
        return session

    run_compaction_residual_live(
        live=True,
        driver="fake:driver",
        arms=[ARM_B, ARM_C],
        case_ids=["early_constraint"],
        rounds=3,
        repeats=3,
        seed=1,
        state_dir=str(tmp_path),
        session_factory=factory,
        save_transcript_fn=lambda *a, **k: None,
    )
    assert os.environ.get("HARNESS_COMPACTION_RESIDUAL") == "summary"
    assert os.environ.get("HARNESS_COMPACTION_MODEL") == "ambient-summarizer"
    assert compaction_residual_mode() == RESIDUAL_SUMMARY
    assert seen_env.count("hybrid") == 3
    assert seen_env.count("catalog") == 3
    assert seen_model == [""] * 6
    assert len(set(seen_dirs)) == 6
    assert all(str(tmp_path) in path for path in seen_dirs)


def test_deterministic_scoring_matches_layer0_oracle():
    case = ResidualCase(
        id="unit",
        template="early_constraint",
        transcript=({"role": "system", "content": "s"},),
        probe_prompt="p",
        must_contain=("alpha-fact", "gamma-fact"),
        must_not_contain=("beta-fab",),
    )
    hit = score_residual_text(case, "the ALPHA-FACT and GAMMA-FACT are present")
    assert hit["end_task_success"] is True
    events = [FakeEvent("message", {"role": "assistant", "text": "ALPHA-FACT and GAMMA-FACT"})]
    assert "alpha-fact" in final_assistant_prose(events, []).lower()
    peek = peek_metrics_from_events_and_history(
        [FakeEvent("action_result", {"types": ["peek_history"]})],
        [{"role": "tool", "content": "(peek_history returned)\nALPHA-FACT GAMMA-FACT"}],
        case,
    )
    assert peek["peek_diagnostic_recall"] is True
    assert peek["stale_recall"] is False


def test_end_task_failure_is_honest_receipt(tmp_path):
    case = _case()
    session = FakeSession(raise_on_send=RuntimeError("provider dropped"))
    row = run_live_arm(
        case,
        ARM_A,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    assert row["status"] == "end_task_failed"
    assert row["end_task_success"] is False
    assert row["failure"] == "provider dropped"
    assert row["provider_cost_usd"] is None
    assert row["tokens_in_basis"] == "provider"
    assert row["residual_text"] == ""
    assert row["residual_recall"] is False
    assert row["residual_recall_round1"] is False
    assert row["final_answer"] == ""
    assert row["scorer_version"] == SCORER_VERSION


def test_lab_schema_is_peek_only_and_additive():
    handle_case = _case("spill_artifact_handle")
    plain_case = _case("catalog_miss_plain_fact")
    assert case_has_durable_handles(handle_case) is True
    assert "peek_artifact" in lab_tool_names(handle_case)
    assert lab_tool_names(plain_case) == ("peek_history",)
    assert lab_tool_names(_case("vault_only_prose_cutoff")) == ()
    schema = lab_visible_tool_schema(plain_case)
    names = [(item.get("function") or {}).get("name") for item in schema]
    assert names == ["peek_history"]
    assert "read_file" not in names
    assert "search_files" not in names
    session = FakeSession()
    apply_lab_visible_tools(session, plain_case)
    applied = [item["function"]["name"] for item in session._build_visible_tools_schema()]
    assert applied == ["peek_history"]


def test_instrumented_pilot_records_phase_and_forwards():
    inner = FakePilot(text="sum", tokens_in=9, tokens_out=2, model="wrap-model")
    recorder = UsageRecorder("fake:driver")
    wrapped = InstrumentedPilot(inner, recorder)
    recorder.phase = "compaction"
    wrapped.chat([{"role": "user", "content": "sum"}])
    recorder.phase = "end_task"
    wrapped.complete("probe")
    assert [row["phase"] for row in recorder.calls] == ["compaction", "end_task"]
    assert recorder.calls[0]["model"] == "wrap-model"
    assert recorder.calls[0]["tokens_in_basis"] == "provider"
    assert inner.chat_calls
    assert inner.complete_calls


def test_instrumented_pilot_records_chat_stream_end_task():
    inner = FakeStreamingPilot(
        text="streamed",
        tokens_in=17,
        tokens_out=6,
        model="stream-model",
        meta={
            "cache_read_tokens": 3,
            "cache_write_tokens": 1,
            "provider_cost_usd": 0.012,
        },
    )
    recorder = UsageRecorder("fake:driver")
    wrapped = InstrumentedPilot(inner, recorder)
    assert wrapped.supports_streaming is True
    deltas: list[str] = []
    on_delta = deltas.append
    recorder.phase = "end_task"
    resp = wrapped.chat_stream(
        [{"role": "user", "content": "probe"}],
        on_delta=on_delta,
        session_id="sess-1",
    )
    assert resp.text == "streamed"
    assert deltas == ["streamed"]
    assert len(inner.chat_stream_calls) == 1
    _messages, _system, _tools, forwarded_on_delta, session_id, _args, _kwargs = (
        inner.chat_stream_calls[0]
    )
    assert forwarded_on_delta is on_delta
    assert session_id == "sess-1"
    assert len(recorder.calls) == 1
    row = recorder.calls[0]
    assert row["phase"] == "end_task"
    assert row["tokens_in"] == 17
    assert row["tokens_out"] == 6
    assert row["cache_read_tokens"] == 3
    assert row["cache_write_tokens"] == 1
    assert row["provider_cost_usd"] == 0.012
    assert row["model"] == "stream-model"
    assert row["tokens_in_basis"] == "provider"


def test_live_battery_with_factory_repeats_and_no_winner(tmp_path):
    def factory(driver, state_dir, max_context_tokens):
        answer = "never write to production.db; use scratch.sqlite"
        session = FakeSession(answer=answer, compact_mode="llm")
        session.state_dir = state_dir
        return session

    result = run_compaction_residual_live(
        live=True,
        driver="fake:driver",
        arms=[ARM_A, ARM_D],
        case_ids=["early_constraint"],
        rounds=3,
        repeats=3,
        seed=4,
        state_dir=str(tmp_path),
        session_factory=factory,
        save_transcript_fn=lambda *a, **k: None,
    )
    assert result["schema"] == RECEIPT_SCHEMA
    assert result["n"] == 6
    assert "winner" not in result
    assert set(result["by_arm"]) == {ARM_A, ARM_D}
    assert all(row["schema"] == RECEIPT_SCHEMA for row in result["rows"])


def test_default_residual_mode_untouched_by_live_import(monkeypatch):
    monkeypatch.delenv("HARNESS_COMPACTION_RESIDUAL", raising=False)
    assert compaction_residual_mode() == RESIDUAL_CATALOG
    assert CATALOG_HEADING
    assert HYBRID_INDEX_HEADING


def test_dry_run_plan_lists_selected_cases_and_arms():
    plan = dry_run_plan(
        arms=[ARM_B, ARM_C],
        cases=[_case(), _case("spill_artifact_handle")],
        rounds=3,
        repeats=3,
        seed=9,
        driver="anthropic:claude",
    )
    assert plan["cases"] == ["early_constraint", "spill_artifact_handle"]
    assert plan["arm_residual"] == {ARM_B: "hybrid", ARM_C: "catalog"}
    assert plan["driver"] == "anthropic:claude"


def test_peek_metrics_prefer_history_markers_over_typeless_stale_event():
    case = _case()
    peek = peek_metrics_from_events_and_history(
        [
            FakeEvent("action_result", {"id": "p1", "types": ["peek_history"]}),
            FakeEvent("action_result", {
                "id": "p2",
                "error": "stale: expected_generation=1 current compaction_generation=2",
            }),
        ],
        [
            {
                "role": "tool",
                "content": (
                    "(peek_history returned)\n"
                    "never write to production.db; use scratch.sqlite only."
                ),
            },
            {
                "role": "tool",
                "content": (
                    "(peek_history failed: stale: expected_generation=1 "
                    "current compaction_generation=2)"
                ),
            },
        ],
        case,
    )
    assert peek["peek_calls"] == 2
    assert peek["peek_success"] == 1
    assert peek["peek_stale"] == 1
    assert peek["stale_generation"] is True
    assert peek["peek_diagnostic_recall"] is True
    assert "(peek_history returned)" in peek["peek_text"]
    assert "(peek_history failed:" not in peek["peek_text"]


def test_peek_metrics_events_fallback_when_no_history_markers():
    case = _case()
    peek = peek_metrics_from_events_and_history(
        [
            FakeEvent("action_result", {"id": "p1", "types": ["peek_history"]}),
            FakeEvent("action_result", {
                "id": "p2",
                "types": ["peek_history"],
                "error": "stale: expected_generation=1 current compaction_generation=2",
            }),
        ],
        [{"role": "tool", "content": "unrelated tool output"}],
        case,
    )
    assert peek["peek_calls"] == 2
    assert peek["peek_success"] == 1
    assert peek["peek_stale"] == 1
    assert peek["peek_text"] == ""


def test_compact_rounds_attribute_usage_from_recorder(tmp_path):
    case = _case()
    session = FakeSession(
        answer="never write to production.db; use scratch.sqlite only.",
    )
    session.pilot = FakePilot(
        text="ok",
        tokens_in=20,
        tokens_out=4,
        model="round-model",
        meta={
            "cache_read_tokens": 2,
            "cache_write_tokens": 1,
            "provider_cost_usd": 0.0,
        },
    )
    row = run_live_arm(
        case,
        ARM_A,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    assert len(row["compact_rounds"]) == 3
    for item in row["compact_rounds"]:
        assert item["tokens_in"] == 20
        assert item["tokens_out"] == 4
        assert item["cache_read_tokens"] == 2
        assert item["cache_write_tokens"] == 1
        assert item["provider_cost_usd"] == 0.0
        assert item["estimated_cost_usd"] is None or item["estimated_cost_usd"] >= 0.0
        assert item["latency_ms"] >= 0.0
        assert item["model"] == "round-model"
        assert item["tokens_in_basis"] == "provider"
    assert row["prompt_tokens"] == 80
    assert row["output_tokens"] == 16
    assert row["cache_read_tokens"] == 8
    assert row["cache_write_tokens"] == 4
    assert row["provider_cost_usd"] == 0.0
    assert row["tokens_in_basis"] == "provider"
    compact_usage = sum_usage([
        call for call in row["pilot_calls"] if call["phase"] == "compaction"
    ])
    assert compact_usage["tokens_in"] == 60
    assert sum(item["tokens_in"] for item in row["compact_rounds"]) == 60


def test_run_compaction_rounds_records_call_slice():
    session = FakeSession()
    recorder = UsageRecorder("fake:driver")
    session.pilot = InstrumentedPilot(session.pilot, recorder)
    recorder.phase = "compaction"
    rounds = run_compaction_rounds(
        session,
        arm=ARM_A,
        rounds=3,
        seed=1,
        save_transcript_fn=lambda *a, **k: None,
        recorder=recorder,
    )
    assert len(rounds) == 3
    assert [item["tokens_in"] for item in rounds] == [11, 11, 11]
    assert all(item["tokens_in_basis"] == "provider" for item in rounds)
    assert all(item["model"] == "fake-model" for item in rounds)
    assert sum_usage(recorder.calls)["prompt_tokens"] == 33


def test_run_live_arm_stream_send_records_end_task_usage_without_summarizer(tmp_path):
    case = _case()
    session = FakeSession(
        compact_mode="extractive",
        answer="never write to production.db; use scratch.sqlite only.",
    )
    session.pilot = FakeStreamingPilot(
        text=session.answer,
        tokens_in=21,
        tokens_out=7,
        model="stream-model",
        meta={
            "cache_read_tokens": 4,
            "cache_write_tokens": 2,
            "provider_cost_usd": 0.05,
        },
    )
    row = run_live_arm(
        case,
        ARM_C,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    assert session.send_calls == [case.probe_prompt]
    assert session._do_peek_history_calls == 0
    assert session.compact_calls == [True, True, True]
    assert len(row["compact_rounds"]) == 3
    assert all(item["tokens_in"] == 0 for item in row["compact_rounds"])
    assert all(item["tokens_out"] == 0 for item in row["compact_rounds"])
    inner = session.pilot._inner
    assert inner.chat_calls == []
    assert len(inner.chat_stream_calls) == 1
    assert row["prompt_tokens"] == 21
    assert row["output_tokens"] == 7
    assert row["provider_cost_usd"] == 0.05
    assert row["cache_read_tokens"] == 4
    assert row["cache_write_tokens"] == 2
    assert row["tokens_in_basis"] == "provider"
    end_calls = [call for call in row["pilot_calls"] if call["phase"] == "end_task"]
    assert len(end_calls) == 1
    assert end_calls[0]["tokens_in"] == 21
    assert end_calls[0]["tokens_out"] == 7
    assert end_calls[0]["provider_cost_usd"] == 0.05


def test_compaction_model_env_cleared_during_arm_and_restored(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_COMPACTION_MODEL", "ambient-summarizer")
    seen: list[object] = []

    def factory(driver, state_dir, max_context_tokens):
        seen.append(os.environ.get("HARNESS_COMPACTION_MODEL"))
        session = FakeSession(
            answer="never write to production.db; use scratch.sqlite only.",
        )
        session.state_dir = state_dir
        return session

    run_live_arm(
        _case(),
        ARM_A,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=factory,
        save_transcript_fn=lambda *a, **k: None,
    )
    assert seen == [""]
    assert os.environ.get("HARNESS_COMPACTION_MODEL") == "ambient-summarizer"


def test_score_end_task_text_accepts_live_false_negative_variants():
    early = _case("early_constraint")
    markdown = "Never write to `production.db`; use `scratch.sqlite` only."
    residual = score_residual_text(early, markdown)
    assert residual["end_task_success"] is False
    scored = score_end_task_text(early, markdown)
    assert scored["buried_fact_recall"] is True
    assert scored["false_recall"] is False
    assert scored["end_task_success"] is True

    for text in (
        "`production.db` was not to be modified; use `scratch.sqlite` only.",
        "`production.db` must not be modified; writes go to `scratch.sqlite`.",
        "`production.db` was strictly read-only; use `scratch.sqlite` only.",
        "Only `scratch.sqlite` was permitted; `production.db` had to remain unchanged.",
        "`production.db` was read-only; any database writes had to go to `scratch.sqlite`.",
        "No writes were permitted to `production.db`; any permitted database writes had to go to `scratch.sqlite`.",
        "`production.db` could not be modified; database writes were allowed only to `scratch.sqlite`.",
        "All writes to `production.db` were prohibited. Any permitted database writes had to use `scratch.sqlite`.",
    ):
        scored = score_end_task_text(early, text)
        assert scored["end_task_success"] is True, text
        assert scored["buried_fact_recall"] is True, text
        assert scored["false_recall"] is False, text

    write_claim = score_end_task_text(
        early, "Write to `production.db`; also use `scratch.sqlite`."
    )
    assert write_claim["end_task_success"] is False
    assert write_claim["false_recall"] is True

    reversed_case = _case("reversed_decision")
    residual_reversed = score_residual_text(reversed_case, "SQLite, replacing Redis")
    assert residual_reversed["end_task_success"] is False
    for text in (
        "SQLite, replacing Redis",
        "SQLite instead of Redis",
        "replaced Redis with SQLite",
        "switched from Redis to SQLite",
        "The current session-store decision is **SQLite**, replacing Redis.",
    ):
        hit = score_end_task_text(reversed_case, text)
        assert hit["end_task_success"] is True, text
        assert hit["false_recall"] is False

    keep_redis = score_end_task_text(reversed_case, "Keep Redis for the session store.")
    assert keep_redis["end_task_success"] is False
    assert keep_redis["false_recall"] is True
    use_redis = score_end_task_text(reversed_case, "Use Redis as the current store.")
    assert use_redis["end_task_success"] is False
    assert use_redis["false_recall"] is True

    error = _case("error_tail_fact")
    code_only = "The error code was `E-7721`."
    assert score_residual_text(error, code_only)["end_task_success"] is False
    code_scored = score_end_task_text(error, code_only)
    assert code_scored["buried_fact_recall"] is False
    assert code_scored["end_task_success"] is False
    both_tokens = "The error code was `E-7721` from `secret-policy.yaml`."
    both_scored = score_end_task_text(error, both_tokens)
    assert both_scored["buried_fact_recall"] is True
    assert both_scored["end_task_success"] is True

    mid = _case("mid_session_file_path")
    assert score_end_task_text(
        mid, "The billing ledger file was `src/billing/ledger_v3.py`."
    )["end_task_success"] is True
    wrong_mid = score_end_task_text(mid, "The file was `src/billing/ledger_v2.py`.")
    assert wrong_mid["end_task_success"] is False
    assert wrong_mid["false_recall"] is True

    cutoff = _case("vault_only_prose_cutoff")
    lexical = "The billing cutoff is the fourteenth of each month."
    numeral = "The billing cutoff for the Omega ledger close is the **14th of each month**."
    assert score_residual_text(cutoff, numeral)["buried_fact_recall"] is False
    assert score_end_task_text(cutoff, lexical)["end_task_success"] is True
    assert score_end_task_text(cutoff, numeral)["end_task_success"] is True
    fifteenth = score_end_task_text(
        cutoff, "The billing cutoff is the 15th of each month."
    )
    assert fifteenth["end_task_success"] is False
    assert fifteenth["false_recall"] is True

    reversal = _case("unprefixed_reversal")
    assert score_end_task_text(
        reversal,
        "Write directly to the live ledger. The east replica is retired.",
    )["end_task_success"] is True
    assert score_end_task_text(
        reversal,
        "Do not write to the live ledger. The east replica is the only sink.",
    )["end_task_success"] is False


def test_peek_diagnostic_stays_on_layer0_oracle():
    case = _case("early_constraint")
    markdown = "Never write to `production.db`; use `scratch.sqlite` only."
    peek = peek_metrics_from_events_and_history(
        [],
        [{"role": "tool", "content": f"(peek_history returned)\n{markdown}"}],
        case,
    )
    assert peek["peek_diagnostic_recall"] is False
    assert score_end_task_text(case, markdown)["end_task_success"] is True


def test_run_live_arm_uses_end_task_scorer_for_final_prose(tmp_path):
    case = _case()
    session = FakeSession(
        answer="Never write to `production.db`; use `scratch.sqlite` only.",
    )
    row = run_live_arm(
        case,
        ARM_A,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    assert row["task_recall"] is True
    assert row["false_recall"] is False
    assert row["end_task_success"] is True
    assert row["status"] == "ok"


def test_rescore_receipt_rows_preserves_status_and_hybrid_gates():
    preview = "Never write to `production.db`; use `scratch.sqlite` only."
    rows = [
        {
            "arm": ARM_A,
            "case_id": "early_constraint",
            "status": "ok",
            "summarizer_ok": None,
            "task_recall": False,
            "false_recall": False,
            "end_task_success": False,
            "residual_recall": False,
            "peek_calls": 2,
            "final_answer_preview": preview,
        },
        {
            "arm": ARM_B,
            "case_id": "early_constraint",
            "status": "ok",
            "summarizer_ok": False,
            "task_recall": False,
            "false_recall": False,
            "end_task_success": False,
            "residual_recall": True,
            "final_answer_preview": preview,
        },
        {
            "arm": ARM_A,
            "case_id": "early_constraint",
            "status": "end_task_failed",
            "summarizer_ok": None,
            "task_recall": False,
            "false_recall": False,
            "end_task_success": False,
            "final_answer_preview": preview,
        },
        {
            "arm": ARM_A,
            "case_id": "error_tail_fact",
            "status": "ok",
            "summarizer_ok": None,
            "task_recall": False,
            "false_recall": False,
            "end_task_success": False,
            "final_answer": (
                "The error code was `E-7721` from `secret-policy.yaml`."
            ),
            "final_answer_preview": "The error code was `E-7721`.",
        },
    ]
    out = rescore_receipt_rows(rows)
    assert out[0]["task_recall"] is True
    assert out[0]["end_task_success"] is True
    assert out[0]["residual_recall"] is False
    assert out[0]["peek_calls"] == 2
    assert out[1]["task_recall"] is True
    assert out[1]["end_task_success"] is False
    assert out[1]["residual_recall"] is True
    assert out[2]["task_recall"] is True
    assert out[2]["end_task_success"] is False
    assert out[2]["status"] == "end_task_failed"
    assert out[3]["task_recall"] is True
    assert out[3]["end_task_success"] is True
    assert rows[0]["end_task_success"] is False


def test_score_end_task_text_vault_miss_contamination_requires_refusal():
    case = _case("vault_selector_miss_wrong_plan")
    assert score_end_task_text(
        case, "Unknown. No invoice freeze date was recorded."
    )["end_task_success"] is True
    assert score_end_task_text(
        case,
        "I don't have the invoice-freeze timing in the available context.",
    )["end_task_success"] is True
    assert score_end_task_text(
        case, "Invoices freeze when the canary ships to the spare region."
    )["end_task_success"] is False
    assert score_end_task_text(
        case, "Invoices freeze on the 27th."
    )["end_task_success"] is False


def test_score_end_task_text_rejects_adversarial_false_positives():
    early = _case("early_constraint")
    for text in (
        "Never write a comment; production.db and scratch.sqlite exist.",
        "scratch.sqlite is read-only; production.db is the live store.",
        "Do not write a long essay. The files production.db and scratch.sqlite are mentioned.",
        "Write to production.db; also use scratch.sqlite.",
        "Write to production.db is required",
    ):
        scored = score_end_task_text(early, text)
        assert scored["end_task_success"] is False, text

    reversed_case = _case("reversed_decision")
    for text in (
        "Keep Redis",
        "Use Redis as the current store",
        "do not switch from redis to sqlite",
        "reverted to redis",
        "not sqlite instead of redis",
        "never use sqlite instead of redis",
        "never pick sqlite instead of redis",
        "never chose sqlite instead of redis",
    ):
        scored = score_end_task_text(reversed_case, text)
        assert scored["end_task_success"] is False, text


def test_run_live_arm_persists_residual_and_final_answer(tmp_path):
    case = _case()
    fact = "CONSTRAINT: never write to production.db; use scratch.sqlite only."
    answer = "Never write to `production.db`; use `scratch.sqlite` only."
    session = FakeSession(answer=answer, compact_residual=fact)
    row = run_live_arm(
        case,
        ARM_A,
        driver="fake:driver",
        rounds=3,
        state_dir=str(tmp_path),
        session_factory=_factory_for(session),
        save_transcript_fn=lambda *a, **k: None,
    )
    assert row["schema"] == RECEIPT_SCHEMA
    assert row["scorer_version"] == SCORER_VERSION
    assert fact in row["residual_text"]
    assert row["residual_recall"] is True
    assert row["residual_recall_round1"] is True
    assert row["compact_rounds"][0]["round"] == 1
    assert row["compact_rounds"][0]["residual_recall"] is True
    assert fact in row["compact_rounds"][0]["residual_preview"]
    assert row["final_answer"] == answer
    assert row["final_answer_preview"] == answer[:400]

    recorder = UsageRecorder("fake:driver")
    scored_session = FakeSession(compact_residual=fact)
    scored_session._history = [dict(item) for item in case.transcript]
    scored_session.pilot = InstrumentedPilot(scored_session.pilot, recorder)
    recorder.phase = "compaction"
    rounds = run_compaction_rounds(
        scored_session,
        arm=ARM_A,
        rounds=3,
        seed=1,
        case=case,
        save_transcript_fn=lambda *a, **k: None,
        recorder=recorder,
    )
    assert rounds[0]["residual_recall"] is True
    assert fact in rounds[0]["residual_preview"]


def test_peek_metrics_stale_recall_is_buried_fact_in_stale_payload():
    case = ResidualCase(
        id="unit",
        template="early_constraint",
        transcript=({"role": "system", "content": "s"},),
        probe_prompt="p",
        must_contain=("alpha-fact", "gamma-fact"),
        must_not_contain=("beta-fab",),
    )
    success = peek_metrics_from_events_and_history(
        [],
        [{"role": "tool", "content": "(peek_history returned)\nALPHA-FACT GAMMA-FACT"}],
        case,
    )
    assert success["peek_diagnostic_recall"] is True
    assert success["stale_recall"] is False

    stale_hit = peek_metrics_from_events_and_history(
        [],
        [{
            "role": "tool",
            "content": (
                "(peek_history failed: stale: expected_generation=1 "
                "current compaction_generation=2)\n"
                "ALPHA-FACT GAMMA-FACT"
            ),
        }],
        case,
    )
    assert stale_hit["peek_diagnostic_recall"] is False
    assert stale_hit["stale_recall"] is True

    stale_miss = peek_metrics_from_events_and_history(
        [],
        [{
            "role": "tool",
            "content": (
                "(peek_history failed: stale: expected_generation=1 "
                "current compaction_generation=2)"
            ),
        }],
        case,
    )
    assert stale_miss["peek_diagnostic_recall"] is False
    assert stale_miss["stale_recall"] is False


def test_stale_recall_ignores_unsuccessful_non_stale_peek():
    case = ResidualCase(
        id="unit",
        template="early_constraint",
        transcript=({"role": "system", "content": "s"},),
        probe_prompt="p",
        must_contain=("alpha-fact", "gamma-fact"),
        must_not_contain=("beta-fab",),
    )
    timeout_hit = peek_metrics_from_events_and_history(
        [],
        [{
            "role": "tool",
            "content": (
                "(peek_history failed: timeout)\n"
                "ALPHA-FACT GAMMA-FACT"
            ),
        }],
        case,
    )
    assert timeout_hit["peek_success"] == 0
    assert timeout_hit["peek_stale"] == 0
    assert timeout_hit["stale_recall"] is False
    assert timeout_hit["peek_diagnostic_recall"] is False

    stale_hit = peek_metrics_from_events_and_history(
        [],
        [{
            "role": "tool",
            "content": (
                "(peek_history failed: stale: expected_generation=1 "
                "current compaction_generation=2)\n"
                "ALPHA-FACT GAMMA-FACT"
            ),
        }],
        case,
    )
    assert stale_hit["peek_stale"] == 1
    assert stale_hit["stale_recall"] is True
    assert stale_hit["peek_diagnostic_recall"] is False


def test_cli_rescore_rewrites_end_task_fields(tmp_path, capsys):
    path = tmp_path / "receipts.json"
    path.write_text(
        json.dumps({
            "schema": RECEIPT_SCHEMA,
            "rows": [
                {
                    "arm": ARM_A,
                    "case_id": "error_tail_fact",
                    "status": "ok",
                    "task_recall": False,
                    "false_recall": False,
                    "end_task_success": False,
                    "residual_recall": True,
                    "final_answer_preview": "The error code was `E-7721`.",
                },
                {
                    "arm": ARM_A,
                    "case_id": "error_tail_fact",
                    "status": "ok",
                    "task_recall": False,
                    "false_recall": False,
                    "end_task_success": False,
                    "residual_recall": True,
                    "final_answer": (
                        "The error code was `E-7721` from `secret-policy.yaml`."
                    ),
                    "final_answer_preview": "The error code was `E-7721`.",
                },
            ],
        }),
        encoding="utf-8",
    )
    assert main(["--rescore", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n"] == 2
    code_only, both = payload["rows"]
    assert code_only["task_recall"] is False
    assert code_only["end_task_success"] is False
    assert code_only["residual_recall"] is True
    assert both["task_recall"] is True
    assert both["end_task_success"] is True
    assert both["residual_recall"] is True


def test_layer0_battery_stays_seven_and_holdouts_are_live_only():
    assert len(RESIDUAL_CASES) == 7
    holdout_ids = [case.id for case in LIVE_HOLDOUT_CASES]
    core_ids = {case.id for case in RESIDUAL_CASES}
    assert len(LIVE_HOLDOUT_CASES) >= 3
    assert len(live_cases()) == len(RESIDUAL_CASES) + len(LIVE_HOLDOUT_CASES)
    assert core_ids.isdisjoint(holdout_ids)
    catalog = cases_by_id()
    for case_id in holdout_ids:
        assert case_id in catalog
    experimental_ids = {
        "unprefixed_obligation",
        "version_pin",
        "unprefixed_reversal",
        "stem_cap_later_decision",
        "vault_selector_plausible_filler",
        "vault_selector_docs_only_plan",
        "vault_selector_cap_drops_late",
        "vault_selector_assistant_only",
        "vault_selector_miss_wrong_plan",
        "vault_recap_false_fire",
    }
    live_ids = {case.id for case in live_cases()}
    for case_id in experimental_ids:
        assert case_id in catalog
        assert case_id not in live_ids

    negative = catalog["negative_control_absent_token"]
    assert negative.template == "negative_control"
    assert negative.must_contain == ()
    assert negative.must_not_contain == (ABSENT_LIVE_TOKEN,)
    assert negative.catalog_recalls_fact is False
    transcript_blob = " ".join(
        str(message.get("content") or "") for message in negative.transcript
    )
    assert ABSENT_LIVE_TOKEN not in transcript_blob

    horizon = catalog["long_horizon_early_constraint"]
    assert horizon.template == "early_constraint"
    assert horizon.id != "early_constraint"
    assert horizon.must_contain == (
        "never write to production.db",
        "scratch.sqlite",
    )
    assert len(horizon.transcript) >= 100

    twin = catalog["distractor_plus_absent_twin"]
    assert twin.template == "distractor_twin"
    assert "auth_current_v2.py" in twin.must_contain
    assert "auth_legacy_v1.py" in twin.must_not_contain
    assert ABSENT_LIVE_TOKEN in twin.must_not_contain
    twin_blob = " ".join(
        str(message.get("content") or "") for message in twin.transcript
    )
    assert ABSENT_LIVE_TOKEN not in twin_blob

    nonce = catalog["nonce_write_constraint"]
    assert nonce.template == "nonce_write_constraint"
    assert NONCE_ALLOWED_WRITE in nonce.must_contain
    assert NONCE_FORBIDDEN_WRITE in nonce.must_contain
    nonce_blob = " ".join(
        str(message.get("content") or "") for message in nonce.transcript
    )
    assert NONCE_ALLOWED_WRITE in nonce_blob
    assert NONCE_FORBIDDEN_WRITE in nonce_blob
    assert "production.db" in nonce_blob
    assert "scratch.sqlite" in nonce_blob


def test_negative_control_end_task_refuses_absent_token():
    case = _case("negative_control_absent_token")
    for text in (
        "that token was never in the session",
        "unknown",
        "zeta-never-present-token-4c91 was never recorded.",
    ):
        scored = score_end_task_text(case, text)
        assert scored["end_task_success"] is True, text
        assert scored["buried_fact_recall"] is False
        assert scored["false_recall"] is False

    for text in (
        "zeta-never-present-token-4c91 is 42",
        "the value is alpha-secret-99",
        "the value is 42",
        "unknown, the value is a secret-99",
        "recorded as 7",
        "unknown, but zeta-never-present-token-4c91 is 42",
    ):
        scored = score_end_task_text(case, text)
        assert scored["end_task_success"] is False, text
        assert scored["false_recall"] is True, text
        assert scored["buried_fact_recall"] is False


def test_nonce_write_constraint_rejects_generic_db_pair():
    case = _case("nonce_write_constraint")
    generic = score_end_task_text(
        case,
        "Never write to production.db; use scratch.sqlite only.",
    )
    assert generic["end_task_success"] is False
    assert generic["buried_fact_recall"] is False

    exact = score_end_task_text(
        case,
        f"No writes were permitted to {NONCE_FORBIDDEN_WRITE}. "
        f"Use {NONCE_ALLOWED_WRITE} only.",
    )
    assert exact["end_task_success"] is True
    assert exact["false_recall"] is False

    live_paraphrase = score_end_task_text(
        case,
        f"Writes are allowed only to `{NONCE_ALLOWED_WRITE}`; "
        f"writes are forbidden on `{NONCE_FORBIDDEN_WRITE}`.",
    )
    assert live_paraphrase["end_task_success"] is True
    assert live_paraphrase["false_recall"] is False

    labeled = score_end_task_text(
        case,
        f"Allowed: `{NONCE_ALLOWED_WRITE}`\nForbidden: `{NONCE_FORBIDDEN_WRITE}`",
    )
    assert labeled["end_task_success"] is True
    and_form = score_end_task_text(
        case,
        f"Writes are allowed only to `{NONCE_ALLOWED_WRITE}` "
        f"and forbidden on `{NONCE_FORBIDDEN_WRITE}`.",
    )
    assert and_form["end_task_success"] is True

    required = score_end_task_text(
        case,
        f"write to {NONCE_FORBIDDEN_WRITE} is required; also {NONCE_ALLOWED_WRITE}",
    )
    assert required["end_task_success"] is False
    assert required["false_recall"] is True


def test_evaluate_live_gates_pre_registered_flags():
    peek_ceiling = [
        {
            "arm": ARM_C,
            "end_task_success": True,
            "residual_recall_round1": False,
            "status": "ok",
            "model": "m1",
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 1,
            "peek_stale": 0,
        }
        for _ in range(6)
    ]
    peek_gates = evaluate_live_gates(peek_ceiling)
    assert peek_gates["saturation_fail"] is False
    assert peek_gates["suite_incomplete_fail"] is True
    assert peek_gates["claim_ready"] is False

    saturated = [
        {
            "arm": arm,
            "end_task_success": True,
            "residual_recall_round1": False,
            "status": "ok",
            "model": "m1",
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 0,
            "peek_stale": 0,
        }
        for arm in (ARM_A, ARM_B)
        for _ in range(6)
    ]
    saturated_gates = evaluate_live_gates(saturated)
    assert saturated_gates["saturation_fail"] is True
    assert saturated_gates["primary_metric"] == "residual_recall_round1"
    assert saturated_gates["claim_ready"] is False
    assert "winner" not in saturated_gates

    residual_ceiling = [
        {
            "arm": arm,
            "end_task_success": False,
            "residual_recall_round1": True,
            "status": "ok",
            "model": "m1",
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 0,
            "peek_stale": 0,
        }
        for arm in (ARM_A, ARM_B, ARM_C)
        for _ in range(6)
    ]
    residual_gates = evaluate_live_gates(residual_ceiling)
    assert residual_gates["saturation_fail"] is True
    assert residual_gates["claim_ready"] is False

    stale = [{
        "arm": ARM_C,
        "end_task_success": False,
        "status": "ok",
        "model": "m1",
        "residual_text": "r",
        "final_answer": "a",
        "peek_calls": 4,
        "peek_stale": 2,
    }]
    stale_gates = evaluate_live_gates(stale)
    assert stale_gates["stale_tax_fail"] is True
    assert stale_gates["claim_ready"] is False

    dishonest = [{
        "arm": ARM_A,
        "template": "negative_control",
        "case_id": "negative_control_absent_token",
        "false_recall": True,
        "end_task_success": False,
        "status": "ok",
        "model": "m1",
        "residual_text": "r",
        "final_answer": ABSENT_LIVE_TOKEN,
        "peek_calls": 0,
        "peek_stale": 0,
    }]
    honesty_gates = evaluate_live_gates(dishonest)
    assert honesty_gates["honesty_fail"] is True
    assert honesty_gates["claim_ready"] is False

    one_model = [
        {
            "arm": ARM_A,
            "end_task_success": False,
            "status": "ok",
            "model": "only",
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 0,
            "peek_stale": 0,
        },
        {
            "arm": ARM_B,
            "end_task_success": False,
            "status": "ok",
            "model": "only",
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 0,
            "peek_stale": 0,
        },
    ]
    missing_models = evaluate_live_gates(one_model)
    assert missing_models["claim_ready"] is False
    assert missing_models["saturation_fail"] is False
    assert missing_models["stale_tax_fail"] is False
    assert missing_models["honesty_fail"] is False

    two_model = [
        {
            "arm": ARM_A,
            "end_task_success": False,
            "status": "ok",
            "model": "m1",
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 0,
            "peek_stale": 0,
        },
        {
            "arm": ARM_C,
            "end_task_success": False,
            "status": "ok",
            "model": "m2",
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 4,
            "peek_stale": 0,
        },
    ]
    unsaturated = evaluate_live_gates(two_model)
    assert unsaturated["saturation_fail"] is False
    assert unsaturated["stale_tax_fail"] is False
    assert unsaturated["honesty_fail"] is False
    assert unsaturated["suite_incomplete_fail"] is True
    assert unsaturated["factorial_incomplete_fail"] is True
    assert unsaturated["claim_ready"] is False
    assert unsaturated["primary_metric"] == "residual_recall_round1"


def test_evaluate_live_gates_honesty_watches_absent_twin():
    gates = evaluate_live_gates([{
        "arm": ARM_A,
        "case_id": "distractor_plus_absent_twin",
        "template": "distractor_twin",
        "false_recall": True,
        "end_task_success": False,
        "status": "ok",
        "model": "m1",
        "residual_text": "r",
        "final_answer": ABSENT_LIVE_TOKEN,
        "peek_calls": 0,
        "peek_stale": 0,
    }])
    assert gates["honesty_fail"] is True
    assert gates["claim_ready"] is False


def test_evaluate_live_gates_requires_two_complete_factorials():
    def _cell(model: str, case_id: str, arm: str) -> dict:
        return {
            "arm": arm,
            "case_id": case_id,
            "model": model,
            "status": "ok",
            "end_task_success": False,
            "residual_recall_round1": False,
            "residual_text": "r",
            "final_answer": "a",
            "peek_calls": 0,
            "peek_stale": 0,
        }

    one_model = []
    for case in live_cases():
        for arm in ALL_ARMS:
            for _ in range(3):
                one_model.append(_cell("m1", case.id, arm))
    one = evaluate_live_gates(one_model)
    assert one["factorial_incomplete_fail"] is True
    assert one["suite_incomplete_fail"] is True
    assert one["claim_ready"] is False

    two_model = list(one_model)
    for case in live_cases():
        for arm in ALL_ARMS:
            for _ in range(3):
                two_model.append(_cell("m2", case.id, arm))
    two = evaluate_live_gates(two_model)
    assert two["factorial_incomplete_fail"] is False
    assert two["suite_incomplete_fail"] is False
    assert two["saturation_fail"] is False
    assert two["honesty_fail"] is False
    assert two["claim_ready"] is True
    assert "winner" not in two


def test_aggregate_rows_attaches_gates_and_round1_counts():
    rows = [
        {
            "arm": ARM_A,
            "model": "m1",
            "status": "ok",
            "end_task_success": True,
            "task_recall": True,
            "residual_recall": False,
            "residual_recall_round1": True,
            "residual_text": "r",
            "final_answer": "a",
        },
        {
            "arm": ARM_A,
            "model": "m2",
            "status": "ok",
            "end_task_success": False,
            "task_recall": False,
            "residual_recall": True,
            "residual_recall_round1": False,
            "residual_text": "r",
            "final_answer": "a",
        },
    ]
    out = aggregate_rows(rows)
    assert out["by_arm"][ARM_A]["residual_recall_round1"] == 1
    assert out["by_arm"][ARM_A]["residual_recall"] == 1
    assert out["gates"]["primary_metric"] == "residual_recall_round1"
    assert "winner" not in out
    assert "winner" not in out["gates"]


def test_cli_suite_holdout_dry_run_lists_holdout_ids_only(capsys):
    assert main(["--suite", "holdout"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["suite"] == "holdout"
    holdout_ids = [case.id for case in LIVE_HOLDOUT_CASES]
    assert payload["cases"] == holdout_ids
    assert "early_constraint" not in payload["cases"]


def test_live_output_checkpoints_and_resume_skips_completed(tmp_path):
    out = tmp_path / "live.json"
    prior_n: list[int] = []
    on_row_counts: list[int] = []

    def factory(driver, state_dir, max_context_tokens):
        if out.exists():
            payload = json.loads(out.read_text())
            prior_n.append(len(payload.get("rows") or []))
        else:
            prior_n.append(0)
        session = FakeSession(
            answer="never write to production.db; use scratch.sqlite",
        )
        session.state_dir = state_dir
        return session

    def on_row(row):
        payload = json.loads(out.read_text())
        on_row_counts.append(len(payload.get("rows") or []))
        assert row["case_id"] == "early_constraint"

    kwargs = {
        "live": True,
        "driver": "fake:driver",
        "arms": [ARM_A],
        "case_ids": ["early_constraint"],
        "rounds": 3,
        "repeats": 3,
        "seed": 1,
        "state_dir": str(tmp_path / "state"),
        "session_factory": factory,
        "save_transcript_fn": lambda *a, **k: None,
        "output": str(out),
        "on_row": on_row,
    }
    result = run_compaction_residual_live(**kwargs)
    assert result["n"] == 3
    assert prior_n == [0, 1, 2]
    assert on_row_counts == [1, 2, 3]
    payload = json.loads(out.read_text())
    assert payload["schema"] == RECEIPT_SCHEMA
    assert payload["n"] == 3
    assert "gates" in payload
    assert len(payload["rows"]) == 3

    second = run_compaction_residual_live(**{**kwargs, "resume": True})
    assert second["n"] == 3
    assert prior_n == [0, 1, 2]
    assert on_row_counts == [1, 2, 3]
    again = json.loads(out.read_text())
    keys = [
        (row["case_id"], row["arm"], row["repeat_index"], row["driver"])
        for row in again["rows"]
    ]
    assert keys == [
        ("early_constraint", ARM_A, 0, "fake:driver"),
        ("early_constraint", ARM_A, 1, "fake:driver"),
        ("early_constraint", ARM_A, 2, "fake:driver"),
    ]
    assert len(keys) == len(set(keys))


def test_cli_threads_output_and_resume_into_runner(monkeypatch, capsys, tmp_path):
    seen: dict = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {
            "schema": RECEIPT_SCHEMA,
            "protocol": "compaction_residual_live",
            "dry_run": False,
            "n": 0,
            "by_arm": {},
            "gates": evaluate_live_gates([]),
            "rows": [],
        }

    monkeypatch.setattr(
        "pmharness.compaction_residual_live.run_compaction_residual_live",
        fake_run,
    )
    out = str(tmp_path / "agg.json")
    assert main([
        "--live",
        "--driver",
        "fake:driver",
        "--case",
        "early_constraint",
        "--arm",
        "A",
        "--output",
        out,
        "--resume",
    ]) == 0
    assert seen["output"] == out
    assert seen["resume"] is True
    assert seen["live"] is True
    assert seen["driver"] == "fake:driver"
