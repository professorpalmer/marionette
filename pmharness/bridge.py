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

import copy
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
    (HARNESS_WORKER_TOKEN_BUDGET, default 250000). Ambient AutoBudget still
    governs native ProviderWorker spend when present; this value is the
    agentic payload hint + unsupervised native default.
    """
    import os as _os
    try:
        return max(1, int(_os.environ.get("HARNESS_WORKER_TOKEN_BUDGET", "250000") or 250000))
    except (TypeError, ValueError):
        return 250000


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


_ANALYSIS_OUTPUT_FORMAT_BLOCK = (
    "REQUIRED OUTPUT FORMAT (literal line prefixes -- fail-closed without them):\n"
    "FINDING: path/to/file.py:123 short claim with evidence\n"
    "RISK: path/to/file.py:45 short claim\n"
    "DECISION: keep X because Y\n"
    "\n"
    "Rules:\n"
    "- At least one FINDING or RISK or DECISION line is required before you stop.\n"
    "- Lines must start with the label (optionally after 'Last assistant message: ').\n"
    "- Do not end on unlabeled prose paragraphs alone."
)


def _analysis_submit_contract(*, via_tool: bool) -> str:
    """Shared turn-budget / submit contract for analysis workers.

    Agentic swarm workers call ``submit_findings`` on the tool channel. Native
    ProviderWorker analysis mode has no that tool -- it must conclude with a
    structured FINDING/RISK/DECISION summary in the final message instead.
    Either way: never end on free-text planning/reasoning alone.
    """
    format_block = _ANALYSIS_OUTPUT_FORMAT_BLOCK
    if via_tool:
        return (
            "IMPORTANT: You have a limited number of tool-call turns. Do a focused "
            "investigation (a handful of reads/searches), then ALWAYS call "
            "submit_findings with whatever concrete findings you have BEFORE you run "
            "out of turns. A few well-evidenced findings submitted is far better than "
            "a deep exploration that never submits. If unsure, submit early and stop. "
            "Do not end on planning or mid-thought reasoning alone "
            "(e.g. 'Now let me look at...'). "
            "Typed tool payloads must use type finding/risk/decision with concrete "
            "headlines (not a single unlabeled blob).\n\n"
            f"{format_block}"
        )
    return (
        "IMPORTANT: You have a limited number of turns. Do a focused "
        "investigation (a handful of reads/searches), then ALWAYS end with a "
        "structured findings summary in your final message -- label concrete "
        "FINDING/RISK/DECISION lines with file:line evidence -- BEFORE you run "
        "out of turns. A few well-evidenced findings concluded is far better than "
        "a deep exploration that never concludes. Do not end on planning or "
        "mid-thought reasoning alone (e.g. 'Now let me look at...').\n\n"
        f"{format_block}"
    )


def _analysis_instruction(goal: str, repo_cwd: str, role: str,
                          *, browser: bool = False,
                          via_tool: bool = True,
                          acceptance_criteria: Optional[list] = None) -> str:
    """Build a read-only analysis worker's instruction from the shared goal plus
    the role's lens, so a multi-role swarm fans out into distinct investigations
    rather than N identical passes over the same goal.

    When ``browser`` is set the worker is told it has the live browser toolset
    (browser_navigate/browser_snapshot/browser_get_text/...) so a live-site task
    drives a real page instead of only reading source. Browsing stays read-only:
    it must not edit, create, or delete files.

    ``via_tool=False`` adapts the submit contract for native ProviderWorker
    analysis (final-message findings summary instead of submit_findings).

    ``acceptance_criteria`` is an optional explicit checklist. When absent,
    nothing is inferred from goal prose.

    ``repo_cwd`` is resolved through ``resolve_effective_repo`` at this last mile
    so a Marionette Home parent (non-git) never appears in the brief when a
    single git child checkout exists.
    """
    from harness.repo_resolve import resolve_effective_repo
    from harness.git_upstream import maybe_git_upstream_brief
    repo_cwd = resolve_effective_repo(repo_cwd or "")
    lens = ROLE_LENSES.get(role, "")
    lens_line = f"\n\n{lens}" if lens else ""
    criteria_block = ""
    try:
        from harness.environment_fingerprint import format_acceptance_criteria_block
        criteria_block = format_acceptance_criteria_block(acceptance_criteria or [])
    except Exception:
        criteria_block = ""
    criteria_line = f"\n\n{criteria_block}" if criteria_block else ""
    git_brief = maybe_git_upstream_brief(repo_cwd)
    git_block = f"\n\n{git_brief}" if git_brief else ""
    worktree_notice = (
        "\n\nExecution provenance (provider, model, tokens, cost, routing) comes "
        "from the Marionette job envelope, not from repository source files. "
        "Your git status describes a disposable managed worker worktree, "
        "not the user's live checkout. Only describe this disposable worktree's "
        "diff status; do not claim the user's checkout is clean or dirty from "
        "this worktree's status."
    )
    current_dispatch_notice = (
        "\n\nCURRENT-DISPATCH EVIDENCE RULE: active skills, distilled memory, and "
        "conclusions from any prior transcript or audit are METHODOLOGY AND "
        "CONTEXT ONLY -- never current findings, and never proof that an issue "
        "exists now. Every claim you submit must be supported by evidence from "
        "the subject code or checks you inspect in THIS dispatch (cite path:line "
        "or command evidence), then mapped to any explicit acceptance criterion "
        "above. A criterion states what to prove; it is not proof by itself. "
        "If you cannot run or observe a check here, "
        "report it as not_verified with what is missing; do not report it as a "
        "defect, and do not restate a remembered issue as if you just found it."
    )
    submit = _analysis_submit_contract(via_tool=via_tool)
    if browser:
        submit_tail = (
            "then ALWAYS call submit_findings before you run out of turns."
            if via_tool else
            "then ALWAYS end with a structured FINDING/RISK/DECISION summary "
            "before you run out of turns."
        )
        return (
            f"{goal}{lens_line}{criteria_line}\n\nYou have a real headless browser. Use the "
            f"browser tools to complete this: browser_navigate(url) to open a "
            f"page, then browser_snapshot() to list interactable elements with "
            f"@e-style refs, browser_get_text() for the readable page text, and "
            f"browser_click/browser_type/browser_scroll/browser_back as needed. "
            f"This is READ-ONLY: do not edit, create, or delete any files, and "
            f"do not submit credentials or perform destructive actions on the "
            f"site. Emit what each browser tool returned as evidenced findings, "
            f"{submit_tail}{git_block}\n\n"
            f"{submit}{worktree_notice}{current_dispatch_notice}\n\n{_STOP_CONDITIONS}"
        )
    return (
        f"{goal}{lens_line}{criteria_line}\n\nAnalyze the REAL codebase at {repo_cwd}. "
        f"Emit evidenced findings/risks/decisions as artifacts. This is "
        f"a READ-ONLY analysis: do not edit, create, or delete any files."
        f"{git_block}\n\n"
        # Turn-budget guardrail: broad-audit workers on cheaper models were
        # burning every turn exploring and hitting max_turns WITHOUT ever
        # calling submit_findings -- surfacing as a "completed without
        # structured findings" degrade. Tell the worker to budget explicitly
        # and always submit what it has rather than exhausting its turns.
        f"{submit}{worktree_notice}{current_dispatch_notice}\n\n{_STOP_CONDITIONS}"
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


def _harness_to_puppetmaster_provider_slug(name: str) -> str:
    """Map a harness provider reach name to Puppetmaster's agentic slug."""
    try:
        from harness.auto_registry import _AGENTIC_PROVIDER_SLUGS

        return _AGENTIC_PROVIDER_SLUGS.get(name, name)
    except Exception:
        return name


