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


def test_register_local_job_agentic_never_uses_pilot_slug():
    # Pilot slug on config must not leak into agentic local-job adapter/model.
    s = _session(driver="stub-oracle-v2")
    s.config.driver = "openrouter/anthropic/claude-sonnet-4"
    s._register_local_job(
        "local-abc", "edit foo", role="implement",
        engine="agentic", model="",
    )
    job = s._local_jobs["local-abc"]
    assert job["adapter"] == "agentic"
    assert job["model"] == "agentic"
    assert "openrouter" not in job["adapter"]
    assert "openrouter" not in job["model"]
    assert job["tasks"][0]["role"] == "implement (agentic)"
    assert job["tasks"][0]["adapter"] == "agentic"
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


def test_finish_local_job_overwrites_model_from_worker_result():
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
    assert job["est_cost_usd"] == 0.0042
    assert job["status"] == "completed"


def test_worker_result_engine_model_defaults_back_compat():
    r = WorkerResult(ok=True, summary="x")
    assert r.engine == ""
    assert r.model == ""
