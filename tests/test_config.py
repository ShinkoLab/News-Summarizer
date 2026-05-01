"""Tests for config.py — load_config, AppConfig validation, reload_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

import config as config_module
from config import AppConfig, LLMConfig, load_config, reload_config


# ---------------------------------------------------------------------------
# load_config from config.yaml.example
# ---------------------------------------------------------------------------

class TestLoadConfigFromExample:
    """config.yaml.example should load without errors."""

    def test_loads_example_file(self):
        cfg = load_config("config.yaml.example")
        assert isinstance(cfg, AppConfig)
        assert isinstance(cfg.llm, LLMConfig)

    def test_example_has_model(self):
        cfg = load_config("config.yaml.example")
        assert cfg.llm.model  # non-empty string

    def test_example_summarizer_categories_populated(self):
        cfg = load_config("config.yaml.example")
        assert len(cfg.summarizer.categories) > 0

    def test_example_miniflux_present(self):
        cfg = load_config("config.yaml.example")
        assert cfg.miniflux is not None
        assert cfg.miniflux.base_url

    def test_example_discord_present(self):
        cfg = load_config("config.yaml.example")
        assert cfg.discord is not None


# ---------------------------------------------------------------------------
# Missing required field raises ValidationError
# ---------------------------------------------------------------------------

class TestMissingRequiredField:
    """llm.model is required; omitting it should raise ValidationError."""

    def test_missing_llm_model_raises(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            llm:
              base_url: "http://localhost:11434/v1"
        """)
        cfg_file = tmp_path / "bad_config.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ValidationError) as exc_info:
            load_config(str(cfg_file))

        errors = exc_info.value.errors()
        fields = [e["loc"] for e in errors]
        # 'model' field should appear in the error locations
        assert any("model" in loc for loc in fields)

    def test_missing_llm_section_raises(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            summarizer:
              individual_max_length: 200
        """)
        cfg_file = tmp_path / "no_llm.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ValidationError):
            load_config(str(cfg_file))

    def test_file_not_found_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "nonexistent.yaml"))


# ---------------------------------------------------------------------------
# Unknown key in strict (extra="forbid") inner model raises
# ---------------------------------------------------------------------------

class TestUnknownKeyForbidden:
    """
    LLMConfig uses extra="forbid". An unknown key inside llm: should raise.
    AppConfig uses extra="ignore" so unknown top-level keys are silently dropped.
    """

    def test_unknown_llm_key_raises(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            llm:
              model: "test-model"
              unknown_key: "this should fail"
        """)
        cfg_file = tmp_path / "unknown_key.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        with pytest.raises(ValidationError) as exc_info:
            load_config(str(cfg_file))

        errors = exc_info.value.errors()
        assert any(e["type"] == "extra_forbidden" for e in errors)

    def test_unknown_top_level_key_ignored(self, tmp_path):
        """AppConfig extra='ignore': unknown top-level keys do NOT raise."""
        yaml_content = textwrap.dedent("""\
            llm:
              model: "test-model"
            future_section:
              some_value: 42
        """)
        cfg_file = tmp_path / "extra_top.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        # Should not raise
        cfg = load_config(str(cfg_file))
        assert cfg.llm.model == "test-model"


# ---------------------------------------------------------------------------
# reload_config replaces the module-level singleton
# ---------------------------------------------------------------------------

class TestReloadConfig:
    def test_reload_replaces_module_config(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            llm:
              model: "reloaded-model"
        """)
        cfg_file = tmp_path / "reload.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        original_model = config_module.config.llm.model
        try:
            new_cfg = reload_config(str(cfg_file))
            assert config_module.config.llm.model == "reloaded-model"
            assert new_cfg.llm.model == "reloaded-model"
        finally:
            # Restore original config so other tests are not affected
            reload_config("config.yaml")

    def test_reload_returns_appconfig(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            llm:
              model: "another-model"
        """)
        cfg_file = tmp_path / "reload2.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        try:
            result = reload_config(str(cfg_file))
            assert isinstance(result, AppConfig)
        finally:
            reload_config("config.yaml")
