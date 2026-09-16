"""Session-owned consult and routine command HTTP bodies."""
from __future__ import annotations

import copy
import re
import threading
from contextlib import nullcontext

from .session_control import _goal_pilot, SessionControlServices
from ..advisor_consult import AdvisorService, AdviceConflict
from ..pilot_replacement import input_publication

_services = {}
_lock = threading.Lock()


def _service(svc):
    path = svc.cfg.state_dir
    with _lock:
        if path not in _services:
            _services[path] = AdvisorService(path)
        return _services[path]


def _owner(session_id, svc):
    if not isinstance(session_id, str) or not session_id.strip():
        return None, (400, {"error": "An explicit session_id is required"})
    return _goal_pilot(svc, session_id)


def get_advice(session_id, svc: SessionControlServices):
    _pilot, err = _owner(session_id, svc)
    if err:
        return err
    return 200, {"session_id": session_id, "receipts": _service(svc).history(session_id)}


def post_advise(body, svc: SessionControlServices):
    session_id = body.get("session_id")
    with svc.pilot_swap_lock or nullcontext():
        pilot, err = _owner(session_id, svc)
        if err:
            return err
        try:
            with input_publication(pilot):
                snapshot = copy.deepcopy([
                    {"role": row.get("role"), "content": row.get("content", "")}
                    for row in list(pilot._history) if row.get("role") != "system"
                ])
                receipt = _service(svc).start(session_id, body.get("request_id", ""),
                                              body.get("question", ""), snapshot,
                                              pilot.config.driver)
        except AdviceConflict as exc:
            return 409, {"error": str(exc)}
        except (ValueError, TypeError, AttributeError) as exc:
            return 400, {"error": str(exc)}
    return 202, {"receipt": receipt}


def post_advice_cancel(body, svc: SessionControlServices):
    session_id = body.get("session_id")
    _pilot, err = _owner(session_id, svc)
    if err:
        return err
    try:
        receipt = _service(svc).cancel(session_id, body.get("request_id", ""))
    except (ValueError, TypeError, AttributeError) as exc:
        return 400, {"error": str(exc)}
    return 200, {"receipt": receipt}


def post_routine(body, svc: SessionControlServices):
    from .schedules import post_schedules_add
    session_id = body.get("session_id")
    with svc.pilot_swap_lock or nullcontext():
        pilot, err = _owner(session_id, svc)
        if err:
            return err
        match = re.fullmatch(r"([1-9][0-9]*)([mhd])", str(body.get("every", "")))
        if not match:
            return 400, {"error": "Use an elapsed interval such as 5m, 90m, or 2h"}
        seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return 400, {"error": "Routine prompt is required"}
        if not pilot.config.driver:
            return 400, {"error": "Select a driver before creating a routine"}
        with input_publication(pilot):
            return post_schedules_add({
                "name": prompt.strip()[:80], "objective": prompt.strip(),
                "cron": "", "interval_seconds": seconds, "missed_policy": "once",
                "repo": pilot.config.repo, "driver": pilot.config.driver,
                "swarm_adapter": pilot.config.swarm_adapter,
                "request_id": body.get("request_id"),
            })
