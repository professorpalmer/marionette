"""Deterministic current-run evidence contract for swarm results.

A swarm digest used to end with prose telling the pilot to trust only the
current job. Prose is not evidence: the pilot could still recycle a conclusion
from three turns ago and no reader could tell which run produced which line.

This module answers the same question with facts a reader can check:

* WHICH build ran — Marionette version, installed Puppetmaster version, the
  resolved state root, and the exact subject cwd the workers analyzed.
* WHAT was actually returned — artifact counts by type, and how many
  non-routing artifacts carry an execution ref back to *this* job (always
  reported as ``N/M``, including ``0/M``).
* WHICH criteria are settled — every explicit acceptance criterion echoed with
  ``verified`` / ``not_verified`` plus the artifact that grounds it.
* WHAT the environment could not prove — optional prerequisites (browser,
  pyright, tsc) reported as ``not_verified`` with a remedy. A missing optional
  tool is a readiness gap, never a product finding or risk.

Everything here is pure or probe-backed and free of secrets: MCP servers are
reported by name only, never by command, url, header, or env value.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

VERIFIED = "verified"
NOT_VERIFIED = "not_verified"

# Why an optional prerequisite is unproven. Neither value is a defect.
CLASSIFICATION_AVAILABLE = "available"
CLASSIFICATION_UNAVAILABLE = "unavailable"
CLASSIFICATION_POLICY = "policy"

# file/path.ext or file/path.ext:123 — the first one in an artifact is its
# evidence locus, the thing a reader can open to check the claim.
_LOCUS_RE = re.compile(
    r"\b([\w./\\-]+\.[A-Za-z0-9]{1,6})(?::(\d+))?\b"
)

# Bare filenames and path segments we treat as checkable loci. Dotted symbols
# such as ``datetime.utcnow`` are rejected unless they look like real paths.
_KNOWN_FILE_EXTENSIONS = frozenset({
    "bash", "c", "cjs", "conf", "cfg", "cpp", "cs", "css", "go", "h", "hpp",
    "html", "ini", "java", "js", "json", "jsx", "kt", "lock", "md", "mjs",
    "php", "ps1", "py", "pyi", "rb", "rs", "rst", "scss", "sh", "sql", "svelte",
    "swift", "toml", "ts", "tsx", "txt", "vue", "wasm", "xml", "yaml", "yml",
    "zsh",
})

_ROUTING_TYPE = "routing"

# Spend numbers belong on the job envelope. A child row that copies them is
# fabricating a receipt (including a fake $0 / 0-token measurement).
_SPEND_FIELDS = ("tokens", "est_cost_usd", "estimated_cost_usd")

# Self-report criteria that ask about stamped identity, not a worker checklist.
# Field names and honesty clauses are enough on their own. Identity phrases
# cover "state your model" without requiring the exact "model id" spelling.
# A scope phrase plus a stamp/model word catches "every artifact carries
# provenance" without letting "document the provenance of the cache bug"
# or "fix the model registry" through. Check criteria stay citation-only.
_PROVENANCE_FIELD_PHRASES = (
    "execution provenance",
    "execution_provenance",
    "usage_known",
    "cost_known",
    "usage known",
    "cost known",
)
_PROVENANCE_HONESTY_PHRASES = (
    "no fake zero",
    "no invented zero",
    "no fabricated zero",
    "no fabricated spend",
)
_PROVENANCE_IDENTITY_PHRASES = (
    "model id",
    "model-id",
    "model_id",
    "model identifier",
    "adapter_model_name",
    "adapter model name",
    "router_model_id",
    "router model id",
    "state your model",
    "state the model",
    "name your model",
    "name the model",
)
_PROVENANCE_SCOPE_PHRASES = (
    "non-routing",
    "non routing",
    "every artifact",
    "all artifacts",
    "each artifact",
    "every finding",
    "all findings",
    "each finding",
    "every row",
    "all rows",
    "each row",
)

# Environment probes are bounded but not free (PATH scans, a Chrome lookup).
# A drain pass that settles several jobs at once must not pay for each one.
_PROBE_TTL_SECONDS = 60.0
_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class ReadinessFact:
    """One optional prerequisite and whether this run could prove it."""

    name: str
    status: str
    classification: str
    detail: str
    remedy: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "classification": self.classification,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class CriterionFact:
    """One explicit acceptance criterion and the evidence that settles it."""

    text: str
    status: str
    basis: str

    def as_dict(self) -> dict[str, str]:
        return {"text": self.text, "status": self.status, "basis": self.basis}


@dataclass(frozen=True)
class SwarmRunFacts:
    """Everything the pilot may treat as established by the current run."""

    job_id: str
    job_status: str
    subject_cwd: str
    state_root: str
    marionette_version: str
    puppetmaster_version: str
    artifact_total: int
    artifact_type_counts: dict[str, int]
    non_routing_total: int
    direct_provenance_total: int
    readiness: tuple[ReadinessFact, ...] = ()
    criteria: tuple[CriterionFact, ...] = ()
    mcp_server_names: tuple[str, ...] = ()
    probe_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_status": self.job_status,
            "subject_cwd": self.subject_cwd,
            "state_root": self.state_root,
            "marionette_version": self.marionette_version,
            "puppetmaster_version": self.puppetmaster_version,
            "artifact_total": self.artifact_total,
            "artifact_type_counts": dict(self.artifact_type_counts),
            "non_routing_total": self.non_routing_total,
            "direct_provenance_total": self.direct_provenance_total,
            "readiness": [fact.as_dict() for fact in self.readiness],
            "criteria": [fact.as_dict() for fact in self.criteria],
            "mcp_server_names": list(self.mcp_server_names),
            "probe_error": self.probe_error,
        }


def clear_probe_cache() -> None:
    """Drop cached environment probes (tests, and after an install changes)."""
    _probe_cache.clear()


def _is_file_path(path: str) -> bool:
    """True when *path* names a file a reader can open, not a dotted symbol."""
    candidate = str(path or "").strip()
    if not candidate:
        return False
    if "/" in candidate or "\\" in candidate:
        return True
    if "." not in candidate:
        return False
    ext = candidate.rsplit(".", 1)[-1].casefold()
    return ext in _KNOWN_FILE_EXTENSIONS


def _extract_locus(text: str) -> str:
    """First concrete ``path[:line]`` in *text*, or empty when none qualify."""
    for match in _LOCUS_RE.finditer(str(text or "")):
        path, line = match.group(1), match.group(2)
        if not _is_file_path(path):
            continue
        return f"{path}:{line}" if line else path
    return ""


def first_evidence_locus(artifact: Mapping[str, Any]) -> str:
    """First ``path[:line]`` an artifact cites, or empty when it cites none."""
    if not isinstance(artifact, Mapping):
        return ""
    for key in ("evidence_locus", "locus", "uri"):
        locus = _extract_locus(str(artifact.get(key) or ""))
        if locus:
            return locus
    # Puppetmaster artifacts carry their loci structurally; prefer that list
    # over scraping prose, which only guesses at what the worker cited.
    for entry in artifact.get("evidence") or ():
        locus = _extract_locus(str(entry or ""))
        if locus:
            return locus
    for key in ("headline", "body"):
        locus = _extract_locus(str(artifact.get(key) or ""))
        if locus:
            return locus
    return ""


def _parent_job_id(artifact: Mapping[str, Any]) -> str:
    """The job an artifact claims produced it, or empty when unattributed.

    Only ``execution_ref.job_id`` counts. ``run_job_id`` records which run
    *surfaced* the row and is deliberately not accepted here — treating it as
    attribution would let every batch launder its own provenance.
    """
    ref = artifact.get("execution_ref") if isinstance(artifact, Mapping) else None
    if not isinstance(ref, Mapping):
        return ""
    return str(ref.get("job_id") or "").strip()


def normalize_execution_refs(
    artifacts: Iterable[Mapping[str, Any]],
    job_id: str,
) -> list[dict[str, Any]]:
    """Stamp run identity and evidence locus onto a copy of each artifact.

    The stamp records which run *surfaced* the artifact (``run_job_id``) and
    where its evidence lives (``evidence_locus``). It deliberately does NOT
    rewrite ``execution_ref.job_id``: an artifact that arrived without parent
    attribution must keep reading as unattributed, otherwise the provenance
    count would launder itself to N/N.

    Oversized artifact batches and evidence collections are truncated so a
    runaway worker cannot unbounded-expand the evidence surface.
    """
    current = str(job_id or "").strip()
    normalized: list[dict[str, Any]] = []
    for artifact in artifacts or ():
        if len(normalized) >= _MAX_ARTIFACT_ROWS:
            break
        if not isinstance(artifact, Mapping):
            continue
        row = dict(artifact)
        evidence = row.get("evidence")
        if isinstance(evidence, (list, tuple)) and len(evidence) > _MAX_EVIDENCE_ENTRIES:
            row["evidence"] = list(evidence[:_MAX_EVIDENCE_ENTRIES])
        ref = row.get("execution_ref")
        ref = dict(ref) if isinstance(ref, Mapping) else {}
        ref.setdefault("job_id", "")
        ref["run_job_id"] = current
        row["execution_ref"] = ref
        locus = first_evidence_locus(row)
        if locus:
            row["evidence_locus"] = locus
        normalized.append(row)
    return normalized


def attribute_stored_execution_refs(
    artifacts: Iterable[Mapping[str, Any]],
    job_id: str,
) -> list[dict[str, Any]]:
    """Restore refs from authoritative Puppetmaster store rows.

    ``puppetmaster artifacts`` emits ``job_id`` / ``task_id`` at the top level,
    whereas the in-process bridge emits ``execution_ref``. This helper is only
    for that trusted store boundary. It accepts top-level attribution when it
    exactly matches the job being drained and never rewrites foreign rows.
    """
    current = str(job_id or "").strip()
    attributed: list[dict[str, Any]] = []
    for artifact in artifacts or ():
        if not isinstance(artifact, Mapping):
            continue
        row = dict(artifact)
        existing_ref = row.get("execution_ref")
        if not isinstance(existing_ref, Mapping):
            stored_job_id = str(row.get("job_id") or "").strip()
            if current and stored_job_id == current:
                ref = {"job_id": current}
                task_id = str(row.get("task_id") or "").strip()
                if task_id:
                    ref["task_id"] = task_id
                row["execution_ref"] = ref
        attributed.append(row)
    return attributed


def provenance_counts(
    artifacts: Sequence[Mapping[str, Any]],
    job_id: str,
) -> tuple[int, int]:
    """``(direct, non_routing_total)`` for the current job's artifacts."""
    current = str(job_id or "").strip()
    non_routing = [
        artifact for artifact in artifacts
        if isinstance(artifact, Mapping)
        and str(artifact.get("type") or "").strip().lower() != _ROUTING_TYPE
    ]
    direct = sum(
        1 for artifact in non_routing
        if current and _parent_job_id(artifact) == current
    )
    return direct, len(non_routing)


