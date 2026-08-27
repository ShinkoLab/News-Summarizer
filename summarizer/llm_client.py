import json
import re
from google import genai
from google.genai import types as genai_types
from openai import OpenAI
from config import config
from config import SummarizerStepConfig
from logger import get_logger

logger = get_logger(__name__)


def use_structured_output() -> bool:
    """Structured Output を使用するか（デフォルト: True）。"""
    return config.llm.structured_output


def get_client():
    """Configured provider client. Vertex AI uses Application Default Credentials."""
    if config.llm.provider == "vertex":
        return genai.Client(
            vertexai=True,
            project=config.llm.project_id,
            location=config.llm.location,
        )
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


def _log_usage(response, context: str = "") -> None:
    """レスポンスのトークン使用量を DEBUG レベルで出力する。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    # Chat Completions は prompt_tokens/completion_tokens、Responses API は
    # input_tokens/output_tokens と命名が異なる。
    prompt = getattr(usage, "prompt_tokens", None)
    if prompt is None:
        prompt = getattr(usage, "input_tokens", "?")
    completion = getattr(usage, "completion_tokens", None)
    if completion is None:
        completion = getattr(usage, "output_tokens", "?")
    total = getattr(usage, "total_tokens", "?")
    prefix = f"[{context}] " if context else ""
    logger.debug("%sToken usage — prompt: %s, completion: %s, total: %s", prefix, prompt, completion, total)


def call_with_retry(client, completion_kwargs, stream: bool = False):
    """LLM を呼び出し、パース失敗時は max_retries 回まで再試行する。

    structured_output 設定に応じて Structured Output モードとプレーンテキストモードを切り替える。
    パース済みオブジェクトを返す。全試行失敗時は最後の例外を再送出する。
    """
    if config.llm.provider == "vertex":
        return _call_vertex_with_retry(client, completion_kwargs, stream)
    if config.llm.provider == "openai_responses":
        if use_structured_output():
            return _call_responses_structured_with_retry(client, completion_kwargs, stream)
        return _call_responses_plain_text_with_retry(client, completion_kwargs, stream)
    if use_structured_output():
        return _call_structured_with_retry(client, completion_kwargs, stream)
    else:
        return _call_plain_text_with_retry(client, completion_kwargs, stream)


def _call_vertex_with_retry(client, completion_kwargs, stream: bool = False):
    """Call Vertex AI through Google Gen AI SDK and validate the JSON schema."""
    max_retries = config.llm.max_retries
    kwargs = dict(completion_kwargs)
    model_class = kwargs.pop("response_format")
    model = kwargs.pop("model")
    messages = _inject_thinking_token(kwargs.pop("messages"))
    kwargs.pop("extra_body", None)

    system_parts: list[str] = []
    contents: list[genai_types.Content] = []
    for message in messages:
        role = message.get("role")
        content = str(message.get("content", ""))
        if role == "system":
            system_parts.append(content)
            continue
        contents.append(
            genai_types.Content(
                role="model" if role == "assistant" else "user",
                parts=[genai_types.Part.from_text(text=content)],
            )
        )

    generation_args: dict = {
        "system_instruction": "\n\n".join(system_parts) or None,
        "response_mime_type": "application/json",
        "response_schema": model_class,
    }
    parameter_mapping = {
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "max_tokens": "max_output_tokens",
        "max_output_tokens": "max_output_tokens",
        "seed": "seed",
        "stop": "stop_sequences",
    }
    for source_name, target_name in parameter_mapping.items():
        if source_name in kwargs:
            generation_args[target_name] = kwargs[source_name]

    reasoning_effort = kwargs.get("reasoning_effort")
    if reasoning_effort:
        generation_args["thinking_config"] = genai_types.ThinkingConfig(
            thinking_level=str(reasoning_effort).upper()
        )

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.warning("[再試行 %d/%d] Vertex AI生成を再試行します...", attempt, max_retries)
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(**generation_args),
            )
            if stream and response.text:
                print(response.text, flush=True)
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, model_class):
                return parsed
            if parsed is not None:
                return model_class.model_validate(parsed)
            if not response.text:
                raise ValueError("Vertex AIから空の応答が返されました。")
            return model_class.model_validate_json(response.text)
        except Exception as e:
            last_error = e
            logger.warning(
                "Vertex AI生成エラー (試行 %d/%d): %s",
                attempt + 1,
                max_retries + 1,
                e,
                exc_info=True,
            )

    raise last_error


def _retry_loop(call_fn):
    """max_retries 回まで call_fn() を再試行する共通ハーネス。

    call_fn は成功時に結果を return し、失敗（パース失敗含む）時は例外を送出する。
    全試行失敗時は最後の例外を再送出する。4つの *_with_retry 関数（Chat
    Completions / Responses API × Structured Output / プレーンテキスト）で共有する。
    """
    max_retries = config.llm.max_retries
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.warning("[再試行 %d/%d] LLM 生成を再試行します...", attempt, max_retries)
        try:
            return call_fn()
        except Exception as e:
            last_error = e
            logger.warning("LLM 生成エラー (試行 %d/%d): %s", attempt + 1, max_retries + 1, e, exc_info=True)

    raise last_error


def _call_structured_with_retry(client, completion_kwargs, stream: bool = False):
    """Structured Output モード: response_format に Pydantic モデルを渡して parse する。"""
    completion_kwargs = dict(completion_kwargs)
    completion_kwargs["messages"] = _inject_thinking_token(completion_kwargs["messages"])

    def call():
        if stream:
            response = stream_completion(client, completion_kwargs)
        else:
            response = client.chat.completions.parse(**completion_kwargs)

        parsed = response.choices[0].message.parsed
        if not parsed:
            raise ValueError("Failed to parse the structured output from LLM.")
        if not stream:
            _log_usage(response, completion_kwargs.get("model", ""))
        return parsed

    return _retry_loop(call)


def _call_plain_text_with_retry(client, completion_kwargs, stream: bool = False):
    """プレーンテキストモード: response_format なしで呼び出し、JSON を手動パースする。"""
    # response_format からモデルクラスを取り出し、kwargs から除去
    kwargs = dict(completion_kwargs)
    kwargs["messages"] = _inject_thinking_token(kwargs["messages"])
    model_class = kwargs.pop("response_format", None)
    if model_class is None:
        raise ValueError("response_format が指定されていません。")

    # JSON 出力指示をプロンプトに注入
    kwargs["messages"] = _inject_json_instruction(kwargs["messages"], model_class)

    def call():
        if stream:
            text = stream_plain_text_completion(client, kwargs)
        else:
            response = client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content or ""
            _log_usage(response, kwargs.get("model", ""))
        return _extract_json(text, model_class)

    return _retry_loop(call)


def _build_responses_kwargs(completion_kwargs: dict) -> tuple[dict, type | None]:
    """Chat Completions 形式の completion_kwargs を Responses API 形式に変換する。

    Responses API は messages の代わりに input、max_tokens の代わりに
    max_output_tokens、reasoning_effort の代わりに reasoning={"effort": ...} を使う。
    extra_body（Ollama 専用パラメータ）は Responses API では未対応のため落とす。
    """
    kwargs = dict(completion_kwargs)
    kwargs.pop("extra_body", None)
    kwargs["input"] = _inject_thinking_token(kwargs.pop("messages"))
    if "max_tokens" in kwargs:
        kwargs["max_output_tokens"] = kwargs.pop("max_tokens")
    if "reasoning_effort" in kwargs:
        kwargs["reasoning"] = {"effort": kwargs.pop("reasoning_effort")}
    model_class = kwargs.pop("response_format", None)
    return kwargs, model_class


def _call_responses_structured_with_retry(client, completion_kwargs, stream: bool = False):
    """Responses API の Structured Output（text_format）モード。"""
    kwargs, model_class = _build_responses_kwargs(completion_kwargs)
    if model_class is None:
        raise ValueError("response_format が指定されていません。")
    kwargs["text_format"] = model_class

    def call():
        if stream:
            response = _stream_responses_structured(client, kwargs)
        else:
            response = client.responses.parse(**kwargs)
            _log_usage(response, kwargs.get("model", ""))
        parsed = response.output_parsed
        if not parsed:
            raise ValueError("Failed to parse the structured output from LLM.")
        return parsed

    return _retry_loop(call)


def _call_responses_plain_text_with_retry(client, completion_kwargs, stream: bool = False):
    """Responses API のプレーンテキストモード: response_format なしで呼び出し、JSON を手動パースする。"""
    kwargs, model_class = _build_responses_kwargs(completion_kwargs)
    if model_class is None:
        raise ValueError("response_format が指定されていません。")
    kwargs["input"] = _inject_json_instruction(kwargs["input"], model_class)

    def call():
        if stream:
            text = _stream_responses_plain_text(client, kwargs)
        else:
            response = client.responses.create(**kwargs)
            text = response.output_text or ""
            _log_usage(response, kwargs.get("model", ""))
        return _extract_json(text, model_class)

    return _retry_loop(call)


def _consume_delta_stream(events, get_reasoning_delta, get_content_delta) -> str:
    """reasoning/content delta を持つイベント列を消費し、thinking表示を切り替えつつ全文を返す。

    Chat Completions と Responses API はイベント形状が異なるため、呼び出し側が
    各イベントから reasoning/content のテキスト断片を取り出す関数を渡す。
    reasoning フィールドが存在する場合は「--- [Thinking] ---」ブロックとして表示する。
    """
    thinking_active = False
    content_parts: list[str] = []

    for event in events:
        # reasoning/content は Chat Completions の生チャンクでは同一デルタに両方
        # 乗ることがある（thinking→回答の切り替わり）ため、continue で早期終了せず
        # 両方を毎回チェックする。Responses API 等イベント型が別れている場合は
        # 片方が常に None を返すだけで実害はない。
        reasoning = get_reasoning_delta(event)
        if reasoning:
            if not thinking_active:
                print("\n--- [Thinking] ---", flush=True)
                thinking_active = True
            print(reasoning, end="", flush=True)
        content = get_content_delta(event)
        if content:
            if thinking_active:
                print("\n--- [/Thinking] ---\n", flush=True)
                thinking_active = False
            print(content, end="", flush=True)
            content_parts.append(content)

    if thinking_active:
        print("\n--- [/Thinking] ---\n", flush=True)
    print("\n", flush=True)
    return "".join(content_parts)


def _responses_reasoning_delta(event) -> str | None:
    return event.delta if event.type == "response.reasoning_text.delta" else None


def _responses_content_delta(event) -> str | None:
    return event.delta if event.type == "response.output_text.delta" else None


def _stream_responses_structured(client, kwargs):
    """Structured Output ストリーミング（Responses API）。最終的な response オブジェクトを返す。"""
    with client.responses.stream(**kwargs) as stream_ctx:
        _consume_delta_stream(stream_ctx, _responses_reasoning_delta, _responses_content_delta)
        final = stream_ctx.get_final_response()
        _log_usage(final, kwargs.get("model", ""))
        return final


def _stream_responses_plain_text(client, kwargs) -> str:
    """プレーンテキストストリーミング（Responses API）。出力しながら全文を返す。"""
    with client.responses.create(stream=True, **kwargs) as stream:
        return _consume_delta_stream(stream, _responses_reasoning_delta, _responses_content_delta)


def stream_plain_text_completion(client, completion_kwargs) -> str:
    """ストリーミングでプレーンテキスト補完を実行し、出力しながら全文を返す。"""

    def get_reasoning(chunk) -> str | None:
        if not chunk.choices:
            return None
        return getattr(chunk.choices[0].delta, "reasoning", None)

    def get_content(chunk) -> str | None:
        return chunk.choices[0].delta.content if chunk.choices else None

    with client.chat.completions.create(stream=True, **completion_kwargs) as stream:
        return _consume_delta_stream(stream, get_reasoning, get_content)


def stream_completion(client, completion_kwargs):
    """ストリーミングで LLM 補完を実行し、thinking / content を標準出力に流す。

    最終的な completion オブジェクトを返す。
    """

    def get_reasoning(event) -> str | None:
        if event.type != "chunk" or not event.chunk.choices or not event.chunk.choices[0].delta:
            return None
        return getattr(event.chunk.choices[0].delta, "reasoning", None)

    def get_content(event) -> str | None:
        return event.delta if event.type == "content.delta" else None

    with client.chat.completions.stream(**completion_kwargs) as stream_ctx:
        _consume_delta_stream(stream_ctx, get_reasoning, get_content)
        final = stream_ctx.get_final_completion()
        _log_usage(final, completion_kwargs.get("model", ""))
        return final

