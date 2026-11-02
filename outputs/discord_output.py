import httpx
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

    def payloads(self, digest: DigestResult, summaries: List[ArticleSummary]) -> list[dict]:
        """Render once; retries must never rebuild a changing payload."""
        payloads = []
        if digest.overview or digest.categories:
            payloads.append({"embeds": [self._create_digest_embed(digest)]})
        else:
            logger.warning("ダイジェストが空のため、ダイジェスト投稿をスキップします。")
        if summaries and self.post_individual_articles:
            embeds = [self._create_summary_embed(s) for s in summaries]
            payloads.extend({"embeds": embeds[i:i+10]} for i in range(0, len(embeds), 10))
        return payloads

    def send_payload(self, payload: dict) -> None:
        if not self.webhook_url:
            raise RuntimeError("Discord webhook URL is not configured")
        # Discord wait=false can acknowledge before the message is saved.
        # Preserve thread_id/other query parameters while overriding any wait=false.
        url = httpx.URL(self.webhook_url).copy_set_param("wait", "true")
        response = httpx.post(url, json=payload, timeout=10.0)
        response.raise_for_status()

    def post(self, digest: DigestResult, summaries: List[ArticleSummary]):
        if not self.webhook_url:
            logger.warning("Discord webhook URLが設定されていません。Discord出力をスキップします。")
            return 0
        payloads = self.payloads(digest, summaries)
        for payload in payloads:
            self.send_payload(payload)
        return len(payloads)

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
