"""CLI `gui` subcommand dispatch + default-task path coexistence."""
import threading
import time
import urllib.request
import json

from harness import cli


def test_default_path_still_runs_task(capsys, tmp_path):
    code = cli.main(["--driver", "stub-oracle-v2", "--state-dir", str(tmp_path),
                     "What does JSON stand for?"])
    assert "FINAL [answer]" in capsys.readouterr().out
    assert code == 0


def test_gui_subcommand_starts_server(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    # _run_gui calls serve(); monkeypatch serve to capture args without blocking
    captured = {}
    def fake_serve(host="127.0.0.1", port=8799, force=False, lifetime_receipt=None):
        assert lifetime_receipt is None
        captured["host"] = host; captured["port"] = port; captured["force"] = force
    monkeypatch.setattr("harness.server.serve", fake_serve)
    code = cli.main(["gui", "--port", "8910", "--host", "127.0.0.1"])
    assert code == 0
    assert captured == {"host": "127.0.0.1", "port": 8910, "force": False}

    # test with --force
    cli.main(["gui", "--port", "8910", "--host", "127.0.0.1", "--force"])
    assert captured == {"host": "127.0.0.1", "port": 8910, "force": True}
