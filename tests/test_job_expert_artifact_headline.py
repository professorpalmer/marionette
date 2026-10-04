"""Worker evidence reads as evidence, not as the task instruction."""
from __future__ import annotations

from puppetmaster.models import Artifact, ArtifactType

from harness.job_expert import artifact_projection, chronological

INSTRUCTION = 'Read-only audit of harness/command_policy.py (and its tests under tests/). Three focus areas...'


def verification(**payload):
    return Artifact(job_id='job_1', task_id='task_1', type=ArtifactType.VERIFICATION,
                    created_by='worker-explore-inline', payload=dict(check=INSTRUCTION, **payload),
                    confidence=1.0, evidence=[])


def test_worker_verdict_headline_is_the_verdict():
    # The tracker's "Latest evidence" showed the whole instruction, because a
    # verification's check is the task text. The verdict is the evidence.
    projected = artifact_projection(verification(
        kind='worker_verdict', source='worker', advisory=True, verdict='PASS', result='passed',
        reason='Completed focused read-only inspection of the classifier and its tests.'))
    assert projected['headline'] == 'Worker verdict PASS'
    assert projected['detail'].startswith('Completed focused read-only inspection')
    assert projected['check_result'] == 'passed'


def test_agentic_run_record_headline_summarizes_the_run():
    projected = artifact_projection(verification(
        result='passed', turns=6, total_tool_calls=14, stop_reason='submit', stdout='FINDING: ...'))
    assert projected['headline'] == 'Worker run: 6 turns, 14 tool calls'
    assert INSTRUCTION not in projected['headline']


def test_a_gate_check_command_stays_the_headline():
    projected = artifact_projection(Artifact(
        job_id='job_1', task_id='task_1', type=ArtifactType.VERIFICATION, created_by='gate',
        payload=dict(check='pytest -q', result='passed'), confidence=1.0, evidence=[]))
    assert projected['headline'] == 'pytest -q'


def test_same_second_artifacts_follow_the_order_a_worker_writes_them():
    at = '2026-10-03T20:19:12+00:00'
    def made(type_, payload, ident):
        return Artifact(job_id='job_1', task_id='task_1', type=type_, created_by='w', payload=payload,
                        confidence=1.0, evidence=[], id=ident, created_at=at)
    verdict = made(ArtifactType.VERIFICATION, dict(kind='worker_verdict', verdict='PASS', check=INSTRUCTION), 'artifact_0a')
    finding = made(ArtifactType.FINDING, dict(claim='rm -r -f evades the rule'), 'artifact_1b')
    run = made(ArtifactType.VERIFICATION, dict(turns=6, total_tool_calls=14, check=INSTRUCTION), 'artifact_2c')
    route = made(ArtifactType.ROUTING, dict(model_id='m'), 'artifact_3d')
    assert [a.id for a in chronological([verdict, finding, run, route])] == [
        'artifact_3d', 'artifact_2c', 'artifact_1b', 'artifact_0a']
