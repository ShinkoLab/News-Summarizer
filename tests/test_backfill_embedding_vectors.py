from __future__ import annotations

from unittest.mock import MagicMock, patch

from google.cloud.firestore_v1.vector import Vector

from scripts.backfill_embedding_vectors import backfill


class _FakeDoc:
    def __init__(self, doc_id: str, embedding):
        self.id = doc_id
        self._data = {"summary_title": "題", "embedding": embedding}
        self.exists = True
        self.reference = MagicMock(id=doc_id)

    def to_dict(self):
        return dict(self._data)


class _FakeCollection:
    """order_by("__name__").limit(n).start_after(snapshot) だけを実装する最小の偽物。"""

    def __init__(self, docs: list[_FakeDoc]):
        self._docs = sorted(docs, key=lambda d: d.id)
        self._limit = len(docs)
        self._after: _FakeDoc | None = None

    def order_by(self, _field):
        return self

    def limit(self, n):
        clone = _FakeCollection(self._docs)
        clone._limit = n
        clone._after = self._after
        return clone

    def start_after(self, snapshot):
        clone = _FakeCollection(self._docs)
        clone._limit = self._limit
        clone._after = snapshot
        return clone

    def stream(self):
        docs = self._docs
        if self._after is not None:
            docs = [d for d in docs if d.id > self._after.id]
        return iter(docs[: self._limit])

    def document(self, doc_id):
        for doc in self._docs:
            if doc.id == doc_id:
                return MagicMock(get=lambda: doc)
        missing = MagicMock()
        missing.get.return_value = MagicMock(exists=False)
        return missing


def _run(docs, **kwargs):
    collection = _FakeCollection(docs)
    write_batches: list[MagicMock] = []

    def make_batch():
        write_batch = MagicMock()
        write_batches.append(write_batch)
        return write_batch

    with patch("scripts.backfill_embedding_vectors.firestore.Client") as client_class:
        client = client_class.return_value
        client.collection.return_value = collection
        client.batch.side_effect = make_batch
        stats = backfill(
            "p",
            "(default)",
            kwargs.get("dry_run", False),
            kwargs.get("batch_size", 20),
            kwargs.get("limit"),
            kwargs.get("start_after"),
        )
    return stats, write_batches


def test_plain_arrays_are_converted_to_vectors():
    stats, batches = _run([_FakeDoc("a", [0.1, 0.2, 0.3])])

    assert stats.converted == 1
    _, payload = batches[0].update.call_args.args
    assert isinstance(payload["embedding"], Vector)
    assert list(payload["embedding"]) == [0.1, 0.2, 0.3]


def test_already_converted_documents_are_skipped():
    """再実行しても二重に書かない（途中で失敗しても安全にやり直せる）。"""
    stats, batches = _run([_FakeDoc("a", Vector([0.1, 0.2]))])

    assert stats.converted == 0
    assert stats.skipped_vector == 1
    assert batches == []


def test_documents_without_an_embedding_are_counted_separately():
    """グルーピング失敗時の記事。ベクトル検索には永久に出ないので件数を出す。"""
    stats, _ = _run([_FakeDoc("a", None)])

    assert stats.skipped_none == 1
    assert stats.converted == 0


def test_unexpected_types_are_skipped_without_writing():
    stats, batches = _run([_FakeDoc("a", "[0.1, 0.2]")])

    assert stats.skipped_other == 1
    assert batches == []


def test_writes_are_split_by_batch_size():
    docs = [_FakeDoc(f"doc-{i:03d}", [0.1, 0.2]) for i in range(5)]

    stats, batches = _run(docs, batch_size=2)

    assert stats.converted == 5
    # 2 + 2 + 1
    assert [b.update.call_count for b in batches] == [2, 2, 1]


def test_dry_run_counts_without_writing():
    stats, batches = _run([_FakeDoc("a", [0.1]), _FakeDoc("b", [0.2])], dry_run=True)

    assert stats.converted == 2
    assert batches == []


def test_limit_stops_early():
    docs = [_FakeDoc(f"doc-{i:03d}", [0.1]) for i in range(10)]

    stats, _ = _run(docs, limit=3)

    assert stats.converted == 3


def test_start_after_resumes_from_the_next_document():
    docs = [_FakeDoc(f"doc-{i:03d}", [0.1]) for i in range(5)]

    stats, _ = _run(docs, start_after="doc-002")

    assert stats.converted == 2  # doc-003, doc-004


def test_a_failed_commit_does_not_stop_the_rest():
    docs = [_FakeDoc(f"doc-{i:03d}", [0.1]) for i in range(4)]
    collection = _FakeCollection(docs)
    write_batches: list[MagicMock] = []

    def make_batch():
        write_batch = MagicMock()
        if not write_batches:
            write_batch.commit.side_effect = RuntimeError("transaction too big")
        write_batches.append(write_batch)
        return write_batch

    with patch("scripts.backfill_embedding_vectors.firestore.Client") as client_class:
        client = client_class.return_value
        client.collection.return_value = collection
        client.batch.side_effect = make_batch
        stats = backfill("p", "(default)", False, 2, None, None)

    assert stats.failed == 2
    assert stats.converted == 2


def test_resume_point_does_not_skip_past_a_failed_commit():
    """再開点は「見た最後のID」ではなく「確実に処理済みのID」でなければならない。

    見た最後のIDを案内すると、失敗したドキュメントがその手前に取り残され、
    --start-after で再開したときに恒久的に飛ばされてベクトル検索へ永久に出ない。
    """
    docs = [_FakeDoc(f"doc-{i:03d}", [0.1]) for i in range(6)]
    collection = _FakeCollection(docs)
    write_batches = []

    def make_batch():
        write_batch = MagicMock()
        # 2コミット目（doc-002, doc-003）だけ失敗させる
        if len(write_batches) == 1:
            write_batch.commit.side_effect = RuntimeError("transaction too big")
        write_batches.append(write_batch)
        return write_batch

    with patch("scripts.backfill_embedding_vectors.firestore.Client") as client_class:
        client = client_class.return_value
        client.collection.return_value = collection
        client.batch.side_effect = make_batch
        stats = backfill("p", "(default)", False, 2, None, None)

    assert stats.failed == 2
    assert stats.had_failure is True
    # doc-001 までは確実にコミット済み。失敗した doc-002/003 を飛び越えてはいけない。
    assert stats.resume_id == "doc-001"


def test_resume_point_survives_limit_truncation():
    """--limit で打ち切っても再開点を失わない。"""
    docs = [_FakeDoc(f"doc-{i:03d}", [0.1]) for i in range(10)]

    stats, _ = _run(docs, limit=3, batch_size=20)

    assert stats.converted == 3
    assert stats.resume_id == "doc-002"


def test_resume_point_advances_over_skipped_documents():
    """変換不要なドキュメントだけでも再開点は前進する（再実行が先頭に戻らない）。"""
    docs = [_FakeDoc(f"doc-{i:03d}", Vector([0.1])) for i in range(4)]

    stats, batches = _run(docs)

    assert stats.skipped_vector == 4
    assert batches == []
    assert stats.resume_id == "doc-003"
