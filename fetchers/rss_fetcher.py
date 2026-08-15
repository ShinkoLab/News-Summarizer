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
        self.fetch_limit = miniflux_cfg.fetch_limit
        self.dry_run = dry_run
        self.headers = {
            "X-Auth-Token": self.api_key
        }

    def fetch(self) -> List[Article]:
        url = f"{self.base_url}/v1/entries"
        params = {
            "status": "unread",
            "limit": self.fetch_limit,
            # Miniflux 側の既定値と同じ並び（published_at / asc）だが明示して固定する。
            # サーバ既定に任せるとバージョン差で新しい順に変わりうる。古い順でないと、
            # 未読が fetch_limit を超えて滞留したときに古い記事が永久に取り残される。
            "order": "published_at",
            "direction": "asc",
        }

        try:
            # 本文フルHTMLを含むレスポンスなので、件数に比例して重くなる。
            # 100件で実測3秒程度。fetch_limit を上げても頭打ちしないよう長めに取る。
            response = httpx.get(url, params=params, headers=self.headers, timeout=60.0)
            response.raise_for_status()
        except httpx.RequestError as e:
            logger.error("Failed to fetch from Miniflux: %s", e, exc_info=True)
            return []

        data = response.json()
        entries = data.get("entries", [])

        # total は未読の全件数（limit で切られる前）。取得件数が limit に張り付いて
        # いるとき、残りが何件なのかはこれを見ないと分からない。
        total = data.get("total")
        if total is not None:
            logger.info(
                "Minifluxの未読は%d件、うち%d件を取得しました（limit=%d）。",
                total,
                len(entries),
                self.fetch_limit,
            )

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
