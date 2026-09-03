"""Dest git spawn neutralization (env + -c flags)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from harness.git_spawn import git_extra_args, git_spawn_env
from harness.workspaces import _git


def test_git_spawn_env_sets_nosystem_and_null_global():
    env = git_spawn_env()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] in {"/dev/null", "NUL", "nul"}


def test_git_spawn_env_copies_base_and_overrides_inherited_keys():
    env = git_spawn_env(
        {
            "PATH": "/bin",
            "GIT_CONFIG_NOSYSTEM": "0",
            "GIT_CONFIG_GLOBAL": "/tmp/evil.gitconfig",
        }
    )
    assert env["PATH"] == "/bin"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] in {"/dev/null", "NUL", "nul"}


def test_git_spawn_env_never_raises_on_unusable_base():
    class _Boom:
        def keys(self):
            raise RuntimeError("nope")

        def __iter__(self):
            raise RuntimeError("nope")

    env = git_spawn_env(_Boom())  # type: ignore[arg-type]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] in {"/dev/null", "NUL", "nul"}


def test_git_extra_args_disable_hooks_fsmonitor_and_help_alias():
    args = git_extra_args()
    paired = list(zip(args[0::2], args[1::2]))
    assert all(flag == "-c" for flag, _ in paired)
    assert ("-c", "core.hooksPath=") in paired
    assert ("-c", "core.fsmonitor=") in paired
    assert ("-c", "core.fsmonitorHook=") in paired
    assert ("-c", "alias.help=help") in paired


def test_workspaces_git_passes_hooks_path_neutralize():
    mock_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("harness.workspaces.subprocess.run", return_value=mock_proc) as mocked:
        _git("/tmp/repo", "status", "--porcelain")
    argv = list(mocked.call_args.args[0])
    assert argv[:3] == ["git", "-C", "/tmp/repo"]
    pairs = list(zip(argv, argv[1:]))
    assert ("-c", "core.hooksPath=") in pairs
    assert argv[-2:] == ["status", "--porcelain"]
    env = mocked.call_args.kwargs["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] in {"/dev/null", "NUL", "nul"}
