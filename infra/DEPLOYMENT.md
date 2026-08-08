# GCPデプロイ作業記録・運用手順

News Summarizer（本リポジトリ）と News Viewer（`../News-Viewer`）を
GCPプロジェクト `<PROJECT_ID>` に実際にデプロイし、動作確認を行った際の
作業記録と、以後の構築・設定変更手順をまとめたもの。

`infra/README.md` がクイックスタート、本ドキュメントは実際に踏んだ手順と
ハマりどころを含めた詳細版という位置づけ。

## 前提構成

- GCPプロジェクト: `<PROJECT_ID>`（請求先アカウント: General Account）
- リージョン: `asia-northeast1`
- LLM: OpenCode Zen (go) 経由の `gpt-5.6-luna`（OpenAI互換 Chat Completions API）
- RSSソース: Miniflux SaaS版（`https://reader.miniflux.app`）
- メール取得: 未使用（ダミー値で運用、失敗してもソース単位のエラー分離で無視される）
- Viewerアクセス制御: IAP（Identity-Aware Proxy）、許可ユーザーは `viewer_users` で管理

## 初回構築手順

### 0. 前提ツール

```bash
mise use terraform@1.12.2   # 本リポジトリでは mise 経由で terraform を利用
gcloud auth login
gcloud auth application-default login
gcloud auth application-default set-quota-project <PROJECT_ID>
```

`gcloud auth application-default login` だけだと ADC のクォータプロジェクトが
別プロジェクト（gcloud のデフォルト等）のままになることがあり、
`terraform plan/apply` で予期しない権限エラーが出ることがある。
必ず `set-quota-project` で対象プロジェクトに合わせること。

### 1. GCPプロジェクトの準備

新規プロジェクトを作成し、請求先アカウントを紐付ける（既存プロジェクトに同居させず、
専用プロジェクトを切った方がIAM境界・予算アラートの管理が楽になる）。

```bash
gcloud projects create <PROJECT_ID>
gcloud billing projects link <PROJECT_ID> --billing-account=<BILLING_ACCOUNT_ID>
```

### 2. `config.yaml` をクラウド構成に合わせて更新（ローカル動作確認用）

`config.yaml` は `.gitignore` 対象なのでリポジトリには含まれない。ローカルでの
疎通確認・移行スクリプト実行用に、実際の値（APIキー含む）を記入する。

```yaml
llm:
  base_url: "https://opencode.ai/zen/go/v1"
  model: "gpt-5.6-luna"
  api_key: "実際のキー"
  structured_output: false   # 後述: OpenCode Zen は Structured Output を守らないため false 必須

miniflux:
  base_url: "https://reader.miniflux.app"   # 末尾に /v1/ を付けない（コード側で自動付与）
  api_key: "実際のキー"
```

APIキーの疎通確認は `curl` や `OpenAI` SDK 経由で行う。**素の `urllib` など
デフォルトUser-AgentのHTTPクライアントはCloudflareのbot対策（error code 1010）で
弾かれることがある**ため、疎通確認に失敗しても即座に「キーが無効」と判断しない。
アプリ本体が使う `openai` SDK・`httpx` は正常に通ることを別途確認する。

### 3. `infra/terraform.tfvars` を作成

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
```

`project_id`、`viewer_users`、`llm_base_url`、`llm_model`、`miniflux_base_url`、
`summarizer_categories`（デフォルトで日本語16分類が入っている）などを実際の値に。
`summarizer_image` / `viewer_image` は後述のビルド後に設定するので、初回は
ダミータグ（例: `:PLACEHOLDER`）で構わない。

### 4. 基盤リソースを先に作成（Secret Managerの入れ物・Firestore・Artifact Registry）

```bash
terraform init
terraform plan \
  -target=google_project_service.required \
  -target=google_artifact_registry_repository.containers \
  -target=google_firestore_database.default \
  -target=google_secret_manager_secret.miniflux_api_key \
  -target=google_secret_manager_secret.email_password \
  -target=google_secret_manager_secret.discord_webhook_url \
  -target=google_secret_manager_secret.llm_api_key \
  -out=plan_stage1.tfplan
