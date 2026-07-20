#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from google.cloud import firestore

from outputs.firestore_database import make_document_id


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
        if len(rows) > 400:
            raise ValueError(f"batch_id={batch_id} の記事数が移行上限400件を超えています。")
        for row in rows:
            article_ref = client.collection("articleSummaries").document(
                make_document_id(row["source_type"], row["source_id"])
            )
            write_batch.create(
                article_ref,
                {
                    "id": str(row["id"]),
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
