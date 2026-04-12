from collections import defaultdict
from typing import List
from datetime import datetime
from pydantic import BaseModel
from models import ArticleSummary, CategoryDigest, DigestResult
from summarizer.llm_client import get_client, get_model_name, build_step_params, call_with_retry
from config import config
from logger import get_logger

logger = get_logger(__name__)


class _CategoryDigestLLMOutput(BaseModel):
    """カテゴリ別LLM出力（箇条書きのみ）"""
    articles: list[str]


class _OverviewLLMOutput(BaseModel):
    """overview専用LLM出力"""
    overview: str


def _generate_category_digest(
    category: str,
    summaries: List[ArticleSummary],
    client,
    model: str,
    parameters: dict,
    extra_body: dict | None,
    max_chars: int,
    stream: bool,
) -> CategoryDigest:
    summaries_info = "".join(f"・{s.title}: {s.summary}\n" for s in summaries)

    prompt = f"""カテゴリ「{category}」の記事を1〜2文の箇条書きにまとめてください。

【制約事項】
- 各記事を箇条書き1項目として列挙してください。
- 全体で{max_chars}文字以内としてください。
- 出力は日本語に統一してください。

【記事一覧】
{summaries_info}"""

    completion_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたは優秀なニュース編集者です。記事一覧を読み、各記事の要点を簡潔な箇条書きにまとめます。"},
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
        articles=llm_result.articles,
        article_count=len(summaries),
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


def generate_digest(summaries: List[ArticleSummary], stream: bool = False) -> DigestResult:
    if not summaries:
        return DigestResult(
            overview="記事がありませんでした。",
            categories=[],
            total_articles=0,
        )

    client = get_client()
    model = get_model_name()
    max_length = config["summarizer"]["digest_max_length"]
    parameters, extra_body = build_step_params("digest")

    # カテゴリ別にグループ化
    by_category: dict[str, List[ArticleSummary]] = defaultdict(list)
    for s in summaries:
        by_category[s.category].append(s)

    n_categories = len(by_category)
    max_chars_per_category = max_length // max(1, n_categories)

    # Pass 1: カテゴリ別にCategoryDigestを生成
    category_digests: List[CategoryDigest] = []
    for category, cat_summaries in by_category.items():
        cd = _generate_category_digest(
            category=category,
            summaries=cat_summaries,
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
        total_articles=len(summaries),
    )
