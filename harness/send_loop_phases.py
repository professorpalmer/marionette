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
import queue as queue_mod
import re
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Dict, Iterator, Optional

from pmharness.bridge import execute_intent

from .log_reconstruction import check_outbound_reconstruction
from .pilot import PilotAction, StreamingSayExtractor
from .stream_identity import StreamDeltaBatch, normalize_delta_payload
from .stream_performance import (
    STREAM_PERFORMANCE_KEY,
    StreamTimingAccumulator,
    attach_stream_performance,
    call_timed_phase,
)
from .stream_performance_store import (
    StreamPerformanceReceiptStore,
    build_receipt,
    copy_stream_performance,
)
from .tool_timeout import invoke_do, run_with_tool_deadline
from .workspace_rules_refresh import maybe_refresh_workspace_rules
from .url_safety import sanitize_url_for_display

# job_XXXXXXXXXXXX — same pattern the parallel-dispatch stdout scanner used
# when nested inside _send_locked_inner.
_JOB_ID_RE = re.compile(r"\b(job_[a-fA-F0-9]{12})\b")

# ActionKind string set used by the execute-loop prefetch planner — membership
# stays typed against the pilot contract without living inside the god method.
READ_ONLY_KINDS: frozenset[str] = frozenset({
    "read_file", "list_dir", "search_codegraph", "search_files",
    "web_search", "web_fetch", "read_pdf", "view_image", "lsp",
    "peek_history", "peek_artifact",
})

# Honest composer wait-hint when the provider stream goes quiet mid-turn.
# Stage 1 is a one-shot at 9s; later stages escalate so a 15-minute stall
# is not a single stale "still working" line.
STREAM_IDLE_NOTICE_SEC = 9.0
STREAM_IDLE_ESCALATE_SEC = 60.0
STREAM_IDLE_STUCK_SEC = 300.0
STREAM_IDLE_POLL_SEC = 1.0
STREAM_IDLE_NOTICE_MESSAGE = "Provider still working — stream idle"
STREAM_IDLE_ESCALATE_MESSAGE = "Provider still idle after 1m — stream has not advanced"
STREAM_IDLE_STUCK_MESSAGE = (
    "Provider still idle after 5m — consider Stop if the stream is stuck"
)

LOCAL_ACTION_KINDS: frozenset[str] = frozenset({
    "open_project", "relocate_session", "session_bank",
    "write_file", "edit_file", "hash_edit", "run_command",
    "run_command_batch", "run_ipython", "wait",
    "search_tools", "search_state",
    "store_scratch", "load_scratch", "list_scratch", "clear_scratch",
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_get_text", "browser_screenshot",
    "query_wiki", "call_mcp", "manage_mcp",
})

# Mutating / side-effecting kinds blocked in plan mode (same gate as write/edit
# and run_implement). Includes MCP call + manage so plan turns cannot mutate
# external servers or invoke tools that may write. Browser_* is the MCP-sibling
# peel: navigate/click/type/etc. are external side effects even when some
# variants are observational (snapshot/screenshot still drive a live page).
PLAN_SKIP_KINDS: frozenset[str] = frozenset({
    "run_implement", "run_parallel",
    "write_file", "edit_file", "hash_edit", "run_command",
    "run_command_batch", "run_ipython",
    "call_mcp", "manage_mcp", "memory",
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_get_text", "browser_screenshot",
})

# Cap for run_command SSE ``output`` so the UI card can show an excerpt without
# dumping unbounded shell stdout into the event stream.
_RUN_COMMAND_UI_OUTPUT_CAP = 4 * 1024


def _yield_task_profile_escalation(session: Any, turn_changed_files: list) -> Iterator[Any]:
    """Best-effort MICRO/STANDARD escalate after a successful write/edit."""
    from .conversation import ConvEvent

    try:
        from .task_transaction import note_files

        session._task_tx = note_files(
            getattr(session, "_task_tx", None), turn_changed_files,
        )
    except Exception:
        pass
    try:
        uniq = len(dict.fromkeys(turn_changed_files))
        payload = session._maybe_escalate_task_profile(files_touched=uniq)
        if payload:
            yield ConvEvent("task_profile", payload)
    except Exception:
        pass


def begin_turn_task_kernel(session: Any, user_message: str) -> None:
    """Start a compact task transaction and reset per-turn verify flags."""
    try:
        from .task_transaction import new_transaction

        session._task_tx = new_transaction(user_message)
    except Exception:
        session._task_tx = None
    session._turn_ran_command = False
    session._verify_remind_count = 0
    session._turn_user_message = user_message or ""
    session._turn_verification = ""
    try:
        from .pilot_guards import apply_session_pending_swarm_mandate

        apply_session_pending_swarm_mandate(session, user_message)
    except Exception:
        pass


def _note_turn_command(session: Any, verification: str = "") -> None:
    """Mark that this turn ran a command (or auto-verify). Never raises."""
    try:
        session._turn_ran_command = True
        text = (verification or "").strip()
        if not text:
            return
        session._turn_verification = text
        from .task_transaction import note_verification

        session._task_tx = note_verification(
            getattr(session, "_task_tx", None), text,
        )
    except Exception:
        pass


def persist_turn_receipt(session: Any, user_message: str = "") -> None:
    """Append a compact JSONL receipt. Best-effort; never raises."""
    try:
        from .task_receipt import (
            append_receipt,
            build_receipt,
            compute_patch_hash,
            git_branch,
            prompt_hash,
        )
        from .task_transaction import as_dict

        cfg = getattr(session, "config", None)
        state_dir = str(
            getattr(cfg, "state_dir", "")
            or getattr(session, "state_dir", "")
            or ""
        )
        if not state_dir:
            return
        txd = as_dict(getattr(session, "_task_tx", None))
        files = list(txd.get("files") or [])
        msg = user_message or getattr(session, "_turn_user_message", "") or ""
        repo = str(getattr(cfg, "repo", "") or "")
        verification = (
            getattr(session, "_turn_verification", "")
            or txd.get("verification")
            or ""
        )
        if getattr(session, "_turn_ran_command", False) and not verification:
            verification = "pass"
        elif files and not getattr(session, "_turn_ran_command", False) and not verification:
            verification = "skipped"
        model = ""
        adapter = ""
        try:
            model = str(getattr(getattr(session, "pilot", None), "model", "") or "")
        except Exception:
            pass
        try:
            adapter = str(getattr(cfg, "driver", "") or "")
        except Exception:
            pass
        rec = build_receipt(
            task_id=str(getattr(session, "harness_session_id", "") or "") or "turn",
            profile=getattr(session, "_task_profile", "") or "",
            profile_source=getattr(session, "_task_profile_source", "") or "",
            escalated_from=getattr(session, "_task_profile_escalated_from", None),
            model=model,
            adapter=adapter,
            prompt_hash=prompt_hash(msg),
            repo=repo,
            branch=git_branch(repo) if repo else "",
            changed_files=files,
            patch_hash=compute_patch_hash(files),
            verification=verification,
        )
        append_receipt(state_dir, rec)
    except Exception:
        return


def finalize_assistant_turn(
    session: Any,
    *,
    user_message: str,
    step: int,
    swarms: Any,
    turn_prose: list,
    turn_findings: list,
    extra: Optional[dict] = None,
) -> Iterator[Any]:
    """Persist a compact receipt, emit assistant_done, then housekeeping."""
    from .conversation import ConvEvent

    persist_turn_receipt(session, user_message)
    payload: Dict[str, Any] = {"turns": step + 1, "swarms": swarms}
    if extra:
        payload.update(extra)
    yield ConvEvent("assistant_done", payload)
    session._submit_housekeeping(
        session._maybe_ingest,
        user_message, list(turn_prose), list(turn_findings),
    )


def maybe_soft_verify_nudge(session: Any) -> Iterator[Any]:
    """Remind-then-escalate unverified MICRO/STANDARD edits. True = continue."""
    from .conversation import ConvEvent

    try:
        from .task_transaction import as_dict
        from .tool_requirement import SoftToolRequirement

        txd = as_dict(getattr(session, "_task_tx", None))
        files = list(txd.get("files") or [])
        profile = getattr(session, "_task_profile", "") or ""
        ran = bool(getattr(session, "_turn_ran_command", False))
        count = int(getattr(session, "_verify_remind_count", 0) or 0)
        nfiles = len(files)
        if SoftToolRequirement.should_remind_verify(profile, nfiles, ran, count):
            session._history.append({
                "role": "user",
                "content": SoftToolRequirement.remind_message(),
            })
            session._verify_remind_count = count + 1
            return True
        if SoftToolRequirement.should_escalate_unverified(
            profile, nfiles, ran, count,
        ):
            try:
                payload = session._maybe_escalate_task_profile(
                    files_touched=nfiles,
                )
            except Exception:
                payload = None
            if payload:
                yield ConvEvent("task_profile", payload)
            session._turn_verification = "unverified"
        return False
    except Exception:
        return False


