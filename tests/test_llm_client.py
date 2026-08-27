"""Tests for summarizer/llm_client.py pure/deterministic helpers."""

from __future__ import annotations

import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import config as config_module
from config import AppConfig, LLMConfig, SummarizerConfig, SummarizerStepConfig, reload_config
from models import ArticleSummary


# ---------------------------------------------------------------------------
# Helper to patch the module-level config used inside llm_client
# ---------------------------------------------------------------------------

def _make_cfg(
    parameters: dict | None = None,
    structured_output: bool = True,
    thinking: bool | None = None,
    disable_temperature_with_thinking: bool = False,
    steps: dict | None = None,
) -> AppConfig:
    return AppConfig(
        llm=LLMConfig(
            model="test-model",
            parameters=parameters or {},
            structured_output=structured_output,
            thinking=thinking,
            disable_temperature_with_thinking=disable_temperature_with_thinking,
        ),
        summarizer=SummarizerConfig(steps=steps or {}),
    )


# ---------------------------------------------------------------------------
# build_step_params — parameter merging
# ---------------------------------------------------------------------------

class TestBuildStepParams:
    def test_global_params_returned_when_no_step_override(self):
        cfg = _make_cfg(parameters={"temperature": 0.5, "max_tokens": 1024})
        with patch.object(config_module, "config", cfg):
            # Re-import so it picks up the patched config
            import importlib
            import summarizer.llm_client as llm_client
            importlib.reload(llm_client)
            # patch the module attribute directly
            with patch.object(llm_client, "config", cfg):
                params, extra_body = llm_client.build_step_params("digest")
        assert params["temperature"] == 0.5
        assert params["max_tokens"] == 1024
        assert extra_body is None

    def test_step_override_wins_on_conflict(self):
        steps = {
            "summarizer": SummarizerStepConfig(parameters={"temperature": 0.1})
        }
        cfg = _make_cfg(parameters={"temperature": 0.5, "max_tokens": 1024}, steps=steps)

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            params, _ = llm_client.build_step_params("summarizer")

        assert params["temperature"] == 0.1  # step wins
        assert params["max_tokens"] == 1024  # global inherited

    def test_step_only_key_merged_in(self):
        steps = {
            "grouper": SummarizerStepConfig(parameters={"top_p": 0.9})
        }
        cfg = _make_cfg(parameters={"temperature": 0.3}, steps=steps)

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            params, _ = llm_client.build_step_params("grouper")

        assert params["temperature"] == 0.3  # from global
        assert params["top_p"] == 0.9  # from step

    def test_no_step_config_returns_global_only(self):
        cfg = _make_cfg(parameters={"temperature": 0.7})

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            params, _ = llm_client.build_step_params("nonexistent_step")

        assert params == {"temperature": 0.7}


# ---------------------------------------------------------------------------
# disable_temperature_with_thinking
# ---------------------------------------------------------------------------

