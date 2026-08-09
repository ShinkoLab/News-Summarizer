from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    model_config = {"extra": "forbid"}

    provider: Literal["openai", "vertex"] = "openai"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str
    api_key: str = "ollama"
    project_id: str | None = None
    location: str = "global"
    parameters: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] | None = None
    thinking: bool | None = None
    gemma4_think: bool = False
    disable_temperature_with_thinking: bool = False
    max_retries: int = 3
    structured_output: bool = True
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None


class SummarizerStepConfig(BaseModel):
    model_config = {"extra": "forbid"}

    parameters: dict[str, Any] | None = Field(default=None)
    thinking: bool | None = None
    use_embeddings: bool = False
    similarity_threshold: float = 0.85


class SummarizerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    individual_max_length: int = 200
    digest_max_length: int = 1500
    categories: list[str] = Field(default_factory=list)
    fallback_category: str = "未分類"
    category_max_retries: int = 3
    max_articles_per_run: int = Field(default=100, ge=1, le=200)
    steps: dict[str, SummarizerStepConfig] = Field(default_factory=dict)

    @field_validator("steps", mode="before")
    @classmethod
    def _coerce_null_steps(cls, v: Any) -> Any:
        """YAML の完全コメントアウトされたステップ（null）をデフォルト設定に変換する。"""
        if isinstance(v, dict):
            return {k: (val if val is not None else {}) for k, val in v.items()}
        return v


class DatabaseConfig(BaseModel):
    model_config = {"extra": "forbid"}

    backend: Literal["sqlite", "firestore"] = "sqlite"
    path: str = "data/news_summarizer.db"
    project_id: str | None = None
    firestore_database: str = "(default)"


class MinifluxConfig(BaseModel):
    model_config = {"extra": "forbid"}

    base_url: str
    api_key: str


class EmailConfig(BaseModel):
    model_config = {"extra": "forbid"}

    host: str
    port: int = 995
    username: str
    password: str
    use_ssl: bool = True


class DiscordConfig(BaseModel):
    model_config = {"extra": "forbid"}

    webhook_url: str | None = None
    embed_color: int = 0x58B9C2
    footer_text: str = ""
    post_individual_articles: bool = True


class LoggingConfig(BaseModel):
    model_config = {"extra": "forbid"}

    level: str = "INFO"


# ---------------------------------------------------------------------------
# Top-level app config
# ---------------------------------------------------------------------------

