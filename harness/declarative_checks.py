from __future__ import annotations

"""Declarative pre/post checks for harness worker tasks (Shepherd-style v1).

Specs live as JSON files under ``{repo}/.marionette/checks/`` and optionally
``{state_dir}/checks/``. v1 supports ``shell``, ``file``, and post-only
``artifact`` kinds.
"""
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any, List, Optional, Union

MAX_OUTPUT = 4000

_VALID_KINDS = frozenset({"shell", "file", "artifact"})
_VALID_PHASES = frozenset({"pre", "post"})
_VALID_ON_FAIL = frozenset({"blocked", "failed", "warn"})


@dataclass(frozen=True)
class CheckSpec:
    id: str
    kind: str
    phase: str
    on_fail: str
    cmd: str = ""
    timeout_s: Optional[int] = None
    path: str = ""
    exists: Optional[bool] = None
    contains: str = ""
    not_contains: str = ""
    artifact_type: str = ""
    min_count: int = 1


@dataclass
class CheckResult:
    id: str
    phase: str
    passed: bool
    output: str
    duration_ms: int
    on_fail: str = "warn"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _validate_repo_relative_path(path: str) -> None:
    if not path or path.startswith("/") or path.startswith("\\"):
        raise ValueError(f"invalid repository-relative path: {path!r}")
    if ".." in path.replace("\\", "/").split("/"):
        raise ValueError(f"path traversal rejected: {path!r}")


def _parse_check_item(raw: dict, phase: str) -> CheckSpec:
    if not isinstance(raw, dict):
        raise ValueError("check entry must be an object")
    check_id = str(raw.get("id") or "").strip()
    if not check_id:
        raise ValueError("check missing id")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in _VALID_KINDS:
        raise ValueError(f"check {check_id!r}: unknown kind {kind!r}")
    on_fail = str(raw.get("on_fail") or "warn").strip().lower()
    if on_fail not in _VALID_ON_FAIL:
        raise ValueError(f"check {check_id!r}: unknown on_fail {on_fail!r}")
    if phase not in _VALID_PHASES:
        raise ValueError(f"invalid phase {phase!r}")

    if kind == "shell":
        cmd = str(raw.get("cmd") or "").strip()
        if not cmd:
            raise ValueError(f"check {check_id!r}: shell check requires cmd")
        timeout_raw = raw.get("timeout_s")
        timeout_s: Optional[int] = None
        if timeout_raw is not None:
            timeout_s = int(timeout_raw)
        return CheckSpec(
            id=check_id,
            kind=kind,
            phase=phase,
            on_fail=on_fail,
            cmd=cmd,
            timeout_s=timeout_s,
        )

    if kind == "artifact":
        if phase == "pre":
            raise ValueError(
                f"check {check_id!r}: artifact checks are post-only"
            )
        expect = raw.get("expect")
        if not isinstance(expect, dict):
            raise ValueError(f"check {check_id!r}: artifact check requires expect")
        artifact_type = str(expect.get("type") or "").strip()
        if not artifact_type:
            raise ValueError(f"check {check_id!r}: artifact check requires expect.type")
        min_count_raw = expect.get("min_count", 1)
        min_count = int(min_count_raw) if min_count_raw is not None else 1
        return CheckSpec(
            id=check_id,
            kind=kind,
            phase=phase,
            on_fail=on_fail,
            artifact_type=artifact_type,
            min_count=max(1, min_count),
        )

    rel_path = str(raw.get("path") or "").strip()
    if not rel_path:
        raise ValueError(f"check {check_id!r}: file check requires path")
    _validate_repo_relative_path(rel_path)
    exists_raw = raw.get("exists")
    exists_val: Optional[bool] = None
    if exists_raw is not None:
        exists_val = bool(exists_raw)
    contains = str(raw.get("contains") or "")
    not_contains = str(raw.get("not_contains") or "")
    if exists_val is None and not contains and not not_contains:
        raise ValueError(
            f"check {check_id!r}: file check requires exists, contains, or not_contains"
        )
    return CheckSpec(
        id=check_id,
        kind=kind,
        phase=phase,
        on_fail=on_fail,
        path=rel_path,
        exists=exists_val,
        contains=contains,
        not_contains=not_contains,
    )


