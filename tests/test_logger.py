"""Tests for logger.py — third-party URL logging is kept below INFO."""

from __future__ import annotations

import logging

import pytest

import logger as logger_module
from config import AppConfig, LLMConfig, LoggingConfig


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved = {name: logging.getLogger(name).level for name in logger_module._URL_LOGGING_LIBRARIES}
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
def test_url_logging_libraries_stay_at_warning(monkeypatch, restore_logging, level):
    """httpx は INFO でリクエスト URL（Discord の webhook を含む）を出すので、出させない。"""
    cfg = AppConfig(llm=LLMConfig(model="test-model"), logging=LoggingConfig(level=level))
    monkeypatch.setattr(logger_module, "config", cfg)

    logger_module.setup_logging(debug=True)

    assert logging.getLogger().level == getattr(logging, level)
    for name in logger_module._URL_LOGGING_LIBRARIES:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING
        assert not logging.getLogger(name).isEnabledFor(logging.INFO)


def test_url_logging_libraries_follow_stricter_level(monkeypatch, restore_logging):
    cfg = AppConfig(llm=LLMConfig(model="test-model"), logging=LoggingConfig(level="ERROR"))
    monkeypatch.setattr(logger_module, "config", cfg)

    logger_module.setup_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.ERROR
