# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
