from __future__ import annotations

"""Send-loop phase helpers peeled from SendLoopMixin._send_locked_inner.

These were nested closures / inline orchestration blocks inside the turn
kernel — hard to unit-test in isolation. They are mechanical extractions:
same queue/thread contracts, same exception surfaces, same ConvEvent shapes.
Explicit ``session`` / queue / schema args replace closure capture.

Public orchestration stays on SendLoopMixin.send / _send_locked /
_send_locked_inner; this module owns background-thread targets, prefetch
workers, stream-queue drain, per-step usage metering, idle steer/queue
finalization, read-only tool-result assembly, local tool-result assembly,
auto-verify, and small pure helpers the kernel calls. The per-step action
spree lives in ``send_loop_actions``; swarm / implement / parallel /
route_task / memory dispatch lives in ``send_loop_dispatch``.
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

LOCAL_ACTION_KINDS: frozenset[str] = frozenset({
    "open_project", "relocate_session", "session_bank",
    "write_file", "edit_file", "hash_edit", "run_command",
    "search_tools", "search_state",
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_get_text", "browser_screenshot",
    "query_wiki", "call_mcp", "manage_mcp",
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
        # Sanitize immediately before dispatch (same seam as sync chat).
        r = session.pilot.chat_stream(
            session._messages_for_provider(),
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
        from .repo_resolve import resolve_effective_repo
        _raw_repo = (session.config.repo or "").strip()
        _cwd = resolve_effective_repo(_raw_repo) if _raw_repo else None
        r = execute_intent(
            intent,
            state_dir=session.state_dir,
            session_id=session.harness_session_id or "",
            cwd=_cwd,
            repo=_cwd,
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


def promote_trailing_reasoning_to_say(
    *,
    say_text: str,
    streamed_reasoning: str = "",
    stream_ended_on_reasoning: bool = False,
    meta_reasoning: str = "",
) -> str:
    """Promote thought-channel readout into assistant prose when needed.

    Cursor CLI/ACP (notably Grok) often leaves the final summary only in
    ``agent_thought_chunk`` / thinking events after tools. Live UI paints
    that as REASONING; without a follow-up ``message`` the turn feels
    unfinished. Promote when:

    - say is empty and we have accumulated reasoning, or
    - the stream ended on reasoning and that text is substantially longer
      than a short pre-tool say (typical "Found X. Looking up…" preamble).

    Returns the text to emit as an extra/final assistant message, or "".
    Never invents content — only reuses what the driver already streamed.
    """
    reasoning = (streamed_reasoning or meta_reasoning or "").strip()
    if not reasoning:
        return ""
    say = (say_text or "").strip()
    if not say:
        return reasoning
    if not stream_ended_on_reasoning:
        return ""
    if reasoning == say:
        return ""
    # Short pre-tool narration + long post-tool thought readout.
    if len(reasoning) < max(120, len(say) * 2):
        return ""
    # Say already embeds most of the reasoning (driver duplicated channels).
    if say in reasoning and len(say) >= int(len(reasoning) * 0.6):
        return ""
    if reasoning in say:
        return ""
    return reasoning


def drain_stream_queue(q: Any) -> Iterator[Any]:
    """Consume a ``run_stream`` queue and yield ConvEvents until done/error.

    On success, generator return value is ``(streamed_prose, resp)``. On
    transport failure the queued exception is re-raised (same as the former
    inline loop). Lazy-imports ConvEvent to avoid an import cycle with
    conversation → send_loop → send_loop_phases.

    Accumulated reasoning is stashed on ``resp.meta`` (when present) as
    ``streamed_reasoning`` / ``stream_ended_on_reasoning`` so the send loop
    can promote a thought-only finale into an assistant message.
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
    streamed_reasoning: list[str] = []
    last_content_kind = ""  # "prose" | "reasoning"
    while True:
        kind, val = q.get()
        if kind == "delta":
            clean = say_extractor.feed(val)
            if clean:
                streamed_prose.append(clean)
                last_content_kind = "prose"
                yield ConvEvent("message_delta", {"text": clean})
        elif kind == "reasoning":
            if val:
                streamed_reasoning.append(str(val))
                last_content_kind = "reasoning"
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
            reasoning = "".join(streamed_reasoning)
            if reasoning:
                meta = getattr(val, "meta", None)
                if not isinstance(meta, dict):
                    meta = {}
                    try:
                        val.meta = meta
                    except Exception:
                        meta = None
                if isinstance(meta, dict):
                    meta["streamed_reasoning"] = reasoning
                    meta["stream_ended_on_reasoning"] = (
                        last_content_kind == "reasoning"
                    )
                    # Fill meta.reasoning when the driver omitted it (Cursor ACP/CLI).
                    if not str(meta.get("reasoning") or "").strip():
                        meta["reasoning"] = reasoning
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


