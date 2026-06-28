"""Tests for local-model wiki orchestration (the cheap "backwards" structuring
pass). Hermetic, stdlib-only, no API keys: the driver is a stub returning a fixed
JSON envelope, so we test the parsing/validation/dedup contract deterministically.
"""
import json

from harness.wiki_orchestrator import prepare_pages, _extract_json, _slugify


class _StubDriver:
    """Minimal driver double exposing chat() like the real pilot."""
    def __init__(self, response_text):
        self._text = response_text

    def chat(self, messages, system=""):
        class R:
            text = self._text
            error = None
        R.text = self._text
        return R()


def test_extract_json_plain():
    assert _extract_json('{"pages": []}') == {"pages": []}


def test_extract_json_fenced():
    txt = 'Here you go:\n```json\n{"pages": [{"kind": "concept"}]}\n```\nthanks'
    obj = _extract_json(txt)
    assert obj == {"pages": [{"kind": "concept"}]}


def test_extract_json_garbage():
    assert _extract_json("no json here") is None
    assert _extract_json("") is None


def test_slugify():
    assert _slugify("Project Compass: Contract Intelligence!") == "project-compass-contract-intelligence"
    assert _slugify("") == "untitled"


def test_prepare_pages_happy_path():
    payload = json.dumps({"pages": [
        {"kind": "entity", "title": "Dugout", "body": "A fantasy baseball platform."},
        {"kind": "concept", "title": "Gear Trigger System", "body": "RPG progression via gear."},
        {"kind": "decision", "title": "Use Marcel projections", "body": "Chose Marcel anchor because..."},
    ]})
    res = prepare_pages(_StubDriver(payload), "explore dugout", "USER: explore\nPILOT: it is a fantasy baseball game")
    assert res["status"] == "prepared"
    assert len(res["pages"]) == 3
    kinds = {p["kind"] for p in res["pages"]}
    assert kinds == {"entity", "concept", "decision"}
    # every page has a slug derived from the title
    assert res["pages"][0]["slug"] == "dugout"


def test_prepare_pages_invalid_kinds_filtered():
    payload = json.dumps({"pages": [
        {"kind": "entity", "title": "Valid", "body": "keep me"},
        {"kind": "rumor", "title": "Bad Kind", "body": "drop me"},
        {"kind": "concept", "title": "", "body": "no title -> drop"},
        {"kind": "decision", "title": "No Body", "body": ""},
    ]})
    res = prepare_pages(_StubDriver(payload), "obj", "digest text")
    assert res["status"] == "prepared"
    assert len(res["pages"]) == 1
    assert res["pages"][0]["title"] == "Valid"


def test_prepare_pages_dedup_slugs():
    payload = json.dumps({"pages": [
        {"kind": "concept", "title": "Same Title", "body": "first"},
        {"kind": "concept", "title": "Same Title", "body": "dup -> dropped"},
    ]})
    res = prepare_pages(_StubDriver(payload), "obj", "digest")
    assert len(res["pages"]) == 1


def test_prepare_pages_empty_array():
    res = prepare_pages(_StubDriver('{"pages": []}'), "obj", "digest")
    assert res["status"] == "empty"
    assert res["pages"] == []


def test_prepare_pages_malformed_json():
    res = prepare_pages(_StubDriver("the model rambled with no json"), "obj", "digest")
    assert res["status"] == "error"
    assert res["pages"] == []


def test_prepare_pages_no_digest():
    res = prepare_pages(_StubDriver('{"pages":[]}'), "obj", "")
    assert res["status"] == "empty"
    assert res["reason"] == "no digest"


def test_prepare_pages_max_pages_cap():
    pages = [{"kind": "concept", "title": f"Page {i}", "body": f"body {i}"} for i in range(10)]
    res = prepare_pages(_StubDriver(json.dumps({"pages": pages})), "obj", "digest", max_pages=3)
    assert len(res["pages"]) == 3


def test_prepare_pages_driver_error():
    class _ErrDriver:
        def chat(self, messages, system=""):
            class R:
                text = ""
                error = "rate limited"
            return R()
    res = prepare_pages(_ErrDriver(), "obj", "digest")
    assert res["status"] == "error"
    assert "rate limited" in res["reason"]
