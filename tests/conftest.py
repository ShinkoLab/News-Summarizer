"""Shared fixtures for the News-Summarizer test suite."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from config import AppConfig, LLMConfig, SummarizerConfig, DatabaseConfig, DiscordConfig, LoggingConfig
from models import Article, ArticleSummary


# ---------------------------------------------------------------------------
# Minimal valid AppConfig (built in Python, not from YAML)
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_llm_config() -> LLMConfig:
    return LLMConfig(model="test-model")


@pytest.fixture
def minimal_app_config(minimal_llm_config) -> AppConfig:
    return AppConfig(
        llm=minimal_llm_config,
        summarizer=SummarizerConfig(
            categories=["テクノロジー", "ビジネス", "その他"]
        ),
        database=DatabaseConfig(path=":memory:"),
        discord=DiscordConfig(webhook_url=None),
        logging=LoggingConfig(level="INFO"),
    )


# ---------------------------------------------------------------------------
# Sample Article factory
# ---------------------------------------------------------------------------

@pytest.fixture
def make_article():
    def _factory(
        source_type: str = "rss",
        source_id: str = "test-id-1",
        title: str = "Test Article Title",
        content: str = "Test article content body.",
        url: str | None = "https://example.com/article",
        published_at: datetime | None = None,
        fetched_at: datetime | None = None,
        feed_title: str | None = "Test Feed",
    ) -> Article:
        now = datetime.now()
        return Article(
            source_type=source_type,
            source_id=source_id,
            title=title,
            content=content,
            url=url,
            published_at=published_at or now,
            fetched_at=fetched_at or now,
            feed_title=feed_title,
        )

    return _factory


@pytest.fixture
def sample_article(make_article) -> Article:
    return make_article()


# ---------------------------------------------------------------------------
# Sample ArticleSummary factory
# ---------------------------------------------------------------------------

@pytest.fixture
def make_article_summary():
    def _factory(
        title: str = "テスト記事のタイトル",
        summary: str = "テスト記事の要約文です。内容を簡潔にまとめています。",
        keywords: list[str] | None = None,
        category: str = "テクノロジー",
    ) -> ArticleSummary:
        return ArticleSummary(
            title=title,
            summary=summary,
            keywords=keywords or ["テスト", "記事", "キーワード"],
            category=category,
        )

    return _factory


@pytest.fixture
def sample_summary(make_article_summary) -> ArticleSummary:
    return make_article_summary()


# ---------------------------------------------------------------------------
# Temp SQLite DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db_path(tmp_path) -> Path:
    """Returns a path to a temp directory where a DB file can be created."""
    return tmp_path / "test.db"