def drain_idle_turn(
    session: Any,
    *,
    user_message: str,
    step: int,
    swarms: Any,
    turn_prose: list,
    turn_findings: list,
) -> Iterator[Any]:
    """No-actions path: deliver pending steers / queued prompts, or finalize.

    Generator return value is ``(disposition, user_message)`` where disposition
    is ``"continue"`` (re-enter the step loop), ``"break"`` (stop for a
    driver swap), or ``"return"`` (turn closed with ``assistant_done``).
    Same ConvEvent shapes and history mutations as the former inline block.
    """
    from .conversation import ConvEvent

    # S2 Stop↔steer boundary: never promote queued steers into a new user
    # message after cooperative interrupt — drop + notice instead.
    blocks = getattr(session, "_steer_boundary_blocks_inject", None)
    if callable(blocks) and blocks():
        drop = getattr(session, "drop_queued_steers", None)
        dropped = drop() if callable(drop) else []
        if dropped:
            record = getattr(session, "_record_steer_drop_notice", None)
            if callable(record):
                record(dropped)
        flush = getattr(session, "_flush_steer_drop_notice", None)
        if callable(flush):
            yield from flush()
        return ("return", user_message)

    pending_steers = session.drain_steer()
    if pending_steers:
        for steer in pending_steers:
            yield ConvEvent("steer", {"text": steer})
            session._history.append({"role": "user", "content": session._steer_marker(steer)})
        session._steer_pending = False
        return ("continue", user_message)
    # Steer took priority above; only if no steer was pending do we
    # look at the PROMPT QUEUE ("playlist"). A queued prompt runs as
    # a genuine next-turn user message -- NOT wrapped in the OUT-OF-
    # BAND marker used for steer -- so it flows through the pilot as
    # a normal fresh turn. The `continue` re-enters the same step
    # loop, which is bounded by the existing HARD_PILOT_STEPS /
    # max_steps cap; the queue cannot make the loop unbounded.
    # If the head item was stamped for a different pilot model
    # (Hermes-style mid-turn picker change), stop this turn instead
    # of draining it under the wrong driver -- idle drain + deferred
    # swap will pick it up next.
    if session._next_queued_needs_driver_swap():
        return ("break", user_message)
    queued = session._pop_next_prompt()
    if queued and queued.get("text"):
        q_text = queued.get("text", "")
        q_images = [p for p in (queued.get("images") or []) if p]
        yield ConvEvent("queued_prompt", {"id": queued.get("id", ""), "text": q_text, "images": list(q_images)})
        # A queued prompt is a genuine fresh user turn, so it carries
        # its image attachments the same way a normal turn does
        # (_send_locked_inner). The step loop already holds a valid
        # assistant history tail, so we deliver the images as vision
        # transcription appended to the user content -- identical to
        # the normal-turn plumbing above.
        content = q_text
        if q_images:
            try:
                from .vision import transcribe_images
                yield ConvEvent("vision", {"count": len(q_images), "status": "transcribing"})
                results = transcribe_images(q_images)
                blocks = []
                for path, r in zip(q_images, results):
                    if getattr(r, "error", None):
                        yield ConvEvent("vision", {"path": path, "error": r.error})
                    elif getattr(r, "text", ""):
                        blocks.append(f"[Image: {path}]\n{r.text}")
                        yield ConvEvent("vision", {"path": path,
                            "chars": len(r.text), "model": r.model,
                            "preview": r.text[:200]})
                if blocks:
                    content = ("The user attached image(s). Transcription(s) below "
                               "(you cannot see the image, only this text):\n\n"
                               + "\n\n".join(blocks) + "\n\n---\n" + q_text)
            except Exception:
                pass
        session._history.append({"role": "user", "content": content})
        # Refresh the "current user message" reference so downstream
        # per-turn hooks (compaction, ingest, budget) attribute work
        # to the newly-running queued prompt instead of the previous
        # completed one.
        return ("continue", q_text)
    # assistant_done first; ingest in housekeeping so the busy lock
    # releases immediately. Sync wiki I/O here used to leave the
    # final answer painted while Stop/Still working stayed up
    # (content sat in the Investigating fold until Stop flushed it).
    yield ConvEvent("assistant_done", {"turns": step + 1, "swarms": swarms})
    session._submit_housekeeping(
        session._maybe_ingest,
        user_message, list(turn_prose), list(turn_findings),
    )
    return ("return", user_message)


