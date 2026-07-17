from __future__ import annotations

"""Send-loop phase helpers peeled from SendLoopMixin._send_locked_inner.

These were nested closures / inline orchestration blocks inside the turn
kernel — hard to unit-test in isolation. They are mechanical extractions:
same queue/thread contracts, same exception surfaces, same ConvEvent shapes.
Explicit ``session`` / queue / schema args replace closure capture.

Public orchestration stays on SendLoopMixin.send / _send_locked /
_send_locked_inner; this module owns background-thread targets, prefetch
workers, stream-queue drain, per-step usage metering, and small pure helpers
the kernel calls.
"""

import inspect
import re
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Iterator, Optional

from pmharness.bridge import execute_intent

from .pilot import PilotAction, StreamingSayExtractor

# job_XXXXXXXXXXXX — same pattern the parallel-dispatch stdout scanner used
# when nested inside _send_locked_inner.
_JOB_ID_RE = re.compile(r"\b(job_[a-fA-F0-9]{12})\b")

# ActionKind string set used by the execute-loop prefetch planner — membership
# stays typed against the pilot contract without living inside the god method.
READ_ONLY_KINDS: frozenset[str] = frozenset({
    "read_file", "list_dir", "search_codegraph", "search_files",
    "web_search", "web_fetch", "read_pdf", "view_image", "lsp",
})


def run_stream(
    session: Any,
    q: Any,
    tools_schema: Any,
    sys_prompt: str,
) -> None:
    """Background target: pilot.chat_stream → queue (delta/reasoning/tool_hint/wait/done/error)."""
    try:
        kwargs = {
            "tools": tools_schema,
            "system": sys_prompt,
            "on_delta": lambda delta: q.put(("delta", delta)),
            "on_reasoning_delta": lambda delta: q.put(("reasoning", delta)),
            "on_tool_hint": lambda name: q.put(("tool_hint", name)),
        }
        try:
            if "on_wait_notice" in inspect.signature(
                session.pilot.chat_stream
            ).parameters:
                kwargs["on_wait_notice"] = (
                    lambda msg: q.put(("wait", msg))
                )
        except Exception:
            pass
        r = session.pilot.chat_stream(
            session._elide_stale_reads(session._history[1:]),
            **kwargs,
        )
        q.put(("done", r))
    except Exception as ex:
        q.put(("error", ex))


def run_prefetch(
    session: Any,
    idx_and_act: tuple[int, PilotAction],
) -> tuple[int, Any]:
    """ThreadPool map worker for read-only parallel prefetch before action dispatch."""
    idx, act = idx_and_act
    kind = act.kind
    try:
        if kind == "read_file":
            return idx, session._do_read_file(act)
        elif kind == "list_dir":
            return idx, session._do_list_dir(act)
        elif kind == "search_codegraph":
            return idx, session._do_search_codegraph(act)
        elif kind == "search_files":
            return idx, session._do_search_files(act)
        elif kind == "web_search":
            return idx, session._do_web_search(act)
        elif kind == "web_fetch":
            return idx, session._do_web_fetch(act)
        elif kind == "read_pdf":
            return idx, session._do_read_pdf(act)
        elif kind == "view_image":
            return idx, session._do_view_image(act)
    except Exception as exc:
        return idx, (False, "exception", str(exc))
    return idx, (False, "exception", f"Unknown prefetch kind {kind}")


def run_parallel_prefetch(
    session: Any,
    prefetch_targets: list[tuple[int, PilotAction]],
) -> dict[int, Any]:
    """Run read-only prefetch in a thread pool when ≥2 targets are pending.

    Returns an empty dict when fewer than two targets are given (caller executes
    those serially at dispatch time) — same threshold the kernel used inline.
    """
    if len(prefetch_targets) < 2:
        return {}
    prefetch: dict[int, Any] = {}
    max_workers = min(8, len(prefetch_targets))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(
            partial(run_prefetch, session), prefetch_targets
        )
        for idx, res in results:
            prefetch[idx] = res
    return prefetch


def stream_swarm(
    session: Any,
    intent: Any,
    delta_q: Any,
) -> None:
    """Background target: execute_intent with on_delta → delta_q (delta/done/error)."""
    try:
        r = execute_intent(
            intent,
            state_dir=session.state_dir,
            session_id=session.harness_session_id or "",
            cwd=session.config.repo or None,
            on_delta=lambda wid, kind, text: delta_q.put(
                ("delta", (wid, kind, text))
            ),
        )
        delta_q.put(("done", r))
    except Exception as ex:  # noqa: BLE001 - surfaced by caller
        delta_q.put(("error", ex))


