"""GET /api/diagnostics authenticated route."""

from __future__ import annotations

from harness.api.doctor import DoctorServices, get_diagnostics

DEEPSEEK_V4_FLASH_VISION_EXP = "openrouter:deepseek/deepseek-v4-flash-vision-exp"


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


def test_get_diagnostics_openrouter_slug_uses_build_pilot_not_registry(monkeypatch):
    """Catalog-unknown OpenRouter slugs must not KeyError via registry.build."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "doctor-test-openrouter-key")
    registry_calls: list[tuple] = []

    import pmharness.registry as reg

    original_build = reg.build

    def spy_registry_build(*args, **kwargs):
        registry_calls.append((args, kwargs))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(reg, "build", spy_registry_build)

    svc = DoctorServices(
        get_driver=lambda: DEEPSEEK_V4_FLASH_VISION_EXP,
        get_reach=lambda: "openrouter",
        get_repo=lambda: "",
    )
    with __import__("harness.correlation", fromlist=["correlation_scope"]).correlation_scope(
        "diag-openrouter-slug",
    ):
        status, payload = get_diagnostics(svc)

    assert status == 200
    driver_checks = [c for c in payload["checks"] if c["name"] == f"driver {DEEPSEEK_V4_FLASH_VISION_EXP}"]
    assert len(driver_checks) == 1
    assert driver_checks[0]["status"] == "ok"
    assert "OPENROUTER_API_KEY present" in driver_checks[0]["detail"]
    assert registry_calls == []
    diag = payload["diagnostic"]
    assert diag is None or "failed" not in str(diag.get("summary", "")).lower()


def test_get_diagnostics_openrouter_slug_warns_without_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = DoctorServices(
        get_driver=lambda: DEEPSEEK_V4_FLASH_VISION_EXP,
        get_reach=lambda: "openrouter",
        get_repo=lambda: "",
    )
    with __import__("harness.correlation", fromlist=["correlation_scope"]).correlation_scope(
        "diag-openrouter-missing-key",
    ):
        status, payload = get_diagnostics(svc)

    assert status == 200
    driver_checks = [c for c in payload["checks"] if c["name"] == f"driver {DEEPSEEK_V4_FLASH_VISION_EXP}"]
    assert len(driver_checks) == 1
    assert driver_checks[0]["status"] == "warn"
    assert "failed" not in driver_checks[0]["detail"].lower()
    assert payload["diagnostic"] is not None
    assert "failed" not in str(payload["diagnostic"].get("summary", "")).lower()
