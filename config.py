from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    model_config = {"extra": "forbid"}

    base_url: str = "http://127.0.0.1:11434/v1"
    model: str
    api_key: str = "ollama"
    parameters: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] | None = None
    thinking: bool | None = None
    gemma4_think: bool = False
    disable_temperature_with_thinking: bool = False
    max_retries: int = 3
    structured_output: bool = True
    embedding_model: str | None = None


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

    path: str = "data/news_summarizer.db"


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


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

# Loaded once at import time from the default config.yaml.
# Callers that need to reload (e.g. --config CLI flag) should call
# reload_config() which updates this module attribute in-place.
config: AppConfig = load_config()


def reload_config(config_path: str) -> AppConfig:
    """Reload configuration from *config_path* and replace the module singleton."""
    global config
    config = load_config(config_path)
    return config
