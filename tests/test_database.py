"""Tests for outputs/database.py against a temp SQLite DB."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from models import Article, ArticleSummary
from outputs.database import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_article(source_id: str = "rss-001", source_type: str = "rss") -> Article:
    now = datetime.now()
    return Article(
        source_type=source_type,
        source_id=source_id,
        title="Test Article",
        content="Article content here.",
        url="https://example.com/article",
        published_at=now,
        fetched_at=now,
        feed_title="Test Feed",
    )


def _make_summary() -> ArticleSummary:
    return ArticleSummary(
        title="テスト記事タイトル",
        summary="これはテスト記事の要約です。",
        keywords=["テスト", "記事"],
        category="テクノロジー",
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path) -> Database:
    db_path = str(tmp_path / "test.db")
    return Database(db_path=db_path)


# ---------------------------------------------------------------------------
# create_batch
# ---------------------------------------------------------------------------

class TestCreateBatch:
    def test_returns_integer_id(self, db):
        batch_id = db.create_batch(total_articles=5, digest_text="Overview text")
        assert isinstance(batch_id, int)
        assert batch_id >= 1

    def test_sequential_ids(self, db):
        id1 = db.create_batch(total_articles=1)
        id2 = db.create_batch(total_articles=2)
        assert id2 > id1

    def test_digest_text_stored(self, db):
        batch_id = db.create_batch(total_articles=3, digest_text="My digest")
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT digest_text FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()
        assert row["digest_text"] == "My digest"


# ---------------------------------------------------------------------------
# save_summary → readable row
# ---------------------------------------------------------------------------

class TestSaveSummary:
    def test_save_and_read_back(self, db):
        article = _make_article()
        summary = _make_summary()
        batch_id = db.create_batch(total_articles=1)

        db.save_summary(batch_id, article, summary)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM article_summaries WHERE batch_id = ?", (batch_id,)
            ).fetchone()

        assert row["source_id"] == article.source_id
        assert row["summary_title"] == summary.title
        assert row["category"] == summary.category

    def test_keywords_stored_as_json(self, db):
        article = _make_article()
        summary = _make_summary()
        batch_id = db.create_batch(total_articles=1)

        db.save_summary(batch_id, article, summary)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT keywords FROM article_summaries WHERE batch_id = ?", (batch_id,)
            ).fetchone()

        loaded = json.loads(row["keywords"])
        assert loaded == summary.keywords

    def test_embedding_none_stored_as_null(self, db):
        article = _make_article()
        summary = _make_summary()
        batch_id = db.create_batch(total_articles=1)

        db.save_summary(batch_id, article, summary, embedding=None)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT embedding FROM article_summaries WHERE batch_id = ?", (batch_id,)
            ).fetchone()

        assert row["embedding"] is None

    def test_embedding_list_round_trips(self, db):
        article = _make_article()
        summary = _make_summary()
        batch_id = db.create_batch(total_articles=1)
        original_embedding = [0.1, 0.2, 0.3, 0.4, 0.5]

        db.save_summary(batch_id, article, summary, embedding=original_embedding)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT embedding FROM article_summaries WHERE batch_id = ?", (batch_id,)
            ).fetchone()

        loaded = json.loads(row["embedding"])
        assert loaded == pytest.approx(original_embedding)

    def test_group_info_stored(self, db):
        article = _make_article()
        summary = _make_summary()
        batch_id = db.create_batch(total_articles=1)

        db.save_summary(batch_id, article, summary, group_id=42, group_topic="AI News")

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT group_id, group_topic FROM article_summaries WHERE batch_id = ?",
                (batch_id,)
            ).fetchone()

        assert row["group_id"] == 42
        assert row["group_topic"] == "AI News"


# ---------------------------------------------------------------------------
# mark_email_processed / is_email_processed
# ---------------------------------------------------------------------------

class TestEmailProcessing:
    def test_mark_and_check(self, db):
        db.mark_email_processed("uidl-abc-123")
        assert db.is_email_processed("uidl-abc-123") is True

    def test_unprocessed_returns_false(self, db):
        assert db.is_email_processed("nonexistent-uidl") is False

    def test_mark_is_idempotent(self, db):
        """Calling mark twice should not raise (INSERT OR IGNORE)."""
        db.mark_email_processed("uidl-xyz")
        db.mark_email_processed("uidl-xyz")  # second call must not raise
        assert db.is_email_processed("uidl-xyz") is True

    def test_multiple_uidls_tracked_independently(self, db):
        db.mark_email_processed("uidl-1")
        db.mark_email_processed("uidl-2")

        assert db.is_email_processed("uidl-1") is True
        assert db.is_email_processed("uidl-2") is True
        assert db.is_email_processed("uidl-3") is False


# ---------------------------------------------------------------------------
# Email article save_summary calls mark_email_processed indirectly in pipeline,
# but we test that the DB correctly stores source_type="email"
# ---------------------------------------------------------------------------

class TestEmailArticleStorage:
    def test_email_article_source_type_stored(self, db):
        article = _make_article(source_id="email-uidl-001", source_type="email")
        summary = _make_summary()
        batch_id = db.create_batch(total_articles=1)

        db.save_summary(batch_id, article, summary)

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT source_type FROM article_summaries WHERE source_id = ?",
                (article.source_id,)
            ).fetchone()

        assert row["source_type"] == "email"
