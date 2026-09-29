"""GET /api/diagnostics/resources: what this backend is holding, content-free.

Counts, thread names and child-process kinds only. No transcript text, job
goals, command lines or paths, so the payload is safe to paste in a bug report
when the backend is using more memory or CPU than it should.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Callable

JsonPayload = dict

_THREAD_SUFFIX = re.compile(r"[-_ (]*(?:[0-9a-f]{6,}|\d+)\)?$", re.IGNORECASE)
_DEFAULT_THREAD = re.compile(r"Thread-\d+ \((\w+)\)$")


@dataclass
class ResourceServices:
    counts: Callable[[], dict]


def _thread_kind(name: str) -> str:
    """Group per-object threads: 'pmh-cmd-1a2b3c4d' and 'Thread-12' collapse."""
    default = _DEFAULT_THREAD.match(name or "")
    if default:  # Python names unnamed threads 'Thread-N (target)'
        return default.group(1)
    base = _THREAD_SUFFIX.sub("", name or "") or "unnamed"
    return base.rstrip("-_ ") or "unnamed"


def _child_kind(argv: list) -> str:
    """Executable plus the module/script name. Never free text or paths."""
    if not argv:
        return "?"
    kind = os.path.basename(argv[0])
    rest = argv[1:]
    if "-m" in rest:
        i = rest.index("-m")
        if i + 1 < len(rest):
            kind += " -m " + rest[i + 1]
            for token in rest[i + 2:]:
                if re.fullmatch(r"[a-z][a-z0-9_-]{1,24}", token):
                    return kind + " " + token
            return kind
    for token in rest:
        base = os.path.basename(token)
        if base.endswith((".py", ".js", ".cjs", ".mjs")):
            return kind + " " + base
    return kind


def _children() -> list[dict]:
    if os.name != "posix":
        return []
    try:
        out = subprocess.run(
            ["ps", "-ww", "-Ao", "pid=,ppid=,stat=,rss=,time=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    me = str(os.getpid())
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 5 or parts[1] != me or parts[5:] == ["ps -ww -Ao pid=,ppid=,stat=,rss=,time=,command="]:
            continue
        pid, _ppid, stat, rss, cpu = parts[:5]
        argv = parts[5].split() if len(parts) > 5 else []
        rows.append({
            "pid": int(pid),
            "kind": "zombie" if stat.startswith("Z") else _child_kind(argv),
            "rss_mb": round(int(rss) / 1024, 1) if rss.isdigit() else None,
            "cpu_time": cpu,
        })
    return rows


def _rss_mb() -> float | None:
    if os.name == "posix":
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                                 capture_output=True, text=True, timeout=5).stdout
            return round(int(out.strip()) / 1024, 1)
        except Exception:
            return None
    return None


def get_resources(svc: ResourceServices) -> tuple[int, JsonPayload]:
    times = os.times()
    threads = Counter(_thread_kind(t.name) for t in threading.enumerate())
    try:
        counts = svc.counts()
    except Exception as exc:
        counts = {"error": exc.__class__.__name__}
    return 200, {
        "ok": True,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "rss_mb": _rss_mb(),
        "cpu_seconds": round(times.user + times.system, 2),
        "uptime_seconds": round(time.monotonic() - _STARTED, 1),
        "threads": {"total": sum(threads.values()), "by_kind": dict(threads.most_common())},
        "children": _children(),
        "counts": counts,
    }


_STARTED = time.monotonic()
