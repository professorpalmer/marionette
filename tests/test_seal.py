"""Sealed sessions crypto core: envelope, keys, fail-closed rules."""
from __future__ import annotations

import json
import os
import secrets

import pytest

from harness import seal


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    seal.lock()
    seal._sealed_ids.clear()
    yield tmp_path
    seal.lock()
    seal._sealed_ids.clear()


def _configure(master: bytes, enabled: bool = True) -> None:
    os.makedirs(seal.seal_dir(), exist_ok=True)
    with open(seal.config_path(), "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "enabled": enabled, "key_id": seal.key_id(master).hex()}, fh)


def test_envelope_round_trip_and_layout():
    key = secrets.token_bytes(32)
    blob = seal.encrypt(key, "transcript:abc", b"hello")
    assert blob.startswith(b"MNSL\x01")
    assert blob[5:13] == seal.key_id(key)
    assert b"hello" not in blob
    assert seal.decrypt(key, "transcript:abc", blob) == b"hello"
    assert seal.encrypt(key, "p", b"x") != seal.encrypt(key, "p", b"x")


@pytest.mark.parametrize("mutate", [
    lambda b: b[:-1] + bytes([b[-1] ^ 1]),
    lambda b: b[:20] + bytes([b[20] ^ 1]) + b[21:],
    lambda b: b[:13] + bytes([b[13] ^ 1]) + b[14:],
    lambda b: b[:4] + b"\x02" + b[5:],
    lambda b: b"XXXX" + b[4:],
    lambda b: b[:30],
])
def test_changed_ciphertext_fails(mutate):
    key = secrets.token_bytes(32)
    blob = seal.encrypt(key, "p", b"secret text")
    with pytest.raises(seal.SealError):
        seal.decrypt(key, "p", mutate(blob))


def test_purpose_and_key_bind_the_blob():
    key = secrets.token_bytes(32)
    blob = seal.encrypt(key, "transcript:a", b"x")
    with pytest.raises(seal.SealError):
        seal.decrypt(key, "transcript:b", blob)
    with pytest.raises(seal.SealError):
        seal.decrypt(secrets.token_bytes(32), "transcript:a", blob)


def test_unlock_needs_matching_master(state):
    master = secrets.token_bytes(32)
    with pytest.raises(seal.SealError):
        seal.unlock(master)
    _configure(master)
    with pytest.raises(seal.SealError):
        seal.unlock(secrets.token_bytes(32))
    with pytest.raises(seal.SealError):
        seal.unlock(master[:16])
    assert not seal.unlocked()
    seal.unlock(master)
    assert seal.status() == {"configured": True, "enabled": True, "unlocked": True}


def test_session_codec_round_trip_and_isolation(state):
    master = secrets.token_bytes(32)
    _configure(master)
    seal.unlock(master)
    a = seal.create_session("sessa")
    b = seal.create_session("sessb")
    text = a.seal_text("title", "my private title")
    assert seal.is_sealed_text(text)
    assert "private" not in text
    assert a.open_text("title", text) == "my private title"
    with pytest.raises(seal.SealError):
        b.open_text("title", text)
    with pytest.raises(seal.SealError):
        a.open_text("other", text)
    blob = a.seal_json("transcript", {"history": ["hi"]})
    assert a.open_json("transcript", blob) == {"history": ["hi"]}
    assert "sessa" in repr(a) and repr(a).count("'") == 2


def test_session_key_file_holds_no_plain_key(state):
    master = secrets.token_bytes(32)
    _configure(master)
    seal.unlock(master)
    codec = seal.create_session("sessa")
    wrapped = open(seal.session_key_path("sessa"), "rb").read()
    assert codec._key not in wrapped
    assert seal.is_envelope(wrapped)
    with pytest.raises(seal.SealError):
        seal.create_session("sessa")


def test_locked_sealed_session_raises_and_plain_session_is_none(state):
    master = secrets.token_bytes(32)
    _configure(master)
    seal.unlock(master)
    codec = seal.create_session("sessa")
    blob = codec.seal("transcript", b"data")
    seal.lock()
    assert seal.codec_for("plainsess") is None
    with pytest.raises(seal.SealLocked):
        seal.codec_for("sessa")
    with pytest.raises(seal.SealLocked):
        seal.create_session("sessc")
    seal.unlock(master)
    assert seal.codec_for("sessa").open("transcript", blob) == b"data"


def test_either_marker_keeps_a_session_sealed(state):
    master = secrets.token_bytes(32)
    _configure(master)
    seal.unlock(master)
    seal.create_session("sessa")
    seal._sealed_ids.clear()
    assert seal.is_sealed("sessa")
    seal.mark_sealed("sessb")
    assert seal.is_sealed("sessb")
    with pytest.raises(seal.SealError):
        seal.codec_for("sessb")


def test_erase_session_makes_ciphertext_unreadable(state):
    master = secrets.token_bytes(32)
    _configure(master)
    seal.unlock(master)
    codec = seal.create_session("sessa")
    blob = codec.seal("transcript", b"data")
    seal.erase_session("sessa")
    assert not os.path.exists(seal.session_key_path("sessa"))
    with pytest.raises(seal.SealError):
        seal.codec_for("sessa")
    seal.erase_session("sessa")
    assert blob


@pytest.mark.parametrize("sid", ["", "../x", "a/b", "a" * 129, "a b"])
def test_bad_session_ids_are_refused(state, sid):
    with pytest.raises(seal.SealError):
        seal.session_key_path(sid)
