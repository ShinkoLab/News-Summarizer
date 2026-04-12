from collections import defaultdict
from typing import List

import numpy as np
from sklearn.cluster import AgglomerativeClustering

from models import Article, ArticleGroup, ArticleSummary, GroupingResult, TopicNamingResult
from summarizer.llm_client import (
    build_step_params,
    call_with_retry,
    get_client,
    get_model_name,
    get_step_config,
)
from logger import get_logger

logger = get_logger(__name__)


def group_articles(articles: List[Article], stream: bool = False) -> GroupingResult:
    """記事リストをグルーピングする。

    grouper.use_embeddings が true かつ llm.embedding_model が設定されている場合は
    embedding ベースのグルーピングを試みる。それ以外は LLM による直接グルーピング。
    """
    if not articles:
        return GroupingResult(groups=[])

    step_cfg = get_step_config("grouper")
    use_embeddings = step_cfg.get("use_embeddings", False)

    from summarizer.llm_client import _get_llm_config
    embedding_model = _get_llm_config().get("embedding_model")

    if use_embeddings and embedding_model:
        from summarizer.embedder import get_embeddings
        texts = [article.title for article in articles]
        embeddings = get_embeddings(texts)
        # ArticleSummary の代わりにタイトルだけで命名
        dummy_summaries = [type("S", (), {"title": a.title})() for a in articles]
        return _group_with_embeddings(dummy_summaries, embeddings, step_cfg, stream)

    return _group_with_llm(articles, stream)


def group_summaries(
    summaries: List[ArticleSummary],
    embeddings: np.ndarray,
    stream: bool = False,
    debug: bool = False,
) -> GroupingResult:
    """要約済み記事リストを embedding ベースでグルーピングする。

    パイプライン再構成後のメインエントリポイント。
    Summarize ステップ後に呼び出す。

    Args:
        summaries: 個別要約済みの ArticleSummary リスト
        embeddings: get_embeddings() で取得した shape (N, D) の numpy 配列
        stream: LLM 出力ストリーミング表示フラグ
        debug: True のとき詳細なデバッグ情報を標準出力に表示する

    Returns:
        GroupingResult
    """
    if not summaries:
        return GroupingResult(groups=[])

    step_cfg = get_step_config("grouper")
    return _group_with_embeddings(summaries, embeddings, step_cfg, stream, debug)


def _group_with_embeddings(
    summaries, embeddings: np.ndarray, step_cfg: dict, stream: bool, debug: bool = False
) -> GroupingResult:
    """embedding + AgglomerativeClustering でグルーピングし、LLM でトピック命名する。"""
    from sklearn.metrics.pairwise import cosine_similarity as _cosine_similarity

    similarity_threshold = step_cfg.get("similarity_threshold", 0.85)
    distance_threshold = 1.0 - similarity_threshold

    if stream:
        logger.debug("記事のグループ化（embedding）を計算中...")

    if debug:
        logger.debug(
            "Clustering 閾値: similarity=%s, distance=%.3f, linkage=average",
            similarity_threshold,
            distance_threshold,
        )

    clustering = AgglomerativeClustering(
        n_clusters=None,
        metric="cosine",
        linkage="average",
        distance_threshold=distance_threshold,
    )
    labels = clustering.fit_predict(embeddings)

    # ラベル → 記事インデックスのマッピング
    clusters: dict[int, list[int]] = defaultdict(list)
    for idx, label in enumerate(labels):
        clusters[int(label)].append(idx)

    if debug:
        n_clusters = len(clusters)
        logger.debug(
            "Clustering 結果: %d件 → %dクラスタ",
            len(summaries),
            n_clusters,
        )
        # 全 embedding のコサイン類似度行列（クラスタ内最小類似度の表示に使用）
        sim_matrix = _cosine_similarity(embeddings)
        for label, indices in sorted(clusters.items()):
            titles = [f'"{summaries[i].title}"' for i in indices]
            titles_str = ", ".join(titles)
            if len(indices) >= 2:
                intra_sims = [
                    sim_matrix[i][j]
                    for ii, i in enumerate(indices)
                    for j in indices[ii + 1 :]
                ]
                min_sim = min(intra_sims)
                max_sim = max(intra_sims)
                logger.debug(
                    "  クラスタ%3d (%d件) [類似度 min=%.3f max=%.3f]: %s",
                    label, len(indices), min_sim, max_sim, titles_str,
                )
            else:
                logger.debug("  クラスタ%3d (1件): %s", label, titles_str)

    # LLM でトピック名を一括付与
    topic_map = _name_clusters(clusters, summaries, stream, debug)

    groups = [
        ArticleGroup(
            group_id=cluster_id,
            topic=topic_map.get(label, summaries[indices[0]].title[:15]),
            article_indices=indices,
        )
        for cluster_id, (label, indices) in enumerate(clusters.items())
    ]
    return GroupingResult(groups=groups)


