from __future__ import annotations

"""Puppetmaster flow graphs for the pilot: ``run_flow`` and ``flow_control``.

The pilot writes one graph; Puppetmaster walks it in a detached process and
the session drain wakes the pilot when a run is done, failed, stuck, stopped,
interrupted, or waiting at a gate. This is the only module that imports
``puppetmaster.flow`` / ``commands_flow``; every call goes through
:func:`flow_call`.

Per-session state lives on ``session._flow_runs`` (``run_id`` -> record) and is
touched only under the session's ``_busy`` lock (dispatch and drain).
"""

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

# Mirrors puppetmaster.flow; kept local so importing this module stays cheap.
TERMINAL_STATUSES = ("done", "failed", "stuck", "stopped")
WAKE_STATUSES = TERMINAL_STATUSES + ("waiting", "interrupted")
CONTROLS = ("answer", "resume", "cut", "stop")
DEFAULT_MAX_ACTIVE_FLOWS = 3
_WAKE_STEP_CAP = 12
_TEXT_CAP = 240

# Swarm-pill status for each wake status.
_PILL_STATUS = {
    "done": "done",
    "failed": "failed",
    "stuck": "failed",
    "stopped": "ended",
    "interrupted": "ended",
    "waiting": "waiting",
}


class FlowCallError(Exception):
    """A flow request Puppetmaster or the graph boundary refused."""


@dataclass(frozen=True)
class FlowWake:
    run_id: str
    status: str
    key: str
    summary: dict


def flow_call(state_dir: str, action: str, params: dict) -> dict:
    """Run one Puppetmaster flow action; the single seam tests replace."""
    from pathlib import Path

    try:
        from puppetmaster.cli.commands_flow import flow_action

        body, _code = flow_action(Path(state_dir), action, params)
    except (ImportError, OSError, ValueError) as exc:
        # FlowError subclasses ValueError.
        raise FlowCallError(str(exc) or exc.__class__.__name__) from None
    return body if isinstance(body, dict) else {}


