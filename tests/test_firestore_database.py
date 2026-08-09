from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np

from models import Article, ArticleSummary, DigestResult
from outputs.firestore_database import (
    _MAX_BATCH_WRITES,
    FirestoreDatabase,
    estimate_document_size,
    make_document_id,
)


def _article(source_type: str = "rss", source_id: str = "42") -> Article:
    now = datetime.now()
    return Article(
        source_type=source_type,
        source_id=source_id,
        title="Original",
        content="Content",
        url="https://example.com/42",
        published_at=now,
        fetched_at=now,
        feed_title="Feed",
    )


def _summary() -> ArticleSummary:
    return ArticleSummary(
        title="要約タイトル",
        summary="要約本文",
        keywords=["AI", "クラウド"],
        category="テクノロジー",
    )


def test_document_id_is_stable_and_source_type_sensitive():
    assert make_document_id("rss", "42") == make_document_id("rss", "42")
    assert make_document_id("rss", "42") != make_document_id("email", "42")
    assert len(make_document_id("rss", "42")) == 64


def _digest() -> DigestResult:
    return DigestResult(overview="概要", categories=[], total_articles=1)


class _FakeFirestore:
    """Records write batches so tests can assert how commits were split.

    Each `client.batch()` returns a fresh recorder, so the number of recorders
    is the number of commits the code decided to make.
    """

    def __init__(self, fail_commits: set[int] | None = None, batch_doc_fails: bool = False):
        self.batches: list[MagicMock] = []
        self.fail_commits = fail_commits or set()
        self.batch_doc_fails = batch_doc_fails
        self.batch_doc = MagicMock(id="batch-document")
        if batch_doc_fails:
            self.batch_doc.create.side_effect = RuntimeError("batch doc boom")

    def batch(self) -> MagicMock:
        index = len(self.batches)
        write_batch = MagicMock()
        write_batch.create = MagicMock()
        write_batch.set = MagicMock()
        write_batch.delete = MagicMock()
        if index in self.fail_commits:
            write_batch.commit.side_effect = RuntimeError("transaction too big")
        self.batches.append(write_batch)
        return write_batch

    def collection(self, name: str) -> MagicMock:
        collection_ref = MagicMock()
        if name == "batches":
            collection_ref.document.return_value = self.batch_doc
        else:
            collection_ref.document.side_effect = lambda doc_id: MagicMock(id=doc_id)
        return collection_ref


def _build(fail_commits=None, batch_doc_fails=False):
    with patch("outputs.firestore_database.firestore.Client") as mock_client_class:
        fake = _FakeFirestore(fail_commits, batch_doc_fails)
        client = mock_client_class.return_value
        client.batch.side_effect = fake.batch
        client.collection.side_effect = fake.collection
        return FirestoreDatabase(project_id="test-project"), fake


def _summaries(count: int, source_type: str = "rss"):
    return [
        (_article(source_type, f"id-{i}"), _summary(), None, None) for i in range(count)
    ]


def test_estimate_document_size_is_dominated_by_the_embedding():
    small = estimate_document_size({"summary_text": "あ" * 100})
    with_embedding = estimate_document_size(
        {"summary_text": "あ" * 100, "embedding": [0.1] * 1536}
    )
    assert with_embedding - small >= 1536 * 8


def test_save_batch_writes_article_and_email_documents():
    database, fake = _build()

    result = database.save_batch(
        [(_article("email", "mail-1"), _summary(), None, None)], _digest()
    )

    write_batch = fake.batches[0]
    write_batch.create.assert_called_once()   # articleSummaries
    write_batch.set.assert_called_once()      # processedEmails
    write_batch.delete.assert_called_once()   # emailAttempts の後始末
    assert result.saved == [("email", "mail-1")]
    assert result.failed == 0


def test_batch_document_is_committed_last():
    """記事より先にバッチを書くと「バッチはあるのに記事が欠けている」状態を作る。"""
    database, fake = _build()

    database.save_batch(_summaries(2), _digest())

    fake.batch_doc.create.assert_called_once()
    # バッチドキュメントは write batch ではなく単体で書く。記事側のコミットは
    # すべて完了している。
    for write_batch in fake.batches:
        write_batch.commit.assert_called_once_with()


def test_save_batch_splits_on_write_count():
    database, fake = _build()

    result = database.save_batch(_summaries(_MAX_BATCH_WRITES + 10), _digest())

    assert len(fake.batches) == 2
    assert len(result.saved) == _MAX_BATCH_WRITES + 10


def test_save_batch_splits_on_estimated_size():
    """件数では上限に届かなくても、embedding のサイズでコミットが分かれる。"""
    database, fake = _build()
    count = 40
    # 1件あたり約 1.5 MiB 相当。件数上限（450）には遠く及ばない。
    embeddings = [np.zeros(48_000) for _ in range(count)]

    database.save_batch(_summaries(count), _digest(), embeddings)

    assert len(fake.batches) > 1
    assert fake.batch_doc.create.call_args.args[0]["total_articles"] == count


def test_failed_chunk_does_not_raise_and_keeps_the_rest():
    database, fake = _build(fail_commits={0})

    result = database.save_batch(_summaries(_MAX_BATCH_WRITES + 10), _digest())

    assert len(result.saved) == 10
    assert result.failed == _MAX_BATCH_WRITES
    assert result.is_partial
    batch_doc_data = fake.batch_doc.create.call_args.args[0]
    assert batch_doc_data["status"] == "partial"
    assert batch_doc_data["total_articles"] == 10


def test_no_batch_document_when_nothing_was_saved():
    """空バッチを Viewer に見せない。"""
    database, fake = _build(fail_commits={0})

    result = database.save_batch(_summaries(2), _digest())

    fake.batch_doc.create.assert_not_called()
    assert result.batch_id is None
    assert result.saved == []
    assert result.failed == 2


def test_batch_document_failure_reports_no_batch_id():
    database, fake = _build(batch_doc_fails=True)

    result = database.save_batch(_summaries(2), _digest())

    # 記事は保存されているので既読化してよい。バッチIDは無い。
    assert result.batch_id is None
    assert len(result.saved) == 2