def _truncate_run_command_ui_output(
    output: str, cap: int = _RUN_COMMAND_UI_OUTPUT_CAP,
) -> str:
    """Head+tail excerpt for the investigation Run card (not model history)."""
    text = output if isinstance(output, str) else str(output or "")
    if len(text) <= cap:
        return text
    head_len = cap // 2
    tail_len = cap - head_len
    omitted = len(text) - cap
    marker = (
        f"\n... [truncated {omitted} chars of {len(text)}-char output "
        f"-- middle elided for UI] ...\n"
    )
    return text[:head_len] + marker + text[-tail_len:]


def _run_command_artifact_headline(
    exit_code: int, output: str, status: str = "ok",
) -> str:
    """Compact card headline: prefer ``exit N · <first line>`` when output exists.

    Non-ok statuses (cancelled/timeout/truncated/error) lead so a cancelled or
    timed-out command cannot be mistaken for a successful validation.
    """
    first_line = ""
    for line in (output or "").splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if status and status not in ("ok", "success"):
        prefix = status
    else:
        prefix = f"exit {exit_code}"
    if first_line:
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        return f"{prefix} · {first_line}"
    if status and status not in ("ok", "success"):
        return f"Command {status} (exit {exit_code})"
    return f"Command exited with {exit_code}"


def _with_command_footer(history_text: str, payload: Dict[str, Any]) -> str:
    """Append cwd / recovery-hint / spill footer lines to model-visible history.

    Kept as a footer so the existing ``(run_command '...' completed with exit
    code N)`` header and raw output stay byte-identical for every consumer.
    """
    footer = []
    cwd = payload.get("cwd")
    if cwd:
        footer.append(f"[cwd: {cwd}]")
    if payload.get("spill_uri"):
        footer.append(
            f"[full output ({payload.get('output_chars')} chars) saved to "
            f"{payload['spill_uri']}; read_file works on that URI]"
        )
    if payload.get("hint"):
        footer.append(f"[hint: {payload['hint']}]")
    if not footer:
        return history_text
    return history_text + "\n" + "\n".join(footer)


def pilot_accepts_session_id(pilot_method: Any) -> bool:
    """True when the driver method declares session_id or accepts **kwargs."""
    try:
        params = inspect.signature(pilot_method).parameters
    except Exception:
        return False
    if "session_id" in params:
        return True
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def maybe_attach_pilot_session_id(
    kwargs: Dict[str, Any],
    pilot_method: Any,
    harness_session_id: Optional[str],
) -> None:
    """Attach Marionette chat session id for provider prompt-cache affinity."""
    sid = (harness_session_id or "").strip()
    if not sid or not pilot_accepts_session_id(pilot_method):
        return
    kwargs["session_id"] = sid


def _finish_and_attach_timing(acc: Any, resp: Any) -> None:
    """Mark provider-call return and merge a snapshot. Never raises.

    This is request dispatch → provider return, not drain terminal.
    Malformed ``tokens_out`` must not drop the rest of the receipt or
    raise into the stream/send hot path. Billing still reads ``tokens_out``
    from the response itself.
    """
    if acc is None:
        return
    try:
        acc.finish()
    except Exception:
        pass
    try:
        tokens_out = getattr(resp, "tokens_out", 0)
    except Exception:
        tokens_out = 0
    try:
        attach_stream_performance(resp, acc.snapshot(tokens_out=tokens_out))
    except Exception:
        return


_PROVIDER_DISPATCH_INVOKED_ATTR = "_provider_dispatch_invoked"


def _clear_provider_dispatch_invoked(session: Any) -> None:
    try:
        setattr(session, _PROVIDER_DISPATCH_INVOKED_ATTR, False)
    except Exception:
        return


def _mark_provider_dispatch_invoked(session: Any) -> None:
    try:
        setattr(session, _PROVIDER_DISPATCH_INVOKED_ATTR, True)
    except Exception:
        return


def _provider_dispatch_was_invoked(session: Any) -> bool:
    try:
        return bool(getattr(session, _PROVIDER_DISPATCH_INVOKED_ATTR, False))
    except Exception:
        return False


def dispatch_sync_pilot_chat(
    session: Any,
    tools_schema: Any,
    sys_prompt: str,
    *,
    accumulator: Any = None,
) -> Any:
    """Non-streaming ``pilot.chat`` with the shared sanitize + honesty check."""
    chat_kwargs = {
        "tools": tools_schema,
        "system": sys_prompt,
    }
    maybe_attach_pilot_session_id(
        chat_kwargs,
        session.pilot.chat,
        getattr(session, "harness_session_id", None),
    )
    outbound = session._messages_for_provider()
    try:
        check_outbound_reconstruction(session, outbound, sys_prompt)
    except Exception:
        pass
    if accumulator is not None:
        try:
            accumulator.mark_request_start()
        except Exception:
            pass
    _mark_provider_dispatch_invoked(session)
    resp = session.pilot.chat(outbound, **chat_kwargs)
    _finish_and_attach_timing(accumulator, resp)
    return resp


def run_stream(
    session: Any,
    q: Any,
    tools_schema: Any,
    sys_prompt: str,
    *,
    clock: Any = None,
    accumulator: Any = None,
) -> None:
    """Background target: pilot.chat_stream → queue (delta/reasoning/tool_hint/wait/done/error).

    Best-effort ``stream_performance`` timing is merged onto the terminal
    DriverResponse.meta. Queue event shapes and order are unchanged.
    ``clock`` is a monotonic callable for deterministic tests.
    ``accumulator`` is an optional send-thread pre-request clock; when omitted
    this function constructs its own so standalone tests keep working.
    Request-start is marked immediately before ``chat_stream``, after outbound
    construction / reconstruction check.
    """
    try:
        acc = accumulator
        if acc is None:
            try:
                acc = StreamTimingAccumulator(clock=clock)
            except Exception:
                acc = None
        # Stream-thread close of send-thread thread_start (no-op if unopened).
        if acc is not None:
            try:
                acc.end_phase("thread_start")
            except Exception:
                pass

        def _on_delta(delta: Any) -> None:
            if acc is not None:
                try:
                    acc.note("delta", delta)
                except Exception:
                    pass
            q.put(("delta", delta))

        def _on_reasoning(delta: Any) -> None:
            if acc is not None:
                try:
                    acc.note("reasoning", delta)
                except Exception:
                    pass
            q.put(("reasoning", delta))

        kwargs = {
            "tools": tools_schema,
            "system": sys_prompt,
            "on_delta": _on_delta,
            "on_reasoning_delta": _on_reasoning,
            "on_tool_hint": lambda name: q.put(("tool_hint", name)),
        }
        try:
            params = inspect.signature(session.pilot.chat_stream).parameters
        except Exception:
            params = {}
        if "on_wait_notice" in params:
            kwargs["on_wait_notice"] = (
                lambda msg: q.put(("wait", msg))
            )
        if "on_stream_item_done" in params:
            kwargs["on_stream_item_done"] = (
                lambda payload: q.put(("item_done", payload))
            )
        maybe_attach_pilot_session_id(
            kwargs,
            session.pilot.chat_stream,
            getattr(session, "harness_session_id", None),
        )
        # Sanitize immediately before dispatch (same seam as sync chat).
        outbound = session._messages_for_provider()
        try:
            check_outbound_reconstruction(session, outbound, sys_prompt)
        except Exception:
            pass
        if acc is not None:
            try:
                acc.mark_request_start()
            except Exception:
                pass
        _mark_provider_dispatch_invoked(session)
        r = session.pilot.chat_stream(
            outbound,
            **kwargs,
        )
        _finish_and_attach_timing(acc, r)
        q.put(("done", r))
    except Exception as ex:
        q.put(("error", ex))


