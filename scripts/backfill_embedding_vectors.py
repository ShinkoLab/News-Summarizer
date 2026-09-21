#!/usr/bin/env python3
"""articleSummaries.embedding を素の配列から Firestore の Vector 型へ変換する。

Firestore のベクトル検索（findNearest）は VectorValue 型のフィールドしか対象に
しない。素の array<double> で保存されたドキュメントは**エラーにならずに黙って
検索結果から除外される**ため、Viewer のセマンティック検索を入れる前に一度だけ
全件を変換しておく必要がある。

再 embedding は行わない。保存済みの数値をそのまま Vector で包み直すだけなので、
embedding API の呼び出しもコストも発生しない。

何度実行しても安全（変換済みのドキュメントはスキップする）。

使い方:
    uv run python scripts/backfill_embedding_vectors.py --project <PROJECT> --dry-run
    uv run python scripts/backfill_embedding_vectors.py --project <PROJECT> --limit 20
    uv run python scripts/backfill_embedding_vectors.py --project <PROJECT>
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector

# 1コミットあたりの件数。save_batch の _MAX_BATCH_WRITES（450）より大幅に小さい。
# 1書き込みが 1536要素のベクトルを丸ごと運ぶうえ、過去に 80件の一括コミットが
# インデックス書き込みぶんで「Transaction too big」（上限10MiB）を踏んでいる
# （infra/DEPLOYMENT.md 事例9）。20件なら実サイズ約240KiBで、仮に配列要素の
# 自動インデックスが復活していても上限には遠い。
_DEFAULT_BATCH_SIZE = 20

# 1回のクエリで取得するドキュメント数。読み取りは書き込みより軽いので大きめ。
_PAGE_SIZE = 200


def log(message: str) -> None:
    """必ずフラッシュして出力する。

    print() はパイプやファイルへ向けるとブロックバッファリングされる。
    8000件規模で数分かかる処理なので、流し込み先が端末でないと
    進捗が一切見えないまま終了まで待つことになる。
    """
    print(message, flush=True)


@dataclass
class Stats:
    converted: int = 0
    skipped_vector: int = 0
    skipped_none: int = 0
    skipped_other: int = 0
    failed: int = 0
    #「ここまでは確実に処理済み」と言えるドキュメントID。--start-after に渡せる。
    resume_id: str | None = None
    had_failure: bool = False

    def report(self) -> str:
        return (
            f"変換={self.converted} / 変換済みのためスキップ={self.skipped_vector} / "
            f"embeddingなし={self.skipped_none} / 不明な型={self.skipped_other} / "
            f"失敗={self.failed}"
        )


def _pages(collection, page_size: int, start_after_id: str | None):
    """ドキュメントIDの昇順でページングする。

    __name__ の順序なら索引を追加せずに済み、最後に処理したIDを --start-after に
    渡すだけで途中から再開できる（失敗しても最初からやり直さなくてよい）。

    カーソルにはスナップショットをそのまま渡す。start_after に dict を渡す形だと
    __name__ の値が DocumentReference である必要があり、IDの文字列では通らない。
    """
    cursor = None
    if start_after_id is not None:
        cursor = collection.document(start_after_id).get()
        if not cursor.exists:
            raise SystemExit(f"--start-after のドキュメントが見つかりません: {start_after_id}")

    while True:
        query = collection.order_by("__name__").limit(page_size)
        if cursor is not None:
            query = query.start_after(cursor)
        docs = list(query.stream())
        if not docs:
            return
        yield docs
        cursor = docs[-1]


def backfill(
    project_id: str,
    database_id: str,
    dry_run: bool,
    batch_size: int,
    limit: int | None,
    start_after: str | None,
) -> Stats:
    client = firestore.Client(project=project_id, database=database_id)
    collection = client.collection("articleSummaries")
    stats = Stats()
    started = time.perf_counter()
    seen = 0
    pending: list[tuple[object, list[float]]] = []

    # 再開点。「見た最後のID」ではなく「ここまでは確実に処理済み」と言えるIDを持つ。
    # 見た最後のIDを案内すると、コミットに失敗したドキュメントがその手前に残り、
    # --start-after で再開したときに恒久的に飛ばされる（＝ベクトル検索に永久に出ない）。
    # 一度でも失敗したら、そこで前進を止める。
    last_safe_id: str | None = start_after
    last_processed_id: str | None = None
    had_failure = False

    def flush(covered_id: str | None) -> None:
        """pending をコミットする。covered_id はこのコミットが到達したドキュメントID。"""
        nonlocal pending, last_safe_id, had_failure
        if not pending:
            return
        write_batch = client.batch()
        for ref, values in pending:
            write_batch.update(ref, {"embedding": Vector(values)})
        try:
            write_batch.commit()
        except Exception as e:  # noqa: BLE001 - 途中で止めず、残りを処理する
            stats.failed += len(pending)
            ids = ", ".join(ref.id for ref, _ in pending)
            log(f"  コミット失敗 ({len(pending)}件): {e}")
            log(f"  失敗したドキュメント: {ids}")
            had_failure = True
        else:
            stats.converted += len(pending)
            if not had_failure:
                last_safe_id = covered_id
        pending = []

    def mark_safe(doc_id: str) -> None:
        """未コミットの書き込みが無く、まだ失敗もしていなければ再開点を進める。"""
        nonlocal last_safe_id
        if not pending and not had_failure:
            last_safe_id = doc_id

    for docs in _pages(collection, _PAGE_SIZE, start_after):
        for doc in docs:
            if limit is not None and seen >= limit:
                break
            seen += 1
            last_processed_id = doc.id

            # doc.get("embedding") はフィールドが無いと KeyError を投げる。
            data = doc.to_dict() or {}
            value = data.get("embedding")

            if value is None:
                # グルーピングに失敗した実行の記事。ベクトル検索には永久に出ない。
                stats.skipped_none += 1
                mark_safe(doc.id)
                continue
            if isinstance(value, Vector):
                stats.skipped_vector += 1
                mark_safe(doc.id)
                continue
            if not isinstance(value, (list, tuple)):
                log(f"  {doc.id}: embedding が {type(value).__name__} なのでスキップ")
                stats.skipped_other += 1
                mark_safe(doc.id)
                continue

            if dry_run:
                stats.converted += 1
                mark_safe(doc.id)
                continue

            # set ではなく update。embedding だけを触るので、並行して走っている
            # サマライザの書き込みを踏み潰さない。
            pending.append((doc.reference, [float(v) for v in value]))
            if len(pending) >= batch_size:
                flush(doc.id)

        flush(last_processed_id)
        log(f"  {seen}件処理 ({time.perf_counter() - started:.0f}秒) — {stats.report()}")
        if limit is not None and seen >= limit:
            break

    flush(last_processed_id)

    stats.resume_id = last_safe_id
    stats.had_failure = had_failure

    if last_safe_id is not None:
        log(f"安全に再開できるドキュメントID: {last_safe_id}")
        log(f"  続きから流すには --start-after {last_safe_id}")
    if had_failure:
        log(
            "コミットに失敗したドキュメントがあります。上の「失敗したドキュメント」を"
            "個別に確認するか、--start-after を使わず先頭から流し直してください"
            "（変換済みはスキップされます）。"
        )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="articleSummaries.embedding を Firestore の Vector 型へ変換します。"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--firestore-database", default="(default)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="書き込まずに対象件数だけ数える",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"1コミットあたりの件数（既定 {_DEFAULT_BATCH_SIZE}）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="処理するドキュメント数の上限（動作確認用）",
    )
    parser.add_argument(
        "--start-after",
        default=None,
        help="このドキュメントIDの次から再開する",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size は1以上にしてください。")

    if args.dry_run:
        log("--dry-run: 書き込みは行いません。")

    stats = backfill(
        args.project,
        args.firestore_database,
        args.dry_run,
        args.batch_size,
        args.limit,
        args.start_after,
    )
    log(stats.report())

    if stats.skipped_none:
        log(
            f"embedding を持たない記事が {stats.skipped_none} 件あります。"
            "これらはベクトル検索に出ず、キーワード一致でのみ引けます。"
        )
    if stats.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
