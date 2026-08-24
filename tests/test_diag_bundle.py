"""Diagnostics bundle: redacted zip + manifest for bug reports (hermetic)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from harness.api.doctor import DoctorServices, get_diagnostics_bundle
from harness.diag_bundle import build_manifest, write_diag_bundle
from harness import cli


PLANTED_SK = "sk-plantedsecretvalue99999999"
PLANTED_GHP = "ghp_plantedGitHubPatXXXXXXXX"
PLANTED_BEARER = "Bearer planted_bearer_token_abcdef"


def _stub_plugins(monkeypatch):
    from harness import plugin_registry as pr

    class _Rec:
        id = "demo-plugin"
        name = "Demo Plugin"
        enabled = True

    monkeypatch.setattr(pr, "discover_plugins", lambda: [_Rec()])
    monkeypatch.setattr(
        pr,
        "plugin_record_to_dict",
        lambda r: {"id": r.id, "name": r.name, "enabled": r.enabled, "path": "/secret/path"},
    )


def _seed_state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    (state / "diagnostics.log").write_text(
        f"boot ok api_key={PLANTED_SK} token={PLANTED_GHP} auth={PLANTED_BEARER}\n",
        encoding="utf-8",
    )
    sessions = {
        "active": "sess-2",
        "sessions": [
            {"id": "sess-1", "title": "Old", "created": 100.0},
            {"id": "sess-2", "title": "New", "created": 200.0},
            {"id": "sess-3", "title": "Mid", "created": 150.0},
        ],
    }
    (state / "harness_sessions.json").write_text(json.dumps(sessions), encoding="utf-8")
    return state


def test_write_diag_bundle_manifest_and_redaction(tmp_path, monkeypatch):
    state = _seed_state(tmp_path)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _stub_plugins(monkeypatch)

    outdir = tmp_path / "out"
    zip_path, manifest = write_diag_bundle(
        str(outdir),
        session_limit=2,
        state_dir=str(state),
        get_driver=lambda: "stub-oracle-v2",
        get_reach=lambda: "local",
        get_repo=lambda: "",
    )

    assert Path(zip_path).is_file()
    manifest_sidecars = list(outdir.glob("*.manifest.json"))
    assert len(manifest_sidecars) == 1

    for key in ("version", "os", "pin", "plugins", "session_ids", "checks"):
        assert key in manifest
    assert manifest["pin"].startswith("puppetmaster-ai==")
    assert manifest["session_ids"] == ["sess-2", "sess-3"]
    assert manifest["plugins"] == [
        {"id": "demo-plugin", "name": "Demo Plugin", "enabled": True}
    ]
    assert isinstance(manifest["checks"], list)
    assert manifest["checks"]
    assert "doctor" in manifest and "checks" in manifest["doctor"]

    blob = Path(zip_path).read_bytes()
    assert PLANTED_SK.encode() not in blob
    assert PLANTED_GHP.encode() not in blob
    assert b"planted_bearer_token_abcdef" not in blob

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "settings.json" in names
        assert "logs/diagnostics.log" in names
        log = zf.read("logs/diagnostics.log").decode("utf-8")
        assert PLANTED_SK not in log
        assert "REDACTED" in log
        inner = json.loads(zf.read("manifest.json"))
        packed = json.dumps(inner)
        assert PLANTED_SK not in packed
        assert PLANTED_GHP not in packed


def test_build_manifest_strips_secret_settings(monkeypatch, tmp_path):
    state = _seed_state(tmp_path)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    _stub_plugins(monkeypatch)

    # Plant a secret-shaped value into the public settings path via env that
    # collect_public_settings does not normally emit — exercise strip_secrets.
    from harness import diag_bundle as db

    monkeypatch.setattr(
        db,
        "collect_public_settings",
        lambda: db.strip_secrets(
            {
                "driver": "stub-oracle-v2",
                "api_key": PLANTED_SK,
                "note": f"key={PLANTED_SK}",
            }
        ),
    )
    manifest = build_manifest(
        session_limit=1,
        state_dir=str(state),
        get_driver=lambda: "stub-oracle-v2",
        get_reach=lambda: "local",
        get_repo=lambda: "",
        checks=[{"status": "ok", "name": "result", "detail": "harness ready"}],
    )
    packed = json.dumps(manifest)
    assert "api_key" not in manifest.get("settings", {})
    assert PLANTED_SK not in packed


def test_cli_doctor_bundle_writes_file(tmp_path, monkeypatch, capsys):
    state = _seed_state(tmp_path)
    outdir = tmp_path / "cli-out"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    _stub_plugins(monkeypatch)

    code = cli.main(["doctor", "--bundle", str(outdir), "--sessions", "1"])
    out = capsys.readouterr().out
    assert code == 0
    assert "diagnostics bundle:" in out
    zips = list(outdir.glob("*.zip"))
    assert len(zips) == 1
    assert zips[0].is_file()


def test_get_diagnostics_bundle_api(tmp_path, monkeypatch):
    state = _seed_state(tmp_path)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    _stub_plugins(monkeypatch)

    # Force default outdir under tmp so we never write into a real home.
    from harness import diag_bundle as db

    outdir = tmp_path / "api-out"
    monkeypatch.setattr(db, "_default_outdir", lambda: str(outdir))

    svc = DoctorServices(
        get_driver=lambda: "stub-oracle-v2",
        get_reach=lambda: "local",
        get_repo=lambda: "",
    )
    with __import__("harness.correlation", fromlist=["correlation_scope"]).correlation_scope(
        "diag-bundle-test",
    ):
        status, payload = get_diagnostics_bundle("2", svc)

    assert status == 200
    assert payload["ok"] is True
    assert Path(payload["path"]).is_file()
    assert "manifest" in payload
    for key in ("version", "os", "pin", "plugins", "session_ids", "checks"):
        assert key in payload["manifest"]
    packed = json.dumps(payload)
    assert PLANTED_SK not in packed
    assert PLANTED_GHP not in packed
