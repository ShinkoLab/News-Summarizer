"""Tests for summarizer/summarizer.py — category validation and retry logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import config as config_module
from config import AppConfig, LLMConfig, SummarizerConfig, DatabaseConfig, DiscordConfig
from models import Article, ArticleSummary
from datetime import datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(
    categories: list[str] | None = None,
    fallback_category: str = "未分類",
    max_retries: int = 2,
    category_max_retries: int = 2,
) -> AppConfig:
    return AppConfig(
        llm=LLMConfig(model="test-model", max_retries=max_retries),
        summarizer=SummarizerConfig(
            categories=categories or ["テクノロジー", "ビジネス", "その他"],
            fallback_category=fallback_category,
            category_max_retries=category_max_retries,
        ),
        database=DatabaseConfig(path=":memory:"),
        discord=DiscordConfig(webhook_url=None),
    )


def _make_article() -> Article:
    now = datetime.now()
    return Article(
        source_type="rss",
        source_id="test-1",
        title="テスト記事",
        content="テスト本文",
        url="https://example.com",
        published_at=now,
        fetched_at=now,
        feed_title="テストフィード",
    )


def _make_summary(category: str) -> ArticleSummary:
    return ArticleSummary(
        title="タイトル",
        summary="要約文です。",
        keywords=["A", "B", "C"],
        category=category,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSummarizeArticleCategoryRetry:
    """summarize_article のカテゴリバリデーションと再試行ロジックのテスト。"""

    def test_valid_category_returns_immediately(self):
        """有効なカテゴリが返された場合は1回の呼び出しで完了する。"""
        cfg = _make_cfg()
        article = _make_article()

        with patch.object(config_module, "config", cfg):
            import importlib
            import summarizer.summarizer as summod
            importlib.reload(summod)

            with patch("summarizer.summarizer.call_with_retry") as mock_call, \
                 patch("summarizer.summarizer.get_client"), \
                 patch("summarizer.summarizer.get_model_name", return_value="test-model"), \
                 patch("summarizer.summarizer.build_step_params", return_value=({}, None)):

                mock_call.return_value = _make_summary("テクノロジー")
                result = summod.summarize_article(article)

        assert result.category == "テクノロジー"
        assert mock_call.call_count == 1

    def test_invalid_category_triggers_retry(self):
        """無効なカテゴリが返された場合は再試行する。"""
        cfg = _make_cfg(max_retries=2)
        article = _make_article()

        with patch.object(config_module, "config", cfg):
            import importlib
            import summarizer.summarizer as summod
            importlib.reload(summod)

            with patch("summarizer.summarizer.call_with_retry") as mock_call, \
                 patch("summarizer.summarizer.get_client"), \
                 patch("summarizer.summarizer.get_model_name", return_value="test-model"), \
                 patch("summarizer.summarizer.build_step_params", return_value=({}, None)):

                # 1回目: 無効、2回目: 有効
                mock_call.side_effect = [
                    _make_summary("存在しないカテゴリ"),
                    _make_summary("ビジネス"),
                ]
                result = summod.summarize_article(article)

        assert result.category == "ビジネス"
        assert mock_call.call_count == 2

    def test_retry_prompt_includes_invalid_category_feedback(self):
        """再試行時のメッセージ履歴に無効カテゴリ名・前回応答・カテゴリ一覧が含まれること。"""
        cfg = _make_cfg(max_retries=2)
        article = _make_article()

        with patch.object(config_module, "config", cfg):
            import importlib
            import summarizer.summarizer as summod
            importlib.reload(summod)

            with patch("summarizer.summarizer.call_with_retry") as mock_call, \
                 patch("summarizer.summarizer.get_client"), \
                 patch("summarizer.summarizer.get_model_name", return_value="test-model"), \
                 patch("summarizer.summarizer.build_step_params", return_value=({}, None)):

                mock_call.side_effect = [
                    _make_summary("未知のカテゴリ"),
                    _make_summary("その他"),
                ]
                summod.summarize_article(article)

        # 2回目の呼び出しのメッセージ履歴を確認
        second_call_kwargs = mock_call.call_args_list[1]
        messages = second_call_kwargs[0][1]["messages"]  # positional arg index 1

        roles = [m["role"] for m in messages]
        assert roles == ["system", "user", "assistant", "user"], "マルチターン形式になっていること"

        # assistantメッセージ（前回の出力JSON）に無効カテゴリが含まれること
        assistant_content = messages[2]["content"]
        assert "未知のカテゴリ" in assistant_content

        # 最後のuserメッセージに無効カテゴリ名とカテゴリ一覧が含まれること
        correction_message = messages[3]["content"]
        assert "未知のカテゴリ" in correction_message
        assert "テクノロジー" in correction_message

    def test_fallback_applied_after_all_retries_exhausted(self):
        """全試行が失敗した場合に fallback_category が適用される。"""
        cfg = _make_cfg(fallback_category="未分類", max_retries=2)
        article = _make_article()

        with patch.object(config_module, "config", cfg):
            import importlib
            import summarizer.summarizer as summod
            importlib.reload(summod)

            with patch("summarizer.summarizer.call_with_retry") as mock_call, \
                 patch("summarizer.summarizer.get_client"), \
                 patch("summarizer.summarizer.get_model_name", return_value="test-model"), \
                 patch("summarizer.summarizer.build_step_params", return_value=({}, None)):

                # 全試行で無効カテゴリを返す (max_retries=2 → 計3回)
                mock_call.side_effect = [_make_summary("謎カテゴリ")] * 3
                result = summod.summarize_article(article)

        assert result.category == "未分類"
        assert mock_call.call_count == 3  # 初回 + 2回再試行

    def test_fallback_uses_config_value(self):
        """fallback_category に設定した値が使われること。"""
        cfg = _make_cfg(fallback_category="その他", category_max_retries=1)
        article = _make_article()

        with patch.object(config_module, "config", cfg):
            import importlib
            import summarizer.summarizer as summod
            importlib.reload(summod)

            with patch("summarizer.summarizer.call_with_retry") as mock_call, \
                 patch("summarizer.summarizer.get_client"), \
                 patch("summarizer.summarizer.get_model_name", return_value="test-model"), \
                 patch("summarizer.summarizer.build_step_params", return_value=({}, None)):

                mock_call.side_effect = [_make_summary("不明")] * 2
                result = summod.summarize_article(article)

        assert result.category == "その他"

    def test_category_max_retries_independent_from_llm_max_retries(self):
        """category_max_retries は llm.max_retries と独立して動作すること。"""
        # llm.max_retries=5（大きい）、category_max_retries=1（小さい）で
        # API 呼び出し回数が category_max_retries + 1 = 2 回であることを確認
        cfg = _make_cfg(max_retries=5, category_max_retries=1)
        article = _make_article()

        with patch.object(config_module, "config", cfg):
            import importlib
            import summarizer.summarizer as summod
            importlib.reload(summod)

            with patch("summarizer.summarizer.call_with_retry") as mock_call, \
                 patch("summarizer.summarizer.get_client"), \
                 patch("summarizer.summarizer.get_model_name", return_value="test-model"), \
                 patch("summarizer.summarizer.build_step_params", return_value=({}, None)):

                mock_call.side_effect = [_make_summary("不正")] * 2
                result = summod.summarize_article(article)

        assert result.category == "未分類"
        assert mock_call.call_count == 2  # category_max_retries=1 → 初回+1回 = 2回
