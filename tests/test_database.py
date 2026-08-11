"""Tests for outputs/database.py against a temp SQLite DB."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from models import Article, ArticleSummary, DigestResult
from outputs.database import Database
from urls import url_key


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


class TestAtomicBatchStorage:
    def test_save_batch_persists_result_and_processed_email(self, db):
        article = _make_article(source_id="mail-001", source_type="email")

        result = db.save_batch(
            [(article, _make_summary(), 7, "テストトピック")],
            DigestResult(overview="全体概要", categories=[], total_articles=1),
        )

        with db.get_connection() as conn:
            batch = conn.execute(
                "SELECT * FROM batches WHERE id = ?", (result.batch_id,)
            ).fetchone()
            saved = conn.execute(
                "SELECT * FROM article_summaries WHERE batch_id = ?", (result.batch_id,)
            ).fetchone()
        assert batch["digest_text"] == "全体概要"
        assert saved["group_id"] == 7
        assert db.is_article_processed("email", "mail-001") is True
        assert db.is_email_processed("mail-001") is True
        assert result.saved == [("email", "mail-001")]
        assert result.failed == 0

    def test_save_batch_clears_recorded_email_attempts(self, db):
        """保存できたメールの試行回数は残さない（poison message判定を汚さない）。"""
        article = _make_article(source_id="mail-002", source_type="email")
        db.record_email_attempt("mail-002")

        db.save_batch(
            [(article, _make_summary(), None, None)],
            DigestResult(overview="全体概要", categories=[], total_articles=1),
        )

        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM email_attempts WHERE uidl = ?", ("mail-002",)
            ).fetchone()
        assert row is None


class TestEmailAttempts:
    def test_record_email_attempt_increments(self, db):
        assert db.record_email_attempt("uidl-a") == 1
        assert db.record_email_attempt("uidl-a") == 2
        assert db.record_email_attempt("uidl-a") == 3

    def test_record_email_attempt_is_per_uidl(self, db):
        db.record_email_attempt("uidl-a")
        db.record_email_attempt("uidl-a")
        assert db.record_email_attempt("uidl-b") == 1

    def test_attempt_does_not_mark_email_processed(self, db):
        """試行回数の記録だけでは取得対象から外れない。"""
        db.record_email_attempt("uidl-a")
        assert db.is_email_processed("uidl-a") is False


# ---------------------------------------------------------------------------
# 同一URLの重複判定（url_key）と feed_title の保存
# ---------------------------------------------------------------------------

class TestUrlDeduplication:
    def test_unknown_url_is_not_processed(self, db):
        assert db.is_url_processed(url_key("https://example.com/article")) is False

    def test_saved_article_makes_its_url_processed(self, db):
        db.save_batch(
            [(_make_article(), _make_summary(), None, None)],
            DigestResult(overview="全体概要", categories=[], total_articles=1),
        )

        assert db.is_url_processed(url_key("https://example.com/article")) is True

    def test_tracking_params_do_not_change_the_verdict(self, db):
        """別フィード経由でトラッキングパラメータ付きの同一URLが来ても検出できる。"""
        db.save_batch(
            [(_make_article(), _make_summary(), None, None)],
            DigestResult(overview="全体概要", categories=[], total_articles=1),
        )

        assert db.is_url_processed(
            url_key("https://www.example.com/article?utm_source=rss")
        ) is True

    def test_a_different_article_is_not_processed(self, db):
        db.save_batch(
            [(_make_article(), _make_summary(), None, None)],
            DigestResult(overview="全体概要", categories=[], total_articles=1),
        )

        assert db.is_url_processed(url_key("https://example.com/other")) is False

    def test_save_summary_also_records_the_url_key(self, db):
        batch_id = db.create_batch(total_articles=1)
        db.save_summary(batch_id, _make_article(), _make_summary())

        assert db.is_url_processed(url_key("https://example.com/article")) is True

    def test_feed_title_is_persisted(self, db):
        """Viewer が統合カードのソース名として使う。"""
        db.save_batch(
            [(_make_article(), _make_summary(), None, None)],
            DigestResult(overview="全体概要", categories=[], total_articles=1),
        )

        with db.get_connection() as conn:
            row = conn.execute("SELECT feed_title FROM article_summaries").fetchone()
        assert row["feed_title"] == "Test Feed"


class TestSchemaMigration:
    def test_new_columns_are_added_to_a_pre_existing_database(self, tmp_path):
        """url_key / feed_title 導入前のDBファイルをそのまま開けること。"""
        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE article_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                original_title TEXT NOT NULL,
                original_url TEXT,
                summary_title TEXT NOT NULL,
                summary_text TEXT NOT NULL,
                keywords TEXT NOT NULL,
                category TEXT NOT NULL,
                group_id INTEGER,
                group_topic TEXT,
                published_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
        conn.close()

        database = Database(db_path=str(db_path))

        with database.get_connection() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(article_summaries)")}
        assert {"embedding", "url_key", "feed_title"} <= cols

    def test_existing_duplicate_urls_do_not_block_startup(self, tmp_path):
        """url_key に UNIQUE を張っていないこと。既存DBには同一URLの重複がある。"""
        db_path = str(tmp_path / "dupes.db")
        database = Database(db_path=db_path)
        digest = DigestResult(overview="全体概要", categories=[], total_articles=2)
        database.save_batch(
            [
                (_make_article(source_id="rss-001"), _make_summary(), None, None),
                (_make_article(source_id="rss-002"), _make_summary(), None, None),
            ],
            digest,
        )

        # 同じURLの2行が保存されたうえで、再オープンできる。
        Database(db_path=db_path)
        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM article_summaries WHERE url_key = ?",
                (url_key("https://example.com/article"),),
            ).fetchone()
        assert rows["n"] == 2
