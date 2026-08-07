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
                        "tags": ["code", "vision", "detailed-vision"],
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
    assert "vision" in by_id["agentic/cursor-grok-4.5-high-fast"]["tags"]
    assert by_id["agentic/deepseek/deepseek-v4-pro"]["capability_score"] == 85
    assert "vision" not in by_id["agentic/deepseek/deepseek-v4-pro"]["tags"]
    assert "detailed-vision" not in by_id["agentic/deepseek/deepseek-v4-pro"]["tags"]
    assert by_id["agentic/composer-2.5-fast"]["capability_score"] == 76
    assert "vision" in by_id["agentic/composer-2.5-fast"]["tags"]
    assert by_id["agentic/minimax/minimax-m3"]["capability_score"] == 68
    assert "agentic/moonshotai/kimi-k3" in report["updated"]


def test_ladder_matches_flattened_live_ids_and_aliases(tmp_path, monkeypatch):
    """OpenCode Go catalogs use flattened ids + adapter_model_name aliases."""
    dest = tmp_path / "marionette-models.json"
    dest.write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "id": "agentic/kimi-k3",
                        "adapter": "agentic",
                        "adapter_model_name": "kimi-k3",
                        "capability_score": 50,
                        "tags": ["code", "tools"],
                        "payload_defaults": {"provider": "opencode-go"},
                    },
                    {
                        "id": "agentic/grok-4.5",
                        "adapter": "agentic",
                        "adapter_model_name": "grok-4.5",
                        "capability_score": 50,
                        "tags": ["code"],
                        "payload_defaults": {"provider": "opencode-go"},
                    },
                    {
                        "id": "agentic/deepseek-v4-pro",
                        "adapter": "agentic",
                        "adapter_model_name": "deepseek-v4-pro",
                        "capability_score": 50,
                        "tags": ["code", "vision", "detailed-vision"],
                        "payload_defaults": {"provider": "opencode-go"},
                    },
                    {
                        "id": "agentic/composer-2-5-fast",
                        "adapter": "agentic",
                        "adapter_model_name": "composer-2.5-fast",
                        "capability_score": 50,
                        "tags": ["code"],
                    },
                    {
                        "id": "agentic/minimax-m3",
                        "adapter": "agentic",
                        "adapter_model_name": "minimax-m3",
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
    assert "agentic/moonshotai/kimi-k3" not in report.get("missing", [])
    data = json.loads(dest.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in data["models"]}
    assert by_id["agentic/kimi-k3"]["capability_score"] == 98
    assert "vision" in by_id["agentic/kimi-k3"]["tags"]
    assert "detailed-vision" in by_id["agentic/kimi-k3"]["tags"]
    assert by_id["agentic/grok-4.5"]["capability_score"] == 92
    assert "vision" in by_id["agentic/grok-4.5"]["tags"]
    assert by_id["agentic/deepseek-v4-pro"]["capability_score"] == 85
    assert "vision" not in by_id["agentic/deepseek-v4-pro"]["tags"]
    assert "detailed-vision" not in by_id["agentic/deepseek-v4-pro"]["tags"]
    assert by_id["agentic/composer-2-5-fast"]["capability_score"] == 76
    assert "vision" in by_id["agentic/composer-2-5-fast"]["tags"]
    assert by_id["agentic/minimax-m3"]["capability_score"] == 68


def test_ladder_never_adds_vision_to_deepseek_v4_pro(tmp_path, monkeypatch):
    dest = tmp_path / "marionette-models.json"
    dest.write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "id": "agentic/deepseek-v4-pro",
                        "adapter_model_name": "deepseek-v4-pro",
                        "capability_score": 1,
                        "tags": ["code"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(dest))
    apply_marionette_router_ladder(str(dest))
    data = json.loads(dest.read_text(encoding="utf-8"))
    tags = data["models"][0]["tags"]
    assert "vision" not in tags
    assert "detailed-vision" not in tags


def test_ensure_respects_existing_env(tmp_path, monkeypatch):
    pinned = tmp_path / "pinned.json"
    pinned.write_text('{"version":1,"models":[]}\n', encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(pinned))
    assert ensure_marionette_models_env() == str(pinned)


def test_ensure_materializes_missing_explicit_path(tmp_path, monkeypatch):
    missing = tmp_path / "nested" / "marionette-models.json"
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(missing))
    path = ensure_marionette_models_env()
    assert path == str(missing)
    assert missing.is_file()
    data = json.loads(missing.read_text(encoding="utf-8"))
    assert data == {"version": 1, "models": []}


def test_marionette_ladder_and_demote_ids_producible(tmp_path, monkeypatch):
    from harness.marionette_registry import _DEMOTE, _LADDER, apply_marionette_router_ladder

    models = []
    for mid, score, tags in _LADDER:
        models.append({
            "id": mid,
            "adapter": "agentic" if mid.startswith("agentic/") else "cursor",
            "capability_score": 1,
            "tags": ["code"],
        })
    for mid, score in _DEMOTE.items():
        models.append({
            "id": mid,
            "adapter": "agentic",
            "capability_score": 99,
            "tags": ["code"],
        })
    dest = tmp_path / "marionette-models.json"
    dest.write_text(json.dumps({"version": 1, "models": models}), encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(dest))

    report = apply_marionette_router_ladder(str(dest))
    assert report.get("missing") == []
    data = json.loads(dest.read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in data["models"]}
    assert by_id["agentic/moonshotai/kimi-k3"]["capability_score"] == 98
    assert by_id["agentic/minimax/minimax-m3"]["capability_score"] == 68
    assert "vision" not in by_id["agentic/deepseek/deepseek-v4-pro"]["tags"]
