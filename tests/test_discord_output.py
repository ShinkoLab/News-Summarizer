"""Tests for outputs/discord_output.py — embed building logic, not network calls."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

import config as config_module
from config import AppConfig, LLMConfig, DiscordConfig
from models import ArticleSummary, CategoryDigest, DigestResult
from outputs.discord_output import DiscordOutput


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app_config(
    webhook_url: str | None = "https://discord.com/api/webhooks/test",
    embed_color: int = 0x58B9C2,
    footer_text: str = "Test Footer",
    post_individual_articles: bool = True,
) -> AppConfig:
    return AppConfig(
        llm=LLMConfig(model="test-model"),
        discord=DiscordConfig(
            webhook_url=webhook_url,
            embed_color=embed_color,
            footer_text=footer_text,
            post_individual_articles=post_individual_articles,
        ),
    )


def _make_digest(overview: str = "今日のニュース概要") -> DigestResult:
    return DigestResult(
        overview=overview,
        categories=[
            CategoryDigest(
                category="テクノロジー",
                articles=["記事1の要約", "記事2の要約"],
                article_count=2,
            ),
            CategoryDigest(
                category="ビジネス",
                articles=["ビジネス記事1"],
                article_count=1,
            ),
        ],
        total_articles=3,
        generated_at=datetime(2026, 4, 14, 12, 0, 0),
    )


def _make_summaries(n: int = 2) -> list[ArticleSummary]:
    return [
        ArticleSummary(
            title=f"記事タイトル{i}",
            summary=f"記事{i}の要約文です。",
            keywords=[f"kw{i}a", f"kw{i}b"],
            category="テクノロジー",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# _create_digest_embed structure
# ---------------------------------------------------------------------------

class TestCreateDigestEmbed:
    def setup_method(self):
        cfg = _make_app_config()
        with patch.object(config_module, "config", cfg):
            self.output = DiscordOutput.__new__(DiscordOutput)
            self.output.webhook_url = cfg.discord.webhook_url
            self.output.embed_color = cfg.discord.embed_color
            self.output.footer_text = cfg.discord.footer_text
            self.output.post_individual_articles = cfg.discord.post_individual_articles

    def test_title_is_set(self):
        digest = _make_digest()
        embed = self.output._create_digest_embed(digest)
        assert "title" in embed
        assert embed["title"]  # non-empty

    def test_description_contains_overview(self):
        digest = _make_digest(overview="重要なニュース概要")
        embed = self.output._create_digest_embed(digest)
        assert "重要なニュース概要" in embed["description"]

    def test_description_contains_category_names(self):
        digest = _make_digest()
        embed = self.output._create_digest_embed(digest)
        assert "テクノロジー" in embed["description"]
        assert "ビジネス" in embed["description"]

    def test_color_matches_config(self):
        digest = _make_digest()
        embed = self.output._create_digest_embed(digest)
        assert embed["color"] == 0x58B9C2

    def test_footer_contains_total_articles(self):
        digest = _make_digest()
        embed = self.output._create_digest_embed(digest)
        footer_text = embed["footer"]["text"]
        assert "3" in footer_text  # total_articles = 3

    def test_footer_contains_footer_text(self):
        digest = _make_digest()
        embed = self.output._create_digest_embed(digest)
        footer_text = embed["footer"]["text"]
        assert "Test Footer" in footer_text

    def test_footer_empty_when_no_footer_text(self):
        cfg = _make_app_config(footer_text="")
        self.output.footer_text = ""
        digest = _make_digest()
        embed = self.output._create_digest_embed(digest)
        assert "Test Footer" not in embed["footer"]["text"]

    def test_overview_omitted_when_empty(self):
        # overview 失敗時は太字 overview 行を出さない（カテゴリは表示）
        digest = DigestResult(
            overview="",
            categories=[CategoryDigest(category="テクノロジー", articles=["x"], article_count=3)],
            total_articles=3,
            generated_at=datetime(2026, 4, 14, 12, 0, 0),
        )
        embed = self.output._create_digest_embed(digest)
        assert not embed["description"].startswith("**\n")
        assert "テクノロジー" in embed["description"]

    def test_footer_shows_degraded_note_when_categories_dropped(self):
        # 表示カテゴリの記事数合計 < total_articles なら退化注記を出す
        digest = _make_digest()  # categories合計3, total_articles=3
        digest = DigestResult(
            overview="概要",
            categories=[CategoryDigest(category="テクノロジー", articles=["x"], article_count=1)],
            total_articles=3,  # 2件分のカテゴリが除外された状態
            generated_at=datetime(2026, 4, 14, 12, 0, 0),
        )
        embed = self.output._create_digest_embed(digest)
        assert "※" in embed["footer"]["text"]

    def test_footer_no_degraded_note_when_complete(self):
        digest = _make_digest()  # 合計3 == total 3, overviewあり
        embed = self.output._create_digest_embed(digest)
        assert "※" not in embed["footer"]["text"]


# ---------------------------------------------------------------------------
# _create_summary_embed structure
# ---------------------------------------------------------------------------

class TestCreateSummaryEmbed:
    def setup_method(self):
        cfg = _make_app_config()
        self.output = DiscordOutput.__new__(DiscordOutput)
        self.output.webhook_url = cfg.discord.webhook_url
        self.output.embed_color = cfg.discord.embed_color
        self.output.footer_text = cfg.discord.footer_text
        self.output.post_individual_articles = cfg.discord.post_individual_articles

    def test_title_matches_summary_title(self):
        summary = ArticleSummary(
            title="テスト記事", summary="要約文", keywords=["a"], category="科学"
        )
        embed = self.output._create_summary_embed(summary)
        assert embed["title"] == "テスト記事"

    def test_description_matches_summary_text(self):
        summary = ArticleSummary(
            title="タイトル", summary="詳細な要約", keywords=["a"], category="AI・機械学習"
        )
        embed = self.output._create_summary_embed(summary)
        assert embed["description"] == "詳細な要約"

    def test_fields_include_category(self):
        summary = ArticleSummary(
            title="t", summary="s", keywords=["k"], category="セキュリティ"
        )
        embed = self.output._create_summary_embed(summary)
        field_names = [f["name"] for f in embed["fields"]]
        assert any("カテゴリ" in n for n in field_names)

    def test_fields_include_keywords(self):
        summary = ArticleSummary(
            title="t", summary="s", keywords=["key1", "key2"], category="その他"
        )
        embed = self.output._create_summary_embed(summary)
        fields = {f["name"]: f["value"] for f in embed["fields"]}
        kw_value = fields.get("キーワード", "")
        assert "key1" in kw_value
        assert "key2" in kw_value


# ---------------------------------------------------------------------------
# post() — HTTP call mocked; post_individual_articles behavior
# ---------------------------------------------------------------------------

class TestPost:
    def _make_output(self, post_individual: bool = True) -> DiscordOutput:
        cfg = _make_app_config(post_individual_articles=post_individual)
        out = DiscordOutput.__new__(DiscordOutput)
        out.webhook_url = cfg.discord.webhook_url
        out.embed_color = cfg.discord.embed_color
        out.footer_text = cfg.discord.footer_text
        out.post_individual_articles = post_individual
        return out

    def test_post_sends_digest_embed(self):
        out = self._make_output()
        digest = _make_digest()
        summaries = _make_summaries(2)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        with patch("httpx.post", return_value=mock_response) as mock_post:
            out.post(digest, summaries)

        # At minimum, the digest embed was sent once
        assert mock_post.call_count >= 1
        first_call_payload = mock_post.call_args_list[0][1]["json"]
        assert "embeds" in first_call_payload

    def test_post_individual_articles_false_skips_article_posts(self):
        out = self._make_output(post_individual=False)
        digest = _make_digest()
        summaries = _make_summaries(3)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        with patch("httpx.post", return_value=mock_response) as mock_post:
            out.post(digest, summaries)

        # Only 1 call for the digest embed; no individual article calls
        assert mock_post.call_count == 1

    def test_post_individual_articles_true_sends_multiple_calls(self):
        out = self._make_output(post_individual=True)
        digest = _make_digest()
        summaries = _make_summaries(3)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        with patch("httpx.post", return_value=mock_response) as mock_post:
            out.post(digest, summaries)

        # Digest call + at least one batch of individual articles
        assert mock_post.call_count >= 2

    def test_empty_digest_skips_digest_embed_but_posts_articles(self):
        """overview も categories も空なら digest embed をスキップし個別記事は投稿する。"""
        out = self._make_output(post_individual=True)
        empty_digest = DigestResult(overview="", categories=[], total_articles=2)
        summaries = _make_summaries(2)

        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None

        with patch("httpx.post", return_value=mock_response) as mock_post:
            out.post(empty_digest, summaries)

        # digest embed は送られず、個別記事のバッチのみ送られる
        assert mock_post.call_count == 1
        payload = mock_post.call_args_list[0][1]["json"]
        # 個別記事Embed（タイトル付き）であることを確認
        assert payload["embeds"][0]["title"] == "記事タイトル0"

    def test_no_webhook_skips_post(self):
        cfg = _make_app_config(webhook_url=None)
        out = DiscordOutput.__new__(DiscordOutput)
        out.webhook_url = None
        out.embed_color = 0x58B9C2
        out.footer_text = ""
        out.post_individual_articles = True

        digest = _make_digest()
        summaries = _make_summaries(1)

        with patch("httpx.post") as mock_post:
            out.post(digest, summaries)

        mock_post.assert_not_called()
