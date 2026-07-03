import os
import sys
import shutil
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock
import pytest

from harness._exec import (
    _puppetmaster_python,
    _puppetmaster_available,
    _puppetmaster_cmd,
    _clear_puppetmaster_cache
)
import harness.server as server


@pytest.fixture(autouse=True)
def reset_cache():
    _clear_puppetmaster_cache()
    # Reset server state variables too
    server._startup_index_fired = False
    server._codegraph_status = "unsupported"
    server._codegraph_status_reason = None
    yield
    _clear_puppetmaster_cache()
    server._startup_index_fired = False
    server._codegraph_status = "unsupported"
    server._codegraph_status_reason = None


def test_puppetmaster_cmd_prefers_running_interpreter():
    # Primary path: when the backend's own interpreter can import puppetmaster,
    # run it directly via sys.executable -- never a PATH `puppetmaster` shim
    # (pyenv shims resolve to a different Python and exit 127).
    import importlib.util as _ilu
    with patch.object(_ilu, "find_spec", return_value=object()):
        cmd = _puppetmaster_cmd("codegraph", "status")
        assert cmd == [sys.executable, "-m", "puppetmaster", "codegraph", "status"]


def test_puppetmaster_cmd_console_script(monkeypatch):
    # Fallback: running interpreter CANNOT import puppetmaster -> use the console
    # script found on PATH.
    import importlib.util as _ilu
    monkeypatch.setattr(_ilu, "find_spec", lambda name: None)

    def mock_which(cmd):
        if cmd == "puppetmaster":
            return "/mocked/bin/puppetmaster"
        return None

    monkeypatch.setattr(shutil, "which", mock_which)

    cmd = _puppetmaster_cmd("codegraph", "status")
    assert cmd == ["/mocked/bin/puppetmaster", "codegraph", "status"]


def test_puppetmaster_cmd_python_m(monkeypatch):
    # Fallback: running interpreter can't import puppetmaster AND no console
    # script -> run `-m puppetmaster` under a resolved interpreter.
    import importlib.util as _ilu
    monkeypatch.setattr(_ilu, "find_spec", lambda name: None)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    with patch("harness._exec._puppetmaster_python", return_value="/mocked/python"):
        cmd = _puppetmaster_cmd("codegraph", "status")
        assert cmd == ["/mocked/python", "-m", "puppetmaster", "codegraph", "status"]


def test_puppetmaster_python_env_override(monkeypatch):
    # Create a dummy temporary file to act as the python interpreter
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = tmp.name

    try:
        monkeypatch.setenv("PMHARNESS_PYTHON", tmp_path)
        python_bin = _puppetmaster_python()
        assert python_bin == tmp_path
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def test_puppetmaster_python_dev_not_frozen(monkeypatch):
    monkeypatch.delenv("PMHARNESS_PYTHON", raising=False)
    # Ensuregetattr(sys, "frozen") is False and basename looks like python
    with patch("sys.frozen", False, create=True), \
         patch("sys.executable", "/usr/bin/python3"):
        python_bin = _puppetmaster_python()
        assert python_bin == "/usr/bin/python3"


def test_puppetmaster_available_caches(monkeypatch):
    monkeypatch.delenv("PMHARNESS_PYTHON", raising=False)
    
    # Let's count calls to shutil.which
    which_calls = []
    orig_which = shutil.which
    def mock_which(cmd):
        which_calls.append(cmd)
        return None

    monkeypatch.setattr(shutil, "which", mock_which)
    
    # Mock subprocess.run to avoid hitting real python
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        
        # First call should trigger checks
        avail1 = _puppetmaster_available()
        # Second call should use cache
        avail2 = _puppetmaster_available()
        
        assert avail1 is avail2
        
        # Subsequent calls should not call shutil.which again
        count_before = len(which_calls)
        _puppetmaster_available()
        assert len(which_calls) == count_before


