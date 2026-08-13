"""MinifluxFetcher の既読化タイミングのテスト。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from fetchers.rss_fetcher import MinifluxFetcher


def _fake_config(fetch_limit: int = 100):
    cfg = MagicMock()
    cfg.miniflux.base_url = "http://miniflux.local"
    cfg.miniflux.api_key = "test-key"
    cfg.miniflux.fetch_limit = fetch_limit
    return cfg


def _entries_response(entries: list[dict], total: int | None = None):
    resp = MagicMock()
    payload: dict = {"entries": entries}
    if total is not None:
        payload["total"] = total
    resp.json.return_value = payload
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


def test_fetch_sends_explicit_limit_and_oldest_first_order():
    """limit を明示しないと Miniflux 側の既定値 100 で打ち切られるため、必ず送る。

    order/direction も既定値と同じ値だが明示する。サーバ既定に任せると新しい順に
    変わりうるため、未読が limit を超えたときに古い記事が取り残される。
    """
    with patch("fetchers.rss_fetcher.config", _fake_config(fetch_limit=250)):
        fetcher = MinifluxFetcher(dry_run=False)
        with patch(
            "fetchers.rss_fetcher.httpx.get", return_value=_entries_response([])
        ) as mock_get:
            fetcher.fetch()

    _, kwargs = mock_get.call_args
    assert kwargs["params"] == {
        "status": "unread",
        "limit": 250,
        "order": "published_at",
        "direction": "asc",
    }
    # 本文フルHTMLを250件ぶん受け取るので、既定の10秒では足りない
    assert kwargs["timeout"] >= 60.0


def test_fetch_logs_unread_total(caplog):
    """未読の全件数(total)をログに残す。取得数が limit に張り付いたとき、
    残りが何件なのかはこれを見ないと分からない。"""
    entries = [
        {
            "id": 1,
            "content": "本文あり",
            "title": "記事1",
            "published_at": "2024-01-01T00:00:00Z",
            "url": "https://example.com/1",
            "feed": {"title": "フィード"},
        }
    ]

    with patch("fetchers.rss_fetcher.config", _fake_config(fetch_limit=250)):
        fetcher = MinifluxFetcher(dry_run=False)
        with caplog.at_level("INFO", logger="fetchers.rss_fetcher"):
            with patch(
                "fetchers.rss_fetcher.httpx.get",
                return_value=_entries_response(entries, total=412),
            ):
                fetcher.fetch()

    assert "412" in caplog.text


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
