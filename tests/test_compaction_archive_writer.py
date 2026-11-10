from __future__ import annotations

import json

import pytest

from harness.compaction_archive import _atomic_write_json


def test_atomic_write_uses_the_c_encoder(tmp_path, monkeypatch):
    """The transcript writer sits on the turn thread: the pure-Python encoder is 5x slower."""
    import json.encoder

    def python_encoder(*_a, **_k):
        raise AssertionError("pure-Python JSON encoder used")

    monkeypatch.setattr(json.encoder, "_make_iterencode", python_encoder)
    data = {"history": [{"role": "user", "content": "café " * 50}], "display": list(range(100))}
    path = tmp_path / "t.json"
    _atomic_write_json(str(path), data)
    assert json.loads(path.read_text(encoding="utf-8")) == data
    assert path.read_bytes().isascii()


def test_atomic_write_fails_when_the_file_is_changed_before_readback(tmp_path, monkeypatch):
    import os

    path = tmp_path / "t.json"
    real_replace = os.replace

    def replace_then_clobber(src, dst):
        real_replace(src, dst)
        with open(dst, "wb") as handle:
            handle.write(b'{"other":1}')

    monkeypatch.setattr(os, "replace", replace_then_clobber)
    with pytest.raises(OSError, match="readback mismatch"):
        _atomic_write_json(str(path), {"history": []})
