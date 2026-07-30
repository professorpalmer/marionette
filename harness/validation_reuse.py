"""Dependency-aware incremental validation reuse (Marionette policy owner).

Single owner for:

* source validation fingerprints on terminal analysis / review / explore jobs
* prior-job lookup keyed by normalized objective + analysis role class
* read-before-dispatch gate outcomes: ``reuse`` | ``narrow_verify`` | ``full_swarm``
* git-diff + CodeGraph-affected invalidation scoping
* compact ``artifact://`` delta digests with honest reuse provenance

Prefer Puppetmaster 1.21.3+ ``puppetmaster.validation`` helpers when importable.
Older / missing installs fail closed to ``full_swarm`` without raising ImportError.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

from .environment_fingerprint import (
    ENVIRONMENT_FINGERPRINT_SCHEMA,
    ENVIRONMENT_FINGERPRINT_VERSION,
    compute_environment_fingerprint,
    format_acceptance_criteria_block,
    job_environment_fingerprint,
    job_environment_fingerprint_schema,
    match_environment_fingerprint,
    normalize_acceptance_criteria,
)
from .local_job_artifacts import (
    ANALYSIS_ARTIFACT_TYPE,
    artifacts_are_complete,
    is_bookkeeping_artifact,
    is_read_only_job_role,
)
from .pilot_guards import normalize_objective_key

REUSE_STATUSES = frozenset({"fresh", "reused", "partial", "invalidated"})
GATE_OUTCOMES = frozenset({"reuse", "narrow_verify", "full_swarm"})

ANALYSIS_ROLE_CLASS = frozenset({
    "analysis", "review", "explore", "read_only", "readonly", "audit", "search",
    # Narrow-verify verifier role — still analysis-class for stamping/reuse.
    "conflict-auditor",
})

# Narrow re-verify must not re-open broad explore / pipeline-mapper coverage.
NARROW_VERIFY_ROLES = ("conflict-auditor",)

_DIGEST_ARTIFACT_LIMIT = 8
_DIGEST_HEADLINE_CHARS = 160
_DIGEST_MAX_CHARS = 2400
_PATH_TOKEN_RE = re.compile(
    r"(?P<path>[\w./\\-]+\.(?:py|ts|tsx|js|jsx|cjs|mjs|json|md|toml|yml|yaml|"
    r"css|html|rs|go|java|c|h|cpp|sh|ps1|bat))\b",
    re.IGNORECASE,
)

_PM_VALIDATION: Any = None
_PM_VALIDATION_PROBED = False


def _load_pm_validation() -> Any:
    """Soft-import ``puppetmaster.validation``; never raise ImportError to callers."""
    global _PM_VALIDATION, _PM_VALIDATION_PROBED
    if _PM_VALIDATION_PROBED:
        return _PM_VALIDATION
    _PM_VALIDATION_PROBED = True
    try:
        import puppetmaster.validation as mod  # type: ignore
    except Exception:
        _PM_VALIDATION = None
        return None
    required = (
        "compute_validation_fingerprint",
        "is_reusable_validation_artifact",
        "compact_artifact_ref",
        "VALIDATION_STATUSES",
    )
    if not all(hasattr(mod, name) for name in required):
        _PM_VALIDATION = None
        return None
    _PM_VALIDATION = mod
    return _PM_VALIDATION


def pm_validation_helpers_available() -> bool:
    """True when Puppetmaster 1.21.3+ validation helpers are importable."""
    return _load_pm_validation() is not None


def normalize_workspace_key(cwd: str) -> str:
    """Canonical workspace identity for candidate lookup.

    Uses ``paths._resolve`` (realpath on POSIX, Windows-safe resolve) so
    symlink aliases of the same tree collapse together, then
    ``os.path.normcase`` for platform-aware case folding. Never
    unconditionally lowercases — on case-sensitive filesystems distinct
    paths that differ only by case remain distinct.
    """
    text = (cwd or "").strip()
    if not text:
        return ""
    try:
        from .paths import _resolve
        text = _resolve(text)
    except Exception:
        try:
            text = os.path.abspath(text)
        except Exception:
            text = text.replace("\\", "/")
            return text.rstrip("/")
    try:
        text = os.path.normcase(text)
    except Exception:
        pass
    return text.replace("\\", "/").rstrip("/")


def analysis_role_class(role: Any) -> str:
    text = str(role or "").strip().lower()
    if text in ANALYSIS_ROLE_CLASS or is_read_only_job_role(text):
        return "analysis"
    return text or "implement"


def candidate_lookup_key(objective: str, role: Any, cwd: str) -> tuple[str, str, str]:
    return (
        normalize_objective_key(objective or ""),
        analysis_role_class(role),
        normalize_workspace_key(cwd),
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_rel_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    parts = [p for p in text.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return ""
    return "/".join(parts)


def _repo_relative_path(cwd: str, path: str) -> str:
    """Normalize ``path`` to a repo-relative form under ``cwd``.

    Accepts relative paths and absolute in-repo paths. Rejects paths outside
    the workspace or that cannot be resolved unambiguously (returns "").

    Both the repository root and the candidate are canonicalized via
    ``paths._resolve`` (realpath on POSIX, Windows-safe resolve) so macOS
    ``/var`` vs ``/private/var`` and symlinked workspace views compare equal.
    Outside paths fail closed to "".
    """
    text = str(path or "").strip()
    if not text:
        return ""
    root = (cwd or "").strip()
    if not root:
        return _normalize_rel_path(text)
    try:
        from .paths import _resolve, path_within
    except Exception:
        return ""
    try:
        root_real = _resolve(root)
    except Exception:
        return ""
    if not root_real:
        return ""
    try:
        if os.path.isabs(text):
            candidate = text
        else:
            rel_norm = _normalize_rel_path(text)
            if not rel_norm:
                return ""
            candidate = os.path.join(root_real, rel_norm.replace("/", os.sep))
        # Fail closed: unresolvable / outside / cross-volume → "".
        if not path_within(candidate, root_real, allow_equal=True):
            return ""
        cand_real = _resolve(candidate)
        rel = os.path.relpath(cand_real, root_real).replace("\\", "/")
        return _normalize_rel_path(rel)
    except Exception:
        return ""


def extract_evidence_paths(*texts: Any, files: Optional[Sequence[Any]] = None) -> list[str]:
    """Collect normalized relative evidence paths from prose + explicit file lists."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in files or ():
        rel = _normalize_rel_path(str(raw or ""))
        if rel and rel not in seen:
            seen.add(rel)
            found.append(rel)
    for blob in texts:
        if isinstance(blob, (list, tuple)):
            for item in blob:
                for match in _PATH_TOKEN_RE.finditer(str(item or "")):
                    rel = _normalize_rel_path(match.group("path"))
                    if rel and rel not in seen:
                        seen.add(rel)
                        found.append(rel)
            continue
        for match in _PATH_TOKEN_RE.finditer(str(blob or "")):
            rel = _normalize_rel_path(match.group("path"))
            if rel and rel not in seen:
                seen.add(rel)
                found.append(rel)
    return sorted(found)


