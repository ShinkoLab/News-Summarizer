"""Dry runs must not create or migrate the source SQLite database."""
import sqlite3
from unittest.mock import patch

import pytest

from outputs.database import Database, create_database
from pipeline import RunOptions, run_pipeline


def test_missing_database_does_not_create_parent(tmp_path):
    path = tmp_path / 'missing' / 'news.db'
    db = Database(str(path), read_only=True)
    assert not db.is_article_processed('rss', '1')
    assert not db.is_email_processed('mail')
    assert not db.is_url_processed('url')
    assert not path.parent.exists()
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        db.mark_email_processed('mail')


def test_old_schema_is_migrated_only_in_memory(tmp_path):
    path = tmp_path / 'legacy.db'
    db = Database(str(path))
    db.mark_email_processed('saved')
    with sqlite3.connect(path) as conn:
        conn.execute('DROP INDEX idx_summaries_url_key')
        conn.execute('ALTER TABLE article_summaries DROP COLUMN url_key')
        conn.execute('ALTER TABLE article_summaries DROP COLUMN embedding')
    before = path.read_bytes()
    readonly = Database(str(path), read_only=True)
    assert readonly.is_email_processed('saved')
    assert not readonly.is_url_processed('unknown')
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert 'url_key' not in {r[1] for r in conn.execute('PRAGMA table_info(article_summaries)')}
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        readonly.record_email_attempt('new')


def test_snapshot_reads_committed_wal_data(tmp_path):
    path = tmp_path / 'wal.db'
    Database(str(path))
    writer = sqlite3.connect(path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute("INSERT INTO processed_emails (uidl) VALUES ('in-wal')")
        writer.commit()
        assert Database(str(path), read_only=True).is_email_processed('in-wal')
    finally:
        writer.close()


@pytest.mark.parametrize('options,read_only', [
    (RunOptions(dry_run=True), True),
    (RunOptions(dry_run=True, forced_outputs=frozenset({'discord'})), True),
    (RunOptions(dry_run=True, forced_outputs=frozenset({'db'})), False),
    (RunOptions(dry_run=True, forced_outputs=frozenset({'all'})), False),
    (RunOptions(), False),
])
def test_pipeline_selects_read_only_only_without_db_output(minimal_app_config, options, read_only):
    class StopAfterSelection(Exception):
        pass
    with patch('pipeline.create_database', side_effect=StopAfterSelection) as factory:
        with pytest.raises(StopAfterSelection):
            run_pipeline(minimal_app_config, options)
    factory.assert_called_once_with(read_only=read_only)


def test_factory_forwards_sqlite_mode(minimal_app_config):
    with patch('outputs.database.config', minimal_app_config), patch('outputs.database.Database') as db:
        create_database(read_only=True)
    db.assert_called_once_with(read_only=True)


def test_outbox_migration_never_changes_legacy_source(tmp_path):
    path = tmp_path / "no-outbox.db"
    Database(str(path))
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE discord_outbox")
    before = path.read_bytes()
    readonly = Database(str(path), read_only=True)
    assert readonly.get_connection().execute("SELECT count(*) FROM discord_outbox").fetchone()[0] == 0
    assert path.read_bytes() == before
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='discord_outbox'").fetchone() is None
