"""Hermetic tests for the AUTO-VERIFY module (harness.verify).

No network, no puppetmaster: harness.verify is stdlib-only and imports nothing
heavy. subprocess is monkeypatched so run_verify never actually spawns a
process. Assertions cover:
  - detect_verify_command picks a scoped tsc for a package.json/webapp repo
  - detect_verify_command picks a python syntax check for a pyproject repo
  - run_verify returns (False, output) on nonzero exit and (True, ...) on zero
  - the changed-file scoping builds the expected commands
"""
import os
import subprocess
import sys

import pytest

from harness import verify


# ---------------------------------------------------------------------------
# helpers to build tiny fake repos on disk
# ---------------------------------------------------------------------------
def _write(root, relpath, content=""):
    full = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return full


def _make_web_repo(tmp_path):
    root = str(tmp_path)
    _write(root, "package.json", '{"scripts": {"typecheck": "tsc --noEmit"}}')
    _write(root, "tsconfig.json", "{}")
    _write(root, "src/app.tsx", "export const x = 1;\n")
    return root


def _make_webapp_subdir_repo(tmp_path):
    root = str(tmp_path)
    _write(root, "webapp/package.json", '{"scripts": {"build": "vite build"}}')
    _write(root, "webapp/tsconfig.json", "{}")
    _write(root, "webapp/src/app.tsx", "export const x = 1;\n")
    return root


def _make_python_repo(tmp_path):
    root = str(tmp_path)
    _write(root, "pyproject.toml", "[project]\nname='x'\n")
    _write(root, "tests/test_a.py", "def test_a():\n    assert True\n")
    _write(root, "pkg/mod.py", "VALUE = 1\n")
    return root


def _make_makefile_repo(tmp_path):
    root = str(tmp_path)
    _write(root, "Makefile", "check:\n\techo ok\n\nlint:\n\techo lint\n")
    _write(root, "notes.txt", "hi\n")
    return root


# ---------------------------------------------------------------------------
# detect_verify_command
# ---------------------------------------------------------------------------
def test_detect_picks_tsc_for_package_json_repo(tmp_path):
    root = _make_web_repo(tmp_path)
    cmd = verify.detect_verify_command(root, ["src/app.tsx"])
    assert cmd is not None
    assert "tsc --noEmit" in cmd
    # scoped to the project's tsconfig
    assert "-p" in cmd
    assert "tsconfig.json" in cmd


def test_detect_scopes_tsc_to_webapp_subdir_tsconfig(tmp_path):
    root = _make_webapp_subdir_repo(tmp_path)
    cmd = verify.detect_verify_command(root, ["webapp/src/app.tsx"])
    assert cmd is not None
    assert "tsc --noEmit" in cmd
    assert os.path.join("webapp", "tsconfig.json") in cmd


def test_detect_picks_python_syntax_check_for_pyproject_repo(tmp_path):
    root = _make_python_repo(tmp_path)
    cmd = verify.detect_verify_command(root, ["pkg/mod.py"])
    assert cmd is not None
    # FAST syntax check of the CHANGED file, NOT a full pytest.
    assert "py_compile" in cmd
    assert "pkg/mod.py" in cmd
    assert "pytest" not in cmd


def test_detect_python_repo_skips_when_no_python_edited(tmp_path):
    # A python project but the edit was, say, a README -> no fast scoped check,
    # and a full pytest is too slow inline, so return None.
    root = _make_python_repo(tmp_path)
    cmd = verify.detect_verify_command(root, ["README.md"])
    assert cmd is None


def test_detect_makefile_check_target(tmp_path):
    root = _make_makefile_repo(tmp_path)
    cmd = verify.detect_verify_command(root, ["notes.txt"])
    assert cmd == "make check"


def test_detect_returns_none_for_empty_repo(tmp_path):
    root = str(tmp_path)
    _write(root, "readme.md", "hi\n")
    assert verify.detect_verify_command(root, ["readme.md"]) is None


def test_detect_returns_none_for_missing_repo():
    assert verify.detect_verify_command("/no/such/dir", ["a.py"]) is None


# ---------------------------------------------------------------------------
# changed-file scoping
# ---------------------------------------------------------------------------
def test_build_scoped_command_python(tmp_path):
    root = str(tmp_path)
    cmd = verify.build_scoped_command(root, ["a/b.py", "c.py"])
    assert cmd is not None
    assert "py_compile" in cmd
    assert "a/b.py" in cmd
    assert "c.py" in cmd
    # uses the running interpreter, not a bare "python"
    assert sys.executable in cmd


