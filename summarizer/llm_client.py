import json
import re
from openai import OpenAI
from config import config
from logger import get_logger

logger = get_logger(__name__)


def _get_llm_config() -> dict:
    """LLM 設定を取得する。`llm` キーを優先し、後方互換として `ollama` にフォールバック。"""
    return config.get("llm") or config.get("ollama", {})


def use_structured_output() -> bool:
    """Structured Output を使用するか（デフォルト: True）。"""
    return _get_llm_config().get("structured_output", True)


def get_client() -> OpenAI:
    """OpenAI 互換クライアントを初期化して返す"""
    llm_config = _get_llm_config()
    return OpenAI(
        base_url=llm_config["base_url"],
        api_key=llm_config.get("api_key", "ollama"),
    )


def get_model_name() -> str:
    """使用するモデル名を取得する"""
    return _get_llm_config()["model"]


def get_step_config(step_name: str) -> dict:
    """指定ステップの設定辞書を返す（parameters 以外のフィールドも含む）。"""
    summarizer_config = config.get("summarizer") or {}
    steps_config = summarizer_config.get("steps") or {}
    return steps_config.get(step_name) or {}


def build_step_params(step_name: str) -> tuple[dict, dict | None]:
    """指定ステップの LLM パラメータと extra_body を構築して返す。

    優先順位:
      ステップ固有設定 (summarizer.steps.<step>) > グローバル設定 (llm.*) の順にマージ。

    後方互換:
      summarizer.steps が未設定の場合は summarizer.<step>_thinking フラットキーにフォールバック。
      設定キーは `llm` を優先し、`ollama` にフォールバック。

    Args:
        step_name: "grouper" | "summarizer" | "digest"

    Returns:
        (parameters, extra_body) のタプル。
        parameters は completion_kwargs に ** 展開して渡す。
        extra_body は None または dict（llm.extra_body の値をそのまま使用）。
    """
    llm_config = _get_llm_config()
    summarizer_config = config.get("summarizer") or {}
    steps_config = summarizer_config.get("steps") or {}
    step_config = steps_config.get(step_name) or {}

    # --- parameters: グローバルをベースにステップ固有でオーバーライド ---
    global_params = dict(llm_config.get("parameters") or {})
    step_params_override = step_config.get("parameters", None)
    if step_params_override is not None:
        parameters = {**global_params, **step_params_override}
    else:
        parameters = global_params

    # --- thinking: disable_temperature_with_thinking の判定にのみ使用 ---
    legacy_key = f"{step_name}_thinking"
    thinking = step_config.get(
        "thinking",
        summarizer_config.get(legacy_key, llm_config.get("thinking", None))
    )

    # thinking 有効時に temperature を除外するオプション
    if thinking and llm_config.get("disable_temperature_with_thinking", False):
        parameters.pop("temperature", None)

    # --- extra_body: 設定ファイルの llm.extra_body をそのまま使用 ---
    extra_body = llm_config.get("extra_body", None)

    return parameters, extra_body


def _inject_thinking_token(messages: list[dict]) -> list[dict]:
    """gemma4_think: true のとき、システムプロンプト先頭に <|think|> を注入する。"""
    llm_config = _get_llm_config()
    if not llm_config.get("gemma4_think", False):
        return messages
    messages = list(messages)
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            m = dict(msg)
            if not m["content"].startswith("<|think|>"):
                m["content"] = "<|think|>\n" + m["content"]
            messages[i] = m
            break
    return messages


def _inject_json_instruction(messages: list[dict], model_class) -> list[dict]:
    """プロンプトの末尾に JSON スキーマ出力指示を追加する。"""
    schema = model_class.model_json_schema()
    instruction = (
        "\n\n以下の JSON スキーマに従い、JSON のみを出力してください。"
        "必ず ```json ... ``` のコードブロックで囲んでください。余分な説明文は不要です。\n"
        f"スキーマ:\n```json\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n```"
    )
    messages = list(messages)
    last = dict(messages[-1])
    last["content"] = last["content"] + instruction
    messages[-1] = last
    return messages


def _extract_json(text: str, model_class):
    """プレーンテキストの応答から JSON を抽出して Pydantic モデルとしてパースする。"""
    # <think>...</think> ブロックを除去（Ollama thinking）
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    # ```json ... ``` ブロックを優先して抽出
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if match:
        json_str = match.group(1).strip()
    else:
        # フォールバック1: 途中で切れた ```json ブロック（閉じる ``` がない）
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*)", text)
        if match:
            json_str = match.group(1).strip()
        else:
            # フォールバック2: 生の JSON オブジェクトを探す
            match = re.search(r"(\{[\s\S]*\})", text)
            if match:
                json_str = match.group(1)
            else:
                raise ValueError(f"JSON が見つかりませんでした。応答の冒頭: {text[:300]}")
    return model_class.model_validate_json(json_str)


