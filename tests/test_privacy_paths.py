from __future__ import annotations

import json
import os
from types import SimpleNamespace

from harness.api.privacy import get_privacy, post_privacy
from harness.privacy_paths import (
    FORBIDDEN_ENV,
    forbidden_reason,
    load_forbidden_patterns,
    refuse_path,
    save_forbidden_patterns,
)
from harness.tool_dispatch import ToolDispatchMixin
from harness.pilot import PilotAction


def test_user_pattern_refuses_env_and_prompt_cannot_bypass(tmp_path):
    save_forbidden_patterns([".env", "*.pem"], str(tmp_path))
    env_path = str(tmp_path / "repo" / ".env")
    pem_path = str(tmp_path / "repo" / "certs" / "prod.pem")
    ok_path = str(tmp_path / "repo" / "readme.md")
    assert "blocked for security" in (refuse_path(env_path, state_dir=str(tmp_path)) or "")
    assert refuse_path(pem_path, state_dir=str(tmp_path))
    assert refuse_path(ok_path, state_dir=str(tmp_path)) is None
    # Prompt-injection wording is irrelevant; the tool still sees the path.
    assert forbidden_reason(".env", load_forbidden_patterns(str(tmp_path)))


class _Dispatch(ToolDispatchMixin):
    def __init__(self, repo, state_dir):
        self.config = type("C", (), {"repo": repo})()
        self.state_dir = state_dir

    def _read_allowed_roots(self):
        return [self.config.repo]


def test_read_file_tool_refuses_forbidden_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    secret = repo / ".env"
    secret.write_text("SECRET=1\n", encoding="utf-8")
    save_forbidden_patterns([".env"], str(tmp_path))
    session = _Dispatch(str(repo), str(tmp_path))
    ok, status, text = session._do_read_file(PilotAction(kind="read_file", path=".env"))
    assert ok is False
    assert status == "forbidden"
    assert "blocked for security" in text
    assert "SECRET" not in text


def test_default_patterns_are_empty(tmp_path):
    assert load_forbidden_patterns(str(tmp_path)) == []


def test_search_files_refuses_forbidden_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    save_forbidden_patterns([".env"], str(tmp_path))
    session = _Dispatch(str(repo), str(tmp_path))
    ok, status, text = session._do_search_files(
        PilotAction(kind="search_files", query="SECRET", arguments={"path": ".env"})
    )
    assert ok is False
    assert status == "forbidden"
    assert "SECRET" not in str(text)


def test_repo_wide_search_skips_forbidden_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=unique-env-token\n", encoding="utf-8")
    (repo / "readme.md").write_text("hello world\n", encoding="utf-8")
    save_forbidden_patterns([".env"], str(tmp_path))
    session = _Dispatch(str(repo), str(tmp_path))
    ok, status, text = session._do_search_files(
        PilotAction(kind="search_files", query="unique-env-token")
    )
    assert ok is True
    assert status == "success"
    assert "unique-env-token" not in str(text)
    assert "SECRET" not in str(text)


def test_mention_refuses_forbidden_file(tmp_path):
    from harness.mention_context import read_file_mention

    secret = tmp_path / "repo" / ".env"
    secret.parent.mkdir()
    secret.write_text("SECRET=1\n", encoding="utf-8")
    save_forbidden_patterns([".env"], str(tmp_path))
    block, added = read_file_mention(
        str(secret), ".env", total_size=0, state_dir=str(tmp_path)
    )
    assert added == 0
    assert "blocked for security" in block
    assert "SECRET" not in block


def test_privacy_api_set_add_remove(tmp_path, monkeypatch):
    monkeypatch.delenv(FORBIDDEN_ENV, raising=False)
    exported = []
    svc = SimpleNamespace(
        state_dir=lambda: str(tmp_path),
        refresh_workers=lambda patterns: exported.append(list(patterns)),
    )
    code, payload = get_privacy({}, svc)
    assert code == 200
    assert payload["forbidden_patterns"] == []

    code, payload = post_privacy({"action": "set", "patterns": [".env", "*.pem"]}, svc)
    assert code == 200
    assert payload["forbidden_patterns"] == [".env", "*.pem"]
    assert load_forbidden_patterns(str(tmp_path)) == [".env", "*.pem"]
    assert exported[-1] == [".env", "*.pem"]

    code, payload = post_privacy({"action": "remove", "pattern": ".env"}, svc)
    assert payload["forbidden_patterns"] == ["*.pem"]

    code, payload = post_privacy({"action": "add", "pattern": "secrets/**"}, svc)
    assert payload["forbidden_patterns"] == ["*.pem", "secrets/**"]

    code, payload = post_privacy({"action": "set", "patterns": "nope"}, svc)
    assert code == 400
