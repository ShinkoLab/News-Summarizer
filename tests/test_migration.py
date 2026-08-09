from __future__ import annotations

from pathlib import Path

from models import DigestResult
from outputs.database import Database
from scripts.migrate_sqlite_to_firestore import migrate
from tests.test_database import _make_article, _make_summary


def test_migration_dry_run_validates_and_reports_counts(tmp_path, capsys):
    database_path = tmp_path / "history.db"
    database = Database(str(database_path))
    database.save_batch(
        [(_make_article(), _make_summary(), None, None)],
        DigestResult(overview="概要", categories=[], total_articles=1),
    )

    migrate(Path(database_path), "unused-project", "(default)", dry_run=True)

    output = capsys.readouterr().out
    assert "batches=1" in output
    assert "articles=1" in output
    assert "dry_run=True" in output
