"""EmailFetcher のサーバ側削除まわりのテスト。"""

from __future__ import annotations

import poplib
import ssl

import pytest

from datetime import UTC
from unittest.mock import MagicMock, patch

from fetchers.base import FetchError
from fetchers.email_fetcher import EmailFetcher, POP3_TIMEOUT_SECONDS


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
        with pytest.raises(FetchError) as caught:
            fetcher.fetch()

    assert caught.value.articles == []
    assert isinstance(caught.value.__cause__, OSError)
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


@pytest.mark.parametrize("use_ssl", [True, False])
def test_connect_uses_bounded_timeout_and_verified_tls(use_ssl):
    fetcher = _make_fetcher()
    fetcher.use_ssl = use_ssl
    constructor = "POP3_SSL" if use_ssl else "POP3"
    with patch(f"fetchers.email_fetcher.poplib.{constructor}") as connect:
        server = fetcher._connect()

    args, kwargs = connect.call_args
    assert args == (fetcher.host, fetcher.port)
    assert kwargs["timeout"] == POP3_TIMEOUT_SECONDS == 30.0
    if use_ssl:
        assert isinstance(kwargs["context"], ssl.SSLContext)
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["context"].check_hostname is True
    else:
        assert "context" not in kwargs
    server.user.assert_called_once_with(fetcher.username)
    server.pass_.assert_called_once_with(fetcher.password)


@pytest.mark.parametrize("command", ["user", "pass_"])
@pytest.mark.parametrize("use_ssl", [True, False])
def test_connect_closes_socket_on_login_failure(command, use_ssl):
    fetcher = _make_fetcher()
    fetcher.use_ssl = use_ssl
    server = _fake_server({})
    failure = poplib.error_proto("login rejected")
    getattr(server, command).side_effect = failure
    constructor = "POP3_SSL" if use_ssl else "POP3"
    with patch(f"fetchers.email_fetcher.poplib.{constructor}", return_value=server):
        with pytest.raises(poplib.error_proto) as caught:
            fetcher._connect()

    assert caught.value is failure
    server.close.assert_called_once()
    server.quit.assert_not_called()


def test_fetch_empty_mailbox_is_success():
    fetcher, articles = _fetch_with_messages({})
    assert articles == []
    assert fetcher.failed_uidls == set()


def test_fetch_partial_retr_failure_preserves_articles_and_parse_failures():
    fetcher = _make_fetcher()
    server = _fake_server({"good": 1, "bad": 2, "unretrieved": 3})
    server.retr.side_effect = [
        (b"+OK", _raw_message("good", None), 0),
        (b"+OK", [b"bad"], 0),
        OSError("connection reset"),
    ]
    fetcher.db.is_email_processed.return_value = False
    original = fetcher._parse_message

    def parse(uidl, lines):
        if uidl == "bad":
            raise ValueError("malformed")
        return original(uidl, lines)

    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server), \
        patch.object(fetcher, "_parse_message", side_effect=parse):
        with pytest.raises(FetchError) as caught:
            fetcher.fetch()

    assert [article.source_id for article in caught.value.articles] == ["good"]
    assert fetcher.failed_uidls == {"bad"}
    server.quit.assert_called_once()
    server.dele.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["connect", "uidl", "db"])
def test_fetch_source_failure_is_not_empty_success(failure_stage):
    fetcher = _make_fetcher()
    server = _fake_server({"uid-a": 1})
    failure = OSError("unavailable")
    if failure_stage == "uidl":
        server.uidl.side_effect = failure
    if failure_stage == "db":
        fetcher.db.is_email_processed.side_effect = failure
    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server) as connect:
        if failure_stage == "connect":
            connect.side_effect = failure
        with pytest.raises(FetchError) as caught:
            fetcher.fetch()

    assert caught.value.articles == []
    assert caught.value.__cause__ is failure
    assert fetcher.failed_uidls == set()


def test_fetch_certificate_failure_propagates_as_source_failure():
    fetcher = _make_fetcher()
    failure = ssl.SSLCertVerificationError("certificate verify failed")
    with patch("fetchers.email_fetcher.poplib.POP3_SSL", side_effect=failure):
        with pytest.raises(FetchError) as caught:
            fetcher.fetch()
    assert caught.value.__cause__ is failure
    assert caught.value.articles == []
    assert fetcher.failed_uidls == set()


def test_fetch_closes_socket_when_quit_fails():
    fetcher = _make_fetcher()
    server = _fake_server({})
    server.quit.side_effect = OSError("disconnected")
    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server):
        assert fetcher.fetch() == []
    server.close.assert_called_once()


def test_fetch_all_parse_failures_are_message_local_not_source_failure():
    fetcher = _make_fetcher()
    server = _fake_server({"bad": 1})
    server.retr.return_value = (b"+OK", [b"bad"], 0)
    fetcher.db.is_email_processed.return_value = False
    with patch("fetchers.email_fetcher.poplib.POP3_SSL", return_value=server), \
        patch.object(fetcher, "_parse_message", side_effect=ValueError("malformed")):
        assert fetcher.fetch() == []
    assert fetcher.failed_uidls == {"bad"}
