"""The suite never reads or writes the developer's real Puppetmaster state."""
import os
import tempfile
from pathlib import Path


def test_puppetmaster_app_state_root_is_a_throwaway_dir():
    # Unisolated, every run created ~20 project state dirs (target, c, proj,
    # repo_a, tmp*) in the real ~/Library/Application Support/puppetmaster.
    from puppetmaster import state
    root = state.app_state_root().resolve()
    assert Path(tempfile.gettempdir()).resolve() in root.parents
    saved = os.environ.pop(state.APP_STATE_ROOT_ENV)
    try:
        real = state.app_state_root().resolve()
    finally:
        os.environ[state.APP_STATE_ROOT_ENV] = saved
    assert real != root and real not in root.parents
