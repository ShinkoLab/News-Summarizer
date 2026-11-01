# AGENTS.md

This file provides guidance to coding agents (Claude Code, Codex, etc.) when working with code in this repository.

## Git Workflow

### ブランチ戦略（`main` 一本化）

| ブランチ | 役割 | 分岐元 | マージ先 |
| --- | --- | --- | --- |
| `main` | 本番リリース用。常にリリース可能な状態を保つ | - | - |
| `feature/<name>` | 新機能の開発 | `main` | `main` |
| `fix/<name>` | 不具合の修正 | `main` | `main` |
| `docs/<name>` | ドキュメントのみの変更 | `main` | `main` |

- 恒久ブランチは **`main` だけ**。`dev` は廃止した（2026-10 時点で `main` より 39 コミット
  遅れ、`dev` 固有のコミットはなかった）。統合ブランチは挟まず、作業ブランチは必ず
  最新の `main` から切って `main` へ PR を出す。
- 新しい作業を始める前に必ずブランチを切る。`main` に直接コミット・push しない。必ず PR を通す。
  GitHub のブランチ保護（ルールセット）で `main` への直接 push を禁止する
  （**現状は未設定**。CI は `.github/workflows/publish-image.yml` の PR 検証（スキャンまで）のみ導入済みなので、保護を入れるときはその成功をマージ条件にする）。
- ブランチ名は英小文字のケバブケースにする（例: `feature/strip-html-content`、
  `fix/opencode-session-header`）。

```bash
git fetch origin
git switch --no-track -c feature/<feature-name> origin/main   # worktree 内でも main を checkout せずに切れる
git push -u origin HEAD                                        # 初回 push で upstream を作業ブランチ自身に設定
```

### コミットメッセージ（Conventional Commits）

