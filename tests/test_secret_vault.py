"""Host vault: encrypt at rest, presence only, env inject, no model echo."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from harness.secret_vault import (
    apply_to_environ,
    extract_secret_request_message,
    get_secret,
    parse_secret_request_payload,
    presence,
    presence_payload,
    put_secret,
    redact_secret_text,
    subprocess_env,
    vault_path,
)


def test_put_is_encrypted_and_presence_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    row = put_secret("sess-a", "pypi", "token", "pypi-secret-value-xyz")
    assert row["present"] is True
    assert row["state"] == "present"
    raw = json.loads((tmp_path / "secret-vault.json").read_text())
    blob = raw["agents"]["sess-a"]["pypi"]["token"]
    dumped = json.dumps(raw)
    assert "pypi-secret-value-xyz" not in dumped
    assert blob["n"] and blob["c"]
    assert get_secret("sess-a", "pypi", "token") == "pypi-secret-value-xyz"
    listed = presence("sess-a")
    assert listed["connectors"]["pypi"]["token"] == "present"
    assert "pypi-secret-value-xyz" not in json.dumps(listed)


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
    put_secret("sess-a", "pypi", "token", token)
    env = subprocess_env("sess-a", {"PATH": os.environ.get("PATH", "")})
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
