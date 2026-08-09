"""generate_digest の二段階（Pass1: カテゴリ別 / Pass2: overview）の堅牢化テスト。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from config import AppConfig, CategoryDef, CategoryTaxonomy, LLMConfig, SummarizerConfig
from models import ArticleSummary, CategoryDigest
from summarizer.digest import MIN_CHARS_PER_CATEGORY, generate_digest


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


def test_categories_follow_taxonomy_order():
    """出力順は記事の出現順ではなく taxonomy の定義順に従い、未定義カテゴリは末尾。"""
    # 実ファイルの並び順に依存しないよう、テスト内で定義順を固定する
    cfg = AppConfig(
        llm=LLMConfig(model="test-model"),
        taxonomy=CategoryTaxonomy(
            categories=[
                CategoryDef(name="甲", description="最初。"),
                CategoryDef(name="乙", description="二番目。"),
                CategoryDef(name="丙", description="三番目。"),
            ],
        ),
    )

    # 定義順と逆順＋未定義カテゴリを混ぜて投入する
    grouped = [
        (_summary("未定義"), None, None),
        (_summary("丙"), None, None),
        (_summary("甲"), None, None),
        (_summary("乙"), None, None),
    ]

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest.config", cfg), \
        patch(
            "summarizer.digest._generate_category_digest",
            side_effect=lambda category, **_kw: CategoryDigest(
                category=category, articles=["x"], article_count=1
            ),
        ), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        result = generate_digest(grouped)

    assert [c.category for c in result.categories] == ["甲", "乙", "丙", "未定義"]


def test_char_budget_is_proportional_to_article_count():
    """文字数はカテゴリ数の均等割りではなく記事数に比例配分され、下限も守られる。"""
    cfg = AppConfig(
        llm=LLMConfig(model="test-model"),
        summarizer=SummarizerConfig(digest_max_length=3000),
        taxonomy=CategoryTaxonomy(
            categories=[
                CategoryDef(name="多", description="記事が多い。"),
                CategoryDef(name="少", description="記事が少ない。"),
            ],
        ),
    )
    # 90件 vs 10件（計100件）
    grouped = [(_summary("多"), None, None) for _ in range(90)]
    grouped += [(_summary("少"), None, None) for _ in range(10)]

    received: dict[str, int] = {}

    def capture(category, max_chars, **_kw):
        received[category] = max_chars
        return CategoryDigest(category=category, articles=["x"], article_count=1)

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest.config", cfg), \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    # 均等割りなら両方 1500 になるが、比例配分では 90:10 に分かれる
    assert received["多"] == 2700
    assert received["少"] == 300


def test_small_category_gets_minimum_char_budget():
    """記事が極端に少ないカテゴリでも文章として成立する下限を確保する。"""
    cfg = AppConfig(
        llm=LLMConfig(model="test-model"),
        summarizer=SummarizerConfig(digest_max_length=3000),
        taxonomy=CategoryTaxonomy(
            categories=[
                CategoryDef(name="多", description="記事が多い。"),
                CategoryDef(name="少", description="記事が少ない。"),
            ],
        ),
    )
    # 999件 vs 1件 → 比例だけだと 1件側は 3000*1//1000 = 3文字になってしまう
    grouped = [(_summary("多"), None, None) for _ in range(999)]
    grouped += [(_summary("少"), None, None)]

    received: dict[str, int] = {}

    def capture(category, max_chars, **_kw):
        received[category] = max_chars
        return CategoryDigest(category=category, articles=["x"], article_count=1)

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest.config", cfg), \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    assert received["少"] == MIN_CHARS_PER_CATEGORY


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
