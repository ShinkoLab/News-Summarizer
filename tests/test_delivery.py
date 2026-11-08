"""Durable Discord delivery tests: no network or cloud credentials."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest

from outputs.database import Database
from outputs.delivery import (
    MAX_DELIVERIES_PER_RUN,
    MAX_PAYLOAD_BYTES,
    drain_deliveries,
    new_delivery,
    retry_delay,
)
from tests.test_firestore_database import _build, _digest, _summaries


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "outbox.db"))


def _save(db, payloads=None):
    delivery = new_delivery(payloads or [{"content": "first"}, {"content": "second"}])
    db.save_batch(_summaries(1), _digest(), delivery=delivery)
    return delivery


def _http_error(status=429, payload=None, headers=None):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example.com"),
                              json=payload, headers=headers)
    return httpx.HTTPStatusError("rejected", request=response.request, response=response)


def test_new_delivery_freezes_payload_without_webhook_secret():
    payloads = [{"embeds": [{"description": "original"}]}]
    delivery = new_delivery(payloads)
    payloads[0]["embeds"][0]["description"] = "changed"
    assert json.loads(delivery["payloads"])[0]["embeds"][0]["description"] == "original"
    assert set(delivery) == {"id", "payloads", "next_payload", "created_at", "next_attempt_at", "attempts"}
    assert new_delivery([]) is None
    assert new_delivery([{"content": "same"}])["id"] != new_delivery([{"content": "same"}])["id"]


def test_new_delivery_size_bound_is_utf8_bytes():
    with pytest.raises(ValueError, match="512 KiB"):
        new_delivery([{"content": "あ" * (MAX_PAYLOAD_BYTES // 2)}])


def test_sqlite_outbox_saved_with_articles_and_email_marker(db):
    summaries = _summaries(1, "email")
    delivery = new_delivery([{"content": "frozen"}])
    result = db.save_batch(summaries, _digest(), delivery=delivery)
    row, = db.pending_deliveries()
    assert row == {**delivery, "batch_id": result.batch_id}
    assert db.is_article_processed("email", "id-0")
    assert db.is_email_processed("id-0")


def test_sqlite_failed_outbox_insert_rolls_back_all_article_work(db):
    _save(db)
    existing, = db.pending_deliveries()
    with pytest.raises(sqlite3.IntegrityError):
        db.save_batch(_summaries(1, "email"), _digest(), delivery=existing)
    assert not db.is_email_processed("id-0")
    assert not db.is_article_processed("email", "id-0")
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert db.pending_deliveries() == [existing]


def test_sqlite_full_outbox_rolls_back_new_batch(db):
    _save(db)
    with patch("outputs.database.MAX_PENDING", 1), pytest.raises(RuntimeError, match="full"):
        db.save_batch(_summaries(1, "email"), _digest(), delivery=new_delivery([{"content": "new"}]))
    assert not db.is_email_processed("id-0")
    assert len(db.pending_deliveries()) == 1


def test_failed_second_payload_resumes_without_reposting_first(db):
    delivery = _save(db)
    output = MagicMock(webhook_url="https://example.com/private-token")
    output.send_payload.side_effect = [None, httpx.ReadTimeout("timed out")]
    with patch("outputs.delivery.time.time", return_value=100), pytest.raises(httpx.ReadTimeout):
        drain_deliveries(db, output)
    pending, = db.pending_deliveries()
    assert pending["next_payload"] == 1
    assert pending["attempts"] == 1
    assert pending["next_attempt_at"] == 130
    assert pending["payloads"] == delivery["payloads"]
    output.reset_mock(side_effect=True)
    db = Database(str(db.db_path))  # Resume from disk, not the prior Python object.
    with patch("outputs.delivery.time.time", return_value=131):
        assert drain_deliveries(db, output) == 1
    output.send_payload.assert_called_once_with({"content": "second"})
    assert db.pending_deliveries() == []


def test_429_keeps_pending_and_obeys_not_before(db):
    _save(db)
    output = MagicMock(webhook_url="https://example.com")
    output.send_payload.side_effect = _http_error(payload={"retry_after": 120.5})
    with patch("outputs.delivery.time.time", return_value=100), pytest.raises(httpx.HTTPStatusError):
        drain_deliveries(db, output)
    assert db.pending_deliveries()[0]["next_attempt_at"] == 220.5
    output.reset_mock()
    with patch("outputs.delivery.time.time", return_value=220):
        assert drain_deliveries(db, output) == 0
    output.send_payload.assert_not_called()


@pytest.mark.parametrize("error, expected", [
    (_http_error(payload={"retry_after": 600}), 600),
    (_http_error(payload={}, headers={"Retry-After": "90"}), 90),
    (_http_error(payload={"retry_after": "nan"}), 30),
    (_http_error(payload={"retry_after": -1}), 30),
    (_http_error(status=500), 30),
    (httpx.ConnectError("offline"), 30),
])
def test_retry_delay(error, expected):
    assert retry_delay(error) == expected


def test_missing_webhook_does_not_read_or_mutate_outbox():
    db = MagicMock()
    assert drain_deliveries(db, MagicMock(webhook_url=None)) == 0
    assert db.mock_calls == []


def test_recovery_query_and_http_work_are_bounded(db):
    for _ in range(MAX_DELIVERIES_PER_RUN + 2):
        _save(db, [{"content": "one"}])
    output = MagicMock(webhook_url="https://example.com")
    assert drain_deliveries(db, output) == MAX_DELIVERIES_PER_RUN
    assert output.send_payload.call_count == MAX_DELIVERIES_PER_RUN
    assert len(db.pending_deliveries()) == 2


def test_checkpoint_failure_retains_message_for_at_least_once_retry(db):
    _save(db, [{"content": "one"}])
    output = MagicMock(webhook_url="https://example.com")
    with patch.object(db, "update_delivery", side_effect=RuntimeError("disk failure")):
        with pytest.raises(RuntimeError, match="disk failure"):
            drain_deliveries(db, output)
    assert db.pending_deliveries()[0]["next_payload"] == 0
    assert drain_deliveries(db, output) == 1
    assert output.send_payload.call_count == 2  # Accepted HTTP alone cannot establish exactly-once.


def _outbox_writes(fake, delivery_id):
    return [[call for call in batch.set.call_args_list if call.args[0].id == delivery_id]
            for batch in fake.batches]


@pytest.mark.parametrize("batch_doc_fails", [False, True])
def test_firestore_reuses_one_intent_atomically_in_each_chunk(batch_doc_fails):
    db, fake = _build(batch_doc_fails=batch_doc_fails)
    delivery = new_delivery([{"content": "immutable entire generated batch"}])
    with patch("outputs.firestore_database._MAX_BATCH_WRITES", 3):
        result = db.save_batch(_summaries(3), _digest(), delivery=delivery)
    assert len(result.saved) == 3
    assert (result.batch_id is None) == batch_doc_fails
    writes = _outbox_writes(fake, delivery["id"])
    assert len(writes) == 3
    assert all(len(chunk) == 1 for chunk in writes)
    assert all(chunk[0].args[1] == writes[0][0].args[1] for chunk in writes)
    assert writes[0][0].args[1]["payloads"] == delivery["payloads"]
    assert all(batch.create.call_count + batch.set.call_count == 3 for batch in fake.batches)
    # There is no independent outbox document write after the article commits.
    assert all(batch.commit.call_count == 1 for batch in fake.batches)


def test_firestore_all_failed_chunks_leave_no_confirmed_saved_delivery():
    db, fake = _build(fail_commits={0, 1})
    delivery = new_delivery([{"content": "frozen"}])
    with patch("outputs.firestore_database._MAX_BATCH_WRITES", 3):
        result = db.save_batch(_summaries(2), _digest(), delivery=delivery)
    assert result.saved == []
    assert result.failed == 2
    assert result.batch_id is None
    fake.batch_doc.create.assert_not_called()
    # Each outbox write participates only in the same rejected atomic commit.
    assert all(len(chunk) == 1 for chunk in _outbox_writes(fake, delivery["id"]))


def test_firestore_partial_save_intent_keeps_original_generated_payload():
    db, fake = _build(fail_commits={0})
    delivery = new_delivery([{"content": "both generated articles"}])
    with patch("outputs.firestore_database._MAX_BATCH_WRITES", 3):
        result = db.save_batch(_summaries(2), _digest(), delivery=delivery)
    assert result.saved == [("rss", "id-1")]
    assert result.failed == 1
    assert _outbox_writes(fake, delivery["id"])[1][0].args[1]["payloads"] == delivery["payloads"]


def test_firestore_reserves_payload_size_in_chunk_budget():
    db, fake = _build()
    delivery = new_delivery([{"content": "x" * 1000}])
    # Article data fits together without the intent, but not with its payload.
    with patch("outputs.firestore_database._MAX_CHUNK_BYTES", 1700):
        db.save_batch(_summaries(2), _digest(), delivery=delivery)
    assert len(fake.batches) == 2
    db_without, fake_without = _build()
    with patch("outputs.firestore_database._MAX_CHUNK_BYTES", 1700):
        db_without.save_batch(_summaries(2), _digest())
    assert len(fake_without.batches) == 1


def test_firestore_full_outbox_stops_before_any_article_commit():
    db, fake = _build()
    with patch.object(db, "pending_deliveries", return_value=[{}] * 100):
        with pytest.raises(RuntimeError, match="full"):
            db.save_batch(_summaries(1), _digest(), delivery=new_delivery([{"content": "new"}]))
    assert fake.batches == []


def test_firestore_pending_query_is_bounded_single_field_order():
    db, _ = _build()
    collection = MagicMock()
    snapshot = MagicMock()
    snapshot.to_dict.return_value = {"id": "pending"}
    collection.order_by.return_value.limit.return_value.stream.return_value = [snapshot]
    with patch.object(db.client, "collection", return_value=collection) as get_collection:
        assert db.pending_deliveries(7) == [{"id": "pending"}]
    get_collection.assert_called_once_with("discordOutbox")
    collection.order_by.assert_called_once_with("created_at")
    collection.order_by.return_value.limit.assert_called_once_with(7)


def test_firestore_progress_updates_never_rewrite_immutable_payload():
    db, _ = _build()
    collection = MagicMock()
    with patch.object(db.client, "collection", return_value=collection):
        db.update_delivery("delivery-id", 2, 123.0, 1)
        db.delete_delivery("delivery-id")
    collection.document.assert_any_call("delivery-id")
    collection.document.return_value.update.assert_called_once_with({
        "next_payload": 2, "next_attempt_at": 123.0, "attempts": 1,
    })
    collection.document.return_value.delete.assert_called_once_with()


def test_webhook_cooldown_blocks_other_pending_and_new_deliveries(db):
    first = _save(db, [{"content": "first"}])
    second = _save(db, [{"content": "second"}])
    db.update_delivery(first["id"], 0, 200.0, 1)
    output = MagicMock(webhook_url="https://example.com")
    with patch("outputs.delivery.time.time", return_value=150):
        assert drain_deliveries(db, output) == 0
        third = _save(db, [{"content": "new"}])
        assert drain_deliveries(db, output) == 0
    output.send_payload.assert_not_called()
    assert {row["id"] for row in db.pending_deliveries()} == {first["id"], second["id"], third["id"]}


def test_non429_failure_allows_other_delivery_but_raises_afterward(db):
    first = _save(db, [{"content": "failed"}, {"content": "must not send"}])
    _save(db, [{"content": "other delivery"}])
    error = _http_error(status=400)
    output = MagicMock(webhook_url="https://example.com")
    output.send_payload.side_effect = [error, None]
    with patch("outputs.delivery.time.time", return_value=100), pytest.raises(httpx.HTTPStatusError) as caught:
        drain_deliveries(db, output)
    assert caught.value is error
    assert [call.args[0] for call in output.send_payload.call_args_list] == [
        {"content": "failed"}, {"content": "other delivery"},
    ]
    pending, = db.pending_deliveries()
    assert pending["id"] == first["id"]
    assert pending["next_payload"] == 0
    assert pending["next_attempt_at"] == 130


def test_429_stops_other_deliveries_in_same_drain(db):
    _save(db, [{"content": "first"}])
    _save(db, [{"content": "second"}])
    output = MagicMock(webhook_url="https://example.com")
    output.send_payload.side_effect = _http_error(payload={"retry_after": 90})
    with patch("outputs.delivery.time.time", return_value=100), pytest.raises(httpx.HTTPStatusError):
        drain_deliveries(db, output)
    output.send_payload.assert_called_once_with({"content": "first"})
    assert len(db.pending_deliveries()) == 2


@pytest.mark.parametrize("attempts, expected", [(0, 30), (1, 60), (5, 960), (10, 21600), (100000, 21600)])
def test_retry_backoff_grows_but_is_bounded(attempts, expected):
    assert retry_delay(httpx.ConnectError("offline"), attempts) == expected


def test_429_respects_long_server_delay_even_above_backoff_cap():
    assert retry_delay(_http_error(payload={"retry_after": 86400}), 100) == 86400
    assert retry_delay(_http_error(payload={"retry_after": 1}), 2) == 120


def test_describe_failure_keeps_db_messages_but_redacts_http_errors():
    from outputs.delivery import describe_failure
    assert describe_failure(RuntimeError("Discord outbox is full")) == "RuntimeError: Discord outbox is full"
    secret_url = "https://discord.com/api/webhooks/1/secret-token"
    assert "secret-token" not in describe_failure(_http_error(404))
    assert describe_failure(_http_error(404)) == "HTTPStatusError (HTTP 404)"
    connect = httpx.ConnectError(f"cannot connect to {secret_url}")
    assert describe_failure(connect) == "ConnectError"
    try:
        try:
            raise connect
        except httpx.ConnectError as inner:
            raise RuntimeError(f"wrapped {inner}") from inner
    except RuntimeError as wrapped:
        assert describe_failure(wrapped) == "ConnectError"