def artifact_type_counts(artifacts: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for artifact in artifacts or ():
        if not isinstance(artifact, Mapping):
            continue
        kind = str(artifact.get("type") or "").strip() or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def digest_line(artifact: Mapping[str, Any], job_id: str) -> str:
    """One digest row that names the run, the locus, and the attribution."""
    kind = str(artifact.get("type") or "unknown").strip() or "unknown"
    headline = str(artifact.get("headline") or "").strip() or "(no headline)"
    locus = first_evidence_locus(artifact) or "none"
    ref_job = _parent_job_id(artifact)
    current = str(job_id or "").strip()
    if ref_job and ref_job == current:
        provenance = "direct"
    elif ref_job:
        provenance = f"foreign:{ref_job}"
    else:
        provenance = "unstamped"
    return (
        f"  - [{kind}] {headline} "
        f"(job={current or 'unknown'}, locus={locus}, provenance={provenance})"
    )


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


_VERIFY_STATUSES = frozenset({"passed", "verified"})
_FAIL_STATUSES = frozenset({"failed", "fail", "error", "blocked", "rejected"})
_MAX_EVIDENCE_ENTRIES = 64
_MAX_ARTIFACT_ROWS = 500


def _criterion_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("criterion") or value.get("text") or "").strip()
    return ""


