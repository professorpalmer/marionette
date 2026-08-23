from __future__ import annotations

from harness.api.secrets import (
    SecretServices,
    get_secrets_presence,
    post_secrets_dismiss,
    post_secrets_submit,
)


class _Runner:
    def __init__(self):
        self.harness_session_id = "sess-a"
        self.calls = []

    def decide_secret_request(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


def test_submit_stores_and_resumes_without_secret_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    runner = _Runner()
    svc = SecretServices(get_runners=lambda: {"sess-a": runner})
    status, payload = post_secrets_submit(
        {
            "session_id": "sess-a",
            "connector": "pypi",
            "field": "token",
            "value": "pypi-live-token-should-not-echo",
        },
        svc,
    )
    assert status == 200
    assert payload["provided"] is True
    assert payload["resume"] is True
    assert payload["connector"] == "pypi"
    assert "pypi-live-token-should-not-echo" not in str(payload)
    assert runner.calls == [{"connector": "pypi", "field": "token", "provided": True}]
    st, listed = get_secrets_presence(
        {"session_id": "sess-a", "connector": "pypi", "field": "token"},
        svc,
    )
    assert st == 200
    assert listed["present"] is True
    assert listed["state"] == "present"
    assert "pypi-live-token" not in str(listed)


def test_dismiss_does_not_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    runner = _Runner()
    svc = SecretServices(get_runners=lambda: {"sess-a": runner})
    status, payload = post_secrets_dismiss(
        {"session_id": "sess-a", "connector": "pypi", "field": "token"},
        svc,
    )
    assert status == 200
    assert payload["provided"] is False
    assert payload["resume"] is False
    assert runner.calls[0]["provided"] is False
