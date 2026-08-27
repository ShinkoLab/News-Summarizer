"""scripts/skip_stale_backlog.py のテスト。"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import scripts.skip_stale_backlog as skip_stale_backlog


def _fake_config(miniflux=None, email=None):
    cfg = MagicMock()
    cfg.miniflux = miniflux
    cfg.email = email
    return cfg


def _miniflux_cfg():
    cfg = MagicMock()
    cfg.base_url = "http://miniflux.local"
    cfg.api_key = "test-key"
    return cfg


def _entries_response(entries: list[dict]):
    resp = MagicMock()
    resp.json.return_value = {"entries": entries}
    resp.raise_for_status.return_value = None
    return resp


class TestParseMinifluxDatetime:
    def test_z_suffix(self):
        result = skip_stale_backlog._parse_miniflux_datetime("2026-08-23T00:00:00Z")
        assert result == datetime(2026, 8, 23, tzinfo=timezone.utc)

    def test_offset_suffix(self):
        result = skip_stale_backlog._parse_miniflux_datetime("2026-08-23T09:00:00+09:00")
        assert result is not None
        assert result.utcoffset().total_seconds() == 9 * 3600

    def test_empty_or_invalid_returns_none(self):
        assert skip_stale_backlog._parse_miniflux_datetime("") is None
        assert skip_stale_backlog._parse_miniflux_datetime("not-a-date") is None


class TestFindStaleMinifluxEntries:
    def test_stops_pagination_once_cutoff_reached(self):
        """published_at昇順なのでcutoff以降に到達したらそれ以上ページを取りに行かない。"""
        cutoff = datetime(2026, 8, 25, tzinfo=timezone.utc)
        page1 = _entries_response(
            [
                {"id": 1, "published_at": "2026-08-20T00:00:00Z", "title": "old-1"},
                {"id": 2, "published_at": "2026-08-24T00:00:00Z", "title": "old-2"},
                {"id": 3, "published_at": "2026-08-26T00:00:00Z", "title": "new-1"},
                {"id": 4, "published_at": "2026-08-27T00:00:00Z", "title": "new-2"},
            ]
        )

        with patch.object(skip_stale_backlog.config_module, "config", _fake_config(miniflux=_miniflux_cfg())), \
            patch.object(skip_stale_backlog.httpx, "get", return_value=page1) as mock_get:
            entries = skip_stale_backlog.find_stale_miniflux_entries(cutoff)

        assert [e["id"] for e in entries] == [1, 2]
        # cutoff到達後は2ページ目を取りに行かない
        mock_get.assert_called_once()

    def test_paginates_when_entire_page_is_stale(self):
        """1ページ目が丸ごとcutoffより前（ページサイズちょうど）なら2ページ目も取りに行く。"""
        cutoff = datetime(2026, 8, 25, tzinfo=timezone.utc)
        page1 = _entries_response(
            [{"id": i, "published_at": "2026-08-20T00:00:00Z", "title": f"old-{i}"} for i in range(2)]
        )
        page2 = _entries_response(
            [{"id": 2, "published_at": "2026-08-21T00:00:00Z", "title": "old-2"}]
        )

        with patch.object(skip_stale_backlog.config_module, "config", _fake_config(miniflux=_miniflux_cfg())), \
            patch.object(skip_stale_backlog, "_MINIFLUX_PAGE_SIZE", 2), \
            patch.object(skip_stale_backlog.httpx, "get", side_effect=[page1, page2]) as mock_get:
            entries = skip_stale_backlog.find_stale_miniflux_entries(cutoff)

        assert [e["id"] for e in entries] == [0, 1, 2]
        assert mock_get.call_count == 2
        assert mock_get.call_args_list[1].kwargs["params"]["offset"] == 2

    def test_returns_empty_when_miniflux_not_configured(self):
        cutoff = datetime(2026, 8, 25, tzinfo=timezone.utc)
        with patch.object(skip_stale_backlog.config_module, "config", _fake_config(miniflux=None)):
            assert skip_stale_backlog.find_stale_miniflux_entries(cutoff) == []


class TestMarkMinifluxEntriesRead:
    def test_chunks_requests(self):
        entry_ids = list(range(250))
        put_mock = MagicMock()
        put_mock.return_value.raise_for_status.return_value = None

        with patch.object(skip_stale_backlog.config_module, "config", _fake_config(miniflux=_miniflux_cfg())), \
            patch.object(skip_stale_backlog.httpx, "put", put_mock):
            skip_stale_backlog.mark_miniflux_entries_read(entry_ids)

        assert put_mock.call_count == 3  # 100 + 100 + 50
        first_call_ids = put_mock.call_args_list[0].kwargs["json"]["entry_ids"]
        assert len(first_call_ids) == 100
        assert put_mock.call_args_list[0].kwargs["json"]["status"] == "read"

    def test_noop_for_empty_list(self):
        with patch.object(skip_stale_backlog.httpx, "put") as put_mock:
            skip_stale_backlog.mark_miniflux_entries_read([])
        put_mock.assert_not_called()


def _fake_email_cfg():
    cfg = MagicMock()
    cfg.host = "pop.example.com"
    cfg.port = 995
    cfg.username = "user"
    cfg.password = "pass"
    cfg.use_ssl = True
    return cfg


def _fake_pop3_server(uidl_listings: list[str], top_responses: dict[int, bytes]):
    server = MagicMock()
    server.uidl.return_value = (b"+OK", [s.encode() for s in uidl_listings], 0)
    server.top.side_effect = lambda msg_num, lines: ("+OK", top_responses[msg_num].split(b"\r\n"), 0)
    return server


class TestFindStaleEmailUidls:
    def test_filters_by_date_header_via_top(self):
        cutoff = datetime(2026, 8, 27, tzinfo=timezone.utc)
        server = _fake_pop3_server(
            uidl_listings=["1 uidl-old", "2 uidl-new"],
            top_responses={
                1: b"Subject: Old mail\r\nDate: Wed, 26 Aug 2026 10:00:00 +0000\r\n",
                2: b"Subject: New mail\r\nDate: Fri, 28 Aug 2026 10:00:00 +0000\r\n",
            },
        )

        with patch.object(skip_stale_backlog.config_module, "config", _fake_config(email=_fake_email_cfg())), \
            patch.object(skip_stale_backlog, "_connect_pop3", return_value=server):
            stale = skip_stale_backlog.find_stale_email_uidls(cutoff)

        assert stale == [("uidl-old", "Old mail")]
        server.quit.assert_called_once()

    def test_returns_empty_when_email_not_configured(self):
        cutoff = datetime(2026, 8, 27, tzinfo=timezone.utc)
        with patch.object(skip_stale_backlog.config_module, "config", _fake_config(email=None)):
            assert skip_stale_backlog.find_stale_email_uidls(cutoff) == []


class TestDeleteEmailUidls:
    def test_deletes_and_confirms_with_quit(self):
        server = _fake_pop3_server(uidl_listings=["1 uidl-a", "2 uidl-b"], top_responses={})

        with patch.object(skip_stale_backlog, "_connect_pop3", return_value=server):
            deleted = skip_stale_backlog.delete_email_uidls(["uidl-a"])

        assert deleted == 1
        server.dele.assert_called_once_with(1)
        server.quit.assert_called_once()

    def test_missing_uidl_is_skipped_not_fatal(self):
        server = _fake_pop3_server(uidl_listings=["1 uidl-a"], top_responses={})

        with patch.object(skip_stale_backlog, "_connect_pop3", return_value=server):
            deleted = skip_stale_backlog.delete_email_uidls(["uidl-a", "uidl-missing"])

        assert deleted == 1
        server.quit.assert_called_once()

    def test_exception_closes_without_quit_so_nothing_is_deleted(self):
        """DELEはQUITで確定するため、例外時はQUITせずcloseして削除をロールバックする。"""
        server = _fake_pop3_server(uidl_listings=["1 uidl-a"], top_responses={})
        server.dele.side_effect = RuntimeError("boom")

        with patch.object(skip_stale_backlog, "_connect_pop3", return_value=server):
            try:
                skip_stale_backlog.delete_email_uidls(["uidl-a"])
                assert False, "should have raised"
            except RuntimeError:
                pass

        server.quit.assert_not_called()
        server.close.assert_called_once()

    def test_noop_for_empty_list(self):
        with patch.object(skip_stale_backlog, "_connect_pop3") as connect_mock:
            deleted = skip_stale_backlog.delete_email_uidls([])
        assert deleted == 0
        connect_mock.assert_not_called()
