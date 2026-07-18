"""Regression: MCP boot thread must record errors via diag.note, not TypeError."""
from __future__ import annotations

import harness.server as srv


def test_boot_mcp_records_string_errors_with_msg_kw(monkeypatch):
    notes: list[tuple] = []

    def fake_note(where, exc=None, msg=""):
        notes.append((where, exc, msg))

    class FakeMcp:
        def start_all(self):
            return {"docker": "connection refused", "wiki": "ok"}

    # Exercise the production helper — do not reimplement the nested body.
    srv.boot_mcp_servers(mcp=FakeMcp(), diag=fake_note)

    assert ("mcp.boot_error", None, "docker: connection refused") in notes
    assert all(isinstance(n[1], (type(None), BaseException)) for n in notes)


def test_boot_mcp_records_boot_fail_with_exc_kw(monkeypatch):
    notes: list[tuple] = []

    def fake_note(where, exc=None, msg=""):
        notes.append((where, exc, msg))

    class FakeMcp:
        def start_all(self):
            raise RuntimeError("boom")

    srv.boot_mcp_servers(mcp=FakeMcp(), diag=fake_note)

    assert len(notes) == 1
    where, exc, msg = notes[0]
    assert where == "mcp.boot_fail"
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "boom"
    assert msg == ""


def test_boot_mcp_servers_uses_module_defaults(monkeypatch):
    notes: list[tuple] = []

    def fake_note(where, exc=None, msg=""):
        notes.append((where, exc, msg))

    class FakeMcp:
        def start_all(self):
            return {"local": "timeout"}

    monkeypatch.setattr(srv, "_diag", fake_note)
    monkeypatch.setattr(srv, "_mcp", FakeMcp())

    srv.boot_mcp_servers()

    assert ("mcp.boot_error", None, "local: timeout") in notes
