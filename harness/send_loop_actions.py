from __future__ import annotations

"""Action-spree execution peeled from ``_send_locked_inner``.

Owns guard setup / prefetch planning / advisor / per-action plan+delegation
gates / pilot-guard suppress+replay / and the thin dispatch fan-out into
read-only, local, and delegate helpers. Same ConvEvent shapes, same history
appends, same steer-abandon and cancel early-exit behavior.

Public orchestration stays on ``SendLoopMixin``; this helper takes an explicit
``session`` plus the small counters the kernel owns.
"""

from typing import Any, Iterator

from .diag import note as _diag_note
from .pilot import is_invalid_action
from .pilot_guards import (
    check_backend_restart,
    check_cli_redirect,
    check_pilot_guards,
    cli_redirect_enabled,
    dedupe_dispatch_actions,
    guards_active,
    new_turn_guard_state,
    record_action_execution,
)
from .send_loop_dispatch import (
    DISPATCH_ACTION_KINDS,
    dispatch_implement_action,
    dispatch_memory_action,
    dispatch_parallel_action,
    dispatch_route_task_action,
    dispatch_swarm_action,
)
from .send_loop_phases import (
    LOCAL_ACTION_KINDS,
    READ_ONLY_KINDS,
    action_display_goal,
    dispatch_local_action,
    dispatch_readonly_action,
    run_parallel_prefetch,
)