def dispatch_pilot_provider_call(
    session: Any,
    *,
    plan: bool,
    sys_prompt: str,
    prompt: str,
    synthesis_nudge_active: bool,
    accumulator: Any = None,
) -> Iterator[Any]:
    """Apply host mode and dispatch chat_stream / chat / complete.

    Generator return is ``(streamed_prose, resp)``. The stream path yields the
    same ``drain_stream_queue`` ConvEvents as the former inline kernel.
    ``thread_start`` is opened on the send thread and closed on the stream
    thread; request-start is marked immediately before the provider call.
    """
    _clear_provider_dispatch_invoked(session)
    try:
        session._step_tools_schema = None
    except Exception:
        pass
    # Cursor CLI/ACP: Autopilot → agent tools; Marionette Plan → ask.
    # Env HARNESS_CURSOR_CLI_MODE still wins inside apply_host_mode.
    _apply_mode = getattr(session.pilot, "apply_host_mode", None)
    if callable(_apply_mode):
        try:
            _apply_mode(plan=plan)
        except Exception:
            pass
    if hasattr(session.pilot, "chat"):
        from .send_loop import _build_step_tools
        tools_schema = call_timed_phase(
            accumulator,
            "prompt_tools",
            _build_step_tools,
            synthesis_nudge_active,
            session._build_visible_tools_schema,
        )
        try:
            session._step_tools_schema = tools_schema
        except Exception:
            pass
        is_interactive = not getattr(session.config, "no_delegation", False)
        # Gate on an EXPLICIT capability flag (is True) + a callable chat_stream.
        # Using `is True` avoids MagicMock test pilots (which fabricate any attr as a
        # truthy Mock) wrongly entering the streaming branch.
        _can_stream = (
            getattr(session.pilot, "supports_streaming", False) is True
            and callable(getattr(session.pilot, "chat_stream", None))
        )
        if is_interactive and _can_stream:
            import queue
            import threading
            q = queue.Queue()
            t = threading.Thread(
                target=run_stream,
                args=(session, q, tools_schema, sys_prompt),
                kwargs={"accumulator": accumulator},
                daemon=True,
            )
            if accumulator is not None:
                try:
                    accumulator.begin_phase("thread_start")
                except Exception:
                    pass
            try:
                t.start()
            except Exception:
                if accumulator is not None:
                    try:
                        accumulator.end_phase("thread_start")
                    except Exception:
                        pass
                raise
            return (yield from drain_stream_queue(q, accumulator=accumulator))
        resp = dispatch_sync_pilot_chat(
            session, tools_schema, sys_prompt, accumulator=accumulator,
        )
        return "", resp

    # Same affinity helper as chat — only when complete() declares session_id.
    # Compaction summarizers call complete() directly without this attach.
    complete_kwargs: dict = {"system": sys_prompt}
    maybe_attach_pilot_session_id(
        complete_kwargs,
        session.pilot.complete,
        getattr(session, "harness_session_id", None),
    )
    if accumulator is not None:
        try:
            accumulator.mark_request_start()
        except Exception:
            pass
    _mark_provider_dispatch_invoked(session)
    resp = session.pilot.complete(prompt, **complete_kwargs)
    _finish_and_attach_timing(accumulator, resp)
    return "", resp


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
            return idx, invoke_do(session, act, lambda: session._do_search_codegraph(act))
        elif kind == "search_files":
            return idx, invoke_do(session, act, lambda: session._do_search_files(act))
        elif kind == "web_search":
            return idx, invoke_do(session, act, lambda: session._do_web_search(act))
        elif kind == "web_fetch":
            return idx, invoke_do(session, act, lambda: session._do_web_fetch(act))
        elif kind == "read_pdf":
            return idx, invoke_do(session, act, lambda: session._do_read_pdf(act))
        elif kind == "view_image":
            return idx, session._do_view_image(act)
        elif kind == "lsp":
            return idx, session._do_lsp(act)
        elif kind == "peek_history":
            return idx, session._do_peek_history(act)
        elif kind == "peek_artifact":
            return idx, session._do_peek_artifact(act)
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
    dispatch_id: str = "",
) -> None:
    """Background target: execute_intent with on_delta → delta_q (delta/done/error).

    The intent's validated subject repo wins over the session workspace so an
    explicit ``run_swarm(repo=...)`` audit reads that checkout instead of the
    open project. Passed per call, never via the process-global env pointer.
    """
    try:
        from .repo_resolve import resolve_effective_repo
        _raw_repo = (
            (getattr(intent, "repo", None) or "").strip()
            or (session.config.repo or "").strip()
        )
        _cwd = resolve_effective_repo(_raw_repo) if _raw_repo else None
        r = execute_intent(
            intent,
            state_dir=session.state_dir,
            session_id=session.harness_session_id or "",
            dispatch_id=dispatch_id,
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
    elif act.kind == "run_ipython":
        code = (act.content or "").strip()
        act_goal = code[:80] + ("…" if len(code) > 80 else "")
    elif act.kind == "run_command_batch":
        cmds = list(getattr(act, "commands", None) or [])
        act_goal = f"command batch ({len(cmds)} commands)"
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
        act_goal = sanitize_url_for_display(act.url) if act.url else act.url
    elif act.kind == "read_pdf":
        target = act.path or act.url
        if target and str(target).startswith(("http://", "https://")):
            act_goal = sanitize_url_for_display(str(target))
        else:
            act_goal = target
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


def _emit_batched_delta(
    batch: StreamDeltaBatch,
    *,
    event_kind: str,
    default_channel: str,
) -> Optional[Any]:
    """Flush a pending batch into a ConvEvent, or None when empty."""
    from .conversation import ConvEvent

    payload = batch.flush()
    if not payload:
        return None
    text = payload.get("text") or ""
    if not text:
        return None
    data: Dict[str, Any] = {"text": text}
    if event_kind == "thinking":
        data["delta"] = True
    for key in ("stream_id", "output_index", "channel"):
        if key in payload and payload[key] is not None:
            data[key] = payload[key]
    if "channel" not in data and default_channel:
        data["channel"] = default_channel
    return ConvEvent(event_kind, data)  # type: ignore[arg-type]


def drain_stream_queue(q: Any, accumulator: Any = None) -> Iterator[Any]:
    """Consume a ``run_stream`` queue and yield ConvEvents until done/error.

    On success, generator return value is ``(streamed_prose, resp)``. On
    transport failure the queued exception is re-raised (same as the former
    inline loop). Lazy-imports ConvEvent to avoid an import cycle with
    conversation → send_loop → send_loop_phases.

    Accumulated reasoning is stashed on ``resp.meta`` (when present) as
    ``streamed_reasoning`` / ``stream_ended_on_reasoning`` so the send loop
    can promote a thought-only finale into an assistant message.

    Same-(channel, stream_id) deltas are batched (~16ms / 80 chars) before
    becoming SSE frames so word-sized tokens cannot exhaust the replay ring.
    Each new contentful (channel, stream_id) answer/reasoning identity yields
    its first frame immediately so perceived TTFT is not gated on the batch
    timeout; later same-identity deltas still coalesce. Progress stays on
    the batch path. Identities without stream_id stay on the immediate
    legacy path (no batching). Barriers (item done, tool hint, channel
    change, terminal) always flush first.

    When ``accumulator`` is provided, the first cleaned-answer instant is
    marked at the say-extractor boundary (backend event-ready, not paint)
    and merged key-wise onto ``resp.meta`` at terminal.
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
    answer_batch = StreamDeltaBatch()
    progress_batch = StreamDeltaBatch()
    reasoning_batch = StreamDeltaBatch()
    first_frames_emitted = set()
    last_queue_activity = time.monotonic()
    stream_idle_stage = 0

    def _note_queue_activity():
        nonlocal last_queue_activity, stream_idle_stage
        last_queue_activity = time.monotonic()
        stream_idle_stage = 0

    def _maybe_emit_stream_idle_notice():
        nonlocal stream_idle_stage
        idle_for = time.monotonic() - last_queue_activity
        if idle_for >= STREAM_IDLE_STUCK_SEC and stream_idle_stage < 3:
            stream_idle_stage = 3
            return ConvEvent("notice", {
                "message": STREAM_IDLE_STUCK_MESSAGE,
                "kind": "wait",
            })
        if idle_for >= STREAM_IDLE_ESCALATE_SEC and stream_idle_stage < 2:
            stream_idle_stage = 2
            return ConvEvent("notice", {
                "message": STREAM_IDLE_ESCALATE_MESSAGE,
                "kind": "wait",
            })
        if idle_for >= STREAM_IDLE_NOTICE_SEC and stream_idle_stage < 1:
            stream_idle_stage = 1
            return ConvEvent("notice", {
                "message": STREAM_IDLE_NOTICE_MESSAGE,
                "kind": "wait",
            })
        return None

    def _flush_all():
        for bat, ek, ch in (
            (progress_batch, "message_delta", "progress"),
            (answer_batch, "message_delta", "answer"),
            (reasoning_batch, "thinking", "reasoning"),
        ):
            ev = _emit_batched_delta(bat, event_kind=ek, default_channel=ch)
            if ev is not None:
                yield ev

    def _flush_overdue():
        now = time.monotonic()
        for bat, ek, ch in (
            (progress_batch, "message_delta", "progress"),
            (answer_batch, "message_delta", "answer"),
            (reasoning_batch, "thinking", "reasoning"),
        ):
            if bat.overdue(now):
                ev = _emit_batched_delta(bat, event_kind=ek, default_channel=ch)
                if ev is not None:
                    yield ev

    def _mark_visible_answer() -> None:
        if accumulator is None:
            return
        try:
            mark = getattr(accumulator, "mark_first_visible_answer", None)
            if callable(mark):
                mark()
        except Exception:
            return

    def _emit_push_or_first_frame(
        batch,
        flushed,
        *,
        event_kind,
        default_channel,
        stream_id,
    ):
        """Yield a threshold/identity flush, then this identity's first frame.

        First-frame is per (channel, stream_id), not once per channel for
        the whole drain. A later stream_id (post-tool narration, dual
        output) gets its own immediate first frame; later same-identity
        deltas still batch.
        """
        if flushed:
            data = dict(flushed)
            data.setdefault("channel", default_channel)
            if event_kind == "thinking":
                data["delta"] = True
            yield ConvEvent(event_kind, data)
            flushed_sid = str(flushed.get("stream_id") or "").strip()
            first_frames_emitted.add((default_channel, flushed_sid))
        if not batch.pending:
            return
        identity = (default_channel, stream_id)
        if identity in first_frames_emitted:
            return
        ev = _emit_batched_delta(
            batch, event_kind=event_kind, default_channel=default_channel,
        )
        if ev is not None:
            first_frames_emitted.add(identity)
            yield ev

    def _handle_assistant_delta(val: Any):
        nonlocal last_content_kind
        text, meta = normalize_delta_payload(val)
        if not text:
            return
        channel = str(meta.get("channel") or "answer").strip().lower()
        if channel == "progress":
            # Visible progress must not enter the JSON say extractor — a leading
            # commentary stream would force BARE mode and break later envelopes.
            last_content_kind = "prose"
            sid = str(meta.get("stream_id") or "").strip()
            if not sid:
                # No identity → no batching (legacy / crumb path).
                for ev in _flush_all():
                    yield ev
                data = {"text": text, "channel": "progress"}
                yield ConvEvent("message_delta", data)
                return
            flushed = progress_batch.push(
                text, meta, default_channel="progress",
            )
            if flushed:
                data = dict(flushed)
                data.setdefault("channel", "progress")
                yield ConvEvent("message_delta", data)
            return
        # Answer / legacy plain deltas: extract say prose when JSON-shaped.
        clean = say_extractor.feed(text)
        if not clean:
            return
        _mark_visible_answer()
        streamed_prose.append(clean)
        last_content_kind = "prose"
        ans_meta = dict(meta)
        ans_meta["channel"] = "answer"
        sid = str(ans_meta.get("stream_id") or "").strip()
        if not sid:
            for ev in _flush_all():
                yield ev
            data = {"text": clean, "channel": "answer"}
            # Preserve output_index when present even without stream_id.
            if "output_index" in ans_meta:
                data["output_index"] = ans_meta["output_index"]
            yield ConvEvent("message_delta", data)
            return
        flushed = answer_batch.push(clean, ans_meta, default_channel="answer")
        for ev in _emit_push_or_first_frame(
            answer_batch,
            flushed,
            event_kind="message_delta",
            default_channel="answer",
            stream_id=sid,
        ):
            yield ev

    def _handle_reasoning_delta(val: Any):
        nonlocal last_content_kind
        text, meta = normalize_delta_payload(val)
        if not text:
            return
        streamed_reasoning.append(text)
        last_content_kind = "reasoning"
        r_meta = dict(meta)
        r_meta["channel"] = "reasoning"
        sid = str(r_meta.get("stream_id") or "").strip()
        if not sid:
            for ev in _flush_all():
                yield ev
            yield ConvEvent("thinking", {"text": text, "delta": True})
            return
        flushed = reasoning_batch.push(
            text, r_meta, default_channel="reasoning",
        )
        for ev in _emit_push_or_first_frame(
            reasoning_batch,
            flushed,
            event_kind="thinking",
            default_channel="reasoning",
            stream_id=sid,
        ):
            yield ev

    while True:
        # Prefer a short wait when a batch is open so time thresholds can fire
        # without needing another inbound token.
        has_pending = (
            answer_batch.pending
            or progress_batch.pending
            or reasoning_batch.pending
        )
        try:
            if has_pending:
                kind, val = q.get(timeout=0.01)
            else:
                kind, val = q.get(timeout=STREAM_IDLE_POLL_SEC)
        except queue_mod.Empty:
            for ev in _flush_overdue():
                yield ev
            # If still pending after overdue check, force-flush on idle.
            if (
                answer_batch.pending
                or progress_batch.pending
                or reasoning_batch.pending
            ):
                for ev in _flush_all():
                    yield ev
            idle_notice = _maybe_emit_stream_idle_notice()
            if idle_notice is not None:
                yield idle_notice
            continue

        _note_queue_activity()
        if kind == "delta":
            for ev in _handle_assistant_delta(val):
                yield ev
            for ev in _flush_overdue():
                yield ev
        elif kind == "reasoning":
            for ev in _handle_reasoning_delta(val):
                yield ev
            for ev in _flush_overdue():
                yield ev
        elif kind == "item_done":
            for ev in _flush_all():
                yield ev
            text, meta = normalize_delta_payload(val if val is not None else {})
            sid = meta.get("stream_id")
            if not sid and isinstance(val, dict):
                sid = val.get("stream_id")
            if not sid and isinstance(val, str) and val.strip():
                sid = val.strip()
            if sid:
                yield ConvEvent("stream_item_done", {"stream_id": str(sid)})
        elif kind == "tool_hint":
            # Drivers may pass a plain name or a structured
            # {name, goal, id, status} payload (Cursor ACP / stream-json).
            # Bare "tool" used to paint "Investigating · tool tool" in the fold.
            for ev in _flush_all():
                yield ev
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
            for ev in _flush_all():
                yield ev
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
            if accumulator is not None:
                try:
                    mark_backend = getattr(accumulator, "mark_backend_ready", None)
                    if callable(mark_backend):
                        mark_backend()
                except Exception:
                    pass
                try:
                    tokens_out = getattr(val, "tokens_out", 0)
                except Exception:
                    tokens_out = 0
                try:
                    attach_stream_performance(
                        val, accumulator.snapshot(tokens_out=tokens_out),
                    )
                except Exception:
                    pass
            return "".join(streamed_prose), val
        elif kind == "error":
            for ev in _flush_all():
                yield ev
            raise val


def classify_provider_receipt_status(resp: Any) -> str:
    """Map a driver response to a receipt terminal status. Never raises."""
    try:
        error = getattr(resp, "error", None) if resp is not None else None
        if not error:
            return "success"
        from pmharness.drivers import error_classifier
        err_cls = error_classifier.classify(None, error)
        if err_cls == error_classifier.ErrorClass.CONTEXT_OVERFLOW:
            return "context_overflow"
        return "error"
    except Exception:
        return "error"


def _receipt_model_label(session: Any, resp: Any, driver: str) -> str:
    meta = getattr(resp, "meta", None) if resp is not None else None
    if isinstance(meta, dict):
        raw = meta.get("model")
        if isinstance(raw, str):
            text = raw.strip()
            if text and len(text) <= 128 and "\n" not in text and "\x00" not in text:
                return text
    if "/" in driver:
        return driver.rsplit("/", 1)[-1]
    return driver


def _receipt_meta_label(meta: Any, key: str) -> str:
    if not isinstance(meta, dict):
        return ""
    raw = meta.get(key)
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or "\n" in text or "\x00" in text or len(text) > 128:
        return ""
    return text


def _receipt_identity_kwargs(resp: Any, driver: str) -> Dict[str, Any]:
    """Pull requested/served identity + token totals off a driver response."""
    meta = getattr(resp, "meta", None) if resp is not None else None
    if not isinstance(meta, dict):
        meta = {}
    requested = _receipt_meta_label(meta, "requested_model")
    if not requested and ":" in driver:
        requested = driver.split(":", 1)[-1].strip()
    served = _receipt_meta_label(meta, "served_model")
    identity = _receipt_meta_label(meta, "identity_status").lower()
    token_basis = _receipt_meta_label(meta, "token_basis").lower()
    out: Dict[str, Any] = {}
    if requested:
        out["requested_model"] = requested
    if served:
        out["served_model"] = served
    if identity:
        out["identity_status"] = identity
    if token_basis:
        out["token_basis"] = token_basis
    elif meta.get("raw_usage") or served or requested:
        out["token_basis"] = (
            "provider" if isinstance(meta.get("raw_usage"), dict) else "unknown"
        )
    if resp is not None:
        for attr, key in (("tokens_in", "tokens_in"), ("tokens_out", "tokens_out")):
            value = getattr(resp, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[key] = value
    for key in ("cache_read_tokens", "cache_write_tokens"):
        if key in meta:
            value = meta.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                out[key] = value
    return out


def record_provider_stream_receipt(
    session: Any,
    resp: Any = None,
    *,
    provider_step: Any = 0,
    provider_attempt: Any = 0,
    status: Any = None,
    stream_performance: Any = None,
) -> None:
    """Copy one terminal snapshot into the durable sidecar. Never raises.

    Reads ``resp.meta['stream_performance']`` unless ``stream_performance``
    is supplied. Does not mutate ``resp.meta``, does not store the
    accumulator, and does not change ConvEvents, billing, prompt, retry,
    or response flow on sink failure. Missing session id or state_dir
    skips the write. ``resp`` may be omitted when ``status`` is given
    (invoked-dispatch error path).
    """
    try:
        sid = str(getattr(session, "harness_session_id", "") or "").strip()
        state_dir = str(getattr(session, "state_dir", "") or "").strip()
        if not sid or not state_dir:
            return
        if stream_performance is None:
            if resp is None and status is None:
                return
            meta = getattr(resp, "meta", None) if resp is not None else None
            raw_perf = None
            if isinstance(meta, dict):
                raw_perf = meta.get(STREAM_PERFORMANCE_KEY)
            perf = copy_stream_performance(raw_perf)
        else:
            perf = copy_stream_performance(stream_performance)
        if status is None:
            if resp is None:
                return
            status = classify_provider_receipt_status(resp)
        user_ordinal = None
        try:
            user_ordinal = session._current_user_ordinal()
        except Exception:
            user_ordinal = None
        turn_index = 0
        if user_ordinal is not None:
            try:
                turn_index = int(user_ordinal) + 1
            except (TypeError, ValueError):
                turn_index = 0
        driver = ""
        try:
            driver = str(getattr(getattr(session, "config", None), "driver", "") or "")
        except Exception:
            driver = ""
        receipt = build_receipt(
            session_id=sid,
            stream_performance=perf,
            turn_index=turn_index,
            user_ordinal=user_ordinal,
            provider_step=provider_step,
            provider_attempt=provider_attempt,
            driver=driver,
            model=_receipt_model_label(session, resp, driver),
            status=status,
            **_receipt_identity_kwargs(resp, driver),
        )
        StreamPerformanceReceiptStore(state_dir).record(sid, receipt)
    except Exception:
        return


def _snapshot_accumulator_for_receipt(accumulator: Any) -> Any:
    """Best-effort terminal snapshot. Empty when timing evidence is unavailable."""
    if accumulator is None:
        return {}
    try:
        finish = getattr(accumulator, "finish", None)
        if callable(finish):
            finish()
    except Exception:
        pass
    try:
        snap = accumulator.snapshot()
    except Exception:
        return {}
    return snap if isinstance(snap, dict) else {}


def record_provider_dispatch_error_receipt(
    session: Any,
    accumulator: Any = None,
    *,
    provider_step: Any = 0,
    provider_attempt: Any = 0,
) -> None:
    """One terminal status=error receipt after an invoked provider raise.

    Skips when the provider method was never invoked. Empty
    ``stream_performance`` is allowed; a safe accumulator snapshot is
    copied when available. Never raises. Does not meter or mutate
    prompt / history / display.
    """
    try:
        if not _provider_dispatch_was_invoked(session):
            return
        record_provider_stream_receipt(
            session,
            None,
            provider_step=provider_step,
            provider_attempt=provider_attempt,
            status="error",
            stream_performance=_snapshot_accumulator_for_receipt(accumulator),
        )
    except Exception:
        return


def account_provider_attempt(
    session: Any,
    resp: Any,
    prompt: str,
    *,
    provider_step: Any = 0,
    provider_attempt: Any = 0,
) -> None:
    """Persist the sidecar receipt, then meter.

    Receipt is written after dispatch has attached terminal timing and
    before ``meter_pilot_step`` can raise, so a malformed ``tokens_out``
    still produces exactly one receipt. Billing semantics are unchanged.
    """
    record_provider_stream_receipt(
        session, resp, provider_step=provider_step, provider_attempt=provider_attempt,
    )
    meter_pilot_step(session, resp, prompt)


def meter_pilot_step(
    session: Any,
    resp: Any,
    prompt: str,
) -> None:
    """Apply per-step token / cache / cost meters after a pilot transport call.

    Mechanical lift of the post-stream accounting block from
    ``_send_locked_inner`` — same counters, same provider-billed preference,
    same ``_session_cost`` fallback. Mutates ``session`` in place.

    Per-step ``stream_performance`` (when present) stays on ``resp.meta``;
    this function does not persist it onto the session.

    Anthropic may stamp ``meta.cache_write_ttl_basis`` as ``provider``,
    ``inferred``, or ``absent``. Aggregate ``cache_write_tokens`` are always
    measured when the provider reported them; 5m/1h TTL buckets are measured
    only for ``provider`` splits (or when a non-Anthropic driver omits TTL
    provenance and already emits provider-shaped buckets). Inferred TTL never
    enters measured session/provider TTL counters; fallback cost estimation
    then charges the aggregate write at the undifferentiated write multiplier.
    """
    # real token metering: prompt + completion (drivers report tokens_out;
    # estimate tokens_in from prompt length when not provided).
    # Preserve the effective total / fallback for compaction + UI meters, but
    # label whether tokens_in came from the provider or the prompt heuristic
    # so estimated input never masquerades as measured.
    try:
        _raw_out = getattr(resp, "tokens_out", 0)
        if isinstance(_raw_out, bool):
            _t_out = 0
        else:
            _t_out = int(_raw_out or 0)
    except (TypeError, ValueError, OverflowError):
        _t_out = 0
    try:
        _reported_in = int(getattr(resp, "tokens_in", 0) or 0)
    except (TypeError, ValueError):
        _reported_in = 0
    _tokens_in_basis = "provider" if _reported_in > 0 else "estimated"
    _t_in = _reported_in if _reported_in > 0 else int(len(prompt) // 4)
    session._tokens_used += _t_out + _t_in
    session._tokens_out += _t_out
    session._turn_output_tokens += _t_out
    session._tokens_in += _t_in
    session._last_tokens_in_basis = _tokens_in_basis
    # Lazy cumulative provenance splits — lightweight test sessions omit these.
    if _tokens_in_basis == "provider":
        session._tokens_in_measured = (
            int(getattr(session, "_tokens_in_measured", 0) or 0) + _t_in
        )
    else:
        session._tokens_in_estimated = (
            int(getattr(session, "_tokens_in_estimated", 0) or 0) + _t_in
        )
    try:
        _resp_meta = getattr(resp, "meta", None)
        if not isinstance(_resp_meta, dict):
            _resp_meta = {}
            resp.meta = _resp_meta
        _resp_meta["tokens_in_basis"] = _tokens_in_basis
    except Exception:
        pass
    # Remember this turn's REAL prompt size so the live context
    # estimate (compaction trigger + composer % meter) can prefer
    # the driver's actual number over the chars//4 heuristic.
    if _t_in > 0:
        session._last_prompt_tokens = _t_in
        try:
            session._last_prompt_heuristic = session._estimate_context_tokens_for_list(
                getattr(session, "_history", []) or []
            )
        except Exception:
            session._last_prompt_heuristic = 0
        # Schema-token EMA calibration (experiment-gated; telemetry-only).
        try:
            updater = getattr(session, "_maybe_update_schema_token_calibration", None)
            if callable(updater):
                updater(_t_in)
        except Exception:
            pass
    # Cache read/write credit: drivers report prompt-prefix cache
    # hits (and Anthropic/Bedrock writes) in meta. Reads save; writes
    # cost a premium -- both feed the same _session_cost formula.
    # TTL bucket provenance: only provider-reported (or provenance-absent
    # provider-shaped) 5m/1h splits count as measured; inferred Anthropic
    # splits stay out of measured-looking counters.
    try:
        _meta = getattr(resp, "meta", None) or {}
        _cache_delta = int(_meta.get("cache_read_tokens", 0) or 0)
        _write_delta = int(_meta.get("cache_write_tokens", 0) or 0)
        _write_5m_raw = int(_meta.get("cache_write_5m_tokens", 0) or 0)
        _write_1h_raw = int(_meta.get("cache_write_1h_tokens", 0) or 0)
        _ttl_basis = str(_meta.get("cache_write_ttl_basis") or "").strip().lower()
        if _ttl_basis == "inferred":
            _write_5m = 0
            _write_1h = 0
        else:
            # "provider", "absent", or missing (Bedrock/Cursor-shaped).
            _write_5m = _write_5m_raw
            _write_1h = _write_1h_raw
        session._tokens_cached += _cache_delta
        session._tokens_cache_write += _write_delta
        session._tokens_cache_write_5m += _write_5m
        session._tokens_cache_write_1h += _write_1h
        # Additive telemetry only — empty when the driver omitted provenance.
        session._last_cache_write_ttl_basis = _ttl_basis
    except Exception:
        _meta = {}
        _cache_delta = 0
        _write_delta = 0
        _write_5m = 0
        _write_1h = 0
    try:
        session._last_turn_cache_read_tokens = int(_cache_delta or 0)
    except Exception:
        pass
    if _cache_delta > 0:
        # Standing-economics warm window: refresh only on explicit cache reads.
        try:
            import time as _time_mod

            session._last_prompt_cache_activity_at = _time_mod.time()
        except Exception:
            pass
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
    # Explicit OpenRouter unknown rates: keep provenance, price catalog path
    # at $0 (provider billed path below still wins when present).
    if _price_in is None or _price_out is None:
        _price_in, _price_out = 0.0, 0.0
        if not session._price_source:
            session._price_source = "unknown"
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
                # Only provider-reported input belongs in the billed-in
                # denominator. Prompt-length fallback must not inflate it when
                # usage.cost arrived without input tokens.
                if _tokens_in_basis == "provider":
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
            # When TTL basis is inferred, _write_5m/_write_1h are already
            # zeroed above so aggregate cache_write bills at the
            # undifferentiated write multiplier — never as provider TTL fact.
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
        flush = getattr(session, "_flush_stop_boundary_notices", None)
        if callable(flush):
            yield from flush()
        else:
            flush_steer = getattr(session, "_flush_steer_drop_notice", None)
            if callable(flush_steer):
                yield from flush_steer()
        return ("return", user_message)

    pending_steers = session.drain_steer()
    if pending_steers:
        format_steer = getattr(
            session, "_format_steer_user_content", None
        )
        for steer in pending_steers:
            yield ConvEvent("steer", {"text": steer})
            if callable(format_steer):
                content = format_steer(steer)
            else:
                # Compatibility for hosts that still expose the legacy marker.
                marker = getattr(session, "_steer_marker", None)
                content = marker(steer) if callable(marker) else steer
            session._history.append({"role": "user", "content": content})
        session._steer_pending = False
        return ("continue", user_message)
    # Steer took priority above; only if no steer was pending do we
    # look at the PROMPT QUEUE ("playlist"). A queued prompt runs as
    # a genuine next-turn user message — same first-class user shape as
    # a steer at the idle boundary — so it flows through the pilot as
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
            from .vision import (
                native_multimodal_user_content,
                pilot_supports_native_images,
                resolve_provider_for_spec,
                transcribe_images,
            )
            provider = resolve_provider_for_spec(
                getattr(getattr(session, "config", None), "driver", "") or ""
            )
            pilot = getattr(session, "pilot", None)
            pilot_model = str(getattr(pilot, "model", "") or "")
            try:
                if pilot_supports_native_images(
                    provider, model=pilot_model, pilot=pilot,
                ):
                    yield ConvEvent("vision", {
                        "count": len(q_images), "status": "native",
                    })
                    content = native_multimodal_user_content(q_text, q_images)
                    for path in q_images:
                        yield ConvEvent("vision", {
                            "path": path, "status": "native",
                        })
                else:
                    yield ConvEvent("vision", {
                        "count": len(q_images), "status": "transcribing",
                    })
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
                        content = (
                            "The user attached image(s). Transcription(s) below "
                            "(you cannot see the image, only this text):\n\n"
                            + "\n\n".join(blocks) + "\n\n---\n" + q_text
                        )
                    else:
                        err = (
                            f"All {len(q_images)} image transcription(s) failed; "
                            "cannot answer an image request as text-only."
                        )
                        yield ConvEvent("error", {"error": err})
                        return ("return", user_message)
            except Exception as e:
                yield ConvEvent("error", {
                    "error": f"Failed to load attached image(s): {e}",
                })
                return ("return", user_message)
        session._history.append({"role": "user", "content": content})
        # Refresh the "current user message" reference so downstream
        # per-turn hooks (compaction, ingest, budget) attribute work
        # to the newly-running queued prompt instead of the previous
        # completed one.
        return ("continue", q_text)
    # Soft verify: remind MICRO/STANDARD file edits that skipped a command.
    if (yield from maybe_soft_verify_nudge(session)):
        return ("continue", user_message)
    # assistant_done first; ingest in housekeeping so the busy lock
    # releases immediately. Sync wiki I/O here used to leave the
    # final answer painted while Stop/Still working stayed up
    # (content sat in the Investigating fold until Stop flushed it).
    yield from finalize_assistant_turn(
        session,
        user_message=user_message,
        step=step,
        swarms=swarms,
        turn_prose=turn_prose,
        turn_findings=turn_findings,
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
            maybe_refresh_workspace_rules(session, act.path)
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

        if ok and status == "native_image":
            # Tool output stays a string (Codex stringifies it). Pixels ride a
            # follow-on user message so Responses/chat drivers see input_image.
            from .vision import native_multimodal_user_content
            note = (
                f"(view_image {act.path}): native pixels attached for vision pilot."
            )
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["image"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "image", "headline": f"Viewed image {act.path}"}],
            })
            session._append_action_result(act, aid, note, is_native)
            try:
                session._history.append({
                    "role": "user",
                    "content": native_multimodal_user_content(
                        f"[native vision: contents of {act.path}]",
                        [val],
                    ),
                })
            except Exception as e:
                yield ConvEvent("action_result", {
                    "id": aid, "error": f"native image attach failed: {e}",
                })
        elif ok:
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
            ok, status, val = invoke_do(session, act, lambda: session._do_web_search(act))

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
            ok, status, val = invoke_do(session, act, lambda: session._do_web_fetch(act))

        if ok:
            result_text = val
            display_url = sanitize_url_for_display(act.url)
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["web_fetch"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "web_fetch", "headline": f"Fetched {display_url}"}],
            })
            session._append_action_result(act, aid, f"(web_fetch '{display_url}' returned)\n{result_text}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            display_url = sanitize_url_for_display(act.url)
            session._append_action_result(act, aid, f"(web_fetch '{display_url}' failed: {val})", is_native)
        return

    if act.kind == "read_pdf":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = invoke_do(session, act, lambda: session._do_read_pdf(act))

        if ok:
            result_text = val
            pdf_target = act.path or act.url
            display_pdf = (
                sanitize_url_for_display(str(pdf_target))
                if pdf_target and str(pdf_target).startswith(("http://", "https://"))
                else pdf_target
            )
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["read_pdf"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "read_pdf", "headline": f"Read PDF from {display_pdf}"}],
            })
            session._append_action_result(act, aid, f"(read_pdf '{display_pdf}' returned)\n{result_text}", is_native)
        else:
            if status == "repo_not_open":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_pdf {aid} failed: {val})", is_native)
            elif status == "path_traversal":
                yield ConvEvent("action_result", {"id": aid, "error": val})
                session._append_action_result(act, aid, f"(read_pdf {aid} failed: {val})", is_native)
            else:  # status == "exception"
                yield ConvEvent("action_result", {"id": aid, "error": val})
                pdf_target = act.path or act.url
                display_pdf = (
                    sanitize_url_for_display(str(pdf_target))
                    if pdf_target and str(pdf_target).startswith(("http://", "https://"))
                    else pdf_target
                )
                session._append_action_result(act, aid, f"(read_pdf '{display_pdf}' failed: {val})", is_native)
        return

    if act.kind == "search_codegraph":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = invoke_do(session, act, lambda: session._do_search_codegraph(act))

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
            ok, status, val = invoke_do(session, act, lambda: session._do_search_files(act))

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

    if act.kind == "peek_history":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_peek_history(act)
        if ok:
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["peek_history"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "peek_history", "headline": "peek_history"}],
            })
            session._append_action_result(act, aid, f"(peek_history returned)\n{val}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(peek_history failed: {val})", is_native)
        return

    if act.kind == "peek_artifact":
        if idx in prefetch:
            ok, status, val = prefetch[idx]
        else:
            ok, status, val = session._do_peek_artifact(act)
        if ok:
            uri = (act.path or act.url or (act.arguments or {}).get("uri") or "artifact")
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["peek_artifact"], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": "peek_artifact", "headline": f"peek_artifact {uri}"}],
            })
            session._append_action_result(act, aid, f"(peek_artifact returned)\n{val}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"(peek_artifact failed: {val})", is_native)
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
            _note_turn_command(session, "fail")
            feedback = (
                "[auto-verify] The project check failed after your edits:\n"
                f"$ {_verify_display}\n{output}\n"
                "Fix the issue, then continue."
            )
            session._history.append({"role": "user", "content": feedback})
            return (auto_verify_iters, True)
        _note_turn_command(session, "pass")
    return (auto_verify_iters, False)


def dispatch_local_action(
    session: Any,
    act: PilotAction,
    aid: str,
    is_native: bool,
    turn_changed_files: list,
    act_goal: Any = None,
    plan: bool = False,
) -> Iterator[Any]:
    """Assemble tool-results for LOCAL_ACTION_KINDS (workspace / mutate / browse / mcp).

    Mechanical lift of the per-kind local branches from ``_send_locked_inner``
    (everything after read-only dispatch and before ``send_loop_dispatch``).
    Caller must gate on ``act.kind in LOCAL_ACTION_KINDS``. Yields the same
    ConvEvent shapes and history appends; mutates ``turn_changed_files`` on
    successful writes/edits.

    ``plan=True`` is a second gate for PLAN_SKIP_KINDS (call_mcp / manage_mcp /
    browser_* / write-edit / run_command) so a caller that forgets the
    actions-layer skip still cannot mutate or drive a live browser in plan mode.
    """
    import os

    from .conversation import ConvEvent, _mcp_result_text
    from .tool_dispatch import is_safe_path

    if act_goal is None:
        act_goal = action_display_goal(act)

    if act.kind == "wait":
        from .pilot_wait import dispatch_wait_action
        yield from dispatch_wait_action(session, act, aid, is_native)
        return

    if plan and (
        act.kind in PLAN_SKIP_KINDS or act.kind.startswith("browser_")
    ):
        yield ConvEvent("action_result", {
            "id": aid,
            "kind": act.kind,
            "goal": act_goal or act.tool,
            "error": f"(plan mode: skipped {act.kind})",
        })
        session._append_action_result(
            act, aid, f"(plan mode: skipped {act.kind})", is_native,
        )
        return

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
            try:
                from harness.swarm_adapter import ensure_repo_swarm_adapter
                ensure_repo_swarm_adapter(session.config)
                ensure_repo_swarm_adapter(_cfg)
            except Exception:
                pass
            _record_recent_workspace(target_repo)
        except Exception:
            session.config.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo
            try:
                from harness.swarm_adapter import ensure_repo_swarm_adapter
                ensure_repo_swarm_adapter(session.config)
            except Exception:
                pass

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
                    user_ordinal=session._current_user_ordinal(),
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
                # Path lets the UI refresh open editors / Files / SCM even when
                # the pre-write checkpoint SSE was skipped or failed.
                "path": act.path,
                "artifacts": [{"type": "file", "headline": f"Wrote {bytes_written} bytes to {act.path}"}],
            })
            session._append_action_result(act, aid, f"(write_file {act.path} successfully wrote {bytes_written} bytes)", is_native)
            maybe_refresh_workspace_rules(session, act.path)
            turn_changed_files.append(target_path)
            yield from _yield_task_profile_escalation(session, turn_changed_files)
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
                    user_ordinal=session._current_user_ordinal(),
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
                "path": act.path,
                "artifacts": [{"type": "file", "headline": headline}],
            })
            session._append_action_result(act, aid, f"(edit_file {act.path} successfully edited: {headline})", is_native)
            maybe_refresh_workspace_rules(session, act.path)
            turn_changed_files.append(target_path)
            yield from _yield_task_profile_escalation(session, turn_changed_files)
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
                    user_ordinal=session._current_user_ordinal(),
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
                "path": act.path,
                "artifacts": [{"type": "file", "headline": headline}],
            }
            # AST preview (round 6, opt-in): structural diff
            # computed by _do_hash_edit on the write pass.
            ast_preview = getattr(session, "_last_ast_preview", None)
            if ast_preview and ast_preview.get("available"):
                hash_edit_result["ast_preview"] = ast_preview
            session._last_ast_preview = None
            yield ConvEvent("action_result", hash_edit_result)
            session._append_action_result(act, aid, f"(hash_edit {act.path} successfully applied: {headline})", is_native)
            maybe_refresh_workspace_rules(session, act.path)
            turn_changed_files.append(target_path)
            yield from _yield_task_profile_escalation(session, turn_changed_files)
        except Exception as e:
            yield ConvEvent("action_result", {"id": aid, "error": str(e)})
            session._append_action_result(act, aid, f"(hash_edit {act.path} failed: {e})", is_native)
        return
    # ---- run_command branch ---------------------------------------
    if act.kind == "run_command":
        command = act.command or ""
        if not session.config.repo:
            error_msg = "No workspace directory (config.repo) is open."
            yield ConvEvent("action_result", {
                "id": aid, "error": error_msg, "kind": "run_command", "command": command,
            })
            session._append_action_result(act, aid, f"(run_command {aid} failed: {error_msg})", is_native)
            return
        # Wave 2: explicit background mode returns a durable pending receipt
        # and never holds the turn on run_cancellable. Opt-in only.
        from harness.command_jobs import (
            is_background_run_command,
            secret_free_command_preview,
            start_background_run_command,
        )
        if is_background_run_command(act):
            try:
                receipt = start_background_run_command(session, act, aid)
            except Exception as exc:
                yield ConvEvent("action_result", {
                    "id": aid,
                    "error": str(exc),
                    "kind": "run_command",
                    "command": secret_free_command_preview(command),
                    "status": "error",
                })
                session._append_action_result(
                    act, aid, f"(run_command {aid} background failed: {exc})",
                    is_native, ok=False,
                )
                return
            result = {
                "id": aid,
                "kind": "run_command",
                "status": receipt.get("status") or "pending",
                "job_id": receipt.get("job_id"),
                "session_id": receipt.get("session_id"),
                "action_id": receipt.get("action_id") or aid,
                "command_fingerprint": receipt.get("command_fingerprint"),
                "command_preview": receipt.get("command_preview"),
                "cwd": receipt.get("cwd"),
                "started_at": receipt.get("started_at"),
                "terminal_receipt": receipt.get("terminal_receipt"),
                "message": receipt.get("message") or "",
                "goal": receipt.get("command_preview") or "",
                "num": 1,
                "types": ["command"],
                "adapter": "command",
                "mode": "background",
                "source": receipt.get("source") or "harness",
                "accounting_owned": bool(receipt.get("accounting_owned", True)),
                "accounting_scope": receipt.get("accounting_scope") or "marionette",
                "artifacts": [{
                    "type": "command",
                    "headline": (
                        f"background pending · job {receipt.get('job_id')}"
                    ),
                }],
            }
            yield ConvEvent("action_result", result)
            session._append_action_result(
                act,
                aid,
                (
                    f"(run_command '{secret_free_command_preview(command)}' "
                    f"dispatched in background: job {receipt.get('job_id')})"
                ),
                is_native,
            )
            _note_turn_command(session)
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
                    command=command,
                    command_hash=command_hash,
                    action_id=aid,
                    category=block.get("category") or "",
                    reason=block.get("reason") or "",
                    matched=block.get("matched") or "",
                )
                yield ConvEvent("command_approval_pending", {
                    "id": aid,
                    "command": command,
                    "command_hash": command_hash,
                    "session_id": pending.get("session_id"),
                    "workspace_root": pending.get("workspace_root"),
                    "category": pending.get("category") or block.get("category"),
                    "reason": pending.get("reason") or block.get("reason"),
                    "matched": pending.get("matched") or block.get("matched"),
                })
                # Pair action_start so turn-end settle cannot invent opaque
                # "missing action_result" while the approval card is open.
                yield ConvEvent("action_result", {
                    "id": aid,
                    "kind": "run_command",
                    "command": command,
                    "status": "pending_approval",
                    "message": block_msg,
                    "cwd": block.get("cwd") or session.config.repo,
                    # Recovery handle only: the command was not run or saved.
                    "retry_handle": block.get("retry_handle") or command_hash,
                    "command_preview": block.get("command_preview") or "",
                    "recovery": block.get("recovery") or "",
                })
                session._append_action_result(act, aid, f"(run_command {aid} {block_msg})", is_native)
                return
            # cancelled / timeout / error: preserve partial output + exit_code.
            if status in ("cancelled", "timeout", "error") and isinstance(val, dict):
                output = val.get("output") or ""
                try:
                    exit_code = int(val.get("exit_code"))
                except (TypeError, ValueError):
                    exit_code = -1
                run_status = str(val.get("status") or status)
                ui_output = _truncate_run_command_ui_output(output)
                headline = _run_command_artifact_headline(
                    exit_code, ui_output, status=run_status,
                )
                result = {
                    "id": aid,
                    "kind": "run_command",
                    "goal": command,
                    "command": command,
                    "exit_code": exit_code,
                    "output": ui_output,
                    "status": run_status,
                    "cwd": val.get("cwd") or session.config.repo,
                    "num": 1,
                    "types": ["command"],
                    "adapter": "local",
                    "mode": "tool",
                    "artifacts": [{"type": "command", "headline": headline}],
                }
                for key in ("hint", "spill_uri", "output_spilled", "output_chars"):
                    if val.get(key) not in (None, ""):
                        result[key] = val[key]
                # timeout/error also set error so cards stay expanded; cancelled
                # relies on status so the UI can map it to an interrupted outcome.
                if run_status in ("timeout", "error"):
                    result["error"] = run_status
                yield ConvEvent("action_result", result)
                session._append_action_result(
                    act,
                    aid,
                    _with_command_footer(
                        f"(run_command '{command}' {run_status} with exit code {exit_code})\n{output}",
                        val,
                    ),
                    is_native,
                    ok=False,
                )
                _note_turn_command(session)
                return
            yield ConvEvent("action_result", {
                "id": aid, "error": val, "kind": "run_command", "command": command,
            })
            session._append_action_result(act, aid, f"(run_command {aid} failed: {val})", is_native)
            return
        output = val["output"]
        exit_code = val["exit_code"]
        run_status = str(val.get("status") or "ok")
        if run_status in ("success", ""):
            run_status = "ok"
        ui_output = _truncate_run_command_ui_output(output)
        headline = _run_command_artifact_headline(
            exit_code, ui_output, status=run_status,
        )
        result = {
            "id": aid,
            "kind": "run_command",
            "goal": command,
            "command": command,
            "exit_code": exit_code,
            "output": ui_output,
            "status": run_status,
            "cwd": val.get("cwd") or session.config.repo,
            "num": 1,
            "types": ["command"],
            "adapter": "local",
            "mode": "tool",
            "artifacts": [{"type": "command", "headline": headline}],
        }
        for key in ("hint", "spill_uri", "output_spilled", "output_chars"):
            if val.get(key) not in (None, ""):
                result[key] = val[key]
        yield ConvEvent("action_result", result)
        if run_status == "ok":
            hist = (
                f"(run_command '{command}' completed with exit code {exit_code})\n"
                f"{output}"
            )
        else:
            # e.g. truncated: still a finished process, but not a clean ok.
            hist = (
                f"(run_command '{command}' {run_status} with exit code {exit_code})\n"
                f"{output}"
            )
        session._append_action_result(
            act, aid, _with_command_footer(hist, val), is_native,
        )
        _note_turn_command(session, "pass" if run_status == "ok" else run_status)
        return
    # ---- run_command_batch branch (Wave 3) -------------------------
    if act.kind == "run_command_batch":
        from harness.command_batches import (
            COMMAND_BATCH_ADAPTER,
            COMMAND_BATCH_KIND,
            start_command_batch,
        )
        if not session.config.repo:
            error_msg = "No workspace directory (config.repo) is open."
            yield ConvEvent("action_result", {
                "id": aid,
                "error": error_msg,
                "kind": COMMAND_BATCH_KIND,
            })
            session._append_action_result(
                act, aid, f"(run_command_batch {aid} failed: {error_msg})",
                is_native, ok=False,
            )
            return
        try:
            receipt = start_command_batch(
                session,
                list(getattr(act, "commands", None) or []),
                aid,
                max_concurrency=int(getattr(act, "max_concurrency", 0) or 0) or None,
            )
        except Exception as exc:
            yield ConvEvent("action_result", {
                "id": aid,
                "error": str(exc),
                "kind": COMMAND_BATCH_KIND,
                "status": "error",
            })
            session._append_action_result(
                act, aid, f"(run_command_batch {aid} failed: {exc})",
                is_native, ok=False,
            )
            return
        result = {
            "id": aid,
            "kind": COMMAND_BATCH_KIND,
            "status": receipt.get("status") or "pending",
            "job_id": receipt.get("job_id") or receipt.get("batch_id"),
            "batch_id": receipt.get("batch_id") or receipt.get("job_id"),
            "session_id": receipt.get("session_id"),
            "action_id": receipt.get("action_id") or aid,
            "child_job_ids": receipt.get("child_job_ids") or [],
            "children": receipt.get("children") or [],
            "child_count": receipt.get("child_count") or 0,
            "max_concurrency": receipt.get("max_concurrency") or 0,
            "mixed_terminal": bool(receipt.get("mixed_terminal")),
            "cwd": receipt.get("cwd"),
            "started_at": receipt.get("started_at"),
            "terminal_receipt": receipt.get("terminal_receipt"),
            "message": receipt.get("message") or "",
            "goal": f"command batch ({receipt.get('child_count') or 0} commands)",
            "num": int(receipt.get("child_count") or 0) or 1,
            "types": ["command_batch"],
            "adapter": COMMAND_BATCH_ADAPTER,
            "role": COMMAND_BATCH_ADAPTER,
            "mode": "batch",
            "source": receipt.get("source") or "harness",
            "accounting_owned": bool(receipt.get("accounting_owned", True)),
            "accounting_scope": receipt.get("accounting_scope") or "marionette",
            "replayed": bool(receipt.get("replayed")),
            "artifacts": [{
                "type": "command_batch",
                "headline": (
                    f"batch pending · job {receipt.get('job_id')} · "
                    f"{receipt.get('child_count') or 0} children"
                ),
            }],
        }
        yield ConvEvent("action_result", result)
        session._append_action_result(
            act,
            aid,
            (
                f"(run_command_batch dispatched: job {receipt.get('job_id')} "
                f"children={receipt.get('child_count') or 0})"
            ),
            is_native,
        )
        _note_turn_command(session)
        return
    # ---- run_ipython branch (persistent session kernel) ------------
    if act.kind == "run_ipython":
        ok, status, val = session._do_run_ipython(act)
        if ok:
            output = ""
            backend = "stdlib"
            cwd = ""
            if isinstance(val, dict):
                output = str(val.get("output") or "")
                backend = str(val.get("backend") or backend)
                cwd = str(val.get("cwd") or "")
            else:
                output = str(val)
            headline = f"ipython ({backend})"
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": ["ipython"], "adapter": "local",
                "mode": "tool", "kind": "run_ipython",
                "artifacts": [{"type": "ipython", "headline": headline}],
                "output": output[:4096],
                "backend": backend,
                "cwd": cwd,
            })
            session._append_action_result(
                act, aid,
                f"(run_ipython [{backend}] ok)\n{output}",
                is_native,
            )
            _note_turn_command(session)
        else:
            err = val
            if isinstance(val, dict):
                err = val.get("error") or val.get("output") or val
            yield ConvEvent("action_result", {
                "id": aid, "error": err, "kind": "run_ipython", "status": status,
            })
            session._append_action_result(
                act, aid, f"(run_ipython {status}: {err})", is_native, ok=False,
            )
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
    # ---- session scratch bindings ----------------------------------
    if act.kind in ("store_scratch", "load_scratch", "list_scratch", "clear_scratch"):
        handler = {
            "store_scratch": session._do_store_scratch,
            "load_scratch": session._do_load_scratch,
            "list_scratch": session._do_list_scratch,
            "clear_scratch": session._do_clear_scratch,
        }[act.kind]
        try:
            ok, status, val = handler(act)
        except Exception as exc:
            ok, status, val = False, "exception", str(exc)
        if ok:
            yield ConvEvent("action_result", {
                "id": aid, "num": 1, "types": [act.kind], "adapter": "local", "mode": "tool",
                "artifacts": [{"type": act.kind, "headline": act.kind}],
            })
            session._append_action_result(act, aid, f"({act.kind} returned)\n{val}", is_native)
        else:
            yield ConvEvent("action_result", {"id": aid, "error": val})
            session._append_action_result(act, aid, f"({act.kind} failed: {val})", is_native)
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
            res, timed_out = run_with_tool_deadline(
                session, "query_wiki", lambda: session._wiki.query(question),
            )
            if timed_out:
                raise TimeoutError("tool call timed out after %dms" % timed_out)
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
        from .send_loop_dispatch import (
            _TRACKABLE_SWARM_REFUSAL,
            is_untracked_pm_start_tool,
        )
        if is_untracked_pm_start_tool(str(act.tool or "")):
            yield ConvEvent("action_result", {
                "id": aid,
                "error": _TRACKABLE_SWARM_REFUSAL,
            })
            session._append_action_result(
                act, aid, f"(mcp {act.tool} refused: untracked start_*)", is_native,
            )
            return
        try:
            if act.tool:
                session._tool_catalog.activate([act.tool])
            out, timed_out = run_with_tool_deadline(
                session, "call_mcp", lambda: session._mcp.call(act.tool, act.arguments),
            )
            if timed_out:
                raise TimeoutError("tool call timed out after %dms" % timed_out)
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
        from .mcp_manager import redact_mcp_secrets
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
            # Never echo mcp.json env/headers secrets into the transcript.
            text = _json_mcp.dumps(redact_mcp_secrets(out), indent=2)[:4000]
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
