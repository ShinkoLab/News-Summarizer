# データモデル

型の定義は `models.py`、保存の実装は `outputs/database.py`（SQLite）と
`outputs/firestore_database.py`（Firestore）にある。

## 段階間で受け渡す型

### `Article`（取得した記事）

ソースに関係なく、取得した記事はこの形にそろえる。

| フィールド | 型 | 内容 |
| --- | --- | --- |
| `source_type` | `str` | `"rss"` か `"email"` |
| `source_id` | `str` | Miniflux のエントリ ID か、POP3 の UIDL |
| `title` | `str` | 元のタイトル |
| `content` | `str` | 本文（Miniflux は既定でテキスト化済み） |
| `url` | `str \| None` | 記事の URL。メールは `None` |
| `published_at` | `datetime` | 公開日時（UTC、タイムゾーン付き） |
| `fetched_at` | `datetime` | 取得日時（UTC、タイムゾーン付き） |
| `feed_title` | `str \| None` | フィード名（RSS のとき） |

### `SaveResult`（保存の結果）

`save_batch()` が返す。Firestore は保存を複数のコミットに分けるので、一部の記事だけが保存される
ことがある。既読化とメールの処理済みの記録は、`saved` に入った記事だけに行う。

| フィールド | 内容 |
| --- | --- |
| `batch_id` | 保存したバッチの ID。1件も保存できなければ `None` |
| `saved` | 保存できた記事の `(source_type, source_id)` の一覧 |
| `failed` | 保存できなかった記事の数 |

### LLM の構造化出力

LLM の応答は Pydantic のモデルで受け取る。

| モデル | 段階 | 中身 |
| --- | --- | --- |
| `ArticleSummary` | 個別要約 | `title`（要約タイトル）、`summary`（100〜200 文字）、`keywords`（3〜5 個）、`category` |
| `GroupingResult` / `ArticleGroup` | グルーピング（LLM モード） | グループごとの `group_id`、`topic`、属する記事の番号 `article_indices` |
| `TopicNamingResult` / `TopicLabel` | グルーピング（Embedding モード） | クラスタごとの `group_id` と `topic`（15 文字以内） |
| `DigestResult` | ダイジェスト | `overview`（全体の概要 2〜3 文）、`categories`、`total_articles`、`generated_at` |
| `CategoryDigest` | ダイジェスト | `category`、`summary`（散文 1〜2 段落）、`highlights`（最大 3 件）、`article_count` |

`DigestResult.generated_at` は表示用のローカル時刻で、記事の `published_at`（UTC）とは比べない。

## 保存するもの

1 回の実行が1つのバッチになり、要約した記事はバッチに属する。ダイジェストのうち DB に保存するのは
全体の概要（`overview`）だけ。ただし Discord 配信の復旧用 outbox には、カテゴリ本文と
highlights を含む完成済みの投稿 payload を一時保存する。

### SQLite

`Database.init_db()` が起動時にテーブルを作る。あとから足した列（`embedding`、`url_key`、
`feed_title`）は、無ければ `ALTER TABLE` で足す。

