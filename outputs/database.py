import sqlite3
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

from config import config
from models import Article, ArticleSummary
from logger import get_logger

logger = get_logger(__name__)

class Database:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = config.database.path
            
        self.db_path = Path(db_path)
        # 必要なディレクトリの作成
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(
            self.db_path,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        schema = """
        -- 実行バッチの管理
        CREATE TABLE IF NOT EXISTS batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            total_articles INTEGER NOT NULL,
            digest_text TEXT
        );

        -- 個別記事の要約
        CREATE TABLE IF NOT EXISTS article_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES batches(id),
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            original_title TEXT NOT NULL,
            original_url TEXT,
            summary_title TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            keywords TEXT NOT NULL,
            category TEXT NOT NULL,
            group_id INTEGER,
            group_topic TEXT,
            published_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- 処理済みメールIDの管理
        CREATE TABLE IF NOT EXISTS processed_emails (
            uidl TEXT PRIMARY KEY,
            processed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_batch ON article_summaries(batch_id);
        CREATE INDEX IF NOT EXISTS idx_summaries_category ON article_summaries(category);
        CREATE INDEX IF NOT EXISTS idx_summaries_created ON article_summaries(created_at);
        """
        with self.get_connection() as conn:
            conn.executescript(schema)
            # embedding カラムのマイグレーション（既存DBへの後方互換追加）
            cols = {row[1] for row in conn.execute("PRAGMA table_info(article_summaries)")}
            if "embedding" not in cols:
                conn.execute("ALTER TABLE article_summaries ADD COLUMN embedding TEXT")
            conn.commit()
        logger.debug("データベースを初期化しました: %s", self.db_path)

    def create_batch(self, total_articles: int, digest_text: Optional[str] = None) -> int:
        """
        実行バッチを生成し、そのIDを返す。
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO batches (total_articles, digest_text) VALUES (?, ?)",
                (total_articles, digest_text)
            )
            conn.commit()
            batch_id = cursor.lastrowid
            logger.debug("バッチを作成しました (ID: %d, 記事数: %d)", batch_id, total_articles)
            return batch_id

    def update_batch_digest(self, batch_id: int, digest_text: str):
        """
        バッチにダイジェスト結果を更新する。
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE batches SET digest_text = ? WHERE id = ?",
                (digest_text, batch_id)
            )
            conn.commit()

    def save_summary(
        self,
        batch_id: int,
        article: Article,
        summary: ArticleSummary,
        group_id: Optional[int] = None,
        group_topic: Optional[str] = None,
        embedding: Optional[list] = None,
    ):
        """
        個別記事の要約結果をDBに保存する。
        embedding は float のリストを JSON 文字列にシリアライズして保存する。
        """
        # SQLite側で確実に解釈できるようにisoformatの文字列に変換
        published_val = article.published_at.isoformat() if hasattr(article.published_at, "isoformat") else article.published_at
        embedding_val = json.dumps(embedding) if embedding is not None else None

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO article_summaries (
                    batch_id, source_type, source_id, original_title, original_url,
                    summary_title, summary_text, keywords, category,
                    group_id, group_topic, published_at, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    batch_id,
                    article.source_type,
                    article.source_id,
                    article.title,
                    article.url,
                    summary.title,
                    summary.summary,
                    json.dumps(summary.keywords, ensure_ascii=False),
                    summary.category,
                    group_id,
                    group_topic,
                    published_val,
                    embedding_val,
                )
            )
            conn.commit()
            logger.debug("記事要約を保存しました (batch_id: %d, source_id: %s)", batch_id, article.source_id)

    def is_email_processed(self, uidl: str) -> bool:
        """
        指定したメールUIDLがすでに処理済みかどうかを判定する。
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM processed_emails WHERE uidl = ?", (uidl,))
            return cursor.fetchone() is not None

    def mark_email_processed(self, uidl: str):
        """
        指定したメールUIDLを処理済みとしてマークする。
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO processed_emails (uidl) VALUES (?)",
                (uidl,)
            )
            conn.commit()
            logger.debug("メールを処理済みとしてマークしました (uidl: %s)", uidl)