def _sync_agentic_credential_env() -> None:
    """Best-effort Marionette → Puppetmaster credential boundary.

    Invokes :func:`harness.providers.available_providers` so healthy
    credential-pool tokens are mirrored into ``os.environ`` for Puppetmaster's
    agentic providers, and exports disconnected reaches as
    ``PUPPETMASTER_DISABLED_PROVIDERS`` (comma-separated Puppetmaster slugs,
    empty when none).

    Swallows all failures so offline/unit paths stay unchanged. Never logs or
    returns secret values.
    """
    import os

    try:
        from harness.keys import get_disconnected

        slugs = sorted(
            _harness_to_puppetmaster_provider_slug(n) for n in get_disconnected()
        )
        os.environ["PUPPETMASTER_DISABLED_PROVIDERS"] = ",".join(slugs)
    except Exception:
        pass

    try:
        from harness.providers import available_providers
        from harness.registry_wizard import get_provider_key
        from harness.credential_pool import (
            _mirror_pool_token_to_env,
            providers_for_env_var,
        )
    except Exception:
        return

    try:
        for p in available_providers():
            try:
                key = get_provider_key(p)
                key_env = p.key_env()
                if key and key_env:
                    os.environ[key_env] = key
                _mirror_pool_token_to_env(p.name)
                for ev in p.env_vars or ():
                    for prov in providers_for_env_var(ev):
                        _mirror_pool_token_to_env(prov)
            except Exception:
                continue
    except Exception:
        pass


@dataclass
class BridgeResult:
    job_id: str
    status: str
    mode: str
    num_artifacts: int  # == len(artifacts): describes the SURFACED compact list,
    #   not the raw orchestrator output. Reporting the raw count while surfacing a
    #   filtered list is how a run with only dropped reasoning fragments claimed
    #   "N artifacts" the user could never see.
    artifact_types: list  # types present in ``artifacts`` (same surfaced list)
    summary: str
    artifacts: list  # list of compact dicts (type, claim/decision/etc snippet)
    auth_failure: str = ""  # loud one-liner when a provider rejected the key
    #   (dead/revoked/wrong key). Empty when no auth failure occurred. Lets the
    #   harness flag the real cause instead of "completed without findings".
    adapter: str = "demo"  # "demo" = local deterministic substrate (not real
    #   codebase analysis); set to a real worker adapter when configured. Surfaces
    #   use this to label generic substrate so it is never mistaken for real
    #   findings.
    raw_num_artifacts: int = 0  # artifacts the orchestrator produced, pre-filter
    dropped_artifacts: int = 0  # raw - surfaced, so filtering stays observable


