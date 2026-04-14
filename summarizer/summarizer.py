from models import Article, ArticleSummary
from summarizer.llm_client import get_client, get_model_name, build_step_params, call_with_retry
from config import config
from logger import get_logger

logger = get_logger(__name__)

def summarize_article(article: Article, stream: bool = False) -> ArticleSummary:
    """
    記事の内容を読み、要約・キーワード・カテゴリを抽出する。
    LLM による Structured Output を使用して ArticleSummary を生成する。
    """
    client = get_client()
    model = get_model_name()
    categories = ", ".join(config.summarizer.categories)
    max_length = config.summarizer.individual_max_length

    prompt = f"""以下の記事を読み、要約・キーワード・カテゴリ分類を行ってください。

【制約事項】
- 要約は出力言語を「日本語」に統一し、{max_length}文字以内で重要なポイントを端的にまとめてください。
- キーワードは記事から3個抽出してください。
- カテゴリは以下のいずれかから最も適切なものを1つ選択してください。
[{categories}]

【記事情報】
タイトル: {article.title}
本文:
{article.content}
"""

    parameters, extra_body = build_step_params("summarizer")

    completion_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたは優秀なニュース編集者です。与えられたソースから正確で簡潔な要約を生成します。"},
            {"role": "user", "content": prompt}
        ],
        "response_format": ArticleSummary,
        **parameters
    }

    if extra_body:
        completion_kwargs["extra_body"] = extra_body

    if stream:
        logger.debug("[%s] の要約を生成中...", article.title)

    return call_with_retry(client, completion_kwargs, stream)

