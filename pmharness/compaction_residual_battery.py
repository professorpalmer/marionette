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
    # True when the live lab must hide peek_history so vault retrieve is
    # the only recovery path. Default stays peek-visible.
    hide_peek: bool = False


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
        probe_prompt=(
            "What error code and policy filename were returned "
            "when the policy file was missing?"
        ),
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


ABSENT_LIVE_TOKEN = "zeta-never-present-token-4c91"
NONCE_FORBIDDEN_WRITE = "prod-omega-lock.db"
NONCE_ALLOWED_WRITE = "scratch-omega-9f3a.sqlite"


def _negative_control_absent_token() -> ResidualCase:
    """Holdout: the probe token never appears in the transcript."""
    history = (
        _base_system()
        + _filler_pairs(6, "neg")
        + _tail()
    )
    return ResidualCase(
        id="negative_control_absent_token",
        template="negative_control",
        transcript=tuple(history),
        probe_prompt=(
            "What value was recorded for zeta-never-present-token-4c91?"
        ),
        must_contain=(),
        must_not_contain=(ABSENT_LIVE_TOKEN,),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=False,
    )


def _long_horizon_early_constraint() -> ResidualCase:
    """Holdout: same write constraint as early_constraint, much more filler."""
    buried = (
        "CONSTRAINT: never write to production.db; use scratch.sqlite only."
    )
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Constraint recorded.")]
        + _filler_pairs(50, "horizon")
        + _tail()
    )
    return ResidualCase(
        id="long_horizon_early_constraint",
        template="early_constraint",
        transcript=tuple(history),
        probe_prompt="What early write constraint was set for the database files?",
        must_contain=("never write to production.db", "scratch.sqlite"),
        must_not_contain=("write to production.db is required",),
        expected_arms=_arm_meta(),
    )


def _nonce_write_constraint() -> ResidualCase:
    """Holdout: session-specific write targets world knowledge cannot guess."""
    buried = (
        f"CONSTRAINT: never write to {NONCE_FORBIDDEN_WRITE}; "
        f"use {NONCE_ALLOWED_WRITE} only."
    )
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Constraint recorded.")]
        + _filler_pairs(3, "nonce-pre")
        + [
            _msg(
                "user",
                "Ignore the generic production.db / scratch.sqlite convention. "
                "The session constraint is the one recorded earlier.",
            ),
            _msg("assistant", "Acknowledged. The recorded constraint stands."),
        ]
        + _filler_pairs(5, "nonce-post")
        + _tail()
    )
    return ResidualCase(
        id="nonce_write_constraint",
        template="nonce_write_constraint",
        transcript=tuple(history),
        probe_prompt=(
            "Name the only database file writes are allowed to, and the file "
            "writes are forbidden on. Use the exact filenames from this session."
        ),
        must_contain=(NONCE_ALLOWED_WRITE, NONCE_FORBIDDEN_WRITE),
        must_not_contain=(f"write to {NONCE_FORBIDDEN_WRITE} is required",),
        expected_arms=_arm_meta(),
    )


