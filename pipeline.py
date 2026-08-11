"""Pipeline orchestration for the News Summarizer.

Exposes `run_pipeline(config, options)` as the single entrypoint.
Internal steps are broken into small functions for clarity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from tqdm import tqdm

import config as config_module
from config import AppConfig
from logger import get_logger
from models import Article, ArticleSummary, DigestResult, SaveResult
from urls import url_key

from fetchers.rss_fetcher import MinifluxFetcher
from fetchers.email_fetcher import EmailFetcher
from outputs.database import create_database
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
    options: RunOptions, db
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


def select_articles(articles: list[Article], limit: int) -> list[Article]:
    """1回の処理上限までを、ソース間で公平に配分して選ぶ。

    先頭から単純に切ると、`fetch_articles()` が RSS → Email の順に並べる以上、
    RSS だけで枠が埋まったとき Email が永久に処理されない。ソースごとに
    元の順序を保ったままラウンドロビンで配分し、記事数の少ないソースが
    使わなかった枠は他のソースへ回す。
    """
    if len(articles) <= limit:
        return articles

    queues: dict[str, list[Article]] = {}
    for article in articles:
        queues.setdefault(article.source_type, []).append(article)

    selected: list[Article] = []
    while len(selected) < limit:
        # 1周しても1件も取れなければ全ソースが空。
        progressed = False
        for queue in queues.values():
            if not queue:
                continue
            selected.append(queue.pop(0))
            progressed = True
            if len(selected) == limit:
                break
        if not progressed:
            break

    return selected


@dataclass(frozen=True)
class DedupResult:
    """`filter_new_articles()` の結果。"""

    remaining: list[Article]
    # 今回処理しないと確定したRSSエントリ。Minifluxで既読にしないと毎回降ってくる。
    rss_ids_to_mark_read: list[int]
    already_processed: int
    duplicate_urls: int


def filter_new_articles(articles: list[Article], db) -> DedupResult:
    """未処理の記事だけを残す。

    重複判定は2段階ある。

    1. `(source_type, source_id)` — Miniflux の entry ID。同じ entry を
       二度処理しないための既存の判定
    2. 正規化URLのハッシュ — 同じ記事が複数フィードから**別々の entry ID**で
       配信されるケース。1だけでは素通りしてしまい、実際に同一URLの記事が
       同一バッチにも別バッチにも保存されていた

    2はバッチを跨いで効く点が重要で、grouper のクラスタリングは1回の実行内でしか
    働かないため、ここで落とさないと Viewer に同じ記事が並び続ける。
    """
    remaining: list[Article] = []
    rss_ids_to_mark_read: list[int] = []
    already_processed = 0
    duplicate_urls = 0
    # 同一実行内で取得した記事どうしの重複。DBにはまだ無いのでDB照会では拾えない。
    seen_url_keys: set[str] = set()

    for article in articles:
        key = url_key(article.url)

        if db.is_article_processed(article.source_type, article.source_id):
            already_processed += 1
        elif key and (key in seen_url_keys or db.is_url_processed(key)):
            duplicate_urls += 1
            logger.debug(
                "同一URLの記事を除外します (source_id: %s, url: %s)",
                article.source_id,
                article.url,
            )
        else:
            if key:
                seen_url_keys.add(key)
            remaining.append(article)
            continue

        if article.source_type == "rss":
            rss_ids_to_mark_read.append(int(article.source_id))

    return DedupResult(
        remaining=remaining,
        rss_ids_to_mark_read=rss_ids_to_mark_read,
        already_processed=already_processed,
        duplicate_urls=duplicate_urls,
    )


def unify_group_categories(
    pairs: list[tuple[Article, ArticleSummary]], article_group_map: dict[int, tuple]
) -> int:
    """同一クラスタ内のカテゴリを多数決で揃え、変更した記事数を返す。

    ダイジェスト（`summarizer/digest.py`）も Viewer も「カテゴリ → グループ」の順で
    階層化するため、要約LLMが同じニュースに違うカテゴリを付けるとクラスタが割れる。
    実データでも「北日本東日本の大雨警戒」が 社会 と 環境 に分断されていた。

    同数のときは公開が最も古い記事のカテゴリを採る（実行ごとに結果が変わらないよう
    決定的にするためで、どのカテゴリが正しいかという判断ではない）。
    """
    members: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(pairs)):
        group_id = article_group_map.get(idx, (None, None))[0]
        if group_id is not None:
            members[group_id].append(idx)

    changed = 0
    for indices in members.values():
        if len(indices) < 2:
            continue

        counts = Counter(pairs[i][1].category for i in indices)
        top = max(counts.values())
        candidates = {category for category, n in counts.items() if n == top}
        if len(candidates) == 1:
            winner = candidates.pop()
        else:
            oldest = min(indices, key=lambda i: (pairs[i][0].published_at, i))
            winner = pairs[oldest][1].category

        for i in indices:
            if pairs[i][1].category != winner:
                pairs[i][1].category = winner
                changed += 1

    return changed


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


def reconcile_email_attempts(
    db, attempted_uidls: set[str], saved_uidls: set[str], options: RunOptions,
    max_attempts: int,
) -> None:
    """保存に至らなかったメールの試行回数を記録し、上限に達したら打ち切る。

    POP3のメールはサーバから削除しないため、保存されないメールは毎回フルRETRされる。
    恒久的に失敗するメール（poison message）が毎回ダウンロードされ続けるのを防ぐ。
    """
    if not options.run_db:
        return

    for uidl in sorted(attempted_uidls - saved_uidls):
        try:
            # 保存後の工程（Discord投稿など）で例外離脱した場合、保存済みのメールが
            # ここに紛れ込みうる。処理済みなら試行回数を数える必要はない。
            if db.is_email_processed(uidl):
                continue
            attempts = db.record_email_attempt(uidl)
        except Exception as e:
            logger.error(
                "メールの試行回数の記録に失敗しました (uidl: %s): %s", uidl, e, exc_info=True
            )
            continue

        if attempts >= max_attempts:
            db.mark_email_processed(uidl)
            logger.warning(
                "メールの取得試行が%d回に達したため、処理済みとして打ち切ります "
                "(uidl: %s)",
                attempts,
                uidl,
            )


def persist_and_publish(
    summaries, digest, embeddings, db, options: RunOptions,
    rss_fetcher: MinifluxFetcher | None = None,
) -> SaveResult:
    """Save to DB and/or post to Discord based on RunOptions.

    Returns what was actually persisted, so the caller can limit mark-as-read
    and processed-marking to those articles.
    """
    logger.info("結果を保存・出力しています...")
    only_summaries = [s[1] for s in summaries]
    result = SaveResult(batch_id=None, saved=[])

    if options.run_db:
        result = db.save_batch(summaries, digest, embeddings)
        if result.is_partial:
            logger.warning(
                "%d件の記事を保存できませんでした。未保存分は既読化せず次回に回します。",
                result.failed,
            )
        # DB保存に成功したRSS記事だけを既読化する（要約・保存に失敗した記事は次回再取得）
        rss_entry_ids = [
            int(source_id) for source_type, source_id in result.saved if source_type == "rss"
        ]
        logger.info("データベースへの保存が完了しました。")
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
            print(f"\n[{c.category}] ({c.article_count}件)\n{c.summary}")
            for highlight in c.highlights:
                print(f"  • {highlight}")

    return result


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------

def run_pipeline(config: AppConfig, options: RunOptions) -> None:
    """Run the full fetch → summarize → group → digest → output pipeline."""
    logger.info("プロセス開始")

    db = create_database()

    # The Firestore lock is a real write (and a 2h lease if the run is killed),
    # so a --dry-run must not take it — --dry-run is documented as skipping DB writes.
    lock = db.execution_lock() if options.run_db else nullcontext()

    with lock:
        articles, rss_fetcher = fetch_articles(options, db)
        if not articles:
            logger.info("新規記事はありませんでした。処理を終了します。")
            return

        dedup = filter_new_articles(articles, db)
        articles = dedup.remaining

        # 処理対象外と確定したRSS記事はMinifluxでも既読にする。ここで既読化しないと
        # 「DB保存済み（または重複）だがMiniflux上は未読」の記事が毎回取得され続ける。
        # 今回の実行の成否とは無関係なので、早期returnより前に済ませておく。
        if rss_fetcher is not None and dedup.rss_ids_to_mark_read:
            rss_fetcher.mark_as_read(dedup.rss_ids_to_mark_read)

        if dedup.already_processed:
            logger.info("処理済み記事を%d件除外しました。", dedup.already_processed)
        if dedup.duplicate_urls:
            logger.info("同一URLの重複記事を%d件除外しました。", dedup.duplicate_urls)
        if not articles:
            logger.info("未処理の記事はありませんでした。処理を終了します。")
            return

        max_articles = config.summarizer.max_articles_per_run
        if len(articles) > max_articles:
            logger.warning(
                "1回の処理上限%d件を超えたため、%d件を次回へ繰り越します。",
                max_articles,
                len(articles) - max_articles,
            )
            articles = select_articles(articles, max_articles)

        logger.info("%d件の新規記事を取得しました。", len(articles))

        # 繰り越された（＝要約すら試みていない）メールは対象外。試行していないもので
        # リトライ枠を消費させない。
        attempted_email_uidls = {
            article.source_id for article in articles if article.source_type == "email"
        }
        save_result = SaveResult(batch_id=None, saved=[])
        try:
            save_result = _process(articles, db, options, rss_fetcher)
        finally:
            saved_uidls = {
                source_id
                for source_type, source_id in save_result.saved
                if source_type == "email"
            }
            reconcile_email_attempts(
                db,
                attempted_email_uidls,
                saved_uidls,
                options,
                config.email.max_fetch_attempts if config.email else 3,
            )

    logger.info("プロセス完了")


def _process(
    articles: list[Article], db, options: RunOptions,
    rss_fetcher: MinifluxFetcher | None,
) -> SaveResult:
    """Summarize → group → digest → output. Returns what was persisted."""
    pairs = summarize_all(articles, options)
    if not pairs:
        logger.warning("要約に成功した記事がありませんでした。処理を終了します。")
        return SaveResult(batch_id=None, saved=[])

    article_group_map, embeddings = group_pairs(pairs, options)

    # ダイジェストも Viewer も「カテゴリ → グループ」で階層化するため、
    # クラスタ内のカテゴリを先に揃えておかないと同じニュースが2箇所に分かれる。
    changed = unify_group_categories(pairs, article_group_map)
    if changed:
        logger.info("同一グループ内のカテゴリを%d件統一しました。", changed)

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

    return persist_and_publish(summaries, digest, embeddings, db, options, rss_fetcher)
