"""GET /api/diagnostics authenticated route."""

from __future__ import annotations

from harness.api.doctor import DoctorServices, get_diagnostics


def test_get_diagnostics_returns_quotable_payload(monkeypatch):
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle")
    svc = DoctorServices(
        get_driver=lambda: "stub-oracle",
        get_reach=lambda: "local",
        get_repo=lambda: "",
    )
    with __import__("harness.correlation", fromlist=["correlation_scope"]).correlation_scope("diag-test"):
        status, payload = get_diagnostics(svc)

    assert status == 200
    assert payload["ok"] is True
    assert payload["correlation_id"] == "diag-test"
    assert isinstance(payload["checks"], list)
    assert payload["checks"]
    assert payload["diagnostic"] is None


def test_get_diagnostics_surfaces_hard_failures(monkeypatch):
    svc = DoctorServices(
        get_driver=lambda: "definitely-not-a-real-driver-spec",
        get_reach=lambda: "cloud",
        get_repo=lambda: "",
    )
    with __import__("harness.correlation", fromlist=["correlation_scope"]).correlation_scope("diag-fail"):
        status, payload = get_diagnostics(svc)
    assert status == 200
    assert payload["diagnostic"] is not None
    assert payload["diagnostic"]["scope"] == "backend"
    assert payload["diagnostic"]["recovery"] == {"kind": "retry", "label": "Retry"}
    assert payload["diagnostic"]["correlation_id"] == "diag-fail"
