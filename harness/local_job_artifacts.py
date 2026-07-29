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
})

# Terminal artifact types.
ANALYSIS_ARTIFACT_TYPE = "analysis"
PATCH_ARTIFACT_TYPE = "patch"
ERROR_ARTIFACT_TYPE = "error"

_MAX_FINDING_ARTIFACTS = 20


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


def normalize_finding_artifacts(
    job_id: str,
    findings: Optional[list],
) -> List[Dict[str, Any]]:
    """Carry a worker's real structured findings onto the sidecar row.

    Bounded and id-stamped so ``artifact://<job>/<id>`` stays resolvable, and
    filtered to substantive rows so plumbing cannot inflate the count.
    """
    if not isinstance(findings, list):
        return []
    out = []  # type: List[Dict[str, Any]]
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        headline = str(finding.get("headline") or finding.get("claim") or "").strip()
        if not headline or is_bookkeeping_artifact(finding):
            continue
        row = dict(finding)
        row["headline"] = headline[:240]
        row["id"] = finding_artifact_id(job_id, len(out))
        out.append(row)
        if len(out) >= _MAX_FINDING_ARTIFACTS:
            break
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