```sql
-- Discord の未完了配信（完了後に削除）
CREATE TABLE discord_outbox (
    id TEXT PRIMARY KEY,               -- 配信ごとの UUID
    batch_id INTEGER NOT NULL,
    payloads TEXT NOT NULL,             -- 投稿 payload の配列を JSON 化した不変の内容
    next_payload INTEGER NOT NULL,     -- 次に投稿する配列インデックス
    created_at REAL NOT NULL,          -- Unix 秒
    next_attempt_at REAL NOT NULL,     -- この Unix 秒以降に再試行
    attempts INTEGER NOT NULL          -- 投稿失敗の回数
);

-- 実行バッチ
CREATE TABLE batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_articles INTEGER NOT NULL,
    digest_text TEXT                    -- ダイジェストの全体の概要
);

-- 記事ごとの要約
CREATE TABLE article_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    source_type TEXT NOT NULL,          -- 'rss' | 'email'
    source_id TEXT NOT NULL,
    original_title TEXT NOT NULL,
    original_url TEXT,
    summary_title TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    keywords TEXT NOT NULL,             -- JSON 配列
    category TEXT NOT NULL,
    group_id INTEGER,
    group_topic TEXT,
    published_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    url_key TEXT,                       -- 正規化した URL の SHA-256。重複判定に使う
    feed_title TEXT,
    embedding TEXT                      -- Embedding モードのとき、ベクトルの JSON 配列
);

-- 処理済みのメール
CREATE TABLE processed_emails (
    uidl TEXT PRIMARY KEY,
    processed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 取得したが保存できなかったメールの試行回数
CREATE TABLE email_attempts (
    uidl TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

インデックスは `batch_id`、`category`、`created_at`、`(source_type, source_id)`、`url_key` に張る。
`url_key` に一意制約は無い（導入前のデータに同じ URL の行がすでにあるため）。

`email_attempts` を `processed_emails` と分けているのは、同じ表にすると試行中のメールを処理済みと
見誤るため。

### Firestore

Google Cloud 構成では、同じ内容を次のコレクションに保存する。

| コレクション | ドキュメント ID | 内容 |
| --- | --- | --- |
| `batches` | バッチ ID（保存時刻のミリ秒） | `executed_at`、`total_articles`、`digest_text`、`status`（`complete` / `partial`） |
| `articleSummaries` | `(source_type, source_id)` の SHA-256 | SQLite の `article_summaries` と同じ項目 |
| `articleUrls` | `url_key` | 処理済みの URL。重複判定に使う |
| `processedEmails` | UIDL の SHA-256 | 処理済みのメール |
| `emailAttempts` | UIDL の SHA-256 | 保存できなかったメールの試行回数 |
| `discordOutbox` | 配信 UUID | SQLite の `discord_outbox` と同じ項目（`id` も格納） |
| `pipelineLocks` | `current` | 実行ロック（[運用](operations.md#二重実行の防止)） |

- 記事のドキュメント ID をソースから決めているので、同じ記事を二度保存しない
- 保存は件数とサイズの両方で区切って、複数のコミットに分ける。1回の大きなコミットが失敗すると、
  その実行の LLM 呼び出しがすべて無駄になるため。失敗したコミットの記事は次回に回す
- 記事と、その記事の `articleUrls`・メールの記録は同じコミットに入れる。記事の保存に失敗した URL だけが
  処理済みとして残ることはない
- `batches` のドキュメントは最後に作る。Viewer は `batch_id` で記事を引くので、バッチの無い記事は
  Viewer から見えない。最後の1回だけが失敗したときはログに `batch_id` を残す
- `embedding` は配列ではなく Firestore の `Vector` 型で書く。配列のままだと、Viewer の意味検索
  （`findNearest`）からエラーも出ずに外れる。古いデータは `scripts/backfill_embedding_vectors.py` で変換する

SQLite の履歴は `scripts/migrate_sqlite_to_firestore.py` で Firestore に移せる。

### Discord outbox の整合性

- `payloads` と `batch_id` は配信作成後に変えない。Webhook URL・トークンは保存しない。
- SQLite は記事・処理済み UIDL・outbox を1トランザクションで保存する。
- Firestore は同じ UUID の outbox を各記事チャンクの原子的コミットに含める。チャンクごとに別の
  配信を作らない。送信開始前なので同じ内容の再書き込みは進捗を失わせない。全チャンク失敗なら
  outbox も存在せず、一部成功や最後の `batches` 文書の失敗なら outbox は残る。
- 投稿が成功するごとに `next_payload` を進め、すべて完了した行／文書は削除する。
  HTTP と DB 更新は原子的ではないため、タイムアウトや進捗保存前の停止で重複投稿は起こり得る。
- 未完了は最大100配信、1配信の JSON payload は最大512 KiB。上限超過時は新規保存を失敗させ、
  既存の保留配信を黙って破棄しない。既存履歴から過去の未送信分を自動生成する移行処理はない。

## Read-only dry runs

During `--dry-run` without an explicit DB output (`--output db` or `--output all`), SQLite reads
a private in-memory snapshot opened from the source with `mode=ro`. Schema
initialization and legacy migrations affect only that snapshot; a missing database
and its parent directory are not created. The snapshot rejects writes and requires
memory proportional to the database size. Committed WAL data is included.
Explicit DB output retains normal initialization, migration and persistence.
Firestore dry runs continue to skip the execution lease and all persistence;
fetching and LLM calls still occur unless replaced by test providers.