class AppConfig(BaseModel):
    # extra="ignore" at the top level allows unknown sections (e.g. future additions)
    # without raising errors. Inner models still use extra="forbid" to catch typos.
    model_config = {"extra": "ignore"}

    llm: LLMConfig
    summarizer: SummarizerConfig = Field(default_factory=SummarizerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    miniflux: MinifluxConfig | None = None
    email: EmailConfig | None = None
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config.yaml") -> AppConfig:
    """Load and validate configuration from a YAML file.

    YAML の内容がそのまま設定になる。環境変数による上書きは意図的に行わない
    （`_apply_environment_overrides()` を参照）。

    Raises FileNotFoundError if the file does not exist, and
    pydantic.ValidationError (with clear field-level messages) if the
    content does not match the expected schema.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file '{config_path}' not found.")

    with open(path, "r", encoding="utf-8") as f:
        raw: dict = yaml.safe_load(f) or {}

    return AppConfig.model_validate(raw)


def _env_bool(name: str) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def _apply_environment_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """設定ファイルが無い環境（Cloud Run）向けに、環境変数から設定を組み立てる。

    Cloud Run Job はイメージに config.yaml を含めない（`.dockerignore`）ため、
    すべての設定を環境変数で受け取る。シークレットは Secret Manager から注入される。

    **`load_config()` からは呼ばない。** YAML が存在する場合は YAML が唯一の正であり、
    たまたま export されている環境変数（`GOOGLE_CLOUD_PROJECT` など gcloud/ADC 系の
    ツールが設定するものを含む）が、明示した設定を黙って上書きしないようにする。
    A section is created only when at least one corresponding environment variable
    is present.
    """
    result = dict(raw)

    def section(name: str) -> dict[str, Any]:
        current = dict(result.get(name) or {})
        result[name] = current
        return current

    llm = section("llm")
    llm_mapping = {
        "LLM_PROVIDER": "provider",
        "LLM_MODEL": "model",
        "LLM_BASE_URL": "base_url",
        "LLM_API_KEY": "api_key",
        "GOOGLE_CLOUD_PROJECT": "project_id",
        "GOOGLE_CLOUD_LOCATION": "location",
        "LLM_EMBEDDING_MODEL": "embedding_model",
        "EMBEDDING_BASE_URL": "embedding_base_url",
        "EMBEDDING_API_KEY": "embedding_api_key",
    }
    for env_name, field_name in llm_mapping.items():
        if value := os.getenv(env_name):
            llm[field_name] = value
    if max_retries := os.getenv("LLM_MAX_RETRIES"):
        llm["max_retries"] = int(max_retries)
    if (structured_output := _env_bool("LLM_STRUCTURED_OUTPUT")) is not None:
        llm["structured_output"] = structured_output

    llm_params: dict[str, Any] = {}
    if temperature := os.getenv("LLM_TEMPERATURE"):
        llm_params["temperature"] = float(temperature)
    if max_tokens := os.getenv("LLM_MAX_TOKENS"):
        llm_params["max_tokens"] = int(max_tokens)
    if llm_params:
        llm["parameters"] = {**(llm.get("parameters") or {}), **llm_params}

    if max_articles := os.getenv("MAX_ARTICLES_PER_RUN"):
        section("summarizer")["max_articles_per_run"] = int(max_articles)
    if categories := os.getenv("SUMMARIZER_CATEGORIES"):
        section("summarizer")["categories"] = [c.strip() for c in categories.split(",") if c.strip()]

    steps = section("summarizer").setdefault("steps", {})

    def step_section(name: str) -> dict[str, Any]:
        current = dict(steps.get(name) or {})
        steps[name] = current
        return current

    if (use_embeddings := _env_bool("GROUPER_USE_EMBEDDINGS")) is not None:
        step_section("grouper")["use_embeddings"] = use_embeddings

    step_param_envs: dict[str, dict[str, str]] = {
        "grouper": {"temperature": "GROUPER_TEMPERATURE", "reasoning_effort": "GROUPER_REASONING_EFFORT"},
        "summarizer": {"temperature": "SUMMARIZER_TEMPERATURE", "reasoning_effort": "SUMMARIZER_REASONING_EFFORT"},
        "digest": {"reasoning_effort": "DIGEST_REASONING_EFFORT"},
    }
    for step_name, param_envs in step_param_envs.items():
        step_params: dict[str, Any] = {}
        for param_name, env_name in param_envs.items():
            if value := os.getenv(env_name):
                step_params[param_name] = float(value) if param_name == "temperature" else value
        if step_params:
            step = step_section(step_name)
            step["parameters"] = {**(step.get("parameters") or {}), **step_params}

    database_vars = {
        "DATABASE_BACKEND": "backend",
        "SQLITE_DATABASE_PATH": "path",
        "GOOGLE_CLOUD_PROJECT": "project_id",
        "FIRESTORE_DATABASE": "firestore_database",
    }
    if any(os.getenv(name) for name in database_vars):
        database = section("database")
        for env_name, field_name in database_vars.items():
            if value := os.getenv(env_name):
                database[field_name] = value

    miniflux_vars = {
        "MINIFLUX_BASE_URL": "base_url",
        "MINIFLUX_API_KEY": "api_key",
    }
    if any(os.getenv(name) for name in miniflux_vars):
        miniflux = section("miniflux")
        for env_name, field_name in miniflux_vars.items():
            if value := os.getenv(env_name):
                miniflux[field_name] = value

    email_vars = {
        "EMAIL_HOST": "host",
        "EMAIL_USERNAME": "username",
        "EMAIL_PASSWORD": "password",
    }
    if any(os.getenv(name) for name in email_vars):
        email_config = section("email")
        for env_name, field_name in email_vars.items():
            if value := os.getenv(env_name):
                email_config[field_name] = value
        if port := os.getenv("EMAIL_PORT"):
            email_config["port"] = int(port)
        if (use_ssl := _env_bool("EMAIL_USE_SSL")) is not None:
            email_config["use_ssl"] = use_ssl

    if webhook_url := os.getenv("DISCORD_WEBHOOK_URL"):
        section("discord")["webhook_url"] = webhook_url
    if log_level := os.getenv("LOG_LEVEL"):
        section("logging")["level"] = log_level

    return result


def load_runtime_config(config_path: str | None = None) -> AppConfig:
    """Load local YAML when present, otherwise construct a Cloud Run config."""
    path = config_path or os.getenv("CONFIG_PATH", "config.yaml")
    if Path(path).exists():
        return load_config(path)

    # Cloud Run ships no config.yaml and is configured purely through env vars.
    # Locally this branch almost always means a typo or a forgotten
    # `cp config.yaml.example config.yaml`, and the resulting config has no
    # miniflux/email/discord section — which the pipeline reports as "no new
    # articles" rather than as a failure. Make the fallback visible.
    logging.getLogger(__name__).warning(
        "設定ファイル '%s' が見つかりません。環境変数のみで設定を構築します"
        "（ローカル実行の場合は config.yaml.example をコピーしてください）。",
        path,
    )

    raw: dict[str, Any] = {
        "llm": {
            "provider": os.getenv("LLM_PROVIDER", "vertex"),
            "model": os.getenv("LLM_MODEL", "gemini-3.1-flash-lite"),
        }
    }
    return AppConfig.model_validate(_apply_environment_overrides(raw))


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

# Loaded once at import time from the default config.yaml.
# Callers that need to reload (e.g. --config CLI flag) should call
# reload_config() which updates this module attribute in-place.
config: AppConfig = load_runtime_config()


def reload_config(config_path: str) -> AppConfig:
    """Reload configuration while preserving references held by other modules."""
    new_config = load_config(config_path)
    config.__dict__.clear()
    config.__dict__.update(new_config.__dict__)
    return config
