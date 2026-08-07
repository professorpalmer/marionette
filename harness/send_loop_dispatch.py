from __future__ import annotations

"""Delegate / swarm / memory action dispatch peeled from ``_send_locked_inner``.

Mechanical extractions of the remaining per-kind branches after read-only and
local tool-result assembly: ``run_swarm``, ``run_implement``, ``run_parallel``,
``route_task``, and ``memory``. Same ConvEvent shapes, same history appends,
same objective-claim / capacity-gate / assistant_done early-exit behavior.

Public orchestration stays on ``SendLoopMixin``; helpers take an explicit
``session`` plus the small counters / loop indices the kernel owns.
"""

import re
import subprocess
from typing import Any, Iterator, Optional

from pmharness.intent import DriverIntent

from ._exec import _puppetmaster_available, _puppetmaster_cmd
from .repo_resolve import resolve_effective_repo
from .send_loop_phases import read_stdout_thread, stream_swarm
from .swarm_run_facts import (
    build_swarm_run_facts,
    digest_line,
    normalize_execution_refs,
    render_evidence_boundary,
)

DISPATCH_ACTION_KINDS: frozenset[str] = frozenset({
    "run_swarm", "run_implement", "run_parallel", "route_task", "memory",
})


def _non_git_workspace_error(repo: str) -> Optional[str]:
    """Calm refuse when the resolved workspace is not a git work tree.

    Used after ``resolve_effective_repo`` for swarm/implement/parallel so we
    never launch workers against a non-git Home parent. Returns None when
    ``repo`` is empty (callers handle missing workspace separately) or when
    the path looks like a git checkout.

    Prefer a filesystem ``.git`` marker check first — ``subprocess.run`` goes
    through ``Popen``, which tests (and some call sites) mock, so a git
    rev-parse-only check falsely refused real worktrees under those mocks.
    """
    import os

    path = (repo or "").strip()
    if not path:
        return None
    try:
        abs_path = os.path.abspath(path)
    except Exception:
        abs_path = path
    git_marker = os.path.join(abs_path, ".git")
    try:
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            return None
    except Exception:
        pass
    try:
        proc = subprocess.run(
            ["git", "-C", abs_path, "rev-parse", "--is-inside-work-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip() == "true":
            return None
    except Exception:
        pass
    return (
        f"Workspace is not a git repository (resolved to {abs_path}). "
        "Open the project checkout or ensure Home has a marionette child."
    )

# Best-effort guard: refuse memory adds that look like pasted credentials.
_MEMORY_SECRET_RE = re.compile(
    r"(?:"
    r"(?:api[_-]?key|secret|password|token)\s*=\s*\S+"
    r"|sk-[A-Za-z0-9_-]{8,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r")",
    re.IGNORECASE,
)

_PATH_REF_RE = re.compile(
    r"[\w./\\-]+\.(py|ts|tsx|js|jsx|cjs|mjs|json|md|toml|yml|yaml|css|html|rs|go|java|c|h|cpp|sh|ps1|bat)\b"
    r"|line\s+\d+|:\d+\b",
    re.IGNORECASE,
)


def _resolved_swarm_model(result: Any, arts: Optional[list] = None) -> str:
    """Best-effort model id from a swarm/implement result (never raises).

    Prefers ``result.model``, then ROUTING artifact model via edit_engines
    helper / dict arts. Returns '' when nothing real is known — callers must
    not invent bare agentic/native.
    """
    try:
        from harness.model_identity import is_engine_only_model_id
    except Exception:
        def is_engine_only_model_id(mid: str) -> bool:  # type: ignore[misc]
            return not (mid or "").strip() or (mid or "").strip().lower() in (
                "agentic", "native",
            )

    try:
        mid = str(getattr(result, "model", None) or "").strip()
        if mid and not is_engine_only_model_id(mid):
            return mid
    except Exception:
        pass
    try:
        from harness.edit_engines import _routed_model_id
        routed = str(_routed_model_id(result) or "").strip()
        if routed and not is_engine_only_model_id(routed):
            return routed
    except Exception:
        pass
    model_id = ""
    for a in arts or []:
        try:
            if not isinstance(a, dict):
                continue
            if str(a.get("type") or "").strip().upper() != "ROUTING":
                continue
            cand = str(a.get("model") or a.get("model_id") or "").strip()
            if cand and not is_engine_only_model_id(cand):
                model_id = cand
        except Exception:
            continue
    return model_id


def _is_substantive_artifact(a: dict) -> bool:
    """True when a FINDING/RISK/DECISION carries real analysis, not a stub.

    Workers that choke on a goal still emit one-line generic findings ("audit
    complete", "no issues found") with no evidence. Substance = enough prose to
    be an actual finding, or a shorter claim that at least cites a concrete
    file/line. Keeps the badge honest without judging content quality by LLM.

    Reasoning fragments ("Now let me look at...") and meta degrade markers
    (no_tool_calls / without structured findings) are never substantive --
    they must not turn the swarm badge green or masquerade as findings.
    """
    try:
        try:
            from pmharness.bridge import (
                _is_meta_degrade_artifact,
                looks_like_reasoning_fragment,
            )
            if _is_meta_degrade_artifact(a):
                return False
            text = str(a.get('body') or a.get('headline') or '').strip()
            if looks_like_reasoning_fragment(text):
                return False
        except Exception:
            text = str(a.get('body') or a.get('headline') or '').strip()
        if len(text) >= 200:
            return True
        return len(text) >= 40 and bool(_PATH_REF_RE.search(text))
    except Exception:
        # Fail closed: thin/plumbing FINDINGs must not paint the badge green
        # when the gate itself cannot parse the payload.
        return False


# Puppetmaster MCP verbs that create jobs outside Marionette's local tracker.
# Host pilots must use run_swarm / run_implement / run_parallel (or shell
# ``python -m puppetmaster swarm`` which lands in the CLI durable store the
# tracker merges). MCP start_* bypasses swarm_pending + _register_local_job.
_UNTRACKED_PM_START_TOOLS = frozenset({
    "start_cursor_swarm",
    "start_swarm",
    "start_implement",
    "start_cursor_implement",
    "start_claude_implement",
    "start_codex",
    "start_agentic",
    "start_browser_swarm",
    "start_openai",
    "start_prewalk",
    # Sync MCP wait verbs (after stripping puppetmaster_).
    "cursor_implement",
    "claude_implement",
    "cursor_swarm",
})

# Short names that collide with other MCP servers — only refuse when the
# original tool id was Puppetmaster-scoped.
_UNTRACKED_PM_AMBIGUOUS = frozenset({
    "codex",
    "agentic",
    "openai",
})


def is_untracked_pm_start_tool(tool: str) -> bool:
    """True when ``tool`` is a Puppetmaster start_* / sync implement verb that skips the tracker."""
    raw = (tool or "").strip().lower().replace("-", "_")
    if not raw:
        return False
    pm_scoped = "puppetmaster" in raw
    t = raw
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    if t.startswith("puppetmaster_"):
        t = t[len("puppetmaster_"):]
        pm_scoped = True
    if t in _UNTRACKED_PM_START_TOOLS:
        return True
    if t in _UNTRACKED_PM_AMBIGUOUS:
        return pm_scoped
    return False


_TRACKABLE_SWARM_REFUSAL = (
    "Untracked swarm/implement: do not call Puppetmaster MCP start_* tools. "
    "Use host run_swarm / run_implement / run_parallel so the Swarm Tracker "
    "registers the job, or shell `python -m puppetmaster swarm \"<goal>\"` "
    "(CLI store is merged into the tracker for this workspace)."
)


def dispatch_swarm_action(session, act, aid, is_native, *, counters, turn_findings) -> Iterator[Any]:
    """Assemble tool-results for ``run_swarm`` (peeled from ``_send_locked_inner``).

Yields the same ConvEvent stream. Generator return value is ``None``
(continue the action loop) or ``"return"`` (close the turn / exit send).
"""
    from .conversation import ConvEvent
    _acceptance_criteria = list(getattr(act, 'acceptance_criteria', None) or [])
    if not _acceptance_criteria and isinstance(getattr(act, 'arguments', None), dict):
        try:
            from harness.environment_fingerprint import normalize_acceptance_criteria
            _acceptance_criteria = normalize_acceptance_criteria(
                act.arguments.get('acceptance_criteria')
            )
        except Exception:
            _acceptance_criteria = []
    _sync_local_id = f'local-swarm-{aid}'
    # An explicit subject repo audits a DIFFERENT checkout read-only. It fails
    # closed on a non-git/missing path (same contract as run_implement) and
    # never becomes the pilot's own write surface — session.config.repo, and
    # therefore every write/edit/command tool, stays on the open workspace.
    _subject_repo = (getattr(act, 'repo', '') or '').strip()
    if _subject_repo:
        _subject_abs, _subject_err = session._validate_target_repo(_subject_repo)
        if _subject_err:
            error_msg = f'run_swarm: subject repo {_subject_repo} is not a valid git repository'
            yield ConvEvent('action_result', {'id': aid, 'error': error_msg})
            session._append_action_result(act, aid, f'(swarm {aid} failed: {error_msg})', is_native)
            return None
        _swarm_repo = resolve_effective_repo(_subject_abs)
    else:
        _swarm_repo = resolve_effective_repo(session.config.repo or '') if (session.config.repo or '').strip() else ''
    intent = DriverIntent(
        action='run_swarm',
        goal=act.goal,
        roles=act.roles or None,
        rationale='pilot',
        model=(act.model or '').strip() or None,
        acceptance_criteria=_acceptance_criteria or None,
        repo=_swarm_repo or None,
    )
    _non_git = _non_git_workspace_error(_swarm_repo)
    if _non_git:
        # action_start already emitted by execute_turn_actions for run_swarm
        yield ConvEvent('action_result', {'id': aid, 'error': _non_git})
        session._append_action_result(act, aid, f'(swarm {aid} failed: {_non_git})', is_native)
        return None
    # Read-before-dispatch validation reuse gate (deterministic; no adapter spend
    # on a clean fingerprint match).
    _reuse_decision = None
    try:
        from harness.validation_reuse import evaluate_reuse_gate
        _force_fresh = bool(
            (getattr(act, 'arguments', None) or {}).get('force_fresh')
            if isinstance(getattr(act, 'arguments', None), dict) else False
        )
        _reuse_decision = evaluate_reuse_gate(
            session,
            objective=act.goal or '',
            role='explore',
            cwd=_swarm_repo,
            force_fresh=_force_fresh,
            acceptance_criteria=_acceptance_criteria,
        )
    except Exception:
        _reuse_decision = None
    if _reuse_decision is not None and _reuse_decision.outcome == 'reuse':
        _prov = _reuse_decision.as_provenance()
        _reuse_registered = False
        try:
            session._register_local_job(
                _sync_local_id, act.goal, role='explore', cwd=_swarm_repo,
                engine='agentic', skip_routing_preview=True,
            )
            _reuse_registered = True
            session._session_job_ids.append(_sync_local_id)
            session._finish_local_job(
                _sync_local_id,
                ok=True,
                summary=(
                    f"reused {_reuse_decision.source_job_id} "
                    f"({_reuse_decision.reason})"
                )[:200],
                status='done',
                engine='agentic',
                tokens=0,
                est_cost_usd=0.0,
                findings=[
                    {
                        'type': a.get('type') or 'finding',
                        'headline': a.get('headline') or a.get('uri') or '',
                        'id': a.get('id'),
                    }
                    for a in (_reuse_decision.compact_artifacts or [])
                ],
                reuse_status='reused',
                source_job_id=_reuse_decision.source_job_id,
                validation_fingerprint=_reuse_decision.validation_fingerprint,
                reuse_reason=_reuse_decision.reason,
                environment_fingerprint=getattr(
                    _reuse_decision, 'environment_fingerprint', None,
                ) or '',
                acceptance_criteria=list(
                    getattr(_reuse_decision, 'acceptance_criteria', None)
                    or _acceptance_criteria
                    or []
                ),
            )
        except Exception as e:
            # Fail closed: never emit a green reused badge without a durable
            # stamped local job (mirrors non-reuse register failure).
            # If register succeeded but finish failed, settle/drop the orphan
            # so no live spinner remains.
            if _reuse_registered:
                try:
                    session._fail_or_drop_local_job(
                        _sync_local_id,
                        summary=f'tracker finish failed: {e}',
                    )
                except Exception:
                    pass
            err = f'tracker register/finish failed: {e}'
            yield ConvEvent('action_result', {'id': aid, 'error': err})
            session._append_action_result(
                act, aid, f'(swarm {aid} failed: {err})', is_native,
            )
            return None
        _badge = {
            'job_id': _sync_local_id,
            'applied': True,
            'files': [],
            'summary': f"reused prior analysis from {_reuse_decision.source_job_id}",
            'error': None,
            'objective': act.goal,
            'adapter': 'reuse',
            **_prov,
        }
        yield ConvEvent('action_result', {
            'id': aid,
            'job_id': _sync_local_id,
            'num': len(_reuse_decision.compact_artifacts or []),
            'types': sorted({
                str(a.get('type') or 'finding')
                for a in (_reuse_decision.compact_artifacts or [])
            }),
            'artifacts': list(_reuse_decision.compact_artifacts or [])[:12],
            'adapter': 'reuse',
            'mode': 'reuse',
            'auth_failure': '',
            'error': None,
            **_prov,
        })
        session._display_transcript.append({'type': 'swarm_result', **_badge})
        yield ConvEvent('swarm_result', {
            'job_id': _badge['job_id'],
            'objective': act.goal,
            'result': _badge,
        })
        digest = _reuse_decision.digest_text or (
            f"REUSED {_reuse_decision.source_job_id} ({_reuse_decision.reason})"
        )
        session._append_action_result(
            act, aid,
            f"(swarm {aid} reused prior validation; zero new execution spend)\n{digest}",
            is_native,
        )
        return None
    _sync_register_role = 'explore'
    if _reuse_decision is not None and getattr(
        _reuse_decision, 'acceptance_criteria', None,
    ):
        _acceptance_criteria = list(_reuse_decision.acceptance_criteria or [])
    if _reuse_decision is not None and _reuse_decision.outcome == 'narrow_verify':
        # Verifier-only / narrow analysis — do not re-open explore/pipeline-mapper.
        narrow_suffix = _reuse_decision.narrow_goal_suffix or (
            'Re-verify invalidated paths only; do not re-run explore/pipeline-mapper.'
        )
        _sync_narrow_roles = list(
            _reuse_decision.narrow_roles or ('conflict-auditor',)
        )
        _sync_register_role = _sync_narrow_roles[0] if _sync_narrow_roles else 'conflict-auditor'
        _narrow_criteria = list(
            getattr(_reuse_decision, 'acceptance_criteria', None)
            or _acceptance_criteria
            or []
        )
        intent = DriverIntent(
            action='run_swarm',
            goal=f"{act.goal}\n\n{narrow_suffix}",
            roles=_sync_narrow_roles,
            rationale='pilot-narrow-verify',
            model=(act.model or '').strip() or None,
            acceptance_criteria=_narrow_criteria or None,
            repo=_swarm_repo or None,
        )
    elif (
        _reuse_decision is not None
        and _reuse_decision.outcome == 'full_swarm'
        and _acceptance_criteria
        and list(intent.acceptance_criteria or []) != list(_acceptance_criteria)
    ):
        # Preserve prior/explicit criteria into the fresh full-swarm workers.
        intent = DriverIntent(
            action='run_swarm',
            goal=act.goal,
            roles=act.roles or None,
            rationale='pilot',
            model=(act.model or '').strip() or None,
            acceptance_criteria=list(_acceptance_criteria) or None,
            repo=_swarm_repo or None,
        )
    try:
        session._register_local_job(
            _sync_local_id, act.goal, role=_sync_register_role,
            cwd=_swarm_repo, engine='agentic',
        )
        session._session_job_ids.append(_sync_local_id)
        # Pre-stamp narrow_verify / full_swarm rejection lineage so finish/drain
        # keep fingerprint + reuse_reason even if the finish caller omits them.
        if _reuse_decision is not None and _reuse_decision.outcome in (
            'narrow_verify', 'full_swarm',
        ):
            try:
                with session._local_jobs_lock:
                    _nj = session._local_jobs.get(_sync_local_id)
                    if isinstance(_nj, dict):
                        if _reuse_decision.outcome == 'narrow_verify':
                            _nj['reuse_status'] = 'partial'
                            _nj['source_job_id'] = _reuse_decision.source_job_id
                            _nj['validation_fingerprint'] = (
                                _reuse_decision.validation_fingerprint or ''
                            )
                            _nj['invalidated_paths'] = list(
                                getattr(_reuse_decision, 'invalidated_paths', None)
                                or []
                            )
                        else:
                            _nj['reuse_status'] = 'fresh'
                            if _reuse_decision.source_job_id:
                                _nj['source_job_id'] = _reuse_decision.source_job_id
                            if _reuse_decision.validation_fingerprint:
                                _nj['validation_fingerprint'] = (
                                    _reuse_decision.validation_fingerprint
                                )
                        _nj['reuse_reason'] = _reuse_decision.reason
                        _env_fp = getattr(
                            _reuse_decision, 'environment_fingerprint', None,
                        ) or ''
                        if _env_fp:
                            _nj['environment_fingerprint'] = _env_fp
                        _nj['acceptance_criteria'] = list(
                            getattr(_reuse_decision, 'acceptance_criteria', None)
                            or _acceptance_criteria
                            or []
                        )
            except Exception:
                pass
        else:
            try:
                with session._local_jobs_lock:
                    _nj = session._local_jobs.get(_sync_local_id)
                    if isinstance(_nj, dict):
                        _nj['acceptance_criteria'] = list(
                            _acceptance_criteria or []
                        )
            except Exception:
                pass
    except Exception as e:
        err = f'tracker register failed: {e}'
        yield ConvEvent('action_result', {'id': aid, 'error': err})
        session._append_action_result(act, aid, f'(swarm {aid} failed: {err})', is_native)
        return None
    yield ConvEvent('swarm_pending', {'job_ids': [_sync_local_id], 'objective': act.goal})
    import queue as _queue
    import threading as _threading
    _delta_q: '_queue.Queue' = _queue.Queue()
    _swarm_thread = _threading.Thread(target=stream_swarm, args=(session, intent, _delta_q), daemon=True)
    _swarm_thread.start()
    result = None
    swarm_error = None
    while True:
        msg_kind, msg_val = _delta_q.get()
        if msg_kind == 'delta':
            wid, dkind, dtext = msg_val
            yield ConvEvent('worker_delta', {'id': aid, 'worker_id': wid, 'kind': dkind, 'text': dtext})
        elif msg_kind == 'done':
            result = msg_val
            break
        else:
            swarm_error = msg_val
            break
    if swarm_error is not None:
        try:
            session._finish_local_job(_sync_local_id, ok=False, summary=str(swarm_error)[:200], status='failed', engine='agentic')
        except Exception:
            pass
        yield ConvEvent('action_result', {'id': aid, 'error': f'execute: {swarm_error}'})
        session._append_action_result(act, aid, f'(swarm {aid} failed: {swarm_error})', is_native)
        return None
    if result is None:
        try:
            session._finish_local_job(_sync_local_id, ok=False, summary='no result', status='failed', engine='agentic')
        except Exception:
            pass
        yield ConvEvent('action_result', {'id': aid, 'error': 'execute: no result'})
        session._append_action_result(act, aid, f'(swarm {aid} failed: no result)', is_native)
        return None
    counters['swarms'] += 1
    if result.adapter == 'demo':
        counters['demo_swarms'] += 1
    # Product rule: never treat demo substrate as a successful audit, and never
    # surface its placeholder findings to the pilot/UI.
    _demo_refused = False
    try:
        from harness.swarm_adapter import refuse_demo_result
        _demo_refused = refuse_demo_result(getattr(result, 'adapter', '') or '')
    except Exception:
        _demo_refused = (getattr(result, 'adapter', '') or '') == 'demo'
    auth_failure = getattr(result, 'auth_failure', '') or ''
    # Re-derive from artifacts when the bridge field is empty so a zero-signal
    # auth death (verification-only http_status:401) still leads the badge.
    if not auth_failure:
        try:
            from pmharness.bridge import _auth_failure_note
            auth_failure = _auth_failure_note(list(result.artifacts) or []) or ''
        except Exception:
            auth_failure = ''
    if auth_failure:
        yield ConvEvent('swarm_auth_failure', {'id': aid, 'job_id': result.job_id, 'message': auth_failure})
    _SIGNAL = {'finding', 'risk', 'decision'}
    # Strip demo artifacts entirely so placeholder headlines never reach the
    # transcript card or pilot digest.
    _all_arts = [] if _demo_refused else list(result.artifacts)
    # Stamp run identity + first evidence locus BEFORE anything reads the
    # artifacts, so every digest line, sidecar row, and UI card names the job
    # that produced it (parent attribution is left exactly as it arrived).
    _job_id_text = str(getattr(result, 'job_id', '') or _sync_local_id).strip() or _sync_local_id
    _all_arts = normalize_execution_refs(_all_arts, _job_id_text)
    # Reasoning-only fragments must never appear as finding/risk/decision
    # headlines in the digest (same submit contract as swarm workers).
    try:
        from pmharness.bridge import looks_like_reasoning_fragment as _reasoning_frag
    except Exception:
        def _reasoning_frag(_t):  # type: ignore[misc]
            return False
    _signal = [
        a for a in _all_arts
        if str(a.get('type')) in _SIGNAL
        and not _reasoning_frag(a.get('body') or a.get('headline') or '')
    ]
    _plumbing = [a for a in _all_arts if str(a.get('type')) not in _SIGNAL]
    # Keep auth-tagged plumbing (verification) ahead of other plumbing so a
    # zero-signal digest slice cannot drop the credential failure.
    if auth_failure and not _signal:
        try:
            from pmharness.bridge import _is_auth_failure_tag
            _auth_plumb = [a for a in _plumbing if _is_auth_failure_tag(a.get('failure'), a.get('headline'))]
            _other_plumb = [a for a in _plumbing if not _is_auth_failure_tag(a.get('failure'), a.get('headline'))]
            _plumbing = _auth_plumb + _other_plumb
        except Exception:
            pass
    ordered = _signal + _plumbing
    digest_arts = _signal[:20] + _plumbing[:3] if _signal else _plumbing[:8]
    # Every user-visible count/label below describes the SURFACED artifacts, not
    # the raw bridge result. A refused demo surfaces nothing, so it must not
    # report a nonzero count or claim it ran "via demo".
    _ui_adapter = 'refused-demo' if _demo_refused else result.adapter
    _ui_num = len(_all_arts)
    _ui_types = sorted({str(a.get('type')) for a in _all_arts if a.get('type')})
    yield ConvEvent('action_result', {'id': aid, 'job_id': result.job_id, 'num': _ui_num, 'types': _ui_types, 'artifacts': ordered[:12], 'adapter': _ui_adapter, 'mode': result.mode, 'auth_failure': auth_failure, 'error': ('demo substrate -- not real codebase analysis' if _demo_refused else None)})
    _has_signal = bool(_signal)
    # Quality gate: a "finding" with no substance (a one-liner with no file
    # reference) must not turn the badge green -- a swarm whose workers choked
    # on the goal used to read as a clean "N findings" success.
    _substantive = [a for a in _signal if _is_substantive_artifact(a)]
    _swarm_ok = bool(_substantive) and (not auth_failure) and (not _demo_refused)
    if _demo_refused:
        _badge_summary = 'refused: demo substrate (not real codebase analysis)'
    elif auth_failure:
        # Lead with the provider/key note, never a generic "no findings" badge.
        _badge_summary = auth_failure[:160] if len(auth_failure) > 20 else 'auth failure'
    elif _substantive:
        _badge_summary = f'{len(_signal)} findings via {_ui_adapter} ({_ui_num} artifacts)'
    elif _has_signal:
        _badge_summary = f'degraded: {len(_signal)} thin findings via {_ui_adapter} (no file-backed substance)'
    elif _ui_num:
        _badge_summary = f'degraded: {_ui_num} plumbing artifacts via {_ui_adapter}, no findings'
    else:
        _badge_summary = 'no artifacts produced'
    _badge_error = (
        'demo substrate -- not real codebase analysis' if _demo_refused
        else auth_failure or (
            None if _swarm_ok
            else 'swarm findings are thin/generic (no file-backed substance)' if _has_signal
            else 'swarm produced no FINDING/RISK/DECISION artifacts' if _ui_num
            else 'swarm produced no artifacts'))
    _store_jid = (result.job_id or '').strip() or _sync_local_id
    _badge = {'job_id': _store_jid, 'applied': _swarm_ok, 'files': [], 'summary': _badge_summary, 'error': _badge_error, 'objective': act.goal, 'adapter': _ui_adapter}
    _job_engine = _ui_adapter if _demo_refused else (result.adapter or 'agentic')
    # Best-effort routed model so finish cannot clobber a preview ROUTING stamp
    # with bare agentic/native.
    _job_model = _resolved_swarm_model(result, _all_arts)
    # Substantive surfaced findings only: the sidecar must not carry plumbing or
    # refused-demo rows into artifact:// reads.
    _job_findings = _substantive[:20]
    _finish_reuse_status = 'fresh'
    _finish_source_job = ''
    _finish_invalidated: list = []
    _finish_reuse_reason = ''
    _finish_fingerprint = ''
    _finish_env_fingerprint = ''
    _finish_criteria = list(_acceptance_criteria or [])
    if _reuse_decision is not None and _reuse_decision.outcome == 'narrow_verify':
        _finish_reuse_status = 'partial'
        _finish_source_job = _reuse_decision.source_job_id
        _finish_invalidated = list(_reuse_decision.invalidated_paths or [])
        _finish_reuse_reason = _reuse_decision.reason
        _finish_fingerprint = _reuse_decision.validation_fingerprint or ''
        _finish_env_fingerprint = getattr(
            _reuse_decision, 'environment_fingerprint', None,
        ) or ''
        _finish_criteria = list(
            getattr(_reuse_decision, 'acceptance_criteria', None)
            or _finish_criteria
            or []
        )
        _badge['reuse_status'] = 'partial'
        _badge['source_job_id'] = _finish_source_job
        _badge['invalidated_paths'] = _finish_invalidated
        _badge['reuse_reason'] = _finish_reuse_reason
        if _finish_fingerprint:
            _badge['validation_fingerprint'] = _finish_fingerprint
        if _finish_env_fingerprint:
            _badge['environment_fingerprint'] = _finish_env_fingerprint
        if _finish_criteria:
            _badge['acceptance_criteria'] = list(_finish_criteria)
    elif _reuse_decision is not None and _reuse_decision.outcome == 'full_swarm':
        # Preserve the gate rejection reason on the fresh terminal job /
        # transcript / UI (environment_changed, outside_evidence_unproven, …).
        _finish_reuse_status = 'fresh'
        _finish_reuse_reason = _reuse_decision.reason or ''
        _finish_source_job = _reuse_decision.source_job_id or ''
        _finish_fingerprint = _reuse_decision.validation_fingerprint or ''
        _finish_env_fingerprint = getattr(
            _reuse_decision, 'environment_fingerprint', None,
        ) or ''
        _finish_criteria = list(
            getattr(_reuse_decision, 'acceptance_criteria', None)
            or _finish_criteria
            or []
        )
        _finish_invalidated = list(
            getattr(_reuse_decision, 'invalidated_paths', None) or []
        )
        _badge['reuse_status'] = 'fresh'
        if _finish_reuse_reason:
            _badge['reuse_reason'] = _finish_reuse_reason
        if _finish_source_job:
            _badge['source_job_id'] = _finish_source_job
        if _finish_fingerprint:
            _badge['validation_fingerprint'] = _finish_fingerprint
        if _finish_env_fingerprint:
            _badge['environment_fingerprint'] = _finish_env_fingerprint
        if _finish_invalidated:
            _badge['invalidated_paths'] = _finish_invalidated
        if _finish_criteria:
            _badge['acceptance_criteria'] = list(_finish_criteria)
    elif _swarm_ok:
        _badge['reuse_status'] = 'fresh'
        if _finish_criteria:
            _badge['acceptance_criteria'] = list(_finish_criteria)
    try:
        session._finish_local_job(
            _sync_local_id, ok=_swarm_ok, summary=_badge_summary,
            status='done' if _swarm_ok else 'failed', engine=_job_engine,
            model=_job_model,
            findings=_job_findings,
            reuse_status=_finish_reuse_status if _swarm_ok else '',
            source_job_id=_finish_source_job,
            invalidated_paths=_finish_invalidated,
            reuse_reason=_finish_reuse_reason,
            validation_fingerprint=_finish_fingerprint,
            environment_fingerprint=_finish_env_fingerprint,
            acceptance_criteria=list(_finish_criteria),
        )
        if _store_jid != _sync_local_id:
            if _store_jid not in session._session_job_ids:
                session._session_job_ids.append(_store_jid)
            session._register_local_job(
                _store_jid, act.goal, role=_sync_register_role,
                cwd=_swarm_repo, engine=_job_engine,
                model=_job_model,
            )
            session._finish_local_job(
                _store_jid, ok=_swarm_ok, summary=_badge_summary,
                status='done' if _swarm_ok else 'failed', engine=_job_engine,
                model=_job_model,
                findings=_job_findings,
                reuse_status=_finish_reuse_status if _swarm_ok else '',
                source_job_id=_finish_source_job,
                invalidated_paths=_finish_invalidated,
                reuse_reason=_finish_reuse_reason,
                validation_fingerprint=_finish_fingerprint,
                environment_fingerprint=_finish_env_fingerprint,
                acceptance_criteria=list(_finish_criteria),
            )
    except Exception:
        pass
    session._display_transcript.append({'type': 'swarm_result', **_badge})
    yield ConvEvent('swarm_result', {'job_id': _badge['job_id'], 'objective': act.goal, 'result': _badge})
    # Only surfaced artifacts become turn findings; a refused demo contributes none.
    turn_findings.extend((a for a in _all_arts if a.get('type') != 'verification'))
    full_digest_raw = (getattr(act, 'arguments', None) or {}).get('full_digest')
    if isinstance(full_digest_raw, bool):
        want_full_digest = full_digest_raw
    else:
        want_full_digest = str(full_digest_raw or '').strip().lower() in (
            '1', 'true', 'yes', 'on',
        )
    digest = '\n'.join(
        digest_line(a, _job_id_text) for a in digest_arts
    ) or '  (no artifacts)'
    if auth_failure and not _has_signal and auth_failure not in digest:
        digest = f"  - [auth] {auth_failure}\n{digest}"
    from .worker_handles import format_handle_first_result
    handle_body = format_handle_first_result(
        _job_id_text,
        digest_arts or _signal or _all_arts,
    )
    body = digest if want_full_digest else handle_body
    stall = ''
    if _demo_refused or counters['demo_swarms'] >= 1:
        stall = '\n(NOTE: swarm hit the DEMO substrate and was refused -- Marionette never treats demo findings as real analysis. Do NOT retry or cite those findings. Tell the user analysis needs HARNESS_SWARM_ADAPTER=agentic and a provider key, then stop.)'
    if auth_failure:
        stall = f'\n(PROVIDER AUTH FAILURE -- {auth_failure} This is a dead/revoked/wrong API key, NOT a weak model or bad prompt. Do NOT re-run the swarm; tell the user to fix the named key, then stop.)' + stall
    elif not _has_signal:
        stall = '\n(DEGRADED SWARM — only routing/verification plumbing, no FINDING/RISK/DECISION. Tell the user the audit did not produce real findings. Re-dispatch with fewer roles or a sharper goal; do NOT claim the repo was reviewed.)' + stall
    elif not _substantive:
        stall = '\n(THIN SWARM FINDINGS — the findings above are generic one-liners with no file-backed evidence, a known failure mode when the goal is too long/multi-part for the workers. Do NOT present these as a completed audit. Re-dispatch narrowed workers with tight single-domain objectives.)' + stall
    _pilot_via = 'refused demo substrate' if _demo_refused else f'via {_ui_adapter}'
    evidence_boundary = render_evidence_boundary(build_swarm_run_facts(
        job_id=_job_id_text,
        job_status=str(getattr(result, 'status', '') or ''),
        subject_cwd=_swarm_repo,
        state_root=getattr(session, 'state_dir', '') or '',
        artifacts=_all_arts,
        acceptance_criteria=_finish_criteria,
    ))
    _follow = (
        'Explain these findings to the user and either run a narrowed follow-up '
        'swarm or finish with no actions. FETCH full bodies with peek_artifact '
        'or read_file on artifact:// URIs when needed.'
        if not want_full_digest
        else
        'Explain these findings to the user and either run a narrowed follow-up '
        'swarm or finish with no actions.'
    )
    session._append_action_result(
        act, aid,
        f"(swarm {aid} '{act.goal}' returned {_ui_num} artifacts {_pilot_via}:"
        f"{evidence_boundary}\n{body}\n{_follow}){stall}",
        is_native,
    )
    return None

def dispatch_implement_action(session, act, aid, is_native, *, turn_actions, action_idx, action_seq, step, swarms) -> Iterator[Any]:
    """Assemble tool-results for ``run_implement`` (peeled from ``_send_locked_inner``).

Yields the same ConvEvent stream. Generator return value is ``None``
(continue the action loop) or ``"return"`` (close the turn / exit send).
"""
    from .conversation import ConvEvent
    from .conversation import _prewarm_worker_imports
    _target_repo_override = ''
    if (getattr(act, 'repo', '') or '').strip():
        _abs, _err = session._validate_target_repo(act.repo)
        if _err:
            error_msg = f'run_implement: target repo {act.repo} is not a valid git repository'
            _cwd = resolve_effective_repo(session.config.repo or '') if (session.config.repo or '').strip() else None
            yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': _cwd})
            yield ConvEvent('action_result', {'id': aid, 'error': error_msg})
            session._append_action_result(act, aid, f'(run_implement {aid} failed: {error_msg})', is_native)
            return None
        _target_repo_override = _abs
    effective_repo = _target_repo_override or session.config.repo
    if effective_repo:
        effective_repo = resolve_effective_repo(effective_repo)
    if not effective_repo:
        error_msg = 'No workspace directory (config.repo) is open.'
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': None})
        yield ConvEvent('action_result', {'id': aid, 'error': error_msg})
        session._append_action_result(act, aid, f'(run_implement {aid} failed: {error_msg})', is_native)
        return None
    _non_git = _non_git_workspace_error(effective_repo)
    if _non_git:
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': effective_repo})
        yield ConvEvent('action_result', {'id': aid, 'error': _non_git})
        session._append_action_result(act, aid, f'(run_implement {aid} failed: {_non_git})', is_native)
        return None
    try:
        from harness.implement_guards import check_implement_workspace
        git_msg = check_implement_workspace(effective_repo, goal=act.goal or '')
    except Exception:
        git_msg = None
    if git_msg:
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': effective_repo})
        yield ConvEvent('action_result', {'id': aid, 'error': git_msg})
        session._append_action_result(act, aid, f'(run_implement {aid} refused: {git_msg})', is_native)
        return None
    try:
        from harness.implement_guards import check_oversized_single_file_rewrite
        fanout_msg = check_oversized_single_file_rewrite(act.goal, effective_repo)
    except Exception:
        fanout_msg = None
    if fanout_msg:
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': effective_repo})
        yield ConvEvent('action_result', {'id': aid, 'error': fanout_msg})
        session._append_action_result(act, aid, f'(run_implement {aid} refused by fan-out guard: {fanout_msg})', is_native)
        return None
    if not session._claim_objective(act.goal):
        dedup_msg = "An identical objective is already running in a background worker -- not dispatching a duplicate. Wait for the in-flight worker's patch instead of re-issuing the same edit; duplicate workers race the same files and cause PATCH-DID-NOT-APPLY."
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': effective_repo})
        yield ConvEvent('action_result', {'id': aid, 'status': 'skipped', 'message': dedup_msg})
        session._append_action_result(act, aid, f'(run_implement {aid} skipped -- duplicate objective already in flight)', is_native)
        return None
    claimed = True
    dispatched = False
    external_adapters = {'cursor', 'claude-code', 'codex', 'openai', 'hermes'}
    requested_adapter, adapter_remap_note = session._resolve_requested_implement_adapter(act.adapter or '')
    use_external = requested_adapter in external_adapters and _puppetmaster_available() and session._external_adapter_available(requested_adapter)
    if requested_adapter in external_adapters and (not use_external):
        if not adapter_remap_note:
            adapter_remap_note = f"adapter '{requested_adapter}' unavailable; using standalone agentic/native"
        requested_adapter = ''
    # Broad read-only audit/review goals must not silently default bare
    # run_implement to edit-capable implement mode — force analysis, or (when
    # the model already asked for implement on a clear audit) refuse with a
    # swarm/parallel-analysis redirect below.
    try:
        _requested_mode = (getattr(act, 'mode', None) or 'implement').strip().lower()
    except Exception:
        _requested_mode = 'implement'
    if _requested_mode not in ('implement', 'analysis', 'review'):
        _requested_mode = 'implement'
    _force_analysis = False
    if _requested_mode == 'implement':
        try:
            from harness.pilot_guards import is_read_only_analysis_goal
            _force_analysis = is_read_only_analysis_goal(act.goal or '')
        except Exception:
            _force_analysis = False

    if use_external:
        adapter = requested_adapter
        # External CLIs: refuse edit-capable implement for read-only audits and
        # redirect to swarm / parallel analysis (external path has no local
        # analysis-mode worker wiring here).
        if _force_analysis:
            refuse_msg = (
                'run_implement refused: this goal looks like a read-only '
                'audit/review. Re-dispatch with mode=analysis or mode=review '
                'on a provider worker, or use run_swarm / run_parallel with '
                'analysis roles instead of edit-capable implement mode.'
            )
            yield ConvEvent('action_start', {
                'id': aid, 'kind': 'run_implement', 'goal': act.goal,
                'cwd': effective_repo,
            })
            yield ConvEvent('action_result', {'id': aid, 'error': refuse_msg})
            session._append_action_result(
                act, aid, f'(run_implement {aid} refused: {refuse_msg})', is_native,
            )
            session._release_objective(act.goal)
            return None
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': effective_repo})
        try:
            import json
            cmd = _puppetmaster_cmd(adapter, act.goal, '--cwd', effective_repo, '--mode', 'implement', '--allow-dirty', '--allow-non-worktree', *session._job_dispatch_label_args())
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=effective_repo, encoding='utf-8', errors='replace')
            try:
                from harness.worktrees import bind_worktree_subprocess
                bind_worktree_subprocess(effective_repo, p, kind="worker")
            except Exception:
                pass
            job_id = None
            all_output_lines = []
            try:
                for line in p.stdout:
                    all_output_lines.append(line)
                    if not job_id:
                        match = re.search('\\b(job_[a-fA-F0-9]{12})\\b', line)
                        if match:
                            job_id = match.group(1)
                p.wait(timeout=600)
            finally:
                try:
                    from harness.worktrees import release_worktree_subprocess
                    release_worktree_subprocess(effective_repo, p)
                except Exception:
                    pass
            if job_id:
                session._session_job_ids.append(job_id)
                if not session._submit_swarm(session._run_swarm_background, job_id, act.goal, None):
                    cap_msg = session._swarm_submit_reject_message()
                    session._release_objective(act.goal)
                    yield ConvEvent('action_result', {'id': aid, 'error': cap_msg})
                    session._append_action_result(act, aid, f'(run_implement {aid} deferred: {cap_msg})', is_native)
                    return None
                dispatched = True
                yield ConvEvent('swarm_pending', {'job_ids': [job_id], 'objective': act.goal})
                yield ConvEvent('action_result', {'id': aid, 'job_id': job_id, 'status': 'pending', 'message': f'Dispatched background swarm job {job_id}'})
                session._append_action_result(act, aid, f'(run_implement {aid} dispatched in background: job {job_id}' + (f'; {adapter_remap_note}' if adapter_remap_note else '') + ')', is_native)
                yield from session._answer_remaining_tool_calls(turn_actions, action_idx, is_native, action_seq)
                yield ConvEvent('assistant_done', {'turns': step + 1, 'swarms': swarms + 1})
                return 'return'
            else:
                session._release_objective(act.goal)
                output = ''.join(all_output_lines)[:5000]
                yield ConvEvent('action_result', {'id': aid, 'error': f'Failed to detect job_id. CLI output:\n{output}'})
                session._append_action_result(act, aid, f'(run_implement {aid} failed: no job_id detected. Output:\n{output})', is_native)
        except Exception as e:
            if claimed and (not dispatched):
                session._release_objective(act.goal)
            yield ConvEvent('action_result', {'id': aid, 'error': str(e)})
            session._append_action_result(act, aid, f'(run_implement {aid} failed: {e})', is_native)
        return None
    else:
        from harness.edit_engines import select_edit_engine
        engine = select_edit_engine(session.config, requested_adapter)
        _mode = _requested_mode
        if _force_analysis:
            _mode = 'analysis'
        expects_diff = _mode not in ('analysis', 'review')
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': effective_repo, 'mode': engine})
        try:
            import uuid
            short = uuid.uuid4().hex[:8]
            job_id = f'local-{short}'
            try:
                session._register_local_job(
                    job_id, act.goal, role=_mode, cwd=effective_repo, engine=engine,
                    model=session.config.driver or '' if engine == 'native' else '',
                )
            except Exception as e:
                session._release_objective(act.goal)
                err = f'tracker register failed: {e}'
                yield ConvEvent('action_result', {'id': aid, 'error': err})
                session._append_action_result(act, aid, f'(run_implement {aid} failed: {err})', is_native)
                return None
            session._session_job_ids.append(job_id)
            _prewarm_worker_imports()
            if not session._submit_swarm(session._run_provider_worker_background, job_id, act.goal, requested_adapter, effective_repo, expects_diff):
                cap_msg = session._swarm_submit_reject_message()
                session._release_objective(act.goal)
                yield ConvEvent('action_result', {'id': aid, 'status': 'deferred', 'message': cap_msg})
                session._append_action_result(act, aid, f'(run_implement {aid} deferred: {cap_msg})', is_native)
                return None
            dispatched = True
            yield ConvEvent('swarm_pending', {'job_ids': [job_id], 'objective': act.goal})
            dispatch_msg = f'Dispatched background swarm job {job_id}'
            if adapter_remap_note:
                dispatch_msg = f'{dispatch_msg} ({adapter_remap_note})'
            if _force_analysis:
                dispatch_msg = (
                    f'{dispatch_msg} (forced mode=analysis for read-only '
                    'audit/review goal; use run_swarm for multi-role coverage)'
                )
            yield ConvEvent('action_result', {'id': aid, 'job_id': job_id, 'status': 'pending', 'message': dispatch_msg})
            session._append_action_result(act, aid, f'(run_implement {aid} dispatched in background: job {job_id}' + (f'; {adapter_remap_note}' if adapter_remap_note else '') + ')', is_native)
            yield from session._answer_remaining_tool_calls(turn_actions, action_idx, is_native, action_seq)
            yield ConvEvent('assistant_done', {'turns': step + 1, 'swarms': swarms + 1})
            return 'return'
        except Exception as e:
            if claimed and (not dispatched):
                session._release_objective(act.goal)
            yield ConvEvent('action_result', {'id': aid, 'error': str(e)})
            session._append_action_result(act, aid, f'(run_implement {aid} failed: {e})', is_native)
        return None
    return None

