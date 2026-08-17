from __future__ import annotations

"""Hermetic compaction-residual laboratory.

Drives the real ConversationalSession / ``_maybe_compact_history(force=True)``
seam — not ``pmharness.run_episode`` (that path does not compact conversations)
and not CassetteDriver (its request hash includes the residual).

Arms:
    A  scripted omission control (not production summarizer quality)
    B  catalog residual
    C  catalog + Wave 1 archive-backed peek_history
    D  no-compaction ceiling (explicit residual=off)

Default execution is stdlib-only, no API keys, and fast enough for unit tests.
"""

import argparse
import json
import os
import shutil
import tempfile
from typing import Any, Callable, Iterable, Optional

from pmharness.compaction_residual_battery import (
    RESIDUAL_CASES,
    ResidualCase,
    cases_by_id,
)

ARM_A = "A"
ARM_B = "B"
ARM_C = "C"
ARM_D = "D"
ALL_ARMS = (ARM_A, ARM_B, ARM_C, ARM_D)

_ARM_RESIDUAL = {
    ARM_A: "summary",
    ARM_B: "catalog",
    ARM_C: "catalog",
    ARM_D: "off",
}

# Arm A control: valid four-heading seed that omits buried facts on purpose.
# This is a scripted omission, not a claim about production summarizer quality.
SCRIPTED_SUMMARY = (
    "## Historical Task Snapshot\n"
    "Long-session docs pass. Earlier turns were compacted for residual comparison.\n"
    "## Resolved\n"
    "Acknowledged the docs-only refactor requests in the filler turns.\n"
    "## Pending / Open Questions\n"
    "Continue the current docs pass.\n"
    "## Key Facts / Decisions / Files\n"
    "No buried handles retained in this scripted summary.\n"
)


class ScriptedPilot:
    """Mock pilot used only by arm A. Catalog/hybrid/off must not need it."""

    name = "scripted-residual"

    def __init__(self, return_text: str = SCRIPTED_SUMMARY) -> None:
        self.return_text = return_text
        self.chat_calls: list[tuple] = []
        self.complete_calls: list[tuple] = []

    def chat(self, messages, tools=None, system=None):
        self.chat_calls.append((messages, system))
        return type("Resp", (), {"text": self.return_text, "error": None, "tokens_out": 8})()

    def complete(self, prompt, system=None):
        self.complete_calls.append((prompt, system))
        return type("Resp", (), {"text": self.return_text, "error": None, "tokens_out": 8})()


class RaisingPilot:
    """Fails if the summarizer path is entered — catalog/off must stay extractive."""

    name = "raising-residual"

    def chat(self, messages, tools=None, system=None):
        raise RuntimeError("catalog/off residual must not call the summarizer")

    def complete(self, prompt, system=None):
        raise RuntimeError("catalog/off residual must not call the summarizer")


