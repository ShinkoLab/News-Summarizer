# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git Workflow

Always create a branch before implementing any new features.

```bash
git checkout -b feature/<feature-name>
```

コミットメッセージは必ず**日本語**で記載すること。

## Development Commands

```bash
# Run the main pipeline
uv run python main.py

# CLI options
uv run python main.py --dry-run                        # Skip DB writes, Discord posting, and Miniflux mark-as-read
uv run python main.py --dry-run --output discord       # Dry-run but force Discord output
uv run python main.py --dry-run --output db            # Dry-run but force DB write
uv run python main.py --dry-run --output discord db    # Force multiple outputs (nargs='+')
uv run python main.py --dry-run --output all           # Force all outputs
uv run python main.py --source rss                     # RSS only
uv run python main.py --source email                   # Email only
uv run python main.py --config path.yaml               # Custom config file
uv run python main.py --stream                         # Stream LLM output to terminal (debug)
uv run python main.py --debug                          # Show embedding/similarity details (debug)
```

**Package manager**: `uv` (Python 3.12 via `mise`)

```bash
uv sync                  # Install dependencies
uv add <package>         # Add a dependency
```

## Architecture

The pipeline runs in a single pass: **Fetch → Summarize → Group → Digest → Output**

```
Miniflux (RSS) + POP3 (Email)
        ↓
  [Fetchers] → List[Article]
        ↓
  [Summarizer] → per-article summary + keywords + category (LLM)
        ↓
  [Grouper]  → clusters similar articles by topic
               (embedding + cosine similarity, or LLM)
        ↓
  [Digest]  → category-based digest with overview (LLM)
        ↓
  [Database (SQLite)] + [Discord Webhook]
```

### Key modules

- **`main.py`** — CLI entry point only; parses arguments into `RunOptions` and calls `run_pipeline()`
- **`pipeline.py`** — Orchestrates the full pipeline; defines `RunOptions` (frozen dataclass), `run_pipeline()`, and internal step functions (`fetch_articles`, `summarize_all`, `group_pairs`, `build_digest`, `persist_and_publish`); error isolation is per-source and per-article
- **`config.py`** / **`config.yaml`** — Single YAML config for all services; `config.yaml` is gitignored, use `config.yaml.example` as template
- **`models.py`** — All data structures: `Article` (common fetch format, dataclass), plus Pydantic models for LLM structured outputs (`ArticleGroup`, `GroupingResult`, `ArticleSummary`, `CategoryDigest`, `TopicLabel`, `TopicNamingResult`, `DigestResult`)
- **`logger.py`** — Logging setup (`setup_logging()` / `get_logger()`); outputs to stderr only, level controlled by `logging.level` in config
- **`fetchers/`** — `MinifluxFetcher` (REST API) and `EmailFetcher` (POP3); both normalize to `Article`
- **`summarizer/`** — LLM steps using OpenAI SDK; structured output via Pydantic
  - `llm_client.py` — shared LLM call logic, retry handling, step config resolution
  - `summarizer.py` — per-article summarization
  - `grouper.py` — topic grouping (LLM-based or embedding-based)
  - `embedder.py` — embedding retrieval via Ollama embedding model
  - `digest.py` — category digest generation
- **`outputs/`** — `Database` (SQLite, batch-based schema) and `DiscordOutput` (webhook embeds)

### LLM integration

Uses the OpenAI Python SDK pointed at a configurable endpoint (default: local Ollama at `http://127.0.0.1:11434/v1`). Config key is `llm` (not `ollama`). All LLM steps use **structured output** (Pydantic models) by default; can be disabled per-step via `structured_output: false` for providers that don't support it.

Each step has independent parameter overrides under `summarizer.steps.<step>.parameters`.

All LLM output is in **Japanese** regardless of source article language.

Additional `llm:` config fields (all optional):
- `max_retries` — retries on JSON parse / API errors (default: `3`)
- `structured_output` — global toggle for structured output (default: `true`); set `false` for providers that don't support it
- `extra_body` — provider-specific extra parameters passed through to the API (e.g. Ollama `think: true` for thinking mode)
- `thinking` / `disable_temperature_with_thinking` — set `thinking: true` on a step to auto-exclude `temperature` when using thinking models
- `gemma4_think` — prepend `<|think|>` to the system prompt for Gemma 4 thinking stabilization

### Grouping modes

Controlled by `summarizer.steps.grouper.use_embeddings` in config:

- **`false` (default)**: LLM receives article list and clusters by topic directly
- **`true`**: individual summaries are embedded (`llm.embedding_model` required), clustered by cosine similarity (`similarity_threshold`, default `0.85`), then LLM only assigns topic names — avoids context overflow for large article sets

### Deduplication

- **RSS**: Miniflux API state (entries marked as read after fetch; skipped in `--dry-run` mode)
- **Email**: UIDL tracking stored in the `processed_emails` SQLite table; messages are never deleted from the server

### Configuration

Copy `config.yaml.example` → `config.yaml` and fill in:
- LLM endpoint and model name (under `llm:`)
- Miniflux URL + API key
- Discord webhook URL
- POP3 credentials (if using email source)

Notable optional keys (see `config.yaml.example` for full comments):
- `database.path` — SQLite file path (default: `data/news_summarizer.db`)
- `summarizer.categories` / `fallback_category` / `category_max_retries` — category list, fallback when the LLM returns an unlisted value, and retry count on validation failure (default: `3`)
- `summarizer.individual_max_length` / `digest_max_length` — character limits for per-article summaries and the digest
- `discord.post_individual_articles` / `embed_color` / `footer_text` — Discord embed tuning (default: post individual articles = `true`)
- `llm.max_retries` / `structured_output` / `extra_body` / `embedding_model` — LLM behavior tuning
