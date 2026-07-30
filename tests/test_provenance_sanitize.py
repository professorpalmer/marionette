"""Machine-authoritative clean-tree claim sanitization."""
from __future__ import annotations

from harness.provenance_sanitize import (
    CLEAN_TREE_REPLACEMENT,
    sanitize_clean_tree_claims,
)
from pmharness.bridge import _analysis_instruction


def test_sanitize_replaces_clean_claims_when_live_dirty():
    text = (
        "Validated the helper. The working tree is clean and the repository "
        "is clean. Finding: validate_pitcher has no tests."
    )
    out = sanitize_clean_tree_claims(
        text, live_dirty_paths_before=["AGENTS.md", ".cursor/rules/x.mdc"],
    )
    assert CLEAN_TREE_REPLACEMENT in out
    assert "working tree is clean" not in out.lower()
    assert "repository is clean" not in out.lower()
    assert "validate_pitcher has no tests" in out


def test_sanitize_noop_when_no_preexisting_dirty():
    text = "The working tree is clean after our empty analysis."
    assert sanitize_clean_tree_claims(text, live_dirty_paths_before=[]) == text


def test_sanitize_preserves_cleaned_cleaner_cleanliness_words():
    """Stem lookalikes must not false-positive when neutralizing clean claims."""
    text = (
        "We cleaned the cache, picked a cleaner approach, and improved "
        "cleanliness of the module. Finding: helper lacks coverage."
    )
    out = sanitize_clean_tree_claims(
        text, live_dirty_paths_before=["AGENTS.md"],
    )
    assert out == text
    assert "cleaned" in out
    assert "cleaner" in out
    assert "cleanliness" in out
    assert CLEAN_TREE_REPLACEMENT not in out


def test_sanitize_still_neutralizes_full_clean_tree_claims():
    """Golden: real clean-tree assertions are replaced; findings stay."""
    text = (
        "Audit done. The working tree is clean. We cleaned logs with a "
        "cleaner script for cleanliness. The repository is clean."
    )
    out = sanitize_clean_tree_claims(
        text, live_dirty_paths_before=["tracked.txt"],
    )
    assert "working tree is clean" not in out.lower()
    assert "repository is clean" not in out.lower()
    assert CLEAN_TREE_REPLACEMENT in out
    # Stem lookalikes in the same paragraph must survive.
    assert "cleaned" in out
    assert "cleaner" in out
    assert "cleanliness" in out


def test_finish_sanitizes_summary_and_findings(tmp_path):
    import threading

    from harness.local_jobs import LocalJobsMixin

    class _Host(LocalJobsMixin):
        def __init__(self):
            self._local_jobs = {}
            self._local_jobs_lock = threading.RLock()
            self._local_job_cancels = {}
            self._local_jobs_path = str(tmp_path / "jobs.json")
            self.harness_session_id = "s"
            self.config = type("C", (), {"repo": str(tmp_path), "driver": "d"})()

    host = _Host()
    host._register_local_job(
        "local-clean", "audit", role="analysis", engine="native",
    )
    host._finish_local_job(
        "local-clean",
        ok=True,
        summary="The working tree is clean. Real finding remains.",
        engine="native",
        model="stub",
        worker_provenance={
            "live_dirty_paths_before": ["tracked.txt"],
            "live_dirty_paths_after": ["tracked.txt"],
            "managed_worktree_mode": "managed",
            "worktree_diff_empty": True,
        },
        findings=[
            {
                "type": "finding",
                "headline": "The repository is clean but keys.py ignores legacy",
            },
        ],
    )
    job = host._local_jobs["local-clean"]
    terminal = next(a for a in job["artifacts"] if a["type"] == "analysis")
    assert CLEAN_TREE_REPLACEMENT in terminal["headline"]
    finding = next(a for a in job["artifacts"] if a["type"] == "finding")
    assert CLEAN_TREE_REPLACEMENT in finding["headline"]
    assert "keys.py ignores legacy" in finding["headline"]
    assert "tokens" not in finding
    assert "est_cost_usd" not in finding
    assert finding["execution_ref"]["job_id"] == "local-clean"
    assert finding["execution_ref"]["terminal_artifact_id"] == "local-clean-result"


def test_analysis_instruction_points_at_marionette_envelope():
    instruction = _analysis_instruction("audit the code", "/tmp/repo", "explore")
    assert "Marionette job envelope" in instruction
    assert "not from repository source" in instruction
    assert "disposable managed worker worktree" in instruction