terraform apply plan_stage1.tfplan
```

### 5. Secret Managerに値を投入

```bash
printf '%s' "実際の値" | gcloud secrets versions add news-miniflux-api-key --project=<PROJECT_ID> --data-file=-
printf '%s' "実際の値" | gcloud secrets versions add news-llm-api-key --project=<PROJECT_ID> --data-file=-
printf '%s' "実際の値" | gcloud secrets versions add news-discord-webhook-url --project=<PROJECT_ID> --data-file=-
printf '%s' "unused-placeholder" | gcloud secrets versions add news-email-password --project=<PROJECT_ID> --data-file=-
```

**注意**: `python3 -c "print(key)" | gcloud secrets versions add ...` のように
`print()` を使うと末尾に改行が付与され、HTTPヘッダーに使われた際に
`httpx.LocalProtocolError: Illegal header value b'...\n'` で落ちる。
`print(key, end="")` にするか `printf '%s'` を使うこと。

### 6. コンテナイメージのビルド＆プッシュ

各リポジトリのルートで、同一コミットSHAをタグにして実行する。

```bash
# News-Summarizer
gcloud builds submit --project=<PROJECT_ID> --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_TAG=$(git rev-parse --short HEAD)

# News-Viewer
cd ../News-Viewer
gcloud builds submit --project=<PROJECT_ID> --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_TAG=$(git rev-parse --short HEAD)
```

**注意**: News-ViewerのDockerfileは `RUN --mount=type=cache` を使うが、Cloud
Buildのデフォルトdockerビルドステップ（`gcr.io/cloud-builders/docker`）は
BuildKitが無効なため `the --mount option requires BuildKit` で失敗する。
`cloudbuild.yaml` のビルドステップに以下を追加して解消済み。

```yaml
steps:
  - name: gcr.io/cloud-builders/docker
    env:
      - DOCKER_BUILDKIT=1
    args: [build, ...]
```

### 7. `terraform.tfvars` にイメージURLを反映し、残りのリソースを作成

```bash
terraform plan -out=plan_full.tfplan
terraform apply plan_full.tfplan
```

`google_billing_budget` の作成で
`The billingbudgets.googleapis.com API requires a quota project` エラーが出る場合、
`versions.tf` のproviderブロックに以下を追加し、`cloudresourcemanager.googleapis.com`
を有効化してから再実行する（対応済み）。

```hcl
provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}
```

```bash
gcloud services enable cloudresourcemanager.googleapis.com --project=<PROJECT_ID>
```

### 8. IAPのOAuthクライアントを手動設定（個人プロジェクト特有の手順）

2026年1月にIAP OAuth Admin API（`gcloud iap oauth-brands` 等）が新規プロジェクト
向けに廃止されたため、Consoleでの手動設定が必須になっている
（Workspace組織に属さない個人プロジェクトはそもそも対象外）。

1. **OAuth同意画面（Google Auth Platform）を設定**
   `https://console.cloud.google.com/auth/overview?project=<PROJECT_ID>` で
   Audience を **External** にしてブランディングを作成し、
   Audienceページでテストユーザーとして許可したいGoogleアカウントを追加する。
2. **OAuthクライアントIDを作成**（同意画面とは別物）
   `https://console.cloud.google.com/apis/credentials?project=<PROJECT_ID>` で
   「OAuth クライアント ID」（アプリケーションの種類: ウェブ アプリケーション）を作成し、
   クライアントID・シークレットを控える。
3. **リダイレクトURIを追加**（作成したクライアントを編集）
   ```
   https://iap.googleapis.com/v1/oauth/clientIds/<CLIENT_ID>:handleRedirect
   ```
