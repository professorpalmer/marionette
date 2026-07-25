"""Process-wide ``HARNESS_REPO`` publish helpers.

Multi-session deferred cold-attach builds freeze a ``HarnessConfig`` snapshot
at construction time. If that ``ConversationalSession.__init__`` always wrote
``os.environ["HARNESS_REPO"]``, a late background build for an old workspace
could clobber the env after the user had already switched projects — CI saw
``_cfg.repo == repo_a`` with ``HARNESS_REPO`` still on ``repo_b``.

Workspace/open and sessions/switch force-publish the live root. Runner
construction only fills an empty env (boot / first pilot).
"""

from __future__ import annotations

import os


def _norm_repo(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return (path or "").strip()


def publish_harness_repo(repo: str, *, force: bool = False) -> None:
    """Publish ``repo`` to ``HARNESS_REPO``.

    When ``force`` is False, refuse to overwrite a different already-published
    workspace (deferred / background runner builds).
    """
    target = (repo or "").strip()
    if not target:
        return
    if not force:
        current = (os.environ.get("HARNESS_REPO") or "").strip()
        if current and _norm_repo(current) != _norm_repo(target):
            return
    os.environ["HARNESS_REPO"] = target


def sync_harness_repo_from_cfg(cfg: object) -> None:
    """Force-publish the live workspace root from ``cfg.repo`` (view attach)."""
    repo = (getattr(cfg, "repo", None) or "").strip()
    if repo:
        publish_harness_repo(repo, force=True)
    else:
        os.environ.pop("HARNESS_REPO", None)
