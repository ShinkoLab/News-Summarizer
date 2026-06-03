"""Pipeline orchestration for the News Summarizer.

Exposes `run_pipeline(config, options)` as the single entrypoint.
Internal steps are broken into small functions for clarity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from tqdm import tqdm

import config as config_module
from config import AppConfig
from logger import get_logger
from models import Article, ArticleSummary, DigestResult

from fetchers.rss_fetcher import MinifluxFetcher
from fetchers.email_fetcher import EmailFetcher
from outputs.database import Database
from outputs.discord_output import DiscordOutput
from summarizer.grouper import group_articles, group_summaries
from summarizer.summarizer import summarize_article
from summarizer.digest import generate_digest

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# RunOptions — frozen options object threaded through the pipeline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunOptions:
    dry_run: bool = False
    forced_outputs: frozenset[Literal["discord", "db", "all"]] = field(
        default_factory=frozenset
    )
    sources: frozenset[Literal["rss", "email", "all"]] = field(
        default_factory=lambda: frozenset({"all"})
    )
    stream: bool = False
    debug: bool = False

    @property
    def run_db(self) -> bool:
        return not self.dry_run or "db" in self.forced_outputs or "all" in self.forced_outputs

    @property
    def run_discord(self) -> bool:
        return not self.dry_run or "discord" in self.forced_outputs or "all" in self.forced_outputs

    @property
    def run_rss(self) -> bool:
        return "all" in self.sources or "rss" in self.sources

    @property
    def run_email(self) -> bool:
        return "all" in self.sources or "email" in self.sources


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def fetch_articles(
    options: RunOptions, db: Database
) -> tuple[list[Article], MinifluxFetcher | None]:
    """Fetch articles from enabled sources with per-source error isolation.

    Returns the fetched articles and the MinifluxFetcher instance (or None when
    RSS is disabled), so the caller can defer mark-as-read until after persistence.
    """
    articles: list[Article] = []
    rss_fetcher: MinifluxFetcher | None = None

    if options.run_rss:
        try:
            logger.info("RSS（Miniflux）から記事を取得中...")
            rss_fetcher = MinifluxFetcher(dry_run=options.dry_run)
            articles.extend(rss_fetcher.fetch())
        except Exception as e:
            logger.error("RSSの取得中にエラーが発生しました: %s", e, exc_info=True)

    if options.run_email:
        try:
            logger.info("Email（POP3）から記事を取得中...")
            email_fetcher = EmailFetcher(db)
            articles.extend(email_fetcher.fetch())
        except Exception as e:
            logger.error("Emailの取得中にエラーが発生しました: %s", e, exc_info=True)

    return articles, rss_fetcher


def summarize_all(
    articles: list[Article], options: RunOptions
) -> list[tuple[Article, ArticleSummary]]:
    """Summarize each article; skip failures with per-article error isolation."""
    pairs: list[tuple[Article, ArticleSummary]] = []

    if options.stream:
        logger.info("個別記事の要約を行っています...")
        articles_iter = articles
    else:
        articles_iter = tqdm(articles, desc="記事を要約中", unit="件")

    for article in articles_iter:
        try:
            summary = summarize_article(article, stream=options.stream)
            pairs.append((article, summary))
        except Exception as e:
            logger.error(
                "記事要約中にエラーが発生しました (ID: %s): %s",
                article.source_id,
                e,
                exc_info=True,
            )

    return pairs


def group_pairs(
    pairs: list[tuple[Article, ArticleSummary]], options: RunOptions
) -> tuple[dict[int, tuple], np.ndarray | None]:
    """Group articles by topic using embeddings or LLM.

    Returns:
        article_group_map: index → (group_id, topic)
        embeddings: numpy array if embedding-based grouping was used, else None
    """
    article_group_map: dict[int, tuple] = {}
    embeddings: np.ndarray | None = None

    grouper_cfg = config_module.config.summarizer.steps.get("grouper")
    use_embeddings = grouper_cfg.use_embeddings if grouper_cfg else False
    embedding_model = config_module.config.llm.embedding_model

    if use_embeddings and embedding_model:
        logger.info("Embedding を取得中...")
        try:
            from summarizer.embedder import get_embeddings
            texts = [f"{s.title} {s.summary}" for _, s in pairs]
            embeddings = get_embeddings(texts, debug=options.debug)

            logger.info("類似記事の統合を行っています（embedding）...")
            only_summaries = [s for _, s in pairs]
            grouping_result = group_summaries(
                only_summaries, embeddings, stream=options.stream, debug=options.debug
            )
            for group in grouping_result.groups:
                for idx in group.article_indices:
                    article_group_map[idx] = (group.group_id, group.topic)
        except Exception as e:
            logger.error(
                "Embedding グルーピング中にエラーが発生しました: %s", e, exc_info=True
            )
            embeddings = None
            article_group_map = {}
    else:
        logger.info("類似記事の統合を行っています...")
        try:
            articles = [a for a, _ in pairs]
            grouping_result = group_articles(articles, stream=options.stream)
            for group in grouping_result.groups:
                for idx in group.article_indices:
                    article_group_map[idx] = (group.group_id, group.topic)
        except Exception as e:
            logger.error(
                "類似記事統合中にエラーが発生しました: %s", e, exc_info=True
            )
            article_group_map = {}

    return article_group_map, embeddings


def build_digest(
    pairs: list[tuple[Article, ArticleSummary]],
    article_group_map: dict[int, tuple],
    options: RunOptions,
):
    """Attach group info to summaries and generate the digest.

    Returns:
        summaries: list of (Article, ArticleSummary, group_id, group_topic)
        digest: DigestResult
    """
    summaries = []
    for i, (article, summary) in enumerate(pairs):
        group_info = article_group_map.get(i, (None, None))
        summaries.append((article, summary, group_info[0], group_info[1]))

    logger.info("ダイジェストを生成しています...")
    grouped_summaries = [(s[1], s[2], s[3]) for s in summaries]
    digest = generate_digest(grouped_summaries, stream=options.stream)
    return summaries, digest


def persist_and_publish(
    summaries, digest, embeddings, db: Database, options: RunOptions,
    rss_fetcher: MinifluxFetcher | None = None,
) -> None:
    """Save to DB and/or post to Discord based on RunOptions."""
    logger.info("結果を保存・出力しています...")
    only_summaries = [s[1] for s in summaries]

    if options.run_db:
        batch_id = db.create_batch(
            total_articles=len(only_summaries), digest_text=digest.overview
        )
        rss_entry_ids: list[int] = []
        for i, (article, summary, group_id, group_topic) in enumerate(summaries):
            embedding_list = embeddings[i].tolist() if embeddings is not None else None
            db.save_summary(batch_id, article, summary, group_id, group_topic, embedding_list)
            if article.source_type == "email":
                db.mark_email_processed(article.source_id)
            elif article.source_type == "rss":
                rss_entry_ids.append(int(article.source_id))
        logger.info("データベースへの保存が完了しました。")
        # DB保存に成功したRSS記事だけを既読化する（要約・保存に失敗した記事は次回再取得）
        if rss_fetcher is not None and rss_entry_ids:
            rss_fetcher.mark_as_read(rss_entry_ids)
    else:
        logger.info("[Dry-Run] データベースへの保存をスキップしました。")

    if options.run_discord:
        discord_out = DiscordOutput()
        discord_out.post(digest, only_summaries)
        logger.info("Discordへの送信が完了しました。")
    else:
        logger.info("[Dry-Run] Discordへの送信をスキップしました。")
        print("\n=== ダイジェスト結果 ===")
        print(digest.overview)
        for c in digest.categories:
            bullets = "\n".join(f"  • {a}" for a in c.articles)
            print(f"\n[{c.category}] ({c.article_count}件)\n{bullets}")


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------

def run_pipeline(config: AppConfig, options: RunOptions) -> None:
    """Run the full fetch → summarize → group → digest → output pipeline."""
    logger.info("プロセス開始")

    db = Database()

    articles, rss_fetcher = fetch_articles(options, db)
    if not articles:
        logger.info("新規記事はありませんでした。処理を終了します。")
        return

    logger.info("%d件の新規記事を取得しました。", len(articles))

    pairs = summarize_all(articles, options)
    if not pairs:
        logger.warning("要約に成功した記事がありませんでした。処理を終了します。")
        return

    article_group_map, embeddings = group_pairs(pairs, options)

    try:
        summaries, digest = build_digest(pairs, article_group_map, options)
    except Exception as e:
        # ダイジェスト生成の想定外失敗で個別要約まで失わないよう、空ダイジェストで続行する
        logger.error(
            "ダイジェスト生成中にエラーが発生しました。空のダイジェストで出力を続行します: %s",
            e,
            exc_info=True,
        )
        summaries = [
            (article, summary, article_group_map.get(i, (None, None))[0], article_group_map.get(i, (None, None))[1])
            for i, (article, summary) in enumerate(pairs)
        ]
        digest = DigestResult(overview="", categories=[], total_articles=len(pairs))

    persist_and_publish(summaries, digest, embeddings, db, options, rss_fetcher)

    logger.info("プロセス完了")
