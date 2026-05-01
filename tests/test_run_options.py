"""Tests for RunOptions — derived properties run_db and run_discord."""

from __future__ import annotations

import pytest

from pipeline import RunOptions


class TestRunDb:
    """run_db derives from dry_run and forced_outputs."""

    def test_default_non_dry_run_db_true(self):
        opts = RunOptions(dry_run=False)
        assert opts.run_db is True

    def test_dry_run_db_false(self):
        opts = RunOptions(dry_run=True)
        assert opts.run_db is False

    def test_dry_run_forced_discord_only_db_false(self):
        opts = RunOptions(dry_run=True, forced_outputs=frozenset({"discord"}))
        assert opts.run_db is False

    def test_dry_run_forced_db_db_true(self):
        opts = RunOptions(dry_run=True, forced_outputs=frozenset({"db"}))
        assert opts.run_db is True

    def test_dry_run_forced_all_db_true(self):
        opts = RunOptions(dry_run=True, forced_outputs=frozenset({"all"}))
        assert opts.run_db is True


class TestRunDiscord:
    """run_discord derives from dry_run and forced_outputs."""

    def test_default_non_dry_run_discord_true(self):
        opts = RunOptions(dry_run=False)
        assert opts.run_discord is True

    def test_dry_run_discord_false(self):
        opts = RunOptions(dry_run=True)
        assert opts.run_discord is False

    def test_dry_run_forced_discord_discord_true(self):
        opts = RunOptions(dry_run=True, forced_outputs=frozenset({"discord"}))
        assert opts.run_discord is True

    def test_dry_run_forced_db_only_discord_false(self):
        opts = RunOptions(dry_run=True, forced_outputs=frozenset({"db"}))
        assert opts.run_discord is False

    def test_dry_run_forced_all_discord_true(self):
        opts = RunOptions(dry_run=True, forced_outputs=frozenset({"all"}))
        assert opts.run_discord is True


class TestRunSources:
    """run_rss / run_email derive from sources."""

    def test_default_sources_all_rss_and_email(self):
        opts = RunOptions()
        assert opts.run_rss is True
        assert opts.run_email is True

    def test_sources_rss_only(self):
        opts = RunOptions(sources=frozenset({"rss"}))
        assert opts.run_rss is True
        assert opts.run_email is False

    def test_sources_email_only(self):
        opts = RunOptions(sources=frozenset({"email"}))
        assert opts.run_rss is False
        assert opts.run_email is True

    def test_sources_all_explicit(self):
        opts = RunOptions(sources=frozenset({"all"}))
        assert opts.run_rss is True
        assert opts.run_email is True


class TestRunOptionsImmutable:
    """RunOptions is frozen — mutation should fail."""

    def test_frozen_raises_on_assignment(self):
        opts = RunOptions()
        with pytest.raises((AttributeError, TypeError)):
            opts.dry_run = True  # type: ignore[misc]
