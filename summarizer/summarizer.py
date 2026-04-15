from models import Article, ArticleSummary
from summarizer.llm_client import get_client, get_model_name, build_step_params, call_with_retry, call_once
from config import config
from logger import get_logger

logger = get_logger(__name__)


def summarize_article(article: Article, stream: bool = False) -> ArticleSummary:
    """
    記事の内容を読み、要約・キーワード・カテゴリを抽出する。
    LLM による Structured Output を使用して ArticleSummary を生成する。
    カテゴリが設定ファイルの一覧にない場合は max_retries 回まで再試行し、
    それでも失敗した場合は fallback_category にフォールバックする。
    """
    client = get_client()
    model = get_model_name()
    categories = config.summarizer.categories
    categories_str = ", ".join(categories)
    max_length = config.summarizer.individual_max_length
    category_max_retries = config.summarizer.category_max_retries

    base_prompt = f"""以下の記事を読み、要約・キーワード・カテゴリ分類を行ってください。

【制約事項】
- 要約は出力言語を「日本語」に統一し、{max_length}文字以内で重要なポイントを端的にまとめてください。
- キーワードは記事から3個抽出してください。
- カテゴリは以下のいずれかから最も適切なものを1つ選択してください。
[{categories_str}]

【記事情報】
タイトル: {article.title}
本文:
{article.content}
"""

    parameters, extra_body = build_step_params("summarizer")

    if stream:
        logger.debug("[%s] の要約を生成中...", article.title)

    messages = [
        {"role": "system", "content": "あなたは優秀なニュース編集者です。与えられたソースから正確で簡潔な要約を生成します。"},
        {"role": "user", "content": base_prompt},
    ]
    last_result: ArticleSummary | None = None

    for attempt in range(category_max_retries + 1):
        if attempt > 0:
            logger.warning(
                "[カテゴリ再試行 %d/%d] 記事「%s」に未定義カテゴリ「%s」が返されました。再試行します。",
                attempt,
                category_max_retries,
                article.title,
                last_result.category,
            )
            messages.append({"role": "assistant", "content": last_result.model_dump_json()})
            messages.append({
                "role": "user",
                "content": (
                    f"カテゴリに「{last_result.category}」が返されましたが、これは定義されていないカテゴリです。\n"
                    f"必ず以下のカテゴリから1つだけ選択してください: [{categories_str}]"
                ),
            })

        completion_kwargs = {
            "model": model,
            "messages": messages,
            "response_format": ArticleSummary,
            **parameters
        }

        if extra_body:
            completion_kwargs["extra_body"] = extra_body

        result: ArticleSummary = call_once(client, completion_kwargs, stream)

        if result.category in categories:
            return result

        last_result = result

    # 全試行でもカテゴリが定義外のままだった場合はフォールバック
    fallback = config.summarizer.fallback_category
    logger.warning(
        "記事「%s」のカテゴリ「%s」が %d 回試行後も未定義のまま。「%s」にフォールバックします。",
        article.title,
        last_result.category,
        category_max_retries + 1,
        fallback,
    )
    last_result.category = fallback
    return last_result

