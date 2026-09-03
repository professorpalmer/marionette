"""Honest artifact vocabulary for ``local-*`` sidecar jobs.

One owner for three questions every local-job surface used to answer for itself
(and disagree about):

* Which artifact types are real work vs bookkeeping? ROUTING cards, placeholders,
  and error rows are provenance/diagnostics — a job carrying only those has
  produced nothing a user can read, so ``artifacts_complete`` must stay False.
* What type does a finished job's terminal artifact get? A read-only/analysis
  worker produces a finding summary, never a ``patch``; claiming ``patch`` with
  no files or diff is the sidecar asserting an edit that never happened.
* What is an artifact's stable id? ``artifact://local-*`` addresses used to be
  derived from artifact CONTENT, so the same artifact changed URI when the
  headline was updated at finish time. Ids are assigned explicitly instead.
"""

from typing import Any, Dict, List, Optional

# Rows that describe HOW the job ran (or that it failed), not what it found.
BOOKKEEPING_ARTIFACT_TYPES = frozenset({
    "routing", "placeholder", "error", "bookkeeping",
})

# Job roles that can only ever produce analysis, never a patch.
READ_ONLY_JOB_ROLES = frozenset({
    "analysis", "review", "explore", "read_only", "readonly", "audit", "search",
    "qa",
})

# Terminal artifact types.
ANALYSIS_ARTIFACT_TYPE = "analysis"
PATCH_ARTIFACT_TYPE = "patch"
ERROR_ARTIFACT_TYPE = "error"

# Only these may supply spend/model via execution_ref.terminal_artifact_id.
# ROUTING / finding / risk / decision targets are rejected and fall back.
TERMINAL_EXECUTION_ARTIFACT_TYPES = frozenset({
    ANALYSIS_ARTIFACT_TYPE, PATCH_ARTIFACT_TYPE, ERROR_ARTIFACT_TYPE,
})


def is_bookkeeping_artifact(artifact: Any) -> bool:
    """True for a ROUTING / placeholder / error / bookkeeping row."""
    if not isinstance(artifact, dict):
        return True
    return str(artifact.get("type") or "").strip().lower() in BOOKKEEPING_ARTIFACT_TYPES


def artifacts_are_complete(raw: Any) -> bool:
    """True only when a substantive, readable artifact exists.

    Substantive = not bookkeeping AND carrying a non-empty headline. A job whose
    only rows are a ROUTING card and an ``error`` (or a placeholder) has nothing
    to show, and must not advertise completed artifacts to the UI or to
    ``artifact://`` readers.
    """
    if not isinstance(raw, list):
        return False
    return any(
        not is_bookkeeping_artifact(artifact)
        and bool(str(artifact.get("headline") or "").strip())
        for artifact in raw
        if isinstance(artifact, dict)
    )


def is_read_only_job_role(role: Any) -> bool:
    """True when ``role`` can only produce analysis (never an applied patch)."""
    return str(role or "").strip().lower() in READ_ONLY_JOB_ROLES


def terminal_artifact_type(
    *,
    ok: bool,
    cancelled: bool,
    role: Any,
    has_file_evidence: bool,
) -> str:
    """The honest type for a finished job's terminal artifact.

    ``patch`` requires BOTH a non-read-only role and real file/diff evidence.
    Everything else that succeeded is a finding summary; anything that failed or
    was cancelled is an error row.
    """
    if cancelled or not ok:
        return ERROR_ARTIFACT_TYPE
    if is_read_only_job_role(role) or not has_file_evidence:
        return ANALYSIS_ARTIFACT_TYPE
    return PATCH_ARTIFACT_TYPE


def routing_artifact_id(job_id: str) -> str:
    return f"{job_id}-routing"


def terminal_artifact_id(job_id: str) -> str:
    return f"{job_id}-result"


def finding_artifact_id(job_id: str, index: int) -> str:
    return f"{job_id}-finding-{index}"


# Signal rows that get a lightweight execution_ref (never copied spend fields).
_EXECUTION_REF_TYPES = frozenset({"finding", "risk", "decision"})

# Spend / meter fields that must stay on the job + terminal artifact only.
_SPEND_FIELDS = frozenset({
    "tokens", "tokens_in", "tokens_out", "tokens_cached",
    "est_cost_usd", "estimated_cost_usd", "nominal_cost_usd",
    "cost_provenance",
})