class TestDisableTemperatureWithThinking:
    def test_temperature_removed_when_thinking_enabled(self):
        cfg = _make_cfg(
            parameters={"temperature": 0.5, "max_tokens": 512},
            thinking=True,
            disable_temperature_with_thinking=True,
        )

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            params, _ = llm_client.build_step_params("summarizer")

        assert "temperature" not in params
        assert params["max_tokens"] == 512

    def test_temperature_kept_when_thinking_false(self):
        cfg = _make_cfg(
            parameters={"temperature": 0.5},
            thinking=False,
            disable_temperature_with_thinking=True,
        )

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            params, _ = llm_client.build_step_params("summarizer")

        assert params["temperature"] == 0.5

    def test_temperature_kept_when_flag_off(self):
        cfg = _make_cfg(
            parameters={"temperature": 0.5},
            thinking=True,
            disable_temperature_with_thinking=False,
        )

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            params, _ = llm_client.build_step_params("summarizer")

        assert params["temperature"] == 0.5

    def test_step_level_thinking_overrides_global(self):
        """Step thinking=True + disable_temperature_with_thinking removes temperature."""
        steps = {
            "digest": SummarizerStepConfig(
                thinking=True,
                parameters={"temperature": 0.8},
            )
        }
        cfg = _make_cfg(
            parameters={"temperature": 0.5},
            thinking=False,  # global thinking off
            disable_temperature_with_thinking=True,
            steps=steps,
        )

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            params, _ = llm_client.build_step_params("digest")

        assert "temperature" not in params


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    """_extract_json must handle various LLM output formats."""

    def setup_method(self):
        import summarizer.llm_client as llm_client
        self.extract = llm_client._extract_json

    def test_fenced_json_block(self):
        text = textwrap.dedent("""\
            Here is the output:
            ```json
            {"title": "テスト", "summary": "概要", "keywords": ["a", "b", "c"], "category": "テクノロジー"}
            ```
        """)
        result = self.extract(text, ArticleSummary)
        assert result.title == "テスト"

    def test_bare_json_object(self):
        text = '{"title": "タイトル", "summary": "要約", "keywords": ["x"], "category": "経済・ビジネス"}'
        result = self.extract(text, ArticleSummary)
        assert result.category == "経済・ビジネス"

    def test_think_block_stripped(self):
        text = textwrap.dedent("""\
            <think>Let me think about this carefully...</think>
            ```json
            {"title": "考えた結果", "summary": "要約文", "keywords": ["k1"], "category": "科学・環境"}
            ```
        """)
        result = self.extract(text, ArticleSummary)
        assert result.title == "考えた結果"

    def test_truncated_fence_no_closing_backticks(self):
        """Fallback: fenced block that never closes (LLM truncated)."""
        text = '```json\n{"title": "途中", "summary": "要約", "keywords": ["a", "b", "c"], "category": "未分類"}'
        result = self.extract(text, ArticleSummary)
        assert result.title == "途中"

    def test_no_json_raises_value_error(self):
        text = "Sorry, I cannot produce JSON output at this time."
        with pytest.raises((ValueError, Exception)):
            self.extract(text, ArticleSummary)

    def test_fenced_block_without_json_label(self):
        """Plain ``` fence (no 'json' label) should also be extracted."""
        text = '```\n{"title": "フェンス", "summary": "要約", "keywords": ["a"], "category": "AI・機械学習"}\n```'
        result = self.extract(text, ArticleSummary)
        assert result.title == "フェンス"


# ---------------------------------------------------------------------------
# use_structured_output
# ---------------------------------------------------------------------------

class TestUseStructuredOutput:
    def test_default_true(self):
        cfg = _make_cfg(structured_output=True)

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            assert llm_client.use_structured_output() is True

    def test_false_when_overridden(self):
        cfg = _make_cfg(structured_output=False)

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            assert llm_client.use_structured_output() is False


class TestVertexProvider:
    def test_call_maps_messages_and_validates_response(self):
        cfg = AppConfig(
            llm=LLMConfig(provider="vertex", model="gemini-test", max_retries=0),
        )
        client = MagicMock()
        expected = ArticleSummary(
            title="Vertex要約",
            summary="Vertex AIからの応答",
            keywords=["Vertex AI"],
            category="テクノロジー",
        )
        client.models.generate_content.return_value = MagicMock(
            parsed=expected,
            text=expected.model_dump_json(),
        )
        completion_kwargs = {
            "model": "gemini-test",
            "messages": [
                {"role": "system", "content": "日本語で要約してください"},
                {"role": "user", "content": "記事本文"},
            ],
            "response_format": ArticleSummary,
            "temperature": 0.2,
            "max_tokens": 1024,
        }

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            result = llm_client.call_with_retry(client, completion_kwargs)

        assert result == expected
        call = client.models.generate_content.call_args
        assert call.kwargs["model"] == "gemini-test"
        assert call.kwargs["config"].system_instruction == "日本語で要約してください"
        assert call.kwargs["config"].max_output_tokens == 1024