def read_stdout_thread(p_info: dict) -> None:
    """Drain a subprocess stdout pipe; capture job_id when it appears."""
    try:
        for line in p_info["proc"].stdout:
            p_info["lines"].append(line)
            if not p_info["job_id"]:
                m = _JOB_ID_RE.search(line)
                if m:
                    p_info["job_id"] = m.group(1)
    except Exception:
        pass


def action_display_goal(act: PilotAction) -> Any:
    """Resolve the UI/transcript goal label for an action — pure, no side effects."""
    act_goal = act.goal
    if act.kind == "relocate_session":
        _rs = act.arguments or {}
        act_goal = (
            (act.path or "").strip()
            or (act.repo or "").strip()
            or (_rs.get("workspace_root") or _rs.get("path") or _rs.get("repo") or "")
            or "(workspace root)"
        )
    elif act.kind in (
        "read_file", "write_file", "edit_file", "hash_edit",
        "list_dir", "view_image", "open_project",
    ):
        act_goal = act.path or "(workspace root)"
    elif act.kind == "run_command":
        act_goal = act.command
    elif act.kind == "lsp":
        _a = act.arguments or {}
        act_goal = _a.get("mode") or "lsp"
    elif act.kind == "call_mcp":
        act_goal = act.tool
    elif act.kind == "manage_mcp":
        _m = act.arguments or {}
        act_goal = f"{_m.get('action') or 'list'} {_m.get('name') or ''}".strip()
    elif act.kind == "web_search":
        act_goal = act.query
    elif act.kind == "web_fetch":
        act_goal = act.url
    elif act.kind == "read_pdf":
        act_goal = act.path or act.url
    elif act.kind == "search_codegraph":
        act_goal = act.query
    elif act.kind == "search_files":
        act_goal = act.query
    elif act.kind == "search_state":
        act_goal = act.query
    elif act.kind == "session_bank":
        act_goal = (act.arguments or {}).get("session_id") or act.query or "list"
    elif act.kind == "search_tools":
        act_goal = act.query or ",".join(act.arguments.get("activate") or [])
    elif act.kind == "query_wiki":
        act_goal = act.arguments.get("question") or ""
    elif act.kind.startswith("browser_"):
        _b = act.arguments or {}
        act_goal = _b.get("url") or _b.get("ref") or _b.get("direction") or act.kind
    return act_goal


def drain_stream_queue(q: Any) -> Iterator[Any]:
    """Consume a ``run_stream`` queue and yield ConvEvents until done/error.

    On success, generator return value is ``(streamed_prose, resp)``. On
    transport failure the queued exception is re-raised (same as the former
    inline loop). Lazy-imports ConvEvent to avoid an import cycle with
    conversation → send_loop → send_loop_phases.
    """
    from .conversation import ConvEvent

    # The model streams a raw JSON envelope ({"say": "...", "actions": [...]}).
    # Extract just the human-facing `say` prose incrementally so it renders
    # token-by-token — instead of streaming ugly JSON then dumping the parsed
    # prose all at once. streamed_prose tracks what we showed so the final
    # `message` can skip re-emitting it. Reasoning + tool-name hints paint
    # live so a long GLM/OR "thinking" wait is not a blank spinner.
    say_extractor = StreamingSayExtractor()
    streamed_prose: list[str] = []
    while True:
        kind, val = q.get()
        if kind == "delta":
            clean = say_extractor.feed(val)
            if clean:
                streamed_prose.append(clean)
                yield ConvEvent("message_delta", {"text": clean})
        elif kind == "reasoning":
            if val:
                yield ConvEvent("thinking", {"text": val, "delta": True})
        elif kind == "tool_hint":
            # Drivers may pass a plain name or a structured
            # {name, goal, id, status} payload (Cursor ACP / stream-json).
            # Bare "tool" used to paint "Investigating · tool tool" in the fold.
            if isinstance(val, dict):
                name = str(val.get("name") or "").strip()
                if name or val.get("id"):
                    data = {
                        "name": name or "tool_call",
                    }
                    goal = val.get("goal")
                    if goal:
                        data["goal"] = str(goal)
                    call_id = val.get("id")
                    if call_id:
                        data["id"] = str(call_id)
                    status = val.get("status")
                    if status:
                        data["status"] = str(status)
                    yield ConvEvent("tool_prep", data)
            elif val:
                yield ConvEvent("tool_prep", {"name": str(val)})
        elif kind == "wait":
            if val:
                # Hermes-style live status for long Codex incomplete
                # continuations / reconnects.
                yield ConvEvent("notice", {
                    "message": str(val),
                    "kind": "wait",
                })
        elif kind == "done":
            return "".join(streamed_prose), val
        elif kind == "error":
            raise val


