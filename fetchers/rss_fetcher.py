import httpx
from datetime import datetime
from typing import List
from models import Article
from fetchers.base import BaseFetcher
from config import config
from logger import get_logger

logger = get_logger(__name__)

class MinifluxFetcher(BaseFetcher):
    def __init__(self, dry_run: bool = False):
        if config.miniflux is None:
            raise ValueError("miniflux の設定が config.yaml に見つかりません。")
        miniflux_cfg = config.miniflux
        self.base_url = miniflux_cfg.base_url
        self.api_key = miniflux_cfg.api_key
        self.dry_run = dry_run
        self.headers = {
            "X-Auth-Token": self.api_key
        }

    def fetch(self) -> List[Article]:
        url = f"{self.base_url}/v1/entries?status=unread"
        
        try:
            response = httpx.get(url, headers=self.headers, timeout=10.0)
            response.raise_for_status()
        except httpx.RequestError as e:
            logger.error("Failed to fetch from Miniflux: %s", e, exc_info=True)
            return []

        data = response.json()
        entries = data.get("entries", [])
        
        articles = []
        empty_entry_ids = []

        for entry in entries:
            # 本文が空の記事はスキップ
            if not entry.get("content", "").strip():
                logger.debug("本文が空のため記事をスキップします: %r", entry.get("title", ""))
                # 本文が空の記事は要約対象外の恒久スキップ。再取得しても無駄なため即既読化する。
                empty_entry_ids.append(entry["id"])
                continue

            # Minifluxの日時フォーマットのパース
            try:
                pub_date_str = entry.get("published_at", "")
                if pub_date_str.endswith("Z"):
                    pub_date_str = pub_date_str[:-1] + "+00:00"
                published_at = datetime.fromisoformat(pub_date_str)
            except ValueError:
                published_at = datetime.now()

            articles.append(Article(
                source_type="rss",
                source_id=str(entry["id"]),
                title=entry.get("title", ""),
                content=entry.get("content", ""),
                url=entry.get("url"),
                published_at=published_at,
                fetched_at=datetime.now(),
                feed_title=entry.get("feed", {}).get("title", "")
            ))

        # 本文ありの実記事はここで既読化しない。要約・DB保存に成功した分だけ
        # パイプライン側から mark_as_read() を呼んで既読化する（失敗時の記事ロスト防止）。
        # 本文が空のスキップ記事のみ即既読化する。
        if empty_entry_ids:
            self.mark_as_read(empty_entry_ids)

        return articles

    def mark_as_read(self, entry_ids: List[int]):
        if not entry_ids:
            return
        if self.dry_run:
            logger.info("[Dry-Run] 既読化をスキップしました")
            return
        url = f"{self.base_url}/v1/entries"
        payload = {
            "entry_ids": entry_ids,
            "status": "read"
        }
        try:
            response = httpx.put(url, headers=self.headers, json=payload, timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("Minifluxでの既読化に失敗しました: %s", e, exc_info=True)