def execution_ref_for(
    job_id: str,
    *,
    task_id: Any = None,
    terminal_artifact_id: Optional[str] = None,
    default_local_terminal: bool = False,
) -> Dict[str, str]:
    """Lightweight parent-execution pointer (no tokens/cost copied).

    When ``default_local_terminal`` is true (local sidecar findings), fill
    ``terminal_artifact_id`` with ``{job_id}-result`` if the caller omitted it.
    Store/swarm rows leave it optional so readers join via job_id alone.
    """
    ref: Dict[str, str] = {"job_id": str(job_id or "")}
    tid = str(task_id or "").strip()
    if tid:
        ref["task_id"] = tid
    terminal = str(terminal_artifact_id or "").strip()
    if not terminal and default_local_terminal and job_id:
        terminal = f"{job_id}-result"
    if terminal:
        ref["terminal_artifact_id"] = terminal
    return ref


def normalize_finding_artifacts(
    job_id: str,
    findings: Optional[list],
) -> List[Dict[str, Any]]:
    """Carry a worker's real structured findings onto the sidecar row.

    Id-stamped so ``artifact://<job>/<id>`` stays resolvable, and
    filtered to substantive rows so plumbing cannot inflate the count.
    Every substantive finding is kept — first-N is not the accounting set.

    Finding / risk / decision rows receive an ``execution_ref`` pointing at the
    parent job (and terminal artifact) so readers can join spend/model without
    duplicating meters onto each child row.
    """
    if not isinstance(findings, list):
        return []
    out = []  # type: List[Dict[str, Any]]
    terminal_id = terminal_artifact_id(job_id)
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        headline = str(finding.get("headline") or finding.get("claim") or "").strip()
        if not headline or is_bookkeeping_artifact(finding):
            continue
        row = dict(finding)
        row["headline"] = headline[:240]
        row["id"] = finding_artifact_id(job_id, len(out))
        kind = str(row.get("type") or "finding").strip().lower()
        if kind in _EXECUTION_REF_TYPES:
            row["execution_ref"] = execution_ref_for(
                job_id,
                task_id=row.get("task_id"),
                terminal_artifact_id=terminal_id,
                default_local_terminal=True,
            )
            # Never persist copied spend on child signal rows.
            for key in _SPEND_FIELDS:
                row.pop(key, None)
        out.append(row)
    return out


def resolve_execution_provenance(
    artifact: Dict[str, Any],
    job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Hydrate display provenance from ``execution_ref`` → job / terminal.

    Returns adapter/model/result/tokens/cost from the parent job (preferring the
    terminal artifact when present). Never mutates ``artifact``. Empty dict when
    there is nothing to resolve — legacy rows without ``execution_ref`` still
    work when ``job`` is supplied.
    """
    if not isinstance(artifact, dict):
        return {}
    ref = artifact.get("execution_ref")
    if not isinstance(ref, dict):
        ref = {}
    parent = job if isinstance(job, dict) else None
    if parent is None:
        return {}
    terminal_id = str(ref.get("terminal_artifact_id") or "").strip()
    terminal: Optional[Dict[str, Any]] = None
    if terminal_id:
        for art in parent.get("artifacts") or []:
            if not isinstance(art, dict) or str(art.get("id") or "") != terminal_id:
                continue
            # Same-job id alone is not enough — only honor terminal execution
            # types so a forged ROUTING/finding/risk/decision pointer cannot
            # supply model/tokens/cost.
            kind = str(art.get("type") or "").strip().lower()
            if kind in TERMINAL_EXECUTION_ARTIFACT_TYPES:
                terminal = art
            break
    if terminal is None:
        # Fall back to the job's terminal-shaped row (analysis/patch/error).
        for art in parent.get("artifacts") or []:
            if not isinstance(art, dict):
                continue
            t = str(art.get("type") or "").strip().lower()
            if t in TERMINAL_EXECUTION_ARTIFACT_TYPES:
                terminal = art
                break
    src = terminal if isinstance(terminal, dict) else parent
    out: Dict[str, Any] = {}
    for key in (
        "adapter", "model", "result", "tokens", "est_cost_usd",
        "cost_provenance", "worker_provenance",
    ):
        value = src.get(key) if isinstance(src, dict) else None
        if value in (None, "", [], {}):
            value = parent.get(key)
        if value in (None, "", [], {}):
            continue
        out[key] = value
    if ref.get("job_id"):
        out["job_id"] = ref["job_id"]
    if ref.get("task_id"):
        out["task_id"] = ref["task_id"]
    if ref.get("terminal_artifact_id"):
        out["terminal_artifact_id"] = ref["terminal_artifact_id"]
    return out


def stamp_provenance(
    artifact: Dict[str, Any],
    provenance: Dict[str, Any],
) -> Dict[str, Any]:
    """Attach adapter/model/tokens/cost provenance without overwriting values."""
    out = dict(artifact)
    for key, value in provenance.items():
        if value in (None, "", 0, 0.0):
            continue
        out.setdefault(key, value)
    return out
