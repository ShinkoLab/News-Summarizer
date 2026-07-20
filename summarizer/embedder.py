import time

import numpy as np
from google import genai
from google.genai import types as genai_types
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

    start = time.perf_counter()
    if llm_cfg.provider == "vertex":
        client = genai.Client(
            vertexai=True,
            project=llm_cfg.project_id,
            location=llm_cfg.location,
        )
        response = client.models.embed_content(
            model=embedding_model,
            contents=texts,
            config=genai_types.EmbedContentConfig(task_type="CLUSTERING"),
        )
        embeddings = np.array([item.values for item in response.embeddings])
    else:
        client = OpenAI(
            base_url=llm_cfg.base_url,
            api_key=llm_cfg.api_key,
        )
        response = client.embeddings.create(
            model=embedding_model,
            input=texts,
        )
        embeddings = np.array([item.embedding for item in response.data])
    elapsed = time.perf_counter() - start

    if debug:
        logger.debug(
            "Embedding 完了: 次元=%d, 所要時間=%.2fs",
            embeddings.shape[1],
            elapsed,
        )

    return embeddings