def max_active_flows() -> int:
    try:
        return max(1, int(os.environ.get("HARNESS_MAX_ACTIVE_FLOWS", DEFAULT_MAX_ACTIVE_FLOWS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ACTIVE_FLOWS


def default_goal(graph: Any, flow_input: str) -> str:
    graph_id = graph.get("id") if isinstance(graph, dict) else ""
    label = f"Flow {graph_id or 'graph'}"
    text = " ".join((flow_input or "").split())[:80]
    return f"{label}: {text}" if text else label


# --------------------------------------------------------------------------
# Graph boundary


@dataclass
class _Boundary:
    workspace: str
    root: str
    graph_cwd: str
    primary: str
    allowed_adapters: list
    full_auto_guard: bool
    state_dir: str
    problems: list


def parse_graph(raw: Any) -> dict:
    """A deep copy of ``raw`` as a graph object (a dict or a JSON object string)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise FlowCallError(f"graph is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise FlowCallError("graph must be a JSON object")
    return copy.deepcopy(raw)


def prepare_graph(
    raw: Any,
    *,
    workspace: str,
    session_id: str,
    full_auto_guard: bool,
    state_dir: str,
) -> dict:
    """Normalize a pilot graph at the boundary; raise with every problem found."""
    from pmharness.bridge import worker_token_budget

    from .job_scoping import stamp_task_payload
    from .paths import is_safe_path
    from .swarm_worker_route import resolve_product_worker_adapter

    graph = parse_graph(raw)
    if not (workspace or "").strip():
        raise FlowCallError("no workspace directory is open")
    root = os.path.realpath(workspace)
    problems: list = []

    cwd = graph.get("cwd")
    graph_cwd = root
    if cwd not in (None, ""):
        if not isinstance(cwd, str):
            problems.append("cwd must be a string")
        else:
            target = os.path.normpath(cwd if os.path.isabs(cwd) else os.path.join(root, cwd))
            if is_safe_path(target, root):
                graph_cwd = target
            else:
                problems.append(f"cwd {cwd!r} is outside the workspace {root}")
    graph["cwd"] = graph_cwd

    primary = resolve_product_worker_adapter()
    try:
        from .swarm_worker_allowlist import resolve_swarm_worker_allowlist

        allow = resolve_swarm_worker_allowlist()
    except Exception:
        # Same fail-closed shape as bridge.execute_intent: never widen routing
        # to the whole catalog because resolution failed.
        allow = {
            "allowed_adapters": [],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
            "allowed_model_ids": [],
        }
    ctx = _Boundary(
        workspace=workspace,
        root=root,
        graph_cwd=graph_cwd,
        primary=primary,
        allowed_adapters=list(allow.get("allowed_adapters") or []),
        full_auto_guard=bool(full_auto_guard),
        state_dir=state_dir,
        problems=problems,
    )

    defaults = graph.get("defaults")
    if defaults is None:
        defaults = {}
        graph["defaults"] = defaults
    if isinstance(defaults, dict):
        _reject_routing(defaults, "defaults", ctx)
        payload: dict = {
            "auto_route": True,
            "allowed_adapters": list(ctx.allowed_adapters),
            "prefer_plan_billed": bool(allow.get("prefer_plan_billed")),
            "routing_policy": "balanced",
            "token_budget": worker_token_budget(),
        }
        adapter = primary
        pinned = None
        if "model" in defaults:
            pinned = _resolve_pin(defaults.pop("model"), "defaults", ctx)
        if pinned:
            fields, adapter = pinned
            payload.update(fields)
        else:
            # Always stamped, including empty, so no eligible model cannot
            # widen routing back to the whole catalog.
            payload["allowed_model_ids"] = [
                str(item).strip()
                for item in (allow.get("allowed_model_ids") or [])
                if str(item).strip()
            ]
        defaults["adapter"] = adapter
        defaults["payload"] = stamp_task_payload(payload, session_id=session_id, cwd=graph_cwd)

    _walk_nodes(graph.get("nodes"), "", ctx)

    from puppetmaster.flow import validate_graph

    problems.extend(validate_graph(graph))
    if problems:
        raise FlowCallError("graph refused:\n- " + "\n- ".join(str(p) for p in problems))
    return graph


def _reject_routing(obj: dict, where: str, ctx: _Boundary) -> None:
    for key in ("adapter", "payload"):
        if key in obj:
            ctx.problems.append(
                f"{where} sets {key!r}; worker routing and permissions are Marionette's "
                "(use model for an exact worker pin)"
            )


def _resolve_pin(pin: Any, where: str, ctx: _Boundary) -> Optional[tuple]:
    """``(pin_fields, adapter)`` for an exact live worker pin; a problem otherwise."""
    if not isinstance(pin, str) or not pin.strip():
        ctx.problems.append(f"{where} model must be a non-empty string")
        return None
    pin = pin.strip()
    from .swarm_model_pin import _parse_pin_provider_model, resolve_swarm_model_pin

    try:
        resolved = resolve_swarm_model_pin(
            pin,
            allowed_adapters=(
                None if _parse_pin_provider_model(pin)[0] == "codex" else ctx.allowed_adapters
            ),
        )
    except Exception as exc:
        ctx.problems.append(f"{where} model {pin!r} could not be resolved: {exc}")
        return None
    fields = dict(resolved.get("pin_fields") or {})
    if resolved.get("demoted") or not fields.get("pinned_model"):
        reason = str(resolved.get("reason") or f"model pin {pin!r} is unavailable")
        ctx.problems.append(
            f"{where} model {pin!r}: {reason}. Choose an exact Models-enabled live worker model."
        )
        return None
    fields["auto_route"] = False
    adapter = str(resolved.get("adapter") or ctx.primary).strip().lower() or ctx.primary
    return fields, adapter


def _walk_nodes(nodes: Any, prefix: str, ctx: _Boundary) -> None:
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        where = f"{prefix}node {node.get('id')!r}"
        _reject_routing(node, where, ctx)
        if "model" in node:
            pinned = _resolve_pin(node.pop("model"), where, ctx)
            if pinned:
                node["payload"], node["adapter"] = pinned
        kind = node.get("kind")
        if kind == "shell":
            _check_shell(node, where, ctx)
        elif kind == "map":
            template = node.get("node")
            if isinstance(template, dict):
                _walk_nodes([template], f"{where} template ", ctx)
            child = node.get("graph")
            if isinstance(child, dict):
                child_where = f"{where} graph "
                child_defaults = child.get("defaults")
                if isinstance(child_defaults, dict):
                    _reject_routing(child_defaults, f"{child_where}defaults", ctx)
                    if "model" in child_defaults:
                        # Child defaults merge over the parent's (payload too).
                        pinned = _resolve_pin(child_defaults.pop("model"), f"{child_where}defaults", ctx)
                        if pinned:
                            child_defaults["payload"], child_defaults["adapter"] = pinned
                _walk_nodes(child.get("nodes"), child_where, ctx)


def _check_shell(node: dict, where: str, ctx: _Boundary) -> None:
    from .paths import is_safe_path

    cwd = node.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        if "{{" in cwd:
            ctx.problems.append(f"{where} cwd must not be a template (it must stay in the workspace)")
        else:
            target = os.path.normpath(cwd if os.path.isabs(cwd) else os.path.join(ctx.graph_cwd, cwd))
            if not is_safe_path(target, ctx.root):
                ctx.problems.append(f"{where} cwd {cwd!r} is outside the workspace {ctx.root}")
    command = node.get("command")
    if not ctx.full_auto_guard or not isinstance(command, str) or not command.strip():
        return
    from .command_allowlist import allowlist_contains
    from .command_policy import guard_destructive_command

    verdict = guard_destructive_command(command)
    if not verdict.danger:
        return
    # One-shot approvals do not apply: a flow may rerun the command.
    if allowlist_contains(
        command,
        state_dir=ctx.state_dir,
        workspace_root=ctx.workspace,
        command_hash=hashlib.sha256(command.encode("utf-8")).hexdigest(),
    ):
        return
    ctx.problems.append(
        f"{where} shell command blocked by the full-auto guard "
        f"({verdict.category}): {verdict.reason}"
    )


# --------------------------------------------------------------------------
# Session runs


def _runs(session: Any) -> dict:
    runs = getattr(session, "_flow_runs", None)
    if not isinstance(runs, dict):
        runs = {}
        session._flow_runs = runs
    return runs


def active_flow_ids(session: Any) -> list:
    runs = getattr(session, "_flow_runs", None)
    if not isinstance(runs, dict):
        return []
    return [
        run_id for run_id, record in list(runs.items())
        if isinstance(record, dict) and record.get("status") == "running"
    ]


def launch(
    session: Any,
    *,
    graph: Any,
    flow_input: str,
    continue_from: str,
    goal: str,
    repo: str,
) -> tuple:
    """Start a run in the background; returns ``(run_id, summary, objective)``."""
    runs = _runs(session)
    cap = max_active_flows()
    if len(active_flow_ids(session)) >= cap:
        raise FlowCallError(
            f"this session already has {cap} active flows; wait for one to finish "
            "or stop one with flow_control"
        )
    continue_from = (continue_from or "").strip()
    if continue_from and continue_from not in runs:
        raise FlowCallError(f"continue_from {continue_from!r} is not a flow run of this session")
    from .repo_resolve import resolve_effective_repo

    workspace = resolve_effective_repo(repo or getattr(session.config, "repo", "") or "")
    try:
        from .marionette_registry import boot_marionette_registry

        boot_marionette_registry()
    except Exception as exc:
        from .diag import note as _diag

        _diag("flows.boot_marionette_registry", exc)
    # The detached walker inherits this process's environment.
    from pmharness.bridge import _sync_agentic_credential_env

    _sync_agentic_credential_env()
    prepared = prepare_graph(
        graph,
        workspace=workspace,
        session_id=str(getattr(session, "harness_session_id", "") or ""),
        full_auto_guard=bool(
            getattr(session, "_auto_mode", False) and getattr(session, "_auto_command_guard", None)
        ),
        state_dir=session.state_dir,
    )
    summary = flow_call(session.state_dir, "run", {
        "graph": prepared,
        "input": flow_input or "",
        "continue_from": continue_from or None,
        "cwd": prepared["cwd"],
    })
    run_id = str(summary.get("run_id") or "")
    if not run_id:
        raise FlowCallError("Puppetmaster returned no run id")
    objective = (goal or "").strip() or default_goal(prepared, flow_input)
    runs[run_id] = {"objective": objective, "delivered": "", "since": 0, "status": "running"}
    if run_id not in session._session_job_ids:
        session._session_job_ids.append(run_id)
    upsert_pill(session, run_id, objective, "running")
    return run_id, summary, objective


def control(session: Any, run_id: str, control: str, answer: str = "", reason: str = "") -> str:
    """Answer a gate, resume, cut or stop a run of this session; returns a status line."""
    record = _runs(session).get(run_id)
    if not isinstance(record, dict):
        raise FlowCallError(f"{run_id!r} is not a flow run of this session")
    if control == "answer":
        if not (answer or "").strip():
            raise FlowCallError("flow_control answer requires a non-empty answer")
        body = flow_call(session.state_dir, "resume", {
            "run_id": run_id, "answer": answer, "background": True,
        })
    elif control == "resume":
        body = flow_call(session.state_dir, "resume", {"run_id": run_id, "background": True})
    elif control == "stop":
        body = flow_call(session.state_dir, "stop", {"run_id": run_id})
    elif control == "cut":
        body = flow_call(session.state_dir, "cut", {"run_id": run_id, "reason": reason or ""})
    else:
        raise FlowCallError(f"flow_control control must be one of {', '.join(CONTROLS)}")
    status = str(body.get("status") or "")
    if control in ("answer", "resume") and status == "running":
        record["status"] = "running"
        # A re-interrupt with no new steps repeats the old key; it is a new event.
        record["delivered"] = ""
        upsert_pill(session, run_id, str(record.get("objective") or ""), "running")
    line = f"flow {run_id} {control}: status {status or 'unknown'}"
    if body.get("reason"):
        line += f" ({_clip(body['reason'])})"
    return line


def upsert_pill(session: Any, run_id: str, objective: str, status: str) -> None:
    """Persist the run's swarm pill so a reload shows its current state."""
    display = getattr(session, "_display_transcript", None)
    if not isinstance(display, list):
        return
    row = {
        "type": "swarm_pending",
        "job_ids": [run_id],
        "objective": objective,
        "status": status,
        "session_id": str(getattr(session, "harness_session_id", "") or ""),
    }
    for existing in display:
        if (
            isinstance(existing, dict)
            and existing.get("type") == "swarm_pending"
            and list(existing.get("job_ids") or []) == [run_id]
        ):
            existing.update(row)
            return
    display.append(row)


# --------------------------------------------------------------------------
# Wakes


def due_wakes(session: Any) -> list:
    """Runs of this session with an undelivered wake event. Never raises."""
    out: list = []
    runs = getattr(session, "_flow_runs", None)
    if not isinstance(runs, dict):
        return out
    for run_id, record in list(runs.items()):
        try:
            if not isinstance(record, dict) or record.get("status") in TERMINAL_STATUSES:
                continue
            summary = flow_call(session.state_dir, "status", {
                "run_id": run_id, "since": int(record.get("since") or 0),
            })
            status = str(summary.get("status") or "")
            if status not in WAKE_STATUSES:
                continue
            gate = summary.get("gate") if isinstance(summary.get("gate"), dict) else {}
            key = f"{status}:{summary.get('next_since', 0)}:{gate.get('node') or ''}"
            if key == record.get("delivered"):
                continue
            out.append(FlowWake(run_id=run_id, status=status, key=key, summary=summary))
        except Exception:
            continue
    return out


def mark_delivered(session: Any, wake: FlowWake) -> None:
    record = _runs(session).get(wake.run_id)
    if not isinstance(record, dict):
        return
    record["delivered"] = wake.key
    try:
        record["since"] = int(wake.summary.get("next_since") or record.get("since") or 0)
    except (TypeError, ValueError):
        pass
    record["status"] = wake.status


def pill_status(status: str) -> str:
    return _PILL_STATUS.get(status, "ended")


def wake_result(wake: FlowWake) -> dict:
    """The swarm_result body the tracker renders for a terminal wake."""
    summary = wake.summary
    reason = str(summary.get("reason") or "")
    problem = summary.get("last_problem") if isinstance(summary.get("last_problem"), dict) else {}
    detail = reason or str(problem.get("reason") or problem.get("error") or "")
    error = None
    if wake.status == "failed":
        error = detail or "flow failed"
    text = f"flow {wake.status}" + (f": {_clip(detail)}" if detail else "")
    return {
        "applied": False,
        "files": [],
        "summary": text,
        "error": error,
        "analysis_ok": wake.status == "done",
        "held_for_review": False,
        "artifacts": [],
        "degraded": wake.status == "stuck",
        "flow_status": wake.status,
        "status": "cancelled" if pill_status(wake.status) == "ended" else wake.status,
    }


def format_wake(wake: FlowWake, objective: str) -> str:
    """Compact body of the labeled history record for one wake."""
    summary = wake.summary
    lines = [f"status: {wake.status}" + (f" ({_clip(summary['reason'])})" if summary.get("reason") else "")]
    steps = [step for step in summary.get("steps") or [] if isinstance(step, dict)]
    if steps:
        skipped = len(steps) - _WAKE_STEP_CAP
        if skipped > 0:
            lines.append(f"steps (earlier {skipped} omitted):")
            steps = steps[-_WAKE_STEP_CAP:]
        else:
            lines.append("steps:")
        for step in steps:
            bit = f"- {step.get('node')}#{step.get('visit', 1)} {'ok' if step.get('ok') else 'FAILED'}"
            if step.get("verdict"):
                bit += f" {step['verdict']}"
            detail = step.get("reason") or step.get("error")
            if detail:
                bit += f": {_clip(detail)}"
            lines.append(bit)
    problem = summary.get("last_problem")
    if isinstance(problem, dict) and problem:
        lines.append("last problem: " + ", ".join(f"{k}={_clip(v)}" for k, v in problem.items()))
    usage = summary.get("usage")
    if isinstance(usage, dict) and usage:
        lines.append("usage: " + json.dumps(usage, sort_keys=True, default=str))
    gate = summary.get("gate")
    if isinstance(gate, dict) and gate:
        lines.append(f"gate {gate.get('node')}: {gate.get('question')}")
        options = gate.get("options") or []
        if options:
            lines.append("options: " + " | ".join(str(option) for option in options))
    return "\n".join(lines)


def continuation_text(wakes: list) -> str:
    """Status-specific guidance for the pilot's continuation message."""
    lines = []
    for wake in wakes:
        run_id, status = wake.run_id, wake.status
        head = f"[flow {run_id} {status}]"
        if status == "done":
            lines.append(f"{head} Report the result above and take the next step.")
        elif status in ("failed", "stuck"):
            lines.append(
                f"{head} Report the reason above. Fix the cause and call run_flow with "
                f"continue_from={run_id}, or stop."
            )
        elif status == "interrupted":
            lines.append(
                f"{head} The walker died (for example machine sleep). Call flow_control "
                f'{{"run_id": "{run_id}", "control": "resume"}}.'
            )
        elif status == "stopped":
            lines.append(f"{head} The run was stopped. Acknowledge it.")
        elif status == "waiting":
            gate = wake.summary.get("gate") if isinstance(wake.summary.get("gate"), dict) else {}
            options = " | ".join(str(option) for option in gate.get("options") or []) or "(free text)"
            lines.append(
                f"{head} Gate {gate.get('node')}: {gate.get('question')} Options: {options}. "
                "If the decision is the user's, ask them first. Then call flow_control "
                f'{{"run_id": "{run_id}", "control": "answer", "answer": <one option>}}.'
            )
    return "\n".join(lines)


def _clip(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= _TEXT_CAP else text[: _TEXT_CAP - 3] + "..."
