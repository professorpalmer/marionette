from __future__ import annotations

"""Harness configuration. Driver is a swappable choice -- the research proved
the whole open-weights field drives at 100% under a good harness, so the default
is picked on efficiency + quality + license (qwen3-coder-30b: lowest tokens on
both eval batteries, Apache-2.0). Override via HARNESS_DRIVER or ~/.harness.json;
from_env layers defaults < file < environment."""

from dataclasses import dataclass


@dataclass
class HarnessConfig:
    driver: str = "qwen3-coder-30b"   # default: wins both eval batteries (100%, lowest tokens, Apache-2.0)
    reach: str = "openrouter"        # one key, whole field
    budget: int = 3                  # orchestration steps per task
    state_dir: str = ""              # PM state dir; blank -> per-session temp
    worker_mode: str = "subprocess"
    repo: str = ""                   # target repo for REAL analysis (HARNESS_REPO)
    swarm_adapter: str = "agentic"   # agentic (default) | openai | demo (ALLOW_DEMO only)
    wiki_url: str = ""               # portable-llm-wiki base url (HARNESS_WIKI_URL)
    wiki_auto: bool = False          # auto-ingest findings to the wiki (HARNESS_WIKI_AUTO)
    max_context_tokens: int = 96000
    no_delegation: bool = False
    verify_cmd: str = ""
    # AUTO-VERIFY LOOP (interactive pilot): after the agent edits files, run a
    # fast project check (typecheck/syntax of the CHANGED files) and let it
    # self-correct before handing back. verify_command overrides the detected
    # check when set.
    auto_verify: bool = True
    verify_command: str = ""
    # Native browser / computer-use tools (raw CDP over local Chrome). Enabled
    # by default; set HARNESS_BROWSER_ENABLED=0 to hide the browser_* tools.
    browser_enabled: bool = True
    # Swarm ThreadPoolExecutor worker count. Env HARNESS_MAX_WORKERS and
    # ~/.harness.json "max_workers" override; default 4 matches historical
    # getattr(config, "max_workers", 4) behavior in ConversationalSession.
    max_workers: int = 4
    # Optional resource-pressure admission (defaults off — see resource_pressure.py).
    resource_pressure_enabled: bool = False
    resource_pressure_rss_advisory_mb: int | None = None
    resource_pressure_rss_reject_mb: int | None = None
    resource_pressure_fd_advisory: int | None = None
    resource_pressure_fd_reject: int | None = None
    resource_pressure_load_advisory: float | None = None
    resource_pressure_load_reject: float | None = None
    resource_pressure_wait_timeout_sec: float = 5.0
    resource_pressure_poll_interval_sec: float = 0.25

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        """Layered config: defaults < ~/.harness.json < environment. Env wins so
        a one-off override never requires editing the file."""
        import os
        import json
        from pathlib import Path

        file_cfg = {}
        path = Path(os.environ.get("HARNESS_CONFIG", str(Path.home() / ".harness.json")))
        if path.exists():
            try:
                file_cfg = json.loads(path.read_text())
            except (ValueError, OSError):
                file_cfg = {}

        def pick(env_key, file_key, default):
            if env_key in os.environ:
                return os.environ[env_key]
            return file_cfg.get(file_key, default)

        repo_val = pick("HARNESS_REPO", "repo", "")
        # Standalone by default: a repo-scoped swarm routes through the built-in
        # 'agentic' adapter -- direct provider API on the user's own key, no external
        # CLI, model picked live by Puppetmaster's router. This is the shipped
        # identity: agentic out of the box, working the moment a key is plugged in.
        # We deliberately do NOT fall back to 'demo' when keyless -- a demo run
        # produces deterministic placeholder findings that read as "the product is
        # broken." Instead the UI nudges the keyless user to add a key (see
        # ProviderKeyBanner), and 'demo' stays only for the no-repo case or when
        # HARNESS_ALLOW_DEMO_SWARM=1. A stale HARNESS_SWARM_ADAPTER=demo left in
        # the process env from boot-before-workspace-restore is NOT an opt-in.
        from .swarm_adapter import (
            allow_demo_swarm,
            normalize_swarm_adapter,
            resolve_bridge_swarm_adapter,
        )

        if "HARNESS_SWARM_ADAPTER" in os.environ:
            raw_adapter = os.environ["HARNESS_SWARM_ADAPTER"]
        elif "swarm_adapter" in file_cfg:
            raw_adapter = file_cfg.get("swarm_adapter", "")
        else:
            raw_adapter = "agentic"
        swarm_adapter_val = resolve_bridge_swarm_adapter(
            normalize_swarm_adapter(str(raw_adapter or "")),
            repo_cwd=str(repo_val or ""),
        )
        if swarm_adapter_val == "demo" and not allow_demo_swarm():
            swarm_adapter_val = "agentic"

        driver_val = pick("HARNESS_DRIVER", "driver", "qwen3-coder-30b")

        # Context budget resolution, in priority order:
        #   1. explicit HARNESS_MAX_CONTEXT_TOKENS env / config -> always wins
        #   2. the active model's REAL published window from the catalog
        #   3. a safe 96K default for unknown models
        # This stops every model from being throttled to a flat 96K when many
        # carry 200K-1M windows.
        max_ctx_val = None
        if "HARNESS_MAX_CONTEXT_TOKENS" in os.environ:
            try:
                max_ctx_val = int(os.environ["HARNESS_MAX_CONTEXT_TOKENS"])
            except (ValueError, TypeError):
                pass
        elif "max_context_tokens" in file_cfg:
            try:
                max_ctx_val = int(file_cfg["max_context_tokens"])
            except (ValueError, TypeError):
                pass

        if max_ctx_val is not None:
            max_ctx = max_ctx_val
        else:
            try:
                from pmharness.registry import context_window
                max_ctx = context_window(driver_val, default=200000)
            except Exception:
                max_ctx = 96000

        def _opt_int(env_key: str, file_key: str):
            raw = pick(env_key, file_key, "")
            if raw in ("", None):
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        def _opt_float(env_key: str, file_key: str):
            raw = pick(env_key, file_key, "")
            if raw in ("", None):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        def _bool(env_key: str, file_key: str, default: str = "") -> bool:
            return str(pick(env_key, file_key, default)).strip().lower() in ("1", "true", "yes", "on")

        max_workers_raw = pick("HARNESS_MAX_WORKERS", "max_workers", 4)
        try:
            max_workers_val = max(1, int(max_workers_raw))
        except (TypeError, ValueError):
            max_workers_val = 4

        return cls(
            driver=driver_val,
            reach=pick("HARNESS_REACH", "reach", "openrouter"),
            budget=int(pick("HARNESS_BUDGET", "budget", 3)),
            state_dir=pick("HARNESS_STATE_DIR", "state_dir", ""),
            repo=repo_val,
            swarm_adapter=swarm_adapter_val,
            wiki_url=pick("HARNESS_WIKI_URL", "wiki_url", ""),
            wiki_auto=str(pick("HARNESS_WIKI_AUTO", "wiki_auto", "")).strip() in ("1","true","yes","True"),
            max_context_tokens=max_ctx,
            no_delegation=str(pick("HARNESS_NO_DELEGATION", "no_delegation", "")).strip() in ("1","true","yes","True"),
            verify_cmd=pick("HARNESS_VERIFY_CMD", "verify_cmd", ""),
            auto_verify=str(pick("HARNESS_AUTO_VERIFY", "auto_verify", "true")).strip() in ("1","true","yes","True"),
            verify_command=pick("HARNESS_VERIFY_COMMAND", "verify_command", ""),
            browser_enabled=str(pick("HARNESS_BROWSER_ENABLED", "browser_enabled", "true")).strip() in ("1","true","yes","True"),
            max_workers=max_workers_val,
            resource_pressure_enabled=_bool(
                "HARNESS_RESOURCE_PRESSURE_ENABLED", "resource_pressure_enabled",
            ),
            resource_pressure_rss_advisory_mb=_opt_int(
                "HARNESS_RESOURCE_PRESSURE_RSS_ADVISORY_MB", "resource_pressure_rss_advisory_mb",
            ),
            resource_pressure_rss_reject_mb=_opt_int(
                "HARNESS_RESOURCE_PRESSURE_RSS_REJECT_MB", "resource_pressure_rss_reject_mb",
            ),
            resource_pressure_fd_advisory=_opt_int(
                "HARNESS_RESOURCE_PRESSURE_FD_ADVISORY", "resource_pressure_fd_advisory",
            ),
            resource_pressure_fd_reject=_opt_int(
                "HARNESS_RESOURCE_PRESSURE_FD_REJECT", "resource_pressure_fd_reject",
            ),
            resource_pressure_load_advisory=_opt_float(
                "HARNESS_RESOURCE_PRESSURE_LOAD_ADVISORY", "resource_pressure_load_advisory",
            ),
            resource_pressure_load_reject=_opt_float(
                "HARNESS_RESOURCE_PRESSURE_LOAD_REJECT", "resource_pressure_load_reject",
            ),
            resource_pressure_wait_timeout_sec=float(
                _opt_float(
                    "HARNESS_RESOURCE_PRESSURE_WAIT_TIMEOUT_SEC",
                    "resource_pressure_wait_timeout_sec",
                ) or 5.0
            ),
            resource_pressure_poll_interval_sec=float(
                _opt_float(
                    "HARNESS_RESOURCE_PRESSURE_POLL_INTERVAL_SEC",
                    "resource_pressure_poll_interval_sec",
                ) or 0.25
            ),
        )
