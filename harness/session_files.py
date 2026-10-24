"""Per-session JSON files that follow the session's seal.

A plain session writes JSON exactly as before (``_atomic_write_json``). A
sealed session writes a ``harness.seal`` envelope with the same atomic
replace, fsync and read-back check. Readers accept both forms, so a reader
never needs to know which kind of session wrote the file.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from . import seal


def _sync_dir(parent: str) -> None:
    if os.name == "nt":
        return
    directory = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_bytes_atomic(path: str, data: bytes) -> None:
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _sync_dir(parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: str, data: Any, session_id: str, purpose: str) -> None:
    """Write ``data`` for ``session_id``. Sealed sessions never write plaintext."""
    codec = seal.codec_for(session_id)
    if codec is None:
        from .compaction_archive import _atomic_write_json

        _atomic_write_json(path, data)
        return
    blob = codec.seal_json(purpose, data)
    write_bytes_atomic(path, blob)
    with open(path, "rb") as handle:
        if codec.open_json(purpose, handle.read()) != json.loads(json.dumps(data)):
            raise OSError("sealed JSON readback mismatch")


def decode_json(raw: bytes, session_id: str, purpose: str) -> Any:
    """Parse file bytes written by ``write_json`` for either kind of session."""
    if seal.is_envelope(raw):
        codec = seal.codec_for(session_id)
        if codec is None:
            raise seal.SealError("sealed file for a session with no seal")
        return codec.open_json(purpose, raw)
    return json.loads(raw.decode("utf-8"))


def read_json(path: str, session_id: str, purpose: str) -> Any:
    with open(path, "rb") as handle:
        return decode_json(handle.read(), session_id, purpose)


def is_sealed_file(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return seal.is_envelope(handle.read(len(seal.MAGIC)))
    except OSError:
        return False


def sealed_data_path(session_id: str, name: str) -> str:
    """Per-session file of a store that plain sessions keep in one shared file.

    It sits under the seal root, so ``seal.erase_session`` removes it.
    """
    return os.path.join(seal.sealed_data_dir(session_id), name)


def sealed_owner(session_id: str) -> str:
    """``session_id`` when that session is sealed, else ""."""
    return session_id if session_id and seal.is_sealed(session_id) else ""