class TestOpenAIResponsesProvider:
    """openai_responses: OpenAI Responses API (/v1/responses) 経由の呼び出し。"""

    def _cfg(self, structured_output: bool = True) -> AppConfig:
        return AppConfig(
            llm=LLMConfig(
                provider="openai_responses",
                model="gpt-5.6-luna",
                max_retries=0,
                structured_output=structured_output,
            ),
        )

    def _completion_kwargs(self) -> dict:
        return {
            "model": "gpt-5.6-luna",
            "messages": [
                {"role": "system", "content": "日本語で要約してください"},
                {"role": "user", "content": "記事本文"},
            ],
            "response_format": ArticleSummary,
            "max_tokens": 1024,
            "reasoning_effort": "low",
        }

    def test_structured_mode_maps_params_and_returns_parsed(self):
        expected = ArticleSummary(
            title="Responses要約",
            summary="Responses APIからの応答",
            keywords=["OpenAI"],
            category="テクノロジー",
        )
        client = MagicMock()
        client.responses.parse.return_value = MagicMock(output_parsed=expected)

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", self._cfg(structured_output=True)):
            result = llm_client.call_with_retry(client, self._completion_kwargs())

        assert result == expected
        call = client.responses.parse.call_args
        assert call.kwargs["model"] == "gpt-5.6-luna"
        assert call.kwargs["input"] == self._completion_kwargs()["messages"]
        assert call.kwargs["text_format"] is ArticleSummary
        assert call.kwargs["max_output_tokens"] == 1024
        assert call.kwargs["reasoning"] == {"effort": "low"}
        assert "max_tokens" not in call.kwargs
        assert "reasoning_effort" not in call.kwargs
        assert "response_format" not in call.kwargs

    def test_plain_text_mode_extracts_json_from_output_text(self):
        client = MagicMock()
        client.responses.create.return_value = MagicMock(
            output_text='```json\n{"title": "プレーン", "summary": "要約", '
            '"keywords": ["a"], "category": "テクノロジー"}\n```'
        )

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", self._cfg(structured_output=False)):
            result = llm_client.call_with_retry(client, self._completion_kwargs())

        assert result.title == "プレーン"
        call = client.responses.create.call_args
        assert "response_format" not in call.kwargs
        assert "text_format" not in call.kwargs

    def test_structured_mode_retries_and_raises_last_error(self):
        client = MagicMock()
        client.responses.parse.side_effect = RuntimeError("boom")

        cfg = AppConfig(
            llm=LLMConfig(
                provider="openai_responses",
                model="gpt-5.6-luna",
                max_retries=1,
                structured_output=True,
            ),
        )

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", cfg):
            with pytest.raises(RuntimeError, match="boom"):
                llm_client.call_with_retry(client, self._completion_kwargs())

        assert client.responses.parse.call_count == 2

    def test_structured_streaming_consumes_events_and_returns_final_response(self, capsys):
        """client.responses.stream() のイベントを消費し、thinkingを表示しつつ最終レスポンスを返す。"""
        expected = ArticleSummary(
            title="Streamで要約",
            summary="ストリーミング応答",
            keywords=["ストリーム"],
            category="テクノロジー",
        )
        events = [
            SimpleNamespace(type="response.reasoning_text.delta", delta="考え中..."),
            SimpleNamespace(type="response.output_text.delta", delta='{"title": "..."}'),
        ]
        stream_ctx = MagicMock()
        stream_ctx.__iter__.return_value = iter(events)
        stream_ctx.get_final_response.return_value = MagicMock(output_parsed=expected, usage=None)

        client = MagicMock()
        client.responses.stream.return_value.__enter__.return_value = stream_ctx
        client.responses.stream.return_value.__exit__.return_value = False

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", self._cfg(structured_output=True)):
            result = llm_client.call_with_retry(client, self._completion_kwargs(), stream=True)

        assert result == expected
        out = capsys.readouterr().out
        assert "[Thinking]" in out
        assert "考え中..." in out

    def test_plain_text_streaming_accumulates_content_and_extracts_json(self, capsys):
        """client.responses.create(stream=True) のイベントから全文を組み立てJSONを抽出する。"""
        events = [
            SimpleNamespace(type="response.output_text.delta", delta='{"title": "分割", "summary": "要約", '),
            SimpleNamespace(type="response.output_text.delta", delta='"keywords": ["a"], "category": "テクノロジー"}'),
        ]
        stream_cm = MagicMock()
        stream_cm.__enter__.return_value = iter(events)
        stream_cm.__exit__.return_value = False

        client = MagicMock()
        client.responses.create.return_value = stream_cm

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", self._cfg(structured_output=False)):
            result = llm_client.call_with_retry(client, self._completion_kwargs(), stream=True)

        assert result.title == "分割"
        out = capsys.readouterr().out
        assert "分割" in out
        assert "[Thinking]" not in out

    def test_chat_completions_only_param_raises_before_calling_client(self):
        """stop/seed等Chat Completions専用パラメータは、SDKの分かりにくいTypeError
        ではなく _build_responses_kwargs の時点で明示的なValueErrorになる。
        リトライも消費しない（設定ミスは再試行しても直らないため）。"""
        client = MagicMock()
        kwargs = self._completion_kwargs()
        kwargs["stop"] = ["\n"]
        kwargs["seed"] = 42

        import summarizer.llm_client as llm_client
        with patch.object(llm_client, "config", self._cfg(structured_output=True)):
            with pytest.raises(ValueError, match="stop.*seed|seed.*stop"):
                llm_client.call_with_retry(client, kwargs)

        client.responses.parse.assert_not_called()
