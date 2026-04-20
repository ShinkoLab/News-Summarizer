# AI ニュース要約システム

ローカル環境で動作する、AIを活用したニュース記事の自動要約・配信システム。
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
- **データ保存**: SQLite への永続化（Webアプリ連携用）

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
│  Discord         │     │  SQLite          │
│  Webhook出力     │     │  データ保存       │
│  (Embed形式)     │     │  (Webアプリ連携)  │
└─────────────────┘     └─────────────────┘
```


## 技術スタック

| カテゴリ | 技術 | 備考 |
|---------|------|------|
| 言語 | Python 3.12+ | `uv` + `mise` で管理 |
| AIモデル | Ollama（または任意のOpenAI互換API） | `llm.base_url` で切り替え可能 |
| AI連携 | openai (Python SDK) | Structured Output を活用 |
| Embedding | Ollama embedding モデル（任意） | グルーピング精度向上に使用 |
| RSS取得 | Miniflux API | HTTP クライアント経由 |
| メール取得 | poplib（標準ライブラリ） | POP3 + UIDL による差分管理 |
| データベース | SQLite | 要約結果の永続化・Webアプリ連携 |
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

- OpenAI SDK を用いた共通 LLM 呼び出しロジック
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

#### `outputs/database.py` — データベース保存

- SQLite に要約結果・メタデータを保存
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
# LLM (OpenAI 互換 API) の設定
llm:
  base_url: "http://127.0.0.1:11434/v1"
  model: "your-model-name"
  # api_key: "your-api-key"  # 省略時は "ollama"
  # embedding_model: "bge-m3"  # Embeddingグルーピングを使う場合

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
  path: "data/news_summarizer.db"

# 要約設定
summarizer:
  individual_max_length: 200
  digest_max_length: 1500
  categories:
    - "テクノロジー"
    - "ビジネス"
    - "科学"
    - "セキュリティ"
    - "AI・機械学習"
    - "プログラミング"
    - "その他"
  # LLMが定義外カテゴリを返した場合のフォールバック（デフォルト: "未分類"）
  # fallback_category: "その他"
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
    category: str       # カテゴリ（設定ファイルの categories から選択）
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
| 差分管理 | `UIDL` でメッセージID取得 → SQLite で管理 |
| パース | `email` 標準ライブラリで MIME パース |
| 本文抽出 | HTML → テキスト変換（`html2text` 等） |
| 削除 | **しない**（サーバー上に保持） |

## AI処理仕様

### 共通設定

- **APIエンドポイント**: 任意の OpenAI 互換 API（`llm.base_url` で指定、デフォルトはローカル Ollama `http://127.0.0.1:11434/v1`）
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

### SQLite 保存

- 各実行をバッチとして記録
- 個別要約・ダイジェストともにDB保存
- Webアプリケーションから `batches` → `article_summaries` を JOIN して参照可能

## 実行方式

### 実行頻度

1日に3〜5回の定期実行を想定。

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
| Discord Webhook 送信失敗 | ログ出力、SQLite保存は継続 |
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
├── CLAUDE.md                  # Claude Code 向けガイド
├── config.yaml.example        # 設定ファイルテンプレート
├── config.yaml                # 設定ファイル（gitignore）
├── pyproject.toml             # Python プロジェクト定義
├── uv.lock                    # 依存ロックファイル
├── mise.toml                  # ランタイム（Python 3.12）指定
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
│   └── database.py           # SQLite データ保存
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

## 将来の拡張

以下は現時点では対象外とし、必要に応じて追加する。

| 拡張項目 | 概要 |
|---------|------|
| **Slack Webhook 出力** | Slack 用の出力モジュール追加 |
| **REST API** | Webアプリ向けの読み取り専用 API サーバー |
| **Webフロントエンド** | 別プロジェクトとして開発 |
| **イベント駆動実行** | cron の代替（ファイル監視、Webhook トリガー等） |

## 免責事項

本プロジェクトは個人の学習および自宅環境での利用を目的としたものです。ISCライセンスに基づき「現状のまま」提供され、動作保証やサポートは一切行いません。利用によるデータの損失等についても責任を負いかねます。

Issueへの対応は気まぐれです。

## AI利用

本プロジェクトにはClaude Code及びGemini CLIを使用しています。
