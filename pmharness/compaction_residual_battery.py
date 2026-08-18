from __future__ import annotations

"""Deterministic compaction-residual battery (research lab, not product GUI).

Labeled templates bury a fact in a long-session middle, then probe whether
the residual (and, for arm C, archive-backed peek) still carries it. Scoring is
stdlib substring oracles — no LLM-as-judge, no API keys.

Arm A is a scripted omission control (the mock summary deliberately drops
buried tokens). It is not a measure of production summarizer quality.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResidualCase:
    id: str
    template: str
    transcript: tuple
    probe_prompt: str
    must_contain: tuple
    must_not_contain: tuple = ()
    expected_arms: dict = field(default_factory=dict)
    # False when the catalog residual is designed to miss the buried fact so
    # arm C can measure archive-peek lift separately from residual recall.
    catalog_recalls_fact: bool = True


def _msg(role: str, content: str, **extra: Any) -> dict:
    row = {"role": role, "content": content}
    row.update(extra)
    return row


def _filler_pairs(n: int, prefix: str) -> list[dict]:
    rows: list[dict] = []
    for i in range(n):
        rows.append(_msg("user", f"{prefix} user {i}: refactor comments and docs only. " + ("pad " * 20)))
        rows.append(_msg("assistant", f"{prefix} assistant {i}: acknowledged docs pass. " + ("ack " * 20)))
    return rows


def _tail() -> list[dict]:
    return [
        _msg("user", "Please continue the current docs pass."),
        _msg("assistant", "Continuing the docs pass without restating earlier facts."),
    ]


def _base_system() -> list[dict]:
    return [_msg("system", "You are a coding assistant in a long session.")]


def _arm_meta() -> dict:
    return {
        "A": {"peek": False, "compact": True, "residual": "summary"},
        "B": {"peek": False, "compact": True, "residual": "catalog"},
        "C": {"peek": True, "compact": True, "residual": "catalog"},
        "D": {"peek": False, "compact": False, "residual": "off"},
    }


def _early_constraint() -> ResidualCase:
    buried = (
        "CONSTRAINT: never write to production.db; use scratch.sqlite only."
    )
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Constraint recorded.")]
        + _filler_pairs(8, "early")
        + _tail()
    )
    return ResidualCase(
        id="early_constraint",
        template="early_constraint",
        transcript=tuple(history),
        probe_prompt="What early write constraint was set for the database files?",
        must_contain=("never write to production.db", "scratch.sqlite"),
        must_not_contain=("write to production.db is required",),
        expected_arms=_arm_meta(),
    )


def _mid_session_file_path() -> ResidualCase:
    path = "src/billing/ledger_v3.py"
    history = (
        _base_system()
        + _filler_pairs(3, "pre")
        + [
            _msg("user", f"Please read {path} and report the public entry points."),
            _msg(
                "assistant",
                "reading the ledger module",
                tool_calls=[{
                    "id": "call_read_ledger",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"path": "{path}"}}',
                    },
                }],
            ),
            _msg(
                "tool",
                f"def post_invoice():\n    return {path}\n",
                tool_call_id="call_read_ledger",
                _read_path=path,
            ),
            _msg("assistant", f"Read {path}; post_invoice is the public entry."),
        ]
        + _filler_pairs(5, "post")
        + _tail()
    )
    return ResidualCase(
        id="mid_session_file_path",
        template="mid_session_file_path",
        transcript=tuple(history),
        probe_prompt="Which billing ledger file was read mid-session?",
        must_contain=("src/billing/ledger_v3.py",),
        must_not_contain=("src/billing/ledger_v2.py",),
        expected_arms=_arm_meta(),
    )


def _reversed_decision() -> ResidualCase:
    history = (
        _base_system()
        + [
            _msg("user", "Let's use Redis for the session store."),
            _msg("assistant", "Redis is a candidate."),
        ]
        + _filler_pairs(4, "mid")
        + [
            _msg("user", "Decision: use SQLite instead of Redis for the session store."),
            _msg("assistant", "Switched the store decision to SQLite."),
        ]
        + _filler_pairs(4, "after")
        + _tail()
    )
    return ResidualCase(
        id="reversed_decision",
        template="reversed_decision",
        transcript=tuple(history),
        probe_prompt="What is the current session-store decision?",
        must_contain=("sqlite instead of redis",),
        must_not_contain=("keep redis", "postgres"),
        expected_arms=_arm_meta(),
    )


def _error_tail_fact() -> ResidualCase:
    err = "ERROR: ConfigMissing: /etc/marionette/secret-policy.yaml not found (code E-7721)"
    history = (
        _base_system()
        + _filler_pairs(6, "pre-err")
        + [
            _msg("user", "Load the policy file."),
            _msg(
                "assistant",
                "loading policy",
                tool_calls=[{
                    "id": "call_policy",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "policy.yaml"}'},
                }],
            ),
            _msg("tool", err, tool_call_id="call_policy"),
            _msg("assistant", "Policy load failed with the error above."),
        ]
        + _filler_pairs(2, "post-err")
        + _tail()
    )
    return ResidualCase(
        id="error_tail_fact",
        template="error_tail_fact",
        transcript=tuple(history),
        probe_prompt="What error code was returned when the policy file was missing?",
        must_contain=("E-7721", "secret-policy.yaml"),
        must_not_contain=("E-7700",),
        expected_arms=_arm_meta(),
    )


def _spill_artifact_handle() -> ResidualCase:
    spill = "spill://sess-lab/result-omega"
    artifact = "artifact://job-omega/finding-0"
    job = "job://job-omega"
    history = (
        _base_system()
        + _filler_pairs(4, "pre-spill")
        + [
            _msg("user", "Run the audit swarm and keep the handles."),
            _msg(
                "assistant",
                "swarm running",
                tool_calls=[{
                    "id": "call_swarm",
                    "type": "function",
                    "function": {"name": "run_swarm", "arguments": '{"goal": "audit"}'},
                }],
            ),
            _msg(
                "tool",
                f"full output at {spill} and {artifact}; also {job}",
                tool_call_id="call_swarm",
            ),
            _msg("assistant", f"Audit handles retained: {spill} {artifact} {job}"),
        ]
        + _filler_pairs(4, "post-spill")
        + _tail()
    )
    return ResidualCase(
        id="spill_artifact_handle",
        template="spill_artifact_handle",
        transcript=tuple(history),
        probe_prompt="What durable spill and artifact handles were produced?",
        must_contain=(spill, artifact),
        must_not_contain=("spill://sess-lab/result-alpha",),
        expected_arms=_arm_meta(),
    )


def _distractor_twin() -> ResidualCase:
    current = "auth_current_v2.py"
    legacy = "auth_legacy_v1.py"
    history = (
        _base_system()
        + [
            _msg("user", f"Ignore {legacy}; it is the retired twin."),
            _msg("assistant", f"{legacy} is not the active module."),
            _msg("user", f"Read {current} — that is the active auth module."),
            _msg(
                "assistant",
                "reading current auth",
                tool_calls=[{
                    "id": "call_auth",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": f'{{"path": "{current}"}}',
                    },
                }],
            ),
            _msg(
                "tool",
                f"active auth lives in {current}",
                tool_call_id="call_auth",
                _read_path=current,
            ),
            _msg("assistant", f"Confirmed active file is {current}."),
        ]
        + _filler_pairs(6, "twin")
        + _tail()
    )
    return ResidualCase(
        id="distractor_twin",
        template="distractor_twin",
        transcript=tuple(history),
        probe_prompt="Which auth module is the active current file?",
        must_contain=(current,),
        # Retired twin is actually in the buried transcript — a vacuous
        # fabrication token would never fire the false-recall guard.
        must_not_contain=(legacy,),
        expected_arms=_arm_meta(),
    )


def _catalog_miss_plain_fact() -> ResidualCase:
    """Nonce measurement tokens with no path or URI shape.

    The catalog now harvests distinctive hyphenated identifiers that contain
    a digit, so these tokens are residual facts rather than peek-only.
    """
    token_a = "omega-cache-token-9f3a"
    token_b = "shard-omega-p95"
    history = (
        _base_system()
        + _filler_pairs(6, "pre-plain")
        + [
            _msg("user", "Run the cache probe and report the raw measurement only."),
            _msg(
                "assistant",
                "probing cache",
                tool_calls=[{
                    "id": "call_cache_probe",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": '{"command": "probe-cache"}',
                    },
                }],
            ),
            _msg(
                "tool",
                f"Plain measurement only: {token_a} observed on {token_b}. "
                "No file path and no durable URI in this result.",
                tool_call_id="call_cache_probe",
            ),
            _msg("assistant", "Cache probe finished; continuing the docs pass."),
        ]
        + _filler_pairs(4, "post-plain")
        + _tail()
    )
    return ResidualCase(
        id="catalog_miss_plain_fact",
        template="catalog_miss_plain_fact",
        transcript=tuple(history),
        probe_prompt="What cache-shard measurement tokens were returned by the probe?",
        must_contain=(token_a, token_b),
        must_not_contain=("omega-cache-token-0000",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
    )


RESIDUAL_CASES: tuple[ResidualCase, ...] = (
    _early_constraint(),
    _mid_session_file_path(),
    _reversed_decision(),
    _error_tail_fact(),
    _spill_artifact_handle(),
    _distractor_twin(),
    _catalog_miss_plain_fact(),
)


def cases_by_id() -> dict[str, ResidualCase]:
    return {case.id: case for case in RESIDUAL_CASES}
