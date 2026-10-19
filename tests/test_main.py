"""Tests for the CLI entry point's exit status."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

import main
from pipeline import RunFailedError


def _run_main(monkeypatch, **patch_kwargs):
    monkeypatch.setattr(sys, "argv", ["main.py", "--dry-run"])
    with patch("main.run_pipeline", **patch_kwargs) as mock_run:
        main.main()
    return mock_run


def test_no_summaries_exits_with_status_1(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _run_main(monkeypatch, side_effect=RunFailedError("0件"))

    assert excinfo.value.code == 1


def test_successful_run_exits_normally(monkeypatch):
    mock_run = _run_main(monkeypatch)

    mock_run.assert_called_once()
