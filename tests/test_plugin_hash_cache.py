"""Polling /api/mcp and /api/plugins must not re-read every plugin file."""
from __future__ import annotations

import pathlib

from harness.agent_plugins import compute_plugin_content_sha256


def _plugin(tmp_path):
    root = tmp_path / "plug"
    (root / "skills").mkdir(parents=True)
    (root / "plugin.json").write_text('{"name": "p"}', encoding="utf-8")
    (root / "skills" / "a.md").write_text("alpha", encoding="utf-8")
    return root


def test_unchanged_plugin_is_not_re_read(tmp_path, monkeypatch):
    root = _plugin(tmp_path)
    reads = []
    real = pathlib.Path.read_bytes
    monkeypatch.setattr(pathlib.Path, "read_bytes", lambda self: reads.append(self.name) or real(self))

    first = compute_plugin_content_sha256(root)
    assert len(reads) == 2
    assert compute_plugin_content_sha256(root) == first
    assert len(reads) == 2  # cache hit: stat only


def test_an_edit_of_the_same_size_is_re_hashed(tmp_path):
    root = _plugin(tmp_path)
    first = compute_plugin_content_sha256(root)
    (root / "skills" / "a.md").write_text("omega", encoding="utf-8")  # same length
    second = compute_plugin_content_sha256(root)
    assert second != first
    (root / "skills" / "b.md").write_text("new", encoding="utf-8")
    assert compute_plugin_content_sha256(root) not in (first, second)
