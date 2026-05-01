import poplib
import email
from email.policy import default
from datetime import datetime
import email.utils
from typing import List
import re

from models import Article
from fetchers.base import BaseFetcher
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
    def __init__(self, db: Database):
        if config.email is None:
            raise ValueError("email の設定が config.yaml に見つかりません。")
        email_cfg = config.email
        self.host = email_cfg.host
        self.port = email_cfg.port
        self.username = email_cfg.username
        self.password = email_cfg.password
        self.use_ssl = email_cfg.use_ssl
        self.db = db

    def fetch(self) -> List[Article]:
        articles = []
        try:
            if self.use_ssl:
                server = poplib.POP3_SSL(self.host, self.port)
            else:
                server = poplib.POP3(self.host, self.port)
            
            server.user(self.username)
            server.pass_(self.password)

            # メッセージのリストとUIDLを取得
            response, listings, octets = server.uidl()
            
            for listing in listings:
                try:
                    msg_num_str, uidl = listing.decode('utf-8').split(' ')
                except ValueError:
                    continue
                    
                msg_num = int(msg_num_str)
                
                # DBで処理済みかチェック
                if self.db.is_email_processed(uidl):
                    continue
                
                # メール本体を取得
                ret, lines, octets = server.retr(msg_num)
                msg_content = b'\r\n'.join(lines)
                msg = email.message_from_bytes(msg_content, policy=default)
                
                title = msg.get("Subject", "No Subject")
                
                date_str = msg.get("Date")
                published_at = datetime.now()
                if date_str:
                    parsed_date = email.utils.parsedate_to_datetime(date_str)
                    if parsed_date:
                        published_at = parsed_date
                
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

                articles.append(Article(
                    source_type="email",
                    source_id=uidl,
                    title=title,
                    content=content,
                    url=None,
                    published_at=published_at,
                    fetched_at=datetime.now(),
                    feed_title="Email Newsletter"
                ))

            server.quit()
        except Exception as e:
            logger.error("POP3メール取得中にエラーが発生しました: %s", e, exc_info=True)

        return articles
