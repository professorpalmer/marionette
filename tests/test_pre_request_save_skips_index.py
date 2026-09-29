"""The transcript save before the pilot request writes without re-indexing search."""
from types import SimpleNamespace

import harness.input_receipts as ir
import harness.session_fts as fts
from harness.sessions import load_transcript


def test_publish_session_injected_writes_but_does_not_index(tmp_path, monkeypatch):
    indexed = []
    monkeypatch.setattr(fts, "index_session_transcript", lambda *a, **k: indexed.append(a) or True)
    history = [{"role": "user", "content": "hello"}]
    session = SimpleNamespace(state_dir=str(tmp_path), harness_session_id="sess-1",
                              export_transcript_data=lambda: history, _busy_meta=None)
    published = []
    monkeypatch.setattr(ir, "session_input_store",
                        lambda s: SimpleNamespace(publish_injected=lambda ids, t: published.append(ids)))
    ir.publish_session_injected(session, ["in-1"])
    assert load_transcript(str(tmp_path), "sess-1") == history
    assert published == [["in-1"]]
    assert indexed == []