[Conventional Commits](https://www.conventionalcommits.org/) に従い、**英語** で書く
（2026-10 までの履歴は日本語。過去のコミットは書き換えない）。

```
<type>(<scope>): <subject>

<body>

<footer>
```

- `type` は次のいずれか:
  - `feat`: 新機能
  - `fix`: 不具合の修正
  - `docs`: ドキュメントのみの変更
  - `style`: 動作に影響しない書式の変更
  - `refactor`: 機能追加・修正を伴わないリファクタリング
  - `perf`: パフォーマンス改善
  - `test`: テストの追加・修正
  - `build`: ビルドシステム・依存関係の変更（`pyproject.toml` / `uv.lock` / `Dockerfile`）
  - `ci`: CI 設定の変更（`cloudbuild.yaml` など）
  - `chore`: その他の雑務
  - `revert`: コミットの取り消し
- `scope` は任意。変更対象のモジュール名などを入れる（例: `pipeline`、`fetchers`、
  `summarizer`、`grouper`、`digest`、`outputs`、`db`、`discord`、`config`、`infra`、`scripts`、`release`）。
- `subject` は命令形・小文字始まり・末尾ピリオドなしで、50 文字程度に収める。
- 破壊的変更（config キーの削除・改名、DB スキーマの非互換変更など）は `type!:` とするか、
  footer に `BREAKING CHANGE:` を書く。
- 1 コミットには 1 つの論理的な変更だけを含める。

例:

```
feat(fetchers): strip html from miniflux entries before summarizing
fix(pipeline): exit non-zero when no article was summarized
docs: consolidate branch strategy into main only
chore(release): v2.6.0
```

### ドキュメントの更新

仕様（動作、設定キー、環境変数、DB スキーマ、LLM の出力の型、失敗したときの振る舞い、運用手順）を
変えたら、**同じ PR で** 下の文書も直す。コードだけ直して文書を後回しにしない。

| 変えたもの | 直す文書 |
| --- | --- |
| 処理の流れ、モジュールの役割、重複排除、グルーピング、ダイジェスト、LLM の呼び方、設定の読み込み | `docs/architecture.md` |
| `models.py` の型、SQLite のスキーマ、Firestore のコレクション | `docs/data-model.md` |
| 失敗したときの振る舞い、終了コード、メールの試行回数・削除、実行ロック、ログ、`scripts/` | `docs/operations.md` |
| 利用者の手順、Docker Compose の構成、`.env` の変数 | `README.md`、`.env.example` |
| 設定キー | `config.yaml.example`（環境変数も足すなら `config.py` と `infra/main.tf` も。**Configuration** 参照） |
| Google Cloud の構成・手順 | `infra/README.md`、`infra/DEPLOYMENT.md` |
| 利用者から見える変更 | `CHANGELOG.md` の `[Unreleased]` |

このファイルの **Architecture** 以下も同じ事実を書いているので、`docs/` と食い違わないよう両方を直す
（`docs/` は人が読む日本語の設計資料、このファイルはエージェント向けの規則と注意点）。
PR 前のコードレビューでは、仕様の変更に文書が追いついているかも確かめる。

### PR 前のコードレビュー

PR を作る**前に**コードレビューを回し、**重要度「中」以上の指摘が 0 件になってから** PR を出す。

1. 変更をコミットした状態で `/code-review high` を実行する（Claude Code 以外のエージェントは
   同等の観点 —— 正しさ・回帰・テスト不足 —— でセルフレビューする）
2. 中以上の指摘を修正してコミットし、再度レビューする。0 件になるまで繰り返す
3. 「低」の指摘は対応するか、見送る理由を PR 本文に書く
4. PR 作成後に追加でコミットした場合も、その差分に同じ基準を適用する

`uv run pytest` が通っていることも PR の前提とする。

### リリース

1. 機能 PR のブランチ上で `pyproject.toml` の `version` を上げ（`uv lock` で `uv.lock` も更新）、
   `CHANGELOG.md` の `[Unreleased]` を新バージョンの節に移す（コミットは `chore(release): vX.Y.Z`）。
   `docker-compose.yml` の `summarizer` / `summarizer-once` の既定イメージ
   （`ghcr.io/shinkolab/news-summarizer:X.Y.Z`）と、同じ版を書いている README・`.env.example` の例も上げる。News-Viewer を新しくリリースしたら
   `web` の `image` と `build.context` の `#vX.Y.Z` もそろえて上げる
2. PR を `main` にマージしたら、そのマージコミットに `vX.Y.Z` タグを打って push する
   （作業ブランチの HEAD ではなく `origin/main` を指定する）

   ```bash
   git fetch origin
   git tag vX.Y.Z origin/main && git push origin vX.Y.Z
   ```
3. Cloud Run 用イメージはそのタグでビルドする（`infra/DEPLOYMENT.md` 参照）。あわせて安定版タグ（`vX.Y.Z`）の push で
   GitHub Actions（`.github/workflows/publish-image.yml`）が、gitleaks（全履歴）・Trivy・禁止ファイル検査を
   通過した場合だけ `ghcr.io/shinkolab/news-summarizer` へ multi-arch イメージを公開する（Cloud Build の流れとは別の配布経路）。
   再ビルドはせず、検査したイメージそのものを `news-summarizer-staging` に push して digest を照合し、
   publish ジョブがその digest にタグを付けて本番パッケージへ載せる。ステージングも公開リポジトリに
   リンクされるため Public だが、入るのは検査を通ったイメージだけ。同一リポジトリの PR でも
   push と照合までを試し、その PR の版は終わったら自動で削除する（リリース時の版は出所として残す）

### マージ後のブランチ削除

**PR をマージしたら、そのブランチをリモート・ローカルとも必ず削除すること。** 消し忘れると
マージ済みブランチが溜まり、どれが生きているのか分からなくなる。`main` は恒久ブランチなので削除しない。

```bash
gh pr merge <番号> --merge --delete-branch   # リモートはこれで消える
git switch main && git pull
git branch -d <branch-name>                  # ローカルも消す
```

取り漏らしの確認（`main` にマージ済みのブランチが残っていないか）:

```bash
git fetch -p
git branch -r --merged origin/main | grep -vE 'origin/HEAD|origin/main$'
git branch   --merged main         | grep -vE '^\*|^[+ ] main$'   # `+` は別 worktree で checkout 中
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
config source (see **Deployment** below). `README.md` is for users (quickstart, Compose operation);
`docs/` is the human-readable design (Japanese: `architecture.md`, `data-model.md`, `operations.md`).
Keep this file and `docs/` in sync — see 「ドキュメントの更新」 above.

### Key modules

- **`main.py`** — CLI entry point; parses arguments into `RunOptions`, calls `run_pipeline()`, and turns `RunFailedError` into exit status 1
- **`pipeline.py`** — Orchestrates the full pipeline; defines `RunOptions` (frozen dataclass), `run_pipeline()`, and internal step functions (`fetch_articles`, `summarize_all`, `group_pairs`, `build_digest`, `persist_and_publish`); error isolation is per-source and per-article. The whole run is wrapped in `db.execution_lock()` whenever it writes to the DB (a plain `--dry-run` takes no lock), and already-processed articles are skipped via `db.is_article_processed()` before summarization. If articles were
  attempted but **not a single one** was summarized (e.g. an expired LLM API key), or summaries
  exist but **not a single one** was saved while the run writes to the DB (e.g. a Firestore
  outage), `run_pipeline()` raises `RunFailedError` and `main.py` exits with status 1 — per-article
  error isolation would otherwise end the run normally and Cloud Run would record it as a success.
  Such a total failure is environmental, so it does **not** count toward the email attempt limit
  (otherwise an outage alone would exhaust `max_fetch_attempts` and give up on every message).
  A partial failure (at least one summary saved) still exits 0
- **`config.py`** / **`config.yaml`** — Pydantic config layer; `config.yaml` is gitignored, use `config.yaml.example` as template. Most keys can also be supplied via environment variables (see **Configuration**)
- **`models.py`** — All data structures: `Article` (common fetch format, dataclass), `SaveResult` (what `save_batch()` actually persisted), plus Pydantic models for LLM structured outputs (`ArticleGroup`, `GroupingResult`, `ArticleSummary`, `CategoryDigest`, `TopicLabel`, `TopicNamingResult`, `DigestResult`)
- **`logger.py`** — Logging setup (`setup_logging()` / `get_logger()`); outputs to stderr only, level controlled by `logging.level` in config
- **`urls.py`** — URL normalization for cross-feed dedup (`normalize_url()` / `url_key()`; see **Deduplication**)
- **`fetchers/`** — `MinifluxFetcher` (REST API) and `EmailFetcher` (POP3), both subclasses of `BaseFetcher` (`base.py`); both normalize to `Article` with timezone-aware **UTC** datetimes (`to_utc()` in `base.py`) — mixing naive and aware values makes any later comparison raise `TypeError`
  - `html_text.py` — stdlib `html.parser`-based HTML→text conversion (`html_to_text()`); used by `MinifluxFetcher` when `miniflux.strip_html` is on
- **`summarizer/`** — LLM steps; structured output via Pydantic
  - `llm_client.py` — shared LLM call logic, retry handling, step config resolution; OpenAI-compatible and Vertex AI paths
  - `summarizer.py` — per-article summarization
  - `grouper.py` — topic grouping (LLM-based or embedding-based)
  - `embedder.py` — embedding retrieval (separate endpoint from the chat LLM)
  - `digest.py` — category digest generation
- **`outputs/`** — `create_database()` returns `Database` (SQLite) or `FirestoreDatabase` depending on `database.backend`; both expose `save_batch()` / `is_article_processed()` / `is_url_processed()` / `is_email_processed()` / `mark_email_processed()` / `record_email_attempt()` / `execution_lock()`. Plus `DiscordOutput` (webhook embeds, description truncated at the 4096-char Discord limit)
- **`scripts/`** — `migrate_sqlite_to_firestore.py` (one-shot data migration; `--database` / `--project` required, supports `--dry-run`);
  `backfill_embedding_vectors.py` (one-shot conversion of `articleSummaries.embedding` from a plain
  array to Firestore's `Vector` type — required before the Viewer's kNN search can see a document;
  idempotent; `--project` required, plus `--dry-run` / `--limit` / `--start-after` / `--batch-size`);
  `skip_stale_backlog.py` (marks-as-read/deletes Miniflux/POP3 backlog older than a cutoff after an
  outage, so the pipeline doesn't have to grind through days of stale entries; dry-run by default,
  `--execute` + confirmation prompt (skippable with `--yes`) required to actually change anything — see its module docstring)
- **`docker/`** — container entrypoint for the Docker image and the Compose stack: `entrypoint.sh`
  (`run-once` / `run-loop` modes, forwards SIGTERM/SIGINT; `run-loop` keeps looping when `main.py`
  exits non-zero, `run-once` propagates the exit code) and `provision_miniflux_key.py` (creates a
  Miniflux API key from the admin credentials when `MINIFLUX_API_KEY` is unset)
- **`infra/`** — Terraform for the Google Cloud deployment; see `infra/README.md` and `infra/DEPLOYMENT.md`

### LLM integration

Three providers, selected by `llm.provider`:

- **`openai` (default)** — OpenAI Python SDK pointed at any OpenAI-compatible endpoint
  (local Ollama at `http://127.0.0.1:11434/v1`, OpenRouter, etc.); calls `chat.completions`
- **`openai_responses`** — same OpenAI SDK, but calls the OpenAI Responses API
  (`client.responses.create`/`.parse`) instead of Chat Completions. Needed for endpoints
  that only expose `/v1/responses` for a given model (e.g. OpenCode Zen's `gpt-5.6-luna`,
  `grok-4.6` — see `infra/DEPLOYMENT.md` #11); `messages`→`input`, `max_tokens`→
  `max_output_tokens`, `reasoning_effort`→`reasoning.effort` are translated automatically
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
- `session_header` — header name for a per-run session ID some providers require for optimization
  (e.g. OpenCode Zen's `x-opencode-session`); when set, `get_client()` (`summarizer/llm_client.py`)
  sends a UUID generated once per process under that header name. Unset by default, so nothing is
  sent unless a provider actually asks for it — the header name lives in config, not hardcoded per vendor
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
  `max_articles_per_run` are never counted — they were not attempted. A message whose
  **parsing** fails never becomes an `Article`, so `EmailFetcher.fetch()` isolates it (the rest
  of the mailbox is still fetched), records it in `failed_uidls`, and `run_pipeline()` counts it
  right after fetching — before the "no new articles" early return. A `RETR` failure is a
  connection problem and still aborts the fetch without counting anything
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

**Firestore writes `embedding` as `Vector`, not as a list.** Firestore's vector search
(`findNearest`, used by the Viewer) only considers fields of type `VectorValue`; a plain
`array<double>` is **silently excluded from results** — no error, no log. `_article_writes()`
therefore wraps the value in `google.cloud.firestore_v1.vector.Vector`, and
`estimate_document_size()` has an explicit `Vector` branch because `Vector` subclasses
`collections.abc.Sequence` rather than `list`, and would otherwise fall through to the
`str(value)` path and size commits by the length of a 1536-element repr. The SQLite backend
is unaffected and still stores JSON.

## Deployment

- **`Dockerfile`** — builds on a pinned `uv` image, installs with `uv sync --frozen --no-dev`,
  runs as `nobody`. It does a plain `COPY . .`, so anything not listed in `.dockerignore`
  (which excludes `config.yaml`, `.env*`, `data`, `tests`, `infra`, `.git`, `.venv`, local tool dirs, caches) is baked
  into the image
- **Compose env rules** — the summarizer services are configured purely by environment variables
  (no `config.yaml` in the image), so every variable in `docker-compose.yml` must either exist in
  `_apply_environment_overrides()` or be consumed by `docker/` (`NEWS_SUMMARIZER_SOURCE`,
  `NEWS_SUMMARIZER_INTERVAL_SECONDS`, `MINIFLUX_BUNDLED`, `MINIFLUX_ADMIN_*`,
  `MINIFLUX_IMPORT_STARTER_FEEDS`) or the OS (`TZ`) — add config keys in both places together.
  `web`'s `CATEGORY_ORDER` is a hardcoded copy of `categories.yaml`'s order (Terraform derives it
  in production); update it when reordering categories. See README "Docker Compose の構成と運用"
- **`cloudbuild.yaml`** — Cloud Build config for the summarizer image
- **`docker-compose.yml` + `docker-compose.override.yml`** — self-host stack for trying the project
  (clone → edit `.env` → `docker compose up -d`). `docker-compose.yml` holds `summarizer`
  (run-loop), `summarizer-once` (`manual` profile) and `web` (News-Viewer built straight from a
  repository URL pinned to a release tag via `NEWS_VIEWER_CONTEXT`, `DB_BACKEND=sqlite`, shared
  `summarizer-data` volume read-only). The bundled Miniflux + Postgres live in
  `docker-compose.override.yml`, which Compose auto-loads when `COMPOSE_FILE` is unset (so
  an old `.env` keeps the bundled stack) and which adds `depends_on: miniflux` to the summarizer
  services; setting `COMPOSE_FILE=docker-compose.yml` in `.env` skips it for "existing Miniflux"
  (plus `MINIFLUX_BASE_URL` / `MINIFLUX_API_KEY`). It is split into two files — not `profiles` — because a `depends_on` on an
  undefined service is an error and a profiled dependency is force-started. Only the bundled file
  sets `MINIFLUX_BUNDLED=1` and the admin credentials, so admin-key provisioning and starter-feed
  import (`docker/import_starter_feeds.py` + `docker/feeds.opml`, only when the user has zero
  feeds, once, marker in `/data`; `MINIFLUX_IMPORT_STARTER_FEEDS` overrides) never run against an
  external Miniflux. Defaults are trial-friendly: `MAX_ARTICLES_PER_RUN=20`,
  `MINIFLUX_FETCH_LIMIT=25`, 3h interval, `NEWS_SUMMARIZER_SOURCE=all` (email is skipped with a WARNING
  when `EMAIL_*` is unset, but `--source email` without config still errors)
- **`docker/entrypoint.sh`** — `run-once` / `run-loop`, signal forwarding via `run_bg`. SQLite has
  no execution lock, so `main.py` always starts through `docker/with_lock.py` (an `fcntl.flock` on
  `/data/.news-summarizer.lock`, then `exec`): `run-once` waits (`NEWS_SUMMARIZER_LOCK_WAIT_SECONDS`,
  exit 75 on timeout), `run-loop` skips the cycle. It also creates the SQLite schema at start so
  the Viewer shows "no batches" instead of an error before the first run. `flock(1)` was not used
  because it does not forward signals to the command and is absent on macOS (the test host)
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

`_apply_environment_overrides()` covers the keys the Cloud Run Job needs, **not every key**:
`llm.extra_body`, `llm.gemma4_think`, `summarizer.digest_max_length`,
`summarizer.category_max_retries`, `summarizer.steps.<step>.thinking`, most step `parameters`
(only temperature / reasoning effort), and the `discord.*` embed tuning keys have no environment
variable — on Cloud Run they stay at their defaults. Adding a key there means extending
`_apply_environment_overrides()` *and* `infra/main.tf`. The authoritative env-var → config-key
mapping is the Cloud Run Job definition in `infra/main.tf`.

> Note: with no `config.yaml` present, the env-only defaults target Vertex AI. Local runs should
> always have a `config.yaml`.

Notable optional keys (see `config.yaml.example` for full comments):
- `database.backend` — `sqlite` (default) or `firestore`; `database.path` (SQLite file, default `data/news_summarizer.db`), `database.project_id` / `database.firestore_database` (Firestore)
- `summarizer.category_max_retries` — retry count when the LLM returns a category outside the defined list (default: `3`)
- `summarizer.individual_max_length` / `digest_max_length` — character limits for per-article summaries and the digest. `digest_max_length` (default `3000`) is split across categories by `_allocate_chars()` (`summarizer/digest.py`): the `MIN_CHARS_PER_CATEGORY` floor of 120 is reserved for every category first, then the remainder is distributed **in proportion to article count**, so the total never exceeds the limit
- `summarizer.max_articles_per_run` — cap on articles processed in one run (default: `100`, max `200`); the remainder is carried over, with the quota allocated round-robin across sources by `select_articles()` (`pipeline.py`). **Paired with `miniflux.fetch_limit` — see below**
- `miniflux.fetch_limit` — how many unread entries to request from Miniflux in one run (default: `100`, max `1000` = Miniflux's own `MaxEntryLimit`). **These two are a pair, and the smaller one wins.** Miniflux caps `/v1/entries` at 100 when `limit` is omitted, so raising `max_articles_per_run` alone changes nothing — the cap never fires because the input never reaches it. Set `fetch_limit` above `max_articles_per_run` (roughly +25%), since dedup drops ~10% of every fetch. The fetcher also pins `order=published_at&direction=asc` so a backlog drains oldest-first
- `miniflux.strip_html` — convert each Miniflux entry's HTML body to plain text with `fetchers.html_text.html_to_text()` before it becomes `Article.content` (default: `true`; env `MINIFLUX_STRIP_HTML`; Terraform `miniflux_strip_html`). Miniflux delivers full HTML, and tags/attributes make up most of the LLM input tokens (measured ≈5,200 tokens/article as HTML vs ≈980 as text, about 80% less; input is ~83% of the monthly LLM bill). Markup is not needed for summarization. Set `false` to pass raw HTML through as before (debugging / comparison, or when the summary should depend on HTML structure). Email bodies are already tag-stripped, so this only affects Miniflux
- `summarizer.steps.<step>.thinking` — per-step thinking toggle
- `email.max_fetch_attempts` — give up on a POP3 message after this many runs fetch it without persisting it (default: `3`); prevents a permanently failing message from being fully re-downloaded on every run
- `discord.post_individual_articles` / `embed_color` / `footer_text` — Discord embed tuning. `post_individual_articles` defaults to **`false`**; enabling it re-posts every article as its own embed, duplicating what the category digest already covers
- `llm.provider` / `project_id` / `location` — provider selection and Vertex AI target
- `llm.embedding_model` / `embedding_base_url` / `embedding_api_key` — embedding endpoint, independent of the chat LLM
- `llm.max_retries` / `structured_output` / `extra_body` / `session_header` — LLM behavior tuning
