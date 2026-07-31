"""Compaction must preserve an artifact's attribution, not just its prose.

``_compact_artifact`` shrinks a Puppetmaster ``Artifact`` so a follow-up driver
turn stays cheap. When it dropped identity along with the bulk, every surfaced
row arrived with no ``execution_ref`` — and the downstream provenance counter
honestly reported ``0/N`` for a run that had in fact produced all N. The fix is
to carry the identity the ``Artifact`` already knows (id, job_id, task_id,
evidence loci, explicit criteria, sanitized execution provenance) and nothing
else: no fabricated spend, no copied payload bulk.
"""

from __future__ import annotations

from harness.swarm_run_facts import evaluate_acceptance_criteria, provenance_counts
from pmharness.bridge import (
    _compact_artifact,
    _promote_degraded_prose,
    _MAX_EVIDENCE_LOCI,
)

JOB = "job-abc123"
TASK = "task-def456"


class _Artifact:
    """The subset of puppetmaster.models.Artifact that compaction reads."""

    def __init__(
        self,
        type="finding",
        payload=None,
        *,
        job_id=JOB,
        task_id=TASK,
        id="artifact-1",
        evidence=None,
        confidence=0.8,
    ):
        self.type = type
        self.payload = payload if payload is not None else {"claim": "a real finding"}
        self.job_id = job_id
        self.task_id = task_id
        self.id = id
        self.evidence = (
            ["harness/router.py:88"] if evidence is None else list(evidence)
        )
        self.confidence = confidence


class TestIdentityIsPreserved:
    def test_job_and_task_ids_travel(self):
        compact = _compact_artifact(_Artifact())
        assert compact["id"] == "artifact-1"
        assert compact["job_id"] == JOB
        assert compact["task_id"] == TASK

    def test_execution_ref_comes_from_the_artifact_itself(self):
        compact = _compact_artifact(_Artifact())
        assert compact["execution_ref"] == {"job_id": JOB, "task_id": TASK}

    def test_a_jobless_artifact_gets_no_forged_ref(self):
        """An unattributed artifact must keep reading as unattributed."""
        compact = _compact_artifact(_Artifact(job_id="", task_id=""))
        assert "execution_ref" not in compact
        assert compact["job_id"] == ""

    def test_ids_are_bounded(self):
        compact = _compact_artifact(_Artifact(id="x" * 5000, job_id="j" * 5000))
        assert len(compact["id"]) <= 128
        assert len(compact["job_id"]) <= 128


class TestEvidenceLoci:
    def test_loci_are_carried_and_the_first_is_the_locus(self):
        compact = _compact_artifact(_Artifact(
            evidence=["harness/router.py:88", "harness/cost.py:12"],
        ))
        assert compact["evidence"] == ["harness/router.py:88", "harness/cost.py:12"]
        assert compact["evidence_locus"] == "harness/router.py:88"

    def test_loci_are_bounded_in_count_and_width(self):
        compact = _compact_artifact(_Artifact(
            evidence=[f"harness/f{i}.py:{i}" for i in range(50)] + ["y" * 5000],
        ))
        assert len(compact["evidence"]) == _MAX_EVIDENCE_LOCI
        assert all(len(locus) <= 240 for locus in compact["evidence"])

    def test_no_evidence_means_no_keys(self):
        compact = _compact_artifact(_Artifact(evidence=[]))
        assert "evidence" not in compact
        assert "evidence_locus" not in compact


class TestAcceptanceCriteria:
    def test_explicit_criteria_travel(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "a real finding",
            "acceptance_criteria": ["pyright is clean", "tsc passes"],
        }))
        assert compact["acceptance_criteria"] == ["pyright is clean", "tsc passes"]

    def test_nothing_is_invented_when_absent(self):
        assert "acceptance_criteria" not in _compact_artifact(_Artifact())

    def test_a_non_list_checklist_is_ignored(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "c", "acceptance_criteria": "pyright is clean",
        }))
        assert "acceptance_criteria" not in compact

    def test_structured_passed_status_dict_travels(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "pyright run is clean",
            "acceptance_criteria": [{
                "criterion": "pyright is clean",
                "status": "passed",
                "evidence": "harness/a.py:1",
            }],
        }))
        row = compact["acceptance_criteria"][0]
        assert isinstance(row, dict)
        assert row["status"] == "passed"
        assert row["evidence"] == "harness/a.py:1"

    def test_unknown_status_dict_is_preserved_not_stringified(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "could not run tsc",
            "acceptance_criteria": [{
                "criterion": "tsc passes",
                "status": "unknown",
                "evidence": "not_reported",
            }],
        }))
        row = compact["acceptance_criteria"][0]
        assert row["status"] == "unknown"
        assert row["evidence"] == "not_reported"

    def test_malformed_dict_rows_are_omitted(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "c",
            "acceptance_criteria": [
                {"status": "passed", "evidence": "harness/a.py:1"},
                "pyright is clean",
            ],
        }))
        assert compact["acceptance_criteria"] == ["pyright is clean"]


