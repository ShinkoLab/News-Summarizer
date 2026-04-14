import httpx
from datetime import datetime
from typing import List

from models import DigestResult, ArticleSummary
from config import config
from logger import get_logger

logger = get_logger(__name__)

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
        digest_embed = self._create_digest_embed(digest)
        payload = {
            "embeds": [digest_embed]
        }
        
        try:
            response = httpx.post(self.webhook_url, json=payload, timeout=10.0)
            response.raise_for_status()
        except httpx.RequestError as e:
            logger.error("Discordへのダイジェスト送信に失敗しました: %s", e, exc_info=True)
            return

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
        
        description = f"**{digest.overview}**\n\n"
        
        for cat in digest.categories:
            bullets = "\n".join(f"• {a}" for a in cat.articles)
            description += f"**{cat.category} ({cat.article_count}件)**\n{bullets}\n\n"
            
        return {
            "title": "📰 ニュースダイジェスト",
            "description": description.strip(),
            "color": self.embed_color,
            "footer": {
                "text": f"{self.footer_text + ' | ' if self.footer_text else ''}全{digest.total_articles}件の記事 | {now_str}"
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
