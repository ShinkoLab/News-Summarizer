#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from google.cloud import firestore

from outputs.firestore_database import make_document_id
from urls import normalize_url, url_key


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def parse_json_list(value: str | None):
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("JSON配列ではない値が含まれています。")
    return parsed


def migrate(database_path: Path, project_id: str, database_id: str, dry_run: bool) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row

    batches = connection.execute("SELECT * FROM batches ORDER BY id").fetchall()
    articles = connection.execute(
        "SELECT * FROM article_summaries ORDER BY batch_id, id"
    ).fetchall()
    processed_emails = connection.execute(
        "SELECT * FROM processed_emails ORDER BY processed_at"
    ).fetchall()

    source_keys = [(row["source_type"], row["source_id"]) for row in articles]
    if len(source_keys) != len(set(source_keys)):
        raise ValueError(
            "同じ source_type/source_id の記事が複数あります。移行前に重複を解消してください。"
        )

    print(
        f"batches={len(batches)}, articles={len(articles)}, "
        f"processed_emails={len(processed_emails)}, dry_run={dry_run}"
    )
    if dry_run:
        return

    client = firestore.Client(project=project_id, database=database_id)

    articles_by_batch: dict[int, list[sqlite3.Row]] = {}
    for row in articles:
        articles_by_batch.setdefault(row["batch_id"], []).append(row)

    for batch_row in batches:
        batch_id = int(batch_row["id"])
        write_batch = client.batch()
        batch_ref = client.collection("batches").document(str(batch_id))
        write_batch.set(
            batch_ref,
            {
                "id": batch_id,
                "executed_at": parse_datetime(batch_row["executed_at"]),
                "total_articles": int(batch_row["total_articles"]),
                "digest_text": batch_row["digest_text"],
                "status": "complete",
            },
        )

        rows = articles_by_batch.get(batch_id, [])
        # 1記事あたり articleSummaries と articleUrls の最大2書き込み。
        # バッチドキュメント1件と合わせて Firestore の 500 writes/commit に収める。
        if len(rows) > 200:
            raise ValueError(f"batch_id={batch_id} の記事数が移行上限200件を超えています。")
        for row in rows:
            article_ref = client.collection("articleSummaries").document(
                make_document_id(row["source_type"], row["source_id"])
            )
            columns = row.keys()
            feed_title = row["feed_title"] if "feed_title" in columns else None
            # 移行元に url_key が無い世代のDBでも、original_url から作り直せる。
            key = row["url_key"] if "url_key" in columns else None
            key = key or url_key(row["original_url"])
            # set() rather than create(): a migration that dies partway must be
            # re-runnable, and the document id is already content-derived.
            write_batch.set(
                article_ref,
                {
                    # Must match what FirestoreDatabase.save_batch writes at runtime
                    # (the document id), not the SQLite rowid.
                    "id": article_ref.id,
                    "batch_id": batch_id,
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "original_title": row["original_title"],
                    "original_url": row["original_url"],
                    "summary_title": row["summary_title"],
                    "summary_text": row["summary_text"],
                    "keywords": parse_json_list(row["keywords"]) or [],
                    "category": row["category"],
                    "group_id": row["group_id"],
                    "group_topic": row["group_topic"],
                    "published_at": parse_datetime(row["published_at"]),
                    "created_at": parse_datetime(row["created_at"]),
                    "embedding": parse_json_list(row["embedding"]),
                    "url_key": key,
                    "feed_title": feed_title,
                },
            )
            if key:
                # 移行後の実行が同一URLの重複を検出できるよう、判定用の索引も作る。
                write_batch.set(
                    client.collection("articleUrls").document(key),
                    {
                        "url": normalize_url(row["original_url"]),
                        "source_type": row["source_type"],
                        "source_id": row["source_id"],
                        "created_at": parse_datetime(row["created_at"]),
                    },
                )
        write_batch.commit()
        print(f"migrated batch {batch_id}: {len(rows)} articles")

    for start in range(0, len(processed_emails), 400):
        write_batch = client.batch()
        for row in processed_emails[start : start + 400]:
            ref = client.collection("processedEmails").document(
                make_document_id(row["uidl"])
            )
            write_batch.set(
                ref,
                {
                    "uidl": row["uidl"],
                    "processed_at": parse_datetime(row["processed_at"]),
                },
            )
        write_batch.commit()

    print("Migration completed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="SQLiteの履歴をFirestoreへ移行します。")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--firestore-database", default="(default)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.database.is_file():
        parser.error(f"SQLiteファイルが見つかりません: {args.database}")
    migrate(args.database, args.project, args.firestore_database, args.dry_run)


if __name__ == "__main__":
    main()
