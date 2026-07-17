from __future__ import annotations

"""Send-loop phase helpers peeled from SendLoopMixin._send_locked_inner.

These were nested closures / inline orchestration blocks inside the turn
kernel — hard to unit-test in isolation. They are mechanical extractions:
same queue/thread contracts, same exception surfaces, same ConvEvent shapes.
Explicit ``session`` / queue / schema args replace closure capture.

Public orchestration stays on SendLoopMixin.send / _send_locked /
_send_locked_inner; this module owns background-thread targets, prefetch
workers, stream-queue drain, per-step usage metering, idle steer/queue
finalization, read-only tool-result assembly, auto-verify, and small pure
helpers the kernel calls.
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
