"""Tests for summarizer/llm_client.py pure/deterministic helpers."""

from __future__ import annotations

import textwrap
from unittest.mock import patch

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
        text = '{"title": "タイトル", "summary": "要約", "keywords": ["x"], "category": "ビジネス"}'
        result = self.extract(text, ArticleSummary)
        assert result.category == "ビジネス"

    def test_think_block_stripped(self):
        text = textwrap.dedent("""\
            <think>Let me think about this carefully...</think>
            ```json
            {"title": "考えた結果", "summary": "要約文", "keywords": ["k1"], "category": "科学"}
            ```
        """)
        result = self.extract(text, ArticleSummary)
        assert result.title == "考えた結果"

    def test_truncated_fence_no_closing_backticks(self):
        """Fallback: fenced block that never closes (LLM truncated)."""
        text = '```json\n{"title": "途中", "summary": "要約", "keywords": ["a", "b", "c"], "category": "その他"}'
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
