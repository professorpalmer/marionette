"""Browser tool wrapper: disabled / CDP error paths must return calm strings."""
from __future__ import annotations

import types

import harness.browser as browser
import pytest


@pytest.fixture
def restore_engine():
    """Restore harness.browser engine globals after each test mutates them."""
    prev_engine = browser._engine
    prev_err = browser._ENGINE_ERR
    yield
    browser._engine = prev_engine
    browser._ENGINE_ERR = prev_err


def _fake_engine(**methods):
    eng = types.SimpleNamespace()
    for name, fn in methods.items():
        setattr(eng, name, fn)
    return eng


def test_disabled_engine_returns_calm_errors_without_raising(restore_engine):
    browser._engine = None
    browser._ENGINE_ERR = "browser engine unavailable: import failed"

    assert "unavailable" in browser.browser_navigate("https://example.com")
    assert "unavailable" in browser.browser_snapshot()
    assert "unavailable" in browser.browser_click("@e1")
    assert "unavailable" in browser.browser_screenshot()


def test_missing_engine_without_err_string_still_calm(restore_engine):
    browser._engine = None
    browser._ENGINE_ERR = ""

    out = browser.browser_navigate("https://example.com")
    assert isinstance(out, str)
    assert "unavailable" in out


def test_navigate_engine_exception_is_calm(restore_engine):
    def boom(url):
        raise RuntimeError("cdp socket closed")

    browser._ENGINE_ERR = ""
    browser._engine = _fake_engine(navigate=boom)

    out = browser.browser_navigate("https://example.com")
    assert out.startswith("navigate failed:")
    assert "RuntimeError" in out
    assert "cdp socket closed" in out


def test_snapshot_engine_exception_is_calm(restore_engine):
    browser._ENGINE_ERR = ""
    browser._engine = _fake_engine(snapshot=lambda: (_ for _ in ()).throw(ConnectionError("gone")))

    out = browser.browser_snapshot()
    assert out.startswith("snapshot failed:")
    assert "ConnectionError" in out


def test_click_engine_exception_is_calm(restore_engine):
    browser._ENGINE_ERR = ""
    browser._engine = _fake_engine(click=lambda ref: (_ for _ in ()).throw(ValueError("bad ref")))

    out = browser.browser_click("@e9")
    assert out.startswith("click failed:")
    assert "ValueError" in out


def test_screenshot_engine_exception_is_calm(restore_engine):
    browser._ENGINE_ERR = ""
    browser._engine = _fake_engine(
        screenshot=lambda out_dir=None: (_ for _ in ()).throw(OSError("disk full"))
    )

    out = browser.browser_screenshot("/tmp")
    assert out.startswith("screenshot failed:")
    assert "OSError" in out


def test_happy_path_passes_through_engine_strings(restore_engine):
    browser._ENGINE_ERR = ""
    browser._engine = _fake_engine(
        navigate=lambda url: f"Navigated to {url}",
        snapshot=lambda: "Page: ok",
        click=lambda ref: f"Clicked {ref}",
        screenshot=lambda out_dir=None: "Saved screenshot to /tmp/x.png",
    )

    assert browser.browser_navigate("https://example.com") == "Navigated to https://example.com"
    assert browser.browser_snapshot() == "Page: ok"
    assert browser.browser_click("@e1") == "Clicked @e1"
    assert browser.browser_screenshot() == "Saved screenshot to /tmp/x.png"


def test_none_result_becomes_calm_error(restore_engine):
    browser._ENGINE_ERR = ""
    browser._engine = _fake_engine(navigate=lambda url: None)

    out = browser.browser_navigate("https://example.com")
    assert out.startswith("navigate failed:")
    assert "empty result" in out