4. **IAPにOAuthクライアントを割り当て**（`iap_settings.yaml` は `.gitignore` 済み）
   ```yaml
   accessSettings:
     oauthSettings:
       clientId: "<CLIENT_ID>"
       clientSecret: "<CLIENT_SECRET>"
   ```
   ```bash
   gcloud iap settings set iap_settings.yaml \
     --project=<PROJECT_ID> --resource-type=cloud-run \
     --region=asia-northeast1 --service=news-viewer
   ```
   設定が反映されるまで数十秒〜数分かかることがある（それまでは502が返る）。

### 9. 動作確認

```bash
gcloud run jobs execute news-summarizer --project=<PROJECT_ID> --region=asia-northeast1 --wait
```

ブラウザで許可ユーザーのGoogleアカウントでログインし、Viewer URL（terraform出力の
`viewer_url`）にアクセスして表示を確認する。

## 遭遇した不具合と対処（設計上の注意点）

デプロイ作業中に発覚した、コード側の不具合。今後も同様の環境変数追加漏れが
起きうるため、`config.py` の `_apply_environment_overrides()` にフィールドを
追加する際はセットで対応すること。

### 1. `LLM_STRUCTURED_OUTPUT` が環境変数から渡せなかった

Cloud Run実行環境は `config.yaml` を持たず、環境変数のみで設定を組み立てる
（`load_runtime_config()`）。`llm.structured_output` に対応する環境変数マッピングが
存在せず、常にデフォルト値 `true` になっていた。OpenCode Zen (go) はStructured
Outputのレスポンス形式を守らずMarkdown形式のテキストを返すため、全記事の
要約がJSONパースエラーで失敗する不具合になっていた。
`LLM_STRUCTURED_OUTPUT` 環境変数を追加し、Terraform側で `false` を設定して解消。

### 2. `SUMMARIZER_CATEGORIES` が環境変数から渡せなかった（最も影響が大きかった）

同様に `summarizer.categories`（分類カテゴリ一覧）にも環境変数マッピングが
存在せず、Cloud Run上では**空リスト**になっていた。空リストなのでLLMがどんな
値を返しても定義済みカテゴリに一致せず、**全記事が `category_max_retries`
（デフォルト3回、計4回）のリトライを消費した末に `fallback_category`
（「未分類」）へフォールバックしていた**。記事1件あたり本来1回で済むはずの
LLM呼び出しが最大4倍消費される、コスト面で最も深刻な不具合だった。
`SUMMARIZER_CATEGORIES`（カンマ区切り文字列）を追加し解消。

この不具合が直る前に、Cloud Schedulerの初回自動実行（8:00 JST起動想定、
実際は`schedule = "0 7 * * *"`なので7:00 JST）が走ってしまい、未読記事80件が
すべて「未分類」でFirestore・Discordに登録され、Minifluxも既読化された。
実害は軽微（誤分類のみ、データ欠損なし）と判断しそのまま許容している。

### 3. Cloud Buildでのdockerビルドがビルド互換性問題で失敗

前述「6. コンテナイメージのビルド＆プッシュ」参照（`DOCKER_BUILDKIT=1`）。

### 4. Terraform applyでbilling budget作成時にquota projectエラー

前述「7. 残りのリソースを作成」参照（`user_project_override` / `cloudresourcemanager.googleapis.com`）。

### 5. IAPのOAuth Admin API廃止

前述「8. IAPのOAuthクライアントを手動設定」参照。

### 6. パイプラインの排他ロックが強制キャンセル後に残留する

`outputs/firestore_database.py` の `execution_lock()` はFirestoreの
`pipelineLocks/current` ドキュメントでリース制御しており、TTLは2時間。
`gcloud run jobs executions cancel` で実行を強制終了すると、Pythonの
`finally`節（ロック解放処理）が実行されないため、ロックが残ったまま次回実行が
`RuntimeError: 別のパイプライン実行が進行中です。` で即失敗することがある。
2時間待てば自動失効するが、すぐ再実行したい場合は手動で削除する。