def _version_pin() -> ResidualCase:
    """Experimental: dotted version pins with no Decision: prefix."""
    buried = "Pin marionette to 0.9.187 and keep React 18.2.0."
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Versions recorded.")]
        + _filler_pairs(8, "verpin")
        + _tail()
    )
    return ResidualCase(
        id="version_pin",
        template="version_pin",
        transcript=tuple(history),
        probe_prompt="Which marionette and React versions were pinned?",
        must_contain=("0.9.187", "18.2.0"),
        must_not_contain=("0.9.186",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
    )


def _unprefixed_reversal() -> ResidualCase:
    """Experimental: later go-ahead must outrank an earlier don't-write stem."""
    history = (
        _base_system()
        + [
            _msg(
                "user",
                "please don't write to the live ledger; "
                "the east replica is the only sink.",
            ),
            _msg("assistant", "Noted."),
        ]
        + _filler_pairs(4, "rev-mid")
        + [
            _msg(
                "user",
                "go ahead and write to the live ledger now; "
                "the east replica is retired.",
            ),
            _msg("assistant", "Reversed."),
        ]
        + _filler_pairs(4, "rev-after")
        + _tail()
    )
    return ResidualCase(
        id="unprefixed_reversal",
        template="unprefixed_reversal",
        transcript=tuple(history),
        probe_prompt="What is the current live-ledger write policy?",
        must_contain=("write to the live ledger now", "east replica is retired"),
        must_not_contain=("west replica",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
    )


def _stem_cap_later_decision() -> ResidualCase:
    """Experimental: last-wins must keep a late Decision after many stems."""
    early = []
    for i in range(12):
        early.append(_msg("user", f"never touch filler-file-{i}-zz."))
        early.append(_msg("assistant", f"ack filler-file-{i}-zz."))
    history = (
        _base_system()
        + early
        + [
            _msg("user", "Decision: use SQLite instead of Redis for the session store."),
            _msg("assistant", "Switched the store decision to SQLite."),
        ]
        + _filler_pairs(3, "cap-after")
        + _tail()
    )
    return ResidualCase(
        id="stem_cap_later_decision",
        template="reversed_decision",
        transcript=tuple(history),
        probe_prompt="What is the current session-store decision?",
        must_contain=("sqlite instead of redis",),
        must_not_contain=("keep redis", "postgres"),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
    )


def _vault_only_prose_cutoff() -> ResidualCase:
    """Experimental: plain prose the catalog harvests do not keep.

    No Decision:/CONSTRAINT: prefix, no path, no hyphen-digit nonce, no
    dotted version. The vault dump should still retrieve the cutoff sentence
    on the next-turn probe. Not in live_cases(); opt-in via --case.
    """
    buried = (
        "The billing cutoff is the fourteenth of each month "
        "for the omega ledger close."
    )
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted the close date.")]
        + _filler_pairs(8, "cutoff")
        + _tail()
    )
    return ResidualCase(
        id="vault_only_prose_cutoff",
        template="vault_only_prose",
        transcript=tuple(history),
        probe_prompt="When is the billing cutoff for the ledger close?",
        must_contain=("fourteenth of each month",),
        must_not_contain=("fifteenth of each month",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _padded_block(n: int, prefix: str, pad_chars: int = 800) -> list[dict]:
    pad = "x" * pad_chars
    return [_msg("user", f"{prefix} {i} {pad}") for i in range(n)]


def _vault_peek_evicted_cutoff() -> ResidualCase:
    """Experimental: bury the cutoff in the archive middle so peek drops it.

    Oldest+newest retention keeps the 256KB sidecar alive; a mid-history
    sentence is omitted. Vault should still retrieve it. Peek stays offered.
    Not in live_cases(); opt-in via --case.
    """
    buried = (
        "The billing cutoff is the fourteenth of each month "
        "for the omega ledger close."
    )
    history = (
        _base_system()
        + _padded_block(200, "pre")
        + [_msg("user", buried), _msg("assistant", "Noted the close date.")]
        + _padded_block(200, "post")
        + _tail()
    )
    return ResidualCase(
        id="vault_peek_evicted_cutoff",
        template="vault_only_prose",
        transcript=tuple(history),
        probe_prompt="When is the billing cutoff for the ledger close?",
        must_contain=("fourteenth of each month",),
        must_not_contain=("fifteenth of each month",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=False,
        hide_peek=False,
    )


def _vault_narrative_no_overlap() -> ResidualCase:
    """Experimental: later ask shares no tokens with the buried decision.

    Probe is a generic recap. Vault FTS should miss; a paid summary might
    still keep the story. Not in live_cases(); opt-in via --case.
    """
    buried = "The canary ships to the spare region before Friday."
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted the ship plan.")]
        + _filler_pairs(8, "story")
        + _tail()
    )
    return ResidualCase(
        id="vault_narrative_no_overlap",
        template="vault_narrative",
        transcript=tuple(history),
        probe_prompt="Remind me what we decided earlier.",
        must_contain=("spare region",),
        must_not_contain=("primary region",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _vault_paraphrase_no_overlap() -> ResidualCase:
    """Experimental: paraphrase with no shared distinctive tokens.

    Buried cutoff uses twenty-seven and no 'month'. Probe is invoices/freeze.
    Not in live_cases(); opt-in via --case.
    """
    buried = "The omega ledger close uses cutoff day twenty-seven."
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted the close day.")]
        + _filler_pairs(8, "para")
        + _tail()
    )
    return ResidualCase(
        id="vault_paraphrase_no_overlap",
        template="vault_paraphrase",
        transcript=tuple(history),
        probe_prompt="When do invoices freeze?",
        must_contain=("twenty-seven",),
        must_not_contain=("fifteenth", "fourteen"),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _distinctive_pairs(n: int, prefix: str) -> list[dict]:
    """User notes that are real prose, not the lab pad/docs-only filler."""
    topics = (
        "update the changelog tone to be warmer",
        "keep the CI badge amber on flake",
        "rename the footer copy to say staging",
        "move the screenshot to the handbook appendix",
        "switch the spinner label to working",
        "prefer sentence case in the empty state",
        "drop the extra period in the tooltip",
        "align the settings latch copy with the picker",
        "shorten the jump-to-latest aria label",
        "keep the cost pill visible at narrow widths",
        "do not wrap the footer to two rows",
        "leave the session cost lock on the bound pilot",
        "keep Settings open across model toggles",
    )
    rows: list[dict] = []
    for i in range(n):
        line = topics[i % len(topics)]
        rows.append(_msg("user", f"{prefix} note {i}: please {line}."))
        rows.append(_msg("assistant", f"{prefix} note {i}: recorded."))
    return rows


def _vault_false_retrieve_twin() -> ResidualCase:
    """Experimental: retired and current region both match the same ask.

    FTS OR of canary/ship can return the early primary-region line.
    Not in live_cases(); opt-in via --case.
    """
    history = (
        _base_system()
        + [
            _msg("user", "The canary ships to the primary region."),
            _msg("assistant", "Recorded the first ship plan."),
        ]
        + _filler_pairs(4, "oldplan")
        + [
            _msg("user", "The canary now ships to the spare region."),
            _msg("assistant", "Recorded the replacement ship plan."),
        ]
        + _filler_pairs(4, "newplan")
        + _tail()
    )
    return ResidualCase(
        id="vault_false_retrieve_twin",
        template="vault_false_twin",
        transcript=tuple(history),
        probe_prompt="Where does the canary ship?",
        must_contain=("spare region",),
        must_not_contain=("primary region",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _vault_selector_plausible_filler() -> ResidualCase:
    """Experimental: distinctive chrome notes leak into the plan chunk.

    Unlike _filler_pairs, these user lines have no pad/docs-only markers.
    Recap still has the buried canary, plus changelog/CI noise.
    """
    buried = "The canary ships to the spare region before Friday."
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted the ship plan.")]
        + _distinctive_pairs(8, "chrome")
        + _tail()
    )
    return ResidualCase(
        id="vault_selector_plausible_filler",
        template="vault_narrative",
        transcript=tuple(history),
        probe_prompt="Remind me what we decided earlier.",
        must_contain=("spare region",),
        must_not_contain=("primary region",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _vault_selector_docs_only_plan() -> ResidualCase:
    """Experimental: a real plan contains 'docs only' and is dropped as filler."""
    buried = (
        "please keep this docs only: ship the canary to the spare "
        "region before Friday."
    )
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted the ship plan.")]
        + _filler_pairs(8, "docsplan")
        + _tail()
    )
    return ResidualCase(
        id="vault_selector_docs_only_plan",
        template="vault_narrative",
        transcript=tuple(history),
        probe_prompt="Remind me what we decided earlier.",
        must_contain=("spare region",),
        must_not_contain=("primary region",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _vault_selector_cap_drops_late() -> ResidualCase:
    """Experimental: first-12 user-line cap keeps the retired plan, drops spare."""
    history = (
        _base_system()
        + [
            _msg("user", "The canary ships to the primary region."),
            _msg("assistant", "Recorded the first ship plan."),
        ]
        + _distinctive_pairs(12, "cap")
        + [
            _msg("user", "The canary now ships to the spare region."),
            _msg("assistant", "Recorded the replacement ship plan."),
        ]
        + _tail()
    )
    return ResidualCase(
        id="vault_selector_cap_drops_late",
        template="vault_narrative",
        transcript=tuple(history),
        probe_prompt="Remind me what we decided earlier.",
        must_contain=("spare region",),
        must_not_contain=("primary region",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _vault_selector_assistant_only() -> ResidualCase:
    """Experimental: the decision is only in the assistant turn."""
    history = (
        _base_system()
        + [
            _msg("user", "What should we do about the canary?"),
            _msg(
                "assistant",
                "Ship the canary to the spare region before Friday.",
            ),
        ]
        + _filler_pairs(8, "askonly")
        + _tail()
    )
    return ResidualCase(
        id="vault_selector_assistant_only",
        template="vault_narrative",
        transcript=tuple(history),
        probe_prompt="Remind me what we decided earlier.",
        must_contain=("spare region",),
        must_not_contain=("primary region",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
        hide_peek=True,
    )


def _vault_selector_miss_wrong_plan() -> ResidualCase:
    """Experimental: empty-FTS paraphrase injects an unrelated canary plan."""
    buried = "The canary ships to the spare region before Friday."
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted the ship plan.")]
        + _filler_pairs(8, "wrongplan")
        + _tail()
    )
    return ResidualCase(
        id="vault_selector_miss_wrong_plan",
        template="vault_miss_contamination",
        transcript=tuple(history),
        probe_prompt="When do invoices freeze?",
        must_contain=(),
        must_not_contain=("spare region", "twenty-seven", "27th", "fifteenth"),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=False,
        hide_peek=True,
    )


def _vault_recap_false_fire() -> ResidualCase:
    """Experimental: bare 'remind' routes an unrelated ask through recap_plan."""
    buried = "The canary ships to the spare region before Friday."
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted the ship plan.")]
        + _filler_pairs(8, "falsefire")
        + _tail()
    )
    return ResidualCase(
        id="vault_recap_false_fire",
        template="vault_recap_false_fire",
        transcript=tuple(history),
        probe_prompt="Can you remind the test runner to skip flaky peek?",
        must_contain=("flaky peek",),
        must_not_contain=("spare region",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=False,
        hide_peek=True,
    )


def _unprefixed_obligation() -> ResidualCase:
    """Experimental: policy prose with no Decision:/CONSTRAINT: prefix.

    Not in live_cases(); opt-in via --case. Tests whether obligation-line
    harvest can keep unprefixed write-sink language without a summarizer.
    """
    buried = (
        "please don't write to the live ledger; "
        "the east replica is the only sink."
    )
    history = (
        _base_system()
        + [_msg("user", buried), _msg("assistant", "Noted.")]
        + _filler_pairs(8, "unprefixed")
        + _tail()
    )
    return ResidualCase(
        id="unprefixed_obligation",
        template="unprefixed_obligation",
        transcript=tuple(history),
        probe_prompt=(
            "What write sink is allowed, and what must not be written?"
        ),
        must_contain=("live ledger", "east replica"),
        must_not_contain=("west replica",),
        expected_arms=_arm_meta(),
        catalog_recalls_fact=True,
    )


def _distractor_plus_absent_twin() -> ResidualCase:
    """Holdout: active file, retired twin, and an absent token that must not be invented."""
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
        id="distractor_plus_absent_twin",
        template="distractor_twin",
        transcript=tuple(history),
        probe_prompt="Which auth module is the active current file?",
        must_contain=(current,),
        must_not_contain=(legacy, ABSENT_LIVE_TOKEN),
        expected_arms=_arm_meta(),
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

LIVE_HOLDOUT_CASES: tuple[ResidualCase, ...] = (
    _negative_control_absent_token(),
    _long_horizon_early_constraint(),
    _distractor_plus_absent_twin(),
    _nonce_write_constraint(),
)


def live_cases() -> tuple[ResidualCase, ...]:
    return RESIDUAL_CASES + LIVE_HOLDOUT_CASES


EXPERIMENTAL_CASES: tuple[ResidualCase, ...] = (
    _unprefixed_obligation(),
    _version_pin(),
    _unprefixed_reversal(),
    _stem_cap_later_decision(),
    _vault_only_prose_cutoff(),
    _vault_peek_evicted_cutoff(),
    _vault_narrative_no_overlap(),
    _vault_paraphrase_no_overlap(),
    _vault_false_retrieve_twin(),
    _vault_selector_plausible_filler(),
    _vault_selector_docs_only_plan(),
    _vault_selector_cap_drops_late(),
    _vault_selector_assistant_only(),
    _vault_selector_miss_wrong_plan(),
    _vault_recap_false_fire(),
)


def cases_by_id() -> dict[str, ResidualCase]:
    return {case.id: case for case in live_cases() + EXPERIMENTAL_CASES}
