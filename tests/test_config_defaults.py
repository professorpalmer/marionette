"""Focused defaults for HarnessConfig driver / from_env layering.

Keeps the shipped default (qwen3-coder-30b) honest against stale docs that once
claimed glm-5.2. Hermetic: no network, no subprocess.
"""
from __future__ import annotations

import json

from harness.config import HarnessConfig


def test_dataclass_default_driver_is_qwen3_coder_30b():
    assert HarnessConfig().driver == "qwen3-coder-30b"


def test_from_env_default_driver_is_qwen3_coder_30b(monkeypatch, tmp_path):
    # Point at a missing config file so ~/.harness.json cannot leak in.
    monkeypatch.setenv("HARNESS_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.delenv("HARNESS_DRIVER", raising=False)
    assert HarnessConfig.from_env().driver == "qwen3-coder-30b"


def test_from_env_file_driver_overrides_default(monkeypatch, tmp_path):
    cfgfile = tmp_path / "h.json"
    cfgfile.write_text(json.dumps({"driver": "glm-5.2"}))
    monkeypatch.setenv("HARNESS_CONFIG", str(cfgfile))
    monkeypatch.delenv("HARNESS_DRIVER", raising=False)
    assert HarnessConfig.from_env().driver == "glm-5.2"


def test_from_env_env_driver_overrides_file(monkeypatch, tmp_path):
    cfgfile = tmp_path / "h.json"
    cfgfile.write_text(json.dumps({"driver": "glm-5.2"}))
    monkeypatch.setenv("HARNESS_CONFIG", str(cfgfile))
    monkeypatch.setenv("HARNESS_DRIVER", "deepseek-v4-pro")
    assert HarnessConfig.from_env().driver == "deepseek-v4-pro"


def test_from_env_max_workers_defaults_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.delenv("HARNESS_MAX_WORKERS", raising=False)
    assert HarnessConfig.from_env().max_workers == 4

    monkeypatch.setenv("HARNESS_MAX_WORKERS", "12")
    assert HarnessConfig.from_env().max_workers == 12
