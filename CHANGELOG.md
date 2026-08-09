# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Firestore バックエンド（`outputs/firestore_database.py`）と `database.backend` による切り替え
- `create_database()` によるバックエンド選択と、SQLite / Firestore 共通のインターフェース
- Firestore の分散実行ロック `execution_lock()`（TTL 付きリース、実行の重複起動を防止）
- 記事レベルの重複排除 `is_article_processed()` と、1実行あたりの処理件数上限
  `summarizer.max_articles_per_run`（超過分は次回実行へ繰り越し）
- 既存 SQLite 履歴の移行スクリプト `scripts/migrate_sqlite_to_firestore.py`（`--dry-run` 対応）
- Cloud Run Job 用の `Dockerfile` / `.dockerignore` / `cloudbuild.yaml`
- Google Cloud のインフラ定義一式（`infra/` の Terraform）と、
  `infra/README.md` / `infra/DEPLOYMENT.md` の手順・作業記録
- Vertex AI プロバイダ対応（`llm.provider: vertex`、`llm.project_id` / `llm.location`）
- 環境変数による設定上書き機構（`_apply_environment_overrides()`）と、
  YAML 不在時に環境変数のみで構成する `load_runtime_config()` / `CONFIG_PATH`
- Embedding 専用エンドポイントの分離（`llm.embedding_base_url` / `llm.embedding_api_key`）
- LLM ステップ別パラメータ（temperature / reasoning_effort）の環境変数対応
- コーディングエージェント向けガイド `AGENTS.md`
- `article_summaries(source_type, source_id)` の複合インデックス
- カテゴリ定義ファイル `categories.yaml` を追加。カテゴリ名に加えて各カテゴリの
  定義文・判定の原則・タイブレーク規則・フォールバック値を構造化して保持し、
  そのまま要約プロンプトに注入する。分類精度の調整はこのファイルの文言変更のみで
  完結し、コード変更を伴わない。読み込みパスは `CATEGORIES_PATH` で差し替え可能
- カテゴリが空、または名前が重複している場合の起動時バリデーション

### Changed

- `AGENTS.md` をエージェント向けガイドの正とし、`CLAUDE.md` は参照のみに変更
- Discord のダイジェスト本文を 4096 文字で切り詰め、投稿全体の失敗を回避
- 設定ファイルが見つからない場合に警告ログを出力（環境変数のみでの起動が
  意図しない設定ミスと区別できなかったため）
- カテゴリを16分類から8分類に再編（政治・社会 / 事件・事故・災害 / 経済・ビジネス /
  テクノロジー / AI・機械学習 / 科学・環境 / 健康・ライフ / カルチャー）。境界の曖昧さによる
  分類のブレを抑えるのが目的
- ダイジェストのカテゴリ表示順を、記事の出現順から `categories.yaml` の定義順に固定
  （未定義カテゴリは末尾）
- カテゴリ定義を `categories.yaml` に一本化し、ローカルの `config.yaml` と Cloud Run の
  環境変数に分かれていた二重管理を解消。`categories.yaml` は git 管理下にあり Docker
  イメージに同梱されるため、ローカルと Cloud Run が同じ定義を読む
- カテゴリ変更の反映手順が `terraform apply` からイメージ再ビルド＋デプロイに変わった

### Removed

- `summarizer.categories` / `summarizer.fallback_category` 設定キー（`categories.yaml` へ移設）
- 環境変数 `SUMMARIZER_CATEGORIES` と Terraform 変数 `summarizer_categories`

### Fixed

- ダイジェストの文字数配分がカテゴリ数の均等割りだったため、記事が集中した
  カテゴリで1件あたり数文字まで潰れ、収まらない記事が黙って欠落していた問題。
  記事数に比例配分し、記事の少ないカテゴリには下限120字を確保する
- `digest_max_length` のコード既定値が 1500 で、環境変数マッピングもないため
  Cloud Run だけがローカル（3000）より短いダイジェストになっていた問題。
  既定値を 3000 に揃えた
