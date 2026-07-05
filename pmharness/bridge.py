from __future__ import annotations

"""pm_bridge: execute a validated DriverIntent against Puppetmaster's
in-process Orchestrator and normalize the result.

This is the proven seam (Stage 1): MCP and CLI are both thin transports over
Orchestrator(store).run(...). The bridge calls that engine directly -- no MCP,
no CLI subprocess -- which is the entire point of a PM-native harness.

Execution uses an isolated temp SQLite store and the default role path, which
runs on Puppetmaster's free local adapter. For the DRIVER eval that is exactly
what we want: deterministic, key-free ground truth so we measure the driver
model, not worker quality (a separate question).
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .intent import DriverIntent, ROLE_LENSES, infer_roles


def _install_delta_sink(
    on_delta: "Optional[Callable[[str, str, str], None]]",
) -> "Callable[[], None]":
    """Register ``on_delta`` as Puppetmaster's broadcast delta sink and return a
    zero-arg cleanup that clears it. Guarded for older bundled puppetmaster
    builds without the streaming bus: if the module isn't importable we no-op, so
    swarm runs still work (just without live token streaming).
    """
    if on_delta is None:
        return lambda: None
    try:
        from puppetmaster.adapters._delta_bus import set_broadcast_sink
    except Exception:
        return lambda: None
    set_broadcast_sink(on_delta)
    return lambda: set_broadcast_sink(None)


# Analyze-mode turn budget for swarm workers. Puppetmaster's agentic adapter
# defaults to 16 analyze turns; on a broad multi-part audit a cheaper model
# (haiku/flash) spends all 16 reading files and hits max_turns WITHOUT ever
# calling submit_findings, which the bridge then surfaces as a generic
# "completed without structured findings" degrade. Give analysis workers more
# headroom so exploration AND submission both fit. Overridable via env for
# tuning without a code change.
def _analyze_max_turns() -> int:
    import os as _os
    try:
        return max(16, int(_os.environ.get("HARNESS_ANALYZE_MAX_TURNS", "40")))
    except (TypeError, ValueError):
        return 40


def _browser_swarm_enabled(goal: str) -> bool:
    """Whether a swarm worker should get the CDP browser toolset. Opt in either
    explicitly (HARNESS_SWARM_BROWSER=1) or when the goal reads as a
    live-site/browser task (navigate a URL, inspect a rendered page, etc.).

    Read-only analysis workers are code-inspection by default; browsing a live
    site is a distinct, opt-in capability. The agentic adapter's own
    _browser_enabled gate honors payload['allow_browser'], so setting that flag
    is all the bridge has to do to unlock browser_* tools for the worker."""
    import os as _os
    if _os.environ.get("HARNESS_SWARM_BROWSER", "").strip() in ("1", "true", "yes"):
        return True
    g = (goal or "").lower()
    # A URL in the goal, or explicit browser verbs, signal a live-page task.
    if "http://" in g or "https://" in g:
        return True
    signals = (
        "browser", "browse ", "navigate to", "open the page", "open the site",
        "open the url", "the website", "live site", "rendered page",
        "screenshot of", "click the", "on the page",
    )
    return any(s in g for s in signals)


def _analysis_capability_payload() -> dict:
    """Cap the capability the analysis swarm asks for so the router lands on a
    balanced mid-tier model (sonnet / gemini-pro) instead of first-picking the
    frontier model for a routine read-only audit.

    The agentic adapter carries its own retry+degrade envelope, so a mid-tier
    model is more than sufficient for read-only analysis. Set an explicit
    capability ceiling of 85 (clears the strongest analysis roles' need without
    demanding a 99-cap frontier model). Opt back into frontier depth by setting
    HARNESS_ANALYSIS_DEEP=1, which removes the cap.
    """
    import os as _os
    if _os.environ.get("HARNESS_ANALYSIS_DEEP", "").strip() in ("1", "true", "yes"):
        return {}
    try:
        ceiling = int(_os.environ.get("HARNESS_ANALYSIS_MAX_CAPABILITY", "85"))
    except (TypeError, ValueError):
        ceiling = 85
    return {"min_capability": max(0, min(100, ceiling))}


def _analysis_instruction(goal: str, repo_cwd: str, role: str,
                          *, browser: bool = False) -> str:
    """Build a read-only analysis worker's instruction from the shared goal plus
    the role's lens, so a multi-role swarm fans out into distinct investigations
    rather than N identical passes over the same goal.

    When ``browser`` is set the worker is told it has the live browser toolset
    (browser_navigate/browser_snapshot/browser_get_text/...) so a live-site task
    drives a real page instead of only reading source. Browsing stays read-only:
    it must not edit, create, or delete files."""
    lens = ROLE_LENSES.get(role, "")
    lens_line = f"\n\n{lens}" if lens else ""
    if browser:
        return (
            f"{goal}{lens_line}\n\nYou have a real headless browser. Use the "
            f"browser tools to complete this: browser_navigate(url) to open a "
            f"page, then browser_snapshot() to list interactable elements with "
            f"@e-style refs, browser_get_text() for the readable page text, and "
            f"browser_click/browser_type/browser_scroll/browser_back as needed. "
            f"This is READ-ONLY: do not edit, create, or delete any files, and "
            f"do not submit credentials or perform destructive actions on the "
            f"site. Emit what each browser tool returned as evidenced findings, "
            f"then ALWAYS call submit_findings before you run out of turns."
        )
    return (
        f"{goal}{lens_line}\n\nAnalyze the REAL codebase at {repo_cwd}. "
        f"Emit evidenced findings/risks/decisions as artifacts. This is "
        f"a READ-ONLY analysis: do not edit, create, or delete any files.\n\n"
        # Turn-budget guardrail: broad-audit workers on cheaper models were
        # burning every turn exploring and hitting max_turns WITHOUT ever
        # calling submit_findings -- surfacing as a "completed without
        # structured findings" degrade. Tell the worker to budget explicitly
        # and always submit what it has rather than exhausting its turns.
        "IMPORTANT: You have a limited number of tool-call turns. Do a focused "
        "investigation (a handful of reads/searches), then ALWAYS call "
        "submit_findings with whatever concrete findings you have BEFORE you run "
        "out of turns. A few well-evidenced findings submitted is far better than "
        "a deep exploration that never submits. If unsure, submit early and stop."
    )


def _analysis_provider_payload() -> dict:
    """Provider knobs for the read-only analysis worker. Defaults to OpenRouter
    (funded, open models) since the OpenAI adapter speaks the OpenAI-compatible
    schema; set HARNESS_ANALYSIS_REACH=openai to use the native OpenAI API.

    The API KEY is NOT placed in the payload (transiting tool/secret layers can
    truncate it); instead _prepare_analysis_env() sets OPENAI_API_KEY +
    OPENAI_BASE_URL in the process env, which the adapter reads natively."""
    import os
    reach = (os.environ.get("HARNESS_ANALYSIS_REACH", "openrouter") or "openrouter").lower()
    if reach == "openai":
        return {"skip_preflight": True}
    model = os.environ.get("HARNESS_ANALYSIS_MODEL", "qwen/qwen3-coder-30b-a3b-instruct")
    return {
        "model": model,
        "openai_allow_untrusted_base_url": True,
        "skip_preflight": True,
    }


def _codegraph_indexed(repo_cwd: str) -> bool:
    """True when the target repo has a CodeGraph index. Without it, the analysis
    worker gets NO source context and guesses -- the benchmark proved accuracy
    collapses from ~81% to ~30% (blind). We surface this loudly."""
    import os
    return os.path.isdir(os.path.join(repo_cwd, ".codegraph"))


def _warn_if_unindexed(repo_cwd: str) -> None:
    """Emit a clear warning (stderr) when real analysis runs on an unindexed repo.
    Set HARNESS_REQUIRE_CODEGRAPH=1 to hard-fail instead of degrade silently."""
    import os, sys
    if _codegraph_indexed(repo_cwd):
        return
    msg = (f"[harness] WARNING: {repo_cwd} has no .codegraph index -- real analysis "
           f"will run BLIND (no source context, ~30% accuracy vs ~81% indexed). "
           f"Run: python -m puppetmaster codegraph init --index  (cwd={repo_cwd})")
    if os.environ.get("HARNESS_REQUIRE_CODEGRAPH", "").strip() in ("1", "true", "yes"):
        raise RuntimeError(msg.replace("WARNING", "ERROR") +
                           "  [HARNESS_REQUIRE_CODEGRAPH=1]")
    print(msg, file=sys.stderr)


def _prepare_analysis_env() -> None:
    """Point the OpenAI adapter at OpenRouter via process env (masker-safe).
    Only acts when reach is openrouter (default) and a key is present."""
    import os
    reach = (os.environ.get("HARNESS_ANALYSIS_REACH", "openrouter") or "openrouter").lower()
    if reach == "openai":
        return
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        os.environ["OPENAI_API_KEY"] = key
        os.environ["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"


@dataclass
class BridgeResult:
    job_id: str
    status: str
    mode: str
    num_artifacts: int
    artifact_types: list
    summary: str
    artifacts: list  # list of compact dicts (type, claim/decision/etc snippet)
    auth_failure: str = ""  # loud one-liner when a provider rejected the key
    #   (dead/revoked/wrong key). Empty when no auth failure occurred. Lets the
    #   harness flag the real cause instead of "completed without findings".
    adapter: str = "demo"  # "demo" = local deterministic substrate (not real
    #   codebase analysis); set to a real worker adapter when configured. Surfaces
    #   use this to label generic substrate so it is never mistaken for real
    #   findings.


def _compact_artifact(a: Any) -> dict:
    """Reduce a Puppetmaster Artifact to a small dict suitable for feeding back
    to a driver model on a follow-up turn without blowing context."""
    payload = getattr(a, "payload", {}) or {}
    # Priority-ordered keys where a real finding's text may live. Broadened so a
    # worker that put its analysis under a non-canonical key (report, message,
    # etc.) is never surfaced as an empty headline and silently dropped.
    _headline_keys = (
        "claim", "decision", "risk", "check", "summary", "change",
        "report", "mitigation", "why", "result", "observation",
        "note", "detail", "message", "text", "body", "content",
        # A degraded agentic worker parks its prose analysis in a verification
        # artifact's stdout; read it so that text can be promoted to a finding.
        "stdout",
    )
    headline = ""
    for _k in _headline_keys:
        _v = payload.get(_k)
        if isinstance(_v, str) and _v.strip():
            headline = _v
            break
    if not (isinstance(headline, str) and headline.strip()):
        # Last resort: first non-empty string value anywhere in the payload so a
        # genuine finding is NEVER surfaced as empty.
        for _v in payload.values():
            if isinstance(_v, str) and _v.strip():
                headline = _v
                break
    # empty_headline: True when a payload existed but yielded no usable text, so
    # the digest can be honest about "present but empty" vs "genuinely no
    # artifact". After the broadening above this should essentially never happen.
    empty_headline = not (isinstance(headline, str) and headline.strip())
    if empty_headline and payload:
        headline = str(getattr(a, "type", "") or "artifact")
    return {
        "type": str(getattr(a, "type", "")),
        "headline": str(headline)[:240],
        "empty_headline": empty_headline,
        "confidence": getattr(a, "confidence", None),
        # Carry the machine-readable failure tag so consumers can branch on a
        # provider auth rejection (auth_failed:401/403) instead of mistaking it
        # for a weak-model / bad-prompt degrade.
        "failure": str(payload.get("failure") or "") or None,
    }


_SIGNAL_TYPES = frozenset({"finding", "risk", "decision"})


def _promote_degraded_prose(compact: list) -> list:
    """Rescue a swarm whose worker analyzed in PROSE instead of calling
    submit_findings. When the agentic adapter's worker produces no structured
    findings but real final_text, that text is parked in a VERIFICATION artifact's
    stdout and marked degraded -- which the pilot digest treats as plumbing and
    hides, so a swarm that did real analysis reads as 'completed without
    structured findings'. If there are NO signal artifacts (finding/risk/decision)
    but a verification artifact carries substantial prose, promote a copy of it to
    a 'finding' so the analysis actually reaches the pilot/UI. Pure and
    deterministic; leaves the originals intact.

    WHY THIS LIVES IN MARIONETTE'S BRIDGE, NOT UPSTREAM IN PUPPETMASTER:
    The cleaner-looking fix is to make puppetmaster's agentic adapter emit a
    finding directly instead of a degraded verification artifact. Do NOT move it
    there. Puppetmaster ships to users as the PyPI package `puppetmaster-ai`
    (scripts/install.sh: `uv pip install puppetmaster-ai`); only the author has it
    editable-installed from a local checkout. An upstream fix would therefore do
    nothing for anyone until a NEW puppetmaster-ai is published AND Marionette
    pins `puppetmaster-ai>=<that version>` -- adding version coupling and joining
    friction for new users, for zero benefit over normalizing here. Keeping the
    normalization at the harness boundary means the fix ships WITH Marionette,
    works for every install regardless of the Puppetmaster version, and correctly
    treats worker output as an untrusted boundary. Leave it here.
    """
    try:
        has_signal = any(str(a.get("type")) in _SIGNAL_TYPES
                         and not a.get("empty_headline")
                         and str(a.get("headline") or "").strip()
                         for a in compact)
        if has_signal:
            return compact
        promoted = list(compact)
        for a in compact:
            if str(a.get("type")) != "verification":
                continue
            text = str(a.get("headline") or "").strip()
            # Only promote genuine prose analysis, not a one-word "passed"/"blocked".
            if len(text) < 40:
                continue
            promoted.append({
                "type": "finding",
                "headline": text[:240],
                "empty_headline": False,
                "confidence": a.get("confidence"),
                "failure": None,
                "promoted_from": "verification",
            })
        return promoted
    except Exception:
        return compact


def _auth_failure_note(compact: list) -> str:
    """Return a loud, human one-liner when any artifact is a provider auth
    rejection, else empty. Lets the harness surface a dead/revoked key as the
    real cause rather than burying it as "no structured findings"."""
    for a in compact:
        fail = str(a.get("failure") or "")
        if fail.startswith("auth_failed"):
            return str(a.get("headline") or "Provider auth failure").strip()
    return ""


def _hoist_auth_risks(compact: list) -> list:
    """Sort provider auth-failure artifacts to the front so a fixed-size digest
    slice (e.g. artifacts[:8]) can never drop the one finding that explains the
    whole run."""
    auth = [a for a in compact if str(a.get("failure") or "").startswith("auth_failed")]
    rest = [a for a in compact if not str(a.get("failure") or "").startswith("auth_failed")]
    return auth + rest if auth else compact


def execute_intent(
    intent: DriverIntent,
    *,
    state_dir: Optional[str] = None,
    worker_mode: Optional[str] = None,
    on_delta: Optional[Callable[[str, str, str], None]] = None,
) -> Optional[BridgeResult]:
    """Run a run_swarm intent against Puppetmaster. Returns None for non-swarm
    actions (answer/stop) since there is nothing to execute.

    Imports of puppetmaster are local so the schema/validation layer stays
    importable with zero PM dependency (keeps unit tests fast and hermetic).

    When ``on_delta`` is given (``on_delta(worker_id, kind, text)``), inline
    agentic workers stream their token deltas to it live via Puppetmaster's
    delta bus. The bus registration is guarded so an older bundled puppetmaster
    without streaming support simply runs blocking, as before.
    """
    if intent.action != "run_swarm":
        return None
    if not intent.goal:
        raise ValueError("cannot execute run_swarm intent without a goal")

    import os as _os
    from puppetmaster.store_factory import create_store
    from puppetmaster.orchestrator import Orchestrator

    _clear_delta_sink = _install_delta_sink(on_delta)
    tmp = state_dir or tempfile.mkdtemp(prefix="pmh-exec-")
    store = create_store("sqlite", tmp)

    try:
        # Swarm adapter selection (safety-first):
        #   demo (default)  -> built-in local demo adapter: deterministic, free, no
        #                      real code analysis. The substrate for driver eval.
        #   openai          -> REAL LLM analysis of REAL code. We build READ-ONLY
        #                      analysis WorkerSpecs pointed at the target repo cwd so
        #                      Puppetmaster injects CodeGraph context. The "openai"
        #                      adapter is NOT in _EDIT_CAPABLE_ADAPTERS, and we also
        #                      stamp read_only=True -- a triple guard so a real run
        #                      can NEVER edit a target repo (safe even on live repos).
        swarm_adapter = (_os.environ.get("HARNESS_SWARM_ADAPTER", "demo") or "demo").lower()
        repo_cwd = _os.environ.get("HARNESS_REPO", "").strip()

        if swarm_adapter == "agentic" and repo_cwd:
            # Standalone path: run READ-ONLY analysis workers on the built-in
            # 'agentic' adapter, which calls a provider API directly on the user's
            # own key -- no external agent CLI. auto_route lets Puppetmaster's router
            # pick the right-sized model among ONLY the providers the user's keys
            # unlock (key-aware filter) and the enabled platform lock. The agentic
            # adapter's analyze mode exposes no edit tools, so this is safe on live
            # repos even before the triple read-only guard below.
            _warn_if_unindexed(repo_cwd)
            from puppetmaster.workers import WorkerSpec
            roles = intent.roles or infer_roles(intent.goal)
            _browser = _browser_swarm_enabled(intent.goal)
            specs = []
            for r in roles:
                specs.append(WorkerSpec(
                    role=r,
                    instruction=_analysis_instruction(
                        intent.goal, repo_cwd, r, browser=_browser),
                    adapter="agentic",
                    payload={
                        "read_only": True, "no_edit": True, "dry_run": True,
                        "cwd": repo_cwd, "prompt": intent.goal,
                        "auto_route": True,
                        # Opt this worker into the CDP browser toolset. The
                        # agentic adapter's _browser_enabled gate reads this flag
                        # and registers/dispatches the browser_* tools; without
                        # it the worker is code-inspection only (the reason a
                        # browser goal previously came back with no browser
                        # tools). Read-only stays true: browsing is not editing.
                        "allow_browser": _browser,
                        # Extra turn headroom so broad-audit workers submit
                        # findings instead of starving out at max_turns.
                        "max_turns": _analyze_max_turns(),
                        # Cost guardrail: several analysis roles (audit=85,
                        # security-review=90, conflict-auditor=75) carry a high
                        # role base score, which pushes the router to first-pick
                        # the frontier model (opus, ~$15/$75 per Mtok) even for a
                        # routine read-only audit -- ~$12/run. Cap the capability
                        # need at a "balanced" ceiling and route with the cheapest
                        # policy so a sufficient mid-tier model (sonnet / gemini-
                        # pro) wins, and prefer the cheapest sufficient model.
                        # Opus stays available via HARNESS_ANALYSIS_DEEP=1.
                        # 'balanced' = cheapest model whose capability clears the
                        # need (not the absolute-cheapest 'cheap' policy, which
                        # would grab a too-weak model that starves out).
                        "routing_policy": "balanced",
                        **_analysis_capability_payload(),
                    },
                ))
            result = Orchestrator(store).run(
                intent.goal, specs=specs, worker_mode=worker_mode or "inline",
            )
            adapter = "agentic"
        elif swarm_adapter == "openai" and repo_cwd:
            _prepare_analysis_env()
            _warn_if_unindexed(repo_cwd)
            from puppetmaster.workers import WorkerSpec
            roles = intent.roles or infer_roles(intent.goal)
            specs = []
            for r in roles:
                specs.append(WorkerSpec(
                    role=r,
                    instruction=_analysis_instruction(intent.goal, repo_cwd, r),
                    adapter="openai",
                    payload={
                        "read_only": True, "no_edit": True, "dry_run": True,
                        "cwd": repo_cwd, "prompt": intent.goal,
                        "auto_route": False,
                        "max_turns": _analyze_max_turns(),
                        # Route analysis through OpenRouter (funded, open models) by
                        # default; the OpenAI adapter speaks the OpenAI-compatible
                        # schema so base_url + key + an open model just works. Falls
                        # back to native OpenAI only if HARNESS_ANALYSIS_REACH=openai.
                        **_analysis_provider_payload(),
                    },
                ))
            # inline: the analysis worker runs in-process so the env-based key
            # wiring propagates reliably, and it yields richer multi-artifact output.
            result = Orchestrator(store).run(
                intent.goal, specs=specs, worker_mode=worker_mode or "inline",
            )
            adapter = "openai"
        else:
            # The default role path (roles=None) uses the built-in local demo adapter:
            # no API keys, deterministic, free. Label as demo substrate honestly.
            result = Orchestrator(store).run(
                intent.goal,
                roles=intent.roles,
                worker_mode=worker_mode or "subprocess",
            )
            adapter = "demo"

        artifacts = list(result.artifacts)
        compact = _hoist_auth_risks([_compact_artifact(a) for a in artifacts])
        compact = _promote_degraded_prose(compact)
        return BridgeResult(
            job_id=result.job.id,
            status=str(result.job.status),
            mode=str(result.mode),
            num_artifacts=len(artifacts),
            artifact_types=sorted({str(a.type) for a in artifacts}),
            summary=result.summary or "",
            artifacts=compact,
            auth_failure=_auth_failure_note(compact),
            adapter=adapter,
        )
    finally:
        _clear_delta_sink()