def _record_evidence_locus(record: Mapping[str, Any]) -> str:
    """Concrete path:line from a structured status row's own evidence only.

    Parent artifact fields (``evidence``, ``evidence_locus``, headline/body,
    or sibling acceptance rows) must never substitute when this row's evidence
    is missing, ``not_reported``, non-path, or malformed.
    """
    evidence = str(record.get("evidence") or "").strip()
    if not evidence or evidence.casefold() == "not_reported":
        return ""
    return _extract_locus(evidence)


def _criterion_status_loci(
    artifact: Mapping[str, Any],
) -> tuple[dict[str, str], set[str]]:
    """Return ``(verified_loci, failed_keys)`` for one artifact.

    Bare string entries are ignored: a worker echoing the checklist or citing
    criteria in prose must not settle them, even when the artifact carries an
    unrelated path:line locus. Only structured status rows with an exact
    criterion and concrete dispatch evidence may contribute. A contradictory
    failed/passed pair for the same criterion refuses verification rather than
    last-wins green — both within one artifact and across artifacts at the
    evaluator.
    """
    verified: dict[str, str] = {}
    failed: set[str] = set()
    for item in artifact.get("acceptance_criteria") or ():
        if isinstance(item, str):
            continue
        if not isinstance(item, Mapping):
            continue
        key = _normalized_text(_criterion_text(item))
        if not key:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status in _FAIL_STATUSES:
            failed.add(key)
            verified.pop(key, None)
            continue
        if status not in _VERIFY_STATUSES:
            continue
        if key in failed:
            continue
        locus = _record_evidence_locus(item)
        if not locus:
            continue
        verified[key] = locus
    for key in failed:
        verified.pop(key, None)
    return verified, failed


