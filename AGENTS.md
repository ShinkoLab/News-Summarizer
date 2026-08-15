# AGENTS.md

This file provides guidance to coding agents (Claude Code, Codex, etc.) when working with code in this repository.

## Git Workflow

Always create a branch before implementing any new features.

```bash
git checkout -b feature/<feature-name>
```

コミットメッセージは必ず**日本語**で記載すること。

**PRをマージしたら、そのブランチをリモート・ローカルとも必ず削除すること。** 消し忘れると
マージ済みブランチが溜まり、どれが生きているのか分からなくなる。`dev` と `main` は
恒久ブランチなので削除しない。

```bash
gh pr merge <番号> --merge --delete-branch   # リモートはこれで消える
git checkout main && git pull
git branch -d <branch-name>                  # ローカルも消す
```

取り漏らしの確認（`main` にマージ済みのブランチが残っていないか）:

```bash
git fetch -p
git branch -r --merged origin/main | grep -vE 'origin/(main|dev)$'
git branch   --merged main         | grep -vE '^\*|(main|dev)$'
```

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

Grouping does not merge rows: every article keeps its own record, and the cluster survives as
`group_id` / `group_topic`. Both consumers hierarchize as **category → group**, so
`unify_group_categories()` (`pipeline.py`) runs right after grouping and settles each cluster on one
category by majority vote (ties go to the oldest article, to stay deterministic). Without it the
summarizer's per-article category calls split a cluster in two — the digest generates it twice and
the Viewer renders it under two headings.

### Deduplication

- **RSS**: Miniflux API state. Entries are marked as read in two places, never in the fetcher
  itself: everything `filter_new_articles()` drops (already stored **or** a duplicate URL) is
  marked immediately after the dedup filter (before any early return — the steady state is
  "everything filtered out", so deferring this would leave them unread forever), and newly
  persisted articles are marked from `persist_and_publish()` using `SaveResult.saved`. Both
  paths no-op in `--dry-run` via `MinifluxFetcher.dry_run`
- **Email**: UIDL tracking stored in the `processed_emails` table; messages are **not** deleted from
  the server by default. Because a message that is fetched but not saved would be fully re-downloaded
  every run, `reconcile_email_attempts()` (`pipeline.py`) counts attempts in `email_attempts` /
  `emailAttempts` and gives up on a message once it reaches `email.max_fetch_attempts`
  (default `3`), marking it processed. Articles merely carried over by
  `max_articles_per_run` are never counted — they were not attempted.
  Setting `email.delete_after_processing: true` makes `delete_processed_emails()` (`pipeline.py`)
  remove messages from the mailbox after persistence, via `EmailFetcher.delete_messages()`. Only two
  kinds are deleted: those in `SaveResult.saved`, and the ones `reconcile_email_attempts()` just gave
  up on (it returns those UIDLs) — a message that merely failed this run stays on the server so the
  retry machinery above still works. POP3 message numbers are per-session, so the deletion opens a
  **second session** and rebuilds the UIDL→number map there; `DELE` is only committed by `QUIT`, so
  any error skips `QUIT` and closes the socket instead, deleting nothing. `--dry-run` always skips
  deletion, even with `--output db`
- **Article-level**: `filter_new_articles()` (`pipeline.py`) runs before summarization and applies
  two checks. `db.is_article_processed(source_type, source_id)` drops anything already stored, and
  `db.is_url_processed(url_key(article.url))` drops articles whose **normalized URL** has been
  seen — the same story reaches Miniflux as separate entries when several subscribed feeds carry
  it, so the entry-ID check alone let identical articles through. URLs seen earlier in the *same*
  run are tracked in-memory, since they are not in the DB yet. Combined with
  `summarizer.max_articles_per_run`, the overflow is simply carried over to the next run rather
  than dropped. The cap is applied by `select_articles()`, which allocates the quota round-robin
  across source types so RSS cannot starve email
