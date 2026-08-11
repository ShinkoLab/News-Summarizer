# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.2.0] - 2026-08-11

### Added

- 同一URLの重複記事の排除。`urls.py` の `normalize_url()` / `url_key()` で記事URLを
  正規化し、`db.is_url_processed()`（SQLite は `article_summaries.url_key` 列、
  Firestore は `articleUrls` コレクションのドキュメントID）で照合する。
  正規化は scheme/host の小文字化・`www.` 除去・fragment/末尾スラッシュ除去に加え、
  既知のトラッキングパラメータ（`utm_*`, `at_*`, `fbclid` 等）のみを落とす。
  クエリを丸ごと捨てると `?id=123` 型の別記事が同一キーに潰れるため。
  `articleUrls` の書き込みは記事サマリと同一チャンクでコミットするので、
  保存に失敗した記事のURLだけが処理済みとして残ることはない
- `pipeline.unify_group_categories()`。同一クラスタ内のカテゴリを多数決で揃える
  （同数時は最も古い記事のカテゴリを採用し、実行ごとに結果が揺れないようにする）
- `feed_title` の永続化。Viewer が類似記事を束ねたカードのソース名として使う

### Fixed

- 取得ソースの違う同じ記事が別レコードとして保存されていた問題。重複排除キーが
  `(source_type, source_id)` = Miniflux の entry ID のみだったため、複数フィードを
  購読していると同じ記事が別 entry ID で降ってきて素通りしていた。本番 Firestore の
  直近6バッチ250件で同一URLが12組、うち9組はバッチ跨ぎ。グルーピングは1回の実行内でしか
  クラスタリングしないため検出できなかった。`filter_new_articles()` が entry ID とURLの
  2段階で判定し、除外した RSS エントリは既存の `mark_as_read` 経路に乗せて既読化する
  （既読化しないと重複エントリが毎回 Miniflux から降ってくる）
- 同じニュースがカテゴリ違いで2箇所に分かれる問題。ダイジェストも Viewer も
  「カテゴリ → グループ」で階層化するため、要約LLMが同じニュースに違うカテゴリを付けると
  クラスタが割れていた（実データで「北日本東日本の大雨警戒」が 社会 と 環境 に分断）

### Changed

- `scripts/migrate_sqlite_to_firestore.py` の1バッチあたりの移行上限を 400 → 200 件に変更。
  1記事あたりの書き込みが `articleSummaries` + `articleUrls` の2件になったため、
  Firestore の 500 writes/commit に収める

## [2.1.0] - 2026-08-09

### Added

- `email.max_fetch_attempts`（環境変数 `EMAIL_MAX_FETCH_ATTEMPTS`、既定 `3`）。
  取得できるが保存に至らないメールを、この回数に達した時点で処理済みとして打ち切る

### Fixed

- Firestore の `save_batch()` が全件を1トランザクションでコミットしており、
  失敗すると要約・グルーピング・ダイジェストの LLM 呼び出しが丸ごと無駄になっていた問題。
  書き込み件数（`_MAX_BATCH_WRITES` = 450）と推定サイズ（`_MAX_CHUNK_BYTES` = 1 MiB）の
  両方で複数コミットに分割し、途中のチャンクが失敗しても例外を投げずに続行する。
  バッチドキュメントは最後にコミットし、`status` は部分保存なら `"partial"`、
  `total_articles` は実際に保存できた件数になる。
  両バックエンドの `save_batch()` は保存できた記事を示す `SaveResult` を返すようになり、
  既読化・メールの処理済みマークは実際に永続化された記事だけを対象とする
- 取得したが保存しなかった記事を取得元から外す経路が無く、取得コストが単調増加していた問題。
  RSS は `is_article_processed()` で除外した記事を（早期 return より前に）Miniflux で
  既読化するようになり、「DB保存済みだが未読」の滞留が解消する。
  メールは `email_attempts` / `emailAttempts` で試行回数を数え、
  `email.max_fetch_attempts` に達したものを処理済みとして打ち切る
- `summarizer.max_articles_per_run` の枠を RSS が使い切り、メールが永久に処理されない
  ことがあった問題。`select_articles()` がソース種別ごとにラウンドロビンで枠を配分する
