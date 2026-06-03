"""generate_digest の二段階（Pass1: カテゴリ別 / Pass2: overview）の堅牢化テスト。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from models import ArticleSummary, CategoryDigest
from summarizer.digest import generate_digest


def _summary(category: str) -> ArticleSummary:
    return ArticleSummary(
        title="タイトル",
        summary="要約",
        keywords=["キーワード"],
        category=category,
    )


def _patch_common():
    """LLM 呼び出し周辺の共通依存をモックする。"""
    return (
        patch("summarizer.digest.get_client", return_value=MagicMock()),
        patch("summarizer.digest.get_model_name", return_value="test-model"),
        patch("summarizer.digest.build_step_params", return_value=({}, None)),
    )


def test_pass1_category_failure_is_excluded():
    """1カテゴリの生成失敗は、そのカテゴリのみ除外し他は残る。"""
    grouped = [(_summary("A"), None, None), (_summary("B"), None, None)]

    def fake_category(category, **_kwargs):
        if category == "A":
            raise RuntimeError("context overflow")
        return CategoryDigest(category=category, articles=["x"], article_count=1)

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest._generate_category_digest", side_effect=fake_category), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        result = generate_digest(grouped)

    assert [c.category for c in result.categories] == ["B"]
    assert result.overview == "概要"
    # total_articles は除外に関わらず全件数を保持
    assert result.total_articles == 2


def test_pass2_overview_failure_keeps_categories():
    """overview 生成失敗時は overview を空にし、カテゴリ別ダイジェストは保持する。"""
    grouped = [(_summary("A"), None, None)]

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch(
            "summarizer.digest._generate_category_digest",
            return_value=CategoryDigest(category="A", articles=["x"], article_count=1),
        ), \
        patch("summarizer.digest._generate_overview", side_effect=RuntimeError("boom")):
        result = generate_digest(grouped)

    assert result.overview == ""
    assert [c.category for c in result.categories] == ["A"]


def test_all_categories_failing_yields_empty_digest():
    """全カテゴリ失敗 + overview 失敗でも例外を投げず、空のダイジェストを返す。"""
    grouped = [(_summary("A"), None, None)]

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest._generate_category_digest", side_effect=RuntimeError("x")), \
        patch("summarizer.digest._generate_overview", side_effect=RuntimeError("y")):
        result = generate_digest(grouped)

    assert result.categories == []
    assert result.overview == ""
    assert result.total_articles == 1
