"""Local implement jobs must label the edit engine truthfully.

Regression: the swarm panel stamped config.driver / openrouter pilot slug as
adapter and task role 'provider worker' even when the worker ran agentic.
"""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.worker import WorkerResult


def _session(driver: str = "stub-oracle-v2") -> ConversationalSession:
    # Hermetic: never construct with a live OpenRouter slug -- suite has no key.
    cfg = HarnessConfig(driver=driver, state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def test_register_local_job_agentic_never_uses_pilot_slug(monkeypatch):
    # Pilot slug on config must not leak into agentic local-job adapter/model.
    # Stub the router preview so hermetic runs without a models registry still
    # exercise the empty-model path.
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda *a, **k: {},
    )
    s = _session(driver="stub-oracle-v2")
    s.config.driver = "openrouter/anthropic/claude-sonnet-4"
    s._register_local_job(
        "local-abc", "edit foo", role="implement",
        engine="agentic", model="",
    )
    job = s._local_jobs["local-abc"]
    assert job["adapter"] == "agentic"
    # Empty model stays empty — never stamp bare "agentic" as the chosen model.
    assert job["model"] == ""
    assert "openrouter" not in job["adapter"]
    assert "openrouter" not in (job["model"] or "")
    assert job["tasks"][0]["role"] == "implement (agentic)"
    assert job["tasks"][0]["adapter"] == "agentic"
    assert "model" not in job["tasks"][0]
    assert "provider worker" not in job["tasks"][0]["role"]


def test_register_local_job_native_uses_engine_and_driver():
    s = _session(driver="stub-oracle-v2")
    s._register_local_job(
        "local-nat", "edit bar", role="implement",
        engine="native", model="stub-oracle-v2",
    )
    job = s._local_jobs["local-nat"]
    assert job["adapter"] == "native"
    assert job["model"] == "native/stub-oracle-v2"
    assert job["tasks"][0]["role"] == "implement (native)"
    assert job["tasks"][0]["adapter"] == "native"
    assert job["tasks"][0]["model"] == "native/stub-oracle-v2"


def test_finish_local_job_overwrites_model_from_worker_result(monkeypatch):
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda *a, **k: {},
    )
    s = _session(driver="stub-oracle-v2")
    s.config.driver = "openrouter/anthropic/claude-sonnet-4"
    s._register_local_job(
        "local-fin", "edit baz", role="implement",
        engine="agentic", model="",
    )
    s._finish_local_job(
        "local-fin", ok=True, summary="done", files=["a.py"],
        tokens=100, engine="agentic", model="z-ai/glm-5.2",
        est_cost_usd=0.0042,
    )
    job = s._local_jobs["local-fin"]
    assert job["adapter"] == "agentic"
    assert job["model"] == "agentic/z-ai/glm-5.2"
    assert job["tasks"][0]["role"] == "implement (agentic)"
    assert job["tasks"][0]["model"] == "agentic/z-ai/glm-5.2"
    assert job["est_cost_usd"] == 0.0042
    assert job["status"] == "completed"


def test_failed_local_terminal_artifact_carries_exact_task_id(monkeypatch):
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda *a, **k: {},
    )
    s = _session()
    s._register_local_job(
        "local-fail", "audit failure", role="analysis",
        engine="agentic", model="",
    )
    task_id = s._local_jobs["local-fail"]["tasks"][0]["id"]

    s._finish_local_job(
        "local-fail", ok=False, summary="Provider login required",
        engine="agentic", model="",
    )

    terminal = next(
        art for art in s._local_jobs["local-fail"]["artifacts"]
        if art["id"] == "local-fail-result"
    )
    assert terminal["type"] == "error"
    assert terminal["task_id"] == task_id


def test_finish_does_not_promote_preview_model_when_terminal_model_empty(monkeypatch):
    """Preview stays ROUTING metadata; empty finish must not invent a selection."""
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda *a, **k: {
            "model_id": "z-ai/glm-5.2",
            "est_cost_usd": 0.01,
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to z-ai/glm-5.2",
                "created_by": "router",
                "model": "z-ai/glm-5.2",
                "policy": "balanced",
            },
        },
    )
    s = _session(driver="stub-oracle-v2")
    s._register_local_job(
        "local-keep", "edit keep", role="implement",
        engine="agentic", model="",
    )
    assert s._local_jobs["local-keep"]["model"] == ""
    routing = next(
        a for a in s._local_jobs["local-keep"]["artifacts"]
        if a.get("type") == "ROUTING"
    )
    assert routing["model"] == "z-ai/glm-5.2"
    s._finish_local_job(
        "local-keep", ok=True, summary="done", files=["a.py"],
        tokens=10, engine="agentic", model="",
    )
    job = s._local_jobs["local-keep"]
    assert job["adapter"] == "agentic"
    assert job["model"] == ""
    assert job["model"] != "agentic"
    assert next(a for a in job["artifacts"] if a.get("type") == "ROUTING")["model"] == (
        "z-ai/glm-5.2"
    )


def test_preview_is_not_selected_identity_until_finish(monkeypatch):
    """No-provider preview must not become job.model; finish is terminal truth."""
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda *a, **k: {
            "model_id": "gpt-5.6-luna",
            "est_cost_usd": 0.02,
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to gpt-5.6-luna",
                "created_by": "router",
                "model": "gpt-5.6-luna",
            },
        },
    )
    s = _session(driver="stub-oracle-v2")
    s._register_local_job(
        "local-truth", "edit foo", role="implement",
        engine="agentic", model="",
    )
    job = s._local_jobs["local-truth"]
    assert job["model"] == ""
    assert job["artifacts"][0]["model"] == "gpt-5.6-luna"
    s._finish_local_job(
        "local-truth", ok=True, summary="done", files=["a.py"],
        tokens=20, engine="agentic", model="gpt-5",
    )
    assert s._local_jobs["local-truth"]["model"] == "agentic/gpt-5"


def test_refresh_local_job_routed_model_stamps_actual_only(monkeypatch):
    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda *a, **k: {
            "model_id": "gpt-5.6-luna",
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to gpt-5.6-luna",
                "model": "gpt-5.6-luna",
            },
        },
    )
    s = _session(driver="stub-oracle-v2")
    s._register_local_job(
        "local-ref", "edit foo", role="implement",
        engine="agentic", model="",
    )
    assert s._local_jobs["local-ref"]["model"] == ""
    s._refresh_local_job_routed_model("local-ref", "gpt-5", engine="agentic")
    assert s._local_jobs["local-ref"]["model"] == "agentic/gpt-5"
    assert s._local_jobs["local-ref"]["tasks"][0]["model"] == "agentic/gpt-5"


def test_refresh_local_job_routed_model_never_raises():
    s = _session()
    s._refresh_local_job_routed_model("", "gpt-5")
    s._refresh_local_job_routed_model("missing", "gpt-5")
    s._refresh_local_job_routed_model("local-x", "agentic")


def test_worker_result_engine_model_defaults_back_compat():
    r = WorkerResult(ok=True, summary="x")
    assert r.engine == ""
    assert r.model == ""
