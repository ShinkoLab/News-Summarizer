from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from google.cloud import firestore

from logger import get_logger
from models import Article, ArticleSummary, DigestResult, SaveResult

logger = get_logger(__name__)

# 1コミットあたりの上限。Firestore の制限は 500 writes / 10 MiB。
#
# サイズ上限を制限ぎりぎりに置かないのには2つ理由がある。1つは estimate_document_size()
# が概算で、実測（1件 17.6 KiB）に対して3割ほど小さく出ること。もう1つは、
# 上限に寄せるほど1コミットあたりの件数が増え、失敗したときに失う記事が増えること。
# 1 MiB なら実サイズでも 1.5 MiB 程度に収まり、記事1件 17.6 KiB として
# 1コミット約60件。max_articles_per_run の上限200件でも3〜4コミットに分かれるため、
# 1コミット失敗しても残りは保存される。
_MAX_BATCH_WRITES = 450
_MAX_CHUNK_BYTES = 1024 * 1024


def make_document_id(*parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def estimate_document_size(value) -> int:
    """Firestore ドキュメントのおおよそのバイト数を見積もる。

    正確なサイズ計算（フィールド名・型ごとのオーバーヘッド）は目的ではない。
    1件 17.6 KiB のうち大半を占めるのは 1536次元の `embedding` なので、
    コミットを区切る判断にはこの程度の概算で足りる。
    """
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, str):
        return len(value.encode("utf-8")) + 1
    if isinstance(value, (int, float, datetime)):
        return 8
    if isinstance(value, dict):
        return sum(
            len(str(key).encode("utf-8")) + 1 + estimate_document_size(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return sum(estimate_document_size(item) for item in value)
    return len(str(value).encode("utf-8")) + 1


class FirestoreDatabase:
    """Firestore persistence used by the Cloud Run job."""

    def __init__(self, project_id: str | None = None, database: str = "(default)"):
        self.client = firestore.Client(project=project_id, database=database)

    def is_email_processed(self, uidl: str) -> bool:
        ref = self.client.collection("processedEmails").document(make_document_id(uidl))
        return ref.get().exists

    def is_article_processed(self, source_type: str, source_id: str) -> bool:
        ref = self.client.collection("articleSummaries").document(
            make_document_id(source_type, source_id)
        )
        return ref.get().exists

    def mark_email_processed(self, uidl: str) -> None:
        """UIDLを処理済みとしてマークし、以後のPOP3取得対象から外す。"""
        ref = self.client.collection("processedEmails").document(make_document_id(uidl))
        ref.set({"uidl": uidl, "processed_at": datetime.now(UTC)})
        logger.debug("メールを処理済みとしてマークしました (uidl: %s)", uidl)

    def record_email_attempt(self, uidl: str) -> int:
        """UIDLの試行回数を1つ増やし、増加後の回数を返す。"""
        ref = self.client.collection("emailAttempts").document(make_document_id(uidl))
        now = datetime.now(UTC)
        transaction = self.client.transaction()

        @firestore.transactional
        def increment(txn) -> int:
            snapshot = ref.get(transaction=txn)
            current = 0
            if snapshot.exists:
                current = int((snapshot.to_dict() or {}).get("attempts", 0))
            attempts = current + 1
            txn.set(ref, {"uidl": uidl, "attempts": attempts, "last_attempt_at": now})
            return attempts

        return increment(transaction)

    def _article_writes(
        self,
        article: Article,
        summary: ArticleSummary,
        group_id: int | None,
        group_topic: str | None,
        embedding: list | None,
        batch_id: int,
        now: datetime,
    ) -> list[tuple[str, object, dict | None]]:
        """1記事ぶんの書き込み操作を組み立てる。同じチャンクで一緒にコミットする。"""
        summary_ref = self.client.collection("articleSummaries").document(
            make_document_id(article.source_type, article.source_id)
        )
        writes: list[tuple[str, object, dict | None]] = [
            (
                "create",
                summary_ref,
                {
                    "id": summary_ref.id,
                    "batch_id": batch_id,
                    "source_type": article.source_type,
                    "source_id": article.source_id,
                    "original_title": article.title,
                    "original_url": article.url,
                    "summary_title": summary.title,
                    "summary_text": summary.summary,
                    "keywords": summary.keywords,
                    "category": summary.category,
                    "group_id": group_id,
                    "group_topic": group_topic,
                    "published_at": article.published_at,
                    "created_at": now,
                    "embedding": embedding,
                },
            )
        ]
        if article.source_type == "email":
            email_ref = self.client.collection("processedEmails").document(
                make_document_id(article.source_id)
            )
            writes.append(
                ("set", email_ref, {"uidl": article.source_id, "processed_at": now})
            )
            # 保存できた以上、poison message 判定用の試行回数はもう不要。
            attempts_ref = self.client.collection("emailAttempts").document(
                make_document_id(article.source_id)
            )
            writes.append(("delete", attempts_ref, None))
        return writes

    def save_batch(
        self,
        summaries: list[tuple[Article, ArticleSummary, int | None, str | None]],
        digest: DigestResult,
        embeddings=None,
    ) -> SaveResult:
        """記事サマリを複数コミットに分割して保存する。

        全件を1トランザクションでコミットすると、失敗時に要約・グルーピング・
        ダイジェスト生成のLLM呼び出しが丸ごと無駄になる。件数とサイズの両方で
        区切り、途中で失敗しても成功した分は残す。

        バッチドキュメントは**最後に**コミットする。これで「記事はあるのに
        バッチが無い」状態（Viewer から永久に見えなくなる）は、記事チャンクが
        成功したうえで最後の1書き込みだけが失敗したときに限定される。
        その場合は復旧できるよう batch_id をログに残す。
        """
        now = datetime.now(UTC)
        batch_id = int(now.timestamp() * 1000)

        saved: list[tuple[str, str]] = []
        failed = 0

        chunk: list[tuple[str, object, dict | None]] = []
        chunk_keys: list[tuple[str, str]] = []
        chunk_bytes = 0

        def flush() -> None:
            nonlocal chunk, chunk_keys, chunk_bytes, failed
            if not chunk:
                return
            write_batch = self.client.batch()
            for op, ref, data in chunk:
                if op == "create":
                    write_batch.create(ref, data)
                elif op == "set":
                    write_batch.set(ref, data)
                else:
                    write_batch.delete(ref)
            try:
                write_batch.commit()
            except Exception as e:
                failed += len(chunk_keys)
                logger.error(
                    "Firestoreへの分割コミットに失敗しました (%d件): %s",
                    len(chunk_keys),
                    e,
                    exc_info=True,
                )
            else:
                saved.extend(chunk_keys)
            chunk = []
            chunk_keys = []
            chunk_bytes = 0

        for i, (article, summary, group_id, group_topic) in enumerate(summaries):
            embedding = embeddings[i].tolist() if embeddings is not None else None
            writes = self._article_writes(
                article, summary, group_id, group_topic, embedding, batch_id, now
            )
            size = sum(estimate_document_size(data) for _, _, data in writes)

            # 1記事ぶんの書き込み（サマリ＋email関連）は分割せず同じコミットに収める。
            # チャンクが空なら、単体で上限を超える記事でもそのまま送るしかない。
            if chunk and (
                len(chunk) + len(writes) > _MAX_BATCH_WRITES
                or chunk_bytes + size > _MAX_CHUNK_BYTES
            ):
                flush()

            chunk.extend(writes)
            chunk_keys.append((article.source_type, article.source_id))
            chunk_bytes += size

        flush()

        if not saved:
            logger.error(
                "記事を1件も保存できませんでした。バッチドキュメントは作成しません "
                "(batch_id候補: %d, 失敗: %d件)",
                batch_id,
                failed,
            )
            return SaveResult(batch_id=None, saved=[], failed=failed)

        batch_ref = self.client.collection("batches").document(str(batch_id))
        try:
            batch_ref.create(
                {
                    "id": batch_id,
                    "executed_at": now,
                    "total_articles": len(saved),
                    "digest_text": digest.overview,
                    "status": "partial" if failed else "complete",
                }
            )
        except Exception as e:
            # 記事は保存済みなのにバッチドキュメントだけが無い状態。Viewer は
            # バッチ単位で記事を引くため、この記事群は表示されない。手動復旧できるよう
            # batch_id を必ずログに残す。
            logger.error(
                "バッチドキュメントの作成に失敗しました。articleSummaries の "
                "batch_id=%d は参照先の無い状態です: %s",
                batch_id,
                e,
                exc_info=True,
            )
            return SaveResult(batch_id=None, saved=saved, failed=failed)

        if failed:
            logger.warning(
                "Firestoreへバッチを保存しました（一部失敗） "
                "(batch_id: %d, 保存: %d件, 失敗: %d件)",
                batch_id,
                len(saved),
                failed,
            )
        else:
            logger.info(
                "Firestoreへバッチを保存しました (batch_id: %d, %d件)",
                batch_id,
                len(saved),
            )
        return SaveResult(batch_id=batch_id, saved=saved, failed=failed)

    @contextmanager
    def execution_lock(self, ttl: timedelta = timedelta(hours=2)):
        """Prevent overlapping Cloud Scheduler executions using a lease document."""
        lock_ref = self.client.collection("pipelineLocks").document("current")
        owner = uuid.uuid4().hex
        now = datetime.now(UTC)
        transaction = self.client.transaction()

        @firestore.transactional
        def acquire(txn):
            snapshot = lock_ref.get(transaction=txn)
            if snapshot.exists:
                # DocumentSnapshot.get() raises KeyError on a missing field, which
                # would wedge every future run on a half-written lock document.
                existing = snapshot.to_dict() or {}
                expires_at = existing.get("expires_at")
                if expires_at and expires_at > now:
                    raise RuntimeError(
                        "別のパイプライン実行が進行中です "
                        f"(owner={existing.get('owner')}, expires_at={expires_at})。"
                        " 前回の実行が強制終了した場合は Firestore の "
                        "pipelineLocks/current を削除してください。"
                    )
            txn.set(
                lock_ref,
                {"owner": owner, "acquired_at": now, "expires_at": now + ttl},
            )

        acquire(transaction)
        try:
            yield
        finally:
            release_transaction = self.client.transaction()

            @firestore.transactional
            def release(txn):
                snapshot = lock_ref.get(transaction=txn)
                if snapshot.exists and (snapshot.to_dict() or {}).get("owner") == owner:
                    txn.delete(lock_ref)

            release(release_transaction)
