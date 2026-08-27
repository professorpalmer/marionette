from __future__ import annotations

"""Session-local persistent REPL (L2): Python objects live in the session kernel.

Large eval/log/verifier output is bound as kernel variables (or spill-backed
text loaded into the kernel). Values are serialized into the pilot context only
when ``show_kernel`` asks — they do not enter transcript tokens or compaction
summaries by default. Reuses the ipython kernel owner and spill registry.
"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .ipython_kernel import get_or_create_kernel

BINDINGS_FILENAME = "session_repl_bindings.json"
MAX_KERNEL_BINDINGS = 32
OFFLOAD_OUTPUT_CHARS = 4096
_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,47}$")


class SessionReplError(ValueError):
    """Invalid binding name, cap exceeded, or kernel failure."""


class SessionReplStore:
    """Durable binding index under the session state_dir (metadata only)."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir or ""
        self.path = (
            os.path.join(self.state_dir, BINDINGS_FILENAME) if self.state_dir else ""
        )

    def _load(self) -> Dict[str, dict]:
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, dict] = {}
        for key, meta in raw.items():
            name = str(key or "").strip()
            if not name or not isinstance(meta, dict):
                continue
            out[name] = dict(meta)
        return out

    def _save(self, data: Dict[str, dict]) -> None:
        if not self.path or not self.state_dir:
            raise SessionReplError("session repl store has no state_dir")
        os.makedirs(self.state_dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    def register(
        self,
        name: str,
        *,
        kind: str,
        char_estimate: int = 0,
        spill_uri: str = "",
    ) -> None:
        cleaned = _validate_name(name)
        data = self._load()
        replacing = cleaned in data
        if not replacing and len(data) >= MAX_KERNEL_BINDINGS:
            raise SessionReplError(
                f"kernel binding cap reached ({MAX_KERNEL_BINDINGS} max)"
            )
        data[cleaned] = {
            "kind": kind,
            "char_estimate": int(char_estimate),
            "spill_uri": spill_uri or "",
            "created_at": time.time(),
        }
        self._save(data)

    def remove(self, name: str) -> bool:
        cleaned = (name or "").strip()
        if not cleaned:
            return False
        data = self._load()
        if cleaned not in data:
            return False
        del data[cleaned]
        self._save(data)
        return True

    def list_meta(self) -> List[Tuple[str, dict]]:
        return sorted(self._load().items(), key=lambda row: row[0])

    def clear(self) -> int:
        data = self._load()
        n = len(data)
        if n or (self.path and os.path.isfile(self.path)):
            self._save({})
        return n


def _validate_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned or not _SAFE_NAME.match(cleaned):
        raise SessionReplError(
            "kernel binding name must match [A-Za-z_][A-Za-z0-9_]{0,47}"
        )
    return cleaned


def _get_store(session: Any) -> SessionReplStore:
    store = getattr(session, "_repl_store", None)
    if isinstance(store, SessionReplStore):
        return store
    state_dir = (
        getattr(session, "state_dir", None)
        or getattr(getattr(session, "config", None), "state_dir", None)
        or ""
    )
    store = SessionReplStore(str(state_dir))
    try:
        setattr(session, "_repl_store", store)
    except Exception:
        pass
    return store


def _session_id(session: Any) -> str:
    sid = getattr(session, "harness_session_id", None) or getattr(session, "session_id", None)
    return str(sid or "default")


def bind_text(session: Any, name: str, text: str) -> str:
    """Store large text in the kernel namespace under ``name``."""
    cleaned = _validate_name(name)
    payload = "" if text is None else str(text)
    kernel = get_or_create_kernel(session)
    code = (
        f"{cleaned} = {json.dumps(payload)}\n"
        f"_repl_bindings = globals().setdefault('_repl_bindings', {{}})\n"
        f"_repl_bindings[{json.dumps(cleaned)}] = {json.dumps(cleaned)}"
    )
    result = kernel.execute(code)
    if not result.ok:
        raise SessionReplError(result.error or "kernel bind failed")
    _get_store(session).register(
        cleaned, kind="text", char_estimate=len(payload)
    )
    return cleaned


def bind_spill(session: Any, name: str, spill_uri: str) -> str:
    """Load a spill:// body into the kernel under ``name``."""
    cleaned = _validate_name(name)
    uri = (spill_uri or "").strip()
    if not uri.startswith("spill://"):
        raise SessionReplError("bind_spill requires a spill:// URI")
    from .internal_uri import InternalUriContext, resolve_internal_uri

    ctx = InternalUriContext(
        state_dir=str(
            getattr(session, "state_dir", None)
            or getattr(getattr(session, "config", None), "state_dir", None)
            or ""
        ),
        session_id=_session_id(session),
    )
    resource = resolve_internal_uri(uri, ctx)
    text = resource.content if resource and not resource.is_directory else ""
    if not isinstance(text, str) or not text:
        raise SessionReplError(f"spill empty or unreadable: {uri}")
    bound = bind_text(session, cleaned, text)
    _get_store(session).register(
        bound, kind="spill", char_estimate=len(text), spill_uri=uri
    )
    return bound


def list_bindings(session: Any) -> List[dict]:
    rows = []
    for name, meta in _get_store(session).list_meta():
        rows.append(
            {
                "name": name,
                "kind": meta.get("kind") or "object",
                "char_estimate": int(meta.get("char_estimate") or 0),
                "spill_uri": meta.get("spill_uri") or "",
            }
        )
    return rows


def serialize_bindings(session: Any, names: Optional[List[str]] = None) -> str:
    """On-demand serialization of kernel bindings for prompt injection."""
    store = _get_store(session)
    available = {n for n, _ in store.list_meta()}
    if names:
        targets = []
        for raw in names:
            cleaned = (raw or "").strip()
            if cleaned and cleaned in available:
                targets.append(cleaned)
    else:
        targets = sorted(available)
    if not targets:
        return "(kernel bindings empty)"
    kernel = get_or_create_kernel(session)
    lines = []
    for name in targets:
        code = (
            f"import pprint\n"
            f"try:\n"
            f"    _v = {name}\n"
            f"except NameError:\n"
            f"    _v = '<missing>'\n"
            f"print(pprint.pformat(_v, width=120)[:12000])"
        )
        result = kernel.execute(code)
        body = (result.output or result.error or "").strip() or "<empty>"
        lines.append(f"## {name}\n{body}")
    return "\n\n".join(lines)


def offload_ipython_output(session: Any, output: str, *, prefix: str = "_repl_out") -> Tuple[str, Optional[str]]:
    """When stdout is large, bind it in-kernel and return a short handle."""
    text = output or ""
    if len(text) < OFFLOAD_OUTPUT_CHARS:
        return text, None
    seq = int(getattr(session, "_repl_offload_seq", 0) or 0) + 1
    try:
        setattr(session, "_repl_offload_seq", seq)
    except Exception:
        pass
    name = f"{prefix}_{seq}"
    try:
        bind_text(session, name, text)
    except SessionReplError:
        return text, None
    summary = (
        f"(kernel L2: bound {len(text)} chars as {name}; "
        f"use show_kernel with path={name!r} to serialize into context)"
    )
    return summary, name


def clear_bindings(session: Any, name: str = "") -> int:
    store = _get_store(session)
    if name:
        cleaned = _validate_name(name)
        kernel = get_or_create_kernel(session)
        kernel.execute(f"globals().pop({json.dumps(cleaned)}, None)")
        return 1 if store.remove(cleaned) else 0
    kernel = get_or_create_kernel(session)
    kernel.execute("_repl_bindings = {}")
    return store.clear()
