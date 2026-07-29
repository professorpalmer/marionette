#!/usr/bin/env python3
"""Optional Linux-only soak/benchmark for worktree seed copy strategies.

Compares wall time and on-disk block usage between ``copy`` and ``reflink``
(``auto`` when supported) on a synthetic tree. Not part of normal CI.

Usage (Linux with reflink-capable filesystem recommended):

  python -m bench.worktree_seed_copy --files 200 --size-kb 64
  python -m bench.worktree_seed_copy --strategy copy --strategy reflink
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.worktree_seed import reset_copy_capability_cache, seed_worktree_from_goal


def _du_blocks_kib(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.stat(os.path.join(root, name))
                total += getattr(st, "st_blocks", 0)
            except OSError:
                pass
    return total // 2  # st_blocks is 512-byte units -> KiB


def _build_tree(root: Path, *, files: int, size_kb: int) -> None:
    payload = (b"x" * 1024) * max(1, size_kb)
    for i in range(files):
        rel = root / f"pkg/module_{i:04d}.bin"
        rel.parent.mkdir(parents=True, exist_ok=True)
        rel.write_bytes(payload)


def _run_arm(repo: Path, wt: Path, strategy: str, goal: str) -> dict:
    reset_copy_capability_cache()
    if wt.exists():
        shutil.rmtree(wt)
    wt.mkdir()
    t0 = time.perf_counter()
    result = seed_worktree_from_goal(str(repo), str(wt), goal, copy_strategy=strategy)
    elapsed = time.perf_counter() - t0
    blocks_kib = _du_blocks_kib(wt)
    return {
        "strategy": strategy,
        "elapsed_sec": round(elapsed, 4),
        "disk_blocks_kib": blocks_kib,
        "seeded_files": len(result.paths),
        "copy_stats": result.copy_stats.as_log_dict(),
    }


def main() -> int:
    if not sys.platform.startswith("linux"):
        print("bench.worktree_seed_copy is Linux-only (optional soak).", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description="Worktree seed copy strategy benchmark")
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--size-kb", type=int, default=32)
    parser.add_argument(
        "--strategy",
        action="append",
        choices=("copy", "reflink", "auto"),
        help="Repeatable; default copy+reflink when reflink may work",
    )
    args = parser.parse_args()

    strategies = args.strategy or ["copy", "reflink"]
    tmp = Path(tempfile.mkdtemp(prefix="pmharness-wt-copy-bench-"))
    repo = tmp / "repo"
    repo.mkdir()
    _build_tree(repo, files=args.files, size_kb=args.size_kb)
    goal = "edit " + " ".join(f"pkg/module_{i:04d}.bin" for i in range(min(args.files, 8)))

    arms = [_run_arm(repo, tmp / f"wt-{s}", s, goal) for s in strategies]
    receipt = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": sys.platform,
        "files": args.files,
        "size_kb": args.size_kb,
        "arms": arms,
    }
    out_dir = REPO_ROOT / "bench" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"worktree_seed_copy_{stamp}.json"
    json_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    print(f"wrote {json_path}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
