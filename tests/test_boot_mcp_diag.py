"""Regression: MCP boot is lazy (config only) and records via diag.note."""
from __future__ import annotations

import harness.server as srv


def test_boot_mcp_defers_start_and_records_names(monkeypatch):
    notes: list[tuple] = []
    started = {"n": 0}

    def fake_note(where, exc=None, msg=""):
        notes.append((where, exc, msg))

    class FakeMcp:
        def effective_config(self):
            return {"docker": {}, "wiki": {}}

        def start_all(self):
            started["n"] += 1
            return {"docker": "connection refused", "wiki": "ok"}

    # Exercise the production helper — do not reimplement the nested body.
    srv.boot_mcp_servers(mcp=FakeMcp(), diag=fake_note)

    assert started["n"] == 0, "boot must not call start_all"
    assert ("mcp.boot_deferred", None, "docker, wiki") in notes
    assert all(n[0] != "mcp.boot_error" for n in notes)
    assert all(isinstance(n[1], (type(None), BaseException)) for n in notes)


def test_boot_mcp_records_boot_fail_with_exc_kw(monkeypatch):
    notes: list[tuple] = []

    def fake_note(where, exc=None, msg=""):
        notes.append((where, exc, msg))

    class FakeMcp:
        def effective_config(self):
            raise RuntimeError("boom")

        def start_all(self):
            raise AssertionError("start_all must not be called on boot")

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
        def effective_config(self):
            return {"local": {}}

        def start_all(self):
            raise AssertionError("start_all must not be called on boot")

    monkeypatch.setattr(srv, "_diag", fake_note)
    monkeypatch.setattr(srv, "_mcp", FakeMcp())

    srv.boot_mcp_servers()

    assert ("mcp.boot_deferred", None, "local") in notes
