#!/usr/bin/env python3
"""Cache Wars–style cost benchmark for Marionette on Claude Sonnet via OpenRouter.

Steals AGNT's all-1h Anthropic prompt-cache policy (stable + history ttl=1h).
AGNT measured hybrid 1h-stable + 5m-history as a double-write on history;
all-1h was cheaper. This bench compares that default against the old 5m arm.

Compares caching strategies on the SAME model/conversation shape the AGNT
infographic uses (Claude Sonnet, multi-turn session), measuring:

  1. Harness overhead (system + tools tokens) vs AGNT's Claude Code +6,817 claim
  2. Live OpenRouter usage: prompt / cache_read / cache_write / completion
  3. Dollarized cost at Sonnet list rates (full input vs 0.1x cache read)

Arms (same messages, same tools, same model):
  - marionette_1h / marionette_all_1h : AGNT-style all-1h default (every Claude
    breakpoint gets ttl=1h; marionette_all_1h is an explicit alias)
  - marionette_5m   : HARNESS_ANTHROPIC_CACHE_TTL=5m (old hybrid/OMP-era comparison)
  - no_cache        : HARNESS_PROMPT_CACHE=0 (full price every turn)

Usage:
  # Live (loads ~/.pmharness/state/keys.json openrouter key):
  python -m bench.cache_wars --live --messages 12

  # Dry overhead + projected dollars only (no API):
  python -m bench.cache_wars --dry-run

  # Full AGNT-length session (real spend):
  python -m bench.cache_wars --live --messages 40

Receipts land in bench/results/cache_wars_<timestamp>.{json,md}.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Sonnet list rates matching the AGNT Cache Wars card (USD per 1M tokens).
PRICE_IN = 3.0
PRICE_OUT = 15.0
CACHE_READ_MULT = 0.1  # published Anthropic/OpenRouter cache-read ratio
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"
DEFAULT_MESSAGES = 12  # enough for cache hits; use 40 for AGNT parity


def _est_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)


def _load_openrouter_key() -> str:
    state = os.environ.get("HARNESS_STATE_DIR") or os.path.expanduser("~/.pmharness/state")
    os.environ["HARNESS_STATE_DIR"] = state
    from harness.keys import load_api_keys_on_startup

    load_api_keys_on_startup("openrouter")
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise SystemExit(
            "No OpenRouter key. Expected openrouter in ~/.pmharness/state/keys.json "
            "or OPENROUTER_API_KEY in the environment."
        )
    return key


def _marionette_overhead() -> Dict[str, Any]:
    """Measure stable prefix size: frozen-ish system + visible tools schema."""
    from harness.pilot import build_tools_schema
    from pmharness.drivers.base import SYSTEM_PROMPT

    # Lean pilot system (research-rig prompt). Real ConversationalSession adds
    # skills/rules/MCP — we also report a "fat" estimate with a synthetic trailer.
    tools = build_tools_schema(no_delegation=False, browser_enabled=True)
    tools_json = json.dumps(tools)
    system = SYSTEM_PROMPT
    fat_system = (
        system
        + "\n\n# Standing rules (ALWAYS honor)\n- Prefer CodeGraph over blind grep.\n"
        + "\n# Learned skills\n## Skill: demo\nUse tools carefully.\n"
    )
    return {
        "system_chars": len(system),
        "system_tokens_est": _est_tokens(system),
        "fat_system_tokens_est": _est_tokens(fat_system),
        "tools_count": len(tools),
        "tools_chars": len(tools_json),
        "tools_tokens_est": _est_tokens(tools_json),
        "stable_prefix_tokens_est": _est_tokens(system) + _est_tokens(tools_json),
        "fat_stable_prefix_tokens_est": _est_tokens(fat_system) + _est_tokens(tools_json),
        "claude_code_bloat_claim": 6817,
        "vs_claude_code_bloat": _est_tokens(system) + _est_tokens(tools_json) - 6817,
    }


def _price_turn(
    *,
    prompt_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
    completion_tokens: int,
    write_mult: float,
) -> Dict[str, float]:
    """Split prompt into uncached / cache-read / cache-write and price."""
    cached = max(0, int(cached_tokens))
    written = max(0, int(cache_write_tokens))
    prompt = max(0, int(prompt_tokens))
    # Uncached = prompt - cached - written (floor at 0). Providers differ on
    # whether writes are included in prompt_tokens; keep non-negative.
    uncached = max(0, prompt - cached - written)
    cost_uncached = (uncached / 1e6) * PRICE_IN
    cost_read = (cached / 1e6) * PRICE_IN * CACHE_READ_MULT
    cost_write = (written / 1e6) * PRICE_IN * write_mult
    cost_out = (max(0, int(completion_tokens)) / 1e6) * PRICE_OUT
    return {
        "uncached_tokens": float(uncached),
        "cached_tokens": float(cached),
        "cache_write_tokens": float(written),
        "completion_tokens": float(completion_tokens),
        "usd_uncached": cost_uncached,
        "usd_cache_read": cost_read,
        "usd_cache_write": cost_write,
        "usd_out": cost_out,
        "usd_total": cost_uncached + cost_read + cost_write + cost_out,
    }


def _project_agnt_scenario(overhead: Dict[str, Any]) -> Dict[str, Any]:
    """Project dollars for the AGNT card scenario using OUR measured overhead.

    AGNT card: 40 messages, ~5,186 tokens/message content, Sonnet rates.
    We substitute Marionette stable prefix for Claude Code's +6,817 bloat.
    """
    msgs = 40
    content_per_msg = 5186
    our_bloat = int(overhead["stable_prefix_tokens_est"])
    cc_bloat = 6817

    def session_cost(bloat: int, *, cache: str) -> float:
        # Simplified: first message pays full (content+bloat); later messages
        # cache-read the stable prefix when cache != none.
        first = content_per_msg + bloat
        rest_uncached = content_per_msg  # growing history ignored (AGNT card style)
        if cache == "none":
            inp = first + rest_uncached * (msgs - 1)
            return (inp / 1e6) * PRICE_IN
        # Assume stable prefix is cache-read on turns 2..N
        write_mult = CACHE_WRITE_1H_MULT if cache == "1h" else CACHE_WRITE_5M_MULT
        # Turn 1: write stable prefix + pay content
        t1 = (bloat / 1e6) * PRICE_IN * write_mult + (content_per_msg / 1e6) * PRICE_IN
        # Turns 2..N: read bloat + pay content
        tn = (msgs - 1) * (
            (bloat / 1e6) * PRICE_IN * CACHE_READ_MULT
            + (content_per_msg / 1e6) * PRICE_IN
        )
        return t1 + tn

    return {
        "scenario": "AGNT-card projection (40 msgs x ~5186 content tokens)",
        "marionette_1h_usd": round(session_cost(our_bloat, cache="1h"), 4),
        "marionette_5m_usd": round(session_cost(our_bloat, cache="5m"), 4),
        "marionette_no_cache_usd": round(session_cost(our_bloat, cache="none"), 4),
        "claude_code_1h_usd": round(session_cost(cc_bloat, cache="1h"), 4),
        "agnt_published_usd": 2.68,
        "claude_code_published_usd": 6.89,
        "no_cache_published_usd": 12.76,
        "our_bloat_tokens": our_bloat,
        "claude_code_bloat_tokens": cc_bloat,
        "note": (
            "Projection uses AGNT's content-per-msg figure and Sonnet list rates; "
            "it is NOT a live receipt. Live arm below is billing-grade."
        ),
    }


def _make_driver(model: str, max_tokens: int = 64):
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    return OpenAICompatDriver(
        name="cache-wars",
        model=model,
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        temperature=0.0,
        max_tokens=max_tokens,
        timeout=300,
        extra_headers={
            "HTTP-Referer": "https://github.com/professorpalmer/marionette",
            "X-Title": "Marionette Cache Wars Bench",
        },
        enable_reasoning=False,
    )


def _extract_cache_meta(resp) -> Tuple[int, int]:
    meta = getattr(resp, "meta", None) or {}
    cached = int(meta.get("cache_read_tokens") or 0)
    written = int(meta.get("cache_write_tokens") or 0)
    # OpenRouter sometimes nests write under raw usage — peek if present.
    raw = meta.get("raw_usage") or {}
    if not written and isinstance(raw, dict):
        details = raw.get("prompt_tokens_details") or {}
        written = int(details.get("cache_write_tokens") or details.get("cache_write") or 0)
        if not cached:
            cached = int(details.get("cached_tokens") or 0)
    return cached, written


def _pad_user_message(i: int, n_messages: int, content_tokens: int) -> str:
    """Build a user turn sized to ~content_tokens (AGNT card uses ~5186)."""
    header = (
        f"Bench turn {i + 1}/{n_messages}. "
        f"Reply with exactly: OK-{i + 1}. No tools.\n\n"
    )
    if content_tokens <= 0:
        return header.strip()
    # ~4 chars/token; leave room for the header.
    target_chars = max(0, content_tokens * 4 - len(header))
    # Deterministic filler so cache behavior is stable across arms.
    unit = (
        f"[ctx turn={i + 1} block={{n}}] "
        "Synthetic AGNT-parity ballast for cache-cost measurement. "
    )
    chunks: List[str] = []
    n = 0
    while sum(len(c) for c in chunks) < target_chars:
        n += 1
        chunks.append(unit.replace("{n}", str(n)))
    body = "".join(chunks)[:target_chars]
    return header + body


def _run_arm(
    *,
    arm: str,
    model: str,
    n_messages: int,
    tools: list,
    system: str,
    session_id: str,
    write_mult: float,
    max_tokens: int,
    content_tokens: int = 0,
    idle_seconds: float = 0.0,
) -> Dict[str, Any]:
    driver = _make_driver(model, max_tokens=max_tokens)
    history: List[dict] = []
    turns: List[dict] = []
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "usd_total": 0.0,
        "errors": 0,
    }

    for i in range(n_messages):
        if idle_seconds > 0 and i > 0:
            time.sleep(idle_seconds)
        user = _pad_user_message(i, n_messages, content_tokens)
        history.append({"role": "user", "content": user})
        t0 = time.time()
        resp = driver.chat(
            history,
            tools=tools,
            system=system,
            session_id=session_id,
        )
        wall = time.time() - t0
        if resp.error:
            turns.append({"i": i + 1, "error": resp.error, "wall_s": wall})
            totals["errors"] += 1
            # Keep going with a stub assistant so the conversation shape survives.
            history.append({"role": "assistant", "content": f"OK-{i + 1}"})
            continue

        cached, written = _extract_cache_meta(resp)
        # Prefer provider prompt_tokens; fall back to estimate.
        prompt_tokens = int(resp.tokens_in or 0)
        completion_tokens = int(resp.tokens_out or 0)
        priced = _price_turn(
            prompt_tokens=prompt_tokens,
            cached_tokens=cached,
            cache_write_tokens=written,
            completion_tokens=completion_tokens,
            write_mult=write_mult,
        )
        turn = {
            "i": i + 1,
            "wall_s": round(wall, 3),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached,
            "cache_write_tokens": written,
            "usd": round(priced["usd_total"], 6),
            "text_preview": (resp.text or "")[:80],
        }
        turns.append(turn)
        totals["prompt_tokens"] += prompt_tokens
        totals["completion_tokens"] += completion_tokens
        totals["cached_tokens"] += cached
        totals["cache_write_tokens"] += written
        totals["usd_total"] += priced["usd_total"]

        history.append({
            "role": "assistant",
            "content": (resp.text or f"OK-{i + 1}").strip() or f"OK-{i + 1}",
        })

    totals["usd_total"] = round(totals["usd_total"], 6)
    return {"arm": arm, "model": model, "turns": turns, "totals": totals}


def _configure_arm_env(arm: str) -> float:
    """Set env for arm; return cache-write multiplier used for pricing."""
    # Clear first so arms don't leak.
    os.environ.pop("HARNESS_PROMPT_CACHE", None)
    os.environ.pop("HARNESS_ANTHROPIC_CACHE_TTL", None)
    if arm == "no_cache":
        os.environ["HARNESS_PROMPT_CACHE"] = "0"
        return CACHE_WRITE_5M_MULT  # unused when no writes
    if arm == "marionette_5m":
        os.environ["HARNESS_ANTHROPIC_CACHE_TTL"] = "5m"
        return CACHE_WRITE_5M_MULT
    # marionette_1h / marionette_all_1h / unknown → AGNT-style all-1h default
    os.environ["HARNESS_ANTHROPIC_CACHE_TTL"] = "1h"
    return CACHE_WRITE_1H_MULT


def _write_receipt(payload: Dict[str, Any]) -> Tuple[Path, Path]:
    out_dir = REPO_ROOT / "bench" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"cache_wars_{stamp}.json"
    md_path = out_dir / f"cache_wars_{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    oh = payload["overhead"]
    proj = payload["projection"]
    lines = [
        f"# Marionette Cache Wars receipt — {stamp}",
        "",
        f"Model: `{payload.get('model')}`  ",
        f"Mode: `{payload.get('mode')}`  ",
        f"Messages/arm: `{payload.get('messages')}`  ",
        f"Content tokens/msg: `{payload.get('content_tokens', 0)}`",
        "",
        "## Harness overhead",
        "",
        f"- Tools: **{oh['tools_count']}** defs, ~**{oh['tools_tokens_est']}** tokens",
        f"- System (lean): ~**{oh['system_tokens_est']}** tokens",
        f"- Stable prefix (system+tools): ~**{oh['stable_prefix_tokens_est']}** tokens",
        f"- Claude Code bloat claim: **{oh['claude_code_bloat_claim']}** tokens/msg",
        f"- Delta vs Claude Code bloat: **{oh['vs_claude_code_bloat']}** tokens "
        f"({'leaner' if oh['vs_claude_code_bloat'] < 0 else 'heavier'})",
        "",
        "## AGNT-card projection (not live)",
        "",
        f"- Marionette 1h: **${proj['marionette_1h_usd']}**",
        f"- Marionette 5m: **${proj['marionette_5m_usd']}**",
        f"- Marionette no-cache: **${proj['marionette_no_cache_usd']}**",
        f"- Claude Code 1h (same formula, +6817 bloat): **${proj['claude_code_1h_usd']}**",
        f"- AGNT published: ${proj['agnt_published_usd']} / Claude Code published: "
        f"${proj['claude_code_published_usd']} / No-cache published: ${proj['no_cache_published_usd']}",
        "",
        f"_{proj['note']}_",
        "",
    ]
    live = payload.get("live") or {}
    if live:
        lines += ["## Live OpenRouter arms", ""]
        lines.append("| Arm | USD | prompt tok | cache read | cache write | out tok | errors |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for name, arm in live.items():
            t = arm["totals"]
            lines.append(
                f"| {name} | ${t['usd_total']:.4f} | {t['prompt_tokens']} | "
                f"{t['cached_tokens']} | {t['cache_write_tokens']} | "
                f"{t['completion_tokens']} | {t['errors']} |"
            )
        lines.append("")
        # Headline — prefer marionette_all_1h alias, fall back to marionette_1h
        a1 = (
            live.get("marionette_all_1h", {}).get("totals", {}).get("usd_total")
            or live.get("marionette_1h", {}).get("totals", {}).get("usd_total")
        )
        a5 = live.get("marionette_5m", {}).get("totals", {}).get("usd_total")
        an = live.get("no_cache", {}).get("totals", {}).get("usd_total")
        if a1 is not None and an is not None and an > 0:
            saved = (an - a1) / an * 100.0
            lines.append(
                f"**Live headline:** all-1h is **{saved:.1f}%** cheaper than "
                f"no_cache on this run (${a1:.4f} vs ${an:.4f})."
            )
        if a1 is not None and a5 is not None:
            lines.append(
                f"all-1h vs 5m on this run: ${a1:.4f} vs ${a5:.4f} "
                f"(gap widens with idle >5m between turns; this bench is back-to-back)."
            )
        lines.append("")
        lines.append(
            "Caveat: back-to-back turns keep the 5m cache warm, so all-1h vs 5m may look "
            "similar unless you pass `--idle-seconds 360` (or similar) between turns."
        )
    lines.append("")
    lines.append("## Claim hygiene")
    lines.append("")
    lines.append(
        "- Safe: Marionette default is AGNT-style all-1h Anthropic cache_control "
        "(system + last tool + last 2 history msgs all get ttl=1h; incl. Claude via "
        "OpenRouter). The marionette_5m arm is the old hybrid/OMP-era comparison "
        "(HARNESS_ANTHROPIC_CACHE_TTL=5m). Lower tool/system overhead than the "
        "Claude Code +6.8k bloat cited in the AGNT card."
    )
    lines.append(
        "- Not safe from this alone: \"cheapest harness always\" / \"beats AGNT\" — "
        "AGNT's published $2.68 needs their harness run under the same protocol. "
        "Live OpenRouter re-bench still needed after the all-1h steal."
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="Call OpenRouter for real")
    ap.add_argument("--dry-run", action="store_true", help="Overhead + projection only")
    ap.add_argument("--messages", type=int, default=DEFAULT_MESSAGES)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument(
        "--arms",
        default="marionette_1h,marionette_5m,no_cache",
        help=(
            "Comma list of arms to run live "
            "(marionette_all_1h aliases marionette_1h / AGNT all-1h default)"
        ),
    )
    ap.add_argument("--max-tokens", type=int, default=32, help="Cap completion spend")
    ap.add_argument(
        "--idle-seconds",
        type=float,
        default=0.0,
        help="Sleep between turns (use >300 to stress 5m TTL vs 1h)",
    )
    ap.add_argument(
        "--content-tokens",
        type=int,
        default=0,
        help=(
            "Pad each user turn to ~N tokens (AGNT card uses 5186). "
            "0 = short OK-N turns only."
        ),
    )
    args = ap.parse_args(argv)

    if not args.live and not args.dry_run:
        args.dry_run = True  # safe default

    overhead = _marionette_overhead()
    projection = _project_agnt_scenario(overhead)
    payload: Dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if args.live else "dry-run",
        "model": args.model,
        "messages": args.messages,
        "content_tokens": args.content_tokens,
        "prices": {
            "price_in": PRICE_IN,
            "price_out": PRICE_OUT,
            "cache_read_mult": CACHE_READ_MULT,
            "cache_write_5m_mult": CACHE_WRITE_5M_MULT,
            "cache_write_1h_mult": CACHE_WRITE_1H_MULT,
        },
        "overhead": overhead,
        "projection": projection,
    }

    if args.live:
        _load_openrouter_key()
        from harness.pilot import build_tools_schema
        from pmharness.drivers.base import SYSTEM_PROMPT

        tools = build_tools_schema(no_delegation=False, browser_enabled=True)
        system = SYSTEM_PROMPT
        live: Dict[str, Any] = {}
        arms = [a.strip() for a in args.arms.split(",") if a.strip()]
        for arm in arms:
            write_mult = _configure_arm_env(arm)
            print(f"== arm {arm} ({args.messages} turns, model={args.model}) ==")
            # Fresh session id per arm so sticky routing / cache don't cross-contaminate.
            sid = f"cache-wars-{arm}-{int(time.time())}"
            result = _run_arm(
                arm=arm,
                model=args.model,
                n_messages=args.messages,
                tools=tools,
                system=system,
                session_id=sid,
                write_mult=write_mult,
                max_tokens=args.max_tokens,
                content_tokens=args.content_tokens,
                idle_seconds=args.idle_seconds,
            )
            live[arm] = result
            t = result["totals"]
            print(
                f"   usd=${t['usd_total']:.4f}  prompt={t['prompt_tokens']}  "
                f"cache_read={t['cached_tokens']}  cache_write={t['cache_write_tokens']}  "
                f"out={t['completion_tokens']}  errors={t['errors']}"
            )
        payload["live"] = live

    json_path, md_path = _write_receipt(payload)
    print(f"\nWrote {json_path}")
    print(f"Wrote {md_path}")
    print("\n--- overhead ---")
    print(json.dumps(overhead, indent=2))
    print("\n--- projection ---")
    print(json.dumps(projection, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
