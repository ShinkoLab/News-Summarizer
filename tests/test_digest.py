"""generate_digest の二段階（Pass1: カテゴリ別 / Pass2: overview）の堅牢化テスト。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from config import AppConfig, CategoryDef, CategoryTaxonomy, LLMConfig, SummarizerConfig
from models import ArticleSummary, CategoryDigest
from summarizer.digest import (
    MAX_HIGHLIGHTS,
    MIN_CHARS_PER_CATEGORY,
    _CategoryDigestLLMOutput,
    _generate_category_digest,
    _highlight_quota,
    _normalize_bullets,
    generate_digest,
)


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
        return CategoryDigest(category=category, summary="本文", highlights=["x"], article_count=1)

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
            return_value=CategoryDigest(category="A", summary="本文", highlights=["x"], article_count=1),
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
                category=category, summary="本文", highlights=["x"], article_count=1
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
        return CategoryDigest(category=category, summary="本文", highlights=["x"], article_count=1)

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest.config", cfg), \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    # 均等割りなら両方 1500。下限120を先に確保し、残り2760を90:10で配分する
    assert received["多"] == MIN_CHARS_PER_CATEGORY + 2760 * 90 // 100  # 2604
    assert received["少"] == MIN_CHARS_PER_CATEGORY + 2760 * 10 // 100  # 396
    assert sum(received.values()) == 3000


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
        return CategoryDigest(category=category, summary="本文", highlights=["x"], article_count=1)

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest.config", cfg), \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    assert received["少"] >= MIN_CHARS_PER_CATEGORY
    assert sum(received.values()) <= 3000


def test_char_budget_never_exceeds_digest_max_length():
    """下限を確保しても合計が digest_max_length を超えない。

    下限を後から max() で被せる実装では、記事が1カテゴリに集中したときに
    合計が上限を超え、Discord 側の4096字切り詰めを誘発していた。
    """
    names = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]
    cfg = AppConfig(
        llm=LLMConfig(model="test-model"),
        summarizer=SummarizerConfig(digest_max_length=3000),
        taxonomy=CategoryTaxonomy(
            categories=[CategoryDef(name=n, description=f"{n}の説明。") for n in names],
        ),
    )
    # 1カテゴリに93件、残り7カテゴリに1件ずつ（計100件）
    grouped = [(_summary("c1"), None, None) for _ in range(93)]
    grouped += [(_summary(n), None, None) for n in names[1:]]

    received: dict[str, int] = {}

    def capture(category, max_chars, **_kw):
        received[category] = max_chars
        return CategoryDigest(category=category, summary="本文", highlights=["x"], article_count=1)

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest.config", cfg), \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    assert len(received) == 8
    assert sum(received.values()) <= 3000
    assert all(v >= MIN_CHARS_PER_CATEGORY for v in received.values())


def test_char_budget_falls_back_to_even_split_when_floors_do_not_fit():
    """カテゴリ数 × 下限 が上限を超える場合は均等割りに退避する。"""
    names = [f"c{i}" for i in range(10)]
    cfg = AppConfig(
        llm=LLMConfig(model="test-model"),
        # 10カテゴリ × 下限120 = 1200 > 500
        summarizer=SummarizerConfig(digest_max_length=500),
        taxonomy=CategoryTaxonomy(
            categories=[CategoryDef(name=n, description=f"{n}の説明。") for n in names],
        ),
    )
    grouped = [(_summary(n), None, None) for n in names]

    received: dict[str, int] = {}

    def capture(category, max_chars, **_kw):
        received[category] = max_chars
        return CategoryDigest(category=category, summary="本文", highlights=["x"], article_count=1)

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest.config", cfg), \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    assert set(received.values()) == {50}
    assert sum(received.values()) <= 500


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


# ---------------------------------------------------------------------------
# _generate_category_digest（全テストでモックされていた部分）
# ---------------------------------------------------------------------------

def _run_category_digest(groups, max_chars=500, llm_output=None, captured=None):
    """_generate_category_digest を LLM 呼び出しだけモックして実行する。"""
    def fake_call(_client, completion_kwargs, _stream):
        if captured is not None:
            captured["kwargs"] = completion_kwargs
        return llm_output or _CategoryDigestLLMOutput(summary="本文です。", highlights=["トピック"])

    with patch("summarizer.digest.call_with_retry", side_effect=fake_call):
        return _generate_category_digest(
            category="テクノロジー",
            groups=groups,
            client=MagicMock(),
            model="test-model",
            parameters={},
            extra_body=None,
            max_chars=max_chars,
            stream=False,
        )


def test_category_prompt_carries_char_budget_and_highlight_cap():
    """max_chars と highlights の上限件数が実際にプロンプトへ埋め込まれる。"""
    captured: dict = {}
    # ハイライトが最大件数になる規模（30件）
    groups = [("トピックA", [_summary("テクノロジー") for _ in range(30)])]

    result = _run_category_digest(groups, max_chars=777, captured=captured)

    prompt = captured["kwargs"]["messages"][1]["content"]
    assert "777文字以内" in prompt
    assert f"最大{MAX_HIGHLIGHTS}件" in prompt
    assert "トピックA" in prompt
    assert result.summary == "本文です。"
    assert result.article_count == 30


def test_highlights_are_capped_and_normalized():
    """LLM が上限を超える件数や記号付きで返しても、件数制限と記号除去を行う。"""
    noisy = _CategoryDigestLLMOutput(
        summary="  本文です。  ",
        highlights=["・1件目", "2. 2件目", "• 3件目", "4件目", "5件目"],
    )
    groups = [(None, [_summary("テクノロジー") for _ in range(30)])]

    result = _run_category_digest(groups, llm_output=noisy)

    assert result.summary == "本文です。"
    assert result.highlights == ["1件目", "2件目", "3件目"]


class TestHighlightQuota:
    """記事数が少ないカテゴリでは散文が全記事を言い切るため、ハイライトを絞る。"""

    def test_quota_scales_with_article_count(self):
        # 5件未満は散文のみ
        assert _highlight_quota(1) == 0
        assert _highlight_quota(4) == 0
        # 記事の1/3を超えないよう頭打ちにする
        assert _highlight_quota(5) == 1
        assert _highlight_quota(6) == 2
        assert _highlight_quota(9) == 3
        assert _highlight_quota(29) == MAX_HIGHLIGHTS
        assert _highlight_quota(100) == MAX_HIGHLIGHTS

    def test_small_category_drops_highlights_even_if_llm_returns_them(self):
        noisy = _CategoryDigestLLMOutput(summary="本文。", highlights=["余計な1件", "余計な2件"])
        groups = [(None, [_summary("テクノロジー") for _ in range(2)])]

        result = _run_category_digest(groups, llm_output=noisy)

        assert result.highlights == []

    def test_small_category_prompt_asks_for_empty_highlights(self):
        captured: dict = {}
        groups = [(None, [_summary("テクノロジー") for _ in range(2)])]

        _run_category_digest(groups, captured=captured)

        prompt = captured["kwargs"]["messages"][1]["content"]
        assert "highlights は空の配列にしてください" in prompt
        assert "最大" not in prompt

    def test_medium_category_is_capped_at_a_third_of_articles(self):
        noisy = _CategoryDigestLLMOutput(summary="本文。", highlights=["1件目", "2件目", "3件目"])
        groups = [(None, [_summary("テクノロジー") for _ in range(5)])]

        result = _run_category_digest(groups, llm_output=noisy)

        assert result.highlights == ["1件目"]


# ---------------------------------------------------------------------------
# _normalize_bullets
# ---------------------------------------------------------------------------

class TestNormalizeBullets:
    def test_strips_leading_markers(self):
        assert _normalize_bullets(["・あ", "- い", "* う", "• え", "1. お", "2) か"]) == [
            "あ", "い", "う", "え", "お", "か",
        ]

    def test_splits_embedded_newlines(self):
        assert _normalize_bullets(["あ\n・い\nう"]) == ["あ", "い", "う"]

    def test_drops_empty_entries(self):
        assert _normalize_bullets(["", "   ", "・", "あ"]) == ["あ"]

    def test_keeps_inner_punctuation(self):
        assert _normalize_bullets(["A社とB社が提携・統合を発表"]) == ["A社とB社が提携・統合を発表"]


def test_split_categories_break_a_cluster_apart():
    """カテゴリが揃っていないクラスタは2つのグループに割れる。

    ダイジェストは「カテゴリ → group_id」の順で階層化するため、同じ group_id でも
    カテゴリが違えば別グループとして LLM に渡ってしまう。これが実データで
    「北日本東日本の大雨警戒」が 社会 と 環境 に分断されていた理由で、
    `pipeline.unify_group_categories()` が事前にカテゴリを揃える根拠でもある。
    """
    captured: dict[str, list] = {}

    def capture(category, groups, **kwargs):
        captured[category] = groups
        return CategoryDigest(category=category, summary="本文", highlights=[], article_count=1)

    grouped = [(_summary("環境"), 7, "大雨警戒"), (_summary("社会"), 7, "大雨警戒")]

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    assert set(captured) == {"環境", "社会"}
    assert [len(g_summaries) for _, g_summaries in captured["環境"]] == [1]
    assert [len(g_summaries) for _, g_summaries in captured["社会"]] == [1]


def test_unified_categories_keep_a_cluster_in_one_group():
    """カテゴリを揃えておけば、クラスタは1グループとして LLM に渡る。"""
    captured: dict[str, list] = {}

    def capture(category, groups, **kwargs):
        captured[category] = groups
        return CategoryDigest(category=category, summary="本文", highlights=[], article_count=2)

    grouped = [(_summary("社会"), 7, "大雨警戒"), (_summary("社会"), 7, "大雨警戒")]

    c1, c2, c3 = _patch_common()
    with c1, c2, c3, \
        patch("summarizer.digest._generate_category_digest", side_effect=capture), \
        patch("summarizer.digest._generate_overview", return_value="概要"):
        generate_digest(grouped)

    assert set(captured) == {"社会"}
    assert captured["社会"] == [("大雨警戒", [g[0] for g in grouped])]
