import json
import re
from openai import OpenAI
from config import config
from config import SummarizerStepConfig
from logger import get_logger

logger = get_logger(__name__)


def use_structured_output() -> bool:
    """Structured Output を使用するか（デフォルト: True）。"""
    return config.llm.structured_output


def get_client() -> OpenAI:
    """OpenAI 互換クライアントを初期化して返す"""
    return OpenAI(
        base_url=config.llm.base_url,
        api_key=config.llm.api_key,
    )


def get_model_name() -> str:
    """使用するモデル名を取得する"""
    return config.llm.model


def get_step_config(step_name: str) -> SummarizerStepConfig:
    """指定ステップの設定を返す。未定義の場合はデフォルト値を持つ SummarizerStepConfig を返す。"""
    return config.summarizer.steps.get(step_name, SummarizerStepConfig())


def build_step_params(step_name: str) -> tuple[dict, dict | None]:
    """指定ステップの LLM パラメータと extra_body を構築して返す。

    優先順位:
      ステップ固有設定 (summarizer.steps.<step>) > グローバル設定 (llm.*) の順にマージ。

    Args:
        step_name: "grouper" | "summarizer" | "digest"

    Returns:
        (parameters, extra_body) のタプル。
        parameters は completion_kwargs に ** 展開して渡す。
        extra_body は None または dict（llm.extra_body の値をそのまま使用）。
    """
    llm_cfg = config.llm
    step_cfg = get_step_config(step_name)

    # --- parameters: グローバルをベースにステップ固有でオーバーライド ---
    global_params = dict(llm_cfg.parameters)
    if step_cfg.parameters:
        parameters = {**global_params, **step_cfg.parameters}
    else:
        parameters = global_params

    # --- thinking: disable_temperature_with_thinking の判定にのみ使用 ---
    # Step-level thinking overrides the global llm.thinking
    thinking = step_cfg.thinking if step_cfg.thinking is not None else llm_cfg.thinking

    # thinking 有効時に temperature を除外するオプション
    if thinking and llm_cfg.disable_temperature_with_thinking:
        parameters.pop("temperature", None)

    # --- extra_body: 設定ファイルの llm.extra_body をそのまま使用 ---
    extra_body = llm_cfg.extra_body

    return parameters, extra_body


def _inject_thinking_token(messages: list[dict]) -> list[dict]:
    """gemma4_think: true のとき、システムプロンプト先頭に <|think|> を注入する。"""
    if not config.llm.gemma4_think:
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
    max_retries = config.llm.max_retries
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
    max_retries = config.llm.max_retries
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


def call_once(client, completion_kwargs, stream: bool = False):
    """LLM を1回だけ呼び出す（内側リトライなし）。カテゴリ検証の外側ループ用。

    カテゴリ検証では外側ループが再試行を管理するため、内側リトライは不要。
    structured_output 設定に応じてモードを切り替える点は call_with_retry と同じ。
    """
    if use_structured_output():
        return _call_structured_once(client, completion_kwargs, stream)
    else:
        return _call_plain_text_once(client, completion_kwargs, stream)


def _call_structured_once(client, completion_kwargs, stream: bool = False):
    """Structured Output モード（リトライなし）。"""
    completion_kwargs = dict(completion_kwargs)
    completion_kwargs["messages"] = _inject_thinking_token(completion_kwargs["messages"])

    if stream:
        response = stream_completion(client, completion_kwargs)
    else:
        response = client.chat.completions.parse(**completion_kwargs)

    parsed = response.choices[0].message.parsed
    if not parsed:
        raise ValueError("Failed to parse the structured output from LLM.")
    return parsed


def _call_plain_text_once(client, completion_kwargs, stream: bool = False):
    """プレーンテキストモード（リトライなし）。"""
    kwargs = dict(completion_kwargs)
    kwargs["messages"] = _inject_thinking_token(kwargs["messages"])
    model_class = kwargs.pop("response_format", None)
    if model_class is None:
        raise ValueError("response_format が指定されていません。")

    kwargs["messages"] = _inject_json_instruction(kwargs["messages"], model_class)

    if stream:
        text = stream_plain_text_completion(client, kwargs)
    else:
        response = client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""
    return _extract_json(text, model_class)


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


