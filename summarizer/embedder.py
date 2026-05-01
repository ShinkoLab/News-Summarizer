import time

import numpy as np
from openai import OpenAI

from config import config
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
    llm_cfg = config.llm
    embedding_model = llm_cfg.embedding_model
    if not embedding_model:
        raise ValueError("llm.embedding_model が設定されていません。")

    if debug:
        logger.debug("Embedding モデル: %s, 入力: %d件", embedding_model, len(texts))

    client = OpenAI(
        base_url=llm_cfg.base_url,
        api_key=llm_cfg.api_key,
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
