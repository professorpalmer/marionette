"""Local-job ROUTING artifacts must carry the same basis labels as store jobs."""
from __future__ import annotations

from types import SimpleNamespace

from harness.local_job_routing import _routing_artifact, preview_agentic_route


def test_routing_artifact_stamps_policy_for_reload_labels():
    art = _routing_artifact(
        "cheap-model",
        0.01,
        role="implement",
        reason="balanced pick",
        rejected=[],
        policy="balanced",
        adapter="agentic",
        baseline_cost_usd=0.05,
        baseline_model_id="frontier-model",
    )
    assert art["type"] == "ROUTING"
    assert art["policy"] == "balanced"
    assert art["adapter"] == "agentic"
    assert art["baseline_cost_usd"] == 0.05
    assert art["baseline_model_id"] == "frontier-model"
    assert art["model"] == "cheap-model"


def test_preview_pin_uses_explicit_pin_policy(monkeypatch):
    monkeypatch.setenv("HARNESS_IMPLEMENT_PROVIDER", "openrouter")
    monkeypatch.setenv("HARNESS_IMPLEMENT_MODEL", "pinned-model")
    out = preview_agentic_route("do a thing", role="implement")
    assert out["artifact"]["policy"] == "explicit_pin"
    assert out["routing_savings_basis"] == "estimated"
    assert out["model_id"] == "pinned-model"


def test_preview_router_decision_forwards_policy_and_estimated_basis(monkeypatch):
    monkeypatch.delenv("HARNESS_IMPLEMENT_PROVIDER", raising=False)
    monkeypatch.delenv("HARNESS_IMPLEMENT_MODEL", raising=False)

    class FakeModel:
        id = "agentic/cheap"

    decision = SimpleNamespace(
        model=FakeModel(),
        estimated_cost_usd=0.02,
        estimated_tokens_in=1000,
        estimated_tokens_out=200,
        reason="balanced pick",
        rejected=[],
        baseline_cost_usd=0.10,
        baseline_model_id="frontier",
        policy="balanced",
    )

    monkeypatch.setattr(
        "puppetmaster.model_registry.default_registry_path",
        lambda: "/tmp/unused-registry",
    )
    monkeypatch.setattr(
        "puppetmaster.model_registry.load_registry",
        lambda _p: [SimpleNamespace(id="agentic/cheap")],
    )
    monkeypatch.setattr(
        "puppetmaster.platform_lock.active_allowlist",
        lambda: None,
    )
    monkeypatch.setattr(
        "puppetmaster.router.route_task",
        lambda *a, **k: decision,
    )
    # Avoid capability probe import failures.
    monkeypatch.setattr(
        "pmharness.bridge._router_supports_max_capability",
        lambda: True,
        raising=False,
    )

    out = preview_agentic_route("implement savings honesty", role="implement")
    assert out["artifact"]["policy"] == "balanced"
    assert out["routing_savings_basis"] == "estimated"
    assert out["routing_saved_usd"] == 0.08
    assert out["baseline_model_id"] == "frontier"


def test_finish_preserves_routing_preflight_est_cost(monkeypatch, tmp_path):
    """ROUTING.est_cost_usd stays preflight; realized spend lands on the job row."""
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda goal, role="implement": {
            "model_id": "cheap-model",
            "est_cost_usd": 0.01,
            "routing_saved_usd": 0.04,
            "routing_savings_basis": "estimated",
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to cheap-model",
                "created_by": "router",
                "model": "cheap-model",
                "policy": "balanced",
                "est_cost_usd": 0.01,
                "baseline_cost_usd": 0.05,
                "baseline_model_id": "frontier",
            },
        },
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    s = ConversationalSession(cfg)
    s._register_local_job(
        "local-fin", "edit", role="implement", engine="agentic", model="",
    )
    s._finish_local_job(
        "local-fin", ok=True, summary="done", files=["a.py"],
        tokens=100, engine="agentic", model="cheap-model",
        est_cost_usd=0.05,
    )
    job = s._local_jobs["local-fin"]
    routing = next(a for a in job["artifacts"] if a["type"] == "ROUTING")
    patch = next(a for a in job["artifacts"] if a["type"] == "patch")
    assert abs(routing["est_cost_usd"] - 0.01) < 1e-9
    assert abs(job["est_cost_usd"] - 0.05) < 1e-9
    assert abs(patch["est_cost_usd"] - 0.05) < 1e-9
    assert job["routing_savings_basis"] == "estimated"
    assert abs(job["routing_saved_usd"] - 0.04) < 1e-9


def test_finish_does_not_invent_routing_savings_without_basis(monkeypatch, tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setattr(
        "harness.local_job_routing.preview_agentic_route",
        lambda goal, role="implement": {
            "model_id": "cheap-model",
            "est_cost_usd": 0.02,
            "artifact": {
                "type": "ROUTING",
                "headline": "Routed to cheap-model",
                "created_by": "router",
                "model": "cheap-model",
                "est_cost_usd": 0.02,
                "baseline_cost_usd": 0.10,
            },
        },
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    s = ConversationalSession(cfg)
    s._register_local_job(
        "local-no-basis", "edit", role="implement", engine="agentic", model="",
    )
    s._finish_local_job(
        "local-no-basis", ok=True, summary="done",
        tokens=50, engine="agentic", model="cheap-model",
        est_cost_usd=0.03,
    )
    job = s._local_jobs["local-no-basis"]
    assert "routing_saved_usd" not in job
    assert "routing_savings_basis" not in job
