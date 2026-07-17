"""Smoke tests for the LocalJobsMixin extraction.

Guards the mechanical move of local-job register/finish/persist/cancel helpers
out of harness.conversation into harness.local_jobs. If the class-hierarchy
wiring or the MRO ever regresses, these fail loudly.
"""

from harness.conversation import ConversationalSession
from harness.local_jobs import LocalJobsMixin


MOVED_METHODS = (
    "_register_local_job",
    "_finish_local_job",
    "_persist_local_jobs_locked",
    "_persist_local_jobs",
    "_load_local_jobs",
    "cancel_local_job",
    "_local_job_cancelled",
    "live_local_jobs",
)


def test_session_inherits_mixin():
    assert issubclass(ConversationalSession, LocalJobsMixin)
    assert LocalJobsMixin in ConversationalSession.__mro__


def test_moved_methods_present_on_session():
    for name in MOVED_METHODS:
        assert hasattr(ConversationalSession, name), name
        attr = getattr(ConversationalSession, name)
        assert callable(attr), name


def test_moved_methods_resolve_to_mixin():
    for name in MOVED_METHODS:
        attr = getattr(ConversationalSession, name)
        assert attr.__qualname__ == f"LocalJobsMixin.{name}", (
            name,
            attr.__qualname__,
        )


def test_mixin_defines_no_init():
    assert "__init__" not in LocalJobsMixin.__dict__


def test_history_cap_lives_on_mixin():
    assert "_LOCAL_JOBS_HISTORY_CAP" in LocalJobsMixin.__dict__
    assert ConversationalSession._LOCAL_JOBS_HISTORY_CAP == 200


def test_drain_swarm_results_stays_on_session():
    # Busy/swarm drain must not have been swept into LocalJobsMixin.
    attr = getattr(ConversationalSession, "drain_swarm_results")
    assert attr.__qualname__ == "ConversationalSession.drain_swarm_results", (
        attr.__qualname__,
    )


def test_session_cancel_stays_on_session():
    # Session-level cancel stays on ConversationalSession; interrupt lives on
    # BusyControlMixin. Only cancel_local_job moved with LocalJobsMixin.
    cancel = getattr(ConversationalSession, "cancel")
    assert cancel.__qualname__ == "ConversationalSession.cancel", cancel.__qualname__
    interrupt = getattr(ConversationalSession, "interrupt")
    assert interrupt.__qualname__ == "BusyControlMixin.interrupt", interrupt.__qualname__
