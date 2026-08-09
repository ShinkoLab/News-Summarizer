import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from config import config
from models import Article, ArticleSummary, SaveResult
from logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from models import DigestResult

class Database:
    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = config.database.path
            
        self.db_path = Path(db_path)
        # 必要なディレクトリの作成
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
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

        -- 取得したが保存に至らなかったメールの試行回数。processed_emails とは
        -- 別に持つ（同じテーブルに混ぜると is_email_processed() が試行中の
        -- メールを処理済みと誤判定する）。
        CREATE TABLE IF NOT EXISTS email_attempts (
            uidl TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_summaries_batch ON article_summaries(batch_id);
        CREATE INDEX IF NOT EXISTS idx_summaries_category ON article_summaries(category);
        CREATE INDEX IF NOT EXISTS idx_summaries_created ON article_summaries(created_at);
        -- is_article_processed() is called once per fetched article on every run.
        CREATE INDEX IF NOT EXISTS idx_summaries_source
            ON article_summaries(source_type, source_id);
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

    def is_article_processed(self, source_type: str, source_id: str) -> bool:
        """Return whether an article has already been persisted."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM article_summaries WHERE source_type = ? AND source_id = ? LIMIT 1",
                (source_type, source_id),
            )
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

    def record_email_attempt(self, uidl: str) -> int:
        """
        指定したメールUIDLの試行回数を1つ増やし、増加後の回数を返す。
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO email_attempts (uidl, attempts) VALUES (?, 1)
                ON CONFLICT(uidl) DO UPDATE SET
                    attempts = attempts + 1,
                    last_attempt_at = CURRENT_TIMESTAMP
                """,
                (uidl,),
            )
            conn.commit()
            cursor.execute("SELECT attempts FROM email_attempts WHERE uidl = ?", (uidl,))
            row = cursor.fetchone()
            return int(row[0]) if row else 1

    def save_batch(
        self,
        summaries: list[tuple[Article, ArticleSummary, int | None, str | None]],
        digest: "DigestResult",
        embeddings=None,
    ) -> SaveResult:
        """Persist a completed pipeline result in a single SQLite transaction.

        SQLite にはトランザクションサイズの制限が無いため、Firestore と違って
        分割はしない（`SaveResult` の形だけ合わせる）。
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO batches (total_articles, digest_text) VALUES (?, ?)",
                (len(summaries), digest.overview),
            )
            batch_id = cursor.lastrowid

            for i, (article, summary, group_id, group_topic) in enumerate(summaries):
                published_val = (
                    article.published_at.isoformat()
                    if hasattr(article.published_at, "isoformat")
                    else article.published_at
                )
                embedding = embeddings[i].tolist() if embeddings is not None else None
                cursor.execute(
                    """
                    INSERT INTO article_summaries (
                        batch_id, source_type, source_id, original_title, original_url,
                        summary_title, summary_text, keywords, category,
                        group_id, group_topic, published_at, embedding
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
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
                        json.dumps(embedding) if embedding is not None else None,
                    ),
                )
                if article.source_type == "email":
                    cursor.execute(
                        "INSERT OR IGNORE INTO processed_emails (uidl) VALUES (?)",
                        (article.source_id,),
                    )
                    # 保存できた以上、poison message 判定用の試行回数はもう不要。
                    cursor.execute(
                        "DELETE FROM email_attempts WHERE uidl = ?",
                        (article.source_id,),
                    )

            conn.commit()
            logger.debug("バッチを一括保存しました (batch_id: %d)", batch_id)
            return SaveResult(
                batch_id=int(batch_id),
                saved=[(article.source_type, article.source_id) for article, *_ in summaries],
            )

    @contextmanager
    def execution_lock(self):
        """SQLite runs remain single-process; keep a common backend interface."""
        yield


def create_database():
    """Create the configured persistence backend."""
    if config.database.backend == "firestore":
        from outputs.firestore_database import FirestoreDatabase

        return FirestoreDatabase(
            project_id=config.database.project_id,
            database=config.database.firestore_database,
        )
    return Database()
