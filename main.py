import argparse

import numpy as np
from tqdm import tqdm

import config as config_module
from config import reload_config
from logger import setup_logging, get_logger

from outputs.database import Database
from outputs.discord_output import DiscordOutput
from fetchers.rss_fetcher import MinifluxFetcher
from fetchers.email_fetcher import EmailFetcher
from summarizer.grouper import group_articles, group_summaries
from summarizer.summarizer import summarize_article
from summarizer.digest import generate_digest

logger = get_logger(__name__)

def main():
    parser = argparse.ArgumentParser(description="AI News Summarizer")
    parser.add_argument("--dry-run", action="store_true", help="ドライラン（Discord出力や既読化、DB保存を行わない）")
    parser.add_argument("--output", type=str, choices=["discord", "db", "all"], nargs="+", help="ドライラン中でも強制的に出力するターゲット（例: --output discord）")
    parser.add_argument("--source", type=str, choices=["rss", "email", "all"], default="all", help="取得先ソースの指定")
    parser.add_argument("--config", type=str, default="config.yaml", help="設定ファイルのパス")
    parser.add_argument("--stream", action="store_true", help="LLMの出力をストリーミング表示する（デバッグ用）")
    parser.add_argument("--debug", action="store_true", help="embedding・類似度判定の詳細情報を表示する（デバッグ用）")
    args = parser.parse_args()

    # 設定の再読み込み
    if args.config != "config.yaml":
        reload_config(args.config)

    setup_logging()
    logger.info("プロセス開始")

    db = Database()

    # 記事取得
    articles = []
    if args.source in ["all", "rss"]:
        try:
            logger.info("RSS（Miniflux）から記事を取得中...")
            rss_fetcher = MinifluxFetcher()
            if args.dry_run:
                rss_fetcher._mark_as_read = lambda x: logger.info("[Dry-Run] 既読化をスキップしました")
            articles.extend(rss_fetcher.fetch())
        except Exception as e:
            logger.error("RSSの取得中にエラーが発生しました: %s", e, exc_info=True)

    if args.source in ["all", "email"]:
        try:
            logger.info("Email（POP3）から記事を取得中...")
            email_fetcher = EmailFetcher(db)
            articles.extend(email_fetcher.fetch())
        except Exception as e:
            logger.error("Emailの取得中にエラーが発生しました: %s", e, exc_info=True)

    if not articles:
        logger.info("新規記事はありませんでした。処理を終了します。")
        return

    logger.info("%d件の新規記事を取得しました。", len(articles))

    # 個別記事要約
    pairs = []  # list of (Article, ArticleSummary)
    if args.stream:
        logger.info("個別記事の要約を行っています...")
        articles_iter = articles
    else:
        articles_iter = tqdm(articles, desc="記事を要約中", unit="件")
    for article in articles_iter:
        try:
            summary = summarize_article(article, stream=args.stream)
            pairs.append((article, summary))
        except Exception as e:
            logger.error("記事要約中にエラーが発生しました (ID: %s): %s", article.source_id, e, exc_info=True)

    if not pairs:
        logger.warning("要約に成功した記事がありませんでした。処理を終了します。")
        return

    # embedding 取得 + グルーピング
    embeddings: np.ndarray | None = None
    article_group_map: dict[int, tuple] = {}

    from summarizer.llm_client import get_step_config
    step_cfg = get_step_config("grouper")
    use_embeddings = step_cfg.use_embeddings
    embedding_model = config_module.config.llm.embedding_model

    if use_embeddings and embedding_model:
        logger.info("Embedding を取得中...")
        try:
            from summarizer.embedder import get_embeddings
            texts = [f"{s.title} {s.summary}" for _, s in pairs]
            embeddings = get_embeddings(texts, debug=args.debug)

            logger.info("類似記事の統合を行っています（embedding）...")
            only_summaries = [s for _, s in pairs]
            grouping_result = group_summaries(only_summaries, embeddings, stream=args.stream, debug=args.debug)
            for group in grouping_result.groups:
                for idx in group.article_indices:
                    article_group_map[idx] = (group.group_id, group.topic)
        except Exception as e:
            logger.error("Embedding グルーピング中にエラーが発生しました: %s", e, exc_info=True)
            embeddings = None
            article_group_map = {}
    else:
        logger.info("類似記事の統合を行っています...")
        try:
            grouping_result = group_articles(articles, stream=args.stream)
            for group in grouping_result.groups:
                for idx in group.article_indices:
                    article_group_map[idx] = (group.group_id, group.topic)
        except Exception as e:
            logger.error("類似記事統合中にエラーが発生しました: %s", e, exc_info=True)
            article_group_map = {}

    # group 情報を付与した summaries リストを構築
    summaries = []
    for i, (article, summary) in enumerate(pairs):
        group_info = article_group_map.get(i, (None, None))
        summaries.append((article, summary, group_info[0], group_info[1]))

    # ダイジェスト生成
    logger.info("ダイジェストを生成しています...")
    only_summaries = [s[1] for s in summaries]
    try:
        digest = generate_digest(only_summaries, stream=args.stream)
    except Exception as e:
        logger.error("ダイジェスト生成中にエラーが発生しました: %s", e, exc_info=True)
        return

    # 出力処理
    logger.info("結果を保存・出力しています...")

    forced_outputs = set(args.output) if args.output else set()
    run_db = not args.dry_run or "db" in forced_outputs or "all" in forced_outputs
    run_discord = not args.dry_run or "discord" in forced_outputs or "all" in forced_outputs

    if run_db:
        batch_id = db.create_batch(total_articles=len(only_summaries), digest_text=digest.overview)
        for i, (article, summary, group_id, group_topic) in enumerate(summaries):
            embedding_list = embeddings[i].tolist() if embeddings is not None else None
            db.save_summary(batch_id, article, summary, group_id, group_topic, embedding_list)
            if article.source_type == "email":
                db.mark_email_processed(article.source_id)
        logger.info("データベースへの保存が完了しました。")
    else:
        logger.info("[Dry-Run] データベースへの保存をスキップしました。")

    if run_discord:
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

    logger.info("プロセス完了")

if __name__ == "__main__":
    main()