def meter_pilot_step(
    session: Any,
    resp: Any,
    prompt: str,
) -> None:
    """Apply per-step token / cache / cost meters after a pilot transport call.

    Mechanical lift of the post-stream accounting block from
    ``_send_locked_inner`` — same counters, same provider-billed preference,
    same ``_session_cost`` fallback. Mutates ``session`` in place.
    """
    # real token metering: prompt + completion (drivers report tokens_out;
    # estimate tokens_in from prompt length when not provided).
    _t_out = int(getattr(resp, "tokens_out", 0) or 0)
    _t_in = int(getattr(resp, "tokens_in", 0) or len(prompt) // 4)
    session._tokens_used += _t_out + _t_in
    session._tokens_out += _t_out
    session._turn_output_tokens += _t_out
    session._tokens_in += _t_in
    # Remember this turn's REAL prompt size so the live context
    # estimate (compaction trigger + composer % meter) can prefer
    # the driver's actual number over the chars//4 heuristic.
    if _t_in > 0:
        session._last_prompt_tokens = _t_in
    # Cache read/write credit: drivers report prompt-prefix cache
    # hits (and Anthropic/Bedrock writes) in meta. Reads save; writes
    # cost a premium -- both feed the same _session_cost formula.
    try:
        _meta = getattr(resp, "meta", None) or {}
        _cache_delta = int(_meta.get("cache_read_tokens", 0) or 0)
        _write_delta = int(_meta.get("cache_write_tokens", 0) or 0)
        _write_5m = int(_meta.get("cache_write_5m_tokens", 0) or 0)
        _write_1h = int(_meta.get("cache_write_1h_tokens", 0) or 0)
        session._tokens_cached += _cache_delta
        session._tokens_cache_write += _write_delta
        session._tokens_cache_write_5m += _write_5m
        session._tokens_cache_write_1h += _write_1h
    except Exception:
        _meta = {}
        _cache_delta = 0
        _write_delta = 0
        _write_5m = 0
        _write_1h = 0
    if str(_meta.get("billing") or "").lower() == "plan":
        session._plan_billing = True
    try:
        from pmharness.registry import resolve_price_with_source
        _price_in, _price_out, _price_src = resolve_price_with_source(
            session.config.driver
        )
        session._price_source = str(_price_src or "")
    except Exception:
        try:
            from pmharness.registry import resolve_price
            _price_in, _price_out = resolve_price(session.config.driver)
        except Exception:
            _price_in, _price_out = 0.0, 0.0
        _price_src = "default"
        session._price_source = _price_src
    # Prefer provider-billed USD (OpenRouter usage.cost) when the
    # driver surfaced it. Otherwise price this step with the same
    # cache-aware formula /api/usage uses -- never full-price the
    # cached slice, and bill writes at the published premium.
    _provider_step = _meta.get("provider_cost_usd")
    _pilot_cost: Optional[float] = None
    if _provider_step is not None:
        try:
            _cand = float(_provider_step)
            if _cand == _cand and _cand >= 0.0:
                _pilot_cost = _cand
                session._provider_cost_usd += _cand
                session._provider_billed_tokens_in += _t_in
                session._provider_billed_tokens_out += _t_out
                session._provider_billed_tokens_cached += _cache_delta
                session._provider_billed_tokens_cache_write += _write_delta
                session._provider_billed_tokens_cache_write_5m += _write_5m
                session._provider_billed_tokens_cache_write_1h += _write_1h
        except (TypeError, ValueError):
            _pilot_cost = None
    if _pilot_cost is None:
        try:
            from harness.server import _session_cost
            _pilot_cost = float(
                _session_cost(
                    _t_in, _t_out, _cache_delta, _price_in, _price_out,
                    cache_write=_write_delta,
                    cache_write_5m=_write_5m,
                    cache_write_1h=_write_1h,
                )
            )
        except Exception:
            _pilot_cost = (
                (_t_in * float(_price_in) + _t_out * float(_price_out))
                / 1_000_000.0
            )
    session._accumulate_session_meters(
        input_tokens=_t_in,
        output_tokens=_t_out,
        cache_read_tokens=_cache_delta,
        estimated_cost_usd=_pilot_cost,
    )
