"""The current-run evidence contract that replaced the prose-only boundary.

Prose ("trust only this job") is unfalsifiable; these tests pin the facts a
reader can check instead: which build ran, which subject it read, what it
returned, how much of that traces back to THIS job, which acceptance criteria
are settled, and which optional prerequisites went unproven.

Two rules are easy to regress and are asserted head-on:

* provenance never launders itself — normalization stamps run identity and the
  evidence locus but leaves parent attribution alone, so ``0/M`` stays ``0/M``;
* an unavailable optional prerequisite is a readiness gap with a remedy, never
  a finding, risk, or harness defect.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from harness.pilot import PilotAction
from harness.repo_resolve import resolve_effective_repo
from harness.send_loop_dispatch import dispatch_swarm_action
from harness.swarm_run_facts import (
    CLASSIFICATION_POLICY,
    CLASSIFICATION_UNAVAILABLE,
    NOT_VERIFIED,
    VERIFIED,
    attribute_stored_execution_refs,
    build_swarm_run_facts,
    clear_probe_cache,
    digest_line,
    evaluate_acceptance_criteria,
    first_evidence_locus,
    normalize_execution_refs,
    provenance_counts,
    render_evidence_boundary,
)

CURRENT_JOB = "job-current"

_EXPLICIT_SUBJECT_REPO = "/repo/subject"
_RESOLVED_SUBJECT_REPO = resolve_effective_repo(_EXPLICIT_SUBJECT_REPO)


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    clear_probe_cache()
    yield
    clear_probe_cache()


@pytest.fixture
def stub_probe(monkeypatch):
    """Deterministic environment payload; tests opt into the shape they need."""
    def _install(**overrides):
        payload = {
            "tool_paths": {"pyright": "~/n/pyright", "tsc": "~/n/tsc"},
            "browser_path": "/Applications/Chrome",
            "puppetmaster_version": "1.21.5",
            "mcp_server_names": ["discord-mcp", "puppetmaster"],
        }
        payload.update(overrides)
        monkeypatch.setattr(
            "harness.environment_fingerprint.compute_environment_fingerprint",
            lambda cwd, strict=True: (dict(payload), ""),
        )
        return payload
    return _install


def _facts(stub_probe, **kwargs):
    stub_probe()
    defaults = dict(
        job_id=CURRENT_JOB,
        subject_cwd="/subject/marionette",
        state_root="/state/root",
        artifacts=[],
        acceptance_criteria=[],
    )
    defaults.update(kwargs)
    return build_swarm_run_facts(**defaults)


class TestEvidenceLocus:
    def test_locus_is_the_first_cited_path(self):
        assert first_evidence_locus({
            "headline": "router drops alternatives",
            "body": "See harness/router.py:88 then harness/other.py:12",
        }) == "harness/router.py:88"

    def test_locus_prefers_an_explicit_field(self):
        assert first_evidence_locus({
            "evidence_locus": "webapp/src/lib/api.ts:4",
            "body": "harness/router.py:88",
        }) == "webapp/src/lib/api.ts:4"

    def test_locus_is_empty_without_a_path_reference(self):
        assert first_evidence_locus({"headline": "the audit went well"}) == ""

    def test_structural_evidence_beats_scraping_prose(self):
        """Bridge-compacted rows carry the worker's own loci; trust those first."""
        assert first_evidence_locus({
            "evidence": ["harness/router.py:88"],
            "body": "compare against harness/other.py:12",
        }) == "harness/router.py:88"

    def test_non_path_evidence_tags_are_not_reported_as_loci(self):
        assert first_evidence_locus({
            "evidence": ["adapter:agentic", "model:None", "no_model"],
        }) == ""

    def test_dotted_symbols_are_not_reported_as_loci(self):
        assert first_evidence_locus({
            "evidence": ["datetime.utcnow", "os.path.join"],
            "body": "called datetime.utcnow during normalization",
        }) == ""

    def test_bare_source_filenames_still_count_as_loci(self):
        assert first_evidence_locus({
            "headline": "see config.json and router.py:12 for details",
        }) == "config.json"