def dispatch_readonly_action(
    session: Any,
    act: PilotAction,
    idx: int,
    aid: str,
    prefetch: dict,
    is_native: bool,
) -> Iterator[Any]:
    """Assemble tool-results for a READ_ONLY_KINDS action (prefetch or live).

    Mechanical lift of the per-kind read-only branches from
    ``_send_locked_inner``. Caller must gate on ``act.kind in READ_ONLY_KINDS``.
    Yields the same ``action_result`` ConvEvents and history appends.
    """
    from .conversation import ConvEvent

    if act.kind == "read_file":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_read_file(act)

        if ok:
            content = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["file"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "file", "headline": f"Read {len(content)} chars from {act.path}"}],
            })
            session._append_action_result(act, aid, f"(read_file {act.path} returned)\n{content}", is_native)
        else:
            if status == "repo_not_open":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_file {aid} failed: {val})", is_native)
            elif status == "path_traversal":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_file {aid} failed: {val})", is_native)
            else:  # status == "exception"
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_file {act.path} failed: {val})", is_native)
        return

    if act.kind == "view_image":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_view_image(act)

        if ok:
            text = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["image"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "image", "headline": f"Viewed image {act.path}"}],
            })
            session._append_action_result(act, aid, f"(view_image {act.path}):\n{text}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(view_image {act.path} failed: {val})", is_native)
        return

    if act.kind == "list_dir":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_list_dir(act)

        if ok:
            count, result_text = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["dir"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "dir", "headline": f"Listed {count} items in {act.path or '/'}"}],
            })
            session._append_action_result(act, aid, f"(list_dir {act.path or '/'} returned)\n{result_text}", is_native)
        else:
            if status == "repo_not_open":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(list_dir {aid} failed: {val})", is_native)
            elif status == "path_traversal":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(list_dir {aid} failed: {val})", is_native)
            else:  # status == "exception"
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(list_dir {act.path or '/'} failed: {val})", is_native)
        return

    if act.kind == "web_search":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_web_search(act)

        if ok:
            result_text = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["web_search"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "web_search", "headline": f"Searched for '{act.query}'"}],
            })
            session._append_action_result(act, aid, f"(web_search '{act.query}' returned)\n{result_text}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(web_search '{act.query}' failed: {val})", is_native)
        return

    if act.kind == "web_fetch":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_web_fetch(act)

        if ok:
            result_text = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["web_fetch"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "web_fetch", "headline": f"Fetched {act.url}"}],
            })
            session._append_action_result(act, aid, f"(web_fetch '{act.url}' returned)\n{result_text}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(web_fetch '{act.url}' failed: {val})", is_native)
        return

    if act.kind == "read_pdf":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_read_pdf(act)

        if ok:
            result_text = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["read_pdf"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "read_pdf", "headline": f"Read PDF from {act.path or act.url}"}],
            })
            session._append_action_result(act, aid, f"(read_pdf '{act.path or act.url}' returned)\n{result_text}", is_native)
        else:
            if status == "repo_not_open":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_pdf {aid} failed: {val})", is_native)
            elif status == "path_traversal":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_pdf {aid} failed: {val})", is_native)
            else:  # status == "exception"
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_pdf '{act.path or act.url}' failed: {val})", is_native)
        return

    if act.kind == "search_codegraph":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_search_codegraph(act)

        if ok:
            kind, output = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["search_codegraph"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "search_codegraph", "headline": f"CodeGraph {kind}: {act.query}"}],
            })
            session._append_action_result(act, aid, f"(search_codegraph '{act.query}' returned)\n{output}", is_native)
        else:
            if status == "repo_not_open":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(search_codegraph {aid} failed: {val})", is_native)
            elif status == "filenotfound":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(search_codegraph '{act.query}' failed: CodeGraph CLI not found)", is_native)
            else:  # status == "exception"
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(search_codegraph '{act.query}' failed: {val})", is_native)
        return

    if act.kind == "search_files":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_search_files(act)

        if ok:
            output = val
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["search_files"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "search_files", "headline": f"Search Files: {act.query}"}],
            })
            session._append_action_result(act, aid, f"(search_files '{act.query}' returned)\n{output}", is_native)
        else:
            if status == "repo_not_open":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(search_files {aid} failed: {val})", is_native)
            elif status == "path_traversal":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(search_files {aid} failed: {val})", is_native)
            else:  # status == "exception" or "invalid_arguments"
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(search_files '{act.query}' failed: {val})", is_native)
        return

    if act.kind == "lsp":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_lsp(act)

        if ok:
            lang = (act.arguments or {}).get("language") or "auto"
            mode = (act.arguments or {}).get("mode") or "diagnostics"
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["lsp"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "lsp", "headline": f"LSP {lang}/{mode}"}],
            })
            session._append_action_result(act, aid, f"(lsp returned)\n{val}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(lsp failed: {val})", is_native)
        return

    # Unknown READ_ONLY_KINDS member — surface so a catalog drift cannot hang.
    err = f"Unhandled read-only action kind: {act.kind}"
    yield ConvEvent("action_result", {"id": aid, "error": err})
    session._append_action_result(act, aid, err, is_native)