def test_startup_auto_index_skips_when_indexed(monkeypatch):
    # A repo with a BUILT CodeGraph DB (not just the .codegraph/ dir) is ready
    # and must not be re-indexed on boot.
    temp_dir = tempfile.mkdtemp()
    cg_dir = os.path.join(temp_dir, ".codegraph")
    os.makedirs(cg_dir, exist_ok=True)
    with open(os.path.join(cg_dir, "codegraph.db"), "wb") as f:
        f.write(b"\x00")  # presence of the DB file is what marks "indexed"

    try:
        monkeypatch.setattr(server._cfg, "repo", temp_dir)
        monkeypatch.setattr(server, "_puppetmaster_available", lambda: True)

        mock_index_bg = MagicMock()
        monkeypatch.setattr(server, "_index_codegraph_bg", mock_index_bg)

        server._maybe_auto_index_codegraph()

        assert server._codegraph_status == "ready"
        mock_index_bg.assert_not_called()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_startup_auto_index_fires_when_dir_present_but_no_db(monkeypatch):
    # A .codegraph/ dir with config.json but no DB (init'd, never indexed) must
    # still trigger a background index -- the fresh-install bug that showed
    # "unsupported" because status hung on a DB-less checkout.
    temp_dir = tempfile.mkdtemp()
    cg_dir = os.path.join(temp_dir, ".codegraph")
    os.makedirs(cg_dir, exist_ok=True)
    with open(os.path.join(cg_dir, "config.json"), "w") as f:
        f.write("{}")

    try:
        monkeypatch.setattr(server._cfg, "repo", temp_dir)
        monkeypatch.setattr(server, "_puppetmaster_available", lambda: True)

        mock_index_bg = MagicMock()
        monkeypatch.setattr(server, "_index_codegraph_bg", mock_index_bg)

        server._maybe_auto_index_codegraph()
        time.sleep(0.1)  # let the daemon thread run

        mock_index_bg.assert_called_once_with(temp_dir)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_startup_auto_index_fires_when_not_exists(monkeypatch):
    # Setup a temp repo that does NOT contain .codegraph/
    temp_dir = tempfile.mkdtemp()

    try:
        monkeypatch.setattr(server._cfg, "repo", temp_dir)
        monkeypatch.setattr(server, "_puppetmaster_available", lambda: True)

        # Mock _index_codegraph_bg
        mock_index_bg = MagicMock()
        monkeypatch.setattr(server, "_index_codegraph_bg", mock_index_bg)

        # Run the auto index startup check
        server._maybe_auto_index_codegraph()

        # Give the background daemon thread a tiny moment to execute
        time.sleep(0.1)

        # It should call _index_codegraph_bg
        mock_index_bg.assert_called_once_with(temp_dir)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_puppetmaster_cmd_frozen_no_external_python(monkeypatch):
    # Pure-DMG install: no external Python can import puppetmaster, so the frozen
    # binary re-enters itself via `pm-exec` (self-contained fallback).
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_exe = "/mocked/frozen/pmharness"
    monkeypatch.setattr(sys, "executable", fake_exe)
    with patch("harness._exec._external_puppetmaster_python", return_value=""):
        cmd = _puppetmaster_cmd("codegraph", "init", "--index")
    assert cmd == [fake_exe, "pm-exec", "codegraph", "init", "--index"]


def test_puppetmaster_cmd_frozen_prefers_external_python(monkeypatch):
    # When a real external Python with puppetmaster exists (dev / editable venv),
    # a frozen app runs workers through it against the LIVE source instead of the
    # stale PYZ snapshot -- the fix for the "zlib incorrect header check" +
    # "cannot import name 'WorkerResult'" implement-worker failures.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/mocked/frozen/pmharness")
    with patch("harness._exec._external_puppetmaster_python", return_value="/real/venv/bin/python"):
        cmd = _puppetmaster_cmd("codegraph", "init", "--index")
    assert cmd == ["/real/venv/bin/python", "-m", "puppetmaster", "codegraph", "init", "--index"]


def test_external_puppetmaster_python_prefers_env_override(monkeypatch):
    monkeypatch.setenv("PMHARNESS_PYTHON", "/target/repo/.venv/bin/python")
    _clear_puppetmaster_cache()

    def fake_run(cmd, **kwargs):
        # Accept the env-override interpreter as puppetmaster-capable.
        return MagicMock(returncode=0 if cmd[0] == "/target/repo/.venv/bin/python" else 1)

    with patch("os.path.isabs", return_value=True), \
         patch("os.path.exists", return_value=True), \
         patch("subprocess.run", side_effect=fake_run):
        from harness._exec import _external_puppetmaster_python
        assert _external_puppetmaster_python() == "/target/repo/.venv/bin/python"


def test_external_puppetmaster_python_none_when_no_puppetmaster(monkeypatch):
    monkeypatch.delenv("PMHARNESS_PYTHON", raising=False)
    _clear_puppetmaster_cache()

    with patch("shutil.which", return_value="/usr/bin/python3"), \
         patch("os.path.exists", return_value=True), \
         patch("subprocess.run", return_value=MagicMock(returncode=1)):
        from harness._exec import _external_puppetmaster_python
        assert _external_puppetmaster_python() == ""


def test_puppetmaster_available_frozen_success(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _clear_puppetmaster_cache()
    assert _puppetmaster_available() is True


def test_puppetmaster_available_frozen_failure(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _clear_puppetmaster_cache()
    with patch.dict(sys.modules, {"puppetmaster": None}):
        assert _puppetmaster_available() is False


def test_harness_cli_pm_exec_dispatch(monkeypatch):
    from harness.cli import main as harness_main
    import puppetmaster.cli

    mock_pm_main = MagicMock(return_value=42)
    monkeypatch.setattr(puppetmaster.cli, "main", mock_pm_main)

    code = harness_main(["pm-exec", "codegraph", "init", "--index"])
    assert code == 42
    mock_pm_main.assert_called_once_with(["codegraph", "init", "--index"])