def _name_clusters(
    clusters: dict[int, list[int]], summaries, stream: bool, debug: bool = False
) -> dict[int, str]:
    """クラスタごとのトピック名を LLM に一括で付与させる。

    Args:
        clusters: cluster_label → article_indices のマッピング
        summaries: ArticleSummary（または .title を持つオブジェクト）のリスト
        debug: True のとき命名結果を標準出力に表示する

    Returns:
        cluster_label → topic 文字列のマッピング
    """
    cluster_info = ""
    for label, indices in clusters.items():
        titles = [summaries[i].title for i in indices]
        titles_str = ", ".join(f'"{t}"' for t in titles)
        cluster_info += f"グループ{label}: [{titles_str}]\n"

    prompt = f"""以下のグループごとに、記事群を一言で表すトピック名を付けてください。
- トピック名は日本語で15文字以内にしてください。
- グループIDはそのまま使用してください。

{cluster_info}"""

    client = get_client()
    model = get_model_name()
    parameters, extra_body = build_step_params("grouper")

    completion_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたは優秀なニュース分析アシスタントです。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": TopicNamingResult,
        **parameters,
    }
    if extra_body:
        completion_kwargs["extra_body"] = extra_body

    result = call_with_retry(client, completion_kwargs, stream)
    topic_map = {t.group_id: t.topic for t in result.topics}

    if debug:
        for label, indices in sorted(clusters.items()):
            topic = topic_map.get(label)
            fallback = summaries[indices[0]].title[:15]
            if topic:
                logger.debug('  クラスタ%3d → "%s"', label, topic)
            else:
                logger.debug('  クラスタ%3d → "%s"（フォールバック）', label, fallback)

    return topic_map


def _group_with_llm(articles: List[Article], stream: bool) -> GroupingResult:
    """LLM に全記事を渡して直接グルーピングする（従来実装）。"""
    client = get_client()
    model = get_model_name()

    articles_info = ""
    for i, article in enumerate(articles):
        content_preview = article.content[:300].replace('\n', ' ')
        articles_info += f"[{i}] タイトル: {article.title}\n本文冒頭: {content_preview}...\n\n"

    prompt = f"""以下の記事一覧を読み、同じニュース・イベントを扱っている記事を同一グループにまとめてください。
グループ化の基準：
- 全く異なる話題の記事は、それぞれ1つのグループとして独立させてください。
- トピック名は日本語で短く（15文字程度）設定してください。

【記事一覧】
{articles_info}
"""

    parameters, extra_body = build_step_params("grouper")

    completion_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたは優秀なニュース分析アシスタントです。記事の内容を比較し、同一トピックごとに適切に分類します。"},
            {"role": "user", "content": prompt},
        ],
        "response_format": GroupingResult,
        **parameters,
    }
    if extra_body:
        completion_kwargs["extra_body"] = extra_body

    if stream:
        logger.debug("記事のグループ化を計算中...")

    return call_with_retry(client, completion_kwargs, stream)
