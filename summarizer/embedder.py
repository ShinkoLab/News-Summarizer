import time

import numpy as np
from openai import OpenAI

from summarizer.llm_client import _get_llm_config
from logger import get_logger

logger = get_logger(__name__)


def get_embeddings(texts: list[str], debug: bool = False) -> np.ndarray:
    """テキストのリストをまとめて embedding ベクトルに変換して返す。

    llm.embedding_model に設定されたモデルを使用する。
    base_url / api_key は llm 設定を流用（Ollama 共通エンドポイント）。

    Args:
        texts: embedding 対象のテキストリスト
        debug: True のとき詳細なデバッグ情報を標準出力に表示する

    Returns:
        shape (len(texts), embedding_dim) の numpy 配列

    Raises:
        ValueError: llm.embedding_model が未設定の場合
    """
    llm_config = _get_llm_config()
    embedding_model = llm_config.get("embedding_model")
    if not embedding_model:
        raise ValueError("llm.embedding_model が設定されていません。")

    if debug:
        logger.debug("Embedding モデル: %s, 入力: %d件", embedding_model, len(texts))

    client = OpenAI(
        base_url=llm_config["base_url"],
        api_key=llm_config.get("api_key", "ollama"),
    )

    start = time.perf_counter()
    response = client.embeddings.create(
        model=embedding_model,
        input=texts,
    )
    elapsed = time.perf_counter() - start

    embeddings = np.array([item.embedding for item in response.data])

    if debug:
        logger.debug(
            "Embedding 完了: 次元=%d, 所要時間=%.2fs",
            embeddings.shape[1],
            elapsed,
        )

    return embeddings