class TestSanitizedExecutionProvenance:
    def test_only_identity_and_known_status_fields_survive(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "a real finding",
            "execution_provenance": {
                "adapter": "agentic",
                "model": "anthropic/claude-sonnet",
                "adapter_model_name": "claude-sonnet",
                "router_model_id": "agentic/anthropic/claude-sonnet",
                "usage_known": True,
                "cost_known": False,
                # Everything below is spend / bulk and must not be copied.
                "tokens": 12345,
                "est_cost_usd": 4.21,
                "api_key": "sk-live-secret",
                "raw_response": {"choices": [{"text": "x" * 10000}]},
            },
        }))
        assert compact["execution_provenance"] == {
            "adapter": "agentic",
            "model": "anthropic/claude-sonnet",
            "adapter_model_name": "claude-sonnet",
            "router_model_id": "agentic/anthropic/claude-sonnet",
            "usage_known": True,
            "cost_known": False,
        }

    def test_no_spend_is_fabricated_anywhere_on_the_row(self):
        compact = _compact_artifact(_Artifact())
        for spend_field in ("tokens", "est_cost_usd", "cost_provenance"):
            assert spend_field not in compact
        assert "execution_provenance" not in compact

    def test_a_non_dict_provenance_is_ignored(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "c", "execution_provenance": "agentic",
        }))
        assert "execution_provenance" not in compact

    def test_large_payload_values_are_not_copied_wholesale(self):
        compact = _compact_artifact(_Artifact(payload={
            "claim": "a real finding",
            "stdout": "z" * 20000,
            "files": [f"f{i}.py" for i in range(500)],
        }))
        assert "files" not in compact
        assert len(compact["headline"]) <= 240


class TestFreshResultReportsFullProvenance:
    """The end the P0 blocker was really about: a real run must not read 0/N."""

    def test_compacted_artifacts_report_n_over_n(self):
        compact = [
            _compact_artifact(_Artifact(type="finding", id="artifact-1")),
            _compact_artifact(_Artifact(type="risk", id="artifact-2", payload={
                "risk": "cache discount unapplied", "mitigation": "apply it",
            })),
            _compact_artifact(_Artifact(type="routing", id="artifact-3", payload={
                "model_id": "m", "adapter": "agentic", "policy": "balanced",
            })),
        ]
        # Routing rows stay outside the denominator; both signal rows are direct.
        assert provenance_counts(compact, JOB) == (2, 2)

    def test_a_foreign_job_id_still_reports_zero(self):
        compact = [_compact_artifact(_Artifact(job_id="job-older"))]
        assert provenance_counts(compact, JOB) == (0, 1)

    def test_compacted_criteria_can_settle_a_criterion(self):
        compact = [_compact_artifact(_Artifact(payload={
            "claim": "pyright run is clean",
            "acceptance_criteria": [{
                "criterion": "pyright is clean",
                "status": "passed",
                "evidence": "harness/router.py:88",
            }],
        }))]
        facts = evaluate_acceptance_criteria(["pyright is clean"], compact, JOB)
        assert facts[0].status == "verified"
        assert "harness/router.py:88" in facts[0].basis


class TestPromotedProseInheritsAttribution:
    def test_a_promoted_finding_is_attributed_to_this_job(self):
        prose = (
            "harness/server.py:2834 bills cached tokens at full price; apply the "
            "cache discount so multi-turn cost is accurate."
        )
        compact = [_compact_artifact(_Artifact(
            type="verification", payload={"stdout": prose}, id="artifact-9",
        ))]
        promoted = [a for a in _promote_degraded_prose(compact)
                    if a.get("promoted_from") == "verification"]

        assert promoted, "prose analysis must still be promoted"
        assert promoted[0]["execution_ref"] == {"job_id": JOB, "task_id": TASK}
        assert promoted[0]["id"] == "artifact-9:promoted"
        assert provenance_counts(promoted, JOB) == (1, 1)
