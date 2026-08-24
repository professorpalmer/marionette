"""Hermetic tests for the opt-in Chrome browser relay."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import harness.browser as browser
import harness.browser_relay as relay
from harness.api.browser import get_browser_relay, post_browser_relay


ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extensions" / "browser-relay"


@pytest.fixture(autouse=True)
def _reset_relay(monkeypatch):
    monkeypatch.delenv("PM_BROWSER_RELAY", raising=False)
    relay.clear_snapshot()
    yield
    relay.clear_snapshot()


def test_relay_off_by_default():
    assert relay.relay_enabled() is False
    payload, err = relay.record_message({"url": "https://example.com", "title": "Ex"})
    assert payload is None
    assert "off" in err
    assert relay.last_snapshot() is None


def test_record_url_title_and_optional_text(monkeypatch):
    monkeypatch.setenv("PM_BROWSER_RELAY", "1")
    payload, err = relay.record_message(
        {
            "kind": "tab_snapshot",
            "url": "https://example.com/docs",
            "title": "Docs",
            "text": "hello page",
            "tabId": 9,
            "source": "native_host",
        }
    )
    assert err == ""
    assert payload["url"] == "https://example.com/docs"
    assert payload["title"] == "Docs"
    assert payload["text"] == "hello page"
    assert payload["tab_id"] == 9
    assert payload["source"] == "native_host"
    assert payload["kind"] == "tab_snapshot"
    assert relay.last_snapshot()["url"] == "https://example.com/docs"


def test_normalize_rejects_non_http_and_missing_url():
    assert relay.normalize_message({"title": "x"})[1] == "url is required"
    assert "http(s)" in relay.normalize_message({"url": "chrome://settings"})[1]
    assert "unsupported" in relay.normalize_message({"kind": "click", "url": "https://x"})[1]
    assert "JSON object" in relay.normalize_message([])[1]


def test_optional_text_truncated(monkeypatch):
    monkeypatch.setenv("PM_BROWSER_RELAY", "true")
    payload, err = relay.record_message(
        {"url": "http://localhost/", "title": "L", "text": "Z" * (relay.MAX_TEXT_CHARS + 50)}
    )
    assert err == ""
    assert payload["text"] is not None
    assert len(payload["text"]) == relay.MAX_TEXT_CHARS


def test_extension_and_native_host_share_message_shape():
    ext_msg = {
        "kind": "tab_snapshot",
        "url": "https://example.com/",
        "title": "Example",
        "tab_id": 1,
        "source": "extension",
    }
    host_msg = dict(ext_msg)
    host_msg["source"] = "native_host"
    a, err_a = relay.normalize_message(ext_msg)
    b, err_b = relay.normalize_message(host_msg)
    assert err_a == err_b == ""
    assert set(a) == set(b)
    assert a["kind"] == b["kind"] == "tab_snapshot"


def test_api_off_returns_403_and_empty_get():
    status, body = post_browser_relay({"url": "https://example.com", "title": "t"})
    assert status == 403
    assert body["enabled"] is False
    gstatus, gbody = get_browser_relay()
    assert gstatus == 200
    assert gbody["enabled"] is False
    assert gbody["snapshot"] is None


def test_api_records_when_enabled(monkeypatch):
    monkeypatch.setenv("PM_BROWSER_RELAY", "yes")
    status, body = post_browser_relay(
        {"url": "https://example.com/a", "title": "A", "text": "body"}
    )
    assert status == 200
    assert body["ok"] is True
    assert body["snapshot"]["title"] == "A"
    _, listed = get_browser_relay()
    assert listed["enabled"] is True
    assert listed["snapshot"]["url"] == "https://example.com/a"


def test_api_bad_body_is_400(monkeypatch):
    monkeypatch.setenv("PM_BROWSER_RELAY", "1")
    status, body = post_browser_relay({"title": "no url"})
    assert status == 400
    assert "url" in body["error"]


def test_routes_include_browser_relay():
    import harness.server as srv

    srv._POST_JSON_ROUTES = None
    srv._GET_ROUTES = None
    assert "/api/browser/relay" in srv._post_json_routes()
    assert "/api/browser/relay" in srv._get_routes()


def test_browser_module_exposes_relay_without_second_engine():
    assert hasattr(browser, "browser_relay_snapshot")
    assert hasattr(browser, "browser_relay_enabled")
    assert browser.browser_relay_enabled() is False
    assert browser.browser_relay_snapshot() is None
    assert browser._engine is None or getattr(browser._engine, "__name__", "") in (
        "puppetmaster.browser_cdp",
        "",
    )


def test_unpacked_extension_manifest_is_local_only():
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert "update_url" not in manifest
    assert "key" not in manifest
    text = (EXT / "background.js").read_text(encoding="utf-8")
    assert "/api/browser/relay" in text
    assert "tab_snapshot" in text
    for banned in ("sentry", "otel", "opentelemetry", "guardian", "chrome.google.com/webstore"):
        assert banned not in text.lower()
    host = (EXT / "native-host.py").read_text(encoding="utf-8")
    assert "tab_snapshot" in host
    assert "/api/browser/relay" in host
