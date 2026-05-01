"""Integration-style tests for pipeline.run_pipeline() with heavy mocking."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

from config import AppConfig, LLMConfig, SummarizerConfig, DiscordConfig
from models import Article, ArticleSummary, CategoryDigest, DigestResult, GroupingResult, ArticleGroup
from pipeline import RunOptions, run_pipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_article(source_id: str = "rss-1", source_type: str = "rss") -> Article:
    now = datetime.now()
    return Article(
        source_type=source_type,
        source_id=source_id,
        title="Test Article",
        content="Test content.",
        url="https://example.com/1",
        published_at=now,
        fetched_at=now,
        feed_title="Test Feed",
    )


def _make_summary(title: str = "テスト要約") -> ArticleSummary:
    return ArticleSummary(
        title=title,
        summary="これはテスト要約です。",
        keywords=["テスト"],
        category="テクノロジー",
    )


def _make_digest(total: int = 1) -> DigestResult:
    return DigestResult(
        overview="テスト概要",
        categories=[
            CategoryDigest(category="テクノロジー", articles=["記事1"], article_count=1)
        ],
        total_articles=total,
        generated_at=datetime.now(),
    )


def _make_grouping(n: int = 1) -> GroupingResult:
    return GroupingResult(
        groups=[ArticleGroup(group_id=0, topic="テストトピック", article_indices=list(range(n)))]
    )


@pytest.fixture
def minimal_config() -> AppConfig:
    return AppConfig(
        llm=LLMConfig(model="test-model"),
        summarizer=SummarizerConfig(categories=["テクノロジー"]),
        discord=DiscordConfig(webhook_url=None),
    )


# ---------------------------------------------------------------------------
# Helpers to build a fully-patched run_pipeline call
# ---------------------------------------------------------------------------

def _run_with_patches(
    config,
    options: RunOptions,
    articles: list[Article],
    summaries: list[ArticleSummary] | None = None,
    *,
    db_mock=None,
    discord_mock=None,
):
    """Run run_pipeline with all external I/O patched out."""
    if summaries is None:
        summaries = [_make_summary() for _ in articles]

    digest = _make_digest(len(articles))
    grouping = _make_grouping(len(articles))

    db_instance = db_mock or MagicMock()
    db_instance.create_batch.return_value = 1
    discord_instance = discord_mock or MagicMock()

    with (
        patch("pipeline.MinifluxFetcher") as MockRss,
        patch("pipeline.EmailFetcher") as MockEmail,
        patch("pipeline.summarize_article", side_effect=summaries),
        patch("pipeline.group_articles", return_value=grouping),
        patch("pipeline.group_summaries", return_value=grouping),
        patch("pipeline.generate_digest", return_value=digest),
        patch("pipeline.Database", return_value=db_instance),
        patch("pipeline.DiscordOutput", return_value=discord_instance),
    ):
        rss_instance = MagicMock()
        rss_instance.fetch.return_value = [a for a in articles if a.source_type == "rss"]
        MockRss.return_value = rss_instance

        email_instance = MagicMock()
        email_instance.fetch.return_value = [a for a in articles if a.source_type == "email"]
        MockEmail.return_value = email_instance

        run_pipeline(config, options)

    return db_instance, discord_instance


# ---------------------------------------------------------------------------
# Empty article list returns early
# ---------------------------------------------------------------------------

class TestEmptyArticles:
    def test_empty_articles_returns_early_without_summarize(self, minimal_config):
        options = RunOptions(dry_run=True)

        with (
            patch("pipeline.MinifluxFetcher") as MockRss,
            patch("pipeline.EmailFetcher") as MockEmail,
            patch("pipeline.summarize_article") as mock_summarize,
            patch("pipeline.Database"),
            patch("pipeline.DiscordOutput"),
        ):
            MockRss.return_value.fetch.return_value = []
            MockEmail.return_value.fetch.return_value = []
            run_pipeline(minimal_config, options)

        mock_summarize.assert_not_called()


# ---------------------------------------------------------------------------
# dry_run skips DB and Discord
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_skips_db(self, minimal_config):
        articles = [_make_article()]
        db_mock = MagicMock()
        db_mock.create_batch.return_value = 1

        db_mock, discord_mock = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True),
            articles,
            db_mock=db_mock,
        )

        db_mock.create_batch.assert_not_called()
        db_mock.save_summary.assert_not_called()

    def test_dry_run_skips_discord(self, minimal_config):
        articles = [_make_article()]
        discord_mock = MagicMock()

        _, discord_mock = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True),
            articles,
            discord_mock=discord_mock,
        )

        discord_mock.post.assert_not_called()


# ---------------------------------------------------------------------------
# forced_outputs={"discord"} under dry_run calls Discord but not DB
# ---------------------------------------------------------------------------

class TestForcedOutputs:
    def test_dry_run_forced_discord_calls_discord(self, minimal_config):
        articles = [_make_article()]
        discord_mock = MagicMock()

        _, discord_mock = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True, forced_outputs=frozenset({"discord"})),
            articles,
            discord_mock=discord_mock,
        )

        discord_mock.post.assert_called_once()

    def test_dry_run_forced_discord_skips_db(self, minimal_config):
        articles = [_make_article()]
        db_mock = MagicMock()
        db_mock.create_batch.return_value = 1

        db_mock, _ = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True, forced_outputs=frozenset({"discord"})),
            articles,
            db_mock=db_mock,
        )

        db_mock.create_batch.assert_not_called()

    def test_forced_all_calls_both(self, minimal_config):
        articles = [_make_article()]
        db_mock = MagicMock()
        db_mock.create_batch.return_value = 1
        discord_mock = MagicMock()

        db_mock, discord_mock = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True, forced_outputs=frozenset({"all"})),
            articles,
            db_mock=db_mock,
            discord_mock=discord_mock,
        )

        db_mock.create_batch.assert_called_once()
        discord_mock.post.assert_called_once()


# ---------------------------------------------------------------------------
# Non-dry-run — both DB and Discord are called
# ---------------------------------------------------------------------------

class TestNonDryRun:
    def test_non_dry_run_calls_db(self, minimal_config):
        articles = [_make_article()]
        db_mock = MagicMock()
        db_mock.create_batch.return_value = 1

        db_mock, _ = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
            db_mock=db_mock,
        )

        db_mock.create_batch.assert_called_once()
        db_mock.save_summary.assert_called_once()

    def test_non_dry_run_calls_discord(self, minimal_config):
        articles = [_make_article()]
        discord_mock = MagicMock()

        _, discord_mock = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
            discord_mock=discord_mock,
        )

        discord_mock.post.assert_called_once()


# ---------------------------------------------------------------------------
# Source filtering
# ---------------------------------------------------------------------------

class TestSourceFiltering:
    def test_rss_only_source_does_not_call_email_fetcher(self, minimal_config):
        options = RunOptions(dry_run=True, sources=frozenset({"rss"}))

        with (
            patch("pipeline.MinifluxFetcher") as MockRss,
            patch("pipeline.EmailFetcher") as MockEmail,
            patch("pipeline.summarize_article", return_value=_make_summary()),
            patch("pipeline.group_articles", return_value=_make_grouping()),
            patch("pipeline.generate_digest", return_value=_make_digest()),
            patch("pipeline.Database"),
            patch("pipeline.DiscordOutput"),
        ):
            MockRss.return_value.fetch.return_value = []
            run_pipeline(minimal_config, options)

        MockEmail.assert_not_called()

    def test_email_only_source_does_not_call_rss_fetcher(self, minimal_config):
        options = RunOptions(dry_run=True, sources=frozenset({"email"}))

        with (
            patch("pipeline.MinifluxFetcher") as MockRss,
            patch("pipeline.EmailFetcher") as MockEmail,
            patch("pipeline.summarize_article", return_value=_make_summary()),
            patch("pipeline.group_articles", return_value=_make_grouping()),
            patch("pipeline.generate_digest", return_value=_make_digest()),
            patch("pipeline.Database") as MockDb,
            patch("pipeline.DiscordOutput"),
        ):
            MockDb.return_value.create_batch.return_value = 1
            MockEmail.return_value.fetch.return_value = []
            run_pipeline(minimal_config, options)

        MockRss.assert_not_called()
