from __future__ import annotations

"""Send-loop phase helpers peeled from SendLoopMixin._send_locked_inner.

These were nested closures inside the turn kernel — hard to unit-test in
isolation. They are mechanical extractions: same queue/thread contracts, same
exception surfaces, same ConvEvent shapes produced by the caller. Explicit
``session`` / queue / schema args replace closure capture.

Public orchestration stays on SendLoopMixin.send / _send_locked /
_send_locked_inner; this module owns only the background-thread targets and
prefetch map workers.
"""

import inspect
import re
from typing import Any

from pmharness.bridge import execute_intent

from .pilot import PilotAction

# job_XXXXXXXXXXXX — same pattern the parallel-dispatch stdout scanner used
# when nested inside _send_locked_inner.
_JOB_ID_RE = re.compile(r"\b(job_[a-fA-F0-9]{12})\b")


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
