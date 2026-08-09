import re
from collections import defaultdict
from typing import List
from datetime import datetime
from pydantic import BaseModel
from models import ArticleSummary, CategoryDigest, DigestResult
from summarizer.llm_client import get_client, get_model_name, build_step_params, call_with_retry
from config import config
from logger import get_logger

logger = get_logger(__name__)

# 記事数の少ないカテゴリに割り当てる文字数の下限
MIN_CHARS_PER_CATEGORY = 120

# カテゴリごとに添える特筆トピックの最大件数
MAX_HIGHLIGHTS = 3

# ハイライトを添え始める記事数の下限。これ未満のカテゴリは散文だけにする
MIN_ARTICLES_FOR_HIGHLIGHTS = 5

# ハイライト1件あたりに必要な記事数。ハイライトが記事の1/3を超えると
# 散文の言い換えに近づくため、この比率で頭打ちにする
ARTICLES_PER_HIGHLIGHT = 3


def _highlight_quota(article_count: int) -> int:
    """記事数に応じたハイライトの上限件数を返す。

    記事が少ないカテゴリでは散文が全記事を言い切ってしまうため、
    ハイライトを添えると同じ内容の言い換えにしかならない。
    """
    if article_count < MIN_ARTICLES_FOR_HIGHLIGHTS:
        return 0
    return min(MAX_HIGHLIGHTS, max(1, article_count // ARTICLES_PER_HIGHLIGHT))


def _count_articles(bucket: dict) -> int:
    """カテゴリ配下（グループ記事＋単独記事）の記事数を数える。"""
    return sum(len(g["summaries"]) for g in bucket["groups"].values()) + len(bucket["singles"])


def _allocate_chars(by_category: dict, total_articles: int, max_length: int) -> dict[str, int]:
    """カテゴリごとの文字数予算を決める。

    カテゴリ数で均等割りすると、記事が集中したカテゴリで1件あたり数文字まで潰れて
    内容が黙って欠落する。そのため記事数に比例配分する。一方で記事の少ない
    カテゴリも文章として成立する必要があるので下限を設ける。

    下限を後から max() で被せると合計が max_length を超えてしまうため、
    **先に全カテゴリぶんの下限を確保し、残りを比例配分する**。これにより
    比例配分と下限を両立したまま、合計が max_length を超えない。
    """
    n = len(by_category)
    if n == 0:
        return {}

    floor_total = MIN_CHARS_PER_CATEGORY * n
    if floor_total >= max_length:
        # 予算が下限の総和にすら満たない場合は均等割りに退避する
        return {category: max(1, max_length // n) for category in by_category}

    remaining = max_length - floor_total
    return {
        category: MIN_CHARS_PER_CATEGORY + remaining * _count_articles(bucket) // total_articles
        for category, bucket in by_category.items()
    }

# LLM が list[str] の各要素に混入させがちな箇条書き記号・番号を除去する
_BULLET_PREFIX = re.compile(
    r"^(?:"
    r"\d+[.)）]\s*"      # 1. / 1) / 1）
    r"|[・•·\-\*○●→▶︎▶]\s*"  # ・ • · - * ○ ● → ▶
    r")+",
    re.UNICODE,
)


def _normalize_bullets(items: list[str]) -> list[str]:
    """LLM出力の箇条書きリストを正規化する。"""
    result = []
    for item in items:
        # 1要素に複数行が詰め込まれている場合は分割
        for line in item.splitlines():
            line = _BULLET_PREFIX.sub("", line).strip()
            if line:
                result.append(line)
    return result


def _highlights_instruction(quota: int) -> str:
    """ハイライトに関するプロンプト断片を、許可件数に応じて組み立てる。"""
    if quota == 0:
        return (
            "【highlights（特筆すべきトピック）】\n"
            "- このカテゴリは記事数が少ないため、highlights は空の配列にしてください。\n"
            "- 要点はすべて本文に含めてください。\n"
        )
    return (
        "【highlights（特筆すべきトピック）】\n"
        f"- 本文で触れた中から特に重要なものを最大{quota}件、各1文で挙げてください。\n"
        f"- 重要なものが少なければ{quota}件に満たなくて構いません。該当が無ければ空の配列にしてください。\n"
        "- 本文の要約の繰り返しにはせず、具体的な事実（企業名・数字・固有名詞）を含めてください。\n"
        "- 各要素の先頭に「・」「-」「*」「•」「1.」などの記号や番号を含めないでください。\n"
        "- 各要素に改行を含めないでください。\n"
    )


class _CategoryDigestLLMOutput(BaseModel):
    """カテゴリ別LLM出力（散文の本文＋特筆トピック）"""
    summary: str
    highlights: list[str]


class _OverviewLLMOutput(BaseModel):
    """overview専用LLM出力"""
    overview: str


def _generate_category_digest(
    category: str,
    groups: list[tuple[str | None, List[ArticleSummary]]],
    client,
    model: str,
    parameters: dict,
    extra_body: dict | None,
    max_chars: int,
    stream: bool,
) -> CategoryDigest:
    total_articles = sum(len(g_summaries) for _, g_summaries in groups)
    highlight_quota = _highlight_quota(total_articles)

    groups_info = ""
    for i, (topic, g_summaries) in enumerate(groups, start=1):
        header = f"[グループ{i}] トピック: {topic}" if topic else f"[グループ{i}]（単独記事）"
        groups_info += f"{header}\n"
        for s in g_summaries:
            groups_info += f"・{s.title}: {s.summary}\n"
        groups_info += "\n"

    prompt = f"""カテゴリ「{category}」の記事群を、読み物として通読できるダイジェストにまとめてください。

【summary（本文）】
- 1〜2段落の散文で書いてください。箇条書きにはしないでください。
- 個々の記事を機械的に並べるのではなく、共通する動きや対立軸、全体の流れが分かるように束ねてください。
- 文と文が自然につながるようにし、「〜という記事があった」のような列挙調は避けてください。

{_highlights_instruction(highlight_quota)}
【共通】
- summary と highlights を合わせて全体で{max_chars}文字以内としてください。
- 出力は日本語に統一してください。

【グループ一覧】
{groups_info.strip()}"""

    completion_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたは優秀なニュース編集者です。複数の記事を読み、その日の動きを散文で概括したうえで、特筆すべきトピックだけを短く添えます。記事の逐次的な列挙は避け、通読できる文章にまとめます。"},
            {"role": "user", "content": prompt}
        ],
        "response_format": _CategoryDigestLLMOutput,
        **parameters
    }
    if extra_body:
        completion_kwargs["extra_body"] = extra_body

    if stream:
        logger.debug("カテゴリ「%s」のダイジェストを生成中...", category)

    llm_result = call_with_retry(client, completion_kwargs, stream)
    return CategoryDigest(
        category=category,
        summary=llm_result.summary.strip(),
        # LLM は指示しても記号や余分な件数を混ぜてくるので、ここで正規化・件数制限する
        highlights=_normalize_bullets(llm_result.highlights)[:highlight_quota],
        article_count=total_articles,
    )


def _generate_overview(
    category_digests: List[CategoryDigest],
    client,
    model: str,
    parameters: dict,
    extra_body: dict | None,
    stream: bool,
) -> str:
    digest_info = ""
    for cd in category_digests:
        digest_info += f"■ {cd.category}（{cd.article_count}件）\n{cd.summary}\n"
        if cd.highlights:
            digest_info += "".join(f"• {h}\n" for h in cd.highlights)
        digest_info += "\n"

    prompt = f"""以下のカテゴリ別ダイジェストから、本日のニュース全体の傾向を2〜3文で概括してください。

【制約事項】
- 全カテゴリを横断した傾向・トレンドを読み取ってください。
- 出力は日本語に統一してください。

【カテゴリ別ダイジェスト】
{digest_info.strip()}"""

    completion_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたは優秀なニュース編集長です。カテゴリ別ダイジェストから本日全体のニュースの傾向を読み取り、簡潔に概括します。"},
            {"role": "user", "content": prompt}
        ],
        "response_format": _OverviewLLMOutput,
        **parameters
    }
    if extra_body:
        completion_kwargs["extra_body"] = extra_body

    if stream:
        logger.debug("全体のoverviewを生成中...")

    llm_result = call_with_retry(client, completion_kwargs, stream)
    return llm_result.overview


