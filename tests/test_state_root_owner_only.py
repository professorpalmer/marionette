from __future__ import annotations

"""The backend makes ~/.pmharness and its state dir owner-only on POSIX.

Files under the root (tool output, memory, prompt queues) are written with
the umask, often 644. A default home can be read by other local accounts,
so the root directory is the one gate.
"""

import os
import stat
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX modes")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _start_backend(home, state_dir):
    env = dict(os.environ, HOME=str(home), HARNESS_STATE_DIR=str(state_dir))
    code = (
        "from harness.secure_files import restrict_install_root\n"
        "import os; restrict_install_root(os.environ['HARNESS_STATE_DIR'])\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True, timeout=120)


def test_default_root_and_state_become_owner_only(tmp_path):
    root = tmp_path / ".pmharness"
    state = root / "state"
    state.mkdir(parents=True)
    os.chmod(root, 0o755)
    os.chmod(state, 0o755)
    _start_backend(tmp_path, state)
    assert _mode(root) == 0o700
    assert _mode(state) == 0o700


def test_state_dir_outside_the_root_is_left_alone(tmp_path):
    (tmp_path / ".pmharness").mkdir()
    os.chmod(tmp_path / ".pmharness", 0o755)
    custom = tmp_path / "custom-state"
    custom.mkdir()
    os.chmod(custom, 0o755)
    _start_backend(tmp_path, custom)
    assert _mode(custom) == 0o755
    assert _mode(tmp_path / ".pmharness") == 0o755


def test_server_startup_hardens_the_root():
    text = open(os.path.join(ROOT, "harness", "server.py"), encoding="utf-8").read()
    assert "restrict_install_root(" in text
