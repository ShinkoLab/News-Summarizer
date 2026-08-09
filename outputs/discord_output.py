import httpx
from datetime import datetime
from typing import List

from models import DigestResult, ArticleSummary
from config import config
from logger import get_logger

logger = get_logger(__name__)

# Discord API の embed description 文字数上限
DISCORD_DESCRIPTION_LIMIT = 4096


class DiscordOutput:
    def __init__(self):
        discord_cfg = config.discord
        self.webhook_url = discord_cfg.webhook_url
        self.embed_color = discord_cfg.embed_color
        self.footer_text = discord_cfg.footer_text
        self.post_individual_articles = discord_cfg.post_individual_articles

    def post(self, digest: DigestResult, summaries: List[ArticleSummary]):
        if not self.webhook_url:
            logger.warning("Discord webhook URLが設定されていません。Discord出力をスキップします。")
            return

        # 1. ダイジェストの投稿
        # ダイジェスト生成が全て失敗して中身が空の場合は embed をスキップし、
        # 個別記事の投稿だけ行う（個別記事はダイジェスト投稿の成否に依存させない）
        if digest.overview or digest.categories:
            digest_embed = self._create_digest_embed(digest)
            payload = {
                "embeds": [digest_embed]
            }
            try:
                response = httpx.post(self.webhook_url, json=payload, timeout=10.0)
                response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error("Discordへのダイジェスト送信に失敗しました: %s", e, exc_info=True)
        else:
            logger.warning("ダイジェストが空のため、ダイジェスト投稿をスキップします。")

        # 2. 個別記事の詳細を追加Embedとして投稿 (Discord制限で1回につき最大10個)
        if summaries and self.post_individual_articles:
            summary_embeds = [self._create_summary_embed(s) for s in summaries]
            for i in range(0, len(summary_embeds), 10):
                chunk = summary_embeds[i:i+10]
                try:
                    httpx.post(self.webhook_url, json={"embeds": chunk}, timeout=10.0)
                except httpx.RequestError as e:
                    logger.error("Discordへの記事要約送信に失敗しました: %s", e, exc_info=True)

    def _create_digest_embed(self, digest: DigestResult) -> dict:
        now_str = digest.generated_at.strftime("%Y-%m-%d %H:%M")

        # overview 生成に失敗した場合は空になるため、太字 overview 行を省略する
        description = f"**{digest.overview}**\n\n" if digest.overview else ""

        for cat in digest.categories:
            description += f"**{cat.category} ({cat.article_count}件)**\n{cat.summary}\n"
            if cat.highlights:
                description += "".join(f"• {h}\n" for h in cat.highlights)
            description += "\n"

        # Discord の embed description 上限は 4096 文字。
        # 超過すると投稿自体が 400 で失敗するため、末尾を切り詰めて必ず投稿を通す。
        description = description.strip()
        truncated = len(description) > DISCORD_DESCRIPTION_LIMIT
        if truncated:
            logger.warning(
                "ダイジェストがDiscordの上限(%d文字)を超えたため末尾を切り詰めました: %d文字",
                DISCORD_DESCRIPTION_LIMIT,
                len(description),
            )
            description = description[: DISCORD_DESCRIPTION_LIMIT - 1] + "…"

        # 退化を footer に注記する。生成の失敗（カテゴリ除外・overview失敗）と、
        # 生成は成功したが Discord の上限に収まらなかった切り詰めは原因が別なので
        # 区別する。切り詰めだけを「生成に失敗」と表示すると誤解を招く。
        shown_articles = sum(cat.article_count for cat in digest.categories)
        generation_failed = shown_articles < digest.total_articles or not digest.overview
        notes = []
        if generation_failed:
            notes.append("※一部の生成に失敗")
        if truncated:
            notes.append("※文字数超過のため末尾を省略")
        degraded_note = "".join(f" | {note}" for note in notes)

        return {
            "title": "📰 ニュースダイジェスト",
            "description": description,
            "color": self.embed_color,
            "footer": {
                "text": f"{self.footer_text + ' | ' if self.footer_text else ''}全{digest.total_articles}件の記事 | {now_str}{degraded_note}"
            }
        }

    def _create_summary_embed(self, summary: ArticleSummary) -> dict:
        return {
            "title": summary.title,
            "description": summary.summary,
            "color": self.embed_color,
            "fields": [
                {
                    "name": "カテゴリ",
                    "value": summary.category,
                    "inline": True
                },
                {
                    "name": "キーワード",
                    "value": ", ".join(summary.keywords),
                    "inline": True
                }
            ]
        }
