"""Owner-only permissions for credential and config files, cross-platform.

On POSIX, chmod 0o600 is the whole story. On Windows, os.chmod only toggles
the read-only attribute -- it cannot restrict other accounts -- so files that
are supposed to be private (API keys, auth tokens) stay readable by any local
user unless real NTFS ACLs are applied. This module provides the one shared
entry point: restrict_to_owner(path), which uses icacls to drop inherited
ACEs and grant access only to the current user (plus SYSTEM, so services and
elevated tooling keep working).

Best-effort by design: permission hardening must never turn a successful
write into a failure, matching how callers already treated os.chmod errors.
"""
from __future__ import annotations

import os
import subprocess

# icacls runs per secured file; cache the resolved account name once.
_WINDOWS_ACCOUNT: str | None = None


def _windows_account() -> str | None:
    """The account to grant, as DOMAIN\\user when available.

    USERDOMAIN + USERNAME disambiguates the grant on domain-joined machines
    where a bare username could resolve to the wrong principal.
    """
    global _WINDOWS_ACCOUNT
    if _WINDOWS_ACCOUNT is None:
        user = os.environ.get("USERNAME") or ""
        if not user:
            try:
                user = os.getlogin()
            except OSError:
                user = ""
        domain = os.environ.get("USERDOMAIN") or ""
        _WINDOWS_ACCOUNT = f"{domain}\\{user}" if domain and user else user
    return _WINDOWS_ACCOUNT or None


def restrict_to_owner(path: str) -> bool:
    """Make `path` readable/writable by the owner only. Returns True on success.

    Never raises: callers write the file first and harden second, and a
    hardening failure must not destroy an otherwise-successful save.
    """
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            return False

    account = _windows_account()
    if not account:
        return False
    try:
        # /inheritance:r drops inherited ACEs; the explicit grants then form
        # the complete ACL. SYSTEM keeps services/backup tooling functional.
        proc = subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", f"{account}:F",
             "/grant:r", "*S-1-5-18:F"],  # SYSTEM by SID: locale-proof
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception:
        return False
