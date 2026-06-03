"""MinifluxFetcher の既読化タイミングのテスト。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from fetchers.rss_fetcher import MinifluxFetcher


def _fake_config():
    cfg = MagicMock()
    cfg.miniflux.base_url = "http://miniflux.local"
    cfg.miniflux.api_key = "test-key"
    return cfg


def _entries_response(entries: list[dict]):
    resp = MagicMock()
    resp.json.return_value = {"entries": entries}
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_defers_real_articles_and_marks_only_empty():
    """fetch() は本文ありの実記事を既読化せず、本文が空の記事のみ即既読化する。"""
    entries = [
        {
            "id": 1,
            "content": "本文あり",
            "title": "記事1",
            "published_at": "2024-01-01T00:00:00Z",
            "url": "https://example.com/1",
            "feed": {"title": "フィード"},
        },
        {"id": 2, "content": "   ", "title": "空記事"},
    ]

    with patch("fetchers.rss_fetcher.config", _fake_config()):
        fetcher = MinifluxFetcher(dry_run=False)
        with patch("fetchers.rss_fetcher.httpx.get", return_value=_entries_response(entries)), \
            patch.object(fetcher, "mark_as_read") as mock_mark:
            articles = fetcher.fetch()

    # 実記事のみ Article 化される
    assert [a.source_id for a in articles] == ["1"]
    # 既読化は空記事(id=2)に対してのみ即時実行される
    mock_mark.assert_called_once_with([2])


def test_mark_as_read_raises_logged_not_propagated():
    """mark_as_read は HTTP エラーを送出せず error ログ化して握りつぶす。"""
    with patch("fetchers.rss_fetcher.config", _fake_config()):
        fetcher = MinifluxFetcher(dry_run=False)
        failing = MagicMock()
        failing.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=MagicMock()
        )
        with patch("fetchers.rss_fetcher.httpx.put", return_value=failing):
            # 例外が伝播しないこと
            fetcher.mark_as_read([1, 2])


def test_mark_as_read_dry_run_skips_put():
    """dry_run では PUT を送らない。"""
    with patch("fetchers.rss_fetcher.config", _fake_config()):
        fetcher = MinifluxFetcher(dry_run=True)
        with patch("fetchers.rss_fetcher.httpx.put") as mock_put:
            fetcher.mark_as_read([1])
        mock_put.assert_not_called()
