"""Shared fixtures for the News-Summarizer test suite."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import config as config_module
from config import (
    AppConfig,
    CategoryDef,
    CategoryTaxonomy,
    DatabaseConfig,
    DiscordConfig,
    LLMConfig,
    LoggingConfig,
    SummarizerConfig,
)
from models import Article, ArticleSummary


# ---------------------------------------------------------------------------
# Minimal valid AppConfig (built in Python, not from YAML)
# ---------------------------------------------------------------------------

@pytest.fixture
def minimal_llm_config() -> LLMConfig:
    return LLMConfig(model="test-model")


@pytest.fixture
def minimal_taxonomy() -> CategoryTaxonomy:
    return CategoryTaxonomy(
        categories=[
            CategoryDef(name="テクノロジー", description="IT・ガジェット関連。"),
            CategoryDef(name="経済・ビジネス", description="景気・企業動向・市況。"),
            CategoryDef(name="未分類", description="上記いずれにも該当しない場合。"),
        ],
        fallback="未分類",
    )


@pytest.fixture
def minimal_app_config(minimal_llm_config, minimal_taxonomy) -> AppConfig:
    return AppConfig(
        llm=minimal_llm_config,
        summarizer=SummarizerConfig(),
        database=DatabaseConfig(path=":memory:"),
        discord=DiscordConfig(webhook_url=None),
        logging=LoggingConfig(level="INFO"),
        taxonomy=minimal_taxonomy,
    )


# ---------------------------------------------------------------------------
# Isolate the import-time config singleton
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_global_config(minimal_llm_config, minimal_taxonomy):
    """Replace the ``config.config`` singleton's contents for every test.

    ``config.config`` is loaded at import time from the developer's local
    ``config.yaml`` (or, without one, from ambient environment variables), and
    most modules hold it via ``from config import config``. Without this, a
    local config with e.g. embedding grouping enabled makes tests take
    different branches — and reach real endpoints — than they do in CI.

    The contents are swapped in place (as ``reload_config()`` does) so every
    module's reference sees the test config, then restored afterwards.
    """
    singleton = config_module.config
    snapshot = dict(singleton.__dict__)
    isolated = AppConfig(
        llm=minimal_llm_config.model_copy(deep=True),
        summarizer=SummarizerConfig(),
        database=DatabaseConfig(path=":memory:"),
        discord=DiscordConfig(webhook_url=None),
        logging=LoggingConfig(level="INFO"),
        taxonomy=minimal_taxonomy.model_copy(deep=True),
    )
    singleton.__dict__.clear()
    singleton.__dict__.update(isolated.__dict__)
    try:
        yield singleton
    finally:
        singleton.__dict__.clear()
        singleton.__dict__.update(snapshot)


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
