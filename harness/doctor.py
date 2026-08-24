from __future__ import annotations

"""`harness doctor`: one-shot health check. Verifies the harness can actually
run before you waste a task on a broken setup. Each check is independent and
reports ok / warn / fail with an actionable hint. Exit 0 if no hard failures.
"""

import argparse
import os
import sys


def _line(status, name, detail=""):
    color = {"ok": "32", "warn": "33", "fail": "31"}.get(status, "0")
    tag = status.upper().ljust(4)
    s = f"[{tag}] {name}"
    if detail:
        s += f"  --  {detail}"
    if sys.stdout.isatty():
        s = f"\033[{color}m{s}\033[0m"
    print(s)


def run_doctor(argv) -> int:
    ap = argparse.ArgumentParser(prog="harness doctor")
    ap.add_argument("--ping", action="store_true",
                    help="also make a live 1-token call to the driver (costs a fraction of a cent)")
    ap.add_argument(
        "--bundle",
        nargs="?",
        const="",
        default=None,
        metavar="OUTDIR",
        help="write a redacted diagnostics zip + manifest (default outdir: state/diag-bundles)",
    )
    ap.add_argument(
        "--sessions",
        type=int,
        default=20,
        metavar="N",
        help="with --bundle, include last N session ids (default 20)",
    )
    args = ap.parse_args(argv)

    from .config import HarnessConfig
    cfg = HarnessConfig.from_env()
    hard_fail = False

    print(f"harness doctor  (driver={cfg.driver} reach={cfg.reach})\n")

    # 1. Puppetmaster seam importable
    try:
        from puppetmaster.store_factory import create_store
        from puppetmaster.orchestrator import Orchestrator  # noqa
        _line("ok", "puppetmaster seam", "Orchestrator + store_factory importable")
    except Exception as e:
        _line("fail", "puppetmaster seam", f"cannot import: {e}")
        hard_fail = True

    # 2. Store works (create a temp store, round-trip a job)
    try:
        import tempfile
        from puppetmaster.store_factory import create_store
        store = create_store("sqlite", tempfile.mkdtemp(prefix="doctor-"))
        store.list_jobs()
        _line("ok", "durable state", "SQLite store read/write OK")
    except Exception as e:
        _line("fail", "durable state", f"store error: {e}")
        hard_fail = True

    # 3. Driver build + key presence
    try:
        from .providers import ProviderError, build_doctor_driver

        driver = build_doctor_driver(cfg.driver, reach=cfg.reach)
        env = getattr(driver, "api_key_env", None)
        if env is None:
            _line("ok", f"driver {cfg.driver}", "no key required (stub/offline)")
        elif os.environ.get(env, "").strip():
            _line("ok", f"driver {cfg.driver}", f"{env} present")
        else:
            _line("warn", f"driver {cfg.driver}", f"{env} not set -- set it or use a stub driver")
    except ProviderError as e:
        _line("warn", f"driver {cfg.driver}", str(e))
    except Exception as e:
        _line("fail", f"driver {cfg.driver}", f"build failed: {e}")
        hard_fail = True

    # 3a-2. Bedrock BYOK (optional -- agentic/PM workers read AWS_* from env)
    try:
        from .keys import get_bedrock_status
        bst = get_bedrock_status()
        if bst.get("configured"):
            mode = bst.get("auth_mode") or "credentials"
            region = bst.get("region") or "us-east-1"
            detail = f"{mode} auth, region={region}"
            if bst.get("model_id"):
                detail += f", model={bst['model_id']}"
            _line("ok", "bedrock", detail)
        else:
            _line("ok", "bedrock",
                  "not configured (optional -- set AWS_BEARER_TOKEN_BEDROCK or "
                  "access keys in Settings -> Providers)")
    except Exception as e:
        _line("warn", "bedrock", f"could not resolve: {e}")

    # 3b. Swarm adapter -- agentic (default with repo), openai, or demo (no-repo /
    # explicit ALLOW_DEMO only). A live repo must never silently sit on demo.
    repo = os.environ.get("HARNESS_REPO", "").strip()
    try:
        from types import SimpleNamespace
        from .swarm_adapter import ensure_repo_swarm_adapter, normalize_swarm_adapter
        _sa_cfg = SimpleNamespace(
            repo=repo,
            swarm_adapter=os.environ.get("HARNESS_SWARM_ADAPTER", "")
            or ("agentic" if repo else "demo"),
        )
        ensure_repo_swarm_adapter(_sa_cfg)
        sa = normalize_swarm_adapter(
            os.environ.get("HARNESS_SWARM_ADAPTER", _sa_cfg.swarm_adapter) or "demo"
        )
    except Exception:
        sa = os.environ.get("HARNESS_SWARM_ADAPTER", "demo").lower()
    import os.path as _op
    _indexed = bool(repo) and _op.isdir(_op.join(repo, ".codegraph"))
    if sa in ("agentic", "openai") and repo:
        label = "agentic (standalone, your provider keys)" if sa == "agentic" else "openai (OpenRouter-routed)"
        if _indexed:
            _line("ok", "swarm adapter", f"{label} -- REAL analysis of {repo}, CodeGraph indexed")
        else:
            _line("warn", "swarm adapter",
                  f"{label} analysis of {repo} but NO .codegraph index -- analysis runs "
                  f"BLIND (~30% vs ~81% accuracy). Run: python -m puppetmaster codegraph init --index")
        # ChatGPT Codex / Cursor OAuth can pilot the UI while agentic workers
        # still have zero HTTP keys — surface that before the first red swarm.
        if sa == "agentic":
            try:
                from .registry_wizard import get_provider_key
                from .providers import PROVIDERS
                _agentic_http = {
                    "openrouter", "openai", "anthropic", "gemini",
                    "deepseek", "zai", "xai", "bedrock",
                }
                ready = [
                    p.name for p in PROVIDERS
                    if p.name in _agentic_http and get_provider_key(p)
                ]
                if ready:
                    _line("ok", "agentic credentials",
                          f"swarm labor can bill via {', '.join(ready)}")
                else:
                    _line("warn", "agentic credentials",
                          "no OpenRouter/OpenAI/Anthropic/Gemini/… API key visible — "
                          "ChatGPT Codex OAuth alone cannot fund agentic swarms. "
                          "Add a provider key in Settings (or export OPENROUTER_API_KEY) "
                          "so SESSION COST routing/cache savings can accumulate.")
            except Exception as e:
                _line("warn", "agentic credentials", f"could not resolve: {e}")
    elif sa in ("agentic", "openai") and not repo:
        _line("warn", "swarm adapter", f"{sa} set but HARNESS_REPO empty -- open a project before running swarms")
    else:
        _line("warn", "swarm adapter", "demo is eval-only; product swarms use agentic (set HARNESS_ALLOW_DEMO_SWARM=1 only for benches)")

    # 3b-2. Edit engine -- which in-process worker run_implement will use, and
    # whether the standalone (keys-only) path is actually available.
    try:
        from .edit_engines import agentic_available, select_edit_engine
        engine = select_edit_engine(cfg)
        if engine == "agentic":
            _line("ok", "edit engine", "agentic (standalone, keys-only -- no external CLI needed)")
        elif agentic_available():
            _line("ok", "edit engine", "native pilot (agentic available; HARNESS_EDIT_ENGINE=agentic to prefer keys-only)")
        else:
            _line("warn", "edit engine",
                  "native pilot (no provider key visible -- set a provider key to unlock the standalone agentic engine)")
    except Exception as e:
        _line("warn", "edit engine", f"could not resolve: {e}")

    # 3c. Wiki integration (optional durable-knowledge capture)
    from .wiki import WikiClient
    wc = WikiClient()
    if wc.configured:
        if wc.health():
            auto = os.environ.get("HARNESS_WIKI_AUTO", "").strip() in ("1","true","yes")
            _line("ok", "wiki", f"connected ({wc.base_url}), auto-ingest={'on' if auto else 'off'}")
        else:
            _line("warn", "wiki", f"configured ({wc.base_url}) but unreachable")
    else:
        _line("ok", "wiki", "not configured (optional -- set HARNESS_WIKI_URL + HARNESS_WIKI_TOKEN to auto-capture findings)")

    # 4. Vision sidecar (warn-only; vision is optional). Resolved dynamically from
    # whatever provider key is configured, so any of Anthropic/OpenAI/Gemini/xAI/
    # OpenRouter enables image input -- no dedicated VLM key required.
    try:
        from .vision import default_sidecar, NullVisionSidecar
        _sc = default_sidecar()
        if isinstance(_sc, NullVisionSidecar):
            _line("warn", "vision sidecar",
                  "no vision-capable provider key -- image input disabled "
                  "(add Anthropic/OpenAI/Gemini/xAI/OpenRouter key)")
        else:
            _line("ok", "vision sidecar", f"{_sc.model} via {_sc.api_key_env}")
    except Exception as e:
        _line("warn", "vision sidecar", f"could not resolve sidecar: {e!r}")

    # 5. Optional live ping
    if args.ping and not hard_fail:
        try:
            from .providers import build_doctor_driver

            driver = build_doctor_driver(cfg.driver, reach=cfg.reach)
            resp = driver.complete('Reply with exactly: {"action":"stop","rationale":"ok"}')
            if resp.error:
                _line("fail", "driver ping", resp.error[:120])
                hard_fail = True
            else:
                _line("ok", "driver ping", f"{resp.tokens_out} tok, {resp.latency_ms:.0f}ms")
        except Exception as e:
            _line("fail", "driver ping", str(e)[:120])
            hard_fail = True

    print()
    if hard_fail:
        _line("fail", "result", "one or more hard failures -- fix before running tasks")
    else:
        _line("ok", "result", "harness ready")

    if args.bundle is not None:
        try:
            from .diag_bundle import write_diag_bundle

            outdir = (args.bundle or "").strip() or None
            zip_path, _manifest = write_diag_bundle(
                outdir,
                session_limit=max(0, int(args.sessions)),
                state_dir=cfg.state_dir or None,
                get_driver=lambda: cfg.driver,
                get_reach=lambda: cfg.reach,
                get_repo=lambda: cfg.repo,
            )
            print(f"diagnostics bundle: {zip_path}")
            return 0
        except Exception as e:
            _line("fail", "bundle", f"could not write diagnostics archive: {e}")
            return 1

    return 1 if hard_fail else 0
