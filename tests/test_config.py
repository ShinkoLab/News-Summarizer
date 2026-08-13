"""Tests for config.py — load_config, AppConfig validation, reload_config."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

import config as config_module
from config import (
    AppConfig,
    LLMConfig,
    load_config,
    load_runtime_config,
    load_taxonomy,
    reload_config,
)


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

    def test_example_taxonomy_populated(self):
        """カテゴリ定義は config.yaml ではなく categories.yaml から読み込まれる。"""
        cfg = load_config("config.yaml.example")
        assert cfg.taxonomy is not None
        assert len(cfg.taxonomy.names) > 0

    def test_example_miniflux_present(self):
        cfg = load_config("config.yaml.example")
        assert cfg.miniflux is not None
        assert cfg.miniflux.base_url

    def test_example_discord_present(self):
        cfg = load_config("config.yaml.example")
        assert cfg.discord is not None


# ---------------------------------------------------------------------------
# categories.yaml (CategoryTaxonomy)
# ---------------------------------------------------------------------------

class TestCategoryTaxonomy:
    """カテゴリ定義は categories.yaml の1箇所のみを正とする。"""

    def test_loads_repository_categories_yaml(self):
        """定義順は運用上の好みで変わるため、順序ではなく集合として検証する。"""
        taxonomy = load_taxonomy()
        assert set(taxonomy.names) == {
            "政治・社会",
            "事件・事故・災害",
            "経済・ビジネス",
            "テクノロジー",
            "AI・機械学習",
            "科学・環境",
            "健康・ライフ",
            "カルチャー",
        }
        assert len(taxonomy.names) == 8

    def test_fallback_is_outside_the_category_list(self):
        """フォールバックは分類失敗のシグナルなので、意図的に categories 外の値。"""
        taxonomy = load_taxonomy()
        assert taxonomy.fallback == "未分類"
        assert taxonomy.fallback not in taxonomy.names

    def test_principles_and_tiebreak_rules_present(self):
        taxonomy = load_taxonomy()
        assert taxonomy.principles
        assert taxonomy.tiebreak_rules
        assert all(c.description for c in taxonomy.categories)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_taxonomy(str(tmp_path / "absent.yaml"))

    def test_path_override(self, tmp_path):
        path = tmp_path / "custom.yaml"
        path.write_text(
            textwrap.dedent("""\
                categories:
                  - name: 独自カテゴリ
                    description: テスト用。
                fallback: 未分類
            """),
            encoding="utf-8",
        )
        assert load_taxonomy(str(path)).names == ["独自カテゴリ"]

    def test_empty_categories_raises(self, tmp_path):
        """空リストのまま起動すると全記事が無駄なリトライを消費するため弾く。"""
        path = tmp_path / "empty.yaml"
        path.write_text("categories: []\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="must not be empty"):
            load_taxonomy(str(path))

    def test_duplicate_names_raise(self, tmp_path):
        path = tmp_path / "dup.yaml"
        path.write_text(
            textwrap.dedent("""\
                categories:
                  - name: テクノロジー
                    description: 一つ目。
                  - name: テクノロジー
                    description: 二つ目。
            """),
            encoding="utf-8",
        )
        with pytest.raises(ValidationError, match="unique"):
            load_taxonomy(str(path))


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


class TestEnvironmentOverrides:
    def test_cloud_runtime_config_from_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "family-news-project")
        monkeypatch.setenv("LLM_PROVIDER", "vertex")
        monkeypatch.setenv("LLM_MODEL", "gemini-test")
        monkeypatch.setenv("DATABASE_BACKEND", "firestore")
        monkeypatch.setenv("MAX_ARTICLES_PER_RUN", "25")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.llm.provider == "vertex"
        assert cfg.llm.model == "gemini-test"
        assert cfg.llm.project_id == "family-news-project"
        assert cfg.database.backend == "firestore"
        assert cfg.database.project_id == "family-news-project"
        assert cfg.summarizer.max_articles_per_run == 25

    def test_yaml_wins_over_environment(self, tmp_path, monkeypatch):
        """YAML が存在する場合、環境変数は一切上書きしない。

        --config で明示したファイルや手元の config.yaml が、たまたま export されている
        環境変数に黙って上書きされるのを防ぐ。
        """
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "llm:\n  model: local-model\n"
            "summarizer:\n  individual_max_length: 500\n"
            "  steps:\n    grouper:\n      similarity_threshold: 0.7\n"
            "miniflux:\n  base_url: https://example.com\n  api_key: local-secret\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MINIFLUX_API_KEY", "secret-manager-value")
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-ambient-project")
        monkeypatch.setenv("SUMMARIZER_INDIVIDUAL_MAX_LENGTH", "999")
        monkeypatch.setenv("GROUPER_SIMILARITY_THRESHOLD", "0.99")

        cfg = load_config(str(cfg_file))

        assert cfg.miniflux is not None
        assert cfg.miniflux.api_key == "local-secret"
        assert cfg.llm.model == "local-model"
        assert cfg.llm.project_id is None
        assert cfg.summarizer.individual_max_length == 500
        assert cfg.summarizer.steps["grouper"].similarity_threshold == 0.7

    def test_environment_still_applies_when_yaml_is_absent(self, tmp_path, monkeypatch):
        """Cloud Run 相当（YAML 不在）では従来どおり環境変数だけで構成する。"""
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("MINIFLUX_BASE_URL", "https://miniflux.example.com")
        monkeypatch.setenv("MINIFLUX_API_KEY", "secret-manager-value")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.llm.model == "env-model"
        assert cfg.miniflux is not None
        assert cfg.miniflux.api_key == "secret-manager-value"

    def test_summary_length_and_threshold_from_environment(self, tmp_path, monkeypatch):
        """Cloud Run でチューニング値が反映されること。

        この2つは環境変数マッピングが無く、Cloud Run だけがコード既定値
        （200字 / 0.85）で動いていた。ローカルで調整した値が本番に届かないため、
        マッピングの有無をテストで固定する。
        """
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("SUMMARIZER_INDIVIDUAL_MAX_LENGTH", "500")
        monkeypatch.setenv("GROUPER_USE_EMBEDDINGS", "true")
        monkeypatch.setenv("GROUPER_SIMILARITY_THRESHOLD", "0.7")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.summarizer.individual_max_length == 500
        assert cfg.summarizer.steps["grouper"].use_embeddings is True
        assert cfg.summarizer.steps["grouper"].similarity_threshold == 0.7

    def test_miniflux_fetch_limit_from_environment(self, tmp_path, monkeypatch):
        """取得件数の上限も環境変数で渡せること。

        max_articles_per_run だけ上げても、Miniflux 側が limit 未指定で既定の
        100件に打ち切るため処理件数は増えない。2つはセットで効く。
        """
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("MINIFLUX_BASE_URL", "https://miniflux.example.com")
        monkeypatch.setenv("MINIFLUX_API_KEY", "secret-manager-value")
        monkeypatch.setenv("MINIFLUX_FETCH_LIMIT", "250")
        monkeypatch.setenv("MAX_ARTICLES_PER_RUN", "200")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.miniflux is not None
        assert cfg.miniflux.fetch_limit == 250
        assert cfg.summarizer.max_articles_per_run == 200

    def test_miniflux_fetch_limit_alone_does_not_build_broken_section(
        self, tmp_path, monkeypatch
    ):
        """FETCH_LIMIT 単独では miniflux セクションを作らない。

        作ってしまうと base_url / api_key を欠いたセクションになり、必須フィールド
        欠落で設定読み込みごと落ちる。RSS を使わない構成では miniflux は None が正。
        """
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("MINIFLUX_FETCH_LIMIT", "250")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.miniflux is None

    def test_miniflux_fetch_limit_defaults_to_miniflux_server_default(
        self, tmp_path, monkeypatch
    ):
        """未指定なら 100（= Miniflux 側の既定値）。既存構成の挙動を変えない。"""
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("MINIFLUX_BASE_URL", "https://miniflux.example.com")
        monkeypatch.setenv("MINIFLUX_API_KEY", "secret-manager-value")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.miniflux is not None
        assert cfg.miniflux.fetch_limit == 100

    def test_email_max_fetch_attempts_from_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("EMAIL_HOST", "pop.example.com")
        monkeypatch.setenv("EMAIL_USERNAME", "user")
        monkeypatch.setenv("EMAIL_PASSWORD", "pass")
        monkeypatch.setenv("EMAIL_MAX_FETCH_ATTEMPTS", "5")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.email is not None
        assert cfg.email.max_fetch_attempts == 5

    def test_email_max_fetch_attempts_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("EMAIL_HOST", "pop.example.com")
        monkeypatch.setenv("EMAIL_USERNAME", "user")
        monkeypatch.setenv("EMAIL_PASSWORD", "pass")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.email is not None
        assert cfg.email.max_fetch_attempts == 3

    def test_email_delete_after_processing_from_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("EMAIL_HOST", "pop.example.com")
        monkeypatch.setenv("EMAIL_USERNAME", "user")
        monkeypatch.setenv("EMAIL_PASSWORD", "pass")
        monkeypatch.setenv("EMAIL_DELETE_AFTER_PROCESSING", "true")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.email is not None
        assert cfg.email.delete_after_processing is True

    def test_email_delete_after_processing_defaults_to_false(self, tmp_path, monkeypatch):
        """既定はメールを消さない。取り違えると復旧できない操作なので明示的に確認する。"""
        monkeypatch.setenv("LLM_MODEL", "env-model")
        monkeypatch.setenv("EMAIL_HOST", "pop.example.com")
        monkeypatch.setenv("EMAIL_USERNAME", "user")
        monkeypatch.setenv("EMAIL_PASSWORD", "pass")

        cfg = load_runtime_config(str(tmp_path / "missing.yaml"))

        assert cfg.email is not None
        assert cfg.email.delete_after_processing is False


# ---------------------------------------------------------------------------
# reload_config replaces the module-level singleton
# ---------------------------------------------------------------------------

class TestReloadConfig:
    @pytest.fixture(autouse=True)
    def restore_module_config(self):
        """Snapshot the singleton and put it back in-place after each test.

        Restoring by re-reading config.yaml would fail in a clean checkout,
        since config.yaml is gitignored.
        """
        snapshot = dict(config_module.config.__dict__)
        try:
            yield
        finally:
            config_module.config.__dict__.clear()
            config_module.config.__dict__.update(snapshot)

    def test_reload_replaces_module_config(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            llm:
              model: "reloaded-model"
        """)
        cfg_file = tmp_path / "reload.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        new_cfg = reload_config(str(cfg_file))

        assert config_module.config.llm.model == "reloaded-model"
        assert new_cfg.llm.model == "reloaded-model"

    def test_reload_returns_appconfig(self, tmp_path):
        yaml_content = textwrap.dedent("""\
            llm:
              model: "another-model"
        """)
        cfg_file = tmp_path / "reload2.yaml"
        cfg_file.write_text(yaml_content, encoding="utf-8")

        result = reload_config(str(cfg_file))

        assert isinstance(result, AppConfig)

    def test_restores_singleton_after_reload(self):
        """The autouse fixture must actually put the original object back."""
        assert config_module.config.llm.model not in ("reloaded-model", "another-model")