class TestNormalization:
    def test_stamps_run_identity_and_locus(self):
        rows = normalize_execution_refs(
            [{"type": "finding", "headline": "harness/a.py:2 leaks"}], CURRENT_JOB,
        )
        assert rows[0]["execution_ref"]["run_job_id"] == CURRENT_JOB
        assert rows[0]["evidence_locus"] == "harness/a.py:2"

    def test_never_forges_parent_attribution(self):
        rows = normalize_execution_refs(
            [{"type": "finding", "headline": "harness/a.py:2 leaks"}], CURRENT_JOB,
        )
        assert rows[0]["execution_ref"]["job_id"] == ""
        assert provenance_counts(rows, CURRENT_JOB) == (0, 1)

    def test_preserves_a_foreign_parent(self):
        rows = normalize_execution_refs(
            [{"type": "finding", "execution_ref": {"job_id": "job-older"}}],
            CURRENT_JOB,
        )
        assert rows[0]["execution_ref"]["job_id"] == "job-older"
        assert provenance_counts(rows, CURRENT_JOB) == (0, 1)

    def test_does_not_mutate_the_input(self):
        original = {"type": "finding", "headline": "harness/a.py:2"}
        normalize_execution_refs([original], CURRENT_JOB)
        assert original == {"type": "finding", "headline": "harness/a.py:2"}

    def test_routing_rows_are_outside_the_provenance_denominator(self):
        rows = normalize_execution_refs([
            {"type": "routing", "headline": "picked a model"},
            {"type": "finding", "execution_ref": {"job_id": CURRENT_JOB}},
        ], CURRENT_JOB)
        assert provenance_counts(rows, CURRENT_JOB) == (1, 1)

    def test_trusted_store_row_restores_exact_current_attribution(self):
        rows = attribute_stored_execution_refs(
            [{"type": "finding", "job_id": CURRENT_JOB, "task_id": "task-1"}],
            CURRENT_JOB,
        )
        assert rows[0]["execution_ref"] == {
            "job_id": CURRENT_JOB,
            "task_id": "task-1",
        }

    def test_trusted_store_row_never_adopts_foreign_attribution(self):
        rows = attribute_stored_execution_refs(
            [{"type": "finding", "job_id": "job-older"}],
            CURRENT_JOB,
        )
        assert "execution_ref" not in rows[0]


class TestDigestLines:
    def test_line_names_job_locus_and_direct_provenance(self):
        line = digest_line({
            "type": "finding",
            "headline": "harness/router.py:88 drops alternatives",
            "execution_ref": {"job_id": CURRENT_JOB},
        }, CURRENT_JOB)
        assert "[finding]" in line
        assert f"job={CURRENT_JOB}" in line
        assert "locus=harness/router.py:88" in line
        assert "provenance=direct" in line

    def test_unstamped_and_foreign_rows_say_so(self):
        unstamped = digest_line({"type": "finding", "headline": "x"}, CURRENT_JOB)
        assert "provenance=unstamped" in unstamped
        assert "locus=none" in unstamped
        foreign = digest_line(
            {"type": "finding", "headline": "x", "execution_ref": {"job_id": "job-old"}},
            CURRENT_JOB,
        )
        assert "provenance=foreign:job-old" in foreign


