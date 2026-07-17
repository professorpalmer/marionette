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


def worker_token_budget() -> int:
    """Default token ceiling stamped on analysis/implement worker payloads.

    Mirrors the Settings "Worker run token ceiling" control
    (HARNESS_WORKER_TOKEN_BUDGET, default 40000). Ambient AutoBudget still
    governs native ProviderWorker spend when present; this value is the
    agentic payload hint + unsupervised native default.
    """
    import os as _os
    try:
        return max(1, int(_os.environ.get("HARNESS_WORKER_TOKEN_BUDGET", "40000") or 40000))
    except (TypeError, ValueError):
        return 40000


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


def _router_supports_max_capability() -> bool:
    """True when the installed Puppetmaster's router understands the
    ``max_capability`` ceiling. Older puppetmaster-ai builds (<= 1.10.0) only
    know ``min_capability``; sending them the new key would silently drop the
    cost cap entirely, so callers fall back to the legacy pin instead."""
    try:
        from puppetmaster.router import TaskSignals
        return "explicit_max_capability" in getattr(
            TaskSignals, "__dataclass_fields__", {})
    except Exception:
        return False


def _analysis_capability_payload() -> dict:
    """Cap the capability the analysis swarm asks for so the router lands on a
    balanced mid-tier model (sonnet / gemini-pro) instead of first-picking the
    frontier model for a routine read-only audit.

    The agentic adapter carries its own retry+degrade envelope, so a mid-tier
    model is more than sufficient for read-only analysis. Set a capability
    CEILING of 85 via payload.max_capability -- clips the top of the classifier
    output while cheap roles (verify=25, explore=50) still classify low and
    route to cheap models. (The previous min_capability=85 FORCED every task's
    need to exactly 85, which pinned every swarm worker to the one cheapest
    86-cap model regardless of role.) Opt back into frontier depth by setting
    HARNESS_ANALYSIS_DEEP=1, which removes the cap.
    """
    import os as _os
    if _os.environ.get("HARNESS_ANALYSIS_DEEP", "").strip() in ("1", "true", "yes"):
        return {}
    try:
        ceiling = int(_os.environ.get("HARNESS_ANALYSIS_MAX_CAPABILITY", "85"))
    except (TypeError, ValueError):
        ceiling = 85
    ceiling = max(0, min(100, ceiling))
    if _router_supports_max_capability():
        return {"max_capability": ceiling}
    # Legacy fallback: keeps the cost cap on older routers even though it
    # flattens per-task differentiation (need pinned to exactly the ceiling).
    return {"min_capability": ceiling}


# First-principles STOP conditions, ported from the ARC-AGI winning harnesses'
# operations-manual style. Kept short and in plain words so it hardens the brief
# without bloating it. Guards the two real loop-burn failure modes we saw: a
# worker retrying the same idea forever, and a worker resetting its own progress
# to "start clean" -- both of which stop it ever concluding and reporting back.
_STOP_CONDITIONS = (
    "STOP CONDITIONS: If 2-3 variations of an approach fail to produce the "
    "expected result, STOP and report back to whoever called you with what you "
    "learned -- do not keep looping on the same idea. Never restart or reset "
    "your work to 'think more carefully' or 'try a clean approach': that "
    "discards the progress you already have. Prefer returning a few "
    "well-evidenced findings over an exhaustive exploration that never concludes."
)


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
            f"then ALWAYS call submit_findings before you run out of turns.\n\n"
            f"{_STOP_CONDITIONS}"
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
        "\n\n" + _STOP_CONDITIONS
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
    # Preserve the FULL extracted prose BEFORE we truncate the headline for
    # display. A degraded agentic worker parks 1000s of chars of real audit
    # analysis in a verification artifact's stdout; clipping it to 240 for the
    # headline used to silently discard the rest. `body` carries the untruncated
    # text so downstream (finding promotion, digest) can surface the real
    # analysis without breaking existing consumers that only read `headline`.
    body = str(headline) if (isinstance(headline, str) and headline.strip()) else ""
    if empty_headline and payload:
        headline = str(getattr(a, "type", "") or "artifact")
    failure = str(payload.get("failure") or "") or None
    # Auth rejections must surface as AUTH FAILURE with provider + key env, not
    # as the verification check text / empty degrade headline.
    if _is_auth_failure_tag(failure, headline):
        mitigation = str(payload.get("mitigation") or "").strip()
        provider = str(payload.get("provider") or "").strip()
        if "AUTH FAILURE" not in str(headline or "").upper():
            status = payload.get("returncode")
            status_bit = (
                f"HTTP {status}" if status in (401, 403, "401", "403")
                else (failure or "auth rejected")
            )
            who = f"provider '{provider}'" if provider else "provider"
            headline = f"AUTH FAILURE: {who} rejected the API key ({status_bit})"
            empty_headline = False
            body = str(headline)
        if mitigation and mitigation not in str(headline):
            # mitigation names the env var to fix (e.g. OPENAI_API_KEY).
            headline = f"{str(headline).rstrip('. ')}. {mitigation}"
            body = str(headline) if not body else body
            empty_headline = False
    return {
        "type": str(getattr(a, "type", "")),
        "headline": str(headline)[:240],
        "body": body,
        "empty_headline": empty_headline,
        "confidence": getattr(a, "confidence", None),
        # Carry the machine-readable failure tag so consumers can branch on a
        # provider auth rejection (auth_failed:401/403) instead of mistaking it
        # for a weak-model / bad-prompt degrade.
        "failure": failure,
    }