def dispatch_parallel_action(session, act, aid, is_native, *, turn_actions, action_idx, action_seq, step, swarms) -> Iterator[Any]:
    """Assemble tool-results for ``run_parallel`` (peeled from ``_send_locked_inner``).

Yields the same ConvEvent stream. Generator return value is ``None``
(continue the action loop) or ``"return"`` (close the turn / exit send).
"""
    from .conversation import ConvEvent
    from .conversation import _prewarm_worker_imports
    _target_repo_override = ''
    if (getattr(act, 'repo', '') or '').strip():
        _abs, _err = session._validate_target_repo(act.repo)
        if _err:
            error_msg = f'run_parallel: target repo {act.repo} is not a valid git repository'
            yield ConvEvent('action_result', {'id': aid, 'error': error_msg})
            session._append_action_result(act, aid, f'(run_parallel {aid} failed: {error_msg})', is_native)
            return None
        _target_repo_override = _abs
    effective_repo = _target_repo_override or session.config.repo
    if effective_repo:
        effective_repo = resolve_effective_repo(effective_repo)
    if not effective_repo:
        error_msg = 'No workspace directory (config.repo) is open.'
        yield ConvEvent('action_result', {'id': aid, 'error': error_msg})
        session._append_action_result(act, aid, f'(run_parallel {aid} failed: {error_msg})', is_native)
        return None
    goals = act.goals or []
    if not goals:
        yield ConvEvent('action_result', {'id': aid, 'error': 'run_parallel requires a non-empty goals array'})
        session._append_action_result(act, aid, f'(run_parallel {aid} failed: run_parallel requires a non-empty goals array)', is_native)
        return None
    _non_git = _non_git_workspace_error(effective_repo)
    if _non_git:
        # Pair with action_start so chrome never shows a hanging start.
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_parallel', 'goals': goals, 'cwd': effective_repo})
        yield ConvEvent('action_result', {'id': aid, 'error': _non_git})
        session._append_action_result(act, aid, f'(run_parallel {aid} failed: {_non_git})', is_native)
        return None
    try:
        from harness.implement_guards import check_implement_workspace
        git_msg = check_implement_workspace(effective_repo, goal='; '.join(goals[:3]))
    except Exception:
        git_msg = None
    if git_msg:
        yield ConvEvent('action_result', {'id': aid, 'error': git_msg})
        session._append_action_result(act, aid, f'(run_parallel {aid} refused: {git_msg})', is_native)
        return None
    MAX_PARALLEL_CAP = 8
    if len(goals) > MAX_PARALLEL_CAP:
        goals = goals[:MAX_PARALLEL_CAP]
    try:
        from harness.implement_guards import check_oversized_single_file_rewrite
        kept_goals = []
        refused_goals = []
        for g in goals:
            msg = check_oversized_single_file_rewrite(g, effective_repo)
            if msg:
                refused_goals.append((g, msg))
            else:
                kept_goals.append(g)
        if refused_goals:
            for g, msg in refused_goals:
                yield ConvEvent('notice', {'message': f'Fan-out guard refused goal: {msg}'})
            goals = kept_goals
        if not goals:
            err = 'run_parallel: every goal was refused by the fan-out guard (oversized single-file rewrite). Split each file into sectioned run_parallel goals.'
            yield ConvEvent('action_result', {'id': aid, 'error': err})
            session._append_action_result(act, aid, f'(run_parallel {aid} failed: {err})', is_native)
            return None
    except Exception:
        pass
    external_adapters = {'cursor', 'claude-code', 'codex', 'openai', 'hermes'}
    requested_adapter, adapter_remap_note = session._resolve_requested_implement_adapter(act.adapter or '')
    use_external = requested_adapter in external_adapters and _puppetmaster_available() and session._external_adapter_available(requested_adapter)
    if requested_adapter in external_adapters and (not use_external):
        if not adapter_remap_note:
            adapter_remap_note = f"adapter '{requested_adapter}' unavailable; using standalone agentic/native"
        requested_adapter = ''
    if use_external:
        adapter = requested_adapter
        try:
            mode = (act.mode or 'implement').strip().lower() or 'implement'
        except Exception:
            mode = 'implement'
        # Parent card first so reload/hydrate never sees orphan child results.
        yield ConvEvent('action_start', {
            'id': aid, 'kind': 'run_parallel', 'goals': goals,
            'cwd': effective_repo, 'mode': adapter,
        })
        sub_aids = []
        for idx, sub_goal in enumerate(goals):
            sub_aid = f'{aid}_sub_{idx}'
            sub_aids.append(sub_aid)
            yield ConvEvent('action_start', {
                'id': sub_aid, 'kind': f'run_{mode}', 'goal': sub_goal,
                'cwd': effective_repo, 'parent_id': aid,
            })
        # Track which cards already got an action_result so an unexpected
        # exception (or a continue that skipped the parent) settles every
        # started id with the REAL error — never the opaque
        # "missing action_result" turn-end sweep.
        emitted_results = set()

        def _result(data):
            rid = data.get('id')
            if rid:
                emitted_results.add(rid)
            return ConvEvent('action_result', data)

        def _settle_unfinished(err_msg):
            for sub_aid in sub_aids:
                if sub_aid not in emitted_results:
                    yield ConvEvent('action_result', {'id': sub_aid, 'error': err_msg})
                    emitted_results.add(sub_aid)
            if aid not in emitted_results:
                yield ConvEvent('action_result', {'id': aid, 'error': err_msg})
                emitted_results.add(aid)
                try:
                    session._append_action_result(
                        act, aid, f'(run_parallel {aid} failed: {err_msg})', is_native,
                    )
                except Exception:
                    pass

        try:
            import threading
            import tempfile
            import shutil
            processes = []
            threads = []
            for idx, sub_goal in enumerate(goals):
                sub_aid = sub_aids[idx]
                try:
                    state_dir = tempfile.mkdtemp(prefix='pmh-par-')
                except Exception as e:
                    yield _result({'id': sub_aid, 'error': f'Failed to create temp state-dir: {e}'})
                    continue
                cmd = _puppetmaster_cmd('--state-dir', state_dir, adapter, sub_goal, '--cwd', effective_repo, '--mode', mode, '--allow-dirty', '--allow-non-worktree', *session._job_dispatch_label_args())
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=effective_repo, encoding='utf-8', errors='replace')
                    try:
                        from harness.worktrees import bind_worktree_subprocess
                        bind_worktree_subprocess(effective_repo, proc, kind="worker")
                    except Exception:
                        pass
                    p_info = {'proc': proc, 'goal': sub_goal, 'id': sub_aid, 'job_id': None, 'lines': [], 'state_dir': state_dir}
                    processes.append(p_info)
                    t = threading.Thread(target=read_stdout_thread, args=(p_info,), daemon=True)
                    t.start()
                    threads.append(t)
                except Exception as e:
                    yield _result({'id': sub_aid, 'error': f'Failed to start: {e}'})
                    shutil.rmtree(state_dir, ignore_errors=True)
            for p_info in processes:
                try:
                    try:
                        p_info['proc'].wait(timeout=600)
                    except subprocess.TimeoutExpired:
                        p_info['proc'].kill()
                        p_info['proc'].wait()
                finally:
                    try:
                        from harness.worktrees import release_worktree_subprocess
                        release_worktree_subprocess(effective_repo, p_info['proc'])
                    except Exception:
                        pass
            for t in threads:
                t.join(timeout=5)
            aggregate_artifacts_summary = []
            job_ids_collected = []
            for idx, p_info in enumerate(processes):
                sub_aid = p_info['id']
                sub_goal = p_info['goal']
                state_dir = p_info.get('state_dir')
                try:
                    job_id = p_info['job_id']
                    if not job_id and state_dir:
                        try:
                            last_cmd = _puppetmaster_cmd('--state-dir', state_dir, 'last')
                            last_p = subprocess.run(last_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', timeout=10)
                            if last_p.returncode == 0:
                                last_out = last_p.stdout or ''
                                m = re.search('\\b(job_[a-fA-F0-9]{12})\\b', last_out)
                                if m:
                                    p_info['job_id'] = m.group(1)
                                    job_id = p_info['job_id']
                        except Exception:
                            pass
                    if job_id:
                        if not session._submit_swarm(
                            session._run_swarm_background,
                            job_id,
                            sub_goal,
                            state_dir,
                            admission_group=f"parallel-{aid}",
                            admission_size=len(goals),
                        ):
                            cap_msg = session._swarm_submit_reject_message()
                            yield _result({'id': sub_aid, 'status': 'deferred', 'message': cap_msg})
                            aggregate_artifacts_summary.append(f"Sub-worker for '{sub_goal}' deferred: {cap_msg}")
                            continue
                        job_ids_collected.append(job_id)
                        session._session_job_ids.append(job_id)
                        p_info['state_dir'] = None
                        yield _result({'id': sub_aid, 'job_id': job_id, 'status': 'pending', 'message': f'Dispatched parallel background swarm job {job_id}'})
                    else:
                        ret_code = p_info['proc'].returncode
                        output_text = ''.join(p_info['lines'])
                        lower_out = output_text.lower()
                        has_success_marker = any((m in lower_out for m in ['success', 'complete', 'finished', 'done', 'written', 'saved']))
                        if ret_code != 0:
                            err_msg = f'worker process failed (exit {ret_code})'
                        elif has_success_marker:
                            err_msg = 'worker completed but job_id unrecoverable'
                        else:
                            err_msg = 'worker completed but job_id unrecoverable (no success marker found)'
                        yield _result({'id': sub_aid, 'error': err_msg})
                        aggregate_artifacts_summary.append(f"Sub-worker for '{sub_goal}' failed: {err_msg}")
                finally:
                    if p_info.get('state_dir'):
                        import shutil
                        shutil.rmtree(p_info['state_dir'], ignore_errors=True)
            # Settle any sub card that never entered the process list.
            for sub_aid in sub_aids:
                if sub_aid not in emitted_results:
                    yield _result({'id': sub_aid, 'error': 'No jobs successfully dispatched'})
            if job_ids_collected:
                yield ConvEvent('swarm_pending', {'job_ids': job_ids_collected, 'objective': f"Parallel wave of goals: {', '.join(goals)}"})
                yield _result({'id': aid, 'job_id': ','.join(job_ids_collected), 'status': 'pending', 'message': f"Dispatched parallel background swarm jobs: {', '.join(job_ids_collected)}"})
                session._append_action_result(act, aid, f"(run_parallel dispatched {len(job_ids_collected)} jobs in background: {', '.join(job_ids_collected)})", is_native)
                yield from session._answer_remaining_tool_calls(turn_actions, action_idx, is_native, action_seq)
                yield ConvEvent('assistant_done', {'turns': step + 1, 'swarms': swarms + len(job_ids_collected)})
                return 'return'
            yield _result({'id': aid, 'error': 'No jobs successfully dispatched'})
            session._append_action_result(act, aid, f'(run_parallel failed to dispatch any jobs)', is_native)
            return None
        except Exception as e:
            err = str(e) or e.__class__.__name__
            yield from _settle_unfinished(err)
            return None
    else:
        from harness.edit_engines import select_edit_engine
        engine = select_edit_engine(session.config, requested_adapter)
        try:
            _mode = (getattr(act, 'mode', None) or 'implement').strip().lower()
        except Exception:
            _mode = 'implement'
        if _mode not in ('implement', 'analysis', 'review'):
            _mode = 'implement'
        expects_diff = _mode not in ('analysis', 'review')
        yield ConvEvent('action_start', {'id': aid, 'kind': 'run_parallel', 'goals': goals, 'cwd': effective_repo, 'mode': engine})
        try:
            import uuid
            _prewarm_worker_imports()
            job_ids_collected = []
            skipped_goals = []
            deferred_goals = []
            reused_goals = []
            # Buffer reused terminal results until after swarm_pending so the
            # multi-job pill exists before sibling swarm_result frames arrive.
            # Frontend also seeds terminal_job_ids on out-of-order/reattach.
            buffered_reuse_events = []
            _parallel_admission = f"parallel-{aid}"
            _reuse_mod = None
            if _mode in ('analysis', 'review'):
                try:
                    from harness import validation_reuse as _reuse_mod
                except Exception:
                    _reuse_mod = None
            _parallel_criteria = list(getattr(act, 'acceptance_criteria', None) or [])
            if not _parallel_criteria and isinstance(getattr(act, 'arguments', None), dict):
                try:
                    from harness.environment_fingerprint import normalize_acceptance_criteria
                    _parallel_criteria = normalize_acceptance_criteria(
                        act.arguments.get('acceptance_criteria')
                    )
                except Exception:
                    _parallel_criteria = []
            for sub_goal in goals:
                if not session._claim_objective(sub_goal):
                    skipped_goals.append(sub_goal)
                    continue
                # Analysis/review: skip adapter dispatch when a matching green
                # prior job is still valid for this objective.
                if _reuse_mod is not None and _mode in ('analysis', 'review'):
                    try:
                        decision = _reuse_mod.evaluate_reuse_gate(
                            session,
                            objective=sub_goal,
                            role=_mode,
                            cwd=effective_repo,
                            acceptance_criteria=_parallel_criteria,
                        )
                    except Exception:
                        decision = None
                    if decision is not None and decision.outcome == 'reuse':
                        short = uuid.uuid4().hex[:8]
                        job_id = f'local-{short}'
                        _reuse_registered = False
                        try:
                            session._register_local_job(
                                job_id, sub_goal, role=_mode, cwd=effective_repo,
                                engine=engine,
                                model=session.config.driver or '' if engine == 'native' else '',
                                skip_routing_preview=True,
                            )
                            _reuse_registered = True
                            session._finish_local_job(
                                job_id,
                                ok=True,
                                summary=(
                                    f"reused {decision.source_job_id} "
                                    f"({decision.reason})"
                                )[:200],
                                status='done',
                                engine=engine,
                                tokens=0,
                                est_cost_usd=0.0,
                                findings=[
                                    {
                                        'type': a.get('type') or 'finding',
                                        'headline': a.get('headline') or a.get('uri') or '',
                                        'id': a.get('id'),
                                    }
                                    for a in (decision.compact_artifacts or [])
                                ],
                                reuse_status='reused',
                                source_job_id=decision.source_job_id,
                                validation_fingerprint=decision.validation_fingerprint,
                                invalidated_paths=list(decision.invalidated_paths or []),
                                reuse_reason=decision.reason,
                                environment_fingerprint=getattr(
                                    decision, 'environment_fingerprint', None,
                                ) or '',
                                acceptance_criteria=list(
                                    getattr(decision, 'acceptance_criteria', None)
                                    or _parallel_criteria
                                    or []
                                ),
                            )
                            session._session_job_ids.append(job_id)
                            job_ids_collected.append(job_id)
                            reused_goals.append((sub_goal, decision))
                            # Terminal swarm_result before assistant_done so
                            # pending pills settle and SwarmResultCard paints.
                            # Yield after swarm_pending (buffered below).
                            _prov = decision.as_provenance()
                            _badge = {
                                'job_id': job_id,
                                'applied': True,
                                'files': [],
                                'summary': (
                                    f"reused prior analysis from "
                                    f"{decision.source_job_id}"
                                ),
                                'error': None,
                                'objective': sub_goal,
                                'adapter': 'reuse',
                                **_prov,
                            }
                            session._display_transcript.append(
                                {'type': 'swarm_result', **_badge}
                            )
                            buffered_reuse_events.append(ConvEvent('swarm_result', {
                                'job_id': job_id,
                                'objective': sub_goal,
                                'result': _badge,
                            }))
                        except Exception as e:
                            # Fail closed: do not buffer/yield a green reused
                            # swarm_result when the durable tracker write fails.
                            # Settle/drop an already-registered orphan so the
                            # panel does not keep a live spinner.
                            if _reuse_registered:
                                try:
                                    session._fail_or_drop_local_job(
                                        job_id,
                                        summary=f'tracker finish failed: {e}',
                                    )
                                except Exception:
                                    pass
                            session._release_objective(sub_goal)
                            err = f'tracker register/finish failed: {e}'
                            yield ConvEvent('action_result', {'id': aid, 'error': err})
                            session._append_action_result(
                                act, aid,
                                f'(run_parallel {aid} failed: {err})',
                                is_native, ok=False,
                            )
                            return None
                        continue
                    if decision is not None and decision.outcome == 'narrow_verify':
                        # Narrow the worker goal; force verifier-only roles.
                        sub_goal = (
                            f"{sub_goal}\n\n{decision.narrow_goal_suffix}"
                            if decision.narrow_goal_suffix else sub_goal
                        )
                        narrow_role = (
                            list(decision.narrow_roles)[0]
                            if decision.narrow_roles
                            else 'conflict-auditor'
                        )
                        short = uuid.uuid4().hex[:8]
                        job_id = f'local-{short}'
                        try:
                            session._register_local_job(
                                job_id, sub_goal, role=narrow_role,
                                cwd=effective_repo, engine=engine,
                                model=session.config.driver or '' if engine == 'native' else '',
                            )
                            # Pre-stamp partial reuse so background finish/drain
                            # keep invalidated_paths + source lineage.
                            try:
                                with session._local_jobs_lock:
                                    _nj = session._local_jobs.get(job_id)
                                    if isinstance(_nj, dict):
                                        _nj['reuse_status'] = 'partial'
                                        _nj['source_job_id'] = decision.source_job_id
                                        _nj['validation_fingerprint'] = (
                                            decision.validation_fingerprint or ''
                                        )
                                        _nj['invalidated_paths'] = list(
                                            decision.invalidated_paths or []
                                        )
                                        _nj['reuse_reason'] = decision.reason
                                        _env_fp = getattr(
                                            decision, 'environment_fingerprint', None,
                                        ) or ''
                                        if _env_fp:
                                            _nj['environment_fingerprint'] = _env_fp
                                        _nj['acceptance_criteria'] = list(
                                            getattr(decision, 'acceptance_criteria', None)
                                            or _parallel_criteria
                                            or []
                                        )
                            except Exception:
                                pass
                            submitted = session._submit_swarm(
                                session._run_provider_worker_background,
                                job_id,
                                sub_goal,
                                requested_adapter,
                                effective_repo,
                                expects_diff,
                                admission_group=_parallel_admission,
                                admission_size=len(goals),
                            )
                        except Exception:
                            session._release_objective(sub_goal)
                            raise
                        if not submitted:
                            session._release_objective(sub_goal)
                            deferred_goals.append(sub_goal)
                            if session._last_swarm_submit_reason == "resource_pressure":
                                break
                            continue
                        job_ids_collected.append(job_id)
                        session._session_job_ids.append(job_id)
                        continue
                    if (
                        decision is not None
                        and decision.outcome == 'full_swarm'
                        and decision.reason
                        and decision.reason != 'first_pass'
                    ):
                        # Keep the gate rejection reason on the fresh job for
                        # transcript/UI (environment_changed, etc.).
                        short = uuid.uuid4().hex[:8]
                        job_id = f'local-{short}'
                        worker_goal = sub_goal
                        _crit = list(
                            getattr(decision, 'acceptance_criteria', None)
                            or _parallel_criteria
                            or []
                        )
                        if _crit:
                            try:
                                from harness.environment_fingerprint import (
                                    format_acceptance_criteria_block,
                                )
                                _block = format_acceptance_criteria_block(_crit)
                                if _block and _block not in worker_goal:
                                    worker_goal = f"{sub_goal}\n\n{_block}"
                            except Exception:
                                worker_goal = sub_goal
                        try:
                            session._register_local_job(
                                job_id, sub_goal, role=_mode,
                                cwd=effective_repo, engine=engine,
                                model=session.config.driver or '' if engine == 'native' else '',
                            )
                            try:
                                with session._local_jobs_lock:
                                    _nj = session._local_jobs.get(job_id)
                                    if isinstance(_nj, dict):
                                        _nj['reuse_status'] = 'fresh'
                                        _nj['reuse_reason'] = decision.reason
                                        if decision.source_job_id:
                                            _nj['source_job_id'] = decision.source_job_id
                                        if decision.validation_fingerprint:
                                            _nj['validation_fingerprint'] = (
                                                decision.validation_fingerprint
                                            )
                                        _env_fp = getattr(
                                            decision, 'environment_fingerprint', None,
                                        ) or ''
                                        if _env_fp:
                                            _nj['environment_fingerprint'] = _env_fp
                                        _inv = getattr(
                                            decision, 'invalidated_paths', None,
                                        ) or []
                                        if _inv:
                                            _nj['invalidated_paths'] = list(_inv)
                                        _nj['acceptance_criteria'] = list(_crit)
                            except Exception:
                                pass
                            submitted = session._submit_swarm(
                                session._run_provider_worker_background,
                                job_id,
                                worker_goal,
                                requested_adapter,
                                effective_repo,
                                expects_diff,
                                admission_group=_parallel_admission,
                                admission_size=len(goals),
                            )
                        except Exception:
                            session._release_objective(sub_goal)
                            raise
                        if not submitted:
                            session._release_objective(sub_goal)
                            deferred_goals.append(sub_goal)
                            if session._last_swarm_submit_reason == "resource_pressure":
                                break
                            continue
                        job_ids_collected.append(job_id)
                        session._session_job_ids.append(job_id)
                        continue
                short = uuid.uuid4().hex[:8]
                job_id = f'local-{short}'
                worker_goal = sub_goal
                if _parallel_criteria and _mode in ('analysis', 'review'):
                    try:
                        from harness.environment_fingerprint import (
                            format_acceptance_criteria_block,
                        )
                        _block = format_acceptance_criteria_block(_parallel_criteria)
                        if _block and _block not in worker_goal:
                            worker_goal = f"{sub_goal}\n\n{_block}"
                    except Exception:
                        worker_goal = sub_goal
                try:
                    session._register_local_job(
                        job_id, sub_goal, role=_mode, cwd=effective_repo,
                        engine=engine,
                        model=session.config.driver or '' if engine == 'native' else '',
                    )
                    if _parallel_criteria and _mode in ('analysis', 'review'):
                        try:
                            with session._local_jobs_lock:
                                _nj = session._local_jobs.get(job_id)
                                if isinstance(_nj, dict):
                                    _nj['acceptance_criteria'] = list(_parallel_criteria)
                        except Exception:
                            pass
                    submitted = session._submit_swarm(
                        session._run_provider_worker_background,
                        job_id,
                        worker_goal,
                        requested_adapter,
                        effective_repo,
                        expects_diff,
                        admission_group=_parallel_admission,
                        admission_size=len(goals),
                    )
                except Exception:
                    session._release_objective(sub_goal)
                    raise
                if not submitted:
                    session._release_objective(sub_goal)
                    deferred_goals.append(sub_goal)
                    if session._last_swarm_submit_reason == "resource_pressure":
                        break
                    continue
                job_ids_collected.append(job_id)
                session._session_job_ids.append(job_id)
            if deferred_goals:
                cap_msg = session._swarm_submit_reject_message()
                if session._last_swarm_submit_reason != "resource_pressure":
                    cap_msg = (
                        f'{cap_msg} deferred {len(deferred_goals)} of {len(goals)} goal(s): '
                        + ', '.join(deferred_goals)
                    )
                yield ConvEvent('notice', {'message': cap_msg})
            if not job_ids_collected:
                skip_msg = 'All parallel objectives are already running in background workers -- nothing new dispatched. Wait for the in-flight workers rather than re-issuing them.'
                yield ConvEvent('action_result', {'id': aid, 'status': 'skipped', 'message': skip_msg})
                session._append_action_result(act, aid, f'(run_parallel {aid} skipped -- all {len(goals)} objectives already in flight)', is_native)
                return None
            yield ConvEvent('swarm_pending', {'job_ids': job_ids_collected, 'objective': f"Parallel wave of goals: {', '.join(goals)}"})
            for _reuse_ev in buffered_reuse_events:
                yield _reuse_ev
            _reuse_note = ''
            if reused_goals:
                _reuse_note = (
                    f"; reused {len(reused_goals)} prior validation(s) with zero new spend: "
                    + ', '.join(
                        f"{g[:40]}→{d.source_job_id}" for g, d in reused_goals[:4]
                    )
                )
            yield ConvEvent('action_result', {
                'id': aid,
                'job_id': ','.join(job_ids_collected),
                'status': 'pending' if len(reused_goals) < len(job_ids_collected) else 'reused',
                'message': (
                    f"Dispatched parallel background swarm jobs: "
                    f"{', '.join(job_ids_collected)}{_reuse_note}"
                ),
                'reuse_status': 'reused' if reused_goals and len(reused_goals) == len(job_ids_collected) else (
                    'partial' if reused_goals else 'fresh'
                ),
            })
            session._append_action_result(
                act, aid,
                f"(run_parallel {aid} dispatched {len(job_ids_collected)} jobs"
                f"{_reuse_note}: {', '.join(job_ids_collected)})",
                is_native,
            )
            yield from session._answer_remaining_tool_calls(turn_actions, action_idx, is_native, action_seq)
            yield ConvEvent('assistant_done', {'turns': step + 1, 'swarms': swarms + len(job_ids_collected)})
            return 'return'
        except Exception as e:
            err = str(e) or e.__class__.__name__
            try:
                yield ConvEvent('action_result', {'id': aid, 'error': err})
            except Exception:
                pass
            try:
                session._append_action_result(
                    act, aid, f'(run_parallel {aid} failed: {err})', is_native, ok=False,
                )
            except Exception:
                pass
        return None
    return None

def dispatch_route_task_action(session, act, aid, is_native) -> Iterator[Any]:
    """Assemble tool-results for ``route_task`` (peeled from ``_send_locked_inner``).

Yields the same ConvEvent stream. Generator return value is ``None``
(continue the action loop) or ``"return"`` (close the turn / exit send).
"""
    from .conversation import ConvEvent
    if not _puppetmaster_available():
        error_msg = 'puppetmaster CLI not available in this environment'
        yield ConvEvent('action_result', {'id': aid, 'error': error_msg})
        session._append_action_result(act, aid, f'(route_task {aid} failed: {error_msg})', is_native)
        return None
    instruction = act.instruction or act.arguments.get('instruction') or ''
    role = act.arguments.get('role') or 'explore'
    try:
        import json
        cmd = _puppetmaster_cmd('route', instruction, '--role', role, '--json')
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', timeout=60)
        output = p.stdout or ''
        if p.returncode != 0:
            raise Exception(f'Exit code {p.returncode}: {output}')
        route_data = json.loads(output)
        model_id = route_data.get('model_id') or 'unknown'
        adapter = route_data.get('adapter') or 'unknown'
        cost = route_data.get('nominal_cost_usd', 0.0) or route_data.get('estimated_cost_usd', 0.0)
        reason = route_data.get('reason') or 'No reasoning provided.'
        res_str = f'**Routed Model**: {model_id} (via {adapter})\n**Estimated Cost**: ${cost:.6f}\n**Reasoning**: {reason}'
        yield ConvEvent('action_result', {'id': aid, 'num': 1, 'types': ['route_task'], 'adapter': 'local', 'mode': 'tool', 'artifacts': [{'type': 'route_task', 'headline': f'Routed to {model_id} (${cost:.6f})'}]})
        session._append_action_result(act, aid, f"(route_task for '{instruction}' returned):\n{res_str}", is_native)
    except Exception as e:
        yield ConvEvent('action_result', {'id': aid, 'error': str(e)})
        session._append_action_result(act, aid, f"(route_task for '{instruction}' failed: {e})", is_native)
    return None
    return None

def dispatch_memory_action(session, act, aid, is_native) -> Iterator[Any]:
    """Assemble tool-results for ``memory`` (peeled from ``_send_locked_inner``).

Yields the same ConvEvent stream. Generator return value is ``None``
(continue the action loop) or ``"return"`` (close the turn / exit send).
"""
    from .conversation import ConvEvent
    try:
        op = act.memory_action
        if op == 'add':
            if session._auto_mode:
                res_str = 'Memory add ignored: durable-memory proposals are disabled in Autopilot (unattended). Use Settings > Agent Memory for manual adds, or run interactively.'
            else:
                text = (act.memory_content or '').strip()
                cat = (act.memory_category or 'general').strip() or 'general'
                if not text:
                    raise ValueError('memory add requires content')
                already = any(((q.get('text') or '').strip().lower() == text.lower() for q in session._turn_memory_queue))
                if _MEMORY_SECRET_RE.search(text):
                    res_str = (
                        "Refused: memory add looks like it contains secrets "
                        "(API keys, tokens, or passwords). Do not save credentials "
                        "to durable memory."
                    )
                elif already:
                    res_str = f"Already queued for end-of-turn Save/Skip: '{text}' (category: {cat}). Not persisted yet."
                else:
                    session._turn_memory_queue.append({'text': text, 'category': cat})
                    res_str = f"Queued for end-of-turn Save/Skip (not persisted yet): '{text}' (category: {cat}). The user will confirm after this turn finishes."
        elif op == 'remove':
            ok = session._memory.remove(act.memory_id)
            if ok:
                res_str = f'Successfully removed memory entry with ID {act.memory_id}.'
            else:
                res_str = f'Error: memory entry with ID {act.memory_id} not found.'
        elif op == 'update':
            ok = session._memory.update(act.memory_id, act.memory_content)
            if ok:
                res_str = f"Successfully updated memory entry {act.memory_id} to: '{act.memory_content}'"
            else:
                res_str = f'Error: memory entry with ID {act.memory_id} not found.'
        elif op == 'list':
            entries = session._memory.list()
            if entries:
                items = '\n'.join((f'- [{e.id}] ({e.category}): {e.text}' for e in entries))
                res_str = f'Durable memory entries:\n{items}'
            else:
                res_str = 'Durable memory is empty.'
        else:
            raise ValueError(f'Unknown memory action: {op}')
        yield ConvEvent('action_result', {'id': aid, 'num': 1, 'types': ['memory'], 'adapter': 'local', 'mode': 'tool', 'artifacts': [{'type': 'memory', 'headline': f'Memory {op} succeeded'}]})
        session._append_action_result(act, aid, res_str, is_native)
    except Exception as e:
        yield ConvEvent('action_result', {'id': aid, 'error': str(e)})
        session._append_action_result(act, aid, f'(memory tool execution failed: {e})', is_native)
    return None
    return None
