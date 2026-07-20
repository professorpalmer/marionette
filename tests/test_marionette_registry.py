"""Marionette-isolated models registry + router ladder."""
from __future__ import annotations

import json
from pathlib import Path

from harness.marionette_registry import (
    apply_marionette_router_ladder,
    ensure_marionette_models_env,
)


def test_ensure_copies_shared_registry(tmp_path, monkeypatch):
    shared = tmp_path / "shared-models.json"
    shared.write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "id": "agentic/moonshotai/kimi-k3",
                        "capability_score": 50,
                        "tags": ["code"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "marionette-models.json"
    monkeypatch.delenv("PUPPETMASTER_MODELS_PATH", raising=False)
    monkeypatch.setattr(
        "harness.marionette_registry.marionette_models_path",
        lambda: dest,
    )
    monkeypatch.setattr(
        "harness.marionette_registry.shared_puppetmaster_models_path",
        lambda: shared,
    )
    import os

    path = ensure_marionette_models_env()
    assert Path(path) == dest
    assert dest.is_file()
    assert os.environ.get("PUPPETMASTER_MODELS_PATH") == str(dest)


def test_ladder_scores_and_vision_tags(tmp_path, monkeypatch):
    dest = tmp_path / "marionette-models.json"
    dest.write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "id": "agentic/moonshotai/kimi-k3",
                        "capability_score": 50,
                        "tags": ["code"],
                    },
                    {
                        "id": "agentic/cursor-grok-4.5-high-fast",
                        "capability_score": 50,
                        "tags": ["code"],
                    },
                    {
                        "id": "agentic/deepseek/deepseek-v4-pro",
                        "capability_score": 50,
                        "tags": ["code"],
                    },
                    {
                        "id": "agentic/composer-2.5-fast",
                        "capability_score": 50,
                        "tags": ["code"],
                    },
                    {
                        "id": "agentic/minimax/minimax-m3",
                        "capability_score": 99,
                        "tags": ["code"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(dest))
    report = apply_marionette_router_ladder(str(dest))
    data = json.loads(dest.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in data["models"]}
    assert by_id["agentic/moonshotai/kimi-k3"]["capability_score"] == 98
    assert "vision" in by_id["agentic/moonshotai/kimi-k3"]["tags"]
    assert by_id["agentic/cursor-grok-4.5-high-fast"]["capability_score"] == 92
    assert by_id["agentic/deepseek/deepseek-v4-pro"]["capability_score"] == 85
    assert "vision" in by_id["agentic/deepseek/deepseek-v4-pro"]["tags"]
    assert by_id["agentic/composer-2.5-fast"]["capability_score"] == 76
    assert by_id["agentic/minimax/minimax-m3"]["capability_score"] == 68
    assert "agentic/moonshotai/kimi-k3" in report["updated"]


def test_ensure_respects_existing_env(tmp_path, monkeypatch):
    pinned = tmp_path / "pinned.json"
    pinned.write_text('{"version":1,"models":[]}\n', encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(pinned))
    assert ensure_marionette_models_env() == str(pinned)
