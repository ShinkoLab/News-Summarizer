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


class _CategoryDigestLLMOutput(BaseModel):
    """カテゴリ別LLM出力（箇条書きのみ）"""
    articles: list[str]


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

    groups_info = ""
    for i, (topic, g_summaries) in enumerate(groups, start=1):
        header = f"[グループ{i}] トピック: {topic}" if topic else f"[グループ{i}]（単独記事）"
        groups_info += f"{header}\n"
        for s in g_summaries:
            groups_info += f"・{s.title}: {s.summary}\n"
        groups_info += "\n"

    prompt = f"""カテゴリ「{category}」の記事群をグループ単位でまとめてください。

【制約事項】
- 各グループを配列の1要素として出力してください（1要素=1グループ）。
- 各グループの要点を1〜2文で記述してください。複数記事を含むグループは共通する要点に統合してください。トピック名などのプレフィックスは一切付けないでください。
- 各要素の先頭に「・」「-」「*」「•」「1.」などの記号や番号を含めないでください。
- 各要素に改行を含めないでください。
- 全体で{max_chars}文字以内としてください。
- 出力は日本語に統一してください。

【グループ一覧】
{groups_info.strip()}"""

    completion_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたは優秀なニュース編集者です。記事グループを読み、各グループの要点を簡潔な1〜2文にまとめます。複数記事のグループは共通する要点に統合し、プレフィックスは付けません。"},
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
        articles=_normalize_bullets(llm_result.articles),
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
        bullets = "\n".join(f"• {a}" for a in cd.articles)
        digest_info += f"■ {cd.category}（{cd.article_count}件）\n{bullets}\n\n"

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

    n_categories = len(by_category)
    max_chars_per_category = max_length // max(1, n_categories)

    # Pass 1: カテゴリ別にCategoryDigestを生成
    category_digests: List[CategoryDigest] = []
    for category, bucket in by_category.items():
        # グループ記事を先頭、単独記事を後ろに並べる
        groups: list[tuple[str | None, List[ArticleSummary]]] = []
        for g in bucket["groups"].values():
            # 1記事のグループもトピック名を保持して渡す
            groups.append((g["topic"], g["summaries"]))
        for s in bucket["singles"]:
            groups.append((None, [s]))

        cd = _generate_category_digest(
            category=category,
            groups=groups,
            client=client,
            model=model,
            parameters=parameters,
            extra_body=extra_body,
            max_chars=max_chars_per_category,
            stream=stream,
        )
        category_digests.append(cd)

    # Pass 2: カテゴリ別ダイジェストからoverviewを生成
    overview = _generate_overview(
        category_digests=category_digests,
        client=client,
        model=model,
        parameters=parameters,
        extra_body=extra_body,
        stream=stream,
    )

    return DigestResult(
        overview=overview,
        categories=category_digests,
        total_articles=len(grouped_summaries),
    )