def run_auto_verify(
    session: Any,
    *,
    turn_changed_files: list,
    auto_verify_iters: int,
    auto_verify_cap: int,
    plan: bool,
) -> Iterator[Any]:
    """Post-action scoped verify; on failure inject feedback and ask to retry.

    Mechanical lift of the AUTO-VERIFY LOOP from ``_send_locked_inner``.
    Generator return value is ``(auto_verify_iters, should_continue)``.
    Silent (no yields) when the gate conditions are not met.
    """
    import os

    from .conversation import ConvEvent

    if not (
        turn_changed_files
        and getattr(session.config, "auto_verify", True)
        and auto_verify_iters < auto_verify_cap
        and not session._cancel.is_set()
        and not plan
    ):
        return (auto_verify_iters, False)

    from harness import verify as _verify
    override = (getattr(session.config, "verify_command", "") or "").strip()
    _uniq_changed = list(dict.fromkeys(turn_changed_files))
    if override:
        verify_cmd = override
    else:
        try:
            verify_cmd = _verify.detect_verify_command(
                session.config.repo, _uniq_changed)
        except Exception:
            verify_cmd = None
    if verify_cmd:
        _verify_display = (
            _verify._command_display(verify_cmd)
            if hasattr(_verify, "_command_display")
            else str(verify_cmd)
        )
        yield ConvEvent("verifying", {"cmd": _verify_display, "auto": True})
        try:
            _timeout = int(os.environ.get("HARNESS_AUTO_VERIFY_TIMEOUT", "30"))
        except ValueError:
            _timeout = 30
        try:
            passed, output = _verify.run_verify(
                session.config.repo, verify_cmd, _uniq_changed,
                timeout=_timeout, cancel_event=session._cancel)
        except Exception as _ve:  # never break the turn on verify
            passed, output = True, f"[auto-verify skipped: {_ve}]"
        excerpt = output[-1500:] if output else ""
        yield ConvEvent("auto_verify", {
            "passed": passed,
            "command": _verify_display,
            "output_excerpt": excerpt,
        })
        if not passed and not session._cancel.is_set():
            auto_verify_iters += 1
            feedback = (
                "[auto-verify] The project check failed after your edits:\n"
                f"$ {_verify_display}\n{output}\n"
                "Fix the issue, then continue."
            )
            session._history.append({"role": "user", "content": feedback})
            return (auto_verify_iters, True)
    return (auto_verify_iters, False)


