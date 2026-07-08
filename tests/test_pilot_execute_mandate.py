"""The interactive pilot system prompt must mandate autonomous execution of
terminal/CLI work (run_command), not dictate manual command lists to the user."""
from harness.pilot import PILOT_SYSTEM


def test_pilot_system_has_execute_mandate():
    assert "EXECUTE, DON'T DICTATE" in PILOT_SYSTEM
    assert "run_command" in PILOT_SYSTEM


def test_pilot_system_covers_ssh_and_shell_env():
    # Must explicitly say ssh/CLI work from the user's login-shell env will work,
    # so the pilot stops refusing local ssh/deploy/validate it can actually run.
    assert "ssh" in PILOT_SYSTEM
    assert "login-shell environment" in PILOT_SYSTEM


def test_pilot_system_forbids_handing_manual_command_lists():
    assert "type by\nhand" in PILOT_SYSTEM or "type by hand" in PILOT_SYSTEM


def test_pilot_system_forbids_puppetmaster_cli_shell():
    assert "NEVER shell the Puppetmaster CLI" in PILOT_SYSTEM
    assert "run_swarm" in PILOT_SYSTEM
    assert "search_state" in PILOT_SYSTEM
