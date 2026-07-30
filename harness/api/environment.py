"""Environment readiness HTTP route bodies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


@dataclass
class EnvironmentServices:
    """Explicit deps for environment readiness handlers."""

    get_repo: Callable[[], Optional[str]]


JsonPayload = Union[dict, list]


def get_environment_readiness(
    refresh: str,
    svc: EnvironmentServices,
) -> tuple[int, JsonPayload]:
    """GET /api/environment/readiness — optional browser + analyzer probes.

    Optional prerequisites are reported with remedies; they are not framed as
    product failures. Default responses reuse the short workspace cache;
    ``refresh=1`` forces a Chrome + analyzer re-probe.
    """
    from ..environment_readiness import build_environment_readiness

    force = str(refresh or "").strip().lower() in ("1", "true", "yes")
    root = ""
    try:
        root = (svc.get_repo() or "") or ""
    except Exception:
        root = ""
    payload: dict[str, Any] = build_environment_readiness(
        root=root, refresh=force,
    )
    return 200, payload
