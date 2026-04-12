import httpx
from datetime import datetime
from typing import List
from models import Article
from fetchers.base import BaseFetcher
from config import config
from logger import get_logger

logger = get_logger(__name__)

class MinifluxFetcher(BaseFetcher):
    def __init__(self):
        miniflux_cfg = config["miniflux"]
        self.base_url = miniflux_cfg["base_url"]
        self.api_key = miniflux_cfg["api_key"]
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
        entry_ids = []
        
        for entry in entries:
            # 本文が空の記事はスキップ
            if not entry.get("content", "").strip():
                logger.debug("本文が空のため記事をスキップします: %r", entry.get("title", ""))
                entry_ids.append(entry["id"])
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
            entry_ids.append(entry["id"])

        # 取得した記事を既読にする
        if entry_ids:
            self._mark_as_read(entry_ids)

        return articles

    def _mark_as_read(self, entry_ids: List[int]):
        url = f"{self.base_url}/v1/entries"
        payload = {
            "entry_ids": entry_ids,
            "status": "read"
        }
        try:
            httpx.put(url, headers=self.headers, json=payload, timeout=10.0)
        except httpx.RequestError as e:
            logger.warning("Minifluxでの既読化に失敗しました: %s", e, exc_info=True)
