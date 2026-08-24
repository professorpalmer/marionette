from __future__ import annotations

"""SQLite journal selection for network filesystems.

WAL mode relies on shared-memory sidecars that break on NFS and similar remote
mounts. Call ``configure_sqlite_connection`` after ``sqlite3.connect`` so local
disks keep WAL while network paths fall back to TRUNCATE.

When ``Path.home()`` is on a network filesystem, ``host_scoped_state_path``
rewrites default ``~/.pmharness/...`` locations under a per-host subdirectory
so concurrent machines do not share one WAL set.
"""

import os
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Callable, Optional, Union

PathLike = Union[str, Path]
StatfsFn = Callable[[str], bool]

_LINUX_NETWORK_FSTYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "fuse.sshfs",
        "afs",
        "ncpfs",
    }
)

# 26 is APFS (local). Including it forced TRUNCATE on every Mac local DB (v0.9.316).
_DARWIN_NETWORK_FSTYPES = frozenset({6, 13, 28})  # NFS, AFP, SMBFS


def _linux_mount_is_network(path: str) -> bool:
    try:
        target = os.path.realpath(path)
        best_len = -1
        best_fstype: Optional[str] = None
        with open("/proc/mounts", encoding="utf-8") as mounts:
            for raw_line in mounts:
                parts = raw_line.split()
                if len(parts) < 3:
                    continue
                mount_point = parts[1].replace("\\040", " ")
                mount_point = mount_point.rstrip("/") or "/"
                if target == mount_point or (
                    mount_point != "/"
                    and target.startswith(mount_point + os.sep)
                ):
                    if len(mount_point) > best_len:
                        best_len = len(mount_point)
                        best_fstype = parts[2]
        if best_fstype is None:
            return False
        base = best_fstype.split(".", 1)[0].lower()
        return base in _LINUX_NETWORK_FSTYPES or best_fstype.lower() in _LINUX_NETWORK_FSTYPES
    except OSError:
        return False


def _darwin_mount_is_network(path: str) -> bool:
    try:
        import ctypes

        libc = ctypes.CDLL("libc.dylib", use_errno=True)

        class Statfs(ctypes.Structure):
            _fields_ = [
                ("f_bsize", ctypes.c_uint32),
                ("f_iosize", ctypes.c_uint32),
                ("f_blocks", ctypes.c_uint64),
                ("f_bfree", ctypes.c_uint64),
                ("f_bavail", ctypes.c_uint64),
                ("f_files", ctypes.c_uint64),
                ("f_ffree", ctypes.c_uint64),
                ("f_fsid", ctypes.c_int32 * 2),
                ("f_owner", ctypes.c_uint32),
                ("f_type", ctypes.c_uint32),
                ("f_flags", ctypes.c_uint32),
                ("f_fssubtype", ctypes.c_uint32),
                ("f_fstypename", ctypes.c_char * 16),
                ("f_mntonname", ctypes.c_char * 1024),
                ("f_mntfromname", ctypes.c_char * 1024),
                ("f_flags_ext", ctypes.c_uint32),
                ("f_reserved", ctypes.c_uint32 * 7),
            ]

        buf = Statfs()
        if libc.statfs(path.encode("utf-8"), ctypes.byref(buf)) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), path)
        return int(buf.f_type) in _DARWIN_NETWORK_FSTYPES
    except OSError:
        return False


def _win32_mount_is_network(path: str) -> bool:
    normalized = os.path.abspath(path)
    if normalized.startswith("\\\\"):
        return True
    if len(normalized) >= 2 and normalized[1] == ":":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            drive_type = kernel32.GetDriveTypeW(normalized[:3])
            return drive_type == 4  # DRIVE_REMOTE
        except (AttributeError, OSError):
            return False
    return False


def _platform_is_network_fs(path: str) -> bool:
    if sys.platform == "linux":
        return _linux_mount_is_network(path)
    if sys.platform == "darwin":
        return _darwin_mount_is_network(path)
    if sys.platform == "win32":
        return _win32_mount_is_network(path)
    return False


def is_network_filesystem(
    path: PathLike,
    *,
    statfs: Optional[StatfsFn] = None,
) -> bool:
    """Return True when ``path`` lives on a network / remote filesystem."""
    checker = statfs or _platform_is_network_fs
    try:
        resolved = str(Path(path).resolve())
    except OSError:
        resolved = str(path)
    return bool(checker(resolved))


def journal_mode_for(
    path: PathLike,
    *,
    statfs: Optional[StatfsFn] = None,
) -> str:
    """WAL on local disks; TRUNCATE when the DB directory is network-backed."""
    return "truncate" if is_network_filesystem(path, statfs=statfs) else "wal"


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    db_path: PathLike,
    *,
    busy_timeout_ms: int = 5000,
    statfs: Optional[StatfsFn] = None,
) -> str:
    """Apply busy_timeout and a filesystem-appropriate journal mode."""
    mode = journal_mode_for(db_path, statfs=statfs)
    conn.execute(f"PRAGMA journal_mode={mode}")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return mode


def _hostname_token() -> str:
    host = (socket.gethostname() or "localhost").split(".", 1)[0].strip()
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in host)
    return safe or "localhost"


def host_scoped_state_path(
    path: PathLike,
    *,
    statfs: Optional[StatfsFn] = None,
) -> Path:
    """When HOME is network-backed, scope default pmharness files per host."""
    candidate = Path(path)
    home = Path.home()
    if not is_network_filesystem(home, statfs=statfs):
        return candidate
    try:
        home_res = home.resolve()
        resolved = candidate.resolve()
    except OSError:
        return candidate
    pm_root = home_res / ".pmharness"
    try:
        rel = resolved.relative_to(pm_root)
    except ValueError:
        return candidate
    host = _hostname_token()
    parts = list(rel.parts)
    if parts and parts[0] == "state":
        scoped = Path("state", "hosts", host, *parts[1:])
    else:
        scoped = Path("hosts", host, *parts)
    return pm_root / scoped