def load_checks(source: Union[str, dict]) -> List[CheckSpec]:
    """Load checks from a parsed dict or a JSON file path."""
    if isinstance(source, str):
        with open(source, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = source
    if not isinstance(data, dict):
        raise ValueError("check spec root must be an object")

    specs: List[CheckSpec] = []
    for phase in ("pre", "post"):
        entries = data.get(phase) or []
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"{phase} must be a list")
        for item in entries:
            specs.append(_parse_check_item(item, phase))
    return specs


def find_check_specs(repo: str) -> List[CheckSpec]:
    """Read every ``*.json`` in ``{repo}/.marionette/checks/`` (sorted)."""
    checks_dir = os.path.join(repo, ".marionette", "checks")
    if not os.path.isdir(checks_dir):
        return []
    specs: List[CheckSpec] = []
    for name in sorted(os.listdir(checks_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(checks_dir, name)
        try:
            specs.extend(load_checks(path))
        except Exception:
            continue
    return specs


def discover_check_parse_warnings(repo: str) -> List[CheckResult]:
    """Malformed spec files become warn results instead of crashing callers."""
    checks_dir = os.path.join(repo, ".marionette", "checks")
    if not os.path.isdir(checks_dir):
        return []
    warnings: List[CheckResult] = []
    for name in sorted(os.listdir(checks_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(checks_dir, name)
        try:
            load_checks(path)
        except Exception as exc:
            warnings.append(
                CheckResult(
                    id=f"parse:{name}",
                    phase="pre",
                    passed=False,
                    output=_truncate(f"failed to load {name}: {exc}"),
                    duration_ms=0,
                    on_fail="warn",
                )
            )
    return warnings


def declarative_checks_enabled(repo: str) -> bool:
    if os.environ.get("HARNESS_DECLARATIVE_CHECKS", "").strip().lower() in (
        "0",
        "false",
        "off",
        "no",
    ):
        return False
    checks_dir = os.path.join(repo, ".marionette", "checks")
    return os.path.isdir(checks_dir)


def run_checks(
    checks: List[CheckSpec],
    *,
    repo: str,
    phase: str,
    timeout_default: int = 30,
    cancel_event=None,
    state_dir: str = "",
    job_id: str = "",
) -> List[CheckResult]:
    """Run checks for one phase. Never raises."""
    results: List[CheckResult] = []
    for spec in checks:
        if spec.phase != phase:
            continue
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            results.append(
                CheckResult(
                    id=spec.id,
                    phase=phase,
                    passed=False,
                    output="check cancelled",
                    duration_ms=0,
                    on_fail=spec.on_fail,
                )
            )
            continue
        if spec.kind == "shell":
            results.append(_run_shell_check(spec, repo, timeout_default))
        elif spec.kind == "artifact":
            results.append(_run_artifact_check(spec, state_dir, job_id))
        else:
            results.append(_run_file_check(spec, repo))
    return results


def _run_shell_check(spec: CheckSpec, repo: str, timeout_default: int) -> CheckResult:
    timeout = spec.timeout_s if spec.timeout_s is not None else timeout_default
    t0 = time.time()
    if not spec.cmd:
        return CheckResult(
            id=spec.id,
            phase=spec.phase,
            passed=True,
            output="",
            duration_ms=0,
            on_fail=spec.on_fail,
        )
    cwd = repo or None
    try:
        res = subprocess.run(
            spec.cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(1, int(timeout)),
        )
        output = res.stdout or ""
        passed = res.returncode == 0
    except subprocess.TimeoutExpired as te:
        out = te.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", errors="replace")
        passed = False
        output = f"{out}\n[check timed out after {timeout} seconds]"
    except Exception as exc:
        passed = False
        output = str(exc)
    duration_ms = int((time.time() - t0) * 1000)
    return CheckResult(
        id=spec.id,
        phase=spec.phase,
        passed=passed,
        output=_truncate(output),
        duration_ms=duration_ms,
        on_fail=spec.on_fail,
    )


def _run_artifact_check(spec: CheckSpec, state_dir: str, job_id: str) -> CheckResult:
    t0 = time.time()
    if not state_dir or not job_id:
        duration_ms = int((time.time() - t0) * 1000)
        return CheckResult(
            id=spec.id,
            phase=spec.phase,
            passed=False,
            output="artifact check requires a job context",
            duration_ms=duration_ms,
            on_fail=spec.on_fail,
        )
    try:
        from puppetmaster.store_factory import create_store

        store = create_store("sqlite", state_dir)
        store.init()
        artifacts = store.list_artifacts(job_id)
        expected = spec.artifact_type.casefold()
        count = 0
        for art in artifacts:
            type_name = getattr(art.type, "name", str(art.type))
            if str(type_name).casefold() == expected:
                count += 1
        passed = count >= spec.min_count
        if passed:
            output = f"found {count} artifact(s) of type {spec.artifact_type!r}"
        else:
            output = (
                f"expected at least {spec.min_count} artifact(s) of type "
                f"{spec.artifact_type!r}, found {count}"
            )
    except Exception as exc:
        passed = False
        output = str(exc)
    duration_ms = int((time.time() - t0) * 1000)
    return CheckResult(
        id=spec.id,
        phase=spec.phase,
        passed=passed,
        output=_truncate(output),
        duration_ms=duration_ms,
        on_fail=spec.on_fail,
    )


def _run_file_check(spec: CheckSpec, repo: str) -> CheckResult:
    t0 = time.time()
    abs_path = os.path.join(repo, spec.path.replace("/", os.sep))
    passed = True
    output_parts: list[str] = []
    try:
        present = os.path.isfile(abs_path)
        if spec.exists is not None:
            if spec.exists and not present:
                passed = False
                output_parts.append(f"expected file to exist: {spec.path}")
            elif not spec.exists and present:
                passed = False
                output_parts.append(f"expected file to be absent: {spec.path}")
        if present and (spec.contains or spec.not_contains):
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if spec.contains and spec.contains not in text:
                passed = False
                output_parts.append(f"missing substring in {spec.path!r}")
            if spec.not_contains and spec.not_contains in text:
                passed = False
                output_parts.append(f"forbidden substring present in {spec.path!r}")
        if passed and not output_parts:
            output_parts.append("ok")
    except Exception as exc:
        passed = False
        output_parts.append(str(exc))
    duration_ms = int((time.time() - t0) * 1000)
    return CheckResult(
        id=spec.id,
        phase=spec.phase,
        passed=passed,
        output=_truncate("\n".join(output_parts)),
        duration_ms=duration_ms,
        on_fail=spec.on_fail,
    )


def failed_checks_summary_line_from_dicts(checks: list) -> str:
    """Compact one-liner for failed checks (session/UI surfacing)."""
    failed_ids = [
        str(c.get("id", ""))
        for c in (checks or [])
        if c.get("id") and not c.get("passed", True)
    ]
    if not failed_ids:
        return ""
    return f"Declarative checks: {len(failed_ids)} failed ({', '.join(failed_ids)})"


def format_check_failure(results: List[CheckResult]) -> str:
    lines = []
    for r in results:
        if r.passed:
            continue
        lines.append(f"[{r.id}] {r.output}")
    return _truncate("\n".join(lines))


def results_to_dicts(results: List[CheckResult]) -> list[dict[str, Any]]:
    return [r.to_dict() for r in results]
