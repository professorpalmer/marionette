from __future__ import annotations

"""Adapter-resolve mixin: nearly-pure helpers for implement-adapter selection.

Extracted mechanically from harness/conversation.py to continue decomposing the
ConversationalSession god-object, matching ToolDispatchMixin / PromptQueueMixin
contract: these methods operate through `self` only where needed (e.g.
``_external_adapter_available``) and define no state and no __init__.

Method Resolution Order keeps behavior identical: run_implement / system-note
paths still call ``self._resolve_requested_implement_adapter`` etc. via
inheritance. Busy/send/swarm drain stay on ConversationalSession.
"""

import os
import subprocess

from ._exec import _puppetmaster_available, _puppetmaster_cmd


class AdapterResolveMixin:
    """Mixin holding implement-adapter availability and target-repo validation.

    The concrete class (ConversationalSession) supplies any ambient config these
    methods may need via `self`. This mixin defines no __init__ and no instance
    state of its own.
    """

    def _external_adapter_available(self, adapter: str) -> bool:
        """True when the requested external CLI adapter can actually run.

        Honors the live platform lock first: a disabled adapter is never
        "available" even if its CLI is on PATH (fixes cursor stickiness when
        the operator disables cursor and enables agentic). The provider-native
        / agentic in-process path is always the fallback when this returns False.
        """
        import shutil
        a = (adapter or "").lower().strip()
        try:
            from puppetmaster.platform_lock import KNOWN_ADAPTERS, is_adapter_enabled
            if a in KNOWN_ADAPTERS and not is_adapter_enabled(a):
                return False
        except Exception:
            pass
        if a == "cursor":
            return shutil.which("cursor") is not None
        if a == "claude-code":
            return shutil.which("claude") is not None
        if a == "codex":
            return shutil.which("codex") is not None
        if a == "openai":
            return bool(os.environ.get("OPENAI_API_KEY"))
        if a == "hermes":
            return shutil.which("hermes") is not None
        # Unknown adapter name: let the external path try (it will report its own error).
        return True

    def _validate_target_repo(self, repo: str):
        """Validate an optional per-dispatch target repo for run_implement /
        run_parallel. Returns (abs_path, err) where err is a human string on
        failure. The path must be an existing directory that is a git repo
        (either a .git directory, a gitfile, or `git rev-parse` succeeds -- the
        last check accepts secondary worktrees). No fallback: an invalid path
        surfaces as an error so the caller never silently runs against the
        wrong repo.
        """
        raw = (repo or "").strip()
        if not raw:
            return "", ""
        try:
            abs_path = os.path.abspath(raw)
        except Exception as e:
            return "", f"could not resolve target repo path {raw!r}: {e}"
        if not os.path.isdir(abs_path):
            return "", f"target repo {abs_path} is not an existing directory"
        # Fast local check: a .git directory OR a .git file (worktree pointer).
        git_marker = os.path.join(abs_path, ".git")
        if os.path.isdir(git_marker) or os.path.isfile(git_marker):
            return abs_path, ""
        # Fall back to `git -C <repo> rev-parse` so we also accept unusual
        # layouts (e.g. GIT_DIR override). Bounded and quiet.
        try:
            r = subprocess.run(
                ["git", "-C", abs_path, "rev-parse", "--is-inside-work-tree"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if r.returncode == 0 and (r.stdout or "").strip() == "true":
                return abs_path, ""
        except Exception:
            pass
        return "", f"target repo {abs_path} is not a valid git repository"

    def _resolve_requested_implement_adapter(self, requested: str) -> tuple:
        """Map a pilot-requested adapter to what may actually run right now.

        Returns ``(effective, note)``. Empty ``effective`` means use the
        in-process agentic/native path. Disabled or missing external adapters
        are remapped rather than hard-failing.
        """
        requested = (requested or "").strip().lower()
        if not requested or requested in ("agentic", "native", "provider"):
            return requested, ""
        external = {"cursor", "claude-code", "codex", "openai", "hermes"}
        if requested not in external:
            return requested, ""
        if self._external_adapter_available(requested):
            return requested, ""
        note = (
            f"adapter '{requested}' is disabled by platform lock or its CLI is "
            "unavailable; using standalone agentic/native instead"
        )
        return "", note

    def _active_adapters_system_note(self) -> str:
        """Live platform-lock snapshot injected each turn so the pilot cannot
        keep requesting a previously-enabled adapter after the operator flips
        Settings > Platform."""
        try:
            from puppetmaster.platform_lock import enabled_adapters
            enabled = sorted(enabled_adapters())
        except Exception:
            return ""
        if not enabled:
            return (
                "ACTIVE IMPLEMENT PLATFORMS (live): none enabled. "
                "Omit adapter on run_implement (standalone agentic/native only)."
            )
        preferred = "agentic" if "agentic" in enabled else enabled[0]
        disabled_hint = ""
        try:
            from puppetmaster.platform_lock import KNOWN_ADAPTERS
            disabled = sorted(set(KNOWN_ADAPTERS) - set(enabled))
            if disabled:
                disabled_hint = f" Do NOT pass adapter={{{', '.join(disabled)}}} — those are disabled."
        except Exception:
            pass
        return (
            f"ACTIVE IMPLEMENT PLATFORMS (live, re-read every turn): {', '.join(enabled)}. "
            f"Default run_implement MUST omit adapter or use '{preferred}'.{disabled_hint}"
        )

    def _detect_default_implement_adapter(self) -> str:
        """Prefer agentic when enabled; never return a platform-locked adapter."""
        try:
            from puppetmaster.platform_lock import enabled_adapters, is_adapter_enabled
            enabled = enabled_adapters()
            if "agentic" in enabled:
                return "agentic"
        except Exception:
            enabled = None
            is_adapter_enabled = None  # type: ignore

        if not _puppetmaster_available():
            return "agentic"
        try:
            p = subprocess.run(
                _puppetmaster_cmd("platform", "status"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=10
            )
            output = p.stdout or ""
            import re
            matches = re.findall(r"\[on\s*\]\s*([a-zA-Z0-9_-]+)", output)
            on = {m.lower().strip() for m in matches}
            pref = ["agentic", "hermes", "codex", "cursor", "claude-code"]
            for adapter in pref:
                if adapter not in on:
                    continue
                if is_adapter_enabled is not None and not is_adapter_enabled(adapter):
                    continue
                if adapter == "agentic" or self._external_adapter_available(adapter):
                    return adapter
        except Exception:
            pass
        return "agentic"
