"""Tests for automatic per-turn wiki grounding injection and savings ledger."""
import json
import os
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.wiki import WikiClient
from harness.wiki_grounding_savings import (
    JSONL_FILENAME,
    REINFERENCE_BASELINE_PER_PAGE,
    parse_jsonl_records,
    session_grounding_payload,
    try_record_grounding,
)


def _session(tmp_path, *, wiki_url="", wiki_token="", repo=""):
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path),
        repo=repo,
    )
    s = ConversationalSession(cfg)
    s.harness_session_id = "sess-wiki"
    if wiki_url or wiki_token:
        s._wiki = WikiClient(base_url=wiki_url, token=wiki_token)
    return s


def test_wiki_section_empty_when_not_configured(tmp_path):
    s = _session(tmp_path)
    assert s._wiki.configured is False
    assert s._build_turn_wiki_section("what did we decide about auth?") == ""


def test_wiki_section_non_empty_with_mocked_search(tmp_path, monkeypatch):
    s = _session(
        tmp_path,
        wiki_url="https://wiki.example.com",
        wiki_token="tok",
        repo=str(tmp_path / "marionette"),
    )

    def fake_search(query, *, limit=5):
        assert "marionette" in query
        assert "default driver" in query
        return [
            {
                "title": "Driver decision",
                "slug": "driver-decision",
                "snippet": "We chose stub-oracle-v2 as the default driver.",
            }
        ]

    monkeypatch.setattr(s._wiki, "search_pages", fake_search)
    section = s._build_turn_wiki_section("what about the default driver?")
    assert section
    assert "WIKI HAS ALREADY BEEN QUERIED" in section
    assert "Driver decision" in section
    assert "stub-oracle-v2" in section


def test_trailer_includes_wiki_after_cg(tmp_path, monkeypatch):
    s = _session(
        tmp_path,
        wiki_url="https://wiki.example.com",
        wiki_token="tok",
        repo=str(tmp_path / "repo"),
    )

    monkeypatch.setattr(
        s,
        "_build_turn_cg_section",
        lambda msg: "CODEGRAPH SLICE",
    )
    monkeypatch.setattr(
        s,
        "_build_turn_wiki_section",
        lambda msg: "WIKI SLICE",
    )

    out = s._append_turn_context_trailer("user question", "user question")
    assert "[context for this turn]" in out
    cg_pos = out.index("CODEGRAPH SLICE")
    wiki_pos = out.index("WIKI SLICE")
    assert cg_pos < wiki_pos


def test_ledger_record_written_on_successful_inject(tmp_path, monkeypatch):
    s = _session(
        tmp_path,
        wiki_url="https://wiki.example.com",
        wiki_token="tok",
    )

    monkeypatch.setattr(
        s._wiki,
        "search_pages",
        lambda q, *, limit=5: [
            {"title": "A", "slug": "a", "snippet": "alpha"},
            {"title": "B", "slug": "b", "snippet": "beta"},
        ],
    )

    section = s._build_turn_wiki_section("prior release decision?")
    assert section

    ledger_path = os.path.join(str(tmp_path), JSONL_FILENAME)
    assert os.path.isfile(ledger_path)
    records = parse_jsonl_records(ledger_path)
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "wiki_grounding"
    assert rec["session_id"] == "sess-wiki"
    assert rec["pages"] == 2
    assert rec["chars"] == len(section)
    assert rec["tokens_fed"] == len(section) // 4
    assert rec["estimated_reinference_tokens"] == 2 * REINFERENCE_BASELINE_PER_PAGE


def test_fail_soft_on_wiki_exception(tmp_path, monkeypatch):
    s = _session(
        tmp_path,
        wiki_url="https://wiki.example.com",
        wiki_token="tok",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("wiki down")

    monkeypatch.setattr(s._wiki, "search_pages", boom)
    assert s._build_turn_wiki_section("anything") == ""

    ledger_path = os.path.join(str(tmp_path), JSONL_FILENAME)
    assert not os.path.isfile(ledger_path)


def test_wiki_grounding_fields_in_usage(tmp_path):
    s = _session(
        tmp_path,
        wiki_url="https://wiki.example.com",
        wiki_token="tok",
    )
    try_record_grounding(
        state_dir=str(tmp_path),
        session_id="sess-wiki",
        chars=400,
        pages=1,
        price_in=2.0,
    )
    fields = s._wiki_grounding_fields()
    assert fields["wiki_groundings"] == 1
    assert fields["wiki_tokens_fed"] == 100
    assert fields["wiki_pages_fed"] == 1
    assert fields["wiki_estimated_reinference_tokens"] == REINFERENCE_BASELINE_PER_PAGE


def test_session_grounding_payload_shape(tmp_path):
    try_record_grounding(
        state_dir=str(tmp_path),
        session_id="x",
        chars=800,
        pages=2,
        price_in=1.5,
    )
    payload = session_grounding_payload(str(tmp_path), "x", price_in=1.5)
    assert payload["wiki_groundings"] == 1
    assert payload["wiki_tokens_fed"] == 200
    assert payload["wiki_pages_fed"] == 2
    assert "wiki_estimated_savings_usd" in payload


def test_wiki_client_search_pages_parses_results(monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({
                "results": [
                    {"title": "T", "slug": "t", "snippet": "snippet text"},
                ]
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = WikiClient(base_url="https://wiki.example.com", token="secret")
    hits = client.search_pages("auth flow", limit=3)
    assert hits == [{"title": "T", "slug": "t", "snippet": "snippet text"}]
    assert "/wiki/search?q=" in captured["url"]