- Discord の embed description 上限(4096字)を超えると投稿全体が失敗する
  可能性があった問題。超過分を切り詰めて投稿を通し、footer に注記する
- 文字数超過による切り詰めを Discord の footer が「※一部の生成に失敗」と
  表示していた問題。生成が全件成功していても失敗表示になるため、注記を分離する
- `categories.yaml` をプロセスの CWD 相対で解決していたため、リポジトリ外から
  実行すると import 時点で `FileNotFoundError` になっていた問題
- `AppConfig.taxonomy` が未設定のままだと digest 生成が `AttributeError` で
  実行ごと停止しうる問題
- YAML の折りたたみスカラー由来の改行が要約プロンプトのカテゴリ定義に
  そのまま入り、各行の間に空行が生じていた問題
- `--dry-run` が Firestore の実行ロックを取得・書き込みしていた問題
- ロックドキュメントに `expires_at` が欠けている場合に `KeyError` で
  以降の全実行が停止する問題
- `reload_config` のテストが gitignore 対象の `config.yaml` に依存しており、
  クリーンチェックアウトで必ず失敗していた問題
- Firestore 移行スクリプトが途中失敗すると再実行できなかった問題、および
  移行データと実行時データで `id` フィールドの意味が食い違っていた問題
- `infra/README.md` のシークレット作成手順に `llm_api_key` /
  `embedding_api_key` が含まれておらず、手順どおりでは `terraform apply` が
  失敗していた問題
- 環境変数が `config.yaml` や `--config` で指定した YAML を上書きしていた問題。
  YAML が存在する場合は YAML を唯一の正とし、環境変数による構成は YAML 不在時
  （＝Cloud Run）のみに限定する。`GOOGLE_CLOUD_PROJECT` など gcloud/ADC 系ツールが
  設定する変数が、明示した設定を黙って上書きしていた

### Notes

- カテゴリ定義ブロックの注入により、記事1件あたりの入力トークンが約600〜800増える。
  `max_articles_per_run: 100` で1実行あたり最大 +60〜80k トークン

## [1.0.0] - 2026-05-01

### Added

- フルパイプライン実装: Fetch → Summarize → Group → Digest → Output
- Miniflux RSS フェッチャー（REST API、既読マーク対応）
- POP3 メールフェッチャー（UIDL による重複排除）
- LLM による記事要約（構造化出力 / Pydantic モデル）
- LLM によるトピックグルーピング
- Embedding ベースのグルーピングモード（Ollama 対応、コサイン類似度クラスタリング）
- カテゴリ検証リトライ（マルチターン会話形式）とフォールバック処理
- LLM 呼び出しの共通リトライ機構（`call_with_retry`）
- カテゴリ別ダイジェスト生成（LLM）
- Discord Webhook 出力（Embed 形式）
- SQLite データベース出力（バッチベーススキーマ）
- CLI オプション: `--dry-run` / `--source` / `--output` / `--stream` / `--debug` / `--config`
- LLM トークン使用量の DEBUG ログ出力
- Pydantic による型安全な設定レイヤー
- `main.py` を薄い CLI エントリポイントと `pipeline.py` に分割
- pytest テストスイート

### Fixed

- 要約タイトルが英語になる問題（プロンプトに日本語生成指示を追加）
- ストリーミング時の `_log_usage` 二重呼び出し
- `--debug` 時のログ昇格をアプリ固有名前空間に限定
- Embedding グルーピング結果がダイジェスト出力に反映されない問題
- LLM 出力の箇条書き記号混入による表示崩れ
- ダイジェストの【】括弧および不要なトピックプレフィックスを除去

## [0.1.0] - 2026-04-12

### Added

- プロジェクト初期セットアップ

[Unreleased]: https://github.com/ShinkoLab/News-Summarizer/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ShinkoLab/News-Summarizer/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/ShinkoLab/News-Summarizer/releases/tag/v0.1.0
