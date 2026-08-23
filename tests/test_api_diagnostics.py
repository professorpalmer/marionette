"""GET /api/diagnostics authenticated route."""

from __future__ import annotations

from types import SimpleNamespace

from harness.api.doctor import DoctorServices, get_diagnostics


def test_get_diagnostics_returns_quotable_payload(monkeypatch):
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle")
    svc = DoctorServices(
        get_driver=lambda: "stub-oracle",
        get_repo=lambda: "",
        build_driver=lambda _spec: SimpleNamespace(api_key_env=None),
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
        get_repo=lambda: "",
        build_driver=lambda spec: (_ for _ in ()).throw(KeyError(spec)),
    )
    with __import__("harness.correlation", fromlist=["correlation_scope"]).correlation_scope("diag-fail"):
        status, payload = get_diagnostics(svc)
    assert status == 200
    assert payload["diagnostic"] is not None
    assert payload["diagnostic"]["scope"] == "backend"
    assert payload["diagnostic"]["recovery"] == {"kind": "retry", "label": "Retry"}
    assert payload["diagnostic"]["correlation_id"] == "diag-fail"


def test_diagnostics_uses_live_pilot_builder_for_arbitrary_model(monkeypatch):
    calls = []
    spec = "future-provider:future-model-x"

    def build_driver(requested):
        calls.append(requested)
        return SimpleNamespace(api_key_env=None)

    monkeypatch.setattr(
        "pmharness.registry.build",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("research registry must not validate the live pilot")
        ),
    )
    svc = DoctorServices(
        get_driver=lambda: spec,
        get_repo=lambda: "",
        build_driver=build_driver,
    )

    status, payload = get_diagnostics(svc)

    assert status == 200
    assert payload["diagnostic"] is None
    assert calls == [spec]