def evidence_paths_from_job(job: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for art in job.get("artifacts") or []:
        if not isinstance(art, dict) or is_bookkeeping_artifact(art):
            continue
        paths.extend(
            extract_evidence_paths(
                art.get("headline"),
                art.get("body"),
                art.get("detail"),
                art.get("claim"),
                art.get("evidence"),
                files=art.get("files") if isinstance(art.get("files"), list) else None,
            )
        )
        validation = art.get("validation") if isinstance(art.get("validation"), dict) else None
        if validation is None:
            payload = art.get("payload") if isinstance(art.get("payload"), dict) else None
            if isinstance(payload, dict) and isinstance(payload.get("validation"), dict):
                validation = payload.get("validation")
        if isinstance(validation, dict):
            for item in validation.get("scope") or []:
                rel = _normalize_rel_path(str(item or ""))
                if rel:
                    paths.append(rel)
    # Deduplicate while preserving sort order via set round-trip.
    return sorted({p for p in paths if p})


def _git_head_sha(cwd: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if proc.returncode == 0:
            sha = (proc.stdout or "").strip()
            if sha:
                return sha
    except Exception:
        pass
    return "uncommitted"


def _git_probe_name_only(cmd: Sequence[str]) -> tuple[bool, list[str]]:
    """Run one git name-only probe. Returns ``(ok, normalized_paths)``."""
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return False, []
    if proc.returncode != 0:
        return False, []
    found: list[str] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        rel = _normalize_rel_path(line)
        if rel and rel not in seen:
            seen.add(rel)
            found.append(rel)
    return True, found


def _git_head_unborn(cwd: str) -> bool:
    """True when ``HEAD`` does not resolve (unborn branch / no commits yet)."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--verify", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return proc.returncode != 0
    except Exception:
        return True


def git_changed_paths(cwd: str, *, since_sha: str = "") -> tuple[list[str], str]:
    """Return (changed_paths, reason). Empty reason means success.

    Fail closed with independent dirt-category oracles:

    * **tracked/staged** — at least one successful ``git diff`` (or unborn-HEAD
      fallback) must succeed. An empty successful tracked diff does **not**
      compensate for a failed untracked probe.
    * **untracked** — ``git ls-files --others --exclude-standard`` must succeed
      on its own. A successful untracked probe does **not** compensate for
      failed tracked probes.

    If either category lacks a successful oracle, return
    ``git_diff_unavailable`` rather than an empty success that looks like a
    clean tree and would authorize fingerprint reuse while dirt is invisible.
    """
    root = (cwd or "").strip()
    if not root or not os.path.isdir(root):
        return [], "workspace_unavailable"
    paths: list[str] = []
    seen: set[str] = set()

    def _absorb(found: Sequence[str]) -> None:
        for rel in found:
            text = str(rel or "").strip()
            if text and text not in seen:
                seen.add(text)
                paths.append(text)

    try:
        # --- Tracked / staged category ---
        tracked_ok = False
        tracked_cmds: list[list[str]] = []
        since = (since_sha or "").strip()
        if since and since not in ("uncommitted", "unknown"):
            tracked_cmds.append(
                ["git", "-C", root, "diff", "--name-only", f"{since}...HEAD"]
            )
            tracked_cmds.append(["git", "-C", root, "diff", "--name-only", since])
        tracked_cmds.append(["git", "-C", root, "diff", "--name-only", "HEAD"])
        for cmd in tracked_cmds:
            ok, found = _git_probe_name_only(cmd)
            if ok:
                tracked_ok = True
                _absorb(found)

        if not tracked_ok and _git_head_unborn(root):
            # Unborn HEAD: no commit to diff against. Fall back to index oracles
            # (staged + unstaged-vs-index + modified tracked names).
            for cmd in (
                ["git", "-C", root, "diff", "--name-only", "--cached"],
                ["git", "-C", root, "diff", "--name-only"],
                ["git", "-C", root, "ls-files", "--modified"],
            ):
                ok, found = _git_probe_name_only(cmd)
                if ok:
                    tracked_ok = True
                    _absorb(found)

        # --- Untracked category (independent) ---
        untracked_ok, untracked_found = _git_probe_name_only(
            ["git", "-C", root, "ls-files", "--others", "--exclude-standard"]
        )
        if untracked_ok:
            _absorb(untracked_found)

        if not tracked_ok or not untracked_ok:
            return [], "git_diff_unavailable"
        return sorted(paths), ""
    except Exception as exc:
        return [], f"git_diff_unavailable:{exc.__class__.__name__}"


def _codegraph_freshness_confirmed(cwd: str) -> tuple[bool, str]:
    """True when a local ``.codegraph`` exists and is not stale vs the tree.

    Empty/stale CodeGraph ``affected`` results must not prove blast-radius
    non-intersection with prior evidence. Uncertainty fails closed.
    """
    root = (cwd or "").strip()
    if not root or not os.path.isdir(root):
        return False, "workspace_unavailable"
    cg_path = os.path.join(root, ".codegraph")
    if not os.path.exists(cg_path):
        return False, "codegraph_unavailable"
    try:
        from .api.codegraph_index import codegraph_is_stale
    except Exception:
        return False, "codegraph_freshness_unconfirmed"
    try:
        if codegraph_is_stale(root):
            return False, "codegraph_stale"
    except Exception:
        return False, "codegraph_freshness_unconfirmed"
    return True, ""


def codegraph_affected_paths(
    cwd: str,
    changed_paths: Sequence[str],
) -> tuple[list[str], str]:
    """Run ``python -m puppetmaster codegraph affected`` (cross-platform argv).

    Output paths are normalized to repo-relative form. Absolute in-repo paths
    are accepted; outside or ambiguous paths are dropped. An empty successful
    parse is reported as ``codegraph_affected_empty`` so callers never treat
    silence as proven blast-radius non-intersection.
    """
    root = (cwd or "").strip()
    files = [_repo_relative_path(root, p) for p in changed_paths]
    files = [p for p in files if p]
    if not root or not files:
        return [], "no_changed_paths" if root else "workspace_unavailable"
    try:
        from ._exec import _puppetmaster_cmd
        cmd = _puppetmaster_cmd("codegraph", "affected", "-q", *files)
    except Exception as exc:
        return [], f"puppetmaster_cmd_unavailable:{exc.__class__.__name__}"
    try:
        proc = subprocess.run(
            cmd,
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except Exception as exc:
        return [], f"codegraph_affected_failed:{exc.__class__.__name__}"
    if proc.returncode != 0:
        out = (proc.stdout or "").strip().lower()
        if "no module named" in out or proc.returncode == 127:
            return [], "codegraph_unavailable"
        return [], f"codegraph_affected_exit:{proc.returncode}"
    found: list[str] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        rel = _repo_relative_path(root, line.strip())
        if rel and rel not in seen:
            seen.add(rel)
            found.append(rel)
    if not found:
        return [], "codegraph_affected_empty"
    return sorted(found), ""


def compute_source_fingerprint(
    cwd: str,
    scope: Sequence[str],
    *,
    rules_version: Optional[str] = None,
    evaluator_version: Optional[str] = "marionette-validation-reuse",
    strict: bool = False,
) -> tuple[Optional[dict[str, Any]], str]:
    """Compute fingerprint metadata. Returns ``(payload_or_none, reason)``."""
    root = (cwd or "").strip()
    if not root or not os.path.isdir(root):
        return None, "workspace_unavailable"
    scope_paths = sorted({p for p in (_normalize_rel_path(x) for x in scope) if p})
    if not scope_paths:
        # Bind HEAD-only when the prior job cited no file evidence — still
        # fail closed for reuse unless a stamp exists and HEAD matches.
        scope_paths = []

    pm = _load_pm_validation()
    if pm is not None:
        try:
            result = pm.compute_validation_fingerprint(
                root,
                scope_paths,
                rules_version=rules_version,
                evaluator_version=evaluator_version,
                strict=strict,
            )
            payload = result.to_payload(status="fresh")
            # Drop absolute repo_root from persisted local rows (Windows/posix drift).
            payload.pop("repo_root", None)
            # PM to_payload omits completeness flags; preserve them so reuse
            # gates can fail closed on incomplete stamps.
            if "complete" not in payload:
                payload["complete"] = bool(getattr(result, "complete", True))
            if "missing_paths" not in payload:
                payload["missing_paths"] = list(getattr(result, "missing_paths", []) or [])
            if "unreadable_paths" not in payload:
                payload["unreadable_paths"] = list(
                    getattr(result, "unreadable_paths", []) or []
                )
            return payload, ""
        except Exception as exc:
            # Fall through to local hasher; callers still fail closed on reuse
            # when neither path can stamp a complete fingerprint.
            local_reason = f"pm_fingerprint_error:{exc.__class__.__name__}"
    else:
        local_reason = "pm_validation_helpers_absent"

    # Local fail-closed hasher compatible with the PM payload shape.
    # Containment matches ``_repo_relative_path`` / ``path_within`` realpath
    # policy: never follow an in-repo symlink whose target escapes the root
    # and hash outside bytes into the digest.
    head_sha = _git_head_sha(root)
    source_digests: dict[str, str] = {}
    missing: list[str] = []
    unreadable: list[str] = []
    try:
        from .paths import _resolve, path_within
        root_real = _resolve(root)
    except Exception:
        return None, f"workspace_unresolvable:{local_reason}"
    if not root_real:
        return None, f"workspace_unresolvable:{local_reason}"
    for rel in scope_paths:
        candidate = os.path.normpath(
            os.path.join(root_real, rel.replace("/", os.sep))
        )
        try:
            if not path_within(candidate, root_real, allow_equal=True):
                # Symlink escape / outside / unresolvable — fail closed.
                unreadable.append(rel)
                continue
            cand_real = _resolve(candidate)
            if not path_within(cand_real, root_real, allow_equal=True):
                unreadable.append(rel)
                continue
        except Exception:
            unreadable.append(rel)
            continue
        try:
            if not os.path.isfile(cand_real):
                missing.append(rel)
                continue
            with open(cand_real, "rb") as fh:
                source_digests[rel] = _sha256_bytes(fh.read())
        except OSError:
            unreadable.append(rel)
    complete = not missing and not unreadable
    if strict and not complete:
        return None, f"fingerprint_incomplete:{local_reason}"
    source_digest = _sha256_text(
        json.dumps(source_digests, sort_keys=True, separators=(",", ":"))
    )
    rules_digest = _sha256_text(str(rules_version or ""))
    evaluator_digest = _sha256_text(str(evaluator_version or ""))
    material = {
        "head_sha": head_sha,
        "source_digest": source_digest,
        "rules_digest": rules_digest,
        "evaluator_digest": evaluator_digest,
    }
    fingerprint = _sha256_text(
        json.dumps(material, sort_keys=True, separators=(",", ":"))
    )
    payload = {
        "fingerprint": fingerprint,
        "status": "fresh",
        "head_sha": head_sha,
        "scope": scope_paths,
        "source_digests": source_digests,
        "source_digest": source_digest,
        "rules_version": rules_version,
        "rules_digest": rules_digest,
        "evaluator_digest": evaluator_digest,
        "dirty_scoped": False,
        "complete": complete,
        "missing_paths": missing,
        "unreadable_paths": unreadable,
    }
    return payload, local_reason if pm is None else ""


def validation_block_of(artifact: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(artifact, Mapping):
        return None
    direct = artifact.get("validation")
    if isinstance(direct, dict):
        return dict(direct)
    payload = artifact.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("validation"), dict):
        return dict(payload["validation"])
    return None


def job_validation_fingerprint(job: Mapping[str, Any]) -> str:
    fp = str(job.get("validation_fingerprint") or "").strip()
    if fp:
        return fp
    for art in job.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        block = validation_block_of(art)
        if not block:
            continue
        value = str(block.get("fingerprint") or "").strip()
        if value:
            return value
    return ""


def job_validation_status(job: Mapping[str, Any]) -> str:
    status = str(job.get("reuse_status") or "").strip().lower()
    if status in REUSE_STATUSES:
        return status
    for art in job.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        block = validation_block_of(art)
        if not block:
            continue
        value = str(block.get("status") or "").strip().lower()
        if value:
            return value
    return ""


def _job_adapter_is_demo(job: Mapping[str, Any]) -> bool:
    adapter = str(job.get("adapter") or "").strip().lower()
    if adapter in ("demo", "refused-demo"):
        return True
    for art in job.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        if str(art.get("adapter") or "").strip().lower() == "demo":
            return True
        headline = str(art.get("headline") or "").lower()
        if "demo substrate" in headline:
            return True
    return False


def _job_has_auth_failure(job: Mapping[str, Any]) -> bool:
    for key in ("auth_failure", "reuse_reason", "summary"):
        text = str(job.get(key) or "").lower()
        if "auth" in text and ("fail" in text or "401" in text or "403" in text):
            return True
    for art in job.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        failure = str(art.get("failure") or "").lower()
        headline = str(art.get("headline") or "").lower()
        if "auth" in failure or "http_status:401" in failure or "http_status:403" in failure:
            return True
        if "auth failure" in headline or "unauthorized" in headline:
            return True
    return False


def _substantive_findings(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for art in job.get("artifacts") or []:
        if not isinstance(art, dict) or is_bookkeeping_artifact(art):
            continue
        kind = str(art.get("type") or "").strip().lower()
        if kind in ("finding", "risk", "decision", ANALYSIS_ARTIFACT_TYPE, "verification"):
            headline = str(art.get("headline") or art.get("claim") or "").strip()
            if headline:
                out.append(art)
    return out


def _job_validation_complete(job: Mapping[str, Any]) -> tuple[bool, str]:
    """True when every validation stamp is complete with no missing paths."""
    saw_block = False
    for art in job.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        block = validation_block_of(art)
        if not block:
            continue
        saw_block = True
        # complete must be explicitly true — missing/False never authorizes reuse.
        if block.get("complete") is not True:
            return False, "validation_incomplete"
        missing = block.get("missing_paths") or []
        if isinstance(missing, (list, tuple)) and len(missing) > 0:
            return False, "validation_missing_paths"
    if not saw_block:
        # Fingerprint-only rows without a validation block cannot prove completeness.
        return False, "validation_block_missing"
    return True, "ok"


def is_reusable_local_job(job: Mapping[str, Any], *, cwd: str, objective: str, role: Any) -> tuple[bool, str]:
    """Fail-closed reusable-candidate check for a local job row."""
    if not isinstance(job, Mapping):
        return False, "not_a_job"
    status = str(job.get("status") or "").strip().lower()
    if status in ("pending", "running", "queued", "starting", "in_progress"):
        return False, "pending_or_current_job"
    if status not in ("completed", "done", "complete"):
        return False, "not_terminal_green"
    key = candidate_lookup_key(objective, role, cwd)
    # Workspace key is mandatory and exact — never fall back to caller cwd
    # when the candidate omitted its own workspace.
    candidate_cwd = str(job.get("cwd") or "").strip()
    if not key[2]:
        return False, "workspace_key_missing"
    if not candidate_cwd:
        return False, "candidate_cwd_missing"
    job_key = candidate_lookup_key(
        str(job.get("goal") or ""),
        job.get("role") or "explore",
        candidate_cwd,
    )
    if not key[0] or key[0] != job_key[0]:
        return False, "objective_mismatch"
    if key[1] != "analysis" or job_key[1] != "analysis":
        return False, "role_not_analysis"
    if key[2] != job_key[2]:
        return False, "workspace_mismatch"
    if _job_adapter_is_demo(job):
        return False, "demo_adapter"
    if _job_has_auth_failure(job):
        return False, "auth_failure"
    if not artifacts_are_complete(job.get("artifacts")):
        return False, "artifacts_incomplete"
    findings = _substantive_findings(job)
    if not findings:
        return False, "thin_or_bookkeeping_only"
    # Thin one-liners and long prose without file evidence are never reusable.
    # Empty-scope HEAD-only fingerprints enable false-green reuse on dirty trees.
    has_file_evidence = False
    for art in findings:
        text = str(art.get("body") or art.get("headline") or "")
        if art.get("files") or _PATH_TOKEN_RE.search(text):
            has_file_evidence = True
            break
    if not has_file_evidence:
        return False, "thin_findings"
    scope = evidence_paths_from_job(job)
    if not scope:
        return False, "empty_evidence_scope"
    fp = job_validation_fingerprint(job)
    if not fp:
        return False, "missing_validation_fingerprint"
    complete_ok, complete_reason = _job_validation_complete(job)
    if not complete_ok:
        return False, complete_reason
    reuse_status = job_validation_status(job)
    # Only original complete full-validation evidence may be a reusable source.
    # Reused / partial / narrow_verify rows must never shadow the original green
    # explore job when they share a fingerprint and win by updated_at.
    if reuse_status in ("reused", "partial", "stale", "superseded", "invalidated"):
        return False, f"status_{reuse_status or 'invalid'}"
    if reuse_status and reuse_status != "fresh":
        return False, f"status_{reuse_status}"
    return True, "ok"


@dataclass
class ReuseGateDecision:
    outcome: str
    reason: str
    source_job_id: str = ""
    validation_fingerprint: str = ""
    environment_fingerprint: str = ""
    invalidated_paths: list[str] = field(default_factory=list)
    reuse_status: str = ""
    candidate: Optional[dict[str, Any]] = None
    digest_text: str = ""
    compact_artifacts: list[dict[str, Any]] = field(default_factory=list)
    narrow_roles: tuple[str, ...] = ()
    narrow_goal_suffix: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)

    def as_provenance(self) -> dict[str, Any]:
        # Full-swarm rejection reasons (environment_changed, etc.) must remain
        # visible on the fresh terminal job — never drop to a silent empty
        # provenance that looks like fingerprint_match.
        out: dict[str, Any] = {
            "reuse_status": self.reuse_status or (
                "reused" if self.outcome == "reuse"
                else "partial" if self.outcome == "narrow_verify"
                else "fresh" if self.outcome == "full_swarm"
                else ""
            ),
            "source_job_id": self.source_job_id,
            "validation_fingerprint": self.validation_fingerprint,
            "environment_fingerprint": self.environment_fingerprint,
            "invalidated_paths": list(self.invalidated_paths),
            "reuse_reason": self.reason,
            "acceptance_criteria": list(self.acceptance_criteria),
        }
        return {k: v for k, v in out.items() if v not in ("", None, [])}


def iter_local_job_candidates(session: Any) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    try:
        live = session.live_local_jobs() if hasattr(session, "live_local_jobs") else []
        for row in live or []:
            if isinstance(row, dict):
                jobs.append(row)
    except Exception:
        pass
    try:
        with session._local_jobs_lock:  # type: ignore[attr-defined]
            for row in (getattr(session, "_local_jobs", {}) or {}).values():
                if isinstance(row, dict):
                    jobs.append(row)
    except Exception:
        pass
    # Deduplicate by id, prefer later (more recently updated) rows.
    by_id: dict[str, dict[str, Any]] = {}
    for row in jobs:
        jid = str(row.get("id") or "").strip()
        if not jid:
            continue
        prev = by_id.get(jid)
        if prev is None or float(row.get("updated_at") or 0) >= float(prev.get("updated_at") or 0):
            by_id[jid] = row
    return list(by_id.values())


def lookup_reusable_candidates(
    session: Any,
    *,
    objective: str,
    role: Any,
    cwd: str,
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for job in iter_local_job_candidates(session):
        ok, _reason = is_reusable_local_job(job, cwd=cwd, objective=objective, role=role)
        if ok:
            matched.append(job)
    matched.sort(key=lambda j: float(j.get("updated_at") or j.get("created_at") or 0), reverse=True)
    return matched


def compact_delta_digest(
    *,
    source_job_id: str,
    artifacts: Sequence[Mapping[str, Any]],
    reuse_status: str,
    invalidated_paths: Optional[Sequence[str]] = None,
    change_summary: str = "",
    reason: str = "",
    acceptance_criteria: Optional[Sequence[str]] = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Build a bounded pilot digest citing ``artifact://`` ids (no large dumps)."""
    refs: list[dict[str, Any]] = []
    lines = [
        f"REUSED validation from source_job_id={source_job_id} "
        f"(reuse_status={reuse_status}).",
    ]
    if reason:
        lines.append(f"reason: {reason}")
    criteria_block = format_acceptance_criteria_block(
        normalize_acceptance_criteria(acceptance_criteria)
    )
    if criteria_block:
        lines.append(criteria_block)
    if invalidated_paths:
        shown = ", ".join(list(invalidated_paths)[:12])
        lines.append(f"invalidated_paths: {shown}")
    if change_summary:
        lines.append(f"change_summary: {change_summary[:400]}")
    lines.append("artifact:// citations:")
    for art in artifacts:
        if not isinstance(art, Mapping) or is_bookkeeping_artifact(art):
            continue
        art_id = str(art.get("id") or "").strip()
        if not art_id:
            continue
        kind = str(art.get("type") or "finding").strip().lower()
        headline = str(art.get("headline") or art.get("claim") or "").strip()
        headline = headline[:_DIGEST_HEADLINE_CHARS]
        uri = f"artifact://{source_job_id}/{art_id}"
        refs.append({
            "id": art_id,
            "type": kind,
            "headline": headline,
            "uri": uri,
            "reuse_status": reuse_status,
            "source_job_id": source_job_id,
        })
        lines.append(f"  - [{kind}] {uri} — {headline}")
        if len(refs) >= _DIGEST_ARTIFACT_LIMIT:
            break
    if not refs:
        lines.append("  (no substantive artifact:// rows)")
    text = "\n".join(lines)
    if len(text) > _DIGEST_MAX_CHARS:
        text = text[: _DIGEST_MAX_CHARS - 3] + "..."
    return text, refs


def _invalidate_paths_for_candidate(
    cwd: str,
    candidate: Mapping[str, Any],
) -> tuple[list[str], str, dict[str, Any]]:
    """Return (invalidated_paths, reason, current_fingerprint_payload).

    Never authorizes reuse from fingerprint equality alone — always consult
    git dirty paths and CodeGraph affected before returning an empty
    invalidation set. Paths outside prior evidence fail closed to the caller
    (``outside_evidence_unproven``) unless CodeGraph blast-radius evidence
    proves non-intersection with the prior scope.
    """
    scope = evidence_paths_from_job(candidate)
    prior_fp_block: dict[str, Any] = {}
    for art in candidate.get("artifacts") or []:
        if not isinstance(art, dict):
            continue
        block = validation_block_of(art)
        if block and block.get("fingerprint"):
            prior_fp_block = block
            if not scope and isinstance(block.get("scope"), list):
                scope = [
                    _normalize_rel_path(str(p))
                    for p in block["scope"]
                    if _normalize_rel_path(str(p))
                ]
            break
    if not scope:
        return [], "empty_evidence_scope", {}
    if prior_fp_block.get("complete") is not True:
        return [], "validation_incomplete", {}
    missing_prior = prior_fp_block.get("missing_paths") or []
    if isinstance(missing_prior, (list, tuple)) and len(missing_prior) > 0:
        return [], "validation_missing_paths", {}

    current, fp_reason = compute_source_fingerprint(cwd, scope, strict=False)
    if current is None:
        return [], fp_reason or "fingerprint_unavailable", {}
    if current.get("complete") is not True:
        return [], "incomplete_fingerprint", current
    missing_cur = current.get("missing_paths") or []
    if isinstance(missing_cur, (list, tuple)) and len(missing_cur) > 0:
        return [], "incomplete_fingerprint", current

    prior_fp = str(
        prior_fp_block.get("fingerprint")
        or candidate.get("validation_fingerprint")
        or ""
    )
    current_fp = str(current.get("fingerprint") or "")
    since_sha = str(prior_fp_block.get("head_sha") or "")

    # Always evaluate dirty tree + CodeGraph before any fingerprint_match.
    changed, git_reason = git_changed_paths(cwd, since_sha=since_sha)
    if git_reason:
        return [], git_reason, current

    affected: list[str] = []
    cg_reason = ""
    cg_ok = False
    if changed:
        affected, cg_reason = codegraph_affected_paths(cwd, changed)
        cg_ok = not cg_reason
        if not cg_ok:
            affected = []
        else:
            # Re-normalize (accept absolute in-repo; drop outside/ambiguous).
            affected = [
                p for p in (_repo_relative_path(cwd, x) for x in affected) if p
            ]
            if not affected:
                cg_ok = False

    # Keep git dirty paths repo-relative for exact scope comparison.
    # Any nonempty git changed path that cannot canonicalize inside the
    # repo (including an in-repo symlink whose target escapes the root)
    # must fail closed — never filter it away as if the tree were clean.
    normalized_changed: list[str] = []
    for raw in changed:
        text = str(raw or "").strip()
        if not text:
            continue
        rel = _repo_relative_path(cwd, text)
        if not rel:
            return [], "changed_path_unresolvable", current
        normalized_changed.append(rel)
    changed = normalized_changed
    scope_set = set(scope)
    dirty_or_affected = sorted(set(list(changed) + list(affected)))
    invalid = sorted(p for p in dirty_or_affected if p in scope_set)
    outside = sorted(p for p in dirty_or_affected if p not in scope_set)

    if outside:
        # Outside-scope dirt fails closed unless a fresh CodeGraph blast-radius
        # oracle proves the dirty paths do not affect prior evidence.
        # Empty/stale/unavailable affected results are never proof of
        # non-intersection on their own — mtime freshness alone is insufficient.
        if not cg_ok or not affected:
            return [], "outside_evidence_unproven", current
        fresh_ok, fresh_reason = _codegraph_freshness_confirmed(cwd)
        if not fresh_ok:
            return [], fresh_reason or "outside_evidence_unproven", current
        if invalid:
            # Mixed: some evidence paths hit, some outside dirt — narrow or full
            # decided by caller from the invalidated subset.
            return invalid, "", current
        # Non-empty affected, none in prior scope, fresh graph — proven
        # non-intersection with prior evidence.
        if prior_fp and current_fp and prior_fp == current_fp:
            return [], "", current
        prior_source = str(prior_fp_block.get("source_digest") or "")
        current_source = str(current.get("source_digest") or "")
        if prior_source and current_source and prior_source == current_source:
            return [], "", current
        # Fingerprint moved without scoped path intersection — fail closed.
        return [], "fingerprint_drift_unscoped", current

    if not invalid:
        if prior_fp and current_fp and prior_fp == current_fp:
            return [], "", current
        prior_source = str(prior_fp_block.get("source_digest") or "")
        current_source = str(current.get("source_digest") or "")
        if prior_source and current_source and prior_source == current_source:
            return [], "", current
        if not changed and prior_fp != current_fp:
            # No dirt but fingerprint moved (rules/evaluator) — full swarm.
            return [], "fingerprint_drift", current
        return [], "", current

    return invalid, "", current


def _acceptance_criteria_from_job(job: Mapping[str, Any]) -> list[str]:
    """Bounded explicit criteria from a prior job (never inferred from goal)."""
    if not isinstance(job, Mapping):
        return []
    direct = normalize_acceptance_criteria(job.get("acceptance_criteria"))
    if direct:
        return direct
    for art in job.get("artifacts") or []:
        if not isinstance(art, Mapping):
            continue
        block = art.get("validation")
        if isinstance(block, Mapping):
            found = normalize_acceptance_criteria(block.get("acceptance_criteria"))
            if found:
                return found
    return []


def evaluate_reuse_gate(
    session: Any,
    *,
    objective: str,
    role: Any = "explore",
    cwd: str,
    force_fresh: bool = False,
    acceptance_criteria: Optional[Sequence[str]] = None,
) -> ReuseGateDecision:
    """Deterministic read-before-dispatch gate for analysis swarms / parallel."""
    criteria = normalize_acceptance_criteria(acceptance_criteria)
    if force_fresh:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="force_fresh",
            acceptance_criteria=criteria,
        )
    obj_key = normalize_objective_key(objective or "")
    if not obj_key:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="empty_objective",
            acceptance_criteria=criteria,
        )
    if analysis_role_class(role) != "analysis":
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="non_analysis_role",
            acceptance_criteria=criteria,
        )

    # Missing/old PM validation helpers must never authorize reuse. Local
    # fingerprint stamping may still run for observability elsewhere.
    if not pm_validation_helpers_available():
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="pm_validation_helpers_absent",
            acceptance_criteria=criteria,
        )

    workspace_key = normalize_workspace_key(cwd)
    if not workspace_key:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="workspace_key_missing",
            acceptance_criteria=criteria,
        )

    candidates = lookup_reusable_candidates(
        session, objective=objective, role=role, cwd=cwd,
    )
    if not candidates:
        # Distinguish first-pass (no prior at all) from rejected priors.
        any_prior = False
        for job in iter_local_job_candidates(session):
            job_cwd = str(job.get("cwd") or "").strip()
            jk = candidate_lookup_key(
                str(job.get("goal") or ""),
                job.get("role") or "explore",
                job_cwd or cwd,
            )
            if jk[0] == obj_key and jk[1] == "analysis":
                any_prior = True
                break
        if not any_prior:
            return ReuseGateDecision(
                outcome="full_swarm",
                reason="first_pass",
                acceptance_criteria=criteria,
            )
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="no_reusable_candidate",
            acceptance_criteria=criteria,
        )

    # Ambiguous: multiple distinct fingerprints for the same key.
    fingerprints = {
        job_validation_fingerprint(c) for c in candidates if job_validation_fingerprint(c)
    }
    if len(fingerprints) > 1:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="ambiguous_candidates",
            source_job_id=str(candidates[0].get("id") or ""),
            acceptance_criteria=criteria,
        )

    env_fps = {job_environment_fingerprint(c) for c in candidates}
    if len(env_fps) > 1:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="ambiguous_environment_fingerprints",
            source_job_id=str(candidates[0].get("id") or ""),
            acceptance_criteria=criteria,
        )

    candidate = candidates[0]
    source_job_id = str(candidate.get("id") or "")
    prior_fp = job_validation_fingerprint(candidate)
    # Explicit-only: never inherit prior candidate criteria when dispatch
    # omitted them. Empty+empty matches; any other mismatch fails closed.
    prior_criteria = _acceptance_criteria_from_job(candidate)
    if criteria != prior_criteria:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason="acceptance_criteria_changed",
            source_job_id=source_job_id,
            validation_fingerprint=prior_fp,
            environment_fingerprint=job_environment_fingerprint(candidate),
            reuse_status="fresh",
            candidate=dict(candidate),
            acceptance_criteria=list(criteria),
        )

    # Volatile environment must match exactly before any source reuse/narrow.
    # Missing/old stamps and probe failures fail closed — never silent
    # fingerprint_match after PATH/browser/MCP/PM drift.
    env_ok, env_reason, env_payload = match_environment_fingerprint(
        candidate, cwd,
    )
    env_fp = ""
    if isinstance(env_payload, dict):
        env_fp = str(env_payload.get("fingerprint") or "")
    if not env_fp:
        env_fp = job_environment_fingerprint(candidate)
    if not env_ok:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason=env_reason or "environment_changed",
            source_job_id=source_job_id,
            validation_fingerprint=prior_fp,
            environment_fingerprint=env_fp,
            reuse_status="fresh",
            candidate=dict(candidate),
            acceptance_criteria=criteria,
        )

    invalidated, inv_reason, current = _invalidate_paths_for_candidate(cwd, candidate)
    if inv_reason:
        return ReuseGateDecision(
            outcome="full_swarm",
            reason=inv_reason,
            source_job_id=source_job_id,
            validation_fingerprint=prior_fp,
            environment_fingerprint=env_fp,
            reuse_status="fresh",
            candidate=dict(candidate),
            acceptance_criteria=criteria,
        )
    current_fp = str((current or {}).get("fingerprint") or prior_fp)
    if not invalidated:
        digest, refs = compact_delta_digest(
            source_job_id=source_job_id,
            artifacts=list(candidate.get("artifacts") or []),
            reuse_status="reused",
            reason="fingerprint_match",
            acceptance_criteria=criteria,
        )
        return ReuseGateDecision(
            outcome="reuse",
            reason="fingerprint_match",
            source_job_id=source_job_id,
            validation_fingerprint=current_fp or prior_fp,
            environment_fingerprint=env_fp,
            reuse_status="reused",
            candidate=dict(candidate),
            digest_text=digest,
            compact_artifacts=refs,
            acceptance_criteria=criteria,
        )

    # Partial invalidation — verifier-only narrow path.
    evidence_n = len(evidence_paths_from_job(candidate))
    if evidence_n > 0 and len(invalidated) < evidence_n:
        suffix = (
            "Re-verify only these invalidated paths / previously failed checks; "
            "do not re-run explore or pipeline-mapper roles. Paths: "
            + ", ".join(invalidated[:20])
        )
        criteria_block = format_acceptance_criteria_block(criteria)
        if criteria_block:
            suffix = f"{suffix}\n\n{criteria_block}"
        digest, refs = compact_delta_digest(
            source_job_id=source_job_id,
            artifacts=list(candidate.get("artifacts") or []),
            reuse_status="partial",
            invalidated_paths=invalidated,
            change_summary=suffix,
            reason="subset_invalidated",
            acceptance_criteria=criteria,
        )
        return ReuseGateDecision(
            outcome="narrow_verify",
            reason="subset_invalidated",
            source_job_id=source_job_id,
            validation_fingerprint=current_fp or prior_fp,
            environment_fingerprint=env_fp,
            invalidated_paths=list(invalidated),
            reuse_status="partial",
            candidate=dict(candidate),
            digest_text=digest,
            compact_artifacts=refs,
            narrow_roles=NARROW_VERIFY_ROLES,
            narrow_goal_suffix=suffix,
            acceptance_criteria=criteria,
        )

    return ReuseGateDecision(
        outcome="full_swarm",
        reason="broad_invalidation",
        source_job_id=source_job_id,
        validation_fingerprint=current_fp or prior_fp,
        environment_fingerprint=env_fp,
        invalidated_paths=list(invalidated),
        reuse_status="invalidated",
        candidate=dict(candidate),
        acceptance_criteria=criteria,
    )