def execute_turn_actions(
    session: Any,
    *,
    turn: Any,
    user_message: str,
    is_native: bool,
    plan: bool,
    counters: dict,
    step: int,
    turn_findings: list,
) -> Iterator[Any]:
    """Execute one pilot turn's action list (peeled from ``_send_locked_inner``).

    Mutates ``counters`` in place for ``action_seq`` / ``swarms`` / ``demo_swarms``.
    Yields the same ConvEvent stream as the former inline block.

    Generator return value is ``(disposition, turn_changed_files)`` where
    disposition is ``None`` (continue to auto-verify) or ``"return"`` (close
    the turn / exit send). Mid-spree steer abandons remaining actions and
    returns ``None`` so the step loop re-asks the model.
    """
    from .conversation import ConvEvent

    action_seq = counters["action_seq"]
    swarms = counters["swarms"]
    demo_swarms = counters["demo_swarms"]

    # 4. Execute each action as a collapsible tool-call.
    prior_guard = getattr(session, "_turn_guard_state", None)
    guard_state = new_turn_guard_state(user_message)
    # Carry swarm-gate redirect progress across model steps in this send()
    # so broad-intent turns cannot re-burn a full SUPPRESSED payload every
    # step before the model finally dispatches run_swarm.
    if prior_guard is not None:
        guard_state.swarm_gate_suppress_count = getattr(
            prior_guard, "swarm_gate_suppress_count", 0
        )
    session._turn_guard_state = guard_state
    guard_suppressed: dict[int, Any] = {}
    guard_recorded_indices: set[int] = set()
    prefetch = {}
    read_actions_with_idx = []
    prefetch_targets = []
    for idx, act in enumerate(turn.actions):
        if act.kind in READ_ONLY_KINDS:
            read_actions_with_idx.append((idx, act))
            if guards_active():
                guard_verdict = check_pilot_guards(guard_state, act.kind, act)
                if guard_verdict.suppress:
                    if getattr(guard_verdict, "replay", False):
                        guard_suppressed[idx] = guard_verdict
                        # Replay still counts toward the loop-repeat cap.
                        try:
                            record_action_execution(guard_state, act.kind, act)
                        except Exception:
                            pass
                        continue
                    # Defer hard loop-suppress to execution time so an
                    # earlier identical action in this turn can populate
                    # the successful-result cache for replay. Other
                    # guards (swarm_gate, delegate, budget) still apply
                    # immediately.
                    if getattr(guard_verdict, "reason", "") == "loop":
                        continue
                    guard_suppressed[idx] = guard_verdict
                    continue
                record_action_execution(guard_state, act.kind, act)
                guard_recorded_indices.add(idx)
            prefetch_targets.append((idx, act))

    if len(prefetch_targets) >= 2 and not session._cancel.is_set():
        prefetch = run_parallel_prefetch(session, prefetch_targets)

    # Advisor pass (round 6, opt-in): one read-only review of this
    # turn's pending action list. Warnings are attached to the first
    # action_result of the turn in send(); execution never blocks.
    try:
        from .advisor import advise, advisor_enabled

        if turn.actions and advisor_enabled():
            session._pending_advisor_warnings = advise(
                turn.actions, session.config.repo or "", session.pilot
            )
    except Exception:
        session._pending_advisor_warnings = []

    history_len_before_actions = len(session._history)
    # Track files edited THIS turn (for the auto-verify loop below).
    turn_changed_files: list[str] = []
    # Bulletproof same-turn dedupe: twin run_implement tool_calls with
    # near-identical goals never both reach dispatch.
    turn.actions = dedupe_dispatch_actions(turn.actions)
    for idx, act in enumerate(turn.actions):
        if idx > 0:
            # Cancel before steer inject so a Stop mid-spree never delivers
            # queued steers into an abandoned generator (S2 boundary).
            if session._cancel.is_set():
                session._sanitize_tool_pairs()
                flush = getattr(session, "_flush_steer_drop_notice", None)
                if callable(flush):
                    yield from flush()
                yield ConvEvent("interrupted", {"reason": "session interrupted"})
                counters["action_seq"] = action_seq
                counters["swarms"] = swarms
                counters["demo_swarms"] = demo_swarms
                return ("return", turn_changed_files)
            yield from session._check_and_inject_steer()
            if session._steer_pending:
                # A user steer arrived mid-spree. Abandon the REMAINING queued
                # actions and loop back to re-ask the model, which now sees the
                # steer as its current instruction. Heal unanswered sibling
                # tool_calls at this tool-batch boundary BEFORE the next
                # provider request (same seam as cancel) so steer never leaves
                # dangling tool_use ids.
                session._sanitize_tool_pairs()
                break
        if session._cancel.is_set():
            # Heal unanswered sibling tool_calls before abandoning the spree.
            session._sanitize_tool_pairs()
            flush = getattr(session, "_flush_steer_drop_notice", None)
            if callable(flush):
                yield from flush()
            yield ConvEvent("interrupted", {"reason": "session interrupted"})
            counters["action_seq"] = action_seq
            counters["swarms"] = swarms
            counters["demo_swarms"] = demo_swarms
            return ("return", turn_changed_files)
        action_seq += 1
        aid = f"a{action_seq}"
        # Malformed/truncated tool call: do NOT silently drop it. Surface the error
        # back to the model so it re-issues the call with all required arguments, and
        # count it as activity so the autonomous loop does not mistake it for "done".
        if is_invalid_action(act):
            err = act.content or f"invalid tool call '{act.tool}'"
            yield ConvEvent("action_result", {"id": aid, "error": err})
            session._append_action_result(act, aid, err, is_native)
            turn_had_invalid = True  # noqa: F841 — preserved from former inline block
            continue
        act_goal = action_display_goal(act)

        # run_implement / run_parallel emit their own action_start after
        # engine selection (includes mode=agentic|native). Emitting here
        # too produced twin "Investigated 2 run implements" chrome.
        if act.kind not in ("run_implement", "run_parallel"):
            yield ConvEvent("action_start", {
                "id": aid, "kind": act.kind, "goal": act_goal or act.tool,
                "cwd": session.config.repo or None,
                "adapter": session.config.swarm_adapter,
            })

        if plan and act.kind in ("run_implement", "run_parallel", "write_file", "edit_file", "hash_edit", "run_command"):
            if act.kind in ("run_implement", "run_parallel"):
                yield ConvEvent("action_start", {
                    "id": aid, "kind": act.kind, "goal": act_goal or act.tool,
                    "cwd": session.config.repo or None,
                })
            yield ConvEvent("action_result", {
                "id": aid,
                "error": f"(plan mode: skipped {act.kind})"
            })
            session._append_action_result(act, aid, f"(plan mode: skipped {act.kind})", is_native)
            continue

        if getattr(session.config, "no_delegation", False) and act.kind in ("run_implement", "run_parallel", "run_swarm"):
            if act.kind in ("run_implement", "run_parallel"):
                yield ConvEvent("action_start", {
                    "id": aid, "kind": act.kind, "goal": act_goal or act.tool,
                    "cwd": session.config.repo or None,
                })
            err_msg = "delegation is disabled for workers; edit the files directly with write_file, edit_file, or hash_edit"
            yield ConvEvent("action_result", {
                "id": aid,
                "error": err_msg
            })
            session._append_action_result(act, aid, err_msg, is_native)
            continue

        # Never let the pilot POST /api/restart mid-turn (drops SSE).
        if act.kind == "run_command":
            restart_verdict = check_backend_restart(guard_state, act.kind, act)
            if restart_verdict.suppress:
                _diag_note(
                    "pilot_guards",
                    msg=f"{restart_verdict.reason} suppressed {act.kind}: {restart_verdict.message[:200]}",
                )
                yield ConvEvent("action_result", {"id": aid, "error": restart_verdict.message})
                session._append_action_result(act, aid, restart_verdict.message, is_native, ok=False)
                continue

        # Kernel-force native Puppetmaster verbs: CLI redirect runs every turn
        # (independent of broad-intent gate and other guard kill switches).
        if act.kind == "run_command" and cli_redirect_enabled():
            cli_verdict = check_cli_redirect(guard_state, act.kind, act)
            if cli_verdict.suppress:
                _diag_note(
                    "pilot_guards",
                    msg=f"{cli_verdict.reason} suppressed {act.kind}: {cli_verdict.message[:200]}",
                )
                yield ConvEvent("action_result", {"id": aid, "error": cli_verdict.message})
                session._append_action_result(act, aid, cli_verdict.message, is_native, ok=False)
                continue

        if guards_active():
            if idx in guard_suppressed:
                guard_verdict = guard_suppressed[idx]
                if getattr(guard_verdict, "replay", False):
                    _diag_note(
                        "pilot_guards",
                        msg=f"{guard_verdict.reason} replayed {act.kind}",
                    )
                    _replay_headline = (
                        "swarm gate redirect already issued"
                        if getattr(guard_verdict, "reason", "") == "swarm_gate_replay"
                        else "cached repeat of identical call"
                    )
                    yield ConvEvent("action_result", {
                        "id": aid,
                        "num": 1,
                        "types": ["cached"],
                        "adapter": "local",
                        "mode": "tool",
                        "artifacts": [{"type": "cached", "headline": _replay_headline}],
                    })
                    session._append_action_result(act, aid, guard_verdict.message, is_native, ok=True)
                    continue
                _diag_note(
                    "pilot_guards",
                    msg=f"{guard_verdict.reason} suppressed {act.kind}: {guard_verdict.message[:200]}",
                )
                yield ConvEvent("action_result", {"id": aid, "error": guard_verdict.message})
                session._append_action_result(act, aid, guard_verdict.message, is_native, ok=False)
                continue
            if idx not in guard_recorded_indices:
                guard_verdict = check_pilot_guards(guard_state, act.kind, act)
                if guard_verdict.suppress:
                    if getattr(guard_verdict, "replay", False):
                        try:
                            record_action_execution(guard_state, act.kind, act)
                        except Exception:
                            pass
                        _diag_note(
                            "pilot_guards",
                            msg=f"{guard_verdict.reason} replayed {act.kind}",
                        )
                        _replay_headline = (
                            "swarm gate redirect already issued"
                            if getattr(guard_verdict, "reason", "") == "swarm_gate_replay"
                            else "cached repeat of identical call"
                        )
                        yield ConvEvent("action_result", {
                            "id": aid,
                            "num": 1,
                            "types": ["cached"],
                            "adapter": "local",
                            "mode": "tool",
                            "artifacts": [{"type": "cached", "headline": _replay_headline}],
                        })
                        session._append_action_result(act, aid, guard_verdict.message, is_native, ok=True)
                        continue
                    _diag_note(
                        "pilot_guards",
                        msg=f"{guard_verdict.reason} suppressed {act.kind}: {guard_verdict.message[:200]}",
                    )
                    yield ConvEvent("action_result", {"id": aid, "error": guard_verdict.message})
                    session._append_action_result(act, aid, guard_verdict.message, is_native, ok=False)
                    continue
                record_action_execution(guard_state, act.kind, act)

        # ---- read-only tool-result assembly (prefetch or live) ----
        if act.kind in READ_ONLY_KINDS:
            yield from dispatch_readonly_action(
                session, act, idx, aid, prefetch, is_native
            )
            continue

        # ---- local tool-result assembly (workspace / mutate / browse / mcp) ----
        if act.kind in LOCAL_ACTION_KINDS:
            yield from dispatch_local_action(
                session, act, aid, is_native, turn_changed_files, act_goal=act_goal,
            )
            continue

        # ---- delegate / swarm / memory tool-result assembly ----
        if act.kind in DISPATCH_ACTION_KINDS:
            disposition = None
            if act.kind == "run_swarm":
                _counters = {"swarms": swarms, "demo_swarms": demo_swarms}
                disposition = yield from dispatch_swarm_action(
                    session, act, aid, is_native,
                    counters=_counters,
                    turn_findings=turn_findings,
                )
                swarms = _counters["swarms"]
                demo_swarms = _counters["demo_swarms"]
            elif act.kind == "run_implement":
                disposition = yield from dispatch_implement_action(
                    session, act, aid, is_native,
                    turn_actions=turn.actions,
                    action_idx=idx,
                    action_seq=action_seq,
                    step=step,
                    swarms=swarms,
                )
            elif act.kind == "run_parallel":
                disposition = yield from dispatch_parallel_action(
                    session, act, aid, is_native,
                    turn_actions=turn.actions,
                    action_idx=idx,
                    action_seq=action_seq,
                    step=step,
                    swarms=swarms,
                )
            elif act.kind == "route_task":
                disposition = yield from dispatch_route_task_action(
                    session, act, aid, is_native,
                )
            elif act.kind == "memory":
                disposition = yield from dispatch_memory_action(
                    session, act, aid, is_native,
                )
            if disposition == "return":
                counters["action_seq"] = action_seq
                counters["swarms"] = swarms
                counters["demo_swarms"] = demo_swarms
                return ("return", turn_changed_files)
            continue

    # Enforce turn budget on the newly appended actions
    new_messages = session._history[history_len_before_actions:]
    session._turn_economy.enforce_tool_batch(new_messages)
    session._history[history_len_before_actions:] = new_messages

    counters["action_seq"] = action_seq
    counters["swarms"] = swarms
    counters["demo_swarms"] = demo_swarms
    return (None, turn_changed_files)