def _verified_criterion_loci(artifact: Mapping[str, Any]) -> dict[str, str]:
    """Normalized criterion keys an artifact verifies, mapped to their locus."""
    verified, _failed = _criterion_status_loci(artifact)
    return verified


def _phrase_in(text: str, phrase: str) -> bool:
    return bool(phrase) and phrase in text


def _word_in(text: str, word: str) -> bool:
    """Whole-word match so ``stamp`` does not hit ``timestamp``."""
    if not word or not text:
        return False
    return f" {word} " in f" {text} "


def _is_provenance_self_report_criterion(criterion: str) -> bool:
    """True for stamp/model-id self-report rows, not check criteria."""
    text = _normalized_text(criterion)
    if not text:
        return False
    if any(_phrase_in(text, phrase) for phrase in _PROVENANCE_FIELD_PHRASES):
        return True
    if any(_phrase_in(text, phrase) for phrase in _PROVENANCE_HONESTY_PHRASES):
        return True
    if any(_phrase_in(text, phrase) for phrase in _PROVENANCE_IDENTITY_PHRASES):
        return True
    if not any(_phrase_in(text, phrase) for phrase in _PROVENANCE_SCOPE_PHRASES):
        return False
    return (
        _word_in(text, "provenance")
        or _word_in(text, "stamped")
        or _word_in(text, "stamp")
        or _word_in(text, "model")
    )


def _honest_execution_provenance(artifact: Mapping[str, Any]) -> bool:
    """Stamped identity plus known-flags, and no fabricated spend on the child."""
    prov = artifact.get("execution_provenance")
    if not isinstance(prov, Mapping):
        return False
    model = str(
        prov.get("model")
        or prov.get("adapter_model_name")
        or prov.get("router_model_id")
        or ""
    ).strip()
    if not model:
        return False
    if "usage_known" not in prov or "cost_known" not in prov:
        return False
    if not isinstance(prov.get("usage_known"), bool):
        return False
    if not isinstance(prov.get("cost_known"), bool):
        return False
    bags: tuple[Mapping[str, Any], ...] = (artifact, prov)
    for bag in bags:
        for field in _SPEND_FIELDS:
            if field in bag:
                return False
    return True


