"""The background CodeGraph refresh starts Puppetmaster's warm explore helper."""
import time

import harness.api.codegraph_index as cgi


def test_fresh_index_prewarms_the_helper(monkeypatch, tmp_path):
    calls = []
    import puppetmaster.codegraph as pmcg
    monkeypatch.setattr(pmcg, "prewarm_codegraph_context", lambda repo: calls.append(repo) or True, raising=False)
    monkeypatch.setattr(cgi, "codegraph_is_stale", lambda repo: False)
    monkeypatch.setattr(cgi, "codegraph_status", "ready")
    monkeypatch.setattr(cgi, "codegraph_stale_check_at", {})
    monkeypatch.setattr(cgi, "codegraph_fail_until", {})
    cgi.maybe_refresh_codegraph(str(tmp_path))
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == [str(tmp_path)]


def test_older_puppetmaster_without_prewarm_is_a_no_op(monkeypatch):
    import puppetmaster.codegraph as pmcg
    monkeypatch.delattr(pmcg, "prewarm_codegraph_context", raising=False)
    cgi.prewarm_codegraph_context("/nowhere")