def mark_validation_stamp_failed(job: dict[str, Any], error: Any) -> dict[str, Any]:
    """Persist ``complete=false``/error so a failed stamp cannot look reusable.

    Best-effort seam for ``_finish_local_job``: keep the terminal job green for
    the UI, but attach an incomplete validation block so future reuse gates
    fail closed via ``_job_validation_complete``.
    """
    if not isinstance(job, dict):
        return {}
    if isinstance(error, BaseException):
        err_text = f"{error.__class__.__name__}:{error}"
    else:
        err_text = str(error or "stamp_validation_failed")
    err_text = err_text.strip()[:500] or "stamp_validation_failed"
    scope = evidence_paths_from_job(job)
    incomplete = {
        "fingerprint": str(job.get("validation_fingerprint") or ""),
        "status": "fresh",
        "scope": scope,
        "source_digest": "",
        "head_sha": "",
        "complete": False,
        "error": err_text,
        "missing_paths": [],
        "unreadable_paths": [],
    }
    # Do not invent a reusable fingerprint when stamping failed.
    if not incomplete["fingerprint"]:
        job.pop("validation_fingerprint", None)
    arts = list(job.get("artifacts") or [])
    stamped: list[Any] = []
    wrote = False
    for art in arts:
        if not isinstance(art, dict):
            stamped.append(art)
            continue
        row = dict(art)
        kind = str(row.get("type") or "").strip().lower()
        if kind in (
            ANALYSIS_ARTIFACT_TYPE, "finding", "risk", "decision",
            "verification", "patch", "error",
        ) and not is_bookkeeping_artifact(row):
            row["validation"] = dict(incomplete)
            wrote = True
        stamped.append(row)
    if not wrote and stamped:
        # Ensure at least one incomplete block exists for completeness checks.
        for idx, row in enumerate(stamped):
            if isinstance(row, dict) and not is_bookkeeping_artifact(row):
                stamped[idx] = {**row, "validation": dict(incomplete)}
                wrote = True
                break
    job["artifacts"] = stamped
    return job


