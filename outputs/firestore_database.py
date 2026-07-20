from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from google.cloud import firestore

from logger import get_logger
from models import Article, ArticleSummary, DigestResult

logger = get_logger(__name__)

_MAX_BATCH_WRITES = 450


def make_document_id(*parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class FirestoreDatabase:
    """Firestore persistence used by the Cloud Run job."""

    def __init__(self, project_id: str | None = None, database: str = "(default)"):
        self.client = firestore.Client(project=project_id, database=database)

    def is_email_processed(self, uidl: str) -> bool:
        ref = self.client.collection("processedEmails").document(make_document_id(uidl))
        return ref.get().exists

    def is_article_processed(self, source_type: str, source_id: str) -> bool:
        ref = self.client.collection("articleSummaries").document(
            make_document_id(source_type, source_id)
        )
        return ref.get().exists

    def save_batch(
        self,
        summaries: list[tuple[Article, ArticleSummary, int | None, str | None]],
        digest: DigestResult,
        embeddings=None,
    ) -> int:
        email_count = sum(1 for article, *_ in summaries if article.source_type == "email")
        write_count = 1 + len(summaries) + email_count
        if write_count > _MAX_BATCH_WRITES:
            raise ValueError(
                f"Firestore の一括書き込み上限を超えます: {write_count} writes"
            )

        now = datetime.now(UTC)
        batch_id = int(now.timestamp() * 1000)
        batch_ref = self.client.collection("batches").document(str(batch_id))
        write_batch = self.client.batch()
        write_batch.create(
            batch_ref,
            {
                "id": batch_id,
                "executed_at": now,
                "total_articles": len(summaries),
                "digest_text": digest.overview,
                "status": "complete",
            },
        )

        for i, (article, summary, group_id, group_topic) in enumerate(summaries):
            summary_ref = self.client.collection("articleSummaries").document(
                make_document_id(article.source_type, article.source_id)
            )
            embedding = embeddings[i].tolist() if embeddings is not None else None
            write_batch.create(
                summary_ref,
                {
                    "id": summary_ref.id,
                    "batch_id": batch_id,
                    "source_type": article.source_type,
                    "source_id": article.source_id,
                    "original_title": article.title,
                    "original_url": article.url,
                    "summary_title": summary.title,
                    "summary_text": summary.summary,
                    "keywords": summary.keywords,
                    "category": summary.category,
                    "group_id": group_id,
                    "group_topic": group_topic,
                    "published_at": article.published_at,
                    "created_at": now,
                    "embedding": embedding,
                },
            )
            if article.source_type == "email":
                email_ref = self.client.collection("processedEmails").document(
                    make_document_id(article.source_id)
                )
                write_batch.set(
                    email_ref,
                    {"uidl": article.source_id, "processed_at": now},
                )

        write_batch.commit()
        logger.info("Firestoreへバッチを保存しました (batch_id: %d)", batch_id)
        return batch_id

    @contextmanager
    def execution_lock(self, ttl: timedelta = timedelta(hours=2)):
        """Prevent overlapping Cloud Scheduler executions using a lease document."""
        lock_ref = self.client.collection("pipelineLocks").document("current")
        owner = uuid.uuid4().hex
        now = datetime.now(UTC)
        transaction = self.client.transaction()

        @firestore.transactional
        def acquire(txn):
            snapshot = lock_ref.get(transaction=txn)
            if snapshot.exists:
                expires_at = snapshot.get("expires_at")
                if expires_at and expires_at > now:
                    raise RuntimeError("別のパイプライン実行が進行中です。")
            txn.set(
                lock_ref,
                {"owner": owner, "acquired_at": now, "expires_at": now + ttl},
            )

        acquire(transaction)
        try:
            yield
        finally:
            release_transaction = self.client.transaction()

            @firestore.transactional
            def release(txn):
                snapshot = lock_ref.get(transaction=txn)
                if snapshot.exists and snapshot.get("owner") == owner:
                    txn.delete(lock_ref)

            release(release_transaction)
