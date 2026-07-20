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
from typing import Any, Iterator

from pmharness.intent import DriverIntent

from ._exec import _puppetmaster_available, _puppetmaster_cmd
from .repo_resolve import resolve_effective_repo
from .send_loop_phases import read_stdout_thread, stream_swarm

DISPATCH_ACTION_KINDS: frozenset[str] = frozenset({
    "run_swarm", "run_implement", "run_parallel", "route_task", "memory",
})

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
                _looks_like_reasoning_fragment,
            )
            if _is_meta_degrade_artifact(a):
                return False
            text = str(a.get('body') or a.get('headline') or '').strip()
            if _looks_like_reasoning_fragment(text):
                return False
        except Exception:
            text = str(a.get('body') or a.get('headline') or '').strip()
        if len(text) >= 200:
            return True
        return len(text) >= 40 and bool(_PATH_REF_RE.search(text))
    except Exception:
        return True


def dispatch_swarm_action(session, act, aid, is_native, *, counters, turn_findings) -> Iterator[Any]:
    """Assemble tool-results for ``run_swarm`` (peeled from ``_send_locked_inner``).

Yields the same ConvEvent stream. Generator return value is ``None``
(continue the action loop) or ``"return"`` (close the turn / exit send).
"""
    from .conversation import ConvEvent
    intent = DriverIntent(action='run_swarm', goal=act.goal, roles=act.roles or None, rationale='pilot')
    _sync_local_id = f'local-swarm-{aid}'
    _swarm_repo = resolve_effective_repo(session.config.repo or '') if (session.config.repo or '').strip() else ''
    try:
        session._register_local_job(_sync_local_id, act.goal, role='explore', cwd=_swarm_repo, engine='agentic')
        session._session_job_ids.append(_sync_local_id)
    except Exception:
        pass
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
    _all_arts = list(result.artifacts)
    # Reasoning-only fragments must never appear as finding/risk/decision
    # headlines in the digest (same submit contract as swarm workers).
    try:
        from pmharness.bridge import _looks_like_reasoning_fragment as _reasoning_frag
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
    yield ConvEvent('action_result', {'id': aid, 'job_id': result.job_id, 'num': result.num_artifacts, 'types': result.artifact_types, 'artifacts': ordered[:12], 'adapter': result.adapter, 'mode': result.mode, 'auth_failure': auth_failure})
    _has_signal = bool(_signal)
    # Quality gate: a "finding" with no substance (a one-liner with no file
    # reference) must not turn the badge green -- a swarm whose workers choked
    # on the goal used to read as a clean "N findings" success.
    _substantive = [a for a in _signal if _is_substantive_artifact(a)]
    _swarm_ok = bool(_substantive) and (not auth_failure)
    if auth_failure:
        # Lead with the provider/key note, never a generic "no findings" badge.
        _badge_summary = auth_failure[:160] if len(auth_failure) > 20 else 'auth failure'
    elif _substantive:
        _badge_summary = f'{len(_signal)} findings via {result.adapter} ({result.num_artifacts} artifacts)'
    elif _has_signal:
        _badge_summary = f'degraded: {len(_signal)} thin findings via {result.adapter} (no file-backed substance)'
    elif result.num_artifacts:
        _badge_summary = f'degraded: {result.num_artifacts} plumbing artifacts via {result.adapter}, no findings'
    else:
        _badge_summary = 'no artifacts produced'
    _badge_error = auth_failure or (
        None if _swarm_ok
        else 'swarm findings are thin/generic (no file-backed substance)' if _has_signal
        else 'swarm produced no FINDING/RISK/DECISION artifacts' if result.num_artifacts
        else 'swarm produced no artifacts')
    _store_jid = (result.job_id or '').strip() or _sync_local_id
    _badge = {'job_id': _store_jid, 'applied': _swarm_ok, 'files': [], 'summary': _badge_summary, 'error': _badge_error, 'objective': act.goal}
    try:
        session._finish_local_job(_sync_local_id, ok=_swarm_ok, summary=_badge_summary, status='done' if _swarm_ok else 'failed', engine=result.adapter or 'agentic')
        if _store_jid != _sync_local_id:
            if _store_jid not in session._session_job_ids:
                session._session_job_ids.append(_store_jid)
            session._register_local_job(_store_jid, act.goal, role='explore', cwd=_swarm_repo, engine=result.adapter or 'agentic')
            session._finish_local_job(_store_jid, ok=_swarm_ok, summary=_badge_summary, status='done' if _swarm_ok else 'failed', engine=result.adapter or 'agentic')
    except Exception:
        pass
    session._display_transcript.append({'type': 'swarm_result', **_badge})
    yield ConvEvent('swarm_result', {'job_id': _badge['job_id'], 'objective': act.goal, 'result': _badge})
    if result.adapter != 'demo':
        turn_findings.extend((a for a in result.artifacts if a.get('type') != 'verification'))
    digest = '\n'.join((f"  - [{a['type']}] {a['headline']}" for a in digest_arts)) or '  (no artifacts)'
    if auth_failure and not _has_signal and auth_failure not in digest:
        digest = f"  - [auth] {auth_failure}\n{digest}"
    stall = ''
    if counters['demo_swarms'] >= 2:
        stall = '\n(NOTE: swarms are running on the DEMO substrate, which returns generic artifacts -- not real codebase analysis. Do NOT keep retrying; explain this to the user and finish with no actions. Real analysis needs --repo + --swarm-adapter openai.)'
    if auth_failure:
        stall = f'\n(PROVIDER AUTH FAILURE -- {auth_failure} This is a dead/revoked/wrong API key, NOT a weak model or bad prompt. Do NOT re-run the swarm; tell the user to fix the named key, then stop.)' + stall
    elif not _has_signal:
        stall = '\n(DEGRADED SWARM — only routing/verification plumbing, no FINDING/RISK/DECISION. Tell the user the audit did not produce real findings. Re-dispatch with fewer roles or a sharper goal; do NOT claim the repo was reviewed.)' + stall
    elif not _substantive:
        stall = '\n(THIN SWARM FINDINGS — the findings above are generic one-liners with no file-backed evidence, a known failure mode when the goal is too long/multi-part for the workers. Do NOT present these as a completed audit. Re-dispatch narrowed workers with tight single-domain objectives.)' + stall
    session._append_action_result(act, aid, f"(swarm {aid} '{act.goal}' returned {result.num_artifacts} artifacts via {result.adapter}:\n{digest}\nExplain these findings to the user and either run a narrowed follow-up swarm or finish with no actions.){stall}", is_native)
    return None
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
            yield ConvEvent('action_start', {'id': aid, 'kind': 'run_implement', 'goal': act.goal, 'cwd': session.config.repo or None})
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
                    cap_msg = f'Swarm capacity reached ({session._swarm_inflight()} in flight); not dispatching more right now. Wait for an in-flight worker to finish.'
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
            session._session_job_ids.append(job_id)
            session._register_local_job(job_id, act.goal, role=_mode, cwd=effective_repo, engine=engine, model=session.config.driver or '' if engine == 'native' else '')
            _prewarm_worker_imports()
            if not session._submit_swarm(session._run_provider_worker_background, job_id, act.goal, requested_adapter, effective_repo, expects_diff):
                cap_msg = f'Swarm capacity reached ({session._swarm_inflight()} in flight); not dispatching more right now. Wait for an in-flight worker to finish.'
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
        mode = act.mode or 'implement'
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
        import json
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
                yield ConvEvent('action_result', {'id': sub_aid, 'error': f'Failed to create temp state-dir: {e}'})
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
                yield ConvEvent('action_result', {'id': sub_aid, 'error': f'Failed to start: {e}'})
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
        aggregate_num_artifacts = 0
        worker_statuses = []
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
                    if not session._submit_swarm(session._run_swarm_background, job_id, sub_goal, state_dir):
                        cap_msg = f'Swarm capacity reached ({session._swarm_inflight()} in flight); not dispatching follow-up for job {job_id}.'
                        yield ConvEvent('action_result', {'id': sub_aid, 'status': 'deferred', 'message': cap_msg})
                        aggregate_artifacts_summary.append(f"Sub-worker for '{sub_goal}' deferred: {cap_msg}")
                        continue
                    job_ids_collected.append(job_id)
                    session._session_job_ids.append(job_id)
                    p_info['state_dir'] = None
                    yield ConvEvent('action_result', {'id': sub_aid, 'job_id': job_id, 'status': 'pending', 'message': f'Dispatched parallel background swarm job {job_id}'})
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
                    yield ConvEvent('action_result', {'id': sub_aid, 'error': err_msg})
                    aggregate_artifacts_summary.append(f"Sub-worker for '{sub_goal}' failed: {err_msg}")
            finally:
                if p_info.get('state_dir'):
                    import shutil
                    shutil.rmtree(p_info['state_dir'], ignore_errors=True)
        if job_ids_collected:
            yield ConvEvent('swarm_pending', {'job_ids': job_ids_collected, 'objective': f"Parallel wave of goals: {', '.join(goals)}"})
            yield ConvEvent('action_result', {'id': aid, 'job_id': ','.join(job_ids_collected), 'status': 'pending', 'message': f"Dispatched parallel background swarm jobs: {', '.join(job_ids_collected)}"})
            session._append_action_result(act, aid, f"(run_parallel dispatched {len(job_ids_collected)} jobs in background: {', '.join(job_ids_collected)})", is_native)
            yield from session._answer_remaining_tool_calls(turn_actions, action_idx, is_native, action_seq)
            yield ConvEvent('assistant_done', {'turns': step + 1, 'swarms': swarms + len(job_ids_collected)})
            return 'return'
        else:
            yield ConvEvent('action_result', {'id': aid, 'error': 'No jobs successfully dispatched'})
            session._append_action_result(act, aid, f'(run_parallel failed to dispatch any jobs)', is_native)
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
            for sub_goal in goals:
                if not session._claim_objective(sub_goal):
                    skipped_goals.append(sub_goal)
                    continue
                short = uuid.uuid4().hex[:8]
                job_id = f'local-{short}'
                try:
                    session._register_local_job(job_id, sub_goal, role=_mode, cwd=effective_repo, engine=engine, model=session.config.driver or '' if engine == 'native' else '')
                    submitted = session._submit_swarm(session._run_provider_worker_background, job_id, sub_goal, requested_adapter, effective_repo, expects_diff)
                except Exception:
                    session._release_objective(sub_goal)
                    raise
                if not submitted:
                    session._release_objective(sub_goal)
                    deferred_goals.append(sub_goal)
                    continue
                job_ids_collected.append(job_id)
                session._session_job_ids.append(job_id)
            if deferred_goals:
                cap_msg = f'Swarm capacity reached ({session._swarm_inflight()} in flight); deferred {len(deferred_goals)} of {len(goals)} goal(s): ' + ', '.join(deferred_goals)
                yield ConvEvent('notice', {'message': cap_msg})
            if not job_ids_collected:
                skip_msg = 'All parallel objectives are already running in background workers -- nothing new dispatched. Wait for the in-flight workers rather than re-issuing them.'
                yield ConvEvent('action_result', {'id': aid, 'status': 'skipped', 'message': skip_msg})
                session._append_action_result(act, aid, f'(run_parallel {aid} skipped -- all {len(goals)} objectives already in flight)', is_native)
                return None
            yield ConvEvent('swarm_pending', {'job_ids': job_ids_collected, 'objective': f"Parallel wave of goals: {', '.join(goals)}"})
            yield ConvEvent('action_result', {'id': aid, 'job_id': ','.join(job_ids_collected), 'status': 'pending', 'message': f"Dispatched parallel background swarm jobs: {', '.join(job_ids_collected)}"})
            session._append_action_result(act, aid, f"(run_parallel {aid} dispatched {len(job_ids_collected)} jobs in background: {', '.join(job_ids_collected)})", is_native)
            yield from session._answer_remaining_tool_calls(turn_actions, action_idx, is_native, action_seq)
            yield ConvEvent('assistant_done', {'turns': step + 1, 'swarms': swarms + len(job_ids_collected)})
            return 'return'
        except Exception as e:
            yield ConvEvent('action_result', {'id': aid, 'error': str(e)})
            session._append_action_result(act, aid, f'(run_parallel {aid} failed: {e})', is_native)
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