def generate_digest(
    grouped_summaries: List[tuple[ArticleSummary, int | None, str | None]],
    stream: bool = False,
) -> DigestResult:
    if not grouped_summaries:
        return DigestResult(
            overview="記事がありませんでした。",
            categories=[],
            total_articles=0,
        )

    client = get_client()
    model = get_model_name()
    max_length = config.summarizer.digest_max_length
    parameters, extra_body = build_step_params("digest")

    # カテゴリ別 → group_id 別に階層化
    # group_id が None の記事は単独グループ扱い
    by_category: dict[str, dict] = defaultdict(lambda: {"groups": {}, "singles": []})
    for summary, group_id, group_topic in grouped_summaries:
        bucket = by_category[summary.category]
        if group_id is None:
            bucket["singles"].append(summary)
        else:
            g = bucket["groups"].setdefault(group_id, {"topic": group_topic, "summaries": []})
            g["summaries"].append(summary)

    total_articles = len(grouped_summaries)
    chars_by_category = _allocate_chars(by_category, total_articles, max_length)

    # categories.yaml の定義順にソートする。未定義カテゴリ（「未分類」等）は末尾に回す。
    order = {name: i for i, name in enumerate(config.taxonomy.names)}
    ordered_categories = sorted(by_category.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[0]))

    # Pass 1: カテゴリ別にCategoryDigestを生成
    category_digests: List[CategoryDigest] = []
    for category, bucket in ordered_categories:
        # グループ記事を先頭、単独記事を後ろに並べる
        groups: list[tuple[str | None, List[ArticleSummary]]] = []
        for g in bucket["groups"].values():
            # 1記事のグループもトピック名を保持して渡す
            groups.append((g["topic"], g["summaries"]))
        for s in bucket["singles"]:
            groups.append((None, [s]))

        # Pass 1: カテゴリ単位の失敗は握りつぶし、そのカテゴリを除外する
        # （コンテキスト溢れ等で1カテゴリが失敗してもダイジェスト全体を巻き込まない）
        try:
            cd = _generate_category_digest(
                category=category,
                groups=groups,
                client=client,
                model=model,
                parameters=parameters,
                extra_body=extra_body,
                max_chars=chars_by_category[category],
                stream=stream,
            )
            category_digests.append(cd)
        except Exception as e:
            logger.warning(
                "カテゴリ「%s」のダイジェスト生成に失敗しました。このカテゴリを除外します: %s",
                category,
                e,
                exc_info=True,
            )

    # Pass 2: カテゴリ別ダイジェストからoverviewを生成
    # overview生成の失敗も握りつぶし、カテゴリ別ダイジェストは保持する
    try:
        overview = _generate_overview(
            category_digests=category_digests,
            client=client,
            model=model,
            parameters=parameters,
            extra_body=extra_body,
            stream=stream,
        )
    except Exception as e:
        logger.warning(
            "overviewの生成に失敗しました。overviewを空にして続行します: %s",
            e,
            exc_info=True,
        )
        overview = ""

    return DigestResult(
        overview=overview,
        categories=category_digests,
        total_articles=len(grouped_summaries),
    )