def _compact_artifact(a: Any) -> dict:
    """Reduce a Puppetmaster Artifact to a small dict suitable for feeding back
    to a driver model on a follow-up turn without blowing context."""
    payload = getattr(a, "payload", {}) or {}
    failure = str(payload.get("failure") or "") or None
    fail_l = (failure or "").strip().lower()
    art_type = str(getattr(a, "type", "") or "").lower()
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
    # Degraded verification (any adapter) often has a plumbing `summary`
    # ("completed without structured findings") AND the real audit in `stdout`.
    # Prefer stdout first so promote/rescue sees the analysis, not the meta phrase.
    _summary_l = str(payload.get("summary") or "").strip().lower()
    _plumbing_summary = (
        "without structured findings" in _summary_l
        or "no structured findings" in _summary_l
        or "never called any tool" in _summary_l
        or "verification/plumbing" in _summary_l
    )
    if art_type == "verification" and (
        fail_l == "empty_or_unstructured_agentic_result" or _plumbing_summary
    ):
        _headline_keys = (
            "claim", "decision", "risk", "check",
            "stdout",
            "summary", "change",
            "report", "mitigation", "why", "result", "observation",
            "note", "detail", "message", "text", "body", "content",
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
    stdout_text = payload.get("stdout")
    if (
        art_type == "verification"
        and isinstance(stdout_text, str)
        and stdout_text.strip()
    ):
        # Any adapter may park real analysis in verification.stdout while a
        # plumbing summary occupies headline keys. Prefer stdout when the
        # headline is a known plumbing phrase (or the unstructured-agentic
        # degrade tag) so promote/rescue sees substantive prose before the
        # send_loop_dispatch quality gate — never leave capable models as
        # plumbing-only when real text exists. Do not steal a clean
        # structured headline just because stdout is longer.
        stdout_s = stdout_text.strip()
        head_l = str(headline or "").lower()
        plumbing_head = (
            "without structured findings" in head_l
            or "no structured findings" in head_l
            or "never called any tool" in head_l
            or "verification/plumbing" in head_l
            or head_l in ("passed", "ok", "done", "complete", "completed")
        )
        prefer_stdout = (
            fail_l == "empty_or_unstructured_agentic_result" or plumbing_head
        )
        if prefer_stdout and len(stdout_s) >= 40:
            if len(stdout_s) >= len(body) or plumbing_head:
                body = stdout_s
            if plumbing_head or fail_l == "empty_or_unstructured_agentic_result":
                headline = stdout_text
                empty_headline = False
                body = stdout_s
    if empty_headline and payload:
        headline = str(getattr(a, "type", "") or "artifact")
    # Never let a truncated reasoning fragment become the digest/patch headline.
    # Keep the full body for diagnosis, but label the headline honestly when the
    # worker failed the submit contract (or only produced mid-thought prose).
    # Leave genuinely empty headlines alone (empty_headline stays True).
    headline_text = str(headline or "").strip()
    if headline_text and art_type in (
        "verification", "finding", "risk", "decision", "patch",
    ):
        is_no_structure = (
            fail_l in _NO_STRUCTURE_FAILURES or fail_l.startswith("no_tool_calls")
        )
        if is_no_structure or _looks_like_reasoning_fragment(headline_text):
            meta = _is_meta_degrade_artifact(
                {"failure": failure, "headline": headline_text}
            )
            # Meta-risks that already name the degrade keep their diagnosis
            # headline; only rewrite free-text reasoning masquerades.
            if _looks_like_reasoning_fragment(headline_text) and (
                is_no_structure or not meta
            ):
                headline = (
                    f"no structured findings ({failure})"
                    if failure else "no structured findings"
                )
                empty_headline = False
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
    compact = {
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
    compact.update(_artifact_provenance(a, payload))
    return compact


# Bounds on the provenance carried alongside a compact artifact. The point of
# compaction is that a follow-up driver turn stays cheap, so identity travels
# but bulk does not.
_MAX_EVIDENCE_LOCI = 8
_MAX_LOCUS_CHARS = 240
_MAX_ID_CHARS = 128
_MAX_CRITERIA = 12

# The only execution-provenance fields that survive compaction. Spend numbers
# are deliberately absent: the job envelope owns tokens/cost, and copying them
# onto every child row is how a run grew fabricated per-artifact receipts.
_SANITIZED_PROVENANCE_FIELDS = (
    "adapter",
    "model",
    "adapter_model_name",
    "router_model_id",
    "usage_known",
    "cost_known",
)


def _bounded_text_list(values: Any, *, limit: int, width: int) -> list:
    out = []
    for value in values or ():
        text = str(value or "").strip()
        if not text:
            continue
        out.append(text[:width])
        if len(out) >= limit:
            break
    return out


def _bounded_criteria_records(values: Any, *, limit: int, width: int) -> list:
    """Preserve explicit string citations and structured status dicts, bounded."""
    out: list = []
    for value in values or ():
        if isinstance(value, dict):
            criterion = str(value.get("criterion") or value.get("text") or "").strip()
            if not criterion:
                continue
            row: dict[str, Any] = {"criterion": criterion[:width]}
            status = str(value.get("status") or "").strip().lower()
            if status:
                row["status"] = status[:32]
            evidence = value.get("evidence")
            if evidence is not None and str(evidence).strip():
                row["evidence"] = str(evidence).strip()[:width]
            out.append(row)
        elif isinstance(value, str):
            text = value.strip()
            if text:
                out.append(text[:width])
        if len(out) >= limit:
            break
    return out


def _intent_acceptance_criteria(intent: DriverIntent) -> list[str]:
    from harness.environment_fingerprint import normalize_acceptance_criteria
    return normalize_acceptance_criteria(getattr(intent, "acceptance_criteria", None) or [])


def _payload_with_acceptance_criteria(payload: dict, criteria: Optional[list]) -> dict:
    from harness.environment_fingerprint import normalize_acceptance_criteria
    clean = normalize_acceptance_criteria(list(criteria or ()))
    if not clean:
        return payload
    stamped = dict(payload)
    stamped["acceptance_criteria"] = clean
    return stamped


def _artifact_provenance(a: Any, payload: dict) -> dict:
    """Identity, evidence loci, and sanitized provenance for one artifact.

    A compact row that drops ``execution_ref`` reads as unattributed forever
    after — the acceptance-criteria and provenance counters treat it as "not
    from this job" and honestly report 0/N for a run that in fact produced
    everything. The Puppetmaster ``Artifact`` already knows its ``job_id`` /
    ``task_id``, so carry them through instead of reconstructing (or guessing)
    them downstream.
    """
    job_id = str(getattr(a, "job_id", "") or "").strip()[:_MAX_ID_CHARS]
    task_id = str(getattr(a, "task_id", "") or "").strip()[:_MAX_ID_CHARS]
    out: dict = {
        "id": str(getattr(a, "id", "") or "").strip()[:_MAX_ID_CHARS],
        "job_id": job_id,
        "task_id": task_id,
    }
    evidence = _bounded_text_list(
        getattr(a, "evidence", None),
        limit=_MAX_EVIDENCE_LOCI,
        width=_MAX_LOCUS_CHARS,
    )
    if evidence:
        out["evidence"] = evidence
        out["evidence_locus"] = evidence[0]
    # Only an explicit worker-supplied checklist travels; nothing is inferred
    # from goal prose here or anywhere downstream.
    criteria = _bounded_criteria_records(
        payload.get("acceptance_criteria")
        if isinstance(payload.get("acceptance_criteria"), (list, tuple))
        else (),
        limit=_MAX_CRITERIA,
        width=_MAX_LOCUS_CHARS,
    )
    if criteria:
        out["acceptance_criteria"] = criteria
    raw_provenance = payload.get("execution_provenance")
    if isinstance(raw_provenance, dict):
        sanitized = {
            field: raw_provenance[field]
            for field in _SANITIZED_PROVENANCE_FIELDS
            if raw_provenance.get(field) not in (None, "")
        }
        if sanitized:
            out["execution_provenance"] = sanitized
    if job_id:
        ref = {"job_id": job_id}
        if task_id:
            ref["task_id"] = task_id
        out["execution_ref"] = ref
    return out


_SIGNAL_TYPES = frozenset({"finding", "risk", "decision"})

# Failure tags that mean the worker never submitted structured findings (tool
# channel unused, or free-text only). These are NOT real audit findings -- they
# are degrade markers. Never promote their stdout into finding headlines.
_NO_STRUCTURE_FAILURES = frozenset({
    "no_tool_calls",
    "empty_or_unstructured_agentic_result",
    # Provider/routing failures are diagnostics, never worker analysis.  In
    # particular, their stdout often repeats the original goal verbatim.
    "no_model",
    "unknown_provider",
    "route_failed",
    "sdk_not_installed",
    "provider_not_configured",
    "routing_failed",
    "auth_failed",
    # Transport / quota / capacity deaths. A worker that timed out or was
    # rejected by the provider has NO analysis to report; whatever prose sits
    # in its stdout is a truncated mid-thought or a repeat of the goal.
    "timeout",
    "timed_out",
    "model_not_found",
    "insufficient_credits",
    "no_credentials",
    "context_length_exceeded",
    "provider_error",
    "http_status:429",
    "http_status:500",
})


# Failure tag that means the worker wrote free-text analysis but never called
# submit_findings / never labelled FINDING lines. Unlike timeout/auth/route,
# the stdout may still be real audit prose worth promoting.
_PROMOTABLE_UNSTRUCTURED_FAILURE = "empty_or_unstructured_agentic_result"


def _is_nonpromotable_failure(failure: object) -> bool:
    """True when a compact artifact must never be promoted to a finding.

    Non-empty failure tags disqualify promotion by default — a tagged failure
    is positive evidence the run died (timeout / auth / route / ...). The sole
    exception is ``empty_or_unstructured_agentic_result``: that tag parks real
    analysis prose when the worker skipped structured submit, so it remains
    promotable when the body is substantive.
    """
    fail = str(failure or "").strip()
    if not fail:
        return False
    if fail.lower() == _PROMOTABLE_UNSTRUCTURED_FAILURE:
        return False
    return True


def _meta_degrade_only_empty_unstructured(a: dict) -> bool:
    """True when a meta-degrade row still carries promotable analysis prose.

    Allows promotion when failure is ``empty_or_unstructured_agentic_result``
    OR when failure is empty (any adapter parked prose under a plumbing
    headline). Plumbing phrases in the *headline* alone do not block
    promotion when the full body is substantive. Fail closed when the body
    itself is thin, a reasoning fragment, or plumbing-only. Other meta
    markers (no_tool_calls, auth/route tags) stay non-promotable.
    """
    try:
        fail = str(a.get("failure") or "").strip().lower()
        if fail and fail != _PROMOTABLE_UNSTRUCTURED_FAILURE:
            return False
        # Prefer body over headline — compact rows often keep a plumbing
        # summary in headline while parking real analysis in body/stdout.
        body = str(a.get("body") or a.get("headline") or "").strip()
        if len(body) < 40:
            return False
        if _looks_like_reasoning_fragment(body):
            return False
        body_l = body.lower()
        if "without structured findings" in body_l:
            return False
        if "never called any tool" in body_l:
            return False
        if "no structured findings" in body_l:
            return False
        return True
    except Exception:
        return False


def _ensure_finding_label(body: str) -> str:
    """Prefix ``FINDING: `` when promoted prose lacks typed signal labels."""
    text = (body or "").strip()
    if not text:
        return text
    low = text.lower()
    if (
        low.startswith("finding:")
        or low.startswith("risk:")
        or low.startswith("decision:")
        or "\nfinding:" in low
        or "\nrisk:" in low
        or "\ndecision:" in low
    ):
        return text
    return f"FINDING: {text}"

# Planning / mid-thought openers that must never masquerade as a finding headline.
_REASONING_FRAGMENT_PREFIXES = (
    "now let me",
    "let me look",
    "let me check",
    "let me see",
    "let me examine",
    "let me read",
    "let me start",
    "i'll start",
    "i will start",
    "i'll look",
    "i'll check",
    "i'll read",
    "i need to",
    "i'm going to",
    "i am going to",
    "first, i",
    "first i'll",
    "first i will",
    "okay, let me",
    "ok, let me",
    "ok let me",
    "hmm,",
    "wait,",
    "looking at",
    "next i",
    "next, i",
)


def looks_like_reasoning_fragment(text: object) -> bool:
    """True when ``text`` is free-text planning / mid-thought, not a finding.

    Canonical shared contract for harness workers and the bridge submit gate.
    Analysis workers that stream chain-of-thought and never submit structure
    used to surface truncated openers like 'Now let me look at...' as the
    patch/finding headline. Treat those as non-findings so the job fails
    degraded and the pilot can re-dispatch.
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    low = raw.lower()
    # Strip a common "Last assistant message:" wrapper from ProviderWorker.
    for prefix in ("last assistant message:", "halt reason:"):
        if low.startswith(prefix):
            raw = raw[len(prefix):].strip()
            low = raw.lower()
            if not raw:
                return True
    # Structured labels only -- a bare path cite inside "Let me check foo.py"
    # is still mid-thought planning, not a submitted finding.
    finding_markers = (
        "finding:", "findings:", "risk:", "decision:", "audit complete",
        "issue:", "vulnerability:", "recommend:", "recommended:",
    )
    has_finding_shape = any(m in low for m in finding_markers)
    if any(low.startswith(p) for p in _REASONING_FRAGMENT_PREFIXES):
        return not has_finding_shape
    # Truncated mid-thought ellipsis without finding shape.
    if (raw.endswith("...") or raw.endswith("\u2026")) and not has_finding_shape:
        if len(raw) < 160:
            return True
    return False


# Back-compat alias for existing tests and call sites.
_looks_like_reasoning_fragment = looks_like_reasoning_fragment


def _is_meta_degrade_artifact(a: dict) -> bool:
    """True for plumbing RISK/verification rows that only report 'no findings'."""
    try:
        fail = str(a.get("failure") or "").strip().lower()
        if (
            fail in _NO_STRUCTURE_FAILURES
            or fail.startswith("no_tool_calls")
            or fail.startswith("auth")
            or fail.startswith("routing")
            or fail.startswith("route_")
        ):
            return True
        head = str(a.get("headline") or a.get("body") or "").lower()
        if "without structured findings" in head:
            return True
        if "never called any tool" in head:
            return True
        if "no structured findings" in head:
            return True
        return False
    except Exception:
        return False


def _has_real_structured_findings(compact: list) -> bool:
    """True when compact artifacts include a real FINDING/RISK/DECISION signal.

    Excludes meta degrade markers (no_tool_calls / without structured findings)
    and free-text reasoning fragments so a swarm that only streamed thought
    cannot report clean completion.
    """
    try:
        for a in compact or []:
            if str(a.get("type") or "") not in _SIGNAL_TYPES:
                continue
            if a.get("empty_headline"):
                continue
            if _is_meta_degrade_artifact(a):
                continue
            text = str(a.get("body") or a.get("headline") or "").strip()
            if not text:
                continue
            if _looks_like_reasoning_fragment(text):
                continue
            return True
        return False
    except Exception:
        return False


def _worker_submitted_structure(compact: list) -> bool:
    """True when a verification row shows the structured submit channel succeeded.

    An honest ``submit_findings([])`` (found nothing) is a clean pass -- it must
    not be rewritten as 'no structured findings'. Only missing/failed submits
    (no_tool_calls / unstructured) should degrade.
    """
    try:
        saw_clean = False
        for a in compact or []:
            if str(a.get("type") or "") != "verification":
                continue
            if _is_auth_failure_tag(a.get("failure"), a.get("headline")):
                return False
            fail = str(a.get("failure") or "").strip().lower()
            if fail in _NO_STRUCTURE_FAILURES or fail.startswith("no_tool_calls"):
                return False
            if fail:
                continue
            saw_clean = True
        return saw_clean
    except Exception:
        return False


def _analysis_bridge_status(compact: list, *, job_status: str, summary: str,
                            auth_note: str = "") -> tuple[str, str]:
    """Normalize swarm BridgeResult status/summary for the submit contract.

    A worker that produced no structured findings must never report as a clean
    completed success with a reasoning fragment as the artifact headline.
    """
    if (auth_note or "").strip():
        return str(job_status or ""), summary or ""
    if _has_real_structured_findings(compact):
        return str(job_status or ""), summary or ""
    # Honest empty submit (structured channel used, zero findings) stays clean.
    if _worker_submitted_structure(compact):
        return str(job_status or ""), summary or ""
    reason = "no structured findings"
    for a in compact or []:
        fail = str(a.get("failure") or "").strip()
        if fail in _NO_STRUCTURE_FAILURES or fail.startswith("no_tool_calls"):
            reason = f"no structured findings ({fail})"
            break
        if str(a.get("type") or "") == "verification" and _looks_like_reasoning_fragment(
            a.get("body") or a.get("headline") or ""
        ):
            reason = "no structured findings (reasoning only)"
    status = str(job_status or "").strip().lower()
    if status in ("", "completed", "complete", "done", "success", "ok", "passed"):
        status = "failed"
    # Preserve an explicit degraded/failed from the orchestrator when present.
    if status not in ("failed", "degraded", "error"):
        status = "failed"
    raw_summary = (summary or "").strip()
    if not raw_summary or _looks_like_reasoning_fragment(raw_summary):
        raw_summary = reason
    elif "without structured findings" not in raw_summary.lower() and (
            "no structured findings" not in raw_summary.lower()):
        raw_summary = f"{reason}: {raw_summary}"
    return status, raw_summary


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


def _inherited_provenance(source: dict) -> dict:
    """Attribution a derived row copies from the compact artifact it restates.

    ``id`` is suffixed rather than reused so the derived row is addressable
    without colliding with its source.
    """
    out: dict = {}
    for key in (
        "job_id", "task_id", "evidence", "evidence_locus",
        "acceptance_criteria", "execution_provenance", "execution_ref",
    ):
        value = source.get(key)
        if value not in (None, "", [], {}):
            out[key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
    source_id = str(source.get("id") or "").strip()
    if source_id:
        out["id"] = f"{source_id}:promoted"
    return out


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
            # Never launder no_tool_calls / auth / route meta-degrades into a
            # synthetic finding. empty_or_unstructured_agentic_result alone is
            # different: it parks real analysis prose when the worker skipped
            # structured submit, so fall through to body-length / reasoning
            # checks instead of skipping.
            if _is_meta_degrade_artifact(a) and not _meta_degrade_only_empty_unstructured(a):
                continue
            # Promotion requires POSITIVE analysis evidence. Timeout /
            # model_not_found / insufficient_credits / http_status:* /
            # provider_error / ... mean the run died; stdout is diagnostics.
            # empty_or_unstructured_agentic_result is the sole tagged exception.
            if _is_nonpromotable_failure(a.get("failure")):
                continue
            # Use the FULL body (untruncated stdout prose), falling back to the
            # display headline. Detection and the promoted finding both rely on
            # the full text so a broad audit's 3000-char analysis survives whole
            # instead of collapsing to the 240-char headline clip.
            body = str(a.get("body") or a.get("headline") or "").strip()
            # Only promote genuine prose analysis, not a one-word "passed"/"blocked"
            # and never a truncated reasoning fragment ("Now let me look at...").
            if len(body) < 40:
                continue
            if _looks_like_reasoning_fragment(body):
                continue
            # Label unlabeled prose so native parse_analysis_signal_rows can
            # extract later; already-labelled bodies stay unchanged.
            body = _ensure_finding_label(body)
            row = {
                "type": "finding",
                # headline stays clipped for display, but the full body is carried
                # verbatim so the pilot/digest can render the real analysis.
                "headline": body[:240],
                "body": body,
                "empty_headline": False,
                "confidence": a.get("confidence"),
                "failure": None,
                "promoted_from": "verification",
            }
            # The promotion is a re-read of THIS job's own verification artifact,
            # so it inherits that row's attribution. Dropping it would make the
            # rescued analysis read as unattributed evidence.
            row.update(_inherited_provenance(a))
            promoted.append(row)
        return promoted
    except Exception:
        return compact


def rescue_analysis_compact(compact: list) -> list:
    """Shared harness-boundary rescue for analysis artifacts.

    Thin public wrapper over ``_promote_degraded_prose`` so agentic edit and
    swarm/bridge paths share one contract: verification-parked
    ``empty_or_unstructured_agentic_result`` prose becomes finding rows before
    the structured-findings gate. Implementation stays in
    ``_promote_degraded_prose`` (Marionette bridge boundary — see WHY there).
    """
    return _promote_degraded_prose(compact)


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


def _bridge_result(
    *,
    job_id: str,
    status: str,
    mode: str,
    raw_num_artifacts: int,
    summary: str,
    artifacts: list,
    auth_failure: str = "",
    adapter: str = "demo",
) -> BridgeResult:
    """Build a BridgeResult whose counts describe the artifacts it carries.

    Single constructor for both dispatch paths so ``num_artifacts`` and
    ``artifact_types`` can never drift from the surfaced compact list after
    promotion and reasoning-fragment filtering. ``dropped_artifacts`` keeps the
    filtering observable instead of silently shrinking the count.
    """
    surfaced = list(artifacts or [])
    raw = max(int(raw_num_artifacts or 0), 0)
    return BridgeResult(
        job_id=job_id,
        status=status,
        mode=mode,
        num_artifacts=len(surfaced),
        artifact_types=sorted({str(a.get("type") or "") for a in surfaced if a.get("type")}),
        summary=summary,
        artifacts=surfaced,
        auth_failure=auth_failure,
        adapter=adapter,
        raw_num_artifacts=raw,
        dropped_artifacts=max(raw - len(surfaced), 0),
    )


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
    criteria = _intent_acceptance_criteria(intent)
    from harness.environment_fingerprint import normalize_acceptance_criteria
    for spec in specs:
        payload = dict(getattr(spec, "payload", None) or {})
        if "token_budget" not in payload:
            payload["token_budget"] = worker_token_budget()
        if criteria and not normalize_acceptance_criteria(payload.get("acceptance_criteria")):
            payload["acceptance_criteria"] = list(criteria)
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
    return _bridge_result(
        job_id=result.job.id,
        status=str(result.job.status),
        mode=str(result.mode),
        raw_num_artifacts=len(artifacts),
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
    dispatch_id: Optional[str] = None,
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

    ``cwd`` / ``repo`` (aliases, else ``intent.repo``) pin the analysis
    workspace for this call, and every worker/prewalk spec below receives that
    path explicitly. Prefer them over the live ``HARNESS_REPO`` env so a
    mid-turn workspace switch cannot retarget a busy runner's swarm. This call
    never writes ``HARNESS_REPO``: the env is process-global, so aligning it
    per dispatch let a second concurrent swarm on a different subject observe
    (and restore) the wrong pointer. The env is read only as the fallback for
    callers that supply no explicit repo at all.
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
    job_label = job_label_for_session(
        session_id or "", dispatch_id=dispatch_id or "",
    )

    # Explicit per-runner cwd wins over the process-wide HARNESS_REPO view pointer.
    # Resolve at this seam so callers that forget resolve_effective_repo still
    # pin workers to the git checkout (Marionette Home parent → single child).
    from harness.repo_resolve import resolve_effective_repo
    explicit_cwd = (cwd or repo or getattr(intent, "repo", None) or "").strip()
    if explicit_cwd:
        repo_cwd = resolve_effective_repo(explicit_cwd)
    else:
        env_repo = (_os.environ.get("HARNESS_REPO") or "").strip()
        repo_cwd = resolve_effective_repo(env_repo) if env_repo else ""

    # Prepare the isolated Marionette catalog (env pin + reconcile + ladder)
    # before any auto_route / child-worker spawn. Preserve an explicit
    # PUPPETMASTER_MODELS_PATH override; never rewrite ~/.puppetmaster/models.json.
    # Failures stay swallowed on the chat hot path, but logged for audit.
    try:
        from harness.marionette_registry import boot_marionette_registry

        boot_marionette_registry()
    except Exception as exc:
        try:
            from harness.diag import note as _diag

            _diag("bridge.execute_intent.boot_marionette_registry", exc)
        except Exception:
            pass

    _sync_agentic_credential_env()

    try:
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
        #   agentic (default with repo) -> REAL LLM analysis via provider keys.
        #   openai          -> REAL LLM analysis of REAL code (OpenAI-compatible).
        #   demo (no-repo / ALLOW_DEMO only) -> built-in local substrate for eval.
        # A live repo NEVER silently falls through to demo -- that produces
        # generic placeholder findings that read as a successful audit.
        try:
            from harness.swarm_worker_route import resolve_product_worker_adapter
            swarm_adapter = resolve_product_worker_adapter()
        except Exception:
            try:
                from harness.swarm_adapter import resolve_bridge_swarm_adapter
                swarm_adapter = resolve_bridge_swarm_adapter(repo_cwd=repo_cwd)
            except Exception:
                swarm_adapter = (_os.environ.get("HARNESS_SWARM_ADAPTER", "demo") or "demo").lower()
                if repo_cwd and swarm_adapter not in ("agentic", "openai", "cursor"):
                    swarm_adapter = "agentic"

        if swarm_adapter == "cursor" and repo_cwd:
            # Platform Cursor SDK workers (CURSOR_API_KEY). Used when no agentic
            # HTTP provider is keyed — not the same as Settings Cursor CLI login.
            _warn_if_unindexed(repo_cwd)
            from puppetmaster.workers import WorkerSpec
            roles = intent.roles or infer_roles(intent.goal)
            specs = []
            for r in roles:
                cursor_payload = _payload_with_acceptance_criteria({
                    "read_only": True, "no_edit": True, "dry_run": True,
                    "cwd": repo_cwd, "prompt": intent.goal,
                    "auto_route": True,
                    "allowed_adapters": ["cursor"],
                    "prefer_plan_billed": True,
                    "max_turns": _analyze_max_turns(),
                    "token_budget": worker_token_budget(),
                    "routing_policy": "balanced",
                }, getattr(intent, "acceptance_criteria", None))
                specs.append(WorkerSpec(
                    role=r,
                    instruction=_analysis_instruction(
                        intent.goal, repo_cwd, r,
                        acceptance_criteria=getattr(
                            intent, "acceptance_criteria", None
                        ),
                    ),
                    adapter="cursor",
                    payload=stamp_task_payload(
                        cursor_payload, session_id=session_id or "", cwd=repo_cwd
                    ),
                ))
            result = Orchestrator(store).run(
                intent.goal, specs=specs, worker_mode=worker_mode or "inline",
                label=job_label,
            )
            adapter = "cursor"
        elif swarm_adapter == "agentic" and repo_cwd:
            # Product swarm path: Settings + platform driven worker allowlist.
            # Agentic (OpenRouter / OpenCode Go / Codex OAuth / …) stays the
            # default primary when keyed, but Models-enabled Cursor
            # Grok/Composer must also be reachable — never hard-lock
            # allowed_adapters=['agentic'] when the union includes cursor.
            # prefer_plan_billed=False whenever any API-billed agentic model
            # is eligible so $0 plan picks do not starve OR cash models.
            _warn_if_unindexed(repo_cwd)
            from puppetmaster.workers import WorkerSpec
            try:
                from harness.swarm_worker_allowlist import (
                    resolve_swarm_worker_allowlist,
                )
                _allow = resolve_swarm_worker_allowlist()
            except Exception:
                _allow = {
                    "allowed_adapters": ["agentic"],
                    "prefer_plan_billed": False,
                    "primary_adapter": "agentic",
                }
            allowed_adapters = list(
                _allow.get("allowed_adapters") or ["agentic"]
            )
            prefer_plan_billed = bool(_allow.get("prefer_plan_billed"))
            primary_adapter = str(
                _allow.get("primary_adapter") or "agentic"
            ).strip().lower() or "agentic"
            roles = intent.roles or infer_roles(intent.goal)
            _browser = _browser_swarm_enabled(intent.goal)
            pinned_model = (getattr(intent, "model", None) or "").strip()
            pin_fields: dict = {}
            pin_adapter = primary_adapter
            if pinned_model:
                # Resolve against the Settings/platform worker adapter union
                # (not agentic-only remap). Unknown pins demote to auto-route.
                from harness.swarm_model_pin import resolve_swarm_model_pin

                resolved = resolve_swarm_model_pin(
                    pinned_model, allowed_adapters=allowed_adapters,
                )
                pin_fields = dict(resolved.get("pin_fields") or {})
                if resolved.get("demoted"):
                    pin_fields = {}
                    pin_adapter = primary_adapter
                elif pin_fields.get("pinned_model"):
                    pin_fields["auto_route"] = False
                    pin_adapter = (
                        str(resolved.get("adapter") or primary_adapter)
                        .strip().lower()
                        or primary_adapter
                    )
            specs = []
            for r in roles:
                base_payload = {
                    "read_only": True, "no_edit": True, "dry_run": True,
                    "cwd": repo_cwd, "prompt": intent.goal,
                    "auto_route": True,
                    # Settings+platform union (agentic / cursor / openai),
                    # never a hard agentic-only lock that rejects Cursor
                    # Grok when Models toggles enable it.
                    "allowed_adapters": list(allowed_adapters),
                    # False whenever API-billed agentic is eligible so OR
                    # cash models are not starved by plan-billed Cursor.
                    "prefer_plan_billed": prefer_plan_billed,
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
                }
                if pin_fields:
                    base_payload.update(pin_fields)
                base_payload = _payload_with_acceptance_criteria(
                    base_payload, getattr(intent, "acceptance_criteria", None),
                )
                specs.append(WorkerSpec(
                    role=r,
                    instruction=_analysis_instruction(
                        intent.goal, repo_cwd, r, browser=_browser,
                        acceptance_criteria=getattr(
                            intent, "acceptance_criteria", None
                        ),
                    ),
                    adapter=pin_adapter,
                    payload=stamp_task_payload(
                        base_payload, session_id=session_id or "", cwd=repo_cwd
                    ),
                ))
            result = Orchestrator(store).run(
                intent.goal, specs=specs, worker_mode=worker_mode or "inline",
                label=job_label,
            )
            adapter = pin_adapter
        elif swarm_adapter == "openai" and repo_cwd:
            _prepare_analysis_env()
            _warn_if_unindexed(repo_cwd)
            from puppetmaster.workers import WorkerSpec
            roles = intent.roles or infer_roles(intent.goal)
            specs = []
            for r in roles:
                openai_payload = _payload_with_acceptance_criteria({
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
                }, getattr(intent, "acceptance_criteria", None))
                specs.append(WorkerSpec(
                    role=r,
                    instruction=_analysis_instruction(
                        intent.goal, repo_cwd, r,
                        acceptance_criteria=getattr(
                            intent, "acceptance_criteria", None
                        ),
                    ),
                    adapter="openai",
                    payload=stamp_task_payload(
                        openai_payload, session_id=session_id or "", cwd=repo_cwd
                    ),
                ))
            # inline: the analysis worker runs in-process so the env-based key
            # wiring propagates reliably, and it yields richer multi-artifact output.
            result = Orchestrator(store).run(
                intent.goal, specs=specs, worker_mode=worker_mode or "inline",
                label=job_label,
            )
            adapter = "openai"
        else:
            # Product path never runs demo. Opt-in eval only.
            try:
                from harness.swarm_adapter import allow_demo_swarm
                _demo_ok = allow_demo_swarm()
            except Exception:
                _demo_ok = False
            if not _demo_ok:
                raise ValueError(
                    "refusing demo substrate in Marionette product path. "
                    "Swarms require HARNESS_SWARM_ADAPTER=agentic (default) "
                    "and a provider key. Set HARNESS_ALLOW_DEMO_SWARM=1 only "
                    "for intentional driver-eval."
                )
            # Eval substrate: local deterministic adapter, no API keys.
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
        # Drop reasoning-fragment "findings" so they never become digest headlines.
        compact = [
            a for a in compact
            if not (
                str(a.get("type") or "") in _SIGNAL_TYPES
                and _looks_like_reasoning_fragment(
                    a.get("body") or a.get("headline") or ""
                )
                and not _is_meta_degrade_artifact(a)
            )
        ]
        status, summary = _analysis_bridge_status(
            compact,
            job_status=str(result.job.status),
            summary=result.summary or "",
            auth_note=auth_note,
        )
        return _bridge_result(
            job_id=result.job.id,
            status=status,
            mode=str(result.mode),
            raw_num_artifacts=len(artifacts),
            summary=_summary_leading_with_auth(summary, auth_note),
            artifacts=compact,
            auth_failure=auth_note,
            adapter=adapter,
        )
    finally:
        _clear_delta_sink()