def _provenance_self_report_basis(
    criterion: str,
    artifacts: Sequence[Mapping[str, Any]],
    job_id: str,
) -> str:
    """Settle a provenance criterion from stamps, not a worker-cited checklist.

    Every current-job non-routing artifact must already carry honest
    ``execution_provenance``. Zero inspected rows, a missing model id, absent
    known-flags, or copied spend numbers leave the criterion unverified.
    """
    if not _is_provenance_self_report_criterion(criterion):
        return ""
    current = str(job_id or "").strip()
    if not current:
        return ""
    inspected = 0
    for artifact in artifacts or ():
        if not isinstance(artifact, Mapping):
            continue
        if _parent_job_id(artifact) != current:
            continue
        if str(artifact.get("failure") or "").strip():
            continue
        kind = str(artifact.get("type") or "").strip().lower()
        if kind == _ROUTING_TYPE:
            continue
        inspected += 1
        if not _honest_execution_provenance(artifact):
            return ""
    if inspected == 0:
        return ""
    return (
        f"stamped execution_provenance on {inspected} current-job "
        "non-routing artifact(s)"
    )


def evaluate_acceptance_criteria(
    criteria: Sequence[str],
    artifacts: Sequence[Mapping[str, Any]],
    job_id: str,
) -> tuple[CriterionFact, ...]:
    """Echo each criterion with the current-job evidence that settles it.

    A code/check criterion counts as verified only when a successful
    substantive artifact **attributed to THIS job** explicitly maps itself to
    that criterion through its ``acceptance_criteria`` list. Prompt/check prose
    is never searched: a failed worker artifact often repeats the entire
    instruction, and treating that repetition as evidence makes every
    criterion falsely green.

    A narrow second path settles self-report model-id / execution_provenance
    criteria from stamps already on current-job non-routing artifacts. That
    path still fails closed: missing model, missing known-flags, copied spend
    (including fake zeros), or no current-job rows leave the criterion
    ``not_verified``.
    """
    from harness.environment_fingerprint import normalize_acceptance_criteria

    clean = normalize_acceptance_criteria(list(criteria or ()))
    if not clean:
        return ()
    current = str(job_id or "").strip()
    rows = []
    failed_keys: set[str] = set()
    for artifact in artifacts or ():
        if not isinstance(artifact, Mapping):
            continue
        if not current or _parent_job_id(artifact) != current:
            continue
        if str(artifact.get("failure") or "").strip():
            continue
        kind = str(artifact.get("type") or "").strip().lower()
        if kind not in {"finding", "risk", "decision", "verification"}:
            continue
        verified, failed = _criterion_status_loci(artifact)
        failed_keys |= failed
        rows.append((artifact, verified))

    facts = []
    for criterion in clean:
        needle = _normalized_text(criterion)
        if needle and needle in failed_keys:
            facts.append(CriterionFact(
                criterion,
                NOT_VERIFIED,
                "contradictory current-job pass/fail records for this criterion",
            ))
            continue
        basis = ""
        for artifact, verified in rows:
            locus = verified.get(needle) if needle else ""
            if locus:
                kind = str(artifact.get("type") or "artifact").strip() or "artifact"
                basis = f"cited by current-job {kind} at {locus}"
                break
        if basis:
            facts.append(CriterionFact(criterion, VERIFIED, basis))
            continue
        stamp_basis = _provenance_self_report_basis(criterion, artifacts, current)
        if stamp_basis:
            facts.append(CriterionFact(criterion, VERIFIED, stamp_basis))
        elif _is_provenance_self_report_criterion(criterion):
            facts.append(CriterionFact(
                criterion,
                NOT_VERIFIED,
                "current-job non-routing artifacts do not all carry "
                "honest execution_provenance",
            ))
        else:
            facts.append(CriterionFact(
                criterion,
                NOT_VERIFIED,
                "no artifact attributed to job "
                f"{current or 'unknown'} cites this criterion",
            ))
    return tuple(facts)


