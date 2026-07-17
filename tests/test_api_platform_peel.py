"""Characterization tests for platform + bedrock API peels."""
from __future__ import annotations

import json
from pathlib import Path

from harness.api.platform import (
    PlatformServices,
    get_bedrock,
    get_platform,
    post_bedrock,
    post_platform,
)


def _plat_svc(tmp_path: Path):
    path = tmp_path / "platform.json"
    written = {}

    def _write(p, data):
        written["data"] = data
        Path(p).write_text(json.dumps(data), encoding="utf-8")

    def _adapters():
        disabled = written.get("data", {}).get("disabled", [])
        return {"adapters": [{"name": "cursor", "enabled": "cursor" not in disabled}]}

    return (
        PlatformServices(
            get_platform_json_path=lambda: str(path),
            write_platform_json_atomic=_write,
            get_platform_adapters=_adapters,
            diag=lambda *a: None,
        ),
        path,
        written,
    )


def test_platform_toggle(tmp_path):
    svc, path, written = _plat_svc(tmp_path)
    assert post_platform({"name": "nope", "enabled": True}, svc)[0] == 400
    assert post_platform({"name": "cursor", "enabled": "yes"}, svc)[0] == 400

    code, payload = post_platform({"name": "cursor", "enabled": False}, svc)
    assert code == 200
    assert "cursor" in written["data"]["disabled"]
    assert payload["adapters"][0]["enabled"] is False

    code2, payload2 = post_platform({"name": "cursor", "enabled": True}, svc)
    assert code2 == 200
    assert "cursor" not in written["data"]["disabled"]
    assert get_platform(svc)[1]["adapters"][0]["enabled"] is True


def test_bedrock_set_clear(monkeypatch):
    monkeypatch.setattr(
        "harness.keys.set_bedrock_credentials",
        lambda patch: {"configured": True, "keys": list(patch.keys())},
    )
    monkeypatch.setattr(
        "harness.keys.clear_bedrock_credentials",
        lambda: {"configured": False},
    )
    monkeypatch.setattr(
        "harness.keys.get_bedrock_status",
        lambda: {"configured": True, "region": "us-east-1"},
    )
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe", lambda: None
    )

    assert post_bedrock({})[0] == 400
    code, payload = post_bedrock({"AWS_BEARER_TOKEN_BEDROCK": "tok"})
    assert code == 200 and payload["ok"] is True and payload["configured"] is True
    code2, cleared = post_bedrock({"clear": True})
    assert code2 == 200 and cleared["configured"] is False
    code3, status = get_bedrock()
    assert code3 == 200 and status["region"] == "us-east-1"
