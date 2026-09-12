import hashlib
import json

import pytest

from harness.input_receipts import InputReceiptStore, InputReceiptError


def test_originals_retry_and_corruption(tmp_path):
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    image = uploads / 'one.png'
    image.write_bytes(b'original pixels')
    store = InputReceiptStore(str(tmp_path), 'session')
    first = store.admit('  exact text\n', images=[str(image)], upload_root=str(uploads), retry_key='one')
    assert store.admit('  exact text\n', images=[str(image)], upload_root=str(uploads), retry_key='one')['id'] == first['id']
    assert store.admit('  exact text\n', images=[str(image)], upload_root=str(uploads))['id'] != first['id']
    with pytest.raises(InputReceiptError):
        store.admit('different', retry_key='one')
    image.unlink()
    assert store.attachment(first['attachments'][0]['ref']) == b'original pixels'
    assert store.list()[0]['original_text'] == '  exact text\n'
    store.path.write_text('{broken')
    with pytest.raises(InputReceiptError):
        store.admit('must not overwrite')
    assert store.path.read_text() == '{broken'


def test_attempt_survives_cold_read_without_replay(tmp_path):
    store = InputReceiptStore(str(tmp_path), 'session')
    receipt = store.admit('hello')
    store.transition(receipt['id'], 'delivering')
    cold = InputReceiptStore(str(tmp_path), 'session')
    assert cold.list()[0]['status'] == 'uncertain'
    with pytest.raises(InputReceiptError):
        store.transition(receipt['id'], 'injected')


def test_same_instance_list_does_not_steal_in_flight_delivery(tmp_path):
    from harness.sessions import save_transcript

    store = InputReceiptStore(str(tmp_path), "session")
    row = store.admit("Continue from where you left off.")
    store.prepare_delivery(row["id"])
    transcript = {
        "history": [{
            "role": "user",
            "content": "Continue from where you left off.",
            "input_id": row["id"],
        }],
    }
    save_transcript(str(tmp_path), "session", transcript)
    assert store.list()[0]["status"] == "delivering"
    store.publish_injected([row["id"]], transcript)
    assert store.list()[0]["status"] == "injected"


def test_publish_injected_is_idempotent_once_evidence_is_on_disk(tmp_path):
    store = InputReceiptStore(str(tmp_path), "session")
    row = store.admit("again")
    store.prepare_delivery(row["id"])
    transcript = {
        "history": [{"role": "user", "content": "again", "input_id": row["id"]}],
    }
    store.publish_injected([row["id"]], transcript)
    store.publish_injected([row["id"]], transcript)
    assert [r["status"] for r in store.list()] == ["injected"]


def test_retry_key_opens_a_fresh_row_after_a_dead_attempt(tmp_path):
    store = InputReceiptStore(str(tmp_path), "session")
    first = store.admit("same ask", retry_key="rk")
    store.prepare_delivery(first["id"])
    store.publish_injected(
        [first["id"]],
        {"history": [{"role": "user", "content": "same ask", "input_id": first["id"]}]},
    )
    second = store.admit("same ask", retry_key="rk")
    assert second["id"] != first["id"]
    assert second["status"] == "accepted"


def test_cold_list_reconciles_foreign_delivering_when_transcript_has_id(tmp_path):
    from harness.sessions import save_transcript

    store = InputReceiptStore(str(tmp_path), "session")
    row = store.admit("hello")
    store.prepare_delivery(row["id"])
    save_transcript(str(tmp_path), "session", {
        "history": [{"role": "user", "content": "hello", "input_id": row["id"]}],
    })
    cold = InputReceiptStore(str(tmp_path), "session")
    assert cold.list()[0]["status"] == "injected"


def test_attachment_validation(tmp_path):
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    outside = tmp_path / 'secret.png'
    outside.write_bytes(b'private')
    store = InputReceiptStore(str(tmp_path), 'session')
    with pytest.raises(InputReceiptError):
        store.admit('no', images=[str(outside)], upload_root=str(uploads))
    assert store.list() == []
