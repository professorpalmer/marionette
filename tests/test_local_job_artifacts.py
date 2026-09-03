"""Unit tests for honest local-job artifact vocabulary."""

from __future__ import annotations

from harness.local_job_artifacts import (
    READ_ONLY_JOB_ROLES,
    is_read_only_job_role,
    terminal_artifact_type,
)


def test_read_only_job_roles_include_qa_and_analysis():
    for role in (
        "analysis", "review", "explore", "read_only", "readonly",
        "audit", "search", "qa",
    ):
        assert role in READ_ONLY_JOB_ROLES
        assert is_read_only_job_role(role) is True
    assert is_read_only_job_role("QA") is True
    assert is_read_only_job_role(" analysis ") is True
    assert is_read_only_job_role("implement") is False
    assert is_read_only_job_role("") is False
    assert is_read_only_job_role(None) is False


def test_terminal_artifact_type_qa_is_analysis_never_patch():
    assert terminal_artifact_type(
        ok=True, cancelled=False, role="qa", has_file_evidence=True,
    ) == "analysis"
    assert terminal_artifact_type(
        ok=True, cancelled=False, role="implement", has_file_evidence=True,
    ) == "patch"
