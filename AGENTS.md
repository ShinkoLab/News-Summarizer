# AGENTS.md

This file provides guidance to coding agents (Claude Code, Codex, etc.) when working with code in this repository.

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

### Tests

```bash
uv run pytest            # Run the full suite
uv run pytest -q         # Quiet output
uv run pytest tests/test_digest.py::test_name   # Single test
```

Config is `[tool.pytest.ini_options]` in `pyproject.toml` (`testpaths = ["tests"]`, `pythonpath = ["."]`).
`tests/conftest.py` builds `AppConfig` fixtures in Python rather than reading `config.yaml`, so the
suite runs without any local config; external calls are stubbed with `pytest-mock`.

If `uv run pytest` fails with `Failed to spawn: pytest`, the venv was created under a different
absolute path (stale shebangs). `uv sync --dry-run` will not detect this — run `uv sync --reinstall`.

**Package manager**: `uv` (Python 3.12 via `mise`). `mise.toml` also pins `terraform` for `infra/`.

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
  [Database (SQLite or Firestore)] + [Discord Webhook]
```

Runs locally as a CLI and on Google Cloud as a Cloud Run Job — same code, different
config source (see **Deployment** below). For the full design spec see `README.md`.

### Key modules

- **`main.py`** — CLI entry point only; parses arguments into `RunOptions` and calls `run_pipeline()`
- **`pipeline.py`** — Orchestrates the full pipeline; defines `RunOptions` (frozen dataclass), `run_pipeline()`, and internal step functions (`fetch_articles`, `summarize_all`, `group_pairs`, `build_digest`, `persist_and_publish`); error isolation is per-source and per-article. The whole run is wrapped in `db.execution_lock()`, and already-processed articles are skipped via `db.is_article_processed()` before summarization
- **`config.py`** / **`config.yaml`** — Pydantic config layer; `config.yaml` is gitignored, use `config.yaml.example` as template. Every key can also be supplied via environment variables (see **Configuration**)
- **`models.py`** — All data structures: `Article` (common fetch format, dataclass), plus Pydantic models for LLM structured outputs (`ArticleGroup`, `GroupingResult`, `ArticleSummary`, `CategoryDigest`, `TopicLabel`, `TopicNamingResult`, `DigestResult`)
- **`logger.py`** — Logging setup (`setup_logging()` / `get_logger()`); outputs to stderr only, level controlled by `logging.level` in config
- **`fetchers/`** — `MinifluxFetcher` (REST API) and `EmailFetcher` (POP3); both normalize to `Article`
- **`summarizer/`** — LLM steps; structured output via Pydantic
  - `llm_client.py` — shared LLM call logic, retry handling, step config resolution; OpenAI-compatible and Vertex AI paths
  - `summarizer.py` — per-article summarization
  - `grouper.py` — topic grouping (LLM-based or embedding-based)
  - `embedder.py` — embedding retrieval (separate endpoint from the chat LLM)
  - `digest.py` — category digest generation
- **`outputs/`** — `create_database()` returns `Database` (SQLite) or `FirestoreDatabase` depending on `database.backend`; both expose `save_batch()` / `is_article_processed()` / `is_email_processed()` / `execution_lock()`. Plus `DiscordOutput` (webhook embeds, description truncated at the 4096-char Discord limit)
- **`scripts/`** — `migrate_sqlite_to_firestore.py` (one-shot data migration, supports `--dry-run`)
- **`infra/`** — Terraform for the Google Cloud deployment; see `infra/README.md` and `infra/DEPLOYMENT.md`

### LLM integration

Two providers, selected by `llm.provider`:

- **`openai` (default)** — OpenAI Python SDK pointed at any OpenAI-compatible endpoint
  (local Ollama at `http://127.0.0.1:11434/v1`, OpenRouter, etc.)
- **`vertex`** — Vertex AI via `google-genai`; requires `llm.project_id` / `llm.location`

Config key is `llm` (not `ollama`). All LLM steps use **structured output** (Pydantic models) by
default; can be disabled per-step via `structured_output: false` for providers that don't support it.

Embeddings can point at a **different provider than the chat LLM** via `llm.embedding_base_url` /
`llm.embedding_api_key` (falls back to the chat endpoint when unset).

Each step has independent parameter overrides under `summarizer.steps.<step>.parameters`
(steps: `summarizer`, `grouper`, `digest`).

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
- **Email**: UIDL tracking stored in the `processed_emails` table; messages are never deleted from the server
- **Article-level**: `db.is_article_processed(source_type, source_id)` filters out anything already
  stored, before summarization. Combined with `summarizer.max_articles_per_run`, the overflow is
  simply carried over to the next run rather than dropped

### Storage backends