- **URL normalization** (`urls.py`): `normalize_url()` lowercases scheme/host, strips `www.`, the
  fragment and the trailing slash, and removes **only known tracking parameters** (`utm_*`, `at_*`,
  `fbclid`, …) — dropping the whole query string would collapse unrelated `?id=123` style URLs onto
  one key. `url_key()` is its sha256, stored as `article_summaries.url_key` (SQLite, added by the
  same `PRAGMA table_info` migration as `embedding`) and as the document id of the `articleUrls`
  collection (Firestore). The `articleUrls` write is committed in the **same chunk** as its article,
  so a failed article never leaves its URL marked as seen. Articles with no URL (email) are exempt
- **Grouping vs. dedup**: the embedding grouper only clusters within a single run, so cross-batch
  duplicates have to be stopped here. What survives dedup and *is* clustered stays as separate rows
  sharing a `group_id`; the Viewer folds those into one card at display time

### Storage backends

`database.backend` selects the implementation (`create_database()` in `outputs/database.py`):

- **`sqlite` (default)** — local file at `database.path`; `execution_lock()` is a no-op.
  `save_batch()` stays a single transaction (SQLite has no transaction size limit)
- **`firestore`** — Cloud Run / Firestore; `execution_lock()` is a real distributed lock with a TTL,
  so overlapping runs cannot double-post. A force-cancelled run can leave the lock document behind —
  recovery is documented in `infra/DEPLOYMENT.md`. `save_batch()` splits its writes across
  **multiple commits**, bounded by both write count (`_MAX_BATCH_WRITES`) and estimated size
  (`_MAX_CHUNK_BYTES`), because a single `Transaction too big` failure used to throw away every
  LLM call of the run. A failed chunk is logged and skipped rather than raised, and the batch
  document is committed **last**, so a failing article chunk can no longer leave articles
  pointing at a batch that does not exist (the Viewer queries articles by `batch_id`). The one
  case that still can is the final batch-document write itself failing — `save_batch()` then
  returns `batch_id=None` with a non-empty `saved`, and logs the `batch_id` for manual recovery

Both backends return a `SaveResult` (`models.py`) — `batch_id`, the `(source_type, source_id)`
pairs actually persisted, and a failure count — so mark-as-read and email bookkeeping only ever
act on what really landed in the database.

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

**The Viewer's default category order comes from this same file.** `infra/main.tf`
`yamldecode`s it into `local.category_order` and injects it into the Viewer's Cloud
Run service as the `CATEGORY_ORDER` environment variable, so the Viewer never
duplicates the category names. Reordering `categories.yaml` therefore needs a
`terraform apply` to reach the Viewer — the same cycle a taxonomy change already
requires for the summarizer image. (The Viewer also lets the user override the order
per browser; the value here is only the default.)

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
- `summarizer.max_articles_per_run` — cap on articles processed in one run (default: `100`, max `200`); the remainder is carried over, with the quota allocated round-robin across sources by `select_articles()` (`pipeline.py`). **Paired with `miniflux.fetch_limit` — see below**
- `miniflux.fetch_limit` — how many unread entries to request from Miniflux in one run (default: `100`, max `1000` = Miniflux's own `MaxEntryLimit`). **These two are a pair, and the smaller one wins.** Miniflux caps `/v1/entries` at 100 when `limit` is omitted, so raising `max_articles_per_run` alone changes nothing — the cap never fires because the input never reaches it. Set `fetch_limit` above `max_articles_per_run` (roughly +25%), since dedup drops ~10% of every fetch. The fetcher also pins `order=published_at&direction=asc` so a backlog drains oldest-first
- `summarizer.steps.<step>.thinking` — per-step thinking toggle
- `email.max_fetch_attempts` — give up on a POP3 message after this many runs fetch it without persisting it (default: `3`); prevents a permanently failing message from being fully re-downloaded on every run
- `discord.post_individual_articles` / `embed_color` / `footer_text` — Discord embed tuning. `post_individual_articles` defaults to **`false`**; enabling it re-posts every article as its own embed, duplicating what the category digest already covers
- `llm.provider` / `project_id` / `location` — provider selection and Vertex AI target
- `llm.embedding_model` / `embedding_base_url` / `embedding_api_key` — embedding endpoint, independent of the chat LLM
- `llm.max_retries` / `structured_output` / `extra_body` — LLM behavior tuning
