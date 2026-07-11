"""Tests for wiki.json path preference and hosted URL normalization."""
from __future__ import annotations

import json
import os

from harness import wiki_config


def test_parse_personal_llm_url_extracts_api_and_token():
    raw = "https://portablellm.wiki/professorpalmer/llm?t=secret-token"
    parsed = wiki_config.parse_wiki_connection_string(raw)
    assert parsed["api_base"] == "https://api.portablellm.wiki/t/professorpalmer"
    assert parsed["owner_token"] == "secret-token"


def test_parse_frontend_tenant_url():
    parsed = wiki_config.parse_wiki_connection_string(
        "https://portablellm.wiki/professorpalmer"
    )
    assert parsed["api_base"] == "https://api.portablellm.wiki/t/professorpalmer"
    assert parsed["owner_token"] is None


def test_parse_already_correct_api_base():
    parsed = wiki_config.parse_wiki_connection_string(
        "https://api.portablellm.wiki/t/professorpalmer"
    )
    assert parsed["api_base"] == "https://api.portablellm.wiki/t/professorpalmer"


def test_set_wiki_config_normalizes_personal_url(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("WIKI_API_BASE", "")
    monkeypatch.setenv("WIKI_OWNER_TOKEN", "")

    res = wiki_config.set_wiki_config(
        api_base="https://portablellm.wiki/acme/llm?t=tok123",
        owner_token=None,
    )
    assert res["api_base"] == "https://api.portablellm.wiki/t/acme"
    assert res["has_token"] is True
    on_disk = json.loads((state / "wiki.json").read_text(encoding="utf-8"))
    assert on_disk["api_base"] == "https://api.portablellm.wiki/t/acme"
    assert on_disk["owner_token"] == "tok123"
    assert os.environ.get("WIKI_API_BASE") == "https://api.portablellm.wiki/t/acme"


def test_is_hosted_and_remote_helpers():
    assert wiki_config.is_hosted_portablellm_base(
        "https://api.portablellm.wiki/t/x"
    )
    assert wiki_config.is_hosted_portablellm_base(
        "https://portablellm.wiki/x"
    )
    assert wiki_config.is_remote_wiki_base("https://api.portablellm.wiki/t/x")
    assert not wiki_config.is_remote_wiki_base("http://127.0.0.1:8000")
    assert not wiki_config.is_remote_wiki_base("")


def test_clear_wiki_config_wipes_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    wiki_config.set_wiki_config(
        api_base="https://api.portablellm.wiki/t/acme",
        owner_token="tok",
    )
    assert os.environ.get("WIKI_OWNER_TOKEN") == "tok"
    res = wiki_config.clear_wiki_config()
    assert res == {"api_base": "", "has_token": False}
    assert not os.environ.get("WIKI_API_BASE")
    assert not os.environ.get("WIKI_OWNER_TOKEN")
    on_disk = json.loads((state / "wiki.json").read_text(encoding="utf-8"))
    assert on_disk == {}
