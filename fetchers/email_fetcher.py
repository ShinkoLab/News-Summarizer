import poplib
import email
from email.policy import default
from datetime import UTC, datetime
import email.utils
from typing import List
import re

from models import Article
from fetchers.base import BaseFetcher, to_utc
from config import config
from outputs.database import Database
from logger import get_logger

logger = get_logger(__name__)

def strip_tags(html: str) -> str:
    # 簡易なHTMLタグ除去
    text = re.sub(r'<[^>]+>', ' ', html)
    # 連続する空白・改行を整理
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

class EmailFetcher(BaseFetcher):
    def __init__(self, db: Database, dry_run: bool = False):
        if config.email is None:
            raise ValueError("email の設定が config.yaml に見つかりません。")
        email_cfg = config.email
        self.host = email_cfg.host
        self.port = email_cfg.port
        self.username = email_cfg.username
        self.password = email_cfg.password
        self.use_ssl = email_cfg.use_ssl
        self.delete_after_processing = email_cfg.delete_after_processing
        self.db = db
        self.dry_run = dry_run
        # 本文の解析に失敗したメール。Article にならないので保存側の試行回数管理に
        # 乗らず、放置すると毎回フルRETRされ続ける。パイプラインが別途数える。
        self.failed_uidls: set[str] = set()

    def _connect(self):
        """POP3セッションを開いてログインする。"""
        if self.use_ssl:
            server = poplib.POP3_SSL(self.host, self.port)
        else:
            server = poplib.POP3(self.host, self.port)

        server.user(self.username)
        server.pass_(self.password)
        return server

    def _uidl_map(self, server) -> dict[str, int]:
        """UIDL → メッセージ番号のマップを作る。

        POP3のメッセージ番号はセッションごとに振り直されるため、削除する側は
        必ずそのセッションで取り直したマップを使わなければならない。
        """
        response, listings, octets = server.uidl()
        uidl_map: dict[str, int] = {}
        for listing in listings:
            try:
                msg_num_str, uidl = listing.decode('utf-8').split(' ')
            except ValueError:
                continue
            uidl_map[uidl] = int(msg_num_str)
        return uidl_map

    def fetch(self) -> List[Article]:
        articles = []
        self.failed_uidls = set()
        server = None
        try:
            server = self._connect()

            # メッセージのリストとUIDLを取得
            for uidl, msg_num in self._uidl_map(server).items():
                # DBで処理済みかチェック
                if self.db.is_email_processed(uidl):
                    continue

                # RETRの失敗は接続の問題なので、後続も失敗する。ここは外側で打ち切り、
                # 試行回数にも数えない（数えると一時的な障害でメールが打ち切られる）。
                ret, lines, octets = server.retr(msg_num)

                # 1通の不正なメールで後続のメールまで取り逃さないよう、解析は1通ずつ隔離する
                try:
                    articles.append(self._parse_message(uidl, lines))
                except Exception as e:
                    self.failed_uidls.add(uidl)
                    logger.error(
                        "メールの解析に失敗したためスキップします (uidl: %s): %s",
                        uidl, e, exc_info=True,
                    )

        except Exception as e:
            logger.error("POP3メール取得中にエラーが発生しました: %s", e, exc_info=True)
        finally:
            # fetch()はDELEを一切発行しないので、例外で抜けた場合もQUITして構わない。
            if server is not None:
                try:
                    server.quit()
                except Exception as e:
                    logger.warning("POP3セッションの終了に失敗しました: %s", e)

        return articles

    def _parse_message(self, uidl: str, lines: list[bytes]) -> Article:
        msg_content = b'\r\n'.join(lines)
        msg = email.message_from_bytes(msg_content, policy=default)

        title = msg.get("Subject", "No Subject")

        # 日付が壊れているだけで記事ごと捨てる理由はないので、取得時刻で代替する。
        # Python 3.10 以降の parsedate_to_datetime は不正な値で None ではなく例外を投げる。
        published_at = datetime.now(UTC)
        date_str = msg.get("Date")
        if date_str:
            try:
                published_at = to_utc(email.utils.parsedate_to_datetime(date_str))
            except (TypeError, ValueError):
                logger.warning(
                    "Dateヘッダーを解釈できないため取得時刻で代替します (uidl: %s): %r",
                    uidl, date_str,
                )

        # 本文の抽出 (Text優先、なければHTMLからタグ除去)
        content = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        content += payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                elif content_type == "text/html" and not content:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html_content = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
                        content += strip_tags(html_content)
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                text = payload.decode(charset, errors='replace')
                if msg.get_content_type() == "text/html":
                    content = strip_tags(text)
                else:
                    content = text

        if not content.strip():
            content = "(本文なし)"

        return Article(
            source_type="email",
            source_id=uidl,
            title=title,
            content=content,
            url=None,
            published_at=published_at,
            fetched_at=datetime.now(UTC),
            feed_title="Email Newsletter"
        )

    def delete_messages(self, uidls: set[str]) -> int:
        """処理が終わったメールをサーバから削除する。削除できた件数を返す。

        POP3のDELEは削除マークを付けるだけで、確定するのはQUIT。異常終了すれば
        サーバ側でロールバックされる（RFC 1939）ので、途中で失敗したときは
        QUITせずソケットだけ閉じ、1通も消さずに次回の実行へ委ねる。
        """
        if not self.delete_after_processing or not uidls:
            return 0

        if self.dry_run:
            logger.info("[Dry-Run] メールの削除をスキップしました（%d件）", len(uidls))
            return 0

        server = None
        deleted = 0
        try:
            server = self._connect()
            uidl_map = self._uidl_map(server)

            for uidl in sorted(uidls):
                msg_num = uidl_map.get(uidl)
                if msg_num is None:
                    # 既に他のクライアントが削除した等。消す対象が無いだけなので続行する。
                    logger.warning(
                        "削除対象のメールがサーバに見つかりませんでした (uidl: %s)", uidl
                    )
                    continue
                server.dele(msg_num)
                deleted += 1

            # QUITで初めて削除が確定する。
            server.quit()
            server = None
            logger.info("処理済みのメールを%d件サーバから削除しました。", deleted)
        except Exception as e:
            logger.error("メールの削除中にエラーが発生しました: %s", e, exc_info=True)
            deleted = 0
            if server is not None:
                # QUITしない＝サーバ側の削除マークは破棄される。
                try:
                    server.close()
                except Exception as close_error:
                    logger.warning("POP3ソケットの切断に失敗しました: %s", close_error)

        return deleted
