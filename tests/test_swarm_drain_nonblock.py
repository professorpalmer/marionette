"""drain_swarm_results must NOT block on the _busy lock. It's called from an HTTP
handler (the frontend swarm-results poll); a blocking acquire there hangs the
server thread whenever a turn holds the lock -- the 'swarm running forever / app
hung' symptom. It must return immediately (draining nothing) when busy, and the
queued results survive for the next poll."""
import tempfile
import threading

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


def test_drain_does_not_block_when_busy():
    s = _session()
    # Simulate an in-flight turn holding the lock.
    s._busy.acquire(blocking=False)
    try:
        done = threading.Event()

        def call_drain():
            list(s.drain_swarm_results())  # must return immediately, not block
            done.set()

        t = threading.Thread(target=call_drain, daemon=True)
        t.start()
        # If drain blocks on the held lock, this times out (the bug).
        assert done.wait(timeout=2.0), "drain_swarm_results blocked while _busy was held"
    finally:
        s._busy.release()


def test_drain_works_when_free():
    s = _session()
    # Lock free -> drain runs (yields nothing since the queue is empty, no error).
    out = list(s.drain_swarm_results())
    assert out == []


def test_drain_injects_pilot_resume_continuation():
    """A finished background result must not just land silently. After draining,
    a user-role continuation message re-activates the pilot so it reports the
    finding and takes the next step without a new user message, and a
    pilot_resume event is emitted for the UI."""
    s = _session()
    s._swarm_results.put({
        "job_id": "abc123",
        "objective": "fix the parser",
        "result": {
            "applied": True,
            "files": ["parser.py"],
            "summary": "patched the tokenizer",
        },
    })
    events = list(s.drain_swarm_results())
    kinds = [e.kind for e in events]
    assert "swarm_result" in kinds
    assert "pilot_resume" in kinds

    # Raw result recorded as an assistant message...
    assert any(
        m["role"] == "assistant" and "[swarm result for: fix the parser]" in m["content"]
        for m in s._history
    )
    # ...and a user-role continuation re-activates the pilot.
    resume = [
        m for m in s._history
        if m["role"] == "user" and "[background job abc123 finished]" in m["content"]
    ]
    assert resume, "expected a user-role pilot-resume continuation in history"
    assert "without waiting for the user to ask" in resume[0]["content"]


def test_drain_persists_swarm_badge_to_display_transcript():
    """The green/red 'swarm done / swarm failed' badge must survive a session
    reload: the live ConvEvent only reaches a renderer that is open right now,
    so the outcome is also recorded in the display transcript."""
    s = _session()
    s._swarm_results.put({
        "job_id": "abc123",
        "objective": "fix the parser",
        "result": {
            "applied": True,
            "files": ["parser.py"],
            "summary": "patched the tokenizer",
        },
    })
    list(s.drain_swarm_results())
    badges = [d for d in s.export_display_transcript() if d.get("type") == "swarm_result"]
    assert badges == [{
        "type": "swarm_result",
        "job_id": "abc123",
        "applied": True,
        "files": ["parser.py"],
        "summary": "patched the tokenizer",
        "error": None,
        "objective": "fix the parser",
    }]
