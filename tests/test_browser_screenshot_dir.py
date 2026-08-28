"""browser_screenshot defaults off /tmp into ~/.pmharness/browser-shots."""
from __future__ import annotations

from pathlib import Path

import harness.browser as browser


def test_browser_screenshot_defaults_under_pmharness(monkeypatch):
    captured = []

    def _capture(op_name, method_name, *args, **kwargs):
        captured.append((op_name, method_name, args, kwargs))
        return "ok"

    monkeypatch.setattr(browser, "_call", _capture)
    assert browser.browser_screenshot() == "ok"
    assert len(captured) == 1
    op_name, method_name, args, kwargs = captured[0]
    assert op_name == "screenshot"
    assert method_name == "screenshot"
    out_dir = args[0] if args else kwargs.get("out_dir")
    resolved = Path(out_dir)
    assert ".pmharness" in resolved.parts
    assert "browser-shots" in resolved.parts
    assert resolved == Path.home() / ".pmharness" / "browser-shots"


def test_browser_screenshot_explicit_out_dir_unchanged(monkeypatch, tmp_path):
    captured = []

    def _capture(op_name, method_name, *args, **kwargs):
        captured.append(args[0] if args else kwargs.get("out_dir"))
        return "ok"

    monkeypatch.setattr(browser, "_call", _capture)
    explicit = str(tmp_path / "custom-shots")
    assert browser.browser_screenshot(explicit) == "ok"
    assert captured == [explicit]