def _environment_payload(cwd: str) -> dict[str, Any]:
    """Bounded, TTL-cached environment probe. Never raises."""
    key = os.path.normcase(os.path.abspath(cwd)) if cwd else ""
    now = time.monotonic()
    cached = _probe_cache.get(key)
    if cached and (now - cached[0]) < _PROBE_TTL_SECONDS:
        return cached[1]
    try:
        from harness.environment_fingerprint import compute_environment_fingerprint
        payload, reason = compute_environment_fingerprint(cwd, strict=False)
    except Exception as exc:  # noqa: BLE001 - readiness must degrade, not raise
        payload, reason = None, f"environment_probe_failed:{exc.__class__.__name__}"
    result = dict(payload or {})
    if not payload:
        result = {"probe_error": reason or "environment_probe_failed"}
    _probe_cache[key] = (now, result)
    return result


def _tool_readiness(name: str, path: str, remedy: str) -> ReadinessFact:
    if path:
        return ReadinessFact(name, VERIFIED, CLASSIFICATION_AVAILABLE, path)
    return ReadinessFact(
        name, NOT_VERIFIED, CLASSIFICATION_UNAVAILABLE, "not resolved on PATH", remedy,
    )


def probe_readiness(cwd: str) -> tuple[tuple[ReadinessFact, ...], dict[str, Any]]:
    """Optional prerequisites for this run, plus the raw probe payload."""
    payload = _environment_payload(cwd)
    probe_error = str(payload.get("probe_error") or "")
    tool_paths = payload.get("tool_paths") if isinstance(payload.get("tool_paths"), Mapping) else {}

    if probe_error:
        unknown = "environment probe unavailable"
        remedy = "re-run after the environment probe succeeds"
        facts = [
            ReadinessFact(name, NOT_VERIFIED, CLASSIFICATION_UNAVAILABLE, unknown, remedy)
            for name in ("browser", "pyright", "tsc")
        ]
        return tuple(facts), payload

    browser_path = str(payload.get("browser_path") or "")
    facts = [
        ReadinessFact("browser", VERIFIED, CLASSIFICATION_AVAILABLE, browser_path)
        if browser_path else
        ReadinessFact(
            "browser",
            NOT_VERIFIED,
            CLASSIFICATION_UNAVAILABLE,
            "no Chrome/Chromium resolved",
            "install Chrome or set PM_BROWSER_CHROME to its path",
        ),
        _tool_readiness(
            "pyright",
            str(tool_paths.get("pyright") or ""),
            "install pyright (npm i -g pyright) to type-check Python here",
        ),
        _tool_readiness(
            "tsc",
            str(tool_paths.get("tsc") or ""),
            "install TypeScript (npm i -D typescript) in the subject repo",
        ),
    ]
    facts.append(_localhost_policy_fact())
    return tuple(facts), payload


def _localhost_policy_fact() -> ReadinessFact:
    """Loopback browsing is a policy setting, never a harness defect."""
    try:
        from harness.url_safety import allow_private_urls
        allowed = bool(allow_private_urls())
    except Exception:  # noqa: BLE001 - absent policy reads as the safe default
        allowed = False
    if allowed:
        return ReadinessFact(
            "browser_localhost_policy",
            VERIFIED,
            CLASSIFICATION_POLICY,
            "loopback/private URLs permitted (HARNESS_ALLOW_PRIVATE_URLS)",
        )
    return ReadinessFact(
        "browser_localhost_policy",
        NOT_VERIFIED,
        CLASSIFICATION_POLICY,
        "loopback/private URLs blocked by policy",
        "set HARNESS_ALLOW_PRIVATE_URLS=1 to check a local dev server",
    )


