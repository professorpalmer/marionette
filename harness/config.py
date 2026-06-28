from __future__ import annotations

"""Harness configuration. Driver is a swappable choice -- the research proved
the whole open-weights field drives at 100% under a good harness, so the default
is picked on efficiency (glm-5.2: 932 tok, clean MIT license)."""

from dataclasses import dataclass


@dataclass
class HarnessConfig:
    driver: str = "qwen3-coder-30b"   # default: wins both eval batteries (100%, lowest tokens, Apache-2.0)
    reach: str = "openrouter"        # one key, whole field
    budget: int = 3                  # orchestration steps per task
    state_dir: str = ""              # PM state dir; blank -> per-session temp
    worker_mode: str = "subprocess"
    repo: str = ""                   # target repo for REAL analysis (HARNESS_REPO)
    swarm_adapter: str = "demo"      # demo (free/safe) | openai (real read-only analysis)
    wiki_url: str = ""               # portable-llm-wiki base url (HARNESS_WIKI_URL)
    wiki_auto: bool = False          # auto-ingest findings to the wiki (HARNESS_WIKI_AUTO)
    max_context_tokens: int = 96000
    no_delegation: bool = False
    verify_cmd: str = ""

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
        has_explicit_adapter = ("HARNESS_SWARM_ADAPTER" in os.environ) or ("swarm_adapter" in file_cfg)
        default_adapter = "openai" if (repo_val and not has_explicit_adapter) else "demo"
        swarm_adapter_val = pick("HARNESS_SWARM_ADAPTER", "swarm_adapter", default_adapter)

        driver_val = pick("HARNESS_DRIVER", "driver", "qwen3-coder-30b")

        # Context budget resolution, in priority order:
        #   1. explicit HARNESS_MAX_CONTEXT_TOKENS env / config -> always wins
        #   2. the active model's REAL published window from the catalog
        #   3. a safe 96K default for unknown models
        # This stops every model from being throttled to a flat 96K when many
        # carry 200K-1M windows.
        if "HARNESS_MAX_CONTEXT_TOKENS" in os.environ:
            max_ctx = int(os.environ["HARNESS_MAX_CONTEXT_TOKENS"])
        elif "max_context_tokens" in file_cfg:
            max_ctx = int(file_cfg["max_context_tokens"])
        else:
            try:
                from pmharness.registry import context_window
                max_ctx = context_window(driver_val, default=200000)
            except Exception:
                max_ctx = 96000

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
        )
