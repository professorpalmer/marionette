from __future__ import annotations

from types import SimpleNamespace

from harness.log_reconstruction import check_outbound_reconstruction


def test_matching_rebuild_is_honest():
    messages = [{"role": "user", "content": "hi"}]
    session = SimpleNamespace(
        _append_only=True,
        _frozen_system_prompt="sys",
        _history=[{"role": "system", "content": "sys"}] + messages,
        _elide_stale_reads=lambda msgs: list(msgs),
    )
    assert check_outbound_reconstruction(session, messages, "sys") is True
    assert session._last_log_reconstruction["ok"] is True


def test_message_drift_is_logged_not_raised():
    session = SimpleNamespace(
        _append_only=True,
        _frozen_system_prompt="sys",
        _history=[{"role": "system", "content": "sys"}, {"role": "user", "content": "rebuilt"}],
        _elide_stale_reads=lambda msgs: list(msgs),
    )
    assert check_outbound_reconstruction(
        session, [{"role": "user", "content": "stale"}], "sys",
    ) is False
    assert session._last_log_reconstruction["reason"] == "messages"


def test_frozen_system_mismatch_is_logged_when_append_only():
    messages = [{"role": "user", "content": "hi"}]
    session = SimpleNamespace(
        _append_only=True,
        _frozen_system_prompt="frozen",
        _history=[{"role": "system", "content": "frozen"}] + messages,
        _elide_stale_reads=lambda msgs: list(msgs),
    )
    assert check_outbound_reconstruction(session, messages, "other") is False
    assert session._last_log_reconstruction["reason"] == "system"


def test_check_never_raises_into_the_turn():
    session = SimpleNamespace(
        _append_only=True,
        _frozen_system_prompt="sys",
        _history=[{"role": "system", "content": "sys"}],
    )

    def _boom(_msgs):
        raise RuntimeError("elide failed")

    session._elide_stale_reads = _boom
    assert check_outbound_reconstruction(session, [], "sys") is True


def test_stub_without_history_does_not_call_messages_for_provider():
    seen = {"n": 0}

    class _Session:
        _append_only = False
        _frozen_system_prompt = None

        def _messages_for_provider(self):
            seen["n"] += 1
            return [{"role": "user", "content": "hi"}]

    session = _Session()
    outbound = [{"role": "user", "content": "hi"}]
    assert check_outbound_reconstruction(session, outbound, "sys") is True
    assert seen["n"] == 0
