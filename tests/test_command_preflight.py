"""Hermetic tests for workspace-aware run_command preflight.

Rewrites must be unambiguous and filesystem-driven: a dummy .venv python
or webapp/package.json in tmp_path is enough. No network, no real
interpreters, no subprocess.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from harness.command_preflight import (
    classify_env_prerequisite_failure,
    resolve_command_preflight,
    venv_python_path,
)
from harness.pilot import PilotAction
from harness.tool_dispatch import ToolDispatchMixin


def _venv_python(repo):
    if os.name == "nt":
        path = repo / ".venv" / "Scripts" / "python.exe"
    else:
        path = repo / ".venv" / "bin" / "python"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _dispatch_session(repo_path, state_dir=""):
    return SimpleNamespace(
        config=SimpleNamespace(repo=str(repo_path), state_dir=state_dir),
        state_dir=state_dir,
        harness_session_id="sess-preflight",
        _auto_mode=False,
        _auto_command_guard=False,
        _cancel=None,
    )


def test_python_m_pytest_rewrites_to_venv(tmp_path):
    venv_py = _venv_python(tmp_path)
    result = resolve_command_preflight("python -m pytest -q", str(tmp_path))
    assert result["rewritten"] is True
    assert result["kind"] == "interpreter_rewrite"
    assert result["cwd"] == str(tmp_path)
    assert result["command"].split()[0] == str(venv_py)
    assert "-m pytest -q" in result["command"]
    assert result["argv"][:3] == [str(venv_py), "-m", "pytest"]
    assert result["reason"]


def test_already_using_venv_path_is_not_rewritten(tmp_path):
    venv_py = _venv_python(tmp_path)
    command = "%s -m pytest -q" % venv_py
    result = resolve_command_preflight(command, str(tmp_path))
    assert result["rewritten"] is False
    assert result["kind"] is None
    assert result["command"] == command
    assert result["cwd"] == str(tmp_path)


def test_relative_venv_path_is_not_rewritten(tmp_path):
    _venv_python(tmp_path)
    if os.name == "nt":
        rel = os.path.join(".venv", "Scripts", "python.exe")
    else:
        rel = os.path.join(".venv", "bin", "python")
    command = "%s -m pytest" % rel
    result = resolve_command_preflight(command, str(tmp_path))
    assert result["rewritten"] is False
    assert result["command"] == command


def test_npm_test_uses_webapp_when_root_has_no_package_json(tmp_path):
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    result = resolve_command_preflight("npm test", str(tmp_path))
    assert result["rewritten"] is True
    assert result["kind"] == "cwd_rewrite"
    assert result["command"] == "npm test"
    assert result["cwd"] == str(webapp)
    assert result["argv"] == ["npm", "test"]


def test_both_package_json_present_does_not_rewrite_cwd(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    result = resolve_command_preflight("npm test", str(tmp_path))
    assert result["rewritten"] is False
    assert result["command"] == "npm test"
    assert result["cwd"] == str(tmp_path)


def test_no_venv_does_not_rewrite_interpreter(tmp_path):
    result = resolve_command_preflight("python -m pytest -q", str(tmp_path))
    assert result["rewritten"] is False
    assert result["kind"] is None
    assert result["command"] == "python -m pytest -q"
    assert result["cwd"] == str(tmp_path)
    assert venv_python_path(str(tmp_path)) is None


def test_bare_pytest_becomes_venv_module(tmp_path):
    venv_py = _venv_python(tmp_path)
    result = resolve_command_preflight("pytest tests/foo.py -q", str(tmp_path))
    assert result["rewritten"] is True
    assert result["kind"] == "interpreter_rewrite"
    assert result["argv"] == [str(venv_py), "-m", "pytest", "tests/foo.py", "-q"]


def test_python3_rewrites_to_venv(tmp_path):
    venv_py = _venv_python(tmp_path)
    result = resolve_command_preflight("python3 -m pytest -q", str(tmp_path))
    assert result["rewritten"] is True
    assert result["kind"] == "interpreter_rewrite"
    assert result["argv"][:3] == [str(venv_py), "-m", "pytest"]
    assert result["command"].split()[0] == str(venv_py)


def test_env_assignment_prefix_python_rewrites(tmp_path):
    venv_py = _venv_python(tmp_path)
    result = resolve_command_preflight("FOO=1 python -m pytest -q", str(tmp_path))
    assert result["rewritten"] is True
    assert result["kind"] == "interpreter_rewrite"
    assert result["argv"][:4] == ["FOO=1", str(venv_py), "-m", "pytest"]


def test_cd_left_alone(tmp_path):
    _venv_python(tmp_path)
    command = "cd webapp"
    result = resolve_command_preflight(command, str(tmp_path))
    assert result["rewritten"] is False
    assert result["command"] == command


def test_shell_and_left_alone(tmp_path):
    _venv_python(tmp_path)
    command = "python -m pytest -q && echo done"
    result = resolve_command_preflight(command, str(tmp_path))
    assert result["rewritten"] is False
    assert result["command"] == command


def test_node_clients_use_webapp_when_root_has_no_package_json(tmp_path):
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    for client in ("npx", "pnpm", "yarn"):
        command = "%s test" % client
        result = resolve_command_preflight(command, str(tmp_path))
        assert result["rewritten"] is True, client
        assert result["kind"] == "cwd_rewrite"
        assert result["command"] == command
        assert result["cwd"] == str(webapp)


def test_root_only_package_json_does_not_rewrite_cwd(tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    result = resolve_command_preflight("npm test", str(tmp_path))
    assert result["rewritten"] is False
    assert result["command"] == "npm test"
    assert result["cwd"] == str(tmp_path)


def test_resolve_command_preflight_never_raises(tmp_path):
    _venv_python(tmp_path)
    for command, repo in (
        (None, str(tmp_path)),
        (12, str(tmp_path)),
        ("python -m pytest", None),
        ("python -m pytest", 7),
        (object(), object()),
    ):
        result = resolve_command_preflight(command, repo)
        assert isinstance(result, dict)
        assert "rewritten" in result
        assert "command" in result
    assert classify_env_prerequisite_failure("python -m pytest", "nope", "x") is None
    assert classify_env_prerequisite_failure("python -m pytest", None, None) is None


def test_empty_and_malformed_commands_return_original(tmp_path):
    empty = resolve_command_preflight("", str(tmp_path))
    assert empty["rewritten"] is False
    assert empty["command"] == ""
    assert empty["cwd"] == str(tmp_path)

    malformed = resolve_command_preflight('echo "unterminated', str(tmp_path))
    assert malformed["rewritten"] is False
    assert malformed["command"] == 'echo "unterminated'


def test_pipeline_is_not_rewritten(tmp_path):
    _venv_python(tmp_path)
    command = "python -m pytest -q | tee out.log"
    result = resolve_command_preflight(command, str(tmp_path))
    assert result["rewritten"] is False
    assert result["command"] == command


def test_npm_prefix_flag_is_not_rewritten(tmp_path):
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    command = "npm --prefix other test"
    result = resolve_command_preflight(command, str(tmp_path))
    assert result["rewritten"] is False
    assert result["command"] == command
    assert result["cwd"] == str(tmp_path)


def test_env_prerequisite_failure_shapes():
    assert (
        classify_env_prerequisite_failure(
            "python -m pytest",
            1,
            "ModuleNotFoundError: No module named 'pytest'",
        )
        == "env_prerequisite"
    )
    assert (
        classify_env_prerequisite_failure(
            "npm test",
            1,
            "npm ERR! enoent Could not read package.json: Error: ENOENT: "
            "no such file or directory, open '/repo/package.json'",
        )
        == "env_prerequisite"
    )
    assert (
        classify_env_prerequisite_failure(
            "python x.py",
            127,
            "bash: python: command not found",
        )
        == "env_prerequisite"
    )
    assert (
        classify_env_prerequisite_failure(
            "python3 x.py",
            1,
            "ModuleNotFoundError: No module named 'yaml'",
        )
        is None
    )
    assert classify_env_prerequisite_failure("true", 0, "ok") is None
    assert classify_env_prerequisite_failure("grep needle .", 1, "") is None


def test_do_run_command_receives_rewritten_interpreter(tmp_path):
    venv_py = _venv_python(tmp_path)
    session = _dispatch_session(tmp_path)
    act = PilotAction(kind="run_command", command="python -m pytest -q")
    captured = {}

    def fake_run(command, cwd=None, timeout=None, cancel_event=None):
        captured["command"] = command
        captured["cwd"] = cwd
        return ("ok\n", 0, "ok")

    with patch("harness.command_policy.run_cancellable", side_effect=fake_run):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)

    assert ok is True and status == "success"
    assert captured["command"].split()[0] == str(venv_py)
    assert "-m pytest -q" in captured["command"]
    assert captured["cwd"] == str(tmp_path)
    assert val["cwd"] == str(tmp_path)
    assert val["rewritten"] is True
    assert val.get("preflight_reason")
    assert "failure_class" not in val


def test_do_run_command_receives_webapp_cwd(tmp_path):
    webapp = tmp_path / "webapp"
    webapp.mkdir()
    (webapp / "package.json").write_text("{}", encoding="utf-8")
    session = _dispatch_session(tmp_path)
    act = PilotAction(kind="run_command", command="npm test")
    captured = {}

    def fake_run(command, cwd=None, timeout=None, cancel_event=None):
        captured["command"] = command
        captured["cwd"] = cwd
        return ("ok\n", 0, "ok")

    with patch("harness.command_policy.run_cancellable", side_effect=fake_run):
        ok, _status, val = ToolDispatchMixin._do_run_command(session, act)

    assert ok is True
    assert captured["command"] == "npm test"
    assert captured["cwd"] == str(webapp)
    assert val["cwd"] == str(webapp)
    assert val["rewritten"] is True
    assert "webapp" in (val.get("preflight_reason") or "")


def test_do_run_command_sets_env_prerequisite_failure_class(tmp_path):
    session = _dispatch_session(tmp_path)
    act = PilotAction(kind="run_command", command="python -m pytest")
    with patch(
        "harness.command_policy.run_cancellable",
        return_value=("ModuleNotFoundError: No module named 'pytest'\n", 1, "ok"),
    ):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is True and status == "success"
    assert val["status"] == "ok" and val["exit_code"] == 1
    assert val["failure_class"] == "env_prerequisite"
    assert val.get("hint")
    assert "rewritten" not in val


def test_do_run_command_rewritten_and_failure_class_together(tmp_path):
    venv_py = _venv_python(tmp_path)
    session = _dispatch_session(tmp_path)
    act = PilotAction(kind="run_command", command="python -m pytest")
    captured = {}

    def fake_run(command, cwd=None, timeout=None, cancel_event=None):
        captured["command"] = command
        return ("ModuleNotFoundError: No module named 'pytest'\n", 1, "ok")

    with patch("harness.command_policy.run_cancellable", side_effect=fake_run):
        ok, status, val = ToolDispatchMixin._do_run_command(session, act)
    assert ok is True and status == "success"
    assert captured["command"].split()[0] == str(venv_py)
    assert val["rewritten"] is True
    assert val["failure_class"] == "env_prerequisite"
