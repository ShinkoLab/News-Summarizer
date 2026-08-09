from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from models import Article, ArticleSummary, DigestResult
from outputs.firestore_database import FirestoreDatabase, make_document_id


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


@patch("outputs.firestore_database.firestore.Client")
def test_save_batch_uses_one_atomic_firestore_batch(mock_client_class):
    client = mock_client_class.return_value
    write_batch = client.batch.return_value
    batch_ref = MagicMock(id="batch-document")
    summary_ref = MagicMock(id=make_document_id("email", "mail-1"))
    email_ref = MagicMock(id=make_document_id("mail-1"))

    def collection(name):
        collection_ref = MagicMock()
        if name == "batches":
            collection_ref.document.return_value = batch_ref
        elif name == "articleSummaries":
            collection_ref.document.return_value = summary_ref
        elif name == "processedEmails":
            collection_ref.document.return_value = email_ref
        return collection_ref

    client.collection.side_effect = collection
    database = FirestoreDatabase(project_id="test-project")

    database.save_batch(
        [(_article("email", "mail-1"), _summary(), None, None)],
        DigestResult(overview="概要", categories=[], total_articles=1),
    )

    assert write_batch.create.call_count == 2
    write_batch.set.assert_called_once()
    write_batch.commit.assert_called_once_with()
