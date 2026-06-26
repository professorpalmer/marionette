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


def test_puppetmaster_cmd_console_script(monkeypatch):
    # Mock shutil.which to find puppetmaster console script
    def mock_which(cmd):
        if cmd == "puppetmaster":
            return "/mocked/bin/puppetmaster"
        return None

    monkeypatch.setattr(shutil, "which", mock_which)

    cmd = _puppetmaster_cmd("codegraph", "status")
    assert cmd == ["/mocked/bin/puppetmaster", "codegraph", "status"]


def test_puppetmaster_cmd_python_m(monkeypatch):
    # Mock shutil.which to not find puppetmaster console script
    def mock_which(cmd):
        return None

    monkeypatch.setattr(shutil, "which", mock_which)

    # Let's mock _puppetmaster_python to return a dummy path
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


def test_startup_auto_index_skips_when_exists(monkeypatch):
    # Setup a temp repo containing .codegraph/
    temp_dir = tempfile.mkdtemp()
    os.makedirs(os.path.join(temp_dir, ".codegraph"), exist_ok=True)

    try:
        monkeypatch.setattr(server._cfg, "repo", temp_dir)
        monkeypatch.setattr(server, "_puppetmaster_available", lambda: True)

        # Mock _index_codegraph_bg
        mock_index_bg = MagicMock()
        monkeypatch.setattr(server, "_index_codegraph_bg", mock_index_bg)

        # Run the auto index startup check
        server._maybe_auto_index_codegraph()

        # It should check and set status to ready, and not call _index_codegraph_bg
        assert server._codegraph_status == "ready"
        mock_index_bg.assert_not_called()

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


def test_puppetmaster_cmd_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    fake_exe = "/mocked/frozen/pmharness"
    monkeypatch.setattr(sys, "executable", fake_exe)
    cmd = _puppetmaster_cmd("codegraph", "init", "--index")
    assert cmd == [fake_exe, "pm-exec", "codegraph", "init", "--index"]


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

