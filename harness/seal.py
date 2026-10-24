"""Sealed sessions: AES-256-GCM at rest for sessions created with the switch on.

Keys
----
- Master key (MK, 32 bytes). Electron main creates it and keeps it wrapped by
  the OS key store (``safeStorage``) and by a recovery code
  (``webapp/electron/seal-keys.cjs``). Main writes it to this process's stdin
  at spawn, or posts it to ``/api/seal/unlock`` (enable, recovery, adopted
  backend). This process holds MK in memory only and never writes it.
- Session key (SK, 32 bytes). One random key per sealed session, wrapped by MK
  in ``<state>/seal/sessions/<sid>.key``. Deleting that file makes every copy
  of the session's ciphertext unreadable (crypto-erase).

Envelope
--------
``MNSL`` | version (1 byte) | key id (8 bytes) | nonce (12 bytes) | ciphertext
and tag. The associated data is the 13-byte header, a zero byte and a purpose
label, so a blob cannot move to another session or another store. Text fields
carry ``mnsl1:`` + URL-safe base64 of the envelope.

A session is sealed when the registry marks it or its key file exists. Either
marker alone is enough, so a lost marker never turns a sealed session plain.
A sealed session with no master key in this process raises ``SealLocked``. A
sealed write never falls back to plaintext.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Optional

MAGIC = b"MNSL"
VERSION = 1
TEXT_PREFIX = "mnsl1:"
KEY_BYTES = 32
NONCE_BYTES = 12
KEY_ID_BYTES = 8
_HEADER_BYTES = len(MAGIC) + 1 + KEY_ID_BYTES
# Bytes an envelope adds to its plaintext: header, nonce and GCM tag.
ENVELOPE_OVERHEAD = _HEADER_BYTES + NONCE_BYTES + 16
UPLOADS_ID = "~uploads"
MEMORY_ID = "~memory"
_KEY_ID_LABEL = b"marionette-seal-key-id"

_LOCK = threading.RLock()
_master: Optional[bytes] = None
_codecs: dict = {}
_sealed_ids: set = set()


class SealError(ValueError):
    """Bad envelope, wrong key, changed ciphertext or a missing session key."""


class SealLocked(RuntimeError):
    """A sealed session needs the master key, and this process does not have it."""


def _aesgcm(key: bytes):
    # Imported on first use: only sealed sessions need the dependency.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def key_id(key: bytes) -> bytes:
    return hmac.new(key, _KEY_ID_LABEL, hashlib.sha256).digest()[:KEY_ID_BYTES]


def state_dir() -> str:
    explicit = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    return os.path.join(os.path.expanduser("~/.pmharness"), "state")


def sealed_data_dir(session_id: str) -> str:
    return os.path.join(state_dir(), "sealed", _safe_sid(session_id))


def seal_dir() -> str:
    return os.path.join(state_dir(), "seal")


def config_path() -> str:
    return os.path.join(seal_dir(), "config.json")


def session_key_path(session_id: str) -> str:
    return os.path.join(seal_dir(), "sessions", f"{_safe_sid(session_id)}.key")


def _safe_sid(session_id: str) -> str:
    sid = str(session_id or "")
    if not sid or not all((c.isascii() and c.isalnum()) or c in "-_" for c in sid) or len(sid) > 128:
        raise SealError("invalid session id")
    return sid


# ---- envelope -------------------------------------------------------------

def _aad(header: bytes, purpose: str) -> bytes:
    return header + b"\0" + purpose.encode("utf-8")


def encrypt(key: bytes, purpose: str, data: bytes) -> bytes:
    header = MAGIC + bytes([VERSION]) + key_id(key)
    nonce = secrets.token_bytes(NONCE_BYTES)
    return header + nonce + _aesgcm(key).encrypt(nonce, bytes(data), _aad(header, purpose))


def decrypt(key: bytes, purpose: str, blob: bytes) -> bytes:
    blob = bytes(blob)
    if len(blob) < _HEADER_BYTES + NONCE_BYTES + 16 or not blob.startswith(MAGIC):
        raise SealError("not a sealed envelope")
    if blob[len(MAGIC)] != VERSION:
        raise SealError("unsupported envelope version")
    header = blob[:_HEADER_BYTES]
    if not hmac.compare_digest(header[len(MAGIC) + 1:], key_id(key)):
        raise SealError("envelope was sealed with another key")
    nonce = blob[_HEADER_BYTES:_HEADER_BYTES + NONCE_BYTES]
    from cryptography.exceptions import InvalidTag

    try:
        return _aesgcm(key).decrypt(nonce, blob[_HEADER_BYTES + NONCE_BYTES:], _aad(header, purpose))
    except InvalidTag:
        raise SealError("sealed data failed authentication") from None


def is_envelope(blob: Any) -> bool:
    return isinstance(blob, (bytes, bytearray)) and bytes(blob[:len(MAGIC)]) == MAGIC


def is_sealed_text(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(TEXT_PREFIX)


@dataclass(frozen=True)
class Codec:
    """Seals and opens data for one sealed session."""

    session_id: str
    _key: bytes

    def _purpose(self, purpose: str) -> str:
        return f"{purpose}:{self.session_id}"

    def seal(self, purpose: str, data: bytes) -> bytes:
        return encrypt(self._key, self._purpose(purpose), data)

    def open(self, purpose: str, blob: bytes) -> bytes:
        return decrypt(self._key, self._purpose(purpose), blob)

    def seal_text(self, purpose: str, text: str) -> str:
        blob = self.seal(purpose, text.encode("utf-8"))
        return TEXT_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")

    def open_text(self, purpose: str, value: str) -> str:
        if not is_sealed_text(value):
            raise SealError("value is not sealed text")
        try:
            blob = base64.urlsafe_b64decode(value[len(TEXT_PREFIX):].encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            raise SealError("sealed text is not valid base64") from None
        return self.open(purpose, blob).decode("utf-8")

    def name_tag(self, value: str) -> str:
        """A keyed file name for ``value``: stable in this session, opaque elsewhere."""
        tag_key = hmac.new(self._key, b"marionette-seal-name-tag", hashlib.sha256).digest()
        return hmac.new(tag_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def seal_json(self, purpose: str, obj: Any) -> bytes:
        return self.seal(purpose, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def open_json(self, purpose: str, blob: bytes) -> Any:
        return json.loads(self.open(purpose, blob).decode("utf-8"))

    def __repr__(self) -> str:  # never print key material
        return f"Codec(session_id={self.session_id!r})"


# ---- master key and switch -------------------------------------------------

def read_config() -> dict:
    try:
        with open(config_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled() -> bool:
    """The switch: new sessions are sealed."""
    return read_config().get("enabled") is True


def unlock(master_key: bytes) -> None:
    """Hold the master key in this process. It must match the configured key id."""
    global _master
    master_key = bytes(master_key)
    if len(master_key) != KEY_BYTES:
        raise SealError("master key must be 32 bytes")
    expected = str(read_config().get("key_id") or "")
    if not expected:
        raise SealError("sealing is not set up")
    if not hmac.compare_digest(key_id(master_key).hex(), expected):
        raise SealError("master key does not match this profile")
    with _LOCK:
        _master = master_key
        _codecs.clear()


_lock_listeners: list = []


def on_lock(callback) -> None:
    """Call ``callback()`` on lock and session erase: drop plaintext caches."""
    _lock_listeners.append(callback)


def _notify_lock() -> None:
    for callback in list(_lock_listeners):
        try:
            callback()
        except Exception:
            pass  # A cache that fails to clear must not keep the key alive.


def lock() -> None:
    global _master
    with _LOCK:
        _master = None
        _codecs.clear()
    _notify_lock()


def unlocked() -> bool:
    return _master is not None


def status() -> dict:
    cfg = read_config()
    return {
        "configured": bool(cfg.get("key_id")),
        "enabled": cfg.get("enabled") is True,
        "unlocked": unlocked(),
    }


# ---- sealed sessions -------------------------------------------------------

def mark_sealed(session_id: str) -> None:
    """Record, for this process, that a session is sealed (from the registry)."""
    with _LOCK:
        _sealed_ids.add(_safe_sid(session_id))


def is_sealed(session_id: str) -> bool:
    try:
        sid = _safe_sid(session_id)
    except SealError:
        return False  # create_session refuses such an id, so it is never sealed.
    return sid in _sealed_ids or os.path.exists(session_key_path(sid))


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    from .secure_files import restrict_to_owner

    restrict_to_owner(path)


def create_session(session_id: str) -> Codec:
    """Create the key of a new sealed session. Needs the master key."""
    sid = _safe_sid(session_id)
    with _LOCK:
        if _master is None:
            raise SealLocked("Encrypted sessions are locked. Unlock them in Settings > Encrypted sessions, or turn off encryption for new sessions.")
        if os.path.exists(session_key_path(sid)):
            raise SealError("session already has a key")
        session_key = secrets.token_bytes(KEY_BYTES)
        _atomic_write(session_key_path(sid), encrypt(_master, f"session-key:{sid}", session_key))
        _sealed_ids.add(sid)
        codec = Codec(sid, session_key)
        _codecs[sid] = codec
        return codec


def codec_for(session_id: str) -> Optional[Codec]:
    """The codec of a sealed session, or None for a plain session.

    Raises SealLocked when the session is sealed and the master key is absent,
    and SealError when a sealed session has lost its key file.
    """
    if not session_id or not is_sealed(session_id):
        return None
    sid = _safe_sid(session_id)
    with _LOCK:
        cached = _codecs.get(sid)
        if cached is not None:
            return cached
        if _master is None:
            raise SealLocked("This session is encrypted and locked. Unlock it in Settings > Encrypted sessions.")
        try:
            with open(session_key_path(sid), "rb") as fh:
                wrapped = fh.read()
        except OSError:
            raise SealError("the key of this encrypted session is missing") from None
        codec = Codec(sid, decrypt(_master, f"session-key:{sid}", wrapped))
        _codecs[sid] = codec
        return codec


def erase_session(session_id: str) -> None:
    """Destroy a sealed session's key. Its ciphertext everywhere becomes unreadable."""
    sid = _safe_sid(session_id)
    path = session_key_path(sid)
    with _LOCK:
        _codecs.pop(sid, None)
        try:
            size = os.path.getsize(path)
            with open(path, "r+b") as fh:
                fh.write(secrets.token_bytes(size))
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        # Per-session sealed stores (scratch, todos, goal) go with the key.
        shutil.rmtree(sealed_data_dir(sid), ignore_errors=True)
    _notify_lock()


