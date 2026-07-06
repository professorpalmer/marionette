"""Owner-only file hardening must actually restrict access on BOTH platforms.

POSIX gets chmod 0o600; Windows gets a real NTFS ACL via icacls (os.chmod is a
no-op there beyond the read-only bit). The Windows test runs the real icacls
end-to-end and inspects the resulting ACL.
"""
import os
import subprocess

import pytest

from harness.secure_files import restrict_to_owner


def test_restrict_to_owner_reports_success(tmp_path):
    p = tmp_path / "secret.json"
    p.write_text("{}")
    assert restrict_to_owner(str(p)) is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod semantics")
def test_restrict_to_owner_sets_0600_on_posix(tmp_path):
    p = tmp_path / "secret.json"
    p.write_text("{}")
    restrict_to_owner(str(p))
    assert os.stat(p).st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS ACL semantics")
def test_restrict_to_owner_strips_inherited_acl_on_windows(tmp_path):
    p = tmp_path / "secret.json"
    p.write_text("{}")
    assert restrict_to_owner(str(p)) is True

    out = subprocess.run(
        ["icacls", str(p)], capture_output=True, text=True, timeout=15
    ).stdout
    # Broad principals that inheritance would normally add must be gone.
    assert "BUILTIN\\Users" not in out
    assert "Authenticated Users" not in out
    # Only the current user and SYSTEM remain.
    user = os.environ.get("USERNAME", "")
    assert user and user.lower() in out.lower()