_SIGNAL_TYPES = frozenset({"finding", "risk", "decision"})

# Failure tags that mean provider credential rejection (401/403 / missing key),
# including the verification artifact stamped by agentic ``_fail`` when the
# dedicated auth RISK is absent (older Puppetmaster builds).
_AUTH_FAILURE_EXACT = frozenset({
    "not_authenticated",
    "http_status:401",
    "http_status:403",
})


def _is_auth_failure_tag(failure: object, headline: object = "") -> bool:
    """True when a compact/raw failure tag (or headline) is a provider auth reject."""
    fail = str(failure or "").strip()
    if fail.startswith("auth_failed"):
        return True
    low = fail.lower()
    if low in _AUTH_FAILURE_EXACT:
        return True
    if low.startswith("http_status:"):
        code = low.rsplit(":", 1)[-1]
        if code in ("401", "403"):
            return True
    head = str(headline or "")
    if "AUTH FAILURE" in head.upper():
        return True
    return False


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
        # Never launder a provider auth rejection into a synthetic "finding" --
        # that path is exactly how a dead key used to read as "completed without
        # structured findings" / thin findings instead of AUTH FAILURE.
        if any(_is_auth_failure_tag(a.get("failure"), a.get("headline"))
               for a in compact):
            return compact
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
            if _is_auth_failure_tag(a.get("failure"), a.get("headline")):
                continue
            # Use the FULL body (untruncated stdout prose), falling back to the
            # display headline. Detection and the promoted finding both rely on
            # the full text so a broad audit's 3000-char analysis survives whole
            # instead of collapsing to the 240-char headline clip.
            body = str(a.get("body") or a.get("headline") or "").strip()
            # Only promote genuine prose analysis, not a one-word "passed"/"blocked".
            if len(body) < 40:
                continue
            promoted.append({
                "type": "finding",
                # headline stays clipped for display, but the full body is carried
                # verbatim so the pilot/digest can render the real analysis.
                "headline": body[:240],
                "body": body,
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
        headline = str(a.get("headline") or "").strip()
        if not _is_auth_failure_tag(fail, headline):
            continue
        note = headline or "Provider auth failure"
        # Prefer an explicit AUTH FAILURE lead-in so badge/digest never read as
        # a generic degrade when we only have a verification failure tag.
        if "AUTH FAILURE" not in note.upper():
            note = f"AUTH FAILURE: {note}" if note else "AUTH FAILURE: provider auth rejected"
            if fail and fail not in note:
                note = f"{note} ({fail})"
        return note.strip()
    return ""


def _summary_leading_with_auth(summary: str, auth_note: str) -> str:
    """Ensure BridgeResult.summary leads with the auth note when present.

    Orchestrator stitcher text often still says "completed without structured
    findings" even when an auth RISK exists; consumers that only read ``summary``
    must not miss the credential failure.
    """
    note = (auth_note or "").strip()
    if not note:
        return summary or ""
    raw = (summary or "").strip()
    if not raw or "without structured findings" in raw.lower():
        return note
    if raw.startswith("AUTH FAILURE") or note in raw:
        return raw if raw.startswith("AUTH FAILURE") else f"{note}\n{raw}"
    return f"{note}\n{raw}"


def _hoist_auth_risks(compact: list) -> list:
    """Sort provider auth-failure artifacts to the front so a fixed-size digest
    slice (e.g. artifacts[:8]) can never drop the one finding that explains the
    whole run."""
    auth = [a for a in compact
            if _is_auth_failure_tag(a.get("failure"), a.get("headline"))]
    rest = [a for a in compact
            if not _is_auth_failure_tag(a.get("failure"), a.get("headline"))]
    return auth + rest if auth else compact


def _prewalk_timeout_seconds() -> int:
    """Timeout shared by plan + implement stages (CLI default is 900s)."""
    import os as _os
    try:
        return max(60, int(_os.environ.get("HARNESS_PREWALK_TIMEOUT", "900")))
    except (TypeError, ValueError):
        return 900


def _prewalk_allow_dirty() -> bool:
    """Match conversation.py implement dispatch: dirty trees allowed by default."""
    import os as _os
    raw = (_os.environ.get("HARNESS_ALLOW_DIRTY", "1") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _prewalk_allow_non_worktree() -> bool:
    import os as _os
    raw = (_os.environ.get("HARNESS_ALLOW_NON_WORKTREE", "1") or "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _resolve_prewalk_implement_adapter(requested: str = "") -> str:
    """Pick an edit-capable implement adapter (same gate as PM CLI prewalk)."""
    from puppetmaster import platform_lock
    from puppetmaster.workers import pick_implement_adapter

    enabled = platform_lock.enabled_adapters()
    return pick_implement_adapter(enabled, requested or None)


def _build_prewalk_cli_argv(
    goal: str,
    *,
    cwd: str,
    allow_dirty: bool = True,
    allow_non_worktree: bool = True,
    adapter: str = "",
    plan_adapter: str = "",
    timeout_seconds: Optional[int] = None,
    worker_mode: str = "subprocess",
    label: str = "",
) -> list:
    """Build ``python -m puppetmaster prewalk ...`` argv (sans interpreter).

    Kept as a pure command builder so tests can assert the CLI shape without
    spawning Cursor / a live worker.
    """
    cmd = ["prewalk", goal, "--cwd", cwd or "."]
    if adapter:
        cmd.extend(["--adapter", adapter])
    if plan_adapter:
        cmd.extend(["--plan-adapter", plan_adapter])
    if timeout_seconds is not None:
        cmd.extend(["--timeout-seconds", str(int(timeout_seconds))])
    if allow_dirty:
        cmd.append("--allow-dirty")
    if allow_non_worktree:
        cmd.append("--allow-non-worktree")
    if worker_mode:
        cmd.extend(["--worker-mode", worker_mode])
    if label:
        cmd.extend(["--label", label])
    return cmd


def _execute_prewalk(
    intent: DriverIntent,
    *,
    store: Any,
    repo_cwd: str,
    worker_mode: Optional[str],
    job_label: str,
    session_id: str,
) -> BridgeResult:
    """Start a plan-then-cheap prewalk via Puppetmaster's library entry.

    Mirrors ``python -m puppetmaster prewalk``: ``build_prewalk_specs`` +
    ``Orchestrator.run``. Prefer the library path over a CLI subprocess so the
    bridge stays in-process like run_swarm (no second orchestrator).
    """
    import os as _os
    from puppetmaster.orchestrator import Orchestrator
    from puppetmaster.prewalk import build_prewalk_specs
    from harness.job_scoping import stamp_task_payload

    requested = (
        _os.environ.get("HARNESS_IMPLEMENT_ADAPTER", "")
        or _os.environ.get("HARNESS_PREWALK_ADAPTER", "")
        or ""
    ).strip()
    implement_adapter = _resolve_prewalk_implement_adapter(requested)
    plan_adapter = (
        _os.environ.get("HARNESS_PREWALK_PLAN_ADAPTER", "local") or "local"
    ).strip()
    timeout = _prewalk_timeout_seconds()
    allow_dirty = _prewalk_allow_dirty()
    allow_non_worktree = _prewalk_allow_non_worktree()

    specs = build_prewalk_specs(
        intent.goal or "",
        repo_cwd or ".",
        plan_adapter=plan_adapter,
        implement_adapter=implement_adapter,
        plan_timeout_seconds=timeout,
        implement_timeout_seconds=timeout,
        allow_dirty=allow_dirty,
        allow_non_worktree=allow_non_worktree,
    )
    # Stamp session/cwd on each payload the same way swarm workers do, so
    # job scoping and /api/swarm/live attribution stay consistent.
    for spec in specs:
        payload = dict(getattr(spec, "payload", None) or {})
        if "token_budget" not in payload:
            payload["token_budget"] = worker_token_budget()
        stamped = stamp_task_payload(
            payload, session_id=session_id or "", cwd=repo_cwd or ""
        )
        try:
            spec.payload = stamped
        except Exception:
            # WorkerSpec may be frozen/mocked; best-effort only.
            pass

    mode = worker_mode or intent.worker_mode or "subprocess"
    result = Orchestrator(store).run(
        intent.goal,
        specs=specs,
        worker_mode=mode,
        label=job_label,
    )
    artifacts = list(result.artifacts)
    compact = _hoist_auth_risks([_compact_artifact(a) for a in artifacts])
    compact = _promote_degraded_prose(compact)
    auth_note = _auth_failure_note(compact)
    return BridgeResult(
        job_id=result.job.id,
        status=str(result.job.status),
        mode=str(result.mode),
        num_artifacts=len(artifacts),
        artifact_types=sorted({str(a.type) for a in artifacts}),
        summary=_summary_leading_with_auth(result.summary or "", auth_note),
        artifacts=compact,
        auth_failure=auth_note,
        adapter=f"prewalk:{implement_adapter}",
    )


def execute_intent(
    intent: DriverIntent,
    *,
    state_dir: Optional[str] = None,
    worker_mode: Optional[str] = None,
    on_delta: Optional[Callable[[str, str, str], None]] = None,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
    repo: Optional[str] = None,
) -> Optional[BridgeResult]:
    """Run a dispatch intent against Puppetmaster.

    Handles ``run_swarm`` (read-only analysis) and ``run_prewalk`` (plan-then-
    cheap implement). Returns None for terminal actions (answer/stop).

    Imports of puppetmaster are local so the schema/validation layer stays
    importable with zero PM dependency (keeps unit tests fast and hermetic).

    When ``on_delta`` is given (``on_delta(worker_id, kind, text)``), inline
    agentic workers stream their token deltas to it live via Puppetmaster's
    delta bus. The bus registration is guarded so an older bundled puppetmaster
    without streaming support simply runs blocking, as before.

    ``cwd`` / ``repo`` (aliases) pin the analysis workspace for this call.
    Prefer these over the live ``HARNESS_REPO`` env so a mid-turn workspace
    switch cannot retarget a busy runner's swarm. When set, ``HARNESS_REPO``
    is temporarily aligned for the duration of the call and restored after.
    """
    if intent.action not in ("run_swarm", "run_prewalk"):
        return None
    if not intent.goal:
        raise ValueError(f"cannot execute {intent.action} intent without a goal")

    import os as _os
    from puppetmaster.store_factory import create_store
    from puppetmaster.orchestrator import Orchestrator
    from harness.job_scoping import job_label_for_session, stamp_task_payload

    _clear_delta_sink = _install_delta_sink(on_delta)
    tmp = state_dir or tempfile.mkdtemp(prefix="pmh-exec-")
    store = create_store("sqlite", tmp)
    job_label = job_label_for_session(session_id or "")

    # Explicit per-runner cwd wins over the process-wide HARNESS_REPO view pointer.
    explicit_cwd = (cwd or repo or "").strip()
    prev_harness_repo = _os.environ.get("HARNESS_REPO")
    env_patched = False
    if explicit_cwd:
        _os.environ["HARNESS_REPO"] = explicit_cwd
        env_patched = True

    try:
        repo_cwd = explicit_cwd or _os.environ.get("HARNESS_REPO", "").strip()

        if intent.action == "run_prewalk":
            if not repo_cwd:
                raise ValueError(
                    "run_prewalk requires a workspace cwd "
                    "(pass cwd=/repo or set HARNESS_REPO)"
                )
            return _execute_prewalk(
                intent,
                store=store,
                repo_cwd=repo_cwd,
                worker_mode=worker_mode,
                job_label=job_label,
                session_id=session_id or "",
            )

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
                    payload=stamp_task_payload({
                        "read_only": True, "no_edit": True, "dry_run": True,
                        "cwd": repo_cwd, "prompt": intent.goal,
                        "auto_route": True,
                        # Stay on the agentic adapter for BOTH the first pick
                        # and router-fallback. Without this, prefer_plan_billed
                        # first-picks Cursor GPT ($0 plan) then fallback lands
                        # on openai/gpt-* even when the user's Models toggles
                        # only enabled OpenRouter pilots -- the tracker then
                        # shows a GPT model the picker never offered.
                        "allowed_adapters": ["agentic"],
                        # Agentic path is API-billed OpenRouter (or other keyed
                        # providers); do not prefer plan-billed Cursor/Codex.
                        "prefer_plan_billed": False,
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
                        "token_budget": worker_token_budget(),
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
                    }, session_id=session_id or "", cwd=repo_cwd),
                ))
            result = Orchestrator(store).run(
                intent.goal, specs=specs, worker_mode=worker_mode or "inline",
                label=job_label,
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
                    payload=stamp_task_payload({
                        "read_only": True, "no_edit": True, "dry_run": True,
                        "cwd": repo_cwd, "prompt": intent.goal,
                        "auto_route": False,
                        "max_turns": _analyze_max_turns(),
                        "token_budget": worker_token_budget(),
                        # Route analysis through OpenRouter (funded, open models) by
                        # default; the OpenAI adapter speaks the OpenAI-compatible
                        # schema so base_url + key + an open model just works. Falls
                        # back to native OpenAI only if HARNESS_ANALYSIS_REACH=openai.
                        **_analysis_provider_payload(),
                    }, session_id=session_id or "", cwd=repo_cwd),
                ))
            # inline: the analysis worker runs in-process so the env-based key
            # wiring propagates reliably, and it yields richer multi-artifact output.
            result = Orchestrator(store).run(
                intent.goal, specs=specs, worker_mode=worker_mode or "inline",
                label=job_label,
            )
            adapter = "openai"
        else:
            # The default role path (roles=None) uses the built-in local demo adapter:
            # no API keys, deterministic, free. Label as demo substrate honestly.
            result = Orchestrator(store).run(
                intent.goal,
                roles=intent.roles,
                worker_mode=worker_mode or "subprocess",
                label=job_label,
            )
            adapter = "demo"

        artifacts = list(result.artifacts)
        compact = _hoist_auth_risks([_compact_artifact(a) for a in artifacts])
        compact = _promote_degraded_prose(compact)
        auth_note = _auth_failure_note(compact)
        return BridgeResult(
            job_id=result.job.id,
            status=str(result.job.status),
            mode=str(result.mode),
            num_artifacts=len(artifacts),
            artifact_types=sorted({str(a.type) for a in artifacts}),
            summary=_summary_leading_with_auth(result.summary or "", auth_note),
            artifacts=compact,
            auth_failure=auth_note,
            adapter=adapter,
        )
    finally:
        _clear_delta_sink()
        if env_patched:
            if prev_harness_repo is None:
                _os.environ.pop("HARNESS_REPO", None)
            else:
                _os.environ["HARNESS_REPO"] = prev_harness_repo
