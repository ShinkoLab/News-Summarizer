"""EmailFetcher のサーバ側削除まわりのテスト。"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

from fetchers.email_fetcher import EmailFetcher


def _fake_config(delete_after_processing: bool = True):
    cfg = MagicMock()
    cfg.email.host = "pop.example.com"
    cfg.email.port = 995
    cfg.email.username = "news@example.com"
    cfg.email.password = "secret"
    cfg.email.use_ssl = True
    cfg.email.delete_after_processing = delete_after_processing
    return cfg


def _fake_server(uidls: dict[str, int]):
    """UIDL一覧を返すPOP3サーバのモック。"""
    server = MagicMock()
    listings = [f"{num} {uidl}".encode() for uidl, num in uidls.items()]
    server.uidl.return_value = (b"+OK", listings, 0)
    return server


def _make_fetcher(delete_after_processing: bool = True, dry_run: bool = False):
    with patch("fetchers.email_fetcher.config", _fake_config(delete_after_processing)):
        return EmailFetcher(MagicMock(), dry_run=dry_run)


def test_delete_messages_uses_message_numbers_from_new_session():
    """POP3のメッセージ番号はセッションごとに変わるため、削除側で取り直す。"""
    fetcher = _make_fetcher()
    server = _fake_server({"uid-a": 1, "uid-b": 2, "uid-c": 3})

    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server):
        deleted = fetcher.delete_messages({"uid-a", "uid-c"})

    assert deleted == 2
    assert sorted(call.args[0] for call in server.dele.call_args_list) == [1, 3]
    # QUITで初めて削除が確定する
    server.quit.assert_called_once()


def test_delete_messages_skips_when_option_disabled():
    """delete_after_processing が false なら接続すらしない。"""
    fetcher = _make_fetcher(delete_after_processing=False)

    with patch("fetchers.email_fetcher.poplib.POP3_SSL") as mock_pop3:
        deleted = fetcher.delete_messages({"uid-a"})

    assert deleted == 0
    mock_pop3.assert_not_called()


def test_delete_messages_dry_run_skips_connection():
    """dry_run では削除しない（--output db でDB保存を強制した場合も同じ）。"""
    fetcher = _make_fetcher(dry_run=True)

    with patch("fetchers.email_fetcher.poplib.POP3_SSL") as mock_pop3:
        deleted = fetcher.delete_messages({"uid-a"})

    assert deleted == 0
    mock_pop3.assert_not_called()


def test_delete_messages_ignores_unknown_uidl():
    """既にサーバから消えているUIDLは飛ばして、残りの削除を続ける。"""
    fetcher = _make_fetcher()
    server = _fake_server({"uid-b": 7})

    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server):
        deleted = fetcher.delete_messages({"uid-a", "uid-b"})

    assert deleted == 1
    server.dele.assert_called_once_with(7)
    server.quit.assert_called_once()


def test_delete_messages_failure_does_not_commit():
    """DELEの途中で失敗したらQUITせず、サーバ側の削除マークを破棄させる。"""
    fetcher = _make_fetcher()
    server = _fake_server({"uid-a": 1, "uid-b": 2})
    server.dele.side_effect = [None, OSError("connection reset")]

    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server):
        deleted = fetcher.delete_messages({"uid-a", "uid-b"})

    assert deleted == 0
    server.quit.assert_not_called()
    server.close.assert_called_once()


def test_delete_messages_noop_for_empty_set():
    fetcher = _make_fetcher()

    with patch("fetchers.email_fetcher.poplib.POP3_SSL") as mock_pop3:
        assert fetcher.delete_messages(set()) == 0

    mock_pop3.assert_not_called()


def test_fetch_quits_session_on_error():
    """取得中に例外が出てもソケットを放置しない。"""
    fetcher = _make_fetcher()
    server = _fake_server({"uid-a": 1})
    server.retr.side_effect = OSError("boom")
    fetcher.db.is_email_processed.return_value = False

    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server):
        articles = fetcher.fetch()

    assert articles == []
    server.quit.assert_called_once()
    # RETRの失敗は接続の問題。数えると一時的な障害だけでメールが打ち切られる
    assert fetcher.failed_uidls == set()


def _raw_message(subject: str, date: str | None) -> list[bytes]:
    headers = [f"Subject: {subject}"]
    if date is not None:
        headers.append(f"Date: {date}")
    return [h.encode() for h in headers] + [b"", b"body"]


def _fetch_with_messages(messages: dict[str, list[bytes]]):
    fetcher = _make_fetcher()
    server = _fake_server({uidl: i + 1 for i, uidl in enumerate(messages)})
    bodies = list(messages.values())
    server.retr.side_effect = lambda num: (b"+OK", bodies[num - 1], 0)
    fetcher.db.is_email_processed.return_value = False

    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server):
        articles = fetcher.fetch()
    return fetcher, articles


def test_fetch_invalid_date_falls_back_and_keeps_going():
    """不正なDateヘッダー1通で後続のメールを取り逃さない。"""
    fetcher, articles = _fetch_with_messages({
        "uid-a": _raw_message("broken", "not a date"),
        "uid-b": _raw_message("ok", "Mon, 05 Jan 2026 09:00:00 +0900"),
    })

    assert [a.source_id for a in articles] == ["uid-a", "uid-b"]
    assert articles[0].published_at.tzinfo is not None
    assert fetcher.failed_uidls == set()


def test_fetch_normalizes_dates_to_utc():
    """-0000 は naive で返るが、UTCとして扱いタイムゾーン付きにそろえる。"""
    _, articles = _fetch_with_messages({
        "uid-a": _raw_message("jst", "Mon, 05 Jan 2026 09:00:00 +0900"),
        "uid-b": _raw_message("unknown", "Mon, 05 Jan 2026 00:00:00 -0000"),
    })

    assert [a.published_at.utcoffset().total_seconds() for a in articles] == [0, 0]
    assert articles[0].published_at == articles[1].published_at
    assert articles[1].published_at.tzinfo is UTC


def test_fetch_isolates_a_message_that_fails_to_parse():
    """解析で落ちた1通は failed_uidls に記録し、残りは取得を続ける。"""
    fetcher = _make_fetcher()
    server = _fake_server({"uid-a": 1, "uid-b": 2})
    server.retr.return_value = (b"+OK", _raw_message("ok", None), 0)
    fetcher.db.is_email_processed.return_value = False
    original = fetcher._parse_message

    def parse(uidl, lines):
        if uidl == "uid-a":
            raise ValueError("malformed")
        return original(uidl, lines)

    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server), \
        patch.object(fetcher, "_parse_message", side_effect=parse):
        articles = fetcher.fetch()

    assert [a.source_id for a in articles] == ["uid-b"]
    assert fetcher.failed_uidls == {"uid-a"}
