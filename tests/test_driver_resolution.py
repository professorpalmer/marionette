"""Regression: a 'provider:model' picker spec (e.g. openrouter:openai/gpt-5.4)
must not crash Session/_rebuild_pilot_and_session with an uncaught KeyError. The
bug: Session built only via reg.build (exact catalog names), so a picker spec the
catalog did not know raised KeyError, which crashed every POST that rebuilt the
pilot (workspace-open, session-switch) -> "socket hang up" / dead app.
"""
import os

from harness.session import Session
from harness.config import HarnessConfig


def test_provider_model_spec_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
    # A spec the eval catalog does not contain, but the provider layer can route.
    s = Session(HarnessConfig(driver="openrouter:openai/gpt-5.4",
                              state_dir=str(tmp_path)))
    assert s.driver is not None
    assert "OpenAICompat" in type(s.driver).__name__


def test_stub_driver_still_builds(tmp_path):
    s = Session(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    assert "Stub" in type(s.driver).__name__


def test_catalog_name_still_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
    s = Session(HarnessConfig(driver="qwen3-coder-30b", state_dir=str(tmp_path)))
    assert s.driver is not None
