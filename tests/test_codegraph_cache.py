"""Tests for the per-message CodeGraph slice cache that avoids re-running the
blocking codegraph_context subprocess on every step of a multi-step turn."""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def test_codegraph_cache_fields_initialized():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    assert s._cg_cache_key is None
    assert s._cg_cache_section == ""
    assert s._cg_cache_symbols == 0


def test_codegraph_cache_reused_for_same_message(monkeypatch):
    """The codegraph_context subprocess must run at most once per user message,
    even across many pilot steps in the same turn."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)

    calls = {"n": 0}

    def fake_context(task, cwd, **kw):
        calls["n"] += 1
        return "- **sym_a** something\n#### Header\nrelated"

    import puppetmaster.codegraph as cg
    monkeypatch.setattr(cg, "codegraph_context", fake_context)
    monkeypatch.setattr(cg, "codegraph_prompt_section", lambda s: "SECTION\n" + s)

    # Emulate the per-step cache lookup the turn loop performs.
    def step_get(user_message):
        if s._cg_cache_key == user_message:
            return "cache"
        from puppetmaster.codegraph import codegraph_context, codegraph_prompt_section
        sl = codegraph_context(task=user_message, cwd=s.config.repo)
        sec = ""
        sym = 0
        if sl:
            sym = sl.count("- **") + sl.count("#### ")
            sec = "AUTH\n" + codegraph_prompt_section(sl)
        s._cg_cache_key = user_message
        s._cg_cache_section = sec
        s._cg_cache_symbols = sym
        return "compute"

    msg = "do a multi-step task"
    sources = [step_get(msg) for _ in range(5)]
    assert calls["n"] == 1, "subprocess should run exactly once for repeated steps"
    assert sources[0] == "compute"
    assert all(src == "cache" for src in sources[1:])
    assert s._cg_cache_symbols == 2


def test_codegraph_cache_recomputes_for_new_message(monkeypatch):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)

    calls = {"n": 0}

    def fake_context(task, cwd, **kw):
        calls["n"] += 1
        return "- **x** y"

    import puppetmaster.codegraph as cg
    monkeypatch.setattr(cg, "codegraph_context", fake_context)
    monkeypatch.setattr(cg, "codegraph_prompt_section", lambda s: s)

    def step_get(user_message):
        if s._cg_cache_key == user_message:
            return
        from puppetmaster.codegraph import codegraph_context
        codegraph_context(task=user_message, cwd=s.config.repo)
        s._cg_cache_key = user_message

    step_get("message one")
    step_get("message one")
    step_get("message two")
    assert calls["n"] == 2, "a new message must recompute the slice"


def test_repeated_ask_points_at_the_codegraph_slice_already_in_history(monkeypatch):
    """A retry or Continue with the same ask must not stack another ~4.7K-char slice."""
    from harness.conversation import CG_SECTION_UNCHANGED

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)
    s._task_profile = "standard"
    import puppetmaster.codegraph as cg
    monkeypatch.setattr(cg, "codegraph_context", lambda task, cwd, **kw: "- **sym_a** something")
    monkeypatch.setattr("harness.task_profile.profile_skips_codegraph", lambda *a, **k: False)

    first = s._build_turn_cg_section("refactor the uploader")
    assert first and first != CG_SECTION_UNCHANGED
    s._history.append({"role": "user", "content": "refactor the uploader\n\n" + first})
    assert s._build_turn_cg_section("refactor the uploader") == CG_SECTION_UNCHANGED

    # Once compaction drops the copy, the full slice is sent again.
    s._history = [m for m in s._history if first not in (m.get("content") or "")]
    assert s._build_turn_cg_section("refactor the uploader") == first


def test_repeated_ask_does_not_restack_wiki_vault_or_skill_sections(monkeypatch):
    """Retry / Continue of the same ask: one wiki search and one copy of each
    trailer section in append-only history, not one per send."""
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)
    s._task_profile = "standard"
    monkeypatch.setattr("harness.task_profile.profile_skips_codegraph", lambda *a, **k: True)
    monkeypatch.setattr("harness.task_profile.profile_skips_wiki", lambda *a, **k: False)

    searches = []

    class Wiki:
        configured = True

        def search_pages(self, query, limit=5):
            searches.append(query)
            return [{"slug": "uploader-decision", "title": "Uploader decision",
                     "snippet": "We chose chunked uploads in 2026 because retries were cheap."}]

        def page_body(self, slug):
            return ""

    s._wiki = Wiki()
    monkeypatch.setattr(s, "_build_turn_vault_section",
                        lambda msg: "### Vault recall\n- earlier: uploader keeps 5 MiB chunks")
    import harness.conversation as conv
    monkeypatch.setattr(conv, "format_retrieved_skill_bodies",
                        lambda retrieved: "### Skill: uploads\nAlways stream to disk first.")

    ask = "refactor the uploader"
    first = s._append_turn_context_trailer(ask, ask)
    assert "Uploader decision" in first and "5 MiB chunks" in first and "stream to disk" in first
    s._history.append({"role": "user", "content": first})

    second = s._append_turn_context_trailer(ask, ask)
    assert len(searches) == 1
    for text in ("Uploader decision", "5 MiB chunks", "stream to disk"):
        assert text not in second
    for kind in ("Wiki", "Vault", "Skill"):
        assert f"[{kind} context unchanged from the previous turn -- see above]" in second


def test_codegraph_and_wiki_lookups_run_side_by_side(monkeypatch):
    """The turn waits for the slower of the two lookups, not their sum."""
    import time as _time
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)
    monkeypatch.setattr("harness.task_profile.profile_skips_codegraph", lambda *a, **k: False)
    monkeypatch.setattr("harness.task_profile.profile_skips_wiki", lambda *a, **k: False)

    def slow(label):
        def build(msg):
            _time.sleep(0.4)
            return f"### {label} for {msg}"
        return build

    monkeypatch.setattr(s, "_build_turn_cg_section", slow("CodeGraph"))
    monkeypatch.setattr(s, "_build_turn_wiki_section", slow("Wiki"))
    monkeypatch.setattr(s, "_build_turn_vault_section", lambda msg: "")
    started = _time.monotonic()
    out = s._append_turn_context_trailer("fix it", "fix it")
    elapsed = _time.monotonic() - started
    assert out.index("### CodeGraph for fix it") < out.index("### Wiki for fix it")
    assert elapsed < 0.7, elapsed


def test_a_hung_lookup_cannot_hold_the_turn(monkeypatch):
    """A stuck CodeGraph or wiki call costs the turn its slice, not 30 seconds."""
    import threading
    import time as _time
    import harness.conversation as conv
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)
    monkeypatch.setattr("harness.task_profile.profile_skips_codegraph", lambda *a, **k: False)
    monkeypatch.setattr("harness.task_profile.profile_skips_wiki", lambda *a, **k: False)
    monkeypatch.setattr(conv, "TURN_CONTEXT_BUDGET_S", 0.3, raising=False)
    release = threading.Event()

    def hung(msg):
        release.wait(10)
        return "### CodeGraph late"

    monkeypatch.setattr(s, "_build_turn_cg_section", hung)
    monkeypatch.setattr(s, "_build_turn_wiki_section", lambda msg: "### Wiki on time")
    monkeypatch.setattr(s, "_build_turn_vault_section", lambda msg: "")
    started = _time.monotonic()
    try:
        out = s._append_turn_context_trailer("fix it", "fix it")
    finally:
        release.set()
    assert _time.monotonic() - started < 1.5
    assert "### Wiki on time" in out
    assert "CodeGraph late" not in out


def test_a_hung_codegraph_cannot_hold_a_hosted_turn(tmp_path, monkeypatch):
    """Per-step path (hosted providers): same budget, lookups side by side."""
    import threading
    import time as _time
    from types import SimpleNamespace
    import harness.conversation as conv
    import puppetmaster.codegraph as cg
    from pmharness.drivers.base import DriverResponse

    seen = {}

    class Pilot:
        name = "scripted"

        def chat(self, messages, tools=None, system=None):
            seen["system"] = (system or "") + "".join(str(m.get("content")) for m in messages)
            return DriverResponse(text='{"say": "done", "actions": []}', tokens_out=1,
                                  latency_ms=1.0, meta={"finish_reason": "stop"})

        def complete(self, prompt, system=None):
            return self.chat([])

    monkeypatch.setattr("harness.send_loop.profile_skips_auto_inject",
                        lambda session, user_message="": (False, False))
    monkeypatch.setattr(conv, "TURN_CONTEXT_BUDGET_S", 0.3, raising=False)
    release = threading.Event()
    monkeypatch.setattr(cg, "codegraph_context", lambda *a, **k: release.wait(60) and "late")
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path), repo=str(tmp_path)))
    s.pilot = Pilot()
    monkeypatch.setattr(s, "_resolve_append_only", lambda: False)
    monkeypatch.setattr(s, "_maybe_compact_history", lambda **k: iter(()))
    # The per-turn `codegraph search` subprocess (auto_codegraph) hangs too.
    monkeypatch.setattr(s, "_get_codegraph_context", lambda query: release.wait(60) and "late")
    monkeypatch.setattr(s, "_wiki", SimpleNamespace(configured=True))
    monkeypatch.setattr(s, "_build_turn_wiki_section", lambda msg: "### Wiki on time")
    started = _time.monotonic()
    try:
        events = list(s.send("fix the parser"))
    finally:
        release.set()
    assert any(e.kind == "assistant_done" for e in events)
    # Far below the 60 s hang; whole-turn overhead on a loaded Windows
    # runner has measured 2.8 s, so wall time is not a tighter bound.
    assert _time.monotonic() - started < 10.0
    assert "### Wiki on time" in seen["system"]
