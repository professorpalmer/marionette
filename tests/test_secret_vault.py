"""Secret presence registry: no stored values, env inject, no model echo."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from harness.secret_vault import (
    extract_secret_request_message,
    delete_secret,
    legacy_key_path,
    parse_secret_request_payload,
    presence,
    presence_payload,
    put_secret,
    redact_secret_text,
    vault_path,
)


def test_put_stores_presence_only_and_applies_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    env = {}
    row = put_secret("sess-a", "pypi", "token", "pypi-secret-value-xyz", environ=env)
    assert row["present"] is True
    assert row["state"] == "present"
    assert env["TWINE_PASSWORD"] == "pypi-secret-value-xyz"
    raw = (tmp_path / "secret-vault.json").read_text()
    assert "pypi-secret-value-xyz" not in raw
    stored = json.loads(raw)
    assert stored["v"] == 2
    assert set(stored["agents"]["sess-a"]["pypi"]["token"]) == {"present", "at"}
    assert not (tmp_path / "secret-vault.key").exists()
    listed = presence("sess-a")
    assert listed["connectors"]["pypi"]["token"] == "present"
    assert "pypi-secret-value-xyz" not in json.dumps(listed)


def test_version_1_store_loses_values_and_key_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    blob = {"n": "00" * 16, "c": "ab" * 24}
    (tmp_path / "secret-vault.json").write_text(json.dumps(
        {"v": 1, "agents": {"default": {"pypi": {"token": blob}}, "sess-b": {"slack": {"token": blob}}}}))
    (tmp_path / "secret-vault.key").write_bytes(b"k" * 32)
    assert presence("default", "pypi", "token")["present"] is True
    assert presence("sess-b", "slack", "token")["present"] is True
    stored = json.loads((tmp_path / "secret-vault.json").read_text())
    assert stored["v"] == 2
    assert "ab" * 24 not in json.dumps(stored)
    assert not os.path.exists(legacy_key_path())


def test_unreadable_store_keeps_legacy_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    (tmp_path / "secret-vault.json").write_text("{not json")
    (tmp_path / "secret-vault.key").write_bytes(b"k" * 32)
    assert presence("default", "pypi", "token")["present"] is False
    assert (tmp_path / "secret-vault.key").exists()


def test_delete_removes_presence(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    put_secret("sess-a", "pypi", "token", "pypi-secret-value-xyz", environ={})
    delete_secret("sess-a", "pypi", "token")
    assert presence("sess-a", "pypi", "token")["present"] is False
    assert json.loads((tmp_path / "secret-vault.json").read_text())["agents"] == {}


def test_extract_structured_message_ends_clean():
    text = (
        'Need a token.\n\n```json\n'
        '{"type":"secret-request","secret":{'
        '"label":"PyPI token for puppetmaster-ai",'
        '"connector":"pypi","field":"token",'
        '"description":"Project-scoped token for puppetmaster-ai only."'
        '}}\n```\nplease wait'
    )
    cleaned, parsed = extract_secret_request_message(text)
    assert parsed["connector"] == "pypi"
    assert parsed["field"] == "token"
    assert parsed["label"].startswith("PyPI token")
    assert "please wait" in cleaned
    assert "secret-request" not in cleaned
    assert parse_secret_request_payload({"label": "x"}) is None


def test_subprocess_twine_fixture_never_returns_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    token = "pypi-fixture-token-ABCDEF"
    base = {"PATH": os.environ.get("PATH", "")}
    for key in ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT"):
        if key in os.environ:
            base[key] = os.environ[key]
    env = dict(base)
    put_secret("sess-a", "pypi", "token", token, environ=env)
    assert env["TWINE_USERNAME"] == "__token__"
    assert env["TWINE_PASSWORD"] == token
    script = (
        "import os, json; "
        "print(json.dumps({"
        "'user': os.environ.get('TWINE_USERNAME'),"
        "'has_pw': bool(os.environ.get('TWINE_PASSWORD')),"
        "'pw_len': len(os.environ.get('TWINE_PASSWORD') or '')"
        "}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["user"] == "__token__"
    assert payload["has_pw"] is True
    assert payload["pw_len"] == len(token)
    assert token not in proc.stdout
    assert token not in json.dumps(presence_payload("pypi", "token", True))


def test_redact_process_list_and_artifacts():
    leaked = "cmd TWINE_PASSWORD=pypi-fixture-token-ABCDEF extra"
    assert "pypi-fixture-token-ABCDEF" not in redact_secret_text(
        leaked, extra_values=["pypi-fixture-token-ABCDEF"]
    )
    assert "REDACTED" in redact_secret_text(leaked)
