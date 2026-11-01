# 処理の流れと設計

1 回の実行（バッチ）は取得 → 重複排除 → 個別要約 → グルーピング → ダイジェスト → 出力 の順に進む。
入口は `main.py` で、本体は `pipeline.py` の `run_pipeline()`。

```
Miniflux（RSS） + POP3（メール）
        ↓  fetchers/        記事を共通の Article に変換
  重複排除                   処理済みの記事と、正規化した URL が同じ記事を除く
        ↓  pipeline.py      件数の上限を超えた分は次回へ繰り越す
  個別要約（LLM）            summarizer/summarizer.py  記事ごとに要約・キーワード・カテゴリ
        ↓
  グルーピング               summarizer/grouper.py     LLM で直接、または Embedding の類似度で
        ↓
  ダイジェスト（LLM）        summarizer/digest.py      カテゴリごとの散文と、全体の概要
        ↓
  保存 + 投稿                outputs/                  SQLite / Firestore、Discord Webhook
```

## モジュール

| 場所 | 役割 |
| --- | --- |
| `main.py` | CLI 引数を `RunOptions` にして `run_pipeline()` を呼ぶ。`RunFailedError` を終了コード 1 にする |
| `pipeline.py` | 各段階の呼び出し、重複排除、件数の上限、メールの試行回数の管理、既読化 |
| `config.py` | 設定の読み込みと検証（`config.yaml` か環境変数のどちらか一方） |
| `models.py` | 段階間で受け渡すデータの型（[データモデル](data-model.md)） |
| `urls.py` | 重複判定のための URL の正規化 |
| `fetchers/` | `MinifluxFetcher`（`rss_fetcher.py`）と `EmailFetcher`（`email_fetcher.py`）。HTML のテキスト化は `html_text.py` |
| `summarizer/` | LLM を使う段階。共通の呼び出し・再試行は `llm_client.py`、Embedding の取得は `embedder.py` |
| `outputs/` | `database.py`（SQLite と、保存先の選択）、`firestore_database.py`、`discord_output.py` |
| `docker/` | コンテナのエントリポイント（`entrypoint.sh`）、API キーの自動発行、スターターフィード、実行ロック |
| `scripts/` | 一度だけ使う運用の道具（[運用](operations.md#運用スクリプト)） |
| `infra/` | Google Cloud 用の Terraform（`infra/README.md`、`infra/DEPLOYMENT.md`） |

## 取得

ソースごとにエラーを切り離している。片方の取得に失敗しても、もう片方の記事で実行を続ける。
日時はどちらのソースも UTC のタイムゾーン付きにそろえる（`fetchers/base.py` の `to_utc()`）。

- Miniflux: 未読の記事を公開日時の古い順に、`miniflux.fetch_limit` 件まで取る。本文の HTML は
  既定でテキストにしてから LLM に渡す（`miniflux.strip_html`）。HTML のままだと入力トークンが
  約 5 倍になる。既読化は取得した時点ではしない（[重複排除](#重複排除)を参照）
- POP3: `UIDL` で処理済みのメールを飛ばす。解析に失敗したメールはそのメールだけを除いて、
  残りの取得を続ける。メールはサーバーから既定では削除しない

## 重複排除

要約の前に `filter_new_articles()` で2つを確かめる。

1. 処理済みの記事: `(source_type, source_id)` がすでに DB にある
2. 同じ URL の記事: 正規化した URL の SHA-256（`url_key`）がすでに DB にあるか、同じ実行の中で
   先に出ている。複数のフィードが同じ記事を配信すると、Miniflux ではエントリが別になるため

URL の正規化（`urls.py`）はスキームとホストの小文字化、`www.` とフラグメントと末尾の `/` の
除去、既知のトラッキング用パラメータ（`utm_*`、`fbclid` など）の除去だけを行う。クエリ全体は
消さない（`?id=123` のような URL が1つにまとまってしまうため）。URL の無いメールは対象外。

Miniflux の既読化は2か所で行う。

- 重複排除で除いた RSS の記事は、その場で既読にする（しないと毎回取得され続ける）
- 要約して保存できた RSS の記事は、保存の結果（`SaveResult.saved`）を見て既読にする。
  要約や保存に失敗した記事は未読のまま残り、次回また取得される

`--dry-run` ではどちらも既読にしない。

## 件数の上限

重複排除のあとの記事が `summarizer.max_articles_per_run` を超えたら、超えた分は次回に繰り越す。
`select_articles()` がソースごとに順番に1件ずつ割り当てるので、RSS が多くてもメールの枠は残る。

`miniflux.fetch_limit` と `summarizer.max_articles_per_run` は対で、小さい方が効く。重複排除で
取得のおよそ 1 割が落ちるので、`fetch_limit` を 25% ほど大きくしておく。

## 個別要約

記事ごとに LLM を呼び、`ArticleSummary`（要約タイトル・本文・キーワード・カテゴリ）を受け取る。
英語の記事も日本語で出力する。1件の失敗はその記事だけを飛ばす。

カテゴリは `categories.yaml` の定義から選ばせる。定義にないカテゴリが返ったら
`summarizer.category_max_retries` 回まで再試行し、それでもだめなら `categories.yaml` の
`fallback` にする。

## グルーピング

同じ話題の記事を1つのグループにまとめる。記事の行はまとめずにそれぞれ残し、
`group_id` / `group_topic` で同じグループだと分かるようにする。Viewer はこれを1枚のカードに畳む。

- LLM モード（既定）: 記事のタイトルと本文の先頭 300 文字を並べて LLM に渡し、グループを作らせる
- Embedding モード（`summarizer.steps.grouper.use_embeddings: true`）: 個別要約を Embedding にし、
  コサイン類似度（`similarity_threshold`、既定 0.85）でまとめる。LLM はグループに名前を付けるだけ。
  記事が多くても LLM の入力があふれない。Embedding は保存もして、Google Cloud 構成では Viewer の意味検索に使う（SQLite の Viewer はキーワード検索だけ）

グルーピングが失敗したら、グループなしで続ける。

グループ分けのあと `unify_group_categories()` で、グループ内のカテゴリを多数決で1つにそろえる
（同数なら古い記事のカテゴリ）。ダイジェストも Viewer も「カテゴリ → グループ」の順に並べるので、
そろえないと同じ話題が2つのカテゴリに分かれて出る。

## ダイジェスト

`summarizer/digest.py` が2段階で作る。

1. カテゴリごと: カテゴリ1つにつき LLM を1回呼び、散文の本文と、特に目立つ話題（highlights）を
   作らせる。文字数は `summarizer.digest_max_length`（既定 3000）を、まず各カテゴリに 120 文字ずつ
   確保してから、残りを記事数に比例して配る。highlights は、記事が 5 件未満のカテゴリでは付けず、
   それ以上なら記事数の 1/3 まで、最大 3 件
2. 全体の概要: カテゴリごとの結果から、全体の概要を 2〜3 文で作らせる

カテゴリは `categories.yaml` の定義順に並ぶ。概要の生成に失敗したら概要を空にし、ダイジェスト全体の
生成に失敗したら空のダイジェストで出力を続ける。どちらでも個別要約は保存される。

## 出力

- DB: `save_batch()` が1回の実行を1つのバッチとして保存し、実際に保存できた記事を
  `SaveResult` で返す。既読化とメールの処理済みの記録は、この結果に入った記事だけに行う。
  保存先ごとの違いは[データモデル](data-model.md)を参照
- Discord: ダイジェストを Embed 1つで投稿する（本文は Discord の上限の 4096 文字で切る）。
  Webhook の URL が無ければ何もしない。`discord.post_individual_articles: true` なら、記事ごとの
  要約も 10 件ずつ投稿する。投稿の失敗はログに出すだけで、DB への保存には影響しない

## LLM

`llm.provider` で呼び先を選ぶ。

| `llm.provider` | 呼び先 |
| --- | --- |
| `openai`（既定） | OpenAI 互換の Chat Completions（Ollama、OpenRouter など） |
| `openai_responses` | OpenAI の Responses API。`/v1/responses` しか無いモデル向け |
| `vertex` | Vertex AI（`google-genai`）。`llm.project_id` / `llm.location` が要る |

- 出力は Pydantic のモデルで受け取る構造化出力（Structured Output）。対応しないサーバーでは
  `structured_output: false` にすると、プロンプトで JSON を指示して手で解析する方式になる
- JSON の解析や API のエラーは `llm.max_retries` 回まで再試行する
- 段階（`summarizer` / `grouper` / `digest`）ごとに、`summarizer.steps.<段階>.parameters` で
  温度などを上書きできる
- Embedding は `llm.embedding_base_url` / `llm.embedding_api_key` でチャットと別の呼び先にできる

## 設定の読み込み

設定の出どころはどちらか一方だけで混ざらない。

| 状況 | 設定の出どころ |
| --- | --- |
| YAML ファイルがある（ローカル実行、`--config`） | YAML だけ。環境変数は見ない |
| YAML ファイルが無い（コンテナ。`config.yaml` はイメージに入らない） | 環境変数だけ（`config.py` の `_apply_environment_overrides()`） |

環境変数で設定できるのは、Cloud Run と Docker Compose で使うキーだけ。全項目の説明は
`config.yaml.example` にある。

カテゴリの定義は `categories.yaml` に分けている。git で管理してイメージにも入れるので、
どの実行環境も同じ定義を読む。
