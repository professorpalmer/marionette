"""Shared session stores keep sealed sessions out of their plain files."""
from __future__ import annotations

import json
import os
import secrets
import threading

import pytest

from harness import seal
from harness.prompt_queue import PromptQueueMixin
from harness.session_goal import SessionGoal, SessionGoalStore
from harness.session_scratch import SessionScratchStore
from harness.todo import SessionTodoStore, TodoItem, TodoPhase

CANARY = "CNRYsealedstore"


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    seal.lock()
    seal._sealed_ids.clear()
    master = secrets.token_bytes(32)
    os.makedirs(seal.seal_dir(), exist_ok=True)
    with open(seal.config_path(), "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "enabled": True, "key_id": seal.key_id(master).hex()}, fh)
    seal.unlock(master)
    seal.create_session("sealedsid")
    yield str(tmp_path), master
    seal.lock()
    seal._sealed_ids.clear()


def _canary_files(root: str) -> list:
    hits = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                if CANARY.encode() in fh.read():
                    hits.append(os.path.relpath(path, root))
    return hits


def test_scratch_follows_the_owner(sealed):
    state, _ = sealed
    owner = {"sid": "sealedsid"}
    store = SessionScratchStore(state, lambda: owner["sid"])
    store.set("k", CANARY)
    assert store.get("k") == CANARY
    assert not os.path.exists(os.path.join(state, "session_scratch.json"))
    owner["sid"] = "plainsid"
    assert store.get("k") is None
    store.set("p", "plain value")
    assert store.get("p") == "plain value"
    assert _canary_files(state) == []


def test_todos_goal_and_queue_never_write_plaintext(sealed):
    state, _ = sealed
    todos = SessionTodoStore(state)
    phases = [TodoPhase(name="phase", tasks=[TodoItem(content=CANARY)])]
    todos.save(phases, session_id="sealedsid")
    todos.save([TodoPhase(name="plain")], session_id="plainsid")
    assert todos.load(session_id="sealedsid")[0].tasks[0].content == CANARY
    assert todos.load(session_id="plainsid")[0].name == "plain"
    shared = json.load(open(os.path.join(state, "session_todos.json"), encoding="utf-8"))
    assert list(shared["sessions"]) == ["plainsid"]

    goals = SessionGoalStore(state, session_id="sealedsid")
    goals.save(SessionGoal().set(CANARY))
    assert SessionGoalStore(state, session_id="sealedsid").load().text == CANARY

    class Queue(PromptQueueMixin):
        def __init__(self):
            self.state_dir = state
            self.harness_session_id = "sealedsid"
            self._prompt_queue_lock = threading.RLock()

    queue = Queue()
    queue.bind_prompt_queue(state, "sealedsid")
    with queue._queue_transaction():
        queue._commit_queue([{"id": "a", "text": CANARY, "images": [], "model": ""}])
        assert queue._read_queue()[0]["text"] == CANARY

    assert _canary_files(state) == []


def test_locked_seal_fails_closed_and_erase_removes_stores(sealed):
    state, master = sealed
    store = SessionScratchStore(state, lambda: "sealedsid")
    store.set("k", CANARY)
    SessionTodoStore(state).save([TodoPhase(name=CANARY)], session_id="sealedsid")
    seal.lock()
    with pytest.raises(seal.SealLocked):
        store.get("k")
    with pytest.raises(seal.SealLocked):
        store.set("k", "overwrite")
    with pytest.raises(seal.SealLocked):
        SessionTodoStore(state).save([], session_id="sealedsid")
    seal.unlock(master)
    assert store.get("k") == CANARY
    seal.erase_session("sealedsid")
    assert not os.path.exists(seal.sealed_data_dir("sealedsid"))


def _multipart(name: str, data: bytes):
    boundary = "sealedboundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_uploads_are_sealed_and_served_while_unlocked(sealed, tmp_path):
    from harness.api.files import get_image, save_upload

    _state, master = sealed
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    pixels = b"\x89PNG\r\n" + CANARY.encode()
    status, payload = save_upload(*_multipart("shot.png", pixels), str(uploads))
    assert status == 200
    path = payload["saved"][0]["path"]
    assert seal.is_envelope(open(path, "rb").read())
    assert get_image(path, str(uploads)) == (200, pixels, "image/png")
    seal.lock()
    assert get_image(path, str(uploads))[0] == 423
    seal.unlock(master)
    assert get_image(path, str(uploads))[1] == pixels


def test_uploads_stay_plain_when_the_switch_is_off(tmp_path, monkeypatch):
    from harness.api.files import save_upload

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    seal.lock()
    status, payload = save_upload(*_multipart("a.txt", b"plain"), str(tmp_path))
    assert status == 200
    assert open(payload["saved"][0]["path"], "rb").read() == b"plain"


def test_sealed_document_reaches_the_model_as_plaintext(sealed, tmp_path):
    from harness.api.files import save_upload
    from harness.input_receipts import InputReceiptStore

    state, _ = sealed
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    status, payload = save_upload(*_multipart("notes.md", CANARY.encode()), str(uploads))
    assert status == 200
    doc = payload["saved"][0]["path"]
    store = InputReceiptStore(state, "sealedsid")
    first = store.admit("read this", documents=[doc], upload_root=str(uploads), retry_key="rk")
    again = store.admit("read this", documents=[doc], upload_root=str(uploads), retry_key="rk")
    assert again["id"] == first["id"]
    text, images = store.delivery_content(first["id"])
    assert CANARY in text and images == []
    assert _canary_files(state) == []
    assert _canary_files(str(uploads)) == []


def test_full_size_sealed_attachment_is_still_readable(sealed, tmp_path):
    from harness.input_receipts import _MAX_FILE_BYTES, InputReceiptStore

    state, _ = sealed
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    big = uploads / "big.png"
    big.write_bytes(b"\x89PNG" + b"\0" * (_MAX_FILE_BYTES - 4))
    store = InputReceiptStore(state, "sealedsid")
    row = store.admit("look", images=[str(big)], upload_root=str(uploads))
    ref = row["attachments"][0]["ref"]
    assert len(store.attachment(ref)) == _MAX_FILE_BYTES


def test_scheduled_runs_are_sealed_and_fail_closed_when_locked(sealed, monkeypatch):
    from harness import scheduler
    from harness.schedule_core import Schedule, continuity_block

    _state, master = sealed
    created = []

    class FakeSession:
        def __init__(self, _cfg):
            created.append(self)

    monkeypatch.setattr("harness.conversation.ConversationalSession", FakeSession)
    schedule = Schedule(id="s1", name="nightly", objective="check", cron="0 3 * * *")
    session = scheduler._default_session_factory(schedule)
    assert seal.codec_for(session.harness_session_id) is not None

    stored = scheduler._continuity_digest_for_ok(schedule, "ok", f"result {CANARY}")
    assert seal.is_sealed_text(stored) and CANARY not in stored
    schedule.continuity_digest = stored
    assert CANARY in continuity_block(schedule)

    seal.lock()
    with pytest.raises(seal.SealLocked):
        scheduler._default_session_factory(schedule)
    with pytest.raises(seal.SealLocked):
        continuity_block(schedule)
    seal.unlock(master)


def test_sealed_sessions_write_memory_only_to_the_sealed_layer(sealed, tmp_path):
    from harness.memory_store import MemoryStore, SessionMemory

    state, master = sealed
    plain = MemoryStore(str(tmp_path / "memory.json"))
    shared = plain.add("user likes tea")
    owner = {"sid": "sealedsid"}
    memory = SessionMemory(plain, lambda: owner["sid"])
    entry = memory.add(f"secret {CANARY}")
    assert memory.add("USER LIKES TEA").id == shared.id
    assert {e.text for e in memory.list()} == {"user likes tea", f"secret {CANARY}"}
    assert CANARY in memory.sealed_block()
    with pytest.raises(ValueError):
        memory.update(shared.id, f"changed {CANARY}")
    assert memory.update(entry.id, f"secret two {CANARY}")
    assert _canary_files(state) == [] and _canary_files(str(tmp_path)) == []

    owner["sid"] = "plainsid"
    assert [e.text for e in memory.list()] == ["user likes tea"]
    assert memory.sealed_block() == ""

    owner["sid"] = "sealedsid"
    seal.lock()
    with pytest.raises(seal.SealLocked):
        memory.list()
    with pytest.raises(seal.SealLocked):
        memory.add("while locked")
    seal.unlock(master)
    assert memory.remove(entry.id)
    assert [e.text for e in memory.list()] == ["user likes tea"]


def test_model_reachable_reads_never_open_another_sessions_file(sealed, tmp_path):
    other = seal.create_session("othersid")
    mine = seal.codec_for("sealedsid")
    theirs = tmp_path / "theirs.txt"
    seal.write_file(other, str(theirs), CANARY.encode())
    ours = tmp_path / "ours.txt"
    seal.write_file(mine, str(ours), b"mine")
    assert seal.read_file(str(ours), owner="sealedsid") == b"mine"
    with pytest.raises(seal.SealError):
        seal.read_file(str(theirs), owner="sealedsid")
    with pytest.raises(seal.SealError):
        seal.read_file(str(theirs), owner="plainsid")
    with pytest.raises(seal.SealError):
        seal.read_file(str(ours), owner="")
    assert seal.read_file(str(theirs)) == CANARY.encode()
    upload = tmp_path / "up.png"
    seal.write_file(seal.upload_codec(), str(upload), b"pixels")
    assert seal.read_file(str(upload), owner="") == b"pixels"
    assert seal.read_file(str(upload), owner="plainsid") == b"pixels"
