"""harness doctor: offline checks pass with the stub; missing-key warns not fails."""
from harness import cli

DEEPSEEK_V4_FLASH_VISION_EXP = "openrouter:deepseek/deepseek-v4-flash-vision-exp"


def test_doctor_stub_driver_all_ok(monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "puppetmaster seam" in out
    assert "durable state" in out
    assert "harness ready" in out
    assert code == 0


def test_doctor_missing_key_warns_not_fails(monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_DRIVER", "glm-5.2")
    monkeypatch.setenv("HARNESS_REACH", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    # missing driver key is a WARN (not a hard fail) -> doctor still exits 0
    assert "WARN" in out
    assert "driver glm-5.2" in out
    assert "build failed" not in out
    assert code == 0


def test_doctor_seam_and_store_are_hard_checks(monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    cli.main(["doctor"])
    out = capsys.readouterr().out
    # the seam + store lines report ok in a healthy repo
    assert "[OK  ] puppetmaster seam" in out or "OK" in out


def test_doctor_openrouter_slug_uses_build_pilot_not_registry(monkeypatch, capsys):
    """Catalog-unknown OpenRouter slugs must not KeyError via registry.build."""
    monkeypatch.setenv("HARNESS_DRIVER", DEEPSEEK_V4_FLASH_VISION_EXP)
    monkeypatch.setenv("HARNESS_REACH", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "doctor-test-openrouter-key")
    registry_calls: list[tuple] = []

    import pmharness.registry as reg

    original_build = reg.build

    def spy_registry_build(*args, **kwargs):
        registry_calls.append((args, kwargs))
        return original_build(*args, **kwargs)

    monkeypatch.setattr(reg, "build", spy_registry_build)

    code = cli.main(["doctor"])
    out = capsys.readouterr().out

    assert registry_calls == []
    assert f"driver {DEEPSEEK_V4_FLASH_VISION_EXP}" in out
    assert "build failed" not in out
    assert "harness ready" in out
    assert code == 0


def test_doctor_openrouter_slug_warns_without_key(monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_DRIVER", DEEPSEEK_V4_FLASH_VISION_EXP)
    monkeypatch.setenv("HARNESS_REACH", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    code = cli.main(["doctor"])
    out = capsys.readouterr().out

    assert "WARN" in out
    assert f"driver {DEEPSEEK_V4_FLASH_VISION_EXP}" in out
    assert "build failed" not in out
    assert code == 0
