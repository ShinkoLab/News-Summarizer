"""Integration-style tests for pipeline.run_pipeline() with heavy mocking."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

from config import AppConfig, LLMConfig, SummarizerConfig, DiscordConfig, EmailConfig
from models import (
    Article,
    ArticleSummary,
    CategoryDigest,
    DigestResult,
    GroupingResult,
    ArticleGroup,
    SaveResult,
)
from pipeline import (
    RunOptions,
    run_pipeline,
    select_articles,
    unify_group_categories,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_article(source_id: str = "1", source_type: str = "rss") -> Article:
    now = datetime.now()
    return Article(
        source_type=source_type,
        source_id=source_id,
        title="Test Article",
        content="Test content.",
        # URL は source_id ごとに変える。同一URLはパイプラインが重複として
        # 落とすようになったため、固定にすると複数記事のテストが1件に潰れる。
        url=f"https://example.com/{source_type}/{source_id}",
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
            CategoryDigest(category="テクノロジー", summary="本文", highlights=["記事1"], article_count=1)
        ],
        total_articles=total,
        generated_at=datetime.now(),
    )


def _make_grouping(n: int = 1) -> GroupingResult:
    return GroupingResult(
        groups=[ArticleGroup(group_id=0, topic="テストトピック", article_indices=list(range(n)))]
    )


def _make_db(saved_source_ids: list[str] | None = None) -> MagicMock:
    """A DB mock whose save_batch() reports back what it was handed.

    save_batch() now returns a SaveResult and the pipeline drives mark-as-read /
    email bookkeeping off it, so a bare MagicMock (which yields an empty
    SaveResult.saved) would silently disable both.
    `saved_source_ids` narrows the result to simulate a partial save.
    """
    db = MagicMock()
    db.is_article_processed.return_value = False
    db.is_url_processed.return_value = False
    db.is_email_processed.return_value = False
    db.record_email_attempt.return_value = 1

    def save_batch(summaries, digest, embeddings=None):
        keys = [(article.source_type, article.source_id) for article, *_ in summaries]
        if saved_source_ids is None:
            return SaveResult(batch_id=1, saved=keys)
        kept = [key for key in keys if key[1] in saved_source_ids]
        return SaveResult(batch_id=1, saved=kept, failed=len(keys) - len(kept))

    db.save_batch.side_effect = save_batch
    return db


@pytest.fixture
def minimal_config() -> AppConfig:
    return AppConfig(
        llm=LLMConfig(model="test-model"),
        summarizer=SummarizerConfig(),
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

    db_instance = db_mock or _make_db()
    discord_instance = discord_mock or MagicMock()

    with (
        patch("pipeline.MinifluxFetcher") as MockRss,
        patch("pipeline.EmailFetcher") as MockEmail,
        patch("pipeline.summarize_article", side_effect=summaries),
        patch("pipeline.group_articles", return_value=grouping),
        patch("pipeline.group_summaries", return_value=grouping),
        patch("pipeline.generate_digest", return_value=digest),
        patch("pipeline.create_database", return_value=db_instance),
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
            patch("pipeline.create_database"),
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
        db_mock = _make_db()

        db_mock, discord_mock = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True),
            articles,
            db_mock=db_mock,
        )

        db_mock.save_batch.assert_not_called()

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
        db_mock = _make_db()

        db_mock, _ = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True, forced_outputs=frozenset({"discord"})),
            articles,
            db_mock=db_mock,
        )

        db_mock.save_batch.assert_not_called()

    def test_forced_all_calls_both(self, minimal_config):
        articles = [_make_article()]
        db_mock = _make_db()
        discord_mock = MagicMock()

        db_mock, discord_mock = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=True, forced_outputs=frozenset({"all"})),
            articles,
            db_mock=db_mock,
            discord_mock=discord_mock,
        )

        db_mock.save_batch.assert_called_once()
        discord_mock.post.assert_called_once()


# ---------------------------------------------------------------------------
# Non-dry-run — both DB and Discord are called
# ---------------------------------------------------------------------------

class TestNonDryRun:
    def test_non_dry_run_calls_db(self, minimal_config):
        articles = [_make_article()]
        db_mock = _make_db()

        db_mock, _ = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
            db_mock=db_mock,
        )

        db_mock.save_batch.assert_called_once()

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

    def test_processed_articles_are_skipped(self, minimal_config):
        articles = [_make_article(source_id="1"), _make_article(source_id="2")]
        db_mock = _make_db()
        db_mock.is_article_processed.side_effect = [True, False]

        db_mock, _ = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
            db_mock=db_mock,
        )

        saved = db_mock.save_batch.call_args.args[0]
        assert [article.source_id for article, *_ in saved] == ["2"]

    def test_article_count_is_limited_per_run(self, minimal_config):
        minimal_config.summarizer.max_articles_per_run = 1
        articles = [_make_article(source_id="1"), _make_article(source_id="2")]

        db_mock, _ = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
        )

        saved = db_mock.save_batch.call_args.args[0]
        assert len(saved) == 1
        assert saved[0][0].source_id == "1"


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
            patch("pipeline.create_database"),
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
            patch("pipeline.create_database") as MockDb,
            patch("pipeline.DiscordOutput"),
        ):
            MockDb.return_value.is_article_processed.return_value = False
            MockEmail.return_value.fetch.return_value = []
            run_pipeline(minimal_config, options)

        MockRss.assert_not_called()


# ---------------------------------------------------------------------------
# Miniflux 既読化の後ろ倒し + ダイジェスト失敗時のグレースフルデグラデーション
# ---------------------------------------------------------------------------

class TestMinifluxMarkAsRead:
    def _run(self, config, options, articles, *, summarize_side_effect, generate_digest,
             db_instance=None):
        db_instance = db_instance or _make_db()
        discord_instance = MagicMock()
        with (
            patch("pipeline.MinifluxFetcher") as MockRss,
            patch("pipeline.EmailFetcher") as MockEmail,
            patch("pipeline.summarize_article", side_effect=summarize_side_effect),
            patch("pipeline.group_articles", return_value=_make_grouping(len(articles))),
            patch("pipeline.generate_digest", **generate_digest),
            patch("pipeline.create_database", return_value=db_instance),
            patch("pipeline.DiscordOutput", return_value=discord_instance),
        ):
            rss_instance = MagicMock()
            rss_instance.fetch.return_value = [a for a in articles if a.source_type == "rss"]
            MockRss.return_value = rss_instance
            MockEmail.return_value.fetch.return_value = [a for a in articles if a.source_type == "email"]
            run_pipeline(config, options)
        return db_instance, discord_instance, rss_instance

    def test_digest_failure_still_persists_and_marks(self, minimal_config):
        """ダイジェスト失敗でも個別要約は保存され、成功記事は既読化され、Discordも呼ばれる。"""
        articles = [_make_article(source_id="1"), _make_article(source_id="2")]
        db_instance, discord_instance, rss_instance = self._run(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
            summarize_side_effect=[_make_summary(), _make_summary()],
            generate_digest={"side_effect": RuntimeError("digest boom")},
        )

        db_instance.save_batch.assert_called_once()
        assert len(db_instance.save_batch.call_args.args[0]) == 2
        rss_instance.mark_as_read.assert_called_once_with([1, 2])
        discord_instance.post.assert_called_once()

    def test_failed_summary_article_not_marked(self, minimal_config):
        """要約に失敗した記事は既読化対象に含まれない（記事単位）。"""
        articles = [_make_article(source_id="1"), _make_article(source_id="2")]
        db_instance, _, rss_instance = self._run(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
            summarize_side_effect=[_make_summary(), RuntimeError("summarize fail")],
            generate_digest={"return_value": _make_digest(1)},
        )

        db_instance.save_batch.assert_called_once()
        assert len(db_instance.save_batch.call_args.args[0]) == 1
        rss_instance.mark_as_read.assert_called_once_with([1])

    def test_dry_run_does_not_mark(self, minimal_config):
        """dry_run では既読化しない。"""
        articles = [_make_article(source_id="1")]
        _, _, rss_instance = self._run(
            minimal_config,
            RunOptions(dry_run=True),
            articles,
            summarize_side_effect=[_make_summary()],
            generate_digest={"return_value": _make_digest(1)},
        )

        rss_instance.mark_as_read.assert_not_called()

    def test_partially_saved_batch_only_marks_persisted_articles(self, minimal_config):
        """分割コミットで落ちた記事は既読化しない（次回の再取得に回す）。"""
        articles = [_make_article(source_id="1"), _make_article(source_id="2")]
        _, _, rss_instance = self._run(
            minimal_config,
            RunOptions(dry_run=False),
            articles,
            summarize_side_effect=[_make_summary(), _make_summary()],
            generate_digest={"return_value": _make_digest(2)},
            db_instance=_make_db(saved_source_ids=["1"]),
        )

        rss_instance.mark_as_read.assert_called_once_with([1])


# ---------------------------------------------------------------------------
# 保存済みと判明した記事を取得元から外す（issue #27）
# ---------------------------------------------------------------------------

def _run_with_db(config, options, articles, db_instance):
    """run_pipeline を走らせ、MinifluxFetcher のモックを返す。"""
    with (
        patch("pipeline.MinifluxFetcher") as MockRss,
        patch("pipeline.EmailFetcher") as MockEmail,
        patch("pipeline.summarize_article", side_effect=lambda *a, **k: _make_summary()),
        patch("pipeline.group_articles", return_value=_make_grouping(len(articles))),
        patch("pipeline.generate_digest", return_value=_make_digest(len(articles))),
        patch("pipeline.create_database", return_value=db_instance),
        patch("pipeline.DiscordOutput"),
    ):
        rss_instance = MagicMock()
        rss_instance.fetch.return_value = [a for a in articles if a.source_type == "rss"]
        MockRss.return_value = rss_instance
        MockEmail.return_value.fetch.return_value = [
            a for a in articles if a.source_type == "email"
        ]
        run_pipeline(config, options)
    return rss_instance


class TestProcessedArticlesAreMarkedRead:
    def test_already_processed_rss_is_marked_read_on_early_return(self, minimal_config):
        """全件が処理済みで早期returnする経路でも既読化する。

        定常状態はまさにこの経路（毎回取得→毎回フィルタで捨てる）なので、
        ここで既読化しないと未読の滞留は永久に解消されない。
        """
        articles = [_make_article(source_id="1"), _make_article(source_id="2")]
        db_instance = _make_db()
        db_instance.is_article_processed.return_value = True

        rss_instance = _run_with_db(
            minimal_config, RunOptions(dry_run=False), articles, db_instance
        )

        rss_instance.mark_as_read.assert_called_once_with([1, 2])
        db_instance.save_batch.assert_not_called()

    def test_processed_and_new_articles_are_marked_separately(self, minimal_config):
        """処理済み分は除外直後に、新規分は保存後に既読化される。"""
        articles = [_make_article(source_id="1"), _make_article(source_id="2")]
        db_instance = _make_db()
        db_instance.is_article_processed.side_effect = [True, False]

        rss_instance = _run_with_db(
            minimal_config, RunOptions(dry_run=False), articles, db_instance
        )

        assert rss_instance.mark_as_read.call_args_list == [call([1]), call([2])]

    def test_processed_email_is_not_passed_to_mark_as_read(self, minimal_config):
        """MinifluxのIDは整数。email の source_id を混ぜてはいけない。"""
        articles = [_make_article(source_id="mail-1", source_type="email")]
        db_instance = _make_db()
        db_instance.is_article_processed.return_value = True

        rss_instance = _run_with_db(
            minimal_config, RunOptions(dry_run=False), articles, db_instance
        )

        rss_instance.mark_as_read.assert_not_called()


# ---------------------------------------------------------------------------
# 同一URLの重複排除
# ---------------------------------------------------------------------------

def _with_url(article: Article, url: str | None) -> Article:
    article.url = url
    return article


class TestUrlDeduplication:
    def test_same_url_from_two_feeds_is_processed_once(self, minimal_config):
        """同じ記事が別フィードから別 entry ID で降ってくるケース。

        entry ID ベースの `is_article_processed()` では素通りしてしまい、
        実際に同一URLの記事が保存されていた。
        """
        url = "https://www.bbc.co.uk/news/articles/cre40875r9vo"
        articles = [
            _with_url(_make_article(source_id="1"), url + "?at_medium=RSS&at_campaign=rss"),
            _with_url(_make_article(source_id="2"), url),
        ]
        db_instance = _make_db()

        db_instance, _ = _run_with_patches(
            minimal_config, RunOptions(dry_run=False), articles, db_mock=db_instance
        )

        saved = db_instance.save_batch.call_args.args[0]
        assert [article.source_id for article, *_ in saved] == ["1"]

    def test_duplicate_url_entry_is_marked_read_in_miniflux(self, minimal_config):
        """既読化しないと、重複エントリが毎回 Miniflux から降ってくる。"""
        url = "https://example.com/same"
        articles = [
            _with_url(_make_article(source_id="1"), url),
            _with_url(_make_article(source_id="2"), url),
        ]

        rss_instance = _run_with_db(
            minimal_config, RunOptions(dry_run=False), articles, _make_db()
        )

        # 重複分は除外直後に、保存された分は保存後に既読化される。
        assert rss_instance.mark_as_read.call_args_list == [call([2]), call([1])]

    def test_url_already_in_the_database_is_skipped(self, minimal_config):
        """バッチを跨いだ重複。grouper は1回の実行内でしか働かないのでここで落とす。"""
        articles = [_make_article(source_id="1")]
        db_instance = _make_db()
        db_instance.is_url_processed.return_value = True

        db_instance, _ = _run_with_patches(
            minimal_config, RunOptions(dry_run=False), articles, db_mock=db_instance
        )

        db_instance.save_batch.assert_not_called()

    def test_articles_without_a_url_are_never_deduplicated(self, minimal_config):
        """メール記事は URL を持たない。全部が重複扱いされては困る。"""
        articles = [
            _with_url(_make_article(source_id="mail-1", source_type="email"), None),
            _with_url(_make_article(source_id="mail-2", source_type="email"), None),
        ]
        db_instance = _make_db()

        db_instance, _ = _run_with_patches(
            minimal_config,
            RunOptions(dry_run=False, sources=frozenset({"email"})),
            articles,
            db_mock=db_instance,
        )

        saved = db_instance.save_batch.call_args.args[0]
        assert [article.source_id for article, *_ in saved] == ["mail-1", "mail-2"]


# ---------------------------------------------------------------------------
# クラスタ内のカテゴリ統一
# ---------------------------------------------------------------------------

class TestUnifyGroupCategories:
    @staticmethod
    def _pairs(categories: list[str], published: list[int] | None = None):
        pairs = []
        for i, category in enumerate(categories):
            article = _make_article(source_id=str(i))
            if published is not None:
                article.published_at = datetime(2026, 1, 1 + published[i])
            summary = _make_summary()
            summary.category = category
            pairs.append((article, summary))
        return pairs

    def test_majority_category_wins(self):
        pairs = self._pairs(["政治・社会", "政治・社会", "事件・事故・災害"])
        group_map = {0: (0, "topic"), 1: (0, "topic"), 2: (0, "topic")}

        changed = unify_group_categories(pairs, group_map)

        assert changed == 1
        assert {s.category for _, s in pairs} == {"政治・社会"}

    def test_tie_is_broken_by_the_oldest_article(self):
        """同数のときは公開が最も古い記事に寄せる。実行ごとに揺れないようにするため。"""
        pairs = self._pairs(["環境", "社会"], published=[5, 1])
        group_map = {0: (0, "topic"), 1: (0, "topic")}

        unify_group_categories(pairs, group_map)

        assert [s.category for _, s in pairs] == ["社会", "社会"]

    def test_separate_groups_are_untouched(self):
        pairs = self._pairs(["環境", "社会"])
        group_map = {0: (0, "a"), 1: (1, "b")}

        assert unify_group_categories(pairs, group_map) == 0
        assert [s.category for _, s in pairs] == ["環境", "社会"]

    def test_ungrouped_articles_are_untouched(self):
        """group_id が None なのはグルーピング失敗時。None 同士は無関係。"""
        pairs = self._pairs(["環境", "社会"])

        assert unify_group_categories(pairs, {}) == 0
        assert [s.category for _, s in pairs] == ["環境", "社会"]

    def test_already_consistent_group_reports_no_change(self):
        pairs = self._pairs(["社会", "社会"])
        group_map = {0: (0, "topic"), 1: (0, "topic")}

        assert unify_group_categories(pairs, group_map) == 0


# ---------------------------------------------------------------------------
# メールの試行回数カウンタ（issue #27 / poison message 対策）
# ---------------------------------------------------------------------------

class TestEmailAttemptReconciliation:
    def test_unsaved_email_records_an_attempt(self, minimal_config):
        articles = [_make_article(source_id="mail-1", source_type="email")]
        db_instance = _make_db(saved_source_ids=[])  # 保存に失敗した

        _run_with_db(minimal_config, RunOptions(dry_run=False), articles, db_instance)

        db_instance.record_email_attempt.assert_called_once_with("mail-1")
        db_instance.mark_email_processed.assert_not_called()

    def test_saved_email_records_no_attempt(self, minimal_config):
        articles = [_make_article(source_id="mail-1", source_type="email")]
        db_instance = _make_db()

        _run_with_db(minimal_config, RunOptions(dry_run=False), articles, db_instance)

        db_instance.record_email_attempt.assert_not_called()

    def test_attempt_limit_marks_the_email_processed(self, minimal_config):
        """恒久的に失敗するメールを毎回フルRETRし続けないよう打ち切る。"""
        minimal_config.email = EmailConfig(
            host="pop.example.com", username="u", password="p", max_fetch_attempts=2
        )
        articles = [_make_article(source_id="mail-1", source_type="email")]
        db_instance = _make_db(saved_source_ids=[])
        db_instance.record_email_attempt.return_value = 2

        _run_with_db(minimal_config, RunOptions(dry_run=False), articles, db_instance)

        db_instance.mark_email_processed.assert_called_once_with("mail-1")

    def test_attempt_limit_comes_from_config(self, minimal_config):
        """上限は設定値。既定の3ではなく config の値で判定する。"""
        minimal_config.email = EmailConfig(
            host="pop.example.com", username="u", password="p", max_fetch_attempts=10
        )
        articles = [_make_article(source_id="mail-1", source_type="email")]
        db_instance = _make_db(saved_source_ids=[])
        db_instance.record_email_attempt.return_value = 4

        _run_with_db(minimal_config, RunOptions(dry_run=False), articles, db_instance)

        db_instance.mark_email_processed.assert_not_called()

    def test_carried_over_email_records_no_attempt(self, minimal_config):
        """繰り越された（要約すら試みていない）メールでリトライ枠を消費しない。"""
        minimal_config.summarizer.max_articles_per_run = 1
        articles = [
            _make_article(source_id="mail-1", source_type="email"),
            _make_article(source_id="mail-2", source_type="email"),
        ]
        db_instance = _make_db(saved_source_ids=[])

        _run_with_db(minimal_config, RunOptions(dry_run=False), articles, db_instance)

        db_instance.record_email_attempt.assert_called_once_with("mail-1")

    def test_summarize_failure_records_an_attempt(self, minimal_config):
        articles = [_make_article(source_id="mail-1", source_type="email")]
        db_instance = _make_db()

        with (
            patch("pipeline.MinifluxFetcher"),
            patch("pipeline.EmailFetcher") as MockEmail,
            patch("pipeline.summarize_article", side_effect=RuntimeError("boom")),
            patch("pipeline.create_database", return_value=db_instance),
            patch("pipeline.DiscordOutput"),
        ):
            MockEmail.return_value.fetch.return_value = articles
            run_pipeline(minimal_config, RunOptions(dry_run=False, sources=frozenset({"email"})))

        db_instance.record_email_attempt.assert_called_once_with("mail-1")

    def test_dry_run_records_no_attempt(self, minimal_config):
        articles = [_make_article(source_id="mail-1", source_type="email")]
        db_instance = _make_db()

        _run_with_db(minimal_config, RunOptions(dry_run=True), articles, db_instance)

        db_instance.record_email_attempt.assert_not_called()
        db_instance.mark_email_processed.assert_not_called()


# ---------------------------------------------------------------------------
# max_articles_per_run のソース間公平配分（issue #27）
# ---------------------------------------------------------------------------

class TestSelectArticles:
    def test_under_the_limit_returns_everything_unchanged(self):
        articles = [_make_article(source_id=str(i)) for i in range(3)]
        assert select_articles(articles, 10) == articles

    def test_single_source_keeps_the_original_order(self):
        articles = [_make_article(source_id=str(i)) for i in range(5)]
        selected = select_articles(articles, 3)
        assert [a.source_id for a in selected] == ["0", "1", "2"]

    def test_rss_cannot_starve_email(self):
        """RSSが枠を独占してEmailが永久に処理されない状態を作らない。"""
        articles = [_make_article(source_id=f"rss-{i}") for i in range(10)]
        articles += [
            _make_article(source_id=f"mail-{i}", source_type="email") for i in range(10)
        ]

        selected = select_articles(articles, 4)

        assert len(selected) == 4
        assert sum(1 for a in selected if a.source_type == "rss") == 2
        assert sum(1 for a in selected if a.source_type == "email") == 2

    def test_spare_capacity_spills_to_the_other_source(self):
        """記事数の少ないソースが使わなかった枠は他ソースへ回す。"""
        articles = [_make_article(source_id=f"rss-{i}") for i in range(10)]
        articles += [_make_article(source_id="mail-0", source_type="email")]

        selected = select_articles(articles, 5)

        assert len(selected) == 5
        assert sum(1 for a in selected if a.source_type == "email") == 1