def stamp_validation_on_job(
    job: dict[str, Any],
    *,
    cwd: str,
    reuse_status: str = "fresh",
    source_job_id: str = "",
    invalidated_paths: Optional[Sequence[str]] = None,
    reuse_reason: str = "",
    fingerprint_payload: Optional[Mapping[str, Any]] = None,
    validation_fingerprint: str = "",
    environment_fingerprint: str = "",
    acceptance_criteria: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Stamp validation / reuse provenance onto a local job dict (in place)."""
    if not isinstance(job, dict):
        return {}
    scope = evidence_paths_from_job(job)
    payload = dict(fingerprint_payload) if isinstance(fingerprint_payload, Mapping) else None
    explicit_criteria = acceptance_criteria is not None
    criteria = normalize_acceptance_criteria(
        acceptance_criteria
        if explicit_criteria
        else job.get("acceptance_criteria")
    )
    # Reused digests are compact citations — preserve the source fingerprint
    # rather than recomputing from thin headline-only rows.
    override_fp = str(validation_fingerprint or "").strip()
    if payload is None and reuse_status in ("reused", "partial") and override_fp:
        payload = {
            "fingerprint": override_fp,
            "status": "reused",
            "scope": scope,
            "source_digest": "",
            "head_sha": "",
            "complete": bool(scope),
            "missing_paths": [],
            "unreadable_paths": [],
        }
    if payload is None:
        payload, _reason = compute_source_fingerprint(cwd, scope, strict=False)
    if not isinstance(payload, dict):
        # Still record reuse fields even when fingerprinting failed.
        if reuse_status:
            job["reuse_status"] = reuse_status
        if source_job_id:
            job["source_job_id"] = source_job_id
        if override_fp:
            job["validation_fingerprint"] = override_fp
        if invalidated_paths:
            job["invalidated_paths"] = list(invalidated_paths)
        if reuse_reason:
            job["reuse_reason"] = reuse_reason
        if criteria:
            job["acceptance_criteria"] = list(criteria)
        return job

    status = reuse_status if reuse_status in (
        "fresh", "reused", "stale", "superseded", "partial", "invalidated",
    ) else "fresh"
    # PM validation statuses are fresh|reused|stale|superseded; map partial/invalidated.
    pm_status = status
    if status == "partial":
        pm_status = "reused"
    elif status == "invalidated":
        pm_status = "stale"
    validation = dict(payload)
    if override_fp:
        validation["fingerprint"] = override_fp
    validation["status"] = pm_status if pm_status in ("fresh", "reused", "stale", "superseded") else "fresh"
    if source_job_id:
        validation["source_artifact_ids"] = [
            f"{source_job_id}/{str(a.get('id') or '')}"
            for a in (job.get("artifacts") or [])
            if isinstance(a, dict) and a.get("id")
        ][:12]

    # Stamp volatile environment fingerprint on complete validations. Preserve
    # an explicit override (reuse/narrow lineage); otherwise probe current env.
    # Probe failure fail-closes completeness so the job cannot authorize reuse.
    override_env = str(
        environment_fingerprint
        or job.get("environment_fingerprint")
        or ""
    ).strip()
    env_schema = ENVIRONMENT_FINGERPRINT_SCHEMA
    env_version = ENVIRONMENT_FINGERPRINT_VERSION
    if override_env and reuse_status in ("reused", "partial"):
        env_fp = override_env
        prior_schema = job_environment_fingerprint_schema(job)
        if prior_schema is not None:
            env_schema = int(prior_schema)
    else:
        env_payload, env_reason = compute_environment_fingerprint(cwd, strict=True)
        if isinstance(env_payload, dict) and env_payload.get("fingerprint"):
            env_fp = str(env_payload.get("fingerprint") or "")
            try:
                env_schema = int(env_payload.get("schema") or ENVIRONMENT_FINGERPRINT_SCHEMA)
            except (TypeError, ValueError):
                env_schema = ENVIRONMENT_FINGERPRINT_SCHEMA
            env_version = str(
                env_payload.get("version") or ENVIRONMENT_FINGERPRINT_VERSION
            )
        else:
            env_fp = ""
            # Complete source stamp without a usable env fingerprint must not
            # look reusable — fail closed.
            if validation.get("complete") is True and status == "fresh":
                validation["complete"] = False
                validation["error"] = env_reason or "environment_probe_failed"
    if env_fp:
        validation["environment_fingerprint"] = env_fp
        validation["environment_fingerprint_schema"] = env_schema
        validation["environment_fingerprint_version"] = env_version
        job["environment_fingerprint"] = env_fp
        job["environment_fingerprint_schema"] = env_schema
        job["environment_fingerprint_version"] = env_version
    # Persist explicit criteria (including empty) so omitted-vs-prior
    # matching stays honest on durable reused jobs.
    if explicit_criteria or criteria:
        validation["acceptance_criteria"] = list(criteria)
        job["acceptance_criteria"] = list(criteria)

    job["validation_fingerprint"] = str(validation.get("fingerprint") or "")
    job["reuse_status"] = status if status in REUSE_STATUSES else "fresh"
    if source_job_id:
        job["source_job_id"] = source_job_id
    if invalidated_paths:
        job["invalidated_paths"] = list(invalidated_paths)
    if reuse_reason:
        job["reuse_reason"] = reuse_reason

    arts = list(job.get("artifacts") or [])
    stamped: list[Any] = []
    for art in arts:
        if not isinstance(art, dict):
            stamped.append(art)
            continue
        row = dict(art)
        kind = str(row.get("type") or "").strip().lower()
        if kind in (
            ANALYSIS_ARTIFACT_TYPE, "finding", "risk", "decision", "verification", "patch", "error",
        ) and not is_bookkeeping_artifact(row):
            row["validation"] = dict(validation)
            if status in REUSE_STATUSES:
                row["reuse_status"] = status
            if source_job_id:
                row["source_job_id"] = source_job_id
        stamped.append(row)
    job["artifacts"] = stamped
    return job


def provenance_fields_from_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """Optional back-compat fields for API / UI merge."""
    out: dict[str, Any] = {}
    for key in (
        "reuse_status",
        "source_job_id",
        "validation_fingerprint",
        "environment_fingerprint",
        "invalidated_paths",
        "reuse_reason",
        "acceptance_criteria",
    ):
        value = job.get(key)
        if value in (None, "", [], {}):
            continue
        out[key] = value
    if "validation_fingerprint" not in out:
        fp = job_validation_fingerprint(job)
        if fp:
            # Compact / truncated for live rows.
            out["validation_fingerprint"] = fp[:16] + "…" if len(fp) > 16 else fp
    elif isinstance(out.get("validation_fingerprint"), str):
        fp = out["validation_fingerprint"]
        if len(fp) > 16:
            out["validation_fingerprint"] = fp[:16] + "…"
    if "environment_fingerprint" not in out:
        env_fp = job_environment_fingerprint(job)
        if env_fp:
            out["environment_fingerprint"] = (
                env_fp[:16] + "…" if len(env_fp) > 16 else env_fp
            )
    elif isinstance(out.get("environment_fingerprint"), str):
        env_fp = out["environment_fingerprint"]
        if len(env_fp) > 16:
            out["environment_fingerprint"] = env_fp[:16] + "…"
    return out


def is_durable_recall_uri(path: str) -> bool:
    """True for artifact://, job://, spill:// (and agent://) durable recall targets."""
    text = str(path or "").strip().lower()
    return text.startswith((
        "artifact://",
        "job://",
        "spill://",
        "agent://",
        "conflict://",
    ))


__all__ = [
    "ANALYSIS_ROLE_CLASS",
    "ENVIRONMENT_FINGERPRINT_SCHEMA",
    "ENVIRONMENT_FINGERPRINT_VERSION",
    "GATE_OUTCOMES",
    "NARROW_VERIFY_ROLES",
    "REUSE_STATUSES",
    "ReuseGateDecision",
    "analysis_role_class",
    "candidate_lookup_key",
    "codegraph_affected_paths",
    "compact_delta_digest",
    "compute_environment_fingerprint",
    "compute_source_fingerprint",
    "evaluate_reuse_gate",
    "evidence_paths_from_job",
    "extract_evidence_paths",
    "format_acceptance_criteria_block",
    "git_changed_paths",
    "is_durable_recall_uri",
    "is_reusable_local_job",
    "iter_local_job_candidates",
    "job_environment_fingerprint",
    "job_validation_fingerprint",
    "lookup_reusable_candidates",
    "mark_validation_stamp_failed",
    "match_environment_fingerprint",
    "normalize_acceptance_criteria",
    "normalize_workspace_key",
    "pm_validation_helpers_available",
    "provenance_fields_from_job",
    "stamp_validation_on_job",
    "validation_block_of",
]