class TestAcceptanceCriteria:
    """Only an artifact ATTRIBUTED to this job may settle a criterion.

    "It came back in this batch" is not evidence that this run produced it, so
    the citing row must carry ``execution_ref.job_id == job_id``. Without that
    rule a recycled or unattributed artifact could mark a criterion verified —
    exactly the laundering the provenance counters exist to prevent.
    """

    def test_criterion_cited_by_a_current_job_artifact_is_verified(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "headline": "checked harness/a.py:1",
                "acceptance_criteria": [{
                    "criterion": "pyright is clean",
                    "status": "passed",
                    "evidence": "harness/a.py:1",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert [f.status for f in facts] == [VERIFIED]
        assert "current-job verification" in facts[0].basis

    def test_bare_string_with_locus_does_not_verify(self):
        """Checklist echoes must not settle via an unrelated artifact locus."""
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{
                "type": "finding",
                "headline": "checked harness/a.py:1",
                "acceptance_criteria": ["tsc passes"],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_string_citation_without_a_locus_is_not_verified(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{
                "type": "finding",
                "acceptance_criteria": ["tsc passes"],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_passed_status_dict_with_evidence_verifies(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "acceptance_criteria": [{
                    "criterion": "pyright is clean",
                    "status": "passed",
                    "evidence": "harness/a.py:1",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == VERIFIED
        assert " at harness/a.py:1" in facts[0].basis

    def test_passed_record_renders_its_own_locus_not_artifact_tags(self):
        facts = evaluate_acceptance_criteria(
            ["example test passes"],
            [{
                "type": "verification",
                "evidence": ["datetime.utcnow"],
                "acceptance_criteria": [{
                    "criterion": "example test passes",
                    "status": "passed",
                    "evidence": "tests/example.py:12",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == VERIFIED
        assert " at tests/example.py:12" in facts[0].basis
        assert "datetime.utcnow" not in facts[0].basis

    @pytest.mark.parametrize(
        "record",
        [
            {"criterion": "pyright is clean", "status": "passed"},
            {
                "criterion": "pyright is clean",
                "status": "passed",
                "evidence": "not_reported",
            },
            {
                "criterion": "pyright is clean",
                "status": "passed",
                "evidence": "datetime.utcnow",
            },
        ],
        ids=["missing", "not_reported", "non_path"],
    )
    def test_passed_record_without_same_record_path_stays_not_verified(
        self, record,
    ):
        """Parent artifact evidence must not verify a structured passed row."""
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "evidence": ["harness/unrelated.py:99"],
                "evidence_locus": "harness/unrelated.py:99",
                "headline": "checked harness/unrelated.py:99",
                "acceptance_criteria": [record],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED
        assert "harness/unrelated.py" not in facts[0].basis

    def test_malformed_non_path_evidence_stays_not_verified(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "acceptance_criteria": [{
                    "criterion": "pyright is clean",
                    "status": "passed",
                    "evidence": "datetime.utcnow",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_unknown_status_dict_does_not_verify(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{
                "type": "verification",
                "acceptance_criteria": [{
                    "criterion": "tsc passes",
                    "status": "unknown",
                    "evidence": "not_reported",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_failed_status_dict_does_not_verify(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "acceptance_criteria": [{
                    "criterion": "pyright is clean",
                    "status": "failed",
                    "evidence": "harness/a.py:1",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_malformed_dict_does_not_verify(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "acceptance_criteria": [{"status": "passed", "evidence": "x"}],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_foreign_job_structured_record_does_not_verify(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{
                "type": "finding",
                "acceptance_criteria": [{
                    "criterion": "tsc passes",
                    "status": "passed",
                    "evidence": "harness/a.py:1",
                }],
                "execution_ref": {"job_id": "job-older"},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_failed_prompt_repetition_cannot_verify_every_criterion(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean", "tsc passes"],
            [{
                "type": "verification",
                "headline": "Run the audit. Acceptance criteria: pyright is clean; tsc passes.",
                "body": "Run the audit. Acceptance criteria: pyright is clean; tsc passes.",
                "failure": "no_model",
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert {fact.status for fact in facts} == {NOT_VERIFIED}

    def test_current_prose_without_explicit_mapping_is_not_proof(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "body": "Pyright is clean across the package.",
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_unstamped_artifact_cannot_verify_a_criterion(self):
        """No parent attribution means no proof this run produced the check."""
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "headline": "checked harness/a.py:1",
                "body": "Pyright is clean across the package.",
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED
        assert CURRENT_JOB in facts[0].basis

    def test_run_job_id_alone_cannot_verify_a_criterion(self):
        """``run_job_id`` records who surfaced the row, not who produced it."""
        rows = normalize_execution_refs(
            [{"type": "verification", "body": "Pyright is clean across the package."}],
            CURRENT_JOB,
        )
        assert rows[0]["execution_ref"]["run_job_id"] == CURRENT_JOB
        facts = evaluate_acceptance_criteria(["pyright is clean"], rows, CURRENT_JOB)
        assert facts[0].status == NOT_VERIFIED

    def test_foreign_job_artifact_cannot_verify_a_criterion(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "body": "Pyright is clean across the package.",
                "execution_ref": {"job_id": "job-older"},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED
        assert CURRENT_JOB in facts[0].basis

    def test_foreign_structural_citation_is_also_rejected(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{
                "type": "finding",
                "acceptance_criteria": ["tsc passes"],
                "execution_ref": {"job_id": "job-older"},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_an_unknown_current_job_verifies_nothing(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{"type": "finding", "acceptance_criteria": ["tsc passes"],
              "execution_ref": {"job_id": ""}}],
            "",
        )
        assert facts[0].status == NOT_VERIFIED

    def test_uncited_criterion_is_not_verified_never_a_defect(self):
        facts = evaluate_acceptance_criteria(
            ["vitest suite is green"],
            [{
                "type": "finding",
                "headline": "something else entirely",
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED
        assert CURRENT_JOB in facts[0].basis

    def test_zero_criteria_yields_no_rows(self):
        assert evaluate_acceptance_criteria([], [{"type": "finding"}], CURRENT_JOB) == ()

    def test_mixed_string_and_unknown_structured_record_stays_not_verified(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{
                "type": "verification",
                "headline": "checked harness/a.py:1",
                "acceptance_criteria": [
                    {
                        "criterion": "tsc passes",
                        "status": "unknown",
                        "evidence": "not_reported",
                    },
                    "tsc passes",
                ],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_mixed_string_and_failed_structured_record_stays_not_verified(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "headline": "checked harness/a.py:1",
                "acceptance_criteria": [
                    {
                        "criterion": "pyright is clean",
                        "status": "failed",
                        "evidence": "harness/a.py:1",
                    },
                    "pyright is clean",
                ],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_full_checklist_string_echo_cannot_green_every_criterion(self):
        criteria = ["pyright is clean", "tsc passes", "vitest suite is green"]
        facts = evaluate_acceptance_criteria(
            criteria,
            [{
                "type": "finding",
                "headline": "unrelated harness/router.py:88 observation",
                "acceptance_criteria": list(criteria),
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert {fact.status for fact in facts} == {NOT_VERIFIED}

    def test_structured_passed_record_with_evidence_still_verifies(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [{
                "type": "verification",
                "acceptance_criteria": [{
                    "criterion": "pyright is clean",
                    "status": "verified",
                    "evidence": "harness/a.py:1",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == VERIFIED
        assert " at harness/a.py:1" in facts[0].basis


def _honest_provenance(**overrides):
    stamp = {
        "adapter": "agentic",
        "model": "anthropic/claude-sonnet",
        "usage_known": False,
        "cost_known": False,
    }
    stamp.update(overrides)
    return stamp


class TestProvenanceSelfReportCriteria:
    """Model-id / execution_provenance rows settle from stamps, not citations.

    The anti-false-green citation rule still owns check criteria (pyright,
    tsc). This class pins the narrow second path: already-stamped
    ``execution_provenance`` on current-job non-routing artifacts.
    """

    _CRITERION = (
        "Every non-routing artifact carries full provenance "
        "(model id, usage_known:false, cost_known:false — no fake zeros)"
    )

    def _row(self, **overrides):
        row = {
            "type": "finding",
            "headline": "something else entirely",
            "execution_ref": {"job_id": CURRENT_JOB},
            "execution_provenance": _honest_provenance(),
        }
        row.update(overrides)
        return row

    def test_stamped_provenance_verifies_without_a_worker_citation(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [self._row(), self._row(type="risk", headline="another signal")],
            CURRENT_JOB,
        )
        assert [f.status for f in facts] == [VERIFIED]
        assert "stamped execution_provenance" in facts[0].basis
        assert "2 current-job" in facts[0].basis

    def test_routing_rows_are_outside_the_denominator(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [
                self._row(),
                {
                    "type": "routing",
                    "execution_ref": {"job_id": CURRENT_JOB},
                },
            ],
            CURRENT_JOB,
        )
        assert facts[0].status == VERIFIED
        assert "1 current-job" in facts[0].basis

    def test_check_criteria_still_require_a_structured_citation(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean"],
            [self._row()],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED
        assert "stamped execution_provenance" not in facts[0].basis

    def test_prose_echo_without_stamps_does_not_verify(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [{
                "type": "finding",
                "headline": self._CRITERION,
                "body": "model id usage_known cost_known no fake zeros",
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_missing_model_stays_not_verified(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [self._row(execution_provenance=_honest_provenance(model=""))],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_missing_known_flags_stays_not_verified(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [self._row(execution_provenance={
                "model": "anthropic/claude-sonnet",
            })],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_copied_spend_zeros_stay_not_verified(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [self._row(execution_provenance=_honest_provenance(
                tokens=0, est_cost_usd=0.0,
            ))],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_one_unstamped_sibling_blocks_the_row(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [
                self._row(),
                {
                    "type": "finding",
                    "execution_ref": {"job_id": CURRENT_JOB},
                },
            ],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_foreign_job_stamps_do_not_count(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION],
            [self._row(execution_ref={"job_id": "job-older"})],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_mixed_list_verifies_only_the_provenance_row(self):
        facts = evaluate_acceptance_criteria(
            [self._CRITERION, "pyright is clean"],
            [self._row()],
            CURRENT_JOB,
        )
        assert [f.status for f in facts] == [VERIFIED, NOT_VERIFIED]

    @pytest.mark.parametrize(
        "criterion",
        [
            "state your model",
            "state the model identifier on each finding",
            "every artifact carries full provenance",
            "all artifacts report their model",
            "each finding is stamped with provenance",
            "no invented zeros on child artifacts",
            "usage known and cost known on every row",
        ],
        ids=[
            "state_your_model",
            "model_identifier",
            "every_artifact_provenance",
            "all_artifacts_model",
            "each_finding_stamped",
            "no_invented_zeros",
            "usage_known_words",
        ],
    )
    def test_alternate_wordings_still_settle_from_stamps(self, criterion):
        facts = evaluate_acceptance_criteria(
            [criterion],
            [self._row()],
            CURRENT_JOB,
        )
        assert facts[0].status == VERIFIED
        assert "stamped execution_provenance" in facts[0].basis

    @pytest.mark.parametrize(
        "criterion",
        [
            "document the provenance of the cache bug",
            "the model router chose composer",
            "fix the model registry",
            "every artifact has a timestamp",
            "tsc passes",
        ],
        ids=[
            "bug_provenance",
            "router_chose",
            "model_registry",
            "timestamp",
            "tsc",
        ],
    )
    def test_unrelated_wordings_stay_citation_only(self, criterion):
        facts = evaluate_acceptance_criteria(
            [criterion],
            [self._row()],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED
        assert "stamped execution_provenance" not in facts[0].basis


class TestRunFacts:
    def test_surfaces_the_current_build_and_subject(self, stub_probe):
        facts = _facts(stub_probe)
        from harness import __version__

        assert facts.marionette_version == __version__
        assert facts.puppetmaster_version == "1.21.5"
        assert facts.state_root == "/state/root"
        assert facts.subject_cwd == "/subject/marionette"
        assert facts.job_id == CURRENT_JOB

    def test_is_deterministic_across_calls(self, stub_probe):
        artifacts = normalize_execution_refs([
            {"type": "finding", "headline": "harness/a.py:1 leaks",
             "execution_ref": {"job_id": CURRENT_JOB}},
            {"type": "routing", "headline": "route"},
        ], CURRENT_JOB)
        first = _facts(stub_probe, artifacts=artifacts, acceptance_criteria=["x"])
        second = _facts(stub_probe, artifacts=artifacts, acceptance_criteria=["x"])
        assert first.as_dict() == second.as_dict()
        assert render_evidence_boundary(first) == render_evidence_boundary(second)

    def test_counts_describe_the_returned_artifacts(self, stub_probe):
        artifacts = normalize_execution_refs([
            {"type": "finding", "execution_ref": {"job_id": CURRENT_JOB}},
            {"type": "finding"},
            {"type": "routing"},
        ], CURRENT_JOB)
        facts = _facts(stub_probe, artifacts=artifacts)
        assert facts.artifact_total == 3
        assert facts.artifact_type_counts == {"finding": 2, "routing": 1}
        assert (facts.direct_provenance_total, facts.non_routing_total) == (1, 2)

    def test_reports_mcp_servers_by_name_only(self, stub_probe):
        facts = _facts(stub_probe)
        text = render_evidence_boundary(facts)
        assert facts.mcp_server_names == ("discord-mcp", "puppetmaster")
        assert "discord-mcp" in text

    def test_never_renders_secrets_or_raw_mcp_config(self, stub_probe):
        stub_probe(
            mcp_server_names=["discord-mcp"],
            mcp_digests=["deadbeef"],
        )
        facts = build_swarm_run_facts(
            job_id=CURRENT_JOB,
            subject_cwd="/subject",
            state_root="/state",
            artifacts=[],
        )
        text = render_evidence_boundary(facts)
        for secret in ("sk-live-", "Bearer", "DISCORD_TOKEN", "--api-key", "deadbeef"):
            assert secret not in text
        assert "mcpServers" not in text

    def test_probe_failure_degrades_to_not_verified(self, monkeypatch):
        monkeypatch.setattr(
            "harness.environment_fingerprint.compute_environment_fingerprint",
            lambda cwd, strict=True: (None, "environment_probe_failed:OSError"),
        )
        facts = build_swarm_run_facts(
            job_id=CURRENT_JOB, subject_cwd="/subject", artifacts=[],
        )
        assert facts.probe_error == "environment_probe_failed:OSError"
        assert {fact.status for fact in facts.readiness} == {NOT_VERIFIED}
        assert all(fact.remedy for fact in facts.readiness)


class TestOptionalReadiness:
    def test_available_tools_are_verified(self, stub_probe):
        facts = _facts(stub_probe)
        by_name = {fact.name: fact for fact in facts.readiness}
        assert by_name["pyright"].status == VERIFIED
        assert by_name["tsc"].status == VERIFIED
        assert by_name["browser"].status == VERIFIED

    def test_missing_tools_are_unproven_with_a_remedy(self, stub_probe):
        stub_probe(browser_path="", tool_paths={"pyright": "", "tsc": ""})
        facts = build_swarm_run_facts(
            job_id=CURRENT_JOB, subject_cwd="/subject", artifacts=[],
        )
        by_name = {fact.name: fact for fact in facts.readiness}
        for name in ("browser", "pyright", "tsc"):
            assert by_name[name].status == NOT_VERIFIED
            assert by_name[name].classification == CLASSIFICATION_UNAVAILABLE
            assert by_name[name].remedy

        text = render_evidence_boundary(facts)
        assert "[not_verified] browser" in text
        assert "remedy:" in text
        # Readiness must never read as product defect vocabulary.
        assert "[finding]" not in text
        assert "[risk]" not in text
        assert "defect" in text and "never as a product finding" in text

    def test_localhost_browsing_is_policy_not_a_defect(self, monkeypatch, stub_probe):
        monkeypatch.delenv("HARNESS_ALLOW_PRIVATE_URLS", raising=False)
        facts = _facts(stub_probe)
        policy = {fact.name: fact for fact in facts.readiness}["browser_localhost_policy"]
        assert policy.classification == CLASSIFICATION_POLICY
        assert policy.status == NOT_VERIFIED
        assert policy.remedy

    def test_enabled_private_url_policy_is_verified(self, monkeypatch, stub_probe):
        monkeypatch.setenv("HARNESS_ALLOW_PRIVATE_URLS", "1")
        facts = _facts(stub_probe)
        policy = {fact.name: fact for fact in facts.readiness}["browser_localhost_policy"]
        assert policy.status == VERIFIED
        assert policy.classification == CLASSIFICATION_POLICY


class TestRenderedBoundary:
    def test_reports_zero_provenance_explicitly(self, stub_probe):
        artifacts = normalize_execution_refs(
            [{"type": "finding", "headline": "x"}, {"type": "finding", "headline": "y"}],
            CURRENT_JOB,
        )
        text = render_evidence_boundary(_facts(stub_probe, artifacts=artifacts))
        assert "Direct execution provenance: 0/2 non-routing artifacts." in text

    def test_states_the_trust_rules_and_subject(self, stub_probe):
        text = render_evidence_boundary(_facts(
            stub_probe, acceptance_criteria=["pyright is clean"],
        ))
        assert f"Exact current job id: {CURRENT_JOB}" in text
        assert "Subject cwd (read-only audit target): /subject/marionette" in text
        assert "historical/untrusted" in text
        assert "[not_verified] pyright is clean" in text

    def test_no_criteria_says_so_instead_of_inventing_them(self, stub_probe):
        text = render_evidence_boundary(_facts(stub_probe))
        assert "Acceptance criteria: none supplied (do not invent any)" in text


class TestSynchronousBoundary:
    """The digest the pilot receives when a swarm runs inline."""

    def _dispatch(self, monkeypatch, artifacts, criteria=None):
        import harness.send_loop_dispatch as dispatch

        result = SimpleNamespace(
            job_id=CURRENT_JOB,
            adapter="agentic",
            mode="swarm",
            num_artifacts=len(artifacts),
            artifact_types=sorted({a["type"] for a in artifacts}),
            artifacts=artifacts,
            auth_failure="",
            summary="done",
        )
        monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
        monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
        monkeypatch.setattr(
            dispatch, "stream_swarm",
            lambda session, intent, q, *_a: q.put(("done", result)),
        )
        session = SimpleNamespace(
            config=SimpleNamespace(repo=_EXPLICIT_SUBJECT_REPO),
            state_dir="/state/root",
            _session_job_ids=[],
            _register_local_job=MagicMock(),
            _finish_local_job=MagicMock(),
            _append_action_result=MagicMock(),
            _display_transcript=[],
        )
        list(dispatch_swarm_action(
            session,
            PilotAction(
                kind="run_swarm", goal="audit routing",
                acceptance_criteria=list(criteria or []),
                arguments={},
            ),
            "a-1", True,
            counters={"swarms": 0, "demo_swarms": 0},
            turn_findings=[],
        ))
        return session._append_action_result.call_args.args[2]

    def test_digest_carries_run_facts_and_per_line_identity(self, monkeypatch, stub_probe):
        stub_probe()
        text = self._dispatch(monkeypatch, [{
            "type": "finding",
            "headline": "harness/router.py:88 drops the rejected alternative",
            "body": "The receipt is written after the alternatives are discarded.",
            "execution_ref": {"job_id": CURRENT_JOB},
        }])

        assert f"Exact current job id: {CURRENT_JOB}" in text
        assert f"Subject cwd (read-only audit target): {_RESOLVED_SUBJECT_REPO}" in text
        assert "Resolved state root: /state/root" in text
        assert "Direct execution provenance: 1/1 non-routing artifacts." in text
        assert f"job={CURRENT_JOB}, locus=harness/router.py:88, provenance=direct" in text

    def test_a_real_bridge_result_reports_full_provenance(
        self, monkeypatch, stub_probe,
    ):
        """The regression this closes: a genuine fresh run reading as 0/N.

        These rows are shaped by ``pmharness.bridge._compact_artifact``, which
        now carries the Artifact's own job/task ids. Before that, compaction
        dropped ``execution_ref`` and every real swarm reported zero direct
        provenance for work it had just performed.
        """
        stub_probe()
        text = self._dispatch(monkeypatch, [
            {
                "type": "finding",
                "headline": "harness/router.py:88 drops the rejected alternative",
                "body": "The receipt is written after the alternatives are discarded.",
                "id": "artifact-1",
                "job_id": CURRENT_JOB,
                "task_id": "task-1",
                "evidence": ["harness/router.py:88"],
                "execution_ref": {"job_id": CURRENT_JOB, "task_id": "task-1"},
            },
            {
                "type": "risk",
                "headline": "harness/cost.py:12 bills cached tokens at full price",
                "id": "artifact-2",
                "job_id": CURRENT_JOB,
                "task_id": "task-1",
                "evidence": ["harness/cost.py:12"],
                "execution_ref": {"job_id": CURRENT_JOB, "task_id": "task-1"},
            },
        ])
        assert "Direct execution provenance: 2/2 non-routing artifacts." in text
        assert "provenance=direct" in text
        assert "provenance=unstamped" not in text

    def test_zero_provenance_is_still_reported(self, monkeypatch, stub_probe):
        stub_probe()
        text = self._dispatch(monkeypatch, [{
            "type": "finding",
            "headline": "harness/router.py:88 drops the rejected alternative",
            "body": "The receipt is written after the alternatives are discarded.",
        }])
        assert "Direct execution provenance: 0/1 non-routing artifacts." in text

    def test_explicit_criteria_are_echoed_with_status(self, monkeypatch, stub_probe):
        stub_probe()
        text = self._dispatch(
            monkeypatch,
            [{
                "type": "finding",
                "headline": "harness/router.py:88 drops the rejected alternative",
                "body": "The receipt is written after the alternatives are discarded.",
            }],
            criteria=["provenance chips cite a real baseline"],
        )
        assert "[not_verified] provenance chips cite a real baseline" in text


class TestDuplicateContradictoryAndBoundedEvidence:
    def test_duplicate_criteria_are_deduped_not_double_settled(self):
        facts = evaluate_acceptance_criteria(
            ["pyright is clean", "pyright is clean", "  pyright is clean  "],
            [{
                "type": "verification",
                "headline": "checked harness/a.py:1",
                "acceptance_criteria": [{
                    "criterion": "pyright is clean",
                    "status": "passed",
                    "evidence": "harness/a.py:1",
                }],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert len(facts) == 1
        assert facts[0].status == VERIFIED

    def test_contradictory_pass_and_fail_refuse_verification(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [{
                "type": "verification",
                "headline": "checked harness/a.py:1",
                "acceptance_criteria": [
                    {
                        "criterion": "tsc passes",
                        "status": "passed",
                        "evidence": "harness/a.py:1",
                    },
                    {
                        "criterion": "tsc passes",
                        "status": "failed",
                        "evidence": "harness/a.py:2",
                    },
                ],
                "execution_ref": {"job_id": CURRENT_JOB},
            }],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED

    def test_cross_artifact_contradiction_fails_closed(self):
        facts = evaluate_acceptance_criteria(
            ["tsc passes"],
            [
                {
                    "type": "verification",
                    "headline": "checked harness/a.py:1",
                    "acceptance_criteria": [{
                        "criterion": "tsc passes",
                        "status": "passed",
                        "evidence": "harness/a.py:1",
                    }],
                    "execution_ref": {"job_id": CURRENT_JOB},
                },
                {
                    "type": "verification",
                    "headline": "checked harness/b.py:1",
                    "acceptance_criteria": [{
                        "criterion": "tsc passes",
                        "status": "failed",
                        "evidence": "harness/b.py:1",
                    }],
                    "execution_ref": {"job_id": CURRENT_JOB},
                },
            ],
            CURRENT_JOB,
        )
        assert facts[0].status == NOT_VERIFIED
        assert "contradictory" in facts[0].basis

    def test_oversized_evidence_collections_are_bounded(self):
        from harness.swarm_run_facts import _MAX_EVIDENCE_ENTRIES

        huge = [f"harness/mod_{i}.py:{i}" for i in range(_MAX_EVIDENCE_ENTRIES + 40)]
        rows = normalize_execution_refs(
            [{"type": "finding", "headline": "x", "evidence": huge}],
            CURRENT_JOB,
        )
        assert len(rows) == 1
        assert len(rows[0]["evidence"]) == _MAX_EVIDENCE_ENTRIES
        assert rows[0]["evidence_locus"] == "harness/mod_0.py:0"

    def test_hermetic_probe_to_artifact_path(self, stub_probe, tmp_path):
        """Environment probe output must land via the production boundary helper."""
        from harness.conversation_jobs import _background_evidence_boundary

        subject = tmp_path / "subject"
        subject.mkdir()
        stub_probe(
            tool_paths={"pyright": str(subject / "pyright"), "tsc": ""},
            browser_path="",
            puppetmaster_version="9.9.9",
            mcp_server_names=["hermetic-probe"],
        )
        session = SimpleNamespace(
            state_dir=str(tmp_path / "state"),
            config=SimpleNamespace(repo=str(subject)),
        )
        artifacts = [{
            "type": "verification",
            "headline": "checked harness/a.py:1",
            "acceptance_criteria": [{
                "criterion": "pyright is clean",
                "status": "passed",
                "evidence": "harness/a.py:1",
            }],
            "execution_ref": {"job_id": CURRENT_JOB},
            "job_id": CURRENT_JOB,
        }]
        res_job = {
            "status": "completed",
            "acceptance_criteria": ["pyright is clean"],
            "artifacts": artifacts,
        }
        stamped = {
            "cwd": str(subject),
            "status": "completed",
            "acceptance_criteria": ["pyright is clean"],
            "artifacts": artifacts,
        }
        text = _background_evidence_boundary(session, CURRENT_JOB, res_job, stamped)
        assert "9.9.9" in text
        assert "hermetic-probe" in text
        assert CURRENT_JOB in text
        assert "pyright is clean" in text
        # Direct builder still agrees with the boundary for the same inputs.
        facts = build_swarm_run_facts(
            job_id=CURRENT_JOB,
            subject_cwd=str(subject),
            state_root=str(tmp_path / "state"),
            artifacts=normalize_execution_refs(artifacts, CURRENT_JOB),
            acceptance_criteria=["pyright is clean"],
        )
        payload = facts.as_dict()
        assert payload["puppetmaster_version"] == "9.9.9"
        assert any(c["status"] == VERIFIED for c in payload["criteria"])
        by_name = {r["name"]: r for r in payload["readiness"]}
        assert by_name["pyright"]["status"] == VERIFIED
        assert by_name["tsc"]["status"] == NOT_VERIFIED
