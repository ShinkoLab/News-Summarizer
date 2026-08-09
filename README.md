# AI ニュース要約システム

ローカル環境またはGoogle Cloud上で動作する、AIを活用したニュース記事の自動要約・配信システム。
複数ソース（RSS / メールマガジン）から記事を収集し、LLMで要約・分類した上で、Discord等への通知やWebアプリ連携用のデータとして提供する。

## 目次

- [システム概要](#システム概要)
- [アーキテクチャ](#アーキテクチャ)
- [技術スタック](#技術スタック)
- [モジュール構成](#モジュール構成)
- [データフロー](#データフロー)
- [設定ファイル](#設定ファイル)
- [データモデル](#データモデル)
- [入力仕様](#入力仕様)
- [AI処理仕様](#ai処理仕様)
- [出力仕様](#出力仕様)
- [実行方式](#実行方式)
- [ディレクトリ構成](#ディレクトリ構成)
- [セットアップ](#セットアップ)
- [Google Cloudへの移行](#google-cloudへの移行)
- [将来の拡張](#将来の拡張)


## システム概要

### 解決する課題

日常的に多数のニュースソースを追いかける作業は時間がかかる。本システムは、記事の収集・要約・分類・配信を自動化し、効率的な情報収集を実現する。

### 主要機能

- **記事収集**: Miniflux API および POP3 メールサーバーからの記事取得
- **差分管理**: 前回処理分との差分のみを処理（重複回避）
- **類似記事統合**: LLMによる同一トピック記事のグルーピング
- **個別要約**: 各記事の要点を100〜200文字に要約
- **ダイジェスト生成**: カテゴリ別に整理した800〜1500文字のサマリー
- **多言語対応**: 日本語・英語の記事を処理し、出力は日本語に統一
- **配信**: Discord Webhook（Embed形式）での通知
- **データ保存**: ローカルはSQLite、Google CloudではFirestoreへ永続化（Webアプリ連携用）

## アーキテクチャ

```
┌─────────────────┐     ┌─────────────────┐
│  Miniflux API   │     │  POP3サーバー     │
│  (RSS記事)       │     │  (メールマガジン)  │
└────────┬────────┘     └────────┬────────┘
         │                       │
         ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│  RSS取得         │     │  メール取得       │
│  (rss_fetcher)  │     │  (email_fetcher) │
└────────┬────────┘     └────────┬────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
          ┌─────────────────────┐
          │  記事正規化          │
          │  (共通フォーマット化)  │
          └──────────┬──────────┘
                     ▼
          ┌─────────────────────┐
          │  個別要約 (LLM)      │
          │  100〜200文字/記事    │
          └──────────┬──────────┘
                     ▼
          ┌─────────────────────┐
          │  類似記事グルーピング  │
          │  ・Embeddingモード:   │
          │    類似度→クラスタ化   │
          │    +LLMでトピック命名  │
          │  ・LLMモード(デフォルト)│
          │    直接グルーピング    │
          └──────────┬──────────┘
                     ▼
          ┌─────────────────────┐
          │  ダイジェスト生成     │
          │  (LLM)              │
          │  カテゴリ別整理       │
          │  800〜1500文字       │
          └──────────┬──────────┘
                     ▼
         ┌───────────┴───────────┐
         ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│  Discord         │     │ Firestore/SQLite │
│  Webhook出力     │     │  データ保存       │
│  (Embed形式)     │     │  (Webアプリ連携)  │
└─────────────────┘     └─────────────────┘
```


## 技術スタック

| カテゴリ | 技術 | 備考 |
|---------|------|------|
| 言語 | Python 3.12+ | `uv` + `mise` で管理 |
| AIモデル | Vertex AI Gemini / Ollama（または任意のOpenAI互換API） | `llm.provider` で切り替え可能 |
| AI連携 | Google Gen AI SDK / openai (Python SDK) | Structured Output を活用 |
| Embedding | Vertex AI / Ollama embedding モデル（任意） | グルーピング精度向上に使用 |
| RSS取得 | Miniflux API | HTTP クライアント経由 |
| メール取得 | poplib（標準ライブラリ） | POP3 + UIDL による差分管理 |
| データベース | Firestore / SQLite | クラウドとローカルを設定で切り替え |
| 通知 | Discord Webhook | Embed 形式 |
| 設定管理 | YAML | PyYAML を使用 |
| HTTP | httpx | Miniflux API / Webhook 通信 |


## モジュール構成

本システムは以下のモジュール単位で構成する。各モジュールは独立して動作可能な設計とする。

### 1. 記事取得モジュール (`fetchers/`)

外部ソースから記事を取得し、共通フォーマット（正規化済み記事）として返す。

#### `fetchers/rss_fetcher.py` — RSS取得スクリプト

- Miniflux API から**未読記事**を取得
- 取得後、Miniflux 上で**既読にマーク**（差分管理はMiniflux側に委譲）
- 記事を共通フォーマット（`Article` データクラス）に変換して返す

#### `fetchers/email_fetcher.py` — メール取得スクリプト

- POP3 サーバーに接続し、メールを取得
- `UIDL` コマンドで取得したメッセージIDを SQLite に記録し、**処理済みメールをスキップ**
- メール本文（HTML/テキスト）をパースし、共通フォーマットに変換
- サーバー上のメールは**削除しない**

### 2. AI処理モジュール (`summarizer/`)

LLM を使用した記事の分析・要約処理を担当する。

#### `summarizer/llm_client.py` — LLMクライアント

- Vertex AIまたはOpenAI互換APIを用いた共通 LLM 呼び出しロジック
- ステップ別パラメータの解決・マージ
- JSONパースエラー時の自動リトライ（`llm.max_retries` で設定）
- `structured_output: false` 時はプロンプト指示+手動パースにフォールバック

#### `summarizer/summarizer.py` — 個別要約

- 各記事を LLM で要約（記事取得直後に実行）
- 出力言語は**日本語に統一**
- Structured Output で要約テキスト・キーワード・カテゴリを取得

#### `summarizer/grouper.py` — 類似記事グルーピング

- **LLMモード**（デフォルト）: 記事一覧を LLM に渡して同一トピックをグルーピング
- **Embeddingモード**（`use_embeddings: true`）: 個別要約テキストを embedding してコサイン類似度でクラスタリング → LLM はトピック名付けのみ

#### `summarizer/embedder.py` — Embedding取得

- `llm.embedding_model` に設定したモデルで embedding ベクトルを取得
- Ollama の embedding エンドポイントを OpenAI SDK 経由で使用

#### `summarizer/digest.py` — ダイジェスト生成

- 個別要約をカテゴリ別に整理し、全体ダイジェストを LLM で生成
- カテゴリ分類も LLM が実施

### 3. 出力モジュール (`outputs/`)

要約結果を各チャネルに配信する。

#### `outputs/discord_output.py` — Discord 出力

- Discord Webhook API を使用して Embed 形式で投稿
- ダイジェストと個別要約をそれぞれ適切な Embed に整形

#### `outputs/database.py` / `outputs/firestore_database.py` — データベース保存

- FirestoreまたはSQLiteに要約結果・メタデータを保存
- Webアプリケーションからの参照に対応するスキーマ設計
- 処理済みメールIDの管理もここで担当

### 4. 制御モジュール

#### `main.py` — CLIエントリポイント

- CLI引数（`--dry-run` / `--output` / `--source` / `--config` / `--stream` / `--debug`）をパース
- `RunOptions` を構築して `pipeline.run_pipeline()` に委譲するのみ

#### `pipeline.py` — パイプライン本体

- `run_pipeline(config, options)` を公開し、Fetch → Summarize → Group → Digest → Output を順に実行
- `RunOptions`（frozen dataclass）でドライラン・強制出力先・ソース選択・ストリーミング等を保持
- ソース単位・記事単位でエラー分離（例外を握りつぶして続行）

#### `config.py` — 設定読み込み

- YAML 設定ファイルの読み込みとバリデーション

#### `models.py` — データモデル定義

- 各モジュール間で受け渡すデータ構造（dataclass / Pydantic model）の定義
- `Article`, `ArticleGroup`, `GroupingResult`, `ArticleSummary`, `CategoryDigest`, `TopicLabel`, `TopicNamingResult`, `DigestResult`

#### `logger.py` — ロギング設定

- `setup_logging()` / `get_logger()` を提供
- stderr のみに出力（ファイルログなし）
- ログレベルは `logging.level` で制御


## データフロー

### メイン処理フロー

```
1. 設定ファイル読み込み (config.yaml)
2. 記事取得
   a. Miniflux API から未読記事を取得 → 取得直後に既読マーク（--dry-run 時はスキップ）
   b. POP3 サーバーからメールを取得（processed_emails テーブルで処理済みスキップ）
   c. 取得した記事を共通フォーマット (Article) に正規化
3. 新規記事が0件の場合、処理を終了
4. 個別要約
   a. 各記事を LLM で要約 (Structured Output)
   b. 日本語で出力（ArticleSummary: title / summary / keywords / category）
5. 類似記事グルーピング
   a. LLMモード: 記事一覧をそのままLLMに渡してグルーピング
   b. Embeddingモード: 要約テキストをembedding→コサイン類似度でクラスタリング
                       →LLMがトピック名を付与
6. ダイジェスト生成
   a. 個別要約を LLM に渡し、カテゴリ分類 + 全体ダイジェスト生成
7. 出力（--dry-run 時はスキップ。`--output` 指定時は対応ターゲットのみ強制実行）
   a. SQLite にバッチと要約を保存し、処理済みメールの UIDL を `processed_emails` に登録
   b. Discord Webhook で Embed 投稿
```


## 設定ファイル

### `config.yaml`

`config.yaml.example` をコピーして使用する。主要な設定項目は以下の通り。

```yaml
# LLM の設定
llm:
  # provider: "openai"  # "openai"（OpenAI互換エンドポイント、デフォルト） | "vertex"（Vertex AI）
  base_url: "http://127.0.0.1:11434/v1"
  model: "your-model-name"
  # api_key: "your-api-key"  # 省略時は "ollama"
  # provider: "vertex" の場合に指定
  # project_id: "your-gcp-project"
  # location: "asia-northeast1"

  # embedding_model: "bge-m3"  # Embeddingグルーピングを使う場合
  # Embedding だけ別プロバイダに向ける場合（未指定時は上の base_url / api_key を使用）
  # embedding_base_url: "https://openrouter.ai/api/v1"
  # embedding_api_key: "your-embedding-api-key"

  # 全ステップ共通の LLM パラメータ（ステップ別設定で上書き可）
  # parameters:
  #   temperature: 0.3
  #   max_tokens: 8192
  #   reasoning_effort: "medium"

  # APIプロバイダー固有の拡張フィールド（例: Ollama の thinking モード）
  # extra_body:
  #   think: true

  # thinking モード関連
  # gemma4_think: true                    # Gemma 4 向け <|think|> 自動注入
  # disable_temperature_with_thinking: true  # thinking 有効時に temperature を自動除外

  # JSONパース・API エラー時の再試行回数（デフォルト: 3）
  # max_retries: 3
  # Structured Output を使用するか（デフォルト: true。未対応プロバイダでは false）
  # structured_output: true

# Miniflux API の設定
miniflux:
  base_url: "http://your-miniflux-host"
  api_key: "your-miniflux-api-key"

# POP3 メールサーバーの設定
email:
  host: "your-mail-server.example.com"
  port: 995
  username: "your-email@example.com"
  password: "your-email-password"
  use_ssl: true

# Discord Webhook の設定
discord:
  webhook_url: "https://discord.com/api/webhooks/your-webhook-url"
  embed_color: 5814786
  footer_text: "AI News Summarizer"
  post_individual_articles: true  # 個別記事をダイジェスト後に投稿するか

# データベースの設定
database:
  # backend: "sqlite"  # "sqlite"（デフォルト） | "firestore"（Cloud Run 実行時）
  path: "data/news_summarizer.db"
  # backend: "firestore" の場合に指定
  # project_id: "your-gcp-project"
  # firestore_database: "(default)"

# 要約設定
summarizer:
  individual_max_length: 200
  digest_max_length: 3000
  # 1回の実行で処理する記事数の上限（デフォルト: 100）。超過分は次回に繰り越す
  # max_articles_per_run: 100
  # カテゴリ一覧・定義文・フォールバックは categories.yaml で管理する（後述）
  # カテゴリ検証失敗時の再試行回数（デフォルト: 3）
  # category_max_retries: 3
  steps:
    grouper:
      # use_embeddings: true  # Embeddingモードを有効化
      # similarity_threshold: 0.65  # コサイン類似度の閾値（コード上のデフォルトは 0.85）
      parameters:
        temperature: 0.1
    summarizer:
      parameters:
        temperature: 0.3
    digest: {}  # グローバル設定を継承

# ロギング設定
logging:
  level: "INFO"  # DEBUG | INFO | WARNING | ERROR
```

### カテゴリ定義 (`categories.yaml`)

カテゴリ分類の定義はリポジトリ直下の `categories.yaml` に置く。`config.yaml`
（gitignored）とは別ファイルで、機密情報を含まないため git 管理し、Docker
イメージにも同梱される。ローカル実行・Cloud Run 実行の双方がこの同じファイルを
読むため、カテゴリ一覧の二重管理が起きない。読み込むパスは `CATEGORIES_PATH`
環境変数で差し替えできる（既定 `categories.yaml`）。

```yaml
categories:
  - name: AI・機械学習
    description: >
      生成AI・LLM、機械学習・深層学習、AIモデル／研究／製品、
      AIの技術・応用・倫理（技術が主眼のもの）。
  - name: 政治・社会
    description: >
      政治・選挙・政策・外交・国際政治、行政・法律・司法、社会問題・労働、
      教育制度・教育政策。戦争・紛争・軍事行動とその被害もここに含める。
      「国際」「中東」などの地域は軸にせず、記事の内容で判断する。
  - name: 事件・事故・災害
    description: >
      地震・台風・洪水・土砂災害・山火事などの自然災害、航空・鉄道・交通事故、
      火災・爆発・産業事故、殺人・強盗などの犯罪・テロ、避難・救助・被害状況。
      発生と被害そのものを伝える記事が対象。
  # …計8カテゴリ

principles:          # 判定の原則
  - 見出しと第1段落が「何について書かれているか（記事の主眼）」で判断する。

tiebreak_rules:      # 複数カテゴリに該当する場合の優先規則
  - "企業の決算・株価・資金調達・M&A が主眼 → 業種を問わず 経済・ビジネス。"

fallback: 未分類     # 定義外カテゴリが返され続けた場合の値（意図的に categories 外）
```

現在のカテゴリは定義順に **AI・機械学習 / テクノロジー / 経済・ビジネス /
政治・社会 / 事件・事故・災害 / 科学・環境 / 健康・ライフ / カルチャー** の8分類。
`description`・`principles`・`tiebreak_rules` はそのまま要約プロンプトに注入され、
ダイジェストのカテゴリ表示順もこのファイルの定義順に従う（上の並びがそのまま
表示順になる）。分類精度の調整はこのファイルの文言変更だけで完結し、コード変更は不要。

`categories` が空、または名前が重複している場合は設定読み込み時点で
`ValidationError` になる。

> **既存の `config.yaml` からの移行**: `summarizer.categories` と
> `summarizer.fallback_category` は `categories.yaml` へ移設され、設定キーとしては
> 廃止された。`SummarizerConfig` は未知のキーを拒否する（`extra="forbid"`）ため、
> 手元の `config.yaml` にこれらが残っていると起動時に `ValidationError` で
> **失敗する**。両キーを削除すること。

## データモデル

### 共通記事フォーマット (`Article`)

```python
@dataclass
class Article:
    """各ソースから取得した記事の共通フォーマット"""
    source_type: str       # "rss" | "email"
    source_id: str         # ソース固有のID（Miniflux entry_id / POP3 UIDL）
    title: str             # 記事タイトル
    content: str           # 記事本文
    url: str | None        # 記事URL（メールの場合はNone）
    published_at: datetime # 公開日時
    fetched_at: datetime   # 取得日時
    feed_title: str | None # フィード名（RSSの場合）
```

### LLM Structured Output スキーマ

#### グルーピング結果（LLMモード）

```python
class ArticleGroup(BaseModel):
    """類似記事のグループ"""
    group_id: int                # グループID
    topic: str                   # トピック（短い説明）
    article_indices: list[int]   # グループに属する記事のインデックス

class GroupingResult(BaseModel):
    """グルーピング全体の結果"""
    groups: list[ArticleGroup]
```

#### トピック命名結果（Embeddingモード用）

```python
class TopicLabel(BaseModel):
    """クラスタへのトピック名付与結果"""
    group_id: int  # クラスタID
    topic: str     # トピック名（日本語、15文字以内）

class TopicNamingResult(BaseModel):
    """全クラスタのトピック命名結果"""
    topics: list[TopicLabel]
```

#### 個別要約結果

```python
class ArticleSummary(BaseModel):
    """個別記事の要約"""
    title: str          # 要約タイトル（日本語）
    summary: str        # 要約本文（100〜200文字、日本語）
    keywords: list[str] # キーワード（3〜5個）
    category: str       # カテゴリ（categories.yaml の定義から選択）
```

#### ダイジェスト結果

```python
class CategoryDigest(BaseModel):
    """カテゴリ別ダイジェスト"""
    category: str           # カテゴリ名
    articles: list[str]     # 記事の箇条書き（各1〜2文）
    article_count: int      # 記事数

class DigestResult(BaseModel):
    """ダイジェスト全体"""
    overview: str                    # 全体概要（2〜3文）
    categories: list[CategoryDigest] # カテゴリ別ダイジェスト
    total_articles: int              # 総記事数
    generated_at: datetime           # 生成日時
```

### SQLite テーブル設計

```sql
-- 実行バッチの管理
CREATE TABLE batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_articles INTEGER NOT NULL,
    digest_text TEXT  -- ダイジェスト全文
);

-- 個別記事の要約
CREATE TABLE article_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    source_type TEXT NOT NULL,          -- 'rss' | 'email'
    source_id TEXT NOT NULL,            -- ソース固有ID
    original_title TEXT NOT NULL,       -- 元のタイトル
    original_url TEXT,                  -- 元のURL
    summary_title TEXT NOT NULL,        -- 要約タイトル（日本語）
    summary_text TEXT NOT NULL,         -- 要約本文（日本語）
    keywords TEXT NOT NULL,             -- キーワード（JSON配列）
    category TEXT NOT NULL,             -- カテゴリ
    group_id INTEGER,                   -- 類似記事グループID
    group_topic TEXT,                   -- グループトピック
    published_at TIMESTAMP,            -- 元記事の公開日時
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 処理済みメールIDの管理
CREATE TABLE processed_emails (
    uidl TEXT PRIMARY KEY,             -- POP3 UIDL
    processed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- インデックス
CREATE INDEX idx_summaries_batch ON article_summaries(batch_id);
CREATE INDEX idx_summaries_category ON article_summaries(category);
CREATE INDEX idx_summaries_created ON article_summaries(created_at);
```

### Firestore コレクション設計

Google Cloudでは同じデータを`batches`、`articleSummaries`、`processedEmails`へ保存します。
記事ドキュメントIDは`source_type`と`source_id`から決定的に生成し、Cloud Schedulerの重複実行時も
同じ記事を二重保存しません。1回の結果はFirestoreの一括書き込みで確定し、実行の排他制御には
`pipelineLocks/current`の期限付きリースを使います。

## 入力仕様

### Miniflux API

| 項目 | 内容 |
|------|------|
| エンドポイント | `GET /v1/entries?status=unread` |
| 認証 | API Key（`X-Auth-Token` ヘッダー） |
| 差分管理 | 処理後に `PUT /v1/entries` で既読にマーク |
| 取得データ | タイトル、本文（HTML）、URL、公開日時、フィード名 |

### POP3 メール

| 項目 | 内容 |
|------|------|
| プロトコル | POP3 over SSL（ポート 995） |
| 差分管理 | `UIDL` でメッセージID取得 → FirestoreまたはSQLiteで管理 |
| パース | `email` 標準ライブラリで MIME パース |
| 本文抽出 | HTML → テキスト変換（`html2text` 等） |
| 削除 | **しない**（サーバー上に保持） |

## AI処理仕様

### 共通設定

- **APIプロバイダー**: `llm.provider: vertex`ではVertex AI、`openai`ではOpenAI互換APIを使用
- **認証**: Vertex AIはApplication Default Credentials、OpenAI互換APIは`llm.api_key`を使用
- **レスポンス形式**: Structured Output（`response_format` パラメータ）
- **出力言語**: 日本語に統一

### 類似記事統合

LLM に記事タイトルと本文の冒頭を渡し、同一トピックの記事をグルーピングする。

- **入力**: 記事一覧（タイトル + 本文冒頭300文字程度）
- **出力**: `GroupingResult`（Structured Output）
- **判定基準**: 同じニュース・イベントを扱っている記事を同一グループとする

### 個別要約

各記事（またはグループの代表記事）を要約する。

- **入力**: 記事本文（全文）
- **出力**: `ArticleSummary`（Structured Output）
- **文字数**: 100〜200文字
- **言語**: 英語記事も日本語で要約

### ダイジェスト生成

個別要約を統合し、カテゴリ別のダイジェストを生成する。

- **入力**: 個別要約の一覧
- **出力**: `DigestResult`（Structured Output）
- **文字数**: 全体で800〜1500文字
- **構成**: 全体概要 + カテゴリ別サマリー

## 出力仕様

### Discord Embed

Discord Webhook を使用して Embed 形式で投稿する。1回の実行で以下を投稿：

#### ダイジェスト Embed

```
┌─────────────────────────────────────┐
│ 📰 ニュースダイジェスト              │
│ 2026-03-21 18:00                    │
├─────────────────────────────────────┤
│                                     │
│ [全体概要: 2〜3文]                   │
│                                     │
│ 🖥️ テクノロジー (3件)               │
│ [カテゴリ要約]                       │
│                                     │
│ 🔒 セキュリティ (2件)               │
│ [カテゴリ要約]                       │
│                                     │
│ 🤖 AI・機械学習 (4件)               │
│ [カテゴリ要約]                       │
│                                     │
├─────────────────────────────────────┤
│ AI News Summarizer │ 全12件の記事    │
└─────────────────────────────────────┘
```

#### 個別記事 Embed（オプション: 詳細スレッド）

ダイジェストの後にスレッドとして個別記事の要約を投稿することも可能とする。

### Firestore / SQLite 保存

- 各実行をバッチとして記録
- 個別要約・ダイジェストともにDB保存
- FirestoreではViewerが`batch_id`で記事を取得し、SQLiteでは従来どおりJOINして参照可能

## 実行方式

### 実行頻度

ローカルではcron、Google CloudではCloud SchedulerからCloud Run Jobを起動します。
家族利用向けTerraformの既定値は、LLM利用料と外部APIアクセスを抑えるため1日1回です。

```
# cron の例（1日5回: 7時, 10時, 13時, 16時, 20時）
0 7,10,13,16,20 * * * cd /path/to/News-Summarizer && uv run python main.py
```

### エラーハンドリング

| 状況 | 挙動 |
|------|------|
| Miniflux API 接続失敗 | スキップして続行、ログ出力 |
| POP3 接続失敗 | スキップして続行、ログ出力 |
| LLM API 接続失敗 | 処理を中断（要約不可のため） |
| Discord Webhook 送信失敗 | ログ出力、DB保存は継続 |
| 新規記事なし | 正常終了（出力なし） |

### コマンドライン

```bash
# 通常実行
uv run python main.py

# 設定ファイルを指定して実行
uv run python main.py --config /path/to/config.yaml

# ドライラン（DB保存・Discord送信・既読化をスキップ）
uv run python main.py --dry-run

# ドライランでも Discord だけは送信する
uv run python main.py --dry-run --output discord

# 特定ソースのみ実行
uv run python main.py --source rss
uv run python main.py --source email

# LLM出力をターミナルにストリーミング（デバッグ用）
uv run python main.py --stream

# Embedding・類似度の詳細情報を表示（デバッグ用）
uv run python main.py --debug
```

## ディレクトリ構成

```
News-Summarizer/
├── README.md                  # 本ドキュメント
├── AGENTS.md                  # コーディングエージェント向けガイド（実体）
├── CLAUDE.md                  # AGENTS.md への参照のみ
├── CHANGELOG.md               # 変更履歴（Keep a Changelog 準拠）
├── LICENSE
├── categories.yaml            # カテゴリ分類の定義（git管理・イメージに同梱）
├── config.yaml.example        # 設定ファイルテンプレート
├── config.yaml                # 設定ファイル（gitignore）
├── pyproject.toml             # Python プロジェクト定義
├── uv.lock                    # 依存ロックファイル
├── mise.toml                  # ランタイム（Python 3.12 / Terraform）指定
├── main.py                    # CLIエントリポイント
├── pipeline.py                # パイプライン本体（RunOptions + run_pipeline）
├── config.py                  # 設定読み込み・バリデーション
├── models.py                  # データモデル定義
├── logger.py                  # ロギング設定
├── fetchers/                  # 記事取得モジュール
│   ├── __init__.py
│   ├── base.py               # 取得基底クラス
│   ├── rss_fetcher.py        # Miniflux API からの RSS 記事取得
│   └── email_fetcher.py      # POP3 メールマガジン取得
├── summarizer/                # AI処理モジュール
│   ├── __init__.py
│   ├── llm_client.py         # 共通 LLM クライアント・リトライ処理
│   ├── summarizer.py         # 個別要約
│   ├── grouper.py            # 類似記事グルーピング（LLM / Embeddingモード）
│   ├── embedder.py           # Embedding ベクトル取得
│   └── digest.py             # ダイジェスト生成
├── outputs/                   # 出力モジュール
│   ├── __init__.py
│   ├── discord_output.py     # Discord Webhook 出力
│   ├── database.py           # SQLite データ保存・バックエンド選択
│   └── firestore_database.py # Firestore データ保存
├── scripts/
│   └── migrate_sqlite_to_firestore.py # 既存履歴の移行
├── infra/                    # Google Cloud Terraform構成
├── Dockerfile                # Cloud Run Job用イメージ
├── .dockerignore             # イメージから除外するファイル
├── cloudbuild.yaml           # Artifact Registryへのビルド
├── tests/                     # pytest テストスイート
└── data/                      # データディレクトリ（自動生成）
    └── news_summarizer.db    # SQLite データベース
```

## セットアップ

```bash
# 依存パッケージのインストール
uv sync

# 設定ファイルの作成
cp config.yaml.example config.yaml
# config.yaml を編集して各サービスの認証情報を入力

# 動作確認（ドライラン）
uv run python main.py --dry-run
```

## Google Cloudへの移行

家族だけが利用する低固定費構成として、SummarizerをCloud Run Job、Viewerを最小インスタンス0の
Cloud Run、永続化をFirestore、生成AIをVertex AIへ移します。Viewerは直接IAPで許可した
Googleアカウントだけが閲覧できます。サービスアカウント鍵は作らず、Secret Managerと
Application Default Credentialsを使用します。

構築、コンテナ配布、SQLite履歴移行、切替確認の具体的な手順は
[`infra/README.md`](infra/README.md)を参照してください。Terraformには月額予算通知、
Viewer最大1インスタンス、Job再試行なし、Artifact Registryの世代削除も含まれます。

## 将来の拡張

以下は現時点では対象外とし、必要に応じて追加する。

| 拡張項目 | 概要 |
|---------|------|
| **Slack Webhook 出力** | Slack 用の出力モジュール追加 |
| **REST API** | Webアプリ向けの読み取り専用 API サーバー |
| **イベント駆動実行** | cron の代替（ファイル監視、Webhook トリガー等） |

## 免責事項

本プロジェクトは個人の学習および自宅環境での利用を目的としたものです。ISCライセンスに基づき「現状のまま」提供され、動作保証やサポートは一切行いません。利用によるデータの損失等についても責任を負いかねます。

Issueへの対応は気まぐれです。

## AI利用

本プロジェクトにはClaude Code及びGemini CLIを使用しています。