- 記事数が増えると Firestore への一括保存が
  `400 Transaction too big` で失敗していた問題（80記事で発生）。
  `embedding`（1536要素の配列）の各要素が Firestore に自動インデックスされ、
  1ドキュメントあたり約1536のインデックスエントリがトランザクションサイズに
  算入されていた。ドキュメント本体は80件でも 1.37 MiB しかなく、超過分はすべて
  インデックス書き込み。`infra/main.tf` の `google_firestore_field` で
  `embedding` をインデックス対象から除外して解消。
  類似記事の統合はアプリ側でコサイン類似度を計算しており Firestore の
  インデックスは使わないため機能影響はなく、ベクトルデータも保持される。
  **インフラのみの変更で、コード変更・イメージ再ビルドは不要**（`terraform apply` で反映）

## [2.0.1] - 2026-08-09

### Fixed

- `summarizer.individual_max_length` と
  `summarizer.steps.grouper.similarity_threshold` に環境変数マッピングが無く、
  Cloud Run だけがコード既定値（それぞれ 200 / 0.85）で動作していた問題。
  ローカルで調整した値（500 / 0.7）が本番に届かず、個別要約が短いまま
  カテゴリ別ダイジェストの入力になっていた。
  `SUMMARIZER_INDIVIDUAL_MAX_LENGTH` / `GROUPER_SIMILARITY_THRESHOLD` を追加し、
  Terraform 変数 `summarizer_individual_max_length` /
  `grouper_similarity_threshold` から設定できるようにする

## [2.0.0] - 2026-08-09

Google Cloud（Cloud Run Job + Firestore）での実行に対応し、カテゴリ分類を
`categories.yaml` に一本化、ダイジェストを散文形式へ刷新したメジャーリリース。

### Breaking

既存の設定はそのままでは動作しない。アップグレード時に以下の対応が必要。

| 対象 | 変更 | 必要な対応 |
|---|---|---|
| `config.yaml` | `summarizer.categories` を削除 | 手元の `config.yaml` から削除する。残っていると `extra="forbid"` により**起動時に `ValidationError` で失敗する** |
| `config.yaml` | `summarizer.fallback_category` を削除 | 同上。フォールバック値は `categories.yaml` の `fallback` へ移設 |
| 環境変数 | `SUMMARIZER_CATEGORIES` を廃止 | Cloud Run Job の env から削除する |
| Terraform | 変数 `summarizer_categories` を削除 | `terraform.tfvars` から削除する。残っていると `apply` が "Value for undeclared variable" で失敗する |
| 設定の優先順位 | YAML が存在する場合、環境変数による上書きを行わなくなった | 環境変数で値を注入していた場合は `config.yaml` 側に書く。環境変数のみでの構成は YAML 不在時（＝Cloud Run）でのみ有効 |
| 既定値 | `discord.post_individual_articles` が `true` → `false` | 個別記事の投稿を続けたい場合は明示的に `true` を設定する |
| 既定値 | `summarizer.digest_max_length` が `1500` → `3000` | 従来の長さに戻す場合は明示的に `1500` を設定する |

カテゴリ定義の変更手順も変わった。`terraform apply` ではなく、`categories.yaml` を
編集して**イメージを再ビルド・デプロイ**する。初回移行時は「イメージを push →
`summarizer_image` を更新して apply」の順で行うこと（順序を誤ると旧イメージが
カテゴリ一覧を空のまま起動し、全記事が未分類になる）。詳細は `infra/DEPLOYMENT.md`。

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

- カテゴリ別ダイジェストを箇条書きの羅列から**散文＋特筆トピック最大3件**に変更。
  従来はグループごとに1行ずつ箇条書きを出していたため行数が多く通読しづらかった。
  カテゴリ本文を1〜2段落の散文でまとめ、その下に重要なトピックだけを短く添える。
  トピックの件数は記事数に応じて決まり、5件未満のカテゴリは散文のみとする
  （記事が少ないと散文が全記事を言い切ってしまい、箇条書きが本文の言い換えになるため）
- `discord.post_individual_articles` の既定を `true` → `false` に変更。
  有効時は全記事がカテゴリ別ダイジェストと個別 Embed で二重に配信されていた
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
  全カテゴリぶんの下限120字を先に確保したうえで、残りを記事数に比例配分する
  （合計が `digest_max_length` を超えない）
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

[Unreleased]: https://github.com/ShinkoLab/News-Summarizer/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/ShinkoLab/News-Summarizer/compare/v2.0.1...v2.1.0
[2.0.1]: https://github.com/ShinkoLab/News-Summarizer/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/ShinkoLab/News-Summarizer/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/ShinkoLab/News-Summarizer/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/ShinkoLab/News-Summarizer/releases/tag/v0.1.0