STDIN_ENV = "HARNESS_SEAL_STDIN"


def unlock_from_stdin(stream=None, timeout: float = 10.0) -> str:
    """Read the master key line that Electron writes at backend spawn.

    The line is JSON: ``{"master_key": "<base64>"}`` or ``{}`` when sealing is
    not set up. The key never passes through the environment, so children do
    not inherit it. Returns "unlocked", "none", "timeout" or "rejected".
    """
    if os.environ.pop(STDIN_ENV, "") != "1":
        return "none"
    import sys

    source = stream if stream is not None else sys.stdin.buffer
    box: list = []
    reader = threading.Thread(target=lambda: box.append(source.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if not box:
        return "timeout"
    try:
        payload = json.loads(box[0].decode("utf-8") or "{}")
        encoded = payload.get("master_key") if isinstance(payload, dict) else None
        if not encoded:
            return "none"
        unlock(base64.b64decode(encoded, validate=True))
    except (ValueError, SealError):
        return "rejected"
    finally:
        box.clear()
    return "unlocked"


FILE_PURPOSE = "file"


def write_file(codec: Codec, path: str, data: bytes) -> None:
    """Write a sealed file that ``read_file`` can open from its path alone."""
    from .session_files import write_bytes_atomic

    write_bytes_atomic(path, codec.seal(FILE_PURPOSE, data))


def upload_codec() -> Optional[Codec]:
    """Codec for composer uploads while the switch is on and unlocked, else None.

    Uploads arrive before the composer names a session, so they use one key
    derived from the master key, not a session key.
    """
    if not enabled():
        return None
    with _LOCK:
        return _upload_codec_locked()


def _upload_codec_locked() -> Optional[Codec]:
    if _master is None:
        return None
    codec = _codecs.get(UPLOADS_ID)
    if codec is None:
        key = hmac.new(_master, b"marionette-seal-uploads", hashlib.sha256).digest()
        codec = _codecs[UPLOADS_ID] = Codec(UPLOADS_ID, key)
    return codec


def memory_codec() -> Codec:
    """Codec for the memory that only sealed sessions see. Raises when locked."""
    with _LOCK:
        if _master is None:
            raise SealLocked("Encrypted memory is locked. Unlock it in Settings > Encrypted sessions.")
        codec = _codecs.get(MEMORY_ID)
        if codec is None:
            key = hmac.new(_master, b"marionette-seal-memory", hashlib.sha256).digest()
            codec = _codecs[MEMORY_ID] = Codec(MEMORY_ID, key)
        return codec


ANY_OWNER = object()


def read_file(path: str, owner: Any = ANY_OWNER) -> bytes:
    """File bytes; a sealed file is opened with the key named in its header."""
    with open(path, "rb") as handle:
        return open_file_bytes(handle.read(), owner)


def open_file_bytes(data: bytes, owner: Any = ANY_OWNER) -> bytes:
    """``data`` unchanged, or the plaintext of a sealed file written by ``write_file``.

    ``owner`` limits the keys: a session id allows that session and uploads,
    "" allows uploads only. Give it for every path a model can choose, so one
    session cannot read another session's sealed file. ``ANY_OWNER`` (internal
    paths only) allows every key this process holds.
    """
    if not is_envelope(data):
        return data
    wanted = data[len(MAGIC) + 1:_HEADER_BYTES]
    with _LOCK:
        # An upload sealed before a restart is readable once unlocked, also
        # after the switch is turned off.
        uploads = _upload_codec_locked()
        if owner is ANY_OWNER:
            allowed = list(_codecs.values())
        else:
            allowed = [uploads] if uploads is not None else []
            session = codec_for(owner) if owner else None
            if session is not None:
                allowed.append(session)
    codec = next((c for c in allowed if hmac.compare_digest(key_id(c._key), wanted)), None)
    if codec is None:
        if _master is None:
            raise SealLocked("this file belongs to an encrypted session that is not open")
        raise SealError("this file belongs to another encrypted session")
    return codec.open(FILE_PURPOSE, data)
