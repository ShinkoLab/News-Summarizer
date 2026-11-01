# AI ニュース要約システム

RSS とメールマガジンから記事を集め、LLM で要約・分類して、カテゴリ別のダイジェストにまとめるシステム。
結果は Web ビューア（[News-Viewer](https://github.com/ShinkoLab/News-Viewer)）で読めるほか、Discord にも投稿できる。
手元の Docker Compose でも、Google Cloud 上でも動く。

```
Miniflux（RSS） + POP3（メール）
        ↓  記事を取得し、処理済みの記事と重複 URL を除く
  個別要約（LLM）  … 記事ごとに日本語の要約・キーワード・カテゴリ
        ↓
  グルーピング     … 同じ話題の記事をまとめる（LLM、または Embedding の類似度）
        ↓
  ダイジェスト（LLM）… カテゴリ別の散文（既定で全体 3000 文字以内）
        ↓
  SQLite / Firestore に保存（Viewer が読む） + Discord Webhook
```

英語の記事も、出力はすべて日本語になる。

## クイックスタート（Docker Compose）

このリポジトリを clone して、`.env` を数行直し、`docker compose up -d` で試せる。
Miniflux（RSS リーダー）・要約バッチ・Web ビューアがまとめて起動する。必要なのは Docker と、
OpenAI 互換の LLM（ホストの Ollama など）だけ。

```bash
git clone https://github.com/ShinkoLab/News-Summarizer.git
cd News-Summarizer

# 1. 設定ファイルを作る
cp .env.example .env

# 2. .env を編集する。必須なのは LLM_MODEL（と、使う LLM に応じた LLM_BASE_URL / LLM_API_KEY）だけ
#    - 無料: ホストの Ollama   → LLM_MODEL に `ollama list` のモデル名（既定の LLM_BASE_URL のまま）
#    - 有料: OpenRouter など → .env 内の (b)(c) のコメントを外して API キーとモデル名を入れる

# 3. 起動
docker compose up -d

# 4. ブラウザで開く
#    http://localhost:3080  Viewer（最初の要約が出るまで数分〜。それまでは「バッチがありません」）
#    http://localhost:8080  Miniflux（ログインは .env の MINIFLUX_ADMIN_USERNAME / PASSWORD）
```

- **Miniflux は同梱が既定**。初回に定番の RSS フィード5件（`docker/feeds.opml`）を自動登録する。
  フィードの追加・削除は Miniflux の画面（<http://localhost:8080>）で行う。
  自前の Miniflux につなぐ方法は[既存の Miniflux につなぐ](#既存の-miniflux-につなぐ)を参照
- 1 回の実行で記事ごとに LLM を呼ぶ（個別要約 + グルーピング + ダイジェスト）ので、有料の API では費用に注意する。
  お試し向けに既定は「1 回 20 件、3 時間おき」。増やし方は[実行の間隔と手動実行](#実行の間隔と手動実行)を参照
- 止めるとき: `docker compose down`（データも消すなら `down -v`）。ログは `docker compose logs -f summarizer`

## Docker Compose の構成と運用

### 起動するもの

`.env` の `COMPOSE_FILE` で、Miniflux を同梱するかどうかが決まる。

| `.env` の `COMPOSE_FILE` | 起動するもの |
| --- | --- |
| 未設定（既定） | PostgreSQL + Miniflux（同梱）+ `summarizer` + `web` |
| `docker-compose.yml` | `summarizer` + `web` だけ。`MINIFLUX_BASE_URL` / `MINIFLUX_API_KEY` の既存 Miniflux に接続 |

この構成の Viewer の検索はキーワード検索だけ。意味検索（ベクトル検索）は、Firestore を使う
[Google Cloud 構成](#本格運用google-cloud)が前提になっている。

### 同梱の Miniflux

- `.env` の `MINIFLUX_API_KEY` が空なら、管理者ユーザーで API key を自動発行してから要約を始める。
  発行に失敗したときは、定期実行の次の周回でまた試す
- フィードが 0 件のときだけ `docker/feeds.opml` のスターターフィードを取り込む。一度済んだら
  再取り込みしない。`MINIFLUX_IMPORT_STARTER_FEEDS=false` で無効化できる。失敗しても警告だけで続行する
- イメージは `2.3.4` に固定している（`MINIFLUX_IMAGE` で変更可）。すでに新しいバージョンで作った Miniflux の
  volume を使い回す場合は、同じバージョンを `MINIFLUX_IMAGE` に指定する（DB スキーマを戻せないため）

### 既存の Miniflux につなぐ

`.env` に `COMPOSE_FILE=docker-compose.yml` を書き、`MINIFLUX_BASE_URL`（コンテナから届く URL。
ホスト上なら `http://host.docker.internal:8080`）と `MINIFLUX_API_KEY` を設定する。
同梱の Miniflux / PostgreSQL は起動せず、フィードも触らない。

API key は自動発行しないので、`MINIFLUX_API_KEY` は必須。スターターフィードを入れたいときは
`MINIFLUX_IMPORT_STARTER_FEEDS=true` にする（フィードが 0 件のユーザーにだけ登録する）。

### イメージ

- `summarizer` / `web` は既定で GHCR の公開イメージ（`ghcr.io/shinkolab/news-summarizer:2.7.0` /
  `ghcr.io/shinkolab/news-viewer:1.5.3`）を pull する
- 手元のコードで動かすときは `docker compose up -d --build`。Summarizer はこのリポジトリから、
  Viewer は [News-Viewer](https://github.com/ShinkoLab/News-Viewer) のリリースタグ（`v1.5.3`）を
  Compose が URL から直接ビルドする。手元の News-Viewer を開発しているときは
  `.env` に `NEWS_VIEWER_CONTEXT=../News-Viewer` を設定して差し替える。別のバージョンを pull したいときは
  `NEWS_SUMMARIZER_IMAGE` / `NEWS_VIEWER_IMAGE` で指定する
- `--build` したイメージは公開イメージと同じ名前（例: `news-summarizer:2.7.0`）で手元に残り、以後の
  `docker compose up -d` もそれを使い続ける。公開イメージに戻すときは `docker compose pull` する
- pull に失敗した（オフライン・GHCR の障害など）ときは、Compose がソースからのビルドに切り替える
  （数分かかる）

### LLM

ホスト上の Ollama / llama-swap か、クラウドの OpenAI 互換エンドポイントを `.env` で指定する。
`.env.example` に Ollama・OpenRouter・その他の設定例がある。

- `LLM_MODEL` は必須で、空のままだと summarizer が起動時にエラーで終了する
- サーバーが `response_format` を受け付けない／無視するモデルでは `LLM_STRUCTURED_OUTPUT=false` にする
- ホスト上の LLM には `http://host.docker.internal:<ポート>` で届く。ネイティブ Linux でも
  `docker-compose.yml` の `extra_hosts` で設定済みなので、追加の設定は要らない

### 実行の間隔と手動実行

`summarizer` サービスは `NEWS_SUMMARIZER_INTERVAL_SECONDS`（既定 3 時間）間隔で定期実行し続ける。
手動で一度だけ実行したい場合や CLI オプションを渡したい場合は `summarizer-once` を使う。

```bash
docker compose run --rm summarizer-once
docker compose run --rm summarizer-once --dry-run
docker compose run --rm summarizer-once --source email
```

- 実行はロックで1つずつに限られるので、手動実行の前に `docker compose stop summarizer` で止める必要はない。
  `summarizer-once` は定期実行が終わるまで待ってから動く
  （待つのは `NEWS_SUMMARIZER_LOCK_WAIT_SECONDS`、既定 3600 秒まで）。定期実行側は
  手動実行中ならその周回を飛ばす。ロックを取れないファイルシステム（一部の NFS など）では、
  警告を出してロックなしで実行する
- 取得元: `NEWS_SUMMARIZER_SOURCE` の既定は `all`（RSS + メール）。`EMAIL_*` が未設定ならメールは
  自動的に無視される。`rss` / `email` で絞ることもできる
- メール: 処理したメールは既定ではサーバーから削除しない。`EMAIL_DELETE_AFTER_PROCESSING=true` にすると、
  保存できたメールと、`EMAIL_MAX_FETCH_ATTEMPTS`（既定 3）回取得しても保存できずに諦めたメールだけを削除する
- 取り込み量: `.env` の `MAX_ARTICLES_PER_RUN` / `MINIFLUX_FETCH_LIMIT`（対で、小さい方が効く。
  お試し向けの既定は 20 / 25）で調整する。超えた分は次回に繰り越す。HTML 本文のテキスト化
  （LLM の入力トークンを約 8 割減らす）は `MINIFLUX_STRIP_HTML` で切り替えられる（既定は有効）
- 全記事の要約が1件も成功しなかったときも、定期実行の `summarizer` は止まらず次の間隔で再試行する

### 外部に公開するとき

Miniflux と Viewer のポートは既定でループバック（`127.0.0.1`）にだけ公開する。LAN など外へ
出す場合は `.env` に `BIND_ADDRESS=0.0.0.0` を設定し、**先に管理者・Postgres のパスワードを
変更する**こと（Viewer に認証は無い）。

### トラブルシューティング

ログに `Miniflux API キーがありません` が出ていれば、API キーの自動発行が失敗している。
この状態では RSS が取得されない（`--source rss` の `summarizer-once` は終了コード 1、`all` は警告だけで
メール側は動く）。

管理者パスワード（`MINIFLUX_ADMIN_PASSWORD`）は、Postgres の volume を初めて初期化するときにだけ反映される。
後から `.env` で変えると API key の自動発行が 401 で失敗し続けるので、`docker compose down -v` で volume を作り直すか、Miniflux UI でパスワードを合わせるか、
`MINIFLUX_API_KEY` を直接設定する。

それ以外で `summarizer` / `summarizer-once` のログに Miniflux の 401 が出る場合も、API key の
自動発行が失敗している可能性がある。`docker compose ps` で `miniflux` が healthy に
なっていることを確認してから `docker compose run --rm summarizer-once` を再実行する。

## 設定とカスタマイズ

### カテゴリ（`categories.yaml`）

分類に使うカテゴリはリポジトリ直下の `categories.yaml` で定義する。現在は定義順に
**AI・機械学習 / テクノロジー / 経済・ビジネス / 政治・社会 / 事件・事故・災害 / 科学・環境 /
健康・ライフ / カルチャー** の8分類。

- 各カテゴリの `description` と、判定の原則（`principles`）・優先規則（`tiebreak_rules`）は
  そのまま要約プロンプトに入る。分類の精度はこのファイルの文言を直すだけで調整できる
- ダイジェストのカテゴリはこのファイルの定義順に並ぶ
- Compose の Viewer の表示順（`CATEGORY_ORDER`）は、この定義順を `docker-compose.yml` に
  写したもの。並べ替えたら合わせて更新する
- 読み込むパスは `CATEGORIES_PATH` 環境変数で差し替えられる

### Discord への投稿

`.env` の `DISCORD_WEBHOOK_URL`（`config.yaml` なら `discord.webhook_url`）を設定すると、
実行ごとにダイジェストを Embed で投稿する。全体の概要のあとにカテゴリごとの散文が続き、
記事の多いカテゴリには特に目立つ話題を最大3件だけ箇条書きで添える。

`config.yaml` の `discord.post_individual_articles: true` にすると、ダイジェストのあとに全記事の
要約を1件ずつ投稿する。ダイジェストと同じ記事が二重に届くので、既定では無効にしている。
環境変数では設定できないため、Docker Compose では使えない。

## 本格運用（Google Cloud）

家族だけが使う低固定費の構成として、Summarizer を Cloud Run Job、Viewer を Cloud Run、保存先を Firestore、
LLM を Vertex AI で動かせる。構築の手順は [`infra/README.md`](infra/README.md) を参照。

## 開発

### ローカルで実行する

Python 3.12 と [uv](https://docs.astral.sh/uv/) を使う（`mise.toml` で固定）。
ローカル実行の設定は `config.yaml` に書く。全項目の説明は `config.yaml.example` にある。

```bash
uv sync                                  # 依存パッケージのインストール
cp config.yaml.example config.yaml       # 設定ファイルを作り、各サービスの接続情報を入れる
uv run python main.py --dry-run          # 動作確認
```

`--dry-run` は DB 保存・Discord 投稿・Miniflux の既読化をしないだけで、Miniflux からの取得と
LLM の呼び出しは実際に行う（有料の API では料金がかかる）。

```bash
uv run python main.py                          # 通常実行
uv run python main.py --config path.yaml       # 設定ファイルを指定
uv run python main.py --dry-run --output discord  # ドライランでも Discord にだけは投稿する（db / all も指定可）
uv run python main.py --source rss             # RSS だけ（email でメールだけ）
uv run python main.py --stream                 # LLM の出力をターミナルに流す（デバッグ用）
uv run python main.py --debug                  # Embedding・類似度の詳細を表示（デバッグ用）
```

定期実行は cron などで起動する。

```
# 1日5回（7時, 10時, 13時, 16時, 20時）の例
0 7,10,13,16,20 * * * cd /path/to/News-Summarizer && uv run python main.py
```

### Docker で単体実行する

`Dockerfile` のイメージは `config.yaml` を持たず、環境変数だけで動く。コマンドは単発実行の
`run-once`（既定）と、`sleep` を挟んで定期実行し続ける `run-loop` の2つ。

```bash
docker build -t news-summarizer .

# 単発実行（SQLite を使う例。保存先 /data は volume に載せ、パスを明示する。
# LLM_* を渡さないと環境変数のみの既定（Vertex AI）になる）
docker run --rm -v news-data:/data \
  -e DATABASE_BACKEND=sqlite -e SQLITE_DATABASE_PATH=/data/news_summarizer.db \
  -e MINIFLUX_BASE_URL=... -e MINIFLUX_API_KEY=... \
  -e LLM_PROVIDER=openai -e LLM_BASE_URL=... -e LLM_MODEL=... \
  news-summarizer run-once

# 定期実行し続ける場合は、同じ引数でコマンドを run-loop にし、
# -e NEWS_SUMMARIZER_INTERVAL_SECONDS=3600 を足す
```

### 運用スクリプト

`scripts/` に、一度だけ使う運用の道具がある。使い方は `uv run python -m scripts.<名前> --help` で確認できる。

- `skip_stale_backlog.py`: 障害のあとに溜まった古い未読記事・メールを、要約せずに既読化・削除する（既定はドライラン）
- `migrate_sqlite_to_firestore.py`: SQLite の履歴を Firestore へ移す
- `backfill_embedding_vectors.py`: Firestore の Embedding を、Viewer の意味検索で使える形式に変換する

### テストと設計資料

```bash
uv run pytest
```

モジュール構成・重複排除・保存先の仕組みなどの設計は [`AGENTS.md`](AGENTS.md) にまとめてある。
変更履歴は [`CHANGELOG.md`](CHANGELOG.md)。

## 免責事項

本プロジェクトは個人の学習および自宅環境での利用を目的としたものです。ISCライセンスに基づき「現状のまま」提供され、動作保証やサポートは一切行いません。利用によるデータの損失等についても責任を負いかねます。

Issueへの対応は気まぐれです。

## AI利用

本プロジェクトにはClaude Code及びGemini CLIを使用しています。