```bash
uv run python3 -c "
from google.cloud import firestore
client = firestore.Client(project='<PROJECT_ID>')
client.collection('pipelineLocks').document('current').delete()
"
```

通常運用（Cloud Schedulerからの正常完了、または `--wait` を付けた手動実行を
最後まで待つ）ではロックは正しく解放されるため、この対応が必要になるのは
デバッグ目的で実行を中断した場合のみ。

## 設定変更手順

### LLM設定を変更する（プロバイダ・モデル・エンドポイント）

1. ローカル `config.yaml` の `llm.*` を更新し、疎通確認する
   （`openai` SDK の `chat.completions.create` で実際にテストすること。
   `curl`/`urllib` 単体での成功はCloudflare越しの疎通確認にはなるが、
   Structured Output対応可否までは分からない）。
2. `infra/terraform.tfvars` の `llm_base_url` / `llm_model` を更新。
3. 新しいエンドポイントがStructured Output（`response_format`によるJSON
   Schema指定）に対応しているか確認する。対応していなければ
   `infra/main.tf` の `LLM_STRUCTURED_OUTPUT` を `"false"` のままにする
   （対応していれば `"true"` に変更して呼び出し回数を削減できる）。
4. APIキーが変わる場合は `gcloud secrets versions add news-llm-api-key
   --project=<PROJECT_ID> --data-file=-` で新バージョンを追加
   （`print(key, end="")` または `printf '%s'` で改行を含めないこと）。
5. `terraform apply` を実行（Job定義の環境変数が更新される）。

### カテゴリ一覧を変更する

`infra/variables.tf` の `summarizer_categories`（デフォルト値）を編集するか、
`terraform.tfvars` で上書きして `terraform apply`。ローカル動作確認用に
`config.yaml` の `summarizer.categories` も同じ内容に合わせておくとよい。

### Minifluxのエンドポイントを変更する

`terraform.tfvars` の `miniflux_base_url` を更新し `terraform apply`。
末尾に `/v1/` を含めないこと（コードが `{base_url}/v1/entries` を組み立てる）。
APIキーは `news-miniflux-api-key` シークレットに新バージョンを追加する。

### コード変更をデプロイに反映する

```bash
# 変更したリポジトリのルートで
gcloud builds submit --project=<PROJECT_ID> --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_TAG=$(git rev-parse --short HEAD)
```

`infra/terraform.tfvars` の `summarizer_image` / `viewer_image` を新しいタグに
書き換えて `terraform apply`。

### IAPの許可ユーザーを追加・削除する

`infra/terraform.tfvars` の `viewer_users` を編集して `terraform apply`
（`google_iap_web_cloud_run_service_iam_binding.family` が更新される）。
OAuth同意画面が「テスト」モードのままの場合は、Audienceページでテスト
ユーザーとしても追加する必要がある（`https://console.cloud.google.com/auth/audience?project=<PROJECT_ID>`）。

## 日常運用・トラブルシュート

```bash
# 直近の実行一覧
gcloud run jobs executions list --project=<PROJECT_ID> --region=asia-northeast1 --job=news-summarizer --limit=10

# 特定実行のログ
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="news-summarizer" AND labels."run.googleapis.com/execution_name"="<EXECUTION_NAME>"' \
  --project=<PROJECT_ID> --format="value(timestamp,textPayload)" --order=asc

# 手動実行
gcloud run jobs execute news-summarizer --project=<PROJECT_ID> --region=asia-northeast1 --wait

# Schedulerの設定確認（発火時刻など）
gcloud scheduler jobs describe news-summarizer-daily --project=<PROJECT_ID> --location=asia-northeast1
```

Cloud Scheduler は `schedule = "0 7 * * *"`（Asia/Tokyo）で毎日7:00に自動実行される。
デプロイ直後にスケジュール時刻をまたぐと、修正前のイメージで自動実行されてしまう
ことがあるため、設定変更後は次回のスケジュール実行前に手動実行で動作確認しておくと安全。