def _marionette_version() -> str:
    try:
        from harness import __version__
        return str(__version__ or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def build_swarm_run_facts(
    *,
    job_id: str,
    job_status: str = "",
    subject_cwd: str,
    state_root: str = "",
    artifacts: Optional[Sequence[Mapping[str, Any]]] = None,
    acceptance_criteria: Optional[Sequence[str]] = None,
) -> SwarmRunFacts:
    """Collect the current run's checkable facts. Deterministic; never raises."""
    rows = list(artifacts or ())
    direct, non_routing = provenance_counts(rows, job_id)
    readiness, payload = probe_readiness(subject_cwd)
    names = payload.get("mcp_server_names")
    return SwarmRunFacts(
        job_id=str(job_id or "").strip(),
        job_status=str(job_status or "").strip().lower(),
        subject_cwd=str(subject_cwd or "").strip(),
        state_root=str(state_root or "").strip(),
        marionette_version=_marionette_version(),
        puppetmaster_version=str(payload.get("puppetmaster_version") or ""),
        artifact_total=len(rows),
        artifact_type_counts=artifact_type_counts(rows),
        non_routing_total=non_routing,
        direct_provenance_total=direct,
        readiness=readiness,
        criteria=evaluate_acceptance_criteria(
            list(acceptance_criteria or ()), rows, job_id,
        ),
        mcp_server_names=tuple(str(n) for n in names or () if str(n).strip()),
        probe_error=str(payload.get("probe_error") or ""),
    )


def render_evidence_boundary(facts: SwarmRunFacts) -> str:
    """The pilot-facing evidence block. Facts first, then the trust rules."""
    type_text = ", ".join(
        f"{kind}={facts.artifact_type_counts[kind]}"
        for kind in sorted(facts.artifact_type_counts)
    ) or "none"
    lines = [
        "",
        "CURRENT-JOB EVIDENCE BOUNDARY:",
        f"- Exact current job id: {facts.job_id or 'unknown'}",
        f"- Current job status: {facts.job_status or 'unknown'}",
        f"- Marionette version: {facts.marionette_version or 'unknown'}",
        f"- Puppetmaster version: {facts.puppetmaster_version or 'not installed'}",
        f"- Resolved state root: {facts.state_root or 'unknown'}",
        f"- Subject cwd (read-only audit target): {facts.subject_cwd or 'unknown'}",
        f"- Current returned artifacts: {facts.artifact_total} ({type_text})",
        (
            f"- Direct execution provenance: {facts.direct_provenance_total}/"
            f"{facts.non_routing_total} non-routing artifacts."
        ),
        f"- Configured MCP servers (names only): {', '.join(facts.mcp_server_names) or 'none'}",
    ]
    if facts.probe_error:
        lines.append(f"- Environment probe: {facts.probe_error} (optional checks unproven)")

    if facts.criteria:
        lines.append("- Acceptance criteria:")
        for criterion in facts.criteria:
            lines.append(f"  - [{criterion.status}] {criterion.text} — {criterion.basis}")
    else:
        lines.append("- Acceptance criteria: none supplied (do not invent any)")

    if facts.readiness:
        lines.append("- Environment readiness (optional prerequisites):")
        for fact in facts.readiness:
            remedy = f" — remedy: {fact.remedy}" if fact.remedy else ""
            lines.append(
                f"  - [{fact.status}] {fact.name} ({fact.classification}): "
                f"{fact.detail}{remedy}"
            )

    lines.extend([
        "- Optional prerequisites and policy limits above are readiness facts: "
        "report them as not verified with their remedy, never as a product "
        "finding, risk, or harness defect.",
        "- Prior transcript audit conclusions are historical/untrusted.",
        "- Final claims may use only this job’s returned artifacts or explicit "
        "probes run after it.",
        "- Missing acceptance criteria or checks must be reported as not "
        "verified, never as a defect.",
        "- Never carry earlier issue examples forward merely because they "
        "appear earlier in the transcript.",
        "",
    ])
    return "\n".join(lines)


__all__ = [
    "CLASSIFICATION_AVAILABLE",
    "CLASSIFICATION_POLICY",
    "CLASSIFICATION_UNAVAILABLE",
    "CriterionFact",
    "NOT_VERIFIED",
    "ReadinessFact",
    "SwarmRunFacts",
    "VERIFIED",
    "artifact_type_counts",
    "attribute_stored_execution_refs",
    "build_swarm_run_facts",
    "clear_probe_cache",
    "digest_line",
    "evaluate_acceptance_criteria",
    "first_evidence_locus",
    "normalize_execution_refs",
    "provenance_counts",
    "render_evidence_boundary",
]