`database.backend` selects the implementation (`create_database()` in `outputs/database.py`):

- **`sqlite` (default)** — local file at `database.path`; `execution_lock()` is a no-op
- **`firestore`** — Cloud Run / Firestore; `execution_lock()` is a real distributed lock with a TTL,
  so overlapping runs cannot double-post. A force-cancelled run can leave the lock document behind —
  recovery is documented in `infra/DEPLOYMENT.md`

`scripts/migrate_sqlite_to_firestore.py` moves existing data across.

## Deployment

- **`Dockerfile`** — builds on a pinned `uv` image, installs with `uv sync --frozen --no-dev`,
  runs as `nobody`. It does a plain `COPY . .`, so anything not listed in `.dockerignore`
  (which excludes `config.yaml`, `data`, `tests`, `infra`, `.git`, `.venv`, caches) is baked
  into the image
- **`cloudbuild.yaml`** — Cloud Build config for the summarizer image
- **`infra/`** — Terraform: Cloud Run Job, Cloud Scheduler, Firestore, Secret Manager, IAM.
  `infra/README.md` for the layout, `infra/DEPLOYMENT.md` for the actual runbook and
  the operational gotchas hit so far

## Configuration

Copy `config.yaml.example` → `config.yaml` and fill in:
- LLM endpoint and model name (under `llm:`)
- Miniflux URL + API key
- Discord webhook URL
- POP3 credentials (if using email source)

`config.yaml.example` is kept in sync with `config.py` and documents every key — treat it as the
reference, not this file.

**Category taxonomy** lives in `categories.yaml` at the repo root — a separate,
git-tracked file (unlike the gitignored `config.yaml`) that is baked into the
Docker image, so local runs and Cloud Run read the same definitions and the list
is never duplicated across environments. It holds the category names, a
`description` per category, `principles`, `tiebreak_rules`, and a `fallback`
value; all of it is injected into the summarizer prompt, and the digest orders
its categories by the order in this file. Tune classification accuracy by
editing the wording here — no code change needed. Path is overridable via
`CATEGORIES_PATH`. Current taxonomy, **in definition order** (which is also the digest
display order): AI・機械学習 / テクノロジー / 経済・ビジネス / 政治・社会 /
事件・事故・災害 / 科学・環境 / 健康・ライフ / カルチャー.

### Where config comes from

There are exactly two modes, and they never mix:

| | Source of truth |
|---|---|
| A YAML file exists (local runs, `--config path.yaml`) | **The YAML, and only the YAML.** Environment variables are ignored |
| No YAML file (the Cloud Run Job — `config.yaml` is excluded by `.dockerignore`) | **Environment variables only**, via `_apply_environment_overrides()` |

`load_runtime_config()` picks the mode by testing whether the path exists; `CONFIG_PATH`
overrides where it looks. This split is deliberate: ambient environment variables
(`GOOGLE_CLOUD_PROJECT` is set by common `gcloud`/ADC tooling, for instance) must not
silently override a config file the user pointed at.

`_apply_environment_overrides()` covers **every** config key. The authoritative
env-var → config-key mapping is the Cloud Run Job definition in `infra/main.tf`.

> Note: with no `config.yaml` present, the env-only defaults target Vertex AI. Local runs should
> always have a `config.yaml`.

Notable optional keys (see `config.yaml.example` for full comments):
- `database.backend` — `sqlite` (default) or `firestore`; `database.path` (SQLite file, default `data/news_summarizer.db`), `database.project_id` / `database.firestore_database` (Firestore)
- `summarizer.category_max_retries` — retry count when the LLM returns a category outside the defined list (default: `3`)
- `summarizer.individual_max_length` / `digest_max_length` — character limits for per-article summaries and the digest. `digest_max_length` (default `3000`) is split across categories by `_allocate_chars()` (`summarizer/digest.py`): the `MIN_CHARS_PER_CATEGORY` floor of 120 is reserved for every category first, then the remainder is distributed **in proportion to article count**, so the total never exceeds the limit
- `summarizer.max_articles_per_run` — cap on articles processed in one run (default: `100`); the remainder is carried over
- `summarizer.steps.<step>.thinking` — per-step thinking toggle
- `discord.post_individual_articles` / `embed_color` / `footer_text` — Discord embed tuning. `post_individual_articles` defaults to **`false`**; enabling it re-posts every article as its own embed, duplicating what the category digest already covers
- `llm.provider` / `project_id` / `location` — provider selection and Vertex AI target
- `llm.embedding_model` / `embedding_base_url` / `embedding_api_key` — embedding endpoint, independent of the chat LLM
- `llm.max_retries` / `structured_output` / `extra_body` — LLM behavior tuning