def dispatch_local_action(
    session: Any,
    act: PilotAction,
    aid: str,
    is_native: bool,
    turn_changed_files: list,
    act_goal: Any = None,
) -> Iterator[Any]:
    """Assemble tool-results for LOCAL_ACTION_KINDS (workspace / mutate / browse / mcp).

    Mechanical lift of the per-kind local branches from ``_send_locked_inner``
    (everything after read-only dispatch and before ``send_loop_dispatch``).
    Caller must gate on ``act.kind in LOCAL_ACTION_KINDS``. Yields the same
    ConvEvent shapes and history appends; mutates ``turn_changed_files`` on
    successful writes/edits.
    """
    import os

    from .conversation import ConvEvent, _mcp_result_text
    from .tool_dispatch import is_safe_path

    if act_goal is None:
        act_goal = action_display_goal(act)

    # ---- open_project branch --------------------------------------
    if act.kind == "open_project":
        target_repo = (act.path or "").strip()
        if not target_repo:
            err_msg = "Error: path is required for open_project action"
            yield ConvEvent("action_result", {"id": aid, "error": err_msg})
            session._append_action_result(act, aid, err_msg, is_native)
            return
        if not os.path.isdir(target_repo):
            err_msg = f"Error: path '{target_repo}' is not an existing directory"
            yield ConvEvent("action_result", {"id": aid, "error": err_msg})
            session._append_action_result(act, aid, err_msg, is_native)
            return

        # Update active configuration and environment -- but never
        # let an agent open_project yank the workspace onto the
        # Marionette app checkout itself.
        try:
            from harness.server import _cfg, _record_recent_workspace, _is_app_install_root
            if _is_app_install_root(target_repo):
                err_msg = (
                    "Refusing to open the Marionette app checkout as a "
                    "project; pick a user repository instead."
                )
                yield ConvEvent("action_result", {"id": aid, "error": err_msg})
                session._append_action_result(act, aid, err_msg, is_native, ok=False)
                return
            session.config.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo
            _cfg.repo = target_repo
            _record_recent_workspace(target_repo)
        except Exception:
            session.config.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo

        basename = os.path.basename(os.path.abspath(target_repo)) or "Workspace"
        yield ConvEvent("action_result", {
            "id": aid,
            "num": 1,
            "types": ["workspace"],
            "adapter": "local",
            "mode": "tool",
            "path": os.path.abspath(target_repo),
            "workspace_root": os.path.abspath(target_repo),
            "artifacts": [{"type": "workspace", "headline": f"Opened project: {basename}"}]
        })
        session._append_action_result(act, aid, f"Opened project: {basename}", is_native)
        return

    # ---- relocate_session branch ----------------------------------
    if act.kind == "relocate_session":
        args = act.arguments or {}
        target_repo = (
            (act.path or "").strip()
            or (act.repo or "").strip()
            or (args.get("workspace_root") or args.get("path") or args.get("repo") or "")
        ).strip()
        sid = (args.get("session_id") or args.get("id") or "").strip()
        title = args.get("title")
        if not target_repo:
            err_msg = "Error: workspace_root is required for relocate_session"
            yield ConvEvent("action_result", {"id": aid, "error": err_msg})
            session._append_action_result(act, aid, err_msg, is_native, ok=False)
            return
        try:
            from harness.server import _handle_session_relocate
            status, payload = _handle_session_relocate({
                "workspace_root": target_repo,
                "session_id": sid,
                "title": title if isinstance(title, str) else None,
            })
        except Exception as e:
            err_msg = f"Error relocating session: {e}"
            yield ConvEvent("action_result", {"id": aid, "error": err_msg})
            session._append_action_result(act, aid, err_msg, is_native, ok=False)
            return
        if status != 200 or not payload.get("ok"):
            err_msg = payload.get("error") or f"relocate failed ({status})"
            yield ConvEvent("action_result", {"id": aid, "error": err_msg})
            session._append_action_result(act, aid, err_msg, is_native, ok=False)
            return
        # Keep this runner's config.repo aligned with the server.
        try:
            session.config.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo
        except Exception:
            pass
        abs_target = os.path.abspath(target_repo)
        basename = os.path.basename(abs_target) or "Workspace"
        headline = f"Moved conversation into {basename}"
        yield ConvEvent("action_result", {
            "id": aid,
            "num": 1,
            "types": ["workspace"],
            "adapter": "local",
            "mode": "tool",
            "path": abs_target,
            "workspace_root": abs_target,
            "session_id": payload.get("active") or sid,
            "artifacts": [{"type": "workspace", "headline": headline}],
        })
        session._append_action_result(
            act, aid,
            f"{headline}\nsession={payload.get('active')} workspace_root={target_repo}",
            is_native,
        )
        return

    # ---- session_bank branch --------------------------------------
    if act.kind == "session_bank":
        args = act.arguments or {}
        query = (act.query or args.get("query") or "").strip()
        sid = (args.get("session_id") or args.get("id") or "").strip()
        try:
            limit = int(args.get("limit") if args.get("limit") is not None else (act.limit or 20))
        except (TypeError, ValueError):
            limit = 20
        try:
            from harness.server import _sessions, _sessions_state_dir
            from harness.sessions import load_transcript
            if sid:
                rows = [r for r in _sessions.list() if r.get("id") == sid]
                meta = rows[0] if rows else {"id": sid, "title": "(unknown)"}
                data = load_transcript(_sessions_state_dir(), sid)
                history = []
                if isinstance(data, dict):
                    history = data.get("history") or data.get("display") or []
                elif isinstance(data, list):
                    history = data
                lines = [
                    f"Session {sid}: {meta.get('title') or '(untitled)'}",
                    f"workspace_root: {meta.get('workspace_root') or meta.get('repo') or ''}",
                    f"created: {meta.get('created')}",
                    f"messages: {len(history)}",
                    "",
                ]
                for msg in history[:40]:
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role") or msg.get("type") or "?"
                    content = msg.get("content") or msg.get("text") or ""
                    if isinstance(content, list):
                        parts = []
                        for p in content:
                            if isinstance(p, dict) and p.get("type") == "text":
                                parts.append(str(p.get("text") or ""))
                            elif isinstance(p, str):
                                parts.append(p)
                        content = "\n".join(parts)
                    text = str(content).strip().replace("\n", " ")
                    if len(text) > 240:
                        text = text[:237] + "..."
                    if text:
                        lines.append(f"[{role}] {text}")
                val = "\n".join(lines)
            else:
                bank = _sessions.list_bank(
                    query=query,
                    limit=limit,
                    state_dir=_sessions_state_dir(),
                )
                lines = [f"Session bank ({len(bank)}):"]
                for row in bank:
                    lines.append(
                        f"- {row.get('id')} | {row.get('title') or '(untitled)'} | "
                        f"{row.get('workspace_root') or row.get('repo') or '(no root)'} | "
                        f"in={row.get('input_tokens', 0)} out={row.get('output_tokens', 0)}"
                    )
                val = "\n".join(lines) if bank else "No sessions found."
        except Exception as e:
            err_msg = f"session_bank failed: {e}"
            yield ConvEvent("action_result", {"id": aid, "error": err_msg})
            session._append_action_result(act, aid, err_msg, is_native, ok=False)
            return
        yield ConvEvent("action_result", {
            "id": aid, "num": 1, "types": ["session_bank"], "adapter": "local", "mode": "tool",
            "artifacts": [{"type": "session_bank", "headline": f"session_bank: {sid or query or 'list'}"}],
        })
        session._append_action_result(act, aid, f"(session_bank returned)\n{val}", is_native)
        return

    # ---- write_file branch ----------------------------------------
    if act.kind == "write_file":
        if not session.config.repo:
            error_msg = "No workspace directory (config.repo) is open."
            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
            session._append_action_result(act, aid, f"(write_file {aid} failed: {error_msg})", is_native)
            return
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(session.config.repo, target_path)
        if not is_safe_path(target_path, session.config.repo):
            error_msg = f"Path traversal attempt rejected: {act.path}"
            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
            session._append_action_result(act, aid, f"(write_file {aid} failed: {error_msg})", is_native)
            return
        try:
            ok, status, msg = session._do_write_file(act, write=False)
            if not ok:
                yield ConvEvent("action_result", {"id": aid, "error": msg})
                session._append_action_result(act, aid, f"(write_file {act.path} failed: {msg})", is_native)
                return

            try:
                cp_id = session._checkpoints.snapshot(
                    label=f"Before writing {act.path}",
                    trigger="write_file",
                    session_id=session.harness_session_id or None,
                )
                if cp_id:
                    yield ConvEvent("checkpoint", {
                        "id": cp_id,
                        "trigger": "write_file",
                        "label": f"Before writing {act.path}"
                    })
            except Exception as cp_err:
                import sys
                print(f"Checkpoint error before write_file: {cp_err}", file=sys.stderr)

            ok, status, msg = session._do_write_file(act, write=True)
            if not ok:
                yield ConvEvent("action_result", {"id": aid, "error": msg})
                session._append_action_result(act, aid, f"(write_file {act.path} failed: {msg})", is_native)
                return

            bytes_written = msg
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["file"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "file", "headline": f"Wrote {bytes_written} bytes to {act.path}"}],
            })
            session._append_action_result(act, aid, f"(write_file {act.path} successfully wrote {bytes_written} bytes)", is_native)
            turn_changed_files.append(target_path)
        except Exception as e:
            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
            session._append_action_result(act, aid, f"(write_file {act.path} failed: {e})", is_native)
        return
    # ---- edit_file branch -----------------------------------------
    if act.kind == "edit_file":
        if not session.config.repo:
            error_msg = "No workspace directory (config.repo) is open."
            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
            session._append_action_result(act, aid, f"(edit_file {aid} failed: {error_msg})", is_native)
            return
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(session.config.repo, target_path)
        if not is_safe_path(target_path, session.config.repo):
            error_msg = f"Path traversal attempt rejected: {act.path}"
            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
            session._append_action_result(act, aid, f"(edit_file {aid} failed: {error_msg})", is_native)
            return
        try:
            ok, status, msg = session._do_edit_file(act, write=False)
            if not ok:
                yield ConvEvent("action_result", {"id": aid, "error": msg})
                session._append_action_result(act, aid, f"(edit_file {act.path} failed: {msg})", is_native)
                return

            try:
                cp_id = session._checkpoints.snapshot(
                    label=f"Before editing {act.path}",
                    trigger="edit_file",
                    session_id=session.harness_session_id or None,
                )
                if cp_id:
                    yield ConvEvent("checkpoint", {
                        "id": cp_id,
                        "trigger": "edit_file",
                        "label": f"Before editing {act.path}"
                    })
            except Exception as cp_err:
                import sys
                print(f"Checkpoint error before edit_file: {cp_err}", file=sys.stderr)

            ok, status, msg = session._do_edit_file(act, write=True)
            if not ok:
                yield ConvEvent("action_result", {"id": aid, "error": msg})
                session._append_action_result(act, aid, f"(edit_file {act.path} failed: {msg})", is_native)
                return

            headline = msg
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["file"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "file", "headline": headline}],
            })
            session._append_action_result(act, aid, f"(edit_file {act.path} successfully edited: {headline})", is_native)
            turn_changed_files.append(target_path)
        except Exception as e:
            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
            session._append_action_result(act, aid, f"(edit_file {act.path} failed: {e})", is_native)
        return
    # ---- hash_edit branch -----------------------------------------
    if act.kind == "hash_edit":
        if not session.config.repo:
            error_msg = "No workspace directory (config.repo) is open."
            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
            session._append_action_result(act, aid, f"(hash_edit {aid} failed: {error_msg})", is_native)
            return
        target_path = act.path
        if not os.path.isabs(target_path):
            target_path = os.path.join(session.config.repo, target_path)
        if not is_safe_path(target_path, session.config.repo):
            error_msg = f"Path traversal attempt rejected: {act.path}"
            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
            session._append_action_result(act, aid, f"(hash_edit {aid} failed: {error_msg})", is_native)
            return
        try:
            ok, status, msg = session._do_hash_edit(act, write=False)
            if not ok:
                yield ConvEvent("action_result", {"id": aid, "error": msg})
                session._append_action_result(act, aid, f"(hash_edit {act.path} failed: {msg})", is_native)
                return

            try:
                cp_id = session._checkpoints.snapshot(
                    label=f"Before hash_edit {act.path}",
                    trigger="hash_edit",
                    session_id=session.harness_session_id or None,
                )
                if cp_id:
                    yield ConvEvent("checkpoint", {
                        "id": cp_id,
                        "trigger": "hash_edit",
                        "label": f"Before hash_edit {act.path}"
                    })
            except Exception as cp_err:
                import sys
                print(f"Checkpoint error before hash_edit: {cp_err}", file=sys.stderr)

            ok, status, msg = session._do_hash_edit(act, write=True)
            if not ok:
                yield ConvEvent("action_result", {"id": aid, "error": msg})
                session._append_action_result(act, aid, f"(hash_edit {act.path} failed: {msg})", is_native)
                return

            headline = f"hash_edit {act.path}: {msg}"
            hash_edit_result = {
                "id": aid, "num": 1, "types": ["file"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "file", "headline": headline}],
            }
            # AST preview (round 6, opt-in): structural diff
            # computed by _do_hash_edit on the write pass.
            ast_preview = getattr(self, "_last_ast_preview", None)
            if ast_preview and ast_preview.get("available"):
                hash_edit_result["ast_preview"] = ast_preview
            session._last_ast_preview = None
            yield ConvEvent("action_result", hash_edit_result)
            session._append_action_result(act, aid, f"(hash_edit {act.path} successfully applied: {headline})", is_native)
            turn_changed_files.append(target_path)
        except Exception as e:
            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
            session._append_action_result(act, aid, f"(hash_edit {act.path} failed: {e})", is_native)
        return
    # ---- run_command branch ---------------------------------------
    if act.kind == "run_command":
        if not session.config.repo:
            error_msg = "No workspace directory (config.repo) is open."
            yield ConvEvent("action_result", {"id": aid, "error": error_msg})
            session._append_action_result(act, aid, f"(run_command {aid} failed: {error_msg})", is_native)
            return
        # FULL-AUTO safety + cancellable execution live in
        # ToolDispatchMixin._do_run_command; yield/append stay here.
        ok, status, val = session._do_run_command(act)
        if not ok:
            if status == "blocked":
                block = val if isinstance(val, dict) else {"message": str(val)}
                block_msg = block.get("message") or str(val)
                command_hash = block.get("command_hash") or ""
                pending = session.register_pending_command_approval(
                    command=act.command or "",
                    command_hash=command_hash,
                    action_id=aid,
                    category=block.get("category") or "",
                    reason=block.get("reason") or "",
                    matched=block.get("matched") or "",
                )
                yield ConvEvent("command_approval_pending", {
                    "id": aid,
                    "command": act.command,
                    "command_hash": command_hash,
                    "session_id": pending.get("session_id"),
                    "workspace_root": pending.get("workspace_root"),
                    "category": pending.get("category") or block.get("category"),
                    "reason": pending.get("reason") or block.get("reason"),
                    "matched": pending.get("matched") or block.get("matched"),
                })
                session._append_action_result(act, aid, f"(run_command {aid} {block_msg})", is_native)
            else:
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(run_command {aid} failed: {val})", is_native)
            return
        output = val["output"]
        exit_code = val["exit_code"]
        yield ConvEvent("action_result", {
            "id": aid, "num": 1, "types": ["command"], "adapter": "local", "mode": "tool",
            "artifacts": [{"type": "command", "headline": f"Command exited with {exit_code}"}],
        })
        session._append_action_result(act, aid, f"(run_command '{act.command}' completed with exit code {exit_code})\n{output}", is_native)
        return
    # ---- search_tools branch ---------------------------------------
    if act.kind == "search_tools":
        try:
            ok, status, val = session._do_search_tools(act)
        except Exception as exc:
            ok, status, val = False, "exception", str(exc)

        if ok:
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["search_tools"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "search_tools", "headline": f"Tool search: {act.query or 'activate'}"}],
            })
            session._append_action_result(act, aid, f"(search_tools returned)\n{val}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(search_tools failed: {val})", is_native)
        return
    # ---- search_state branch ---------------------------------------
    if act.kind == "search_state":
        try:
            ok, status, val = session._do_search_state(act)
        except Exception as exc:
            ok, status, val = False, "exception", str(exc)

        if ok:
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["search_state"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "search_state", "headline": f"State search: {act.query}"}],
            })
            session._append_action_result(act, aid, f"(search_state returned)\n{val}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(search_state failed: {val})", is_native)
        return
    # ---- native browser / computer-use tools ----------------------
    if act.kind in ("browser_navigate", "browser_snapshot", "browser_click",
                    "browser_type", "browser_scroll", "browser_back",
                    "browser_get_text", "browser_screenshot"):
        try:
            from . import browser as _browser
            bargs = act.arguments or {}
            if act.kind == "browser_navigate":
                res = _browser.browser_navigate(bargs.get("url") or act.url or "")
            elif act.kind == "browser_snapshot":
                res = _browser.browser_snapshot()
            elif act.kind == "browser_click":
                res = _browser.browser_click(bargs.get("ref") or "")
            elif act.kind == "browser_type":
                res = _browser.browser_type(bargs.get("ref") or "", bargs.get("text") or "")
            elif act.kind == "browser_scroll":
                res = _browser.browser_scroll(bargs.get("direction") or "down")
            elif act.kind == "browser_back":
                res = _browser.browser_back()
            elif act.kind == "browser_get_text":
                res = _browser.browser_get_text()
            else:  # browser_screenshot
                res = _browser.browser_screenshot()
        except Exception as e:
            res = f"Error: {e}"
        yield ConvEvent("action_result", {
            "id": aid, "num": 1, "types": [act.kind], "adapter": "local", "mode": "tool",
            "artifacts": [{"type": act.kind, "headline": act.kind}],
        })
        session._append_action_result(act, aid, f"({act.kind} returned)\n{res}", is_native)
        return
    # ---- query_wiki branch ----------------------------------------
    if act.kind == "query_wiki":
        question = act.arguments.get("question") or ""
        if not session._wiki.configured:
            res = "wiki not configured"
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["query_wiki"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "query_wiki", "headline": f"Wiki: {question}"}],
            })
            session._append_action_result(act, aid, f"(query_wiki '{question}' returned)\n{res}", is_native)
            return

        try:
            res = session._wiki.query(question)
            # Grounded synthesis: fold the raw wiki result through
            # harness.nl_memory.answer_from_memory so the surfaced
            # text is a concise, cited answer instead of a raw dump.
            # Everything here is best-effort: on ANY failure we fall
            # straight back to the exact prior behavior (raw res).
            surfaced = f"(query_wiki '{question}' returned)\n{res}"
            try:
                grounded = session._grounded_wiki_answer(question, res)
                if grounded:
                    surfaced = (
                        f"(query_wiki '{question}' returned)\n"
                        f"{grounded}\n\n"
                        f"--- raw wiki result ---\n{res}"
                    )
            except Exception:
                # Never regress the raw-dump path.
                surfaced = f"(query_wiki '{question}' returned)\n{res}"
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["query_wiki"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "query_wiki", "headline": f"Wiki: {question}"}],
            })
            session._append_action_result(act, aid, surfaced, is_native)
        except Exception as e:
            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
            session._append_action_result(act, aid, f"(query_wiki '{question}' failed: {e})", is_native)
        return
    # ---- MCP tool call branch -------------------------------------
    if act.kind == "call_mcp":
        if session._mcp is None:
            yield ConvEvent("action_result", {"id": aid, "error": "MCP not available"})
            session._append_action_result(act, aid, f"(mcp {aid} unavailable)", is_native)
            return
        try:
            if act.tool:
                session._tool_catalog.activate([act.tool])
            out = session._mcp.call(act.tool, act.arguments)
            text = _mcp_result_text(out)
        except Exception as e:
            yield ConvEvent("action_result", {"id": aid, "error": f"mcp: {e}"})
            session._append_action_result(act, aid, f"(mcp {act.tool} failed: {e})", is_native)
            return
        yield ConvEvent("action_result", {
            "id": aid, "tool": act.tool, "num": 1,
            "types": ["mcp"], "adapter": "mcp", "mode": "tool",
            "artifacts": [{"type": "mcp", "headline": f"{act.tool}: {text[:120]}"}],
        })
        session._append_action_result(act, aid, f"(mcp {act.tool} returned)\n{text[:2000]}", is_native)
        return
    if act.kind == "manage_mcp":
        if session._mcp is None:
            yield ConvEvent("action_result", {"id": aid, "error": "MCP not available"})
            session._append_action_result(act, aid, "(manage_mcp unavailable)", is_native)
            return
        import json as _json_mcp
        args = act.arguments if isinstance(act.arguments, dict) else {}
        try:
            out = session._mcp.manage(
                str(args.get("action") or ""),
                name=str(args.get("name") or act.path or ""),
                url=str(args.get("url") or act.url or ""),
                command=str(args.get("command") or act.command or ""),
                args=args.get("args") if isinstance(args.get("args"), list) else None,
                env=args.get("env") if isinstance(args.get("env"), dict) else None,
            )
            text = _json_mcp.dumps(out, indent=2)[:4000]
        except Exception as e:
            yield ConvEvent("action_result", {"id": aid, "error": f"manage_mcp: {e}"})
            session._append_action_result(act, aid, f"(manage_mcp failed: {e})", is_native)
            return
        headline = act_goal or "manage_mcp"
        yield ConvEvent("action_result", {
            "id": aid, "num": 1,
            "types": ["manage_mcp"], "adapter": "mcp", "mode": "tool",
            "artifacts": [{"type": "manage_mcp", "headline": headline}],
        })
        session._append_action_result(
            act, aid, f"(manage_mcp {headline} returned)\n{text}", is_native,
        )
        return

    err = f"Unhandled local action kind: {act.kind}"
    yield ConvEvent("action_result", {"id": aid, "error": err})
    session._append_action_result(act, aid, err, is_native)
