"""Authenticated GET /api/diagnostics — quotable OperationalDiagnostic for the UI."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

from ..correlation import get_correlation_id

JsonPayload = Union[dict, list]


@dataclass
class DoctorServices:
    """Explicit deps for diagnostics HTTP handlers."""

    get_driver: Callable[[], str]
    get_reach: Callable[[], str]
    get_repo: Callable[[], str]


def _check_row(status: str, name: str, detail: str = "") -> dict[str, str]:
    return {"status": status, "name": name, "detail": detail}


def _build_checks(svc: DoctorServices) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    hard_fail = False

    try:
        from puppetmaster.store_factory import create_store  # noqa: F401
        from puppetmaster.orchestrator import Orchestrator  # noqa: F401

        checks.append(_check_row("ok", "puppetmaster seam", "Orchestrator + store_factory importable"))
    except Exception as exc:
        hard_fail = True
        checks.append(_check_row("fail", "puppetmaster seam", f"cannot import: {exc}"))

    try:
        import tempfile

        from puppetmaster.store_factory import create_store

        store = create_store("sqlite", tempfile.mkdtemp(prefix="diag-"))
        store.list_jobs()
        checks.append(_check_row("ok", "durable state", "SQLite store read/write OK"))
    except Exception as exc:
        hard_fail = True
        checks.append(_check_row("fail", "durable state", f"store error: {exc}"))

    driver = (svc.get_driver() or "").strip() or "unknown"
    reach = (svc.get_reach() or "").strip()
    try:
        from ..providers import ProviderError, build_doctor_driver

        built = build_doctor_driver(driver, reach=reach)
        env = getattr(built, "api_key_env", None)
        if env is None:
            checks.append(_check_row("ok", f"driver {driver}", "no key required (stub/offline)"))
        elif os.environ.get(env, "").strip():
            checks.append(_check_row("ok", f"driver {driver}", f"{env} present"))
        else:
            checks.append(
                _check_row("warn", f"driver {driver}", f"{env} not set -- set it or use a stub driver"),
            )
    except ProviderError as exc:
        checks.append(_check_row("warn", f"driver {driver}", str(exc)))
    except Exception as exc:
        hard_fail = True
        checks.append(_check_row("fail", f"driver {driver}", f"build failed: {exc}"))

    repo = (svc.get_repo() or "").strip()
    if repo and not os.path.isdir(repo):
        checks.append(_check_row("warn", "workspace", f"repo path missing: {repo}"))

    checks.append(
        _check_row(
            "ok" if not hard_fail else "fail",
            "result",
            "harness ready" if not hard_fail else "one or more hard failures",
        ),
    )
    return checks


def _diagnostic_from_checks(checks: list[dict[str, str]]) -> Optional[dict[str, Any]]:
    failed = [c for c in checks if c.get("status") == "fail"]
    warned = [c for c in checks if c.get("status") == "warn"]
    if not failed and not warned:
        return None

    if failed:
        first = failed[0]
        summary = f"{first.get('name', 'backend')} failed"
        detail = str(first.get("detail") or "").strip()
        return {
            "scope": "backend",
            "operation": "doctor",
            "code": "backend_not_ready",
            "summary": summary,
            "detail": detail or None,
            "severity": "error",
            "retryable": True,
            "recovery": {"kind": "retry", "label": "Retry"},
            "createdAt": int(time.time() * 1000),
            "correlation_id": get_correlation_id(),
        }

    first = warned[0]
    summary = f"{first.get('name', 'backend')} needs attention"
    detail = str(first.get("detail") or "").strip()
    return {
        "scope": "backend",
        "operation": "doctor",
        "code": "backend_warning",
        "summary": summary,
        "detail": detail or None,
        "severity": "warning",
        "retryable": True,
        "recovery": {"kind": "retry", "label": "Retry"},
        "createdAt": int(time.time() * 1000),
        "correlation_id": get_correlation_id(),
    }


def get_diagnostics(svc: DoctorServices) -> tuple[int, JsonPayload]:
    """GET /api/diagnostics — structured checks plus a quotable diagnostic."""
    checks = _build_checks(svc)
    diagnostic = _diagnostic_from_checks(checks)
    return 200, {
        "ok": True,
        "correlation_id": get_correlation_id(),
        "checks": checks,
        "diagnostic": diagnostic,
    }


def get_diagnostics_bundle(
    sessions: Optional[str],
    svc: DoctorServices,
) -> tuple[int, JsonPayload]:
    """GET /api/diagnostics/bundle — write redacted zip; return path + manifest."""
    from ..diag_bundle import DEFAULT_SESSION_LIMIT, write_diag_bundle

    limit = DEFAULT_SESSION_LIMIT
    raw = (sessions or "").strip() if sessions is not None else ""
    if raw:
        try:
            limit = max(0, int(raw))
        except ValueError:
            return 400, {
                "ok": False,
                "error": "sessions must be an integer",
                "correlation_id": get_correlation_id(),
            }

    checks = _build_checks(svc)
    try:
        zip_path, manifest = write_diag_bundle(
            session_limit=limit,
            get_driver=svc.get_driver,
            get_reach=svc.get_reach,
            get_repo=svc.get_repo,
            checks=checks,
        )
    except Exception as exc:
        return 500, {
            "ok": False,
            "error": f"bundle write failed: {exc}",
            "correlation_id": get_correlation_id(),
        }
    return 200, {
        "ok": True,
        "path": zip_path,
        "manifest": manifest,
        "correlation_id": get_correlation_id(),
    }