def test_build_scoped_command_ts_with_tsconfig(tmp_path):
    root = str(tmp_path)
    _write(root, "tsconfig.json", "{}")
    cmd = verify.build_scoped_command(root, ["src/x.ts"])
    assert cmd is not None
    assert "tsc --noEmit -p" in cmd
    assert "tsconfig.json" in cmd


def test_build_scoped_command_ts_without_tsconfig(tmp_path):
    root = str(tmp_path)
    cmd = verify.build_scoped_command(root, ["src/x.tsx"])
    assert cmd is not None
    assert "tsc --noEmit" in cmd
    assert "src/x.tsx" in cmd


def test_build_scoped_command_none_for_unsupported(tmp_path):
    root = str(tmp_path)
    assert verify.build_scoped_command(root, ["notes.md", "data.json"]) is None
    assert verify.build_scoped_command(root, []) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd.exe quoting")
def test_build_scoped_command_windows_uses_cmdline_quoting(tmp_path):
    """POSIX shlex.quote single quotes break cmd.exe when run_verify uses shell=True."""
    root = str(tmp_path)
    spaced = _write(root, "a spaced/b.py", "x = 1\n")
    cmd = verify.build_scoped_command(root, [spaced])
    assert cmd is not None
    assert "py_compile" in cmd
    assert sys.executable in cmd
    assert "'" not in cmd
    expected = subprocess.list2cmdline(
        [sys.executable, "-m", "py_compile", os.path.join("a spaced", "b.py")]
    )
    assert cmd == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd.exe quoting")
def test_build_scoped_command_windows_tsconfig_quoting(tmp_path):
    root = str(tmp_path)
    _write(root, "tsconfig.json", "{}")
    cmd = verify.build_scoped_command(root, ["src/x.ts"])
    assert cmd is not None
    assert "'" not in cmd
    assert subprocess.list2cmdline(["npx", "tsc", "--noEmit", "-p", "tsconfig.json"]) in cmd


@pytest.mark.skipif(os.name == "nt", reason="POSIX shlex.quote")
def test_build_scoped_command_posix_uses_shlex_quote(tmp_path):
    import shlex

    root = str(tmp_path)
    _write(root, "tsconfig.json", "{}")
    cmd = verify.build_scoped_command(root, ["src/x.ts"])
    assert cmd is not None
    assert shlex.quote("tsconfig.json") in cmd


# ---------------------------------------------------------------------------
# run_verify (subprocess monkeypatched)
# ---------------------------------------------------------------------------
class _FakeCompleted:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_run_verify_fail_on_nonzero(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["timeout"] = kwargs.get("timeout")
        return _FakeCompleted(1, "error: boom\n")

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    passed, output = verify.run_verify(str(tmp_path), "some check", ["a.py"], timeout=7)
    assert passed is False
    assert "boom" in output
    assert captured["command"] == "some check"
    assert captured["cwd"] == str(tmp_path)
    assert captured["timeout"] == 7


def test_run_verify_pass_on_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(
        verify.subprocess, "run",
        lambda command, **kw: _FakeCompleted(0, "ok\n"),
    )
    passed, output = verify.run_verify(str(tmp_path), "some check", ["a.py"])
    assert passed is True
    assert "ok" in output


def test_run_verify_empty_command_is_pass(tmp_path):
    passed, output = verify.run_verify(str(tmp_path), "", ["a.py"])
    assert passed is True
    assert output == ""


def test_run_verify_never_raises_on_exception(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(verify.subprocess, "run", boom)
    passed, output = verify.run_verify(str(tmp_path), "cmd", ["a.py"])
    assert passed is False
    assert "could not run" in output


def test_run_verify_timeout_is_failure(monkeypatch, tmp_path):
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="cmd", timeout=3, output="partial\n")

    monkeypatch.setattr(verify.subprocess, "run", timeout)
    passed, output = verify.run_verify(str(tmp_path), "cmd", ["a.py"], timeout=3)
    assert passed is False
    assert "timed out" in output


def test_run_verify_truncates_long_output(monkeypatch, tmp_path):
    big = "x" * (verify.MAX_OUTPUT + 500)
    monkeypatch.setattr(
        verify.subprocess, "run",
        lambda command, **kw: _FakeCompleted(1, big),
    )
    passed, output = verify.run_verify(str(tmp_path), "cmd", ["a.py"])
    assert passed is False
    assert len(output) <= verify.MAX_OUTPUT + len("\n[output truncated]")
    assert output.endswith("[output truncated]")