def _estimate_tokens(text: str) -> int:
    return max(0, len(text or "") // 4)


def score_residual_text(case: ResidualCase, text: str) -> dict[str, Any]:
    """Deterministic substring oracles. ``must_contain`` is all-of, not any-of."""
    blob = (text or "").lower()
    tokens = tuple(str(token).lower() for token in case.must_contain)
    hit = bool(tokens) and all(token in blob for token in tokens)
    fab = any(str(token).lower() in blob for token in case.must_not_contain) if case.must_not_contain else False
    return {
        "buried_fact_recall": bool(hit),
        "false_recall": bool(fab),
        "end_task_success": bool(hit and not fab),
    }


def _env_scope(updates: dict[str, str]) -> Callable[[], None]:
    previous: dict[str, Optional[str]] = {}
    for key, value in updates.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value

    def restore() -> None:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old

    return restore


def _residual_text(session: Any) -> str:
    parts: list[str] = []
    for message in getattr(session, "_history", []) or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system":
            continue
        parts.append(str(message.get("content") or ""))
    return "\n".join(parts)


def _peek_windows(session: Any, *, max_calls: int = 3) -> tuple[str, int, int]:
    from harness.pilot import PilotAction

    chunks: list[str] = []
    calls = 0
    tokens = 0
    offset = 0
    total = 0
    while calls < max_calls:
        ok, status, text = session._do_peek_history(
            PilotAction(kind="peek_history", arguments={"offset": offset, "limit": 20})
        )
        calls += 1
        text = text or ""
        tokens += _estimate_tokens(text)
        chunks.append(text)
        if not ok or status != "success":
            break
        parsed_total = 0
        for line in text.splitlines():
            if line.startswith("offset="):
                for part in line.split():
                    if part.startswith("total="):
                        try:
                            parsed_total = int(part.split("=", 1)[1])
                        except ValueError:
                            parsed_total = 0
        if parsed_total:
            total = parsed_total
        offset += 20
        if total and offset >= total:
            break
        if not parsed_total:
            break
    return "\n".join(chunks), calls, tokens


def run_residual_arm(
    case: ResidualCase,
    arm: str,
    *,
    state_dir: str = "",
) -> dict[str, Any]:
    """Run one case/arm through the live compaction seam."""
    if arm not in _ARM_RESIDUAL:
        raise ValueError(f"unknown arm {arm!r}")

    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.sessions import save_transcript

    owned_tmp = None
    if not state_dir:
        owned_tmp = tempfile.mkdtemp(prefix="residual-lab-")
        state_dir = owned_tmp

    restore = _env_scope({
        "HARNESS_COMPACTION_RESIDUAL": _ARM_RESIDUAL[arm],
        "HARNESS_MIN_COMPACTABLE_TOKENS": "0",
        "HARNESS_COMPACTION_TAIL_TOKENS": "80",
    })
    compacted = False
    peek_calls = 0
    peek_tokens = 0
    peek_text = ""
    mode = ""
    try:
        session = ConversationalSession(
            HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir, max_context_tokens=8000)
        )
        session.harness_session_id = f"residual-{case.id}-{arm}"
        session._history = [dict(row) for row in case.transcript]
        if arm == ARM_A:
            session.pilot = ScriptedPilot()
        else:
            session.pilot = RaisingPilot()

        events = []
        if arm != ARM_D:
            events = list(session._maybe_compact_history(force=True))
            done = [e for e in events if getattr(e, "kind", "") == "compaction"]
            compacted = bool(done) and not done[-1].data.get("aborted")
            if done:
                mode = str(done[-1].data.get("mode") or "")
        else:
            # Explicit off: force=True must still be a no-op ceiling.
            events = list(session._maybe_compact_history(force=True))
            compacted = False
            mode = "off"

        if arm == ARM_C and compacted:
            save_transcript(
                state_dir,
                session.harness_session_id,
                session.export_transcript_data(),
            )
            peek_text, peek_calls, peek_tokens = _peek_windows(session)

        residual = _residual_text(session)
        residual_scored = score_residual_text(case, residual)
        peek_scored = score_residual_text(case, peek_text)
        # Combined success is at least one clean channel so arm C can credit
        # archive-peek lift without scoring residual+peek as one blob.
        if arm == ARM_C:
            buried = bool(
                residual_scored["buried_fact_recall"] or peek_scored["buried_fact_recall"]
            )
            success = bool(
                residual_scored["end_task_success"] or peek_scored["end_task_success"]
            )
            false_recall = bool(residual_scored["false_recall"])
        else:
            buried = bool(residual_scored["buried_fact_recall"])
            success = bool(residual_scored["end_task_success"])
            false_recall = bool(residual_scored["false_recall"])
        residual_tokens = 0
        try:
            residual_tokens = int(session._estimate_context_tokens() or 0)
        except Exception:
            residual_tokens = _estimate_tokens(residual)

        receipt = {
            "arm": arm,
            "case_id": case.id,
            "template": case.template,
            "probe_prompt": case.probe_prompt,
            "residual_mode": _ARM_RESIDUAL[arm],
            "compacted": compacted,
            "mode": mode,
            "buried_fact_recall": buried,
            "false_recall": false_recall,
            "residual_buried_fact_recall": residual_scored["buried_fact_recall"],
            "peek_buried_fact_recall": peek_scored["buried_fact_recall"],
            "peek_false_recall": peek_scored["false_recall"],
            "peek_task_success": peek_scored["end_task_success"],
            "residual_tokens": residual_tokens,
            "peek_calls": peek_calls,
            "peek_tokens": peek_tokens,
            "end_task_success": success,
            "expected_arms": case.expected_arms,
            "event_kinds": [getattr(e, "kind", "") for e in events],
        }
        return receipt
    finally:
        restore()
        if owned_tmp:
            shutil.rmtree(owned_tmp, ignore_errors=True)


def run_compaction_residual_bench(
    *,
    arms: Optional[Iterable[str]] = None,
    case_ids: Optional[Iterable[str]] = None,
    state_dir: str = "",
) -> dict[str, Any]:
    """Run the labeled battery. Default path is hermetic and fast."""
    wanted_arms = tuple(arms) if arms else ALL_ARMS
    catalog = cases_by_id()
    if case_ids:
        cases = [catalog[cid] for cid in case_ids]
    else:
        cases = list(RESIDUAL_CASES)
    rows = []
    for case in cases:
        for arm in wanted_arms:
            rows.append(run_residual_arm(case, arm, state_dir=state_dir))
    successes = sum(1 for row in rows if row["end_task_success"])
    return {
        "n": len(rows),
        "successes": successes,
        "rows": rows,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hermetic compaction-residual laboratory (no API keys).",
    )
    parser.add_argument("--arm", action="append", choices=list(ALL_ARMS), dest="arms")
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    result = run_compaction_residual_bench(
        arms=args.arms,
        case_ids=args.cases,
        state_dir=args.state_dir,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
