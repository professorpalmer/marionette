"""Platform-appropriate temp-dir wording in implement_guards."""
from __future__ import annotations

import inspect
import tempfile

from harness import implement_guards
from harness.implement_guards import check_implement_workspace


def test_no_workspace_guidance_uses_system_tempdir(monkeypatch):
    monkeypatch.setenv("HARNESS_IMPLEMENT_GIT_GUARD", "1")
    msg = check_implement_workspace("")
    assert msg is not None
    assert tempfile.gettempdir() in msg
    assert "run_command" in msg
    # Guidance must not hardcode a POSIX-only /tmp literal.
    src = inspect.getsource(implement_guards.check_implement_workspace)
    assert "/tmp" not in src