def call_with_retry(client, completion_kwargs, stream: bool = False):
    """LLM を呼び出し、パース失敗時は max_retries 回まで再試行する。

    structured_output 設定に応じて Structured Output モードとプレーンテキストモードを切り替える。
    パース済みオブジェクトを返す。全試行失敗時は最後の例外を再送出する。
    """
    if use_structured_output():
        return _call_structured_with_retry(client, completion_kwargs, stream)
    else:
        return _call_plain_text_with_retry(client, completion_kwargs, stream)


def _call_structured_with_retry(client, completion_kwargs, stream: bool = False):
    """Structured Output モード: response_format に Pydantic モデルを渡して parse する。"""
    max_retries = _get_llm_config().get("max_retries", 3)
    last_error: Exception | None = None

    completion_kwargs = dict(completion_kwargs)
    completion_kwargs["messages"] = _inject_thinking_token(completion_kwargs["messages"])

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.warning("[再試行 %d/%d] LLM 生成を再試行します...", attempt, max_retries)
        try:
            if stream:
                response = stream_completion(client, completion_kwargs)
            else:
                response = client.chat.completions.parse(**completion_kwargs)

            parsed = response.choices[0].message.parsed
            if not parsed:
                raise ValueError("Failed to parse the structured output from LLM.")
            return parsed
        except Exception as e:
            last_error = e
            logger.warning("LLM 生成エラー (試行 %d/%d): %s", attempt + 1, max_retries + 1, e, exc_info=True)

    raise last_error


def _call_plain_text_with_retry(client, completion_kwargs, stream: bool = False):
    """プレーンテキストモード: response_format なしで呼び出し、JSON を手動パースする。"""
    max_retries = _get_llm_config().get("max_retries", 3)
    last_error: Exception | None = None

    # response_format からモデルクラスを取り出し、kwargs から除去
    kwargs = dict(completion_kwargs)
    kwargs["messages"] = _inject_thinking_token(kwargs["messages"])
    model_class = kwargs.pop("response_format", None)
    if model_class is None:
        raise ValueError("response_format が指定されていません。")

    # JSON 出力指示をプロンプトに注入
    kwargs["messages"] = _inject_json_instruction(kwargs["messages"], model_class)

    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.warning("[再試行 %d/%d] LLM 生成を再試行します...", attempt, max_retries)
        try:
            if stream:
                text = stream_plain_text_completion(client, kwargs)
            else:
                response = client.chat.completions.create(**kwargs)
                text = response.choices[0].message.content or ""
            return _extract_json(text, model_class)
        except Exception as e:
            last_error = e
            logger.warning("LLM 生成エラー (試行 %d/%d): %s", attempt + 1, max_retries + 1, e, exc_info=True)

    raise last_error


def stream_plain_text_completion(client, completion_kwargs) -> str:
    """ストリーミングでプレーンテキスト補完を実行し、出力しながら全文を返す。"""
    thinking_active = False
    content_parts: list[str] = []

    with client.chat.completions.create(stream=True, **completion_kwargs) as stream:
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            rc = getattr(delta, "reasoning", None)
            if rc:
                if not thinking_active:
                    print("\n--- [Thinking] ---", flush=True)
                    thinking_active = True
                print(rc, end="", flush=True)
            if delta.content:
                if thinking_active:
                    print("\n--- [/Thinking] ---\n", flush=True)
                    thinking_active = False
                print(delta.content, end="", flush=True)
                content_parts.append(delta.content)

    if thinking_active:
        print("\n--- [/Thinking] ---\n", flush=True)
    print("\n", flush=True)
    return "".join(content_parts)


def stream_completion(client, completion_kwargs):
    """ストリーミングで LLM 補完を実行し、thinking / content を標準出力に流す。

    reasoning フィールドが存在する場合は「--- [Thinking] ---」ブロックとして表示する。
    最終的な completion オブジェクトを返す。
    """
    thinking_active = False

    with client.chat.completions.stream(**completion_kwargs) as stream_ctx:
        for event in stream_ctx:
            if event.type == "chunk":
                chunk = event.chunk
                if chunk.choices and chunk.choices[0].delta:
                    delta = chunk.choices[0].delta
                    rc = getattr(delta, "reasoning", None)
                    if rc:
                        if not thinking_active:
                            print("\n--- [Thinking] ---", flush=True)
                            thinking_active = True
                        print(rc, end="", flush=True)
            elif event.type == "content.delta":
                if thinking_active and event.delta:
                    print("\n--- [/Thinking] ---\n", flush=True)
                    thinking_active = False
                if event.delta:
                    print(event.delta, end="", flush=True)

        if thinking_active:
            print("\n--- [/Thinking] ---\n", flush=True)
        print("\n", flush=True)
        return stream_ctx.get_final_completion()


