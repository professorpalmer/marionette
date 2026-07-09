"""Append-only ledger for automatic per-turn wiki grounding injections.

Each record captures one auto-injected wiki context block: chars/tokens fed to
the model and a labeled *estimate* of what a manual ``query_wiki`` round-trip
would have cost without auto-grounding.

Baseline (estimate only — never measured):
  estimated_reinference_tokens = pages * REINFERENCE_BASELINE_PER_PAGE

``REINFERENCE_BASELINE_PER_PAGE`` (default 800) models one ``query_wiki`` tool
invocation: question framing, raw wiki blob returned to the model, and the
model re-reading that blob. Multiply by the number of pages surfaced in the
injected slice.

Records are append-only JSONL under the harness state dir. Hot-path helpers
never raise — a failed write must not block chat turns.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from .tool_output_savings import CHARS_PER_TOKEN, estimate_tokens, savings_usd

JSONL_FILENAME = "wiki_grounding_savings.jsonl"
REINFERENCE_BASELINE_PER_PAGE = 800


@dataclass(frozen=True)
class WikiGroundingSummary:
    record_count: int = 0
    chars_fed: int = 0
    tokens_fed: int = 0
    pages_fed: int = 0
    estimated_reinference_tokens: int = 0


def estimated_reinference_tokens(pages: int) -> int:
    """Labeled estimate of tokens a manual query_wiki round-trip would cost."""
    return max(0, int(pages)) * REINFERENCE_BASELINE_PER_PAGE


def parse_jsonl_records(path: str | os.PathLike) -> list[dict]:
    """Load grounding records from JSONL, skipping blank/malformed lines."""
    p = os.fspath(path)
    if not os.path.isfile(p):
        return []
    out: list[dict] = []
    try:
        with open(p, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def aggregate_records(
    records: list[dict],
    *,
    session_id: Optional[str] = None,
) -> WikiGroundingSummary:
    count = 0
    chars = 0
    tokens = 0
    pages = 0
    reinfer = 0
    for rec in records:
        if str(rec.get("kind") or "") != "wiki_grounding":
            continue
        sid = str(rec.get("session_id") or "")
        if session_id is not None and sid != session_id:
            continue
        count += 1
        chars += int(rec.get("chars") or 0)
        tokens += int(rec.get("tokens_fed") or 0)
        pages += int(rec.get("pages") or 0)
        reinfer += int(
            rec.get("estimated_reinference_tokens")
            or estimated_reinference_tokens(int(rec.get("pages") or 0))
        )
    return WikiGroundingSummary(
        record_count=count,
        chars_fed=chars,
        tokens_fed=tokens,
        pages_fed=pages,
        estimated_reinference_tokens=reinfer,
    )


class WikiGroundingLedger:
    """Append-only JSONL ledger under ``state_dir``."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = os.path.abspath(state_dir)
        self._jsonl_path = os.path.join(self.state_dir, JSONL_FILENAME)
        self._lock = threading.Lock()

    def record(
        self,
        *,
        session_id: str,
        chars: int,
        pages: int,
        price_in: Optional[float] = None,
    ) -> bool:
        """Append one grounding record. Returns True when written."""
        if chars <= 0 or pages <= 0:
            return False
        sid = session_id or "default"
        tokens_fed = estimate_tokens(chars)
        reinfer = estimated_reinference_tokens(pages)
        rec = {
            "ts": time.time(),
            "kind": "wiki_grounding",
            "session_id": sid,
            "chars": int(chars),
            "tokens_fed": tokens_fed,
            "pages": int(pages),
            "estimated_reinference_tokens": reinfer,
        }
        if price_in is not None and price_in > 0:
            rec["estimated_savings_usd"] = round(
                savings_usd(reinfer, float(price_in)), 6
            )
        try:
            with self._lock:
                os.makedirs(self.state_dir, exist_ok=True)
                with open(self._jsonl_path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            return True
        except OSError:
            return False

    def summarize(self, *, session_id: Optional[str] = None) -> WikiGroundingSummary:
        records = parse_jsonl_records(self._jsonl_path)
        return aggregate_records(records, session_id=session_id)


_LEDGER_CACHE: dict[str, WikiGroundingLedger] = {}
_LEDGER_CACHE_LOCK = threading.Lock()


def get_ledger(state_dir: str) -> WikiGroundingLedger:
    key = os.path.abspath(state_dir)
    with _LEDGER_CACHE_LOCK:
        ledger = _LEDGER_CACHE.get(key)
        if ledger is None:
            ledger = WikiGroundingLedger(key)
            _LEDGER_CACHE[key] = ledger
        return ledger


def try_record_grounding(
    *,
    state_dir: str,
    session_id: str,
    chars: int,
    pages: int,
    price_in: Optional[float] = None,
) -> None:
    """Hot-path helper: record grounding, swallowing all errors."""
    if chars <= 0 or pages <= 0:
        return
    try:
        get_ledger(state_dir).record(
            session_id=session_id or "default",
            chars=chars,
            pages=pages,
            price_in=price_in,
        )
    except Exception:
        pass


def session_grounding_payload(
    state_dir: str,
    session_id: str,
    price_in: float,
) -> dict:
    """Build API-facing wiki grounding fields for a session."""
    try:
        summary = get_ledger(state_dir).summarize(session_id=session_id or None)
    except Exception:
        summary = WikiGroundingSummary()
    usd = savings_usd(summary.estimated_reinference_tokens, price_in)
    return {
        "wiki_groundings": summary.record_count,
        "wiki_tokens_fed": summary.tokens_fed,
        "wiki_pages_fed": summary.pages_fed,
        "wiki_estimated_reinference_tokens": summary.estimated_reinference_tokens,
        "wiki_estimated_savings_usd": round(usd, 6),
    }
