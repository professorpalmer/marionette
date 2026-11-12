"""Every relative link and anchor in tracked Markdown resolves."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _checker():
    spec = importlib.util.spec_from_file_location("check_doc_links", ROOT / "scripts" / "check_doc_links.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tracked_markdown_has_no_dead_links():
    checker = _checker()
    errors = [e for f in checker.tracked_markdown() for e in checker.check(f)]
    assert errors == []


def test_checker_reports_missing_file_and_anchor(tmp_path, monkeypatch):
    checker = _checker()
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    (tmp_path / "target.md").write_text("# Target\n\n## 9. Package map\n", encoding="utf-8")
    probe = tmp_path / "probe.md"
    probe.write_text(
        "[a](nope.md)\n[b](target.md#no-such)\n[c](target.md#9-package-map)\n"
        "```\n[d](fenced.md)\n```\n",
        encoding="utf-8",
    )
    errors = checker.check(probe)
    assert len(errors) == 2
    assert "missing file" in errors[0] and "missing anchor" in errors[1]
