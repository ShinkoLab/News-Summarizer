# Google Cloud deployment

家族利用向けの低固定費構成です。Cloud Run Job、Cloud Run Service、Firestore、
Cloud Scheduler、Secret Manager、直接IAPをTerraformで管理します。Viewerは既定の
`*.run.app` URLに加え、自分のドメインでもIAP認証を保ったままアクセスできます
（`viewer_domain`、詳細は `DEPLOYMENT.md` の「カスタムドメインでViewerに
アクセスできるようにする」参照）。
AIエージェント（Meta Muse などのリモートMCPクライアント）向けには、同じViewerイメージを
IAPなしのエージェント専用サービス `news-agent` としても動かし、`/mcp` と `/api/v1` だけを
固定キー（`news-api-key`）で守って公開します（`agent_domain` で独自ドメインも付けられます。
`DEPLOYMENT.md` の「エージェント専用サービス（news-agent）」参照）。
LLMは既定でOpenAI互換エンドポイント（`llm_base_url`/`llm_model`で指定、APIキーは
Secret Manager経由）を利用します。Vertex AI経路もコード側（`summarizer/llm_client.py`）は
実装済みで、`LLM_PROVIDER=vertex`と`llm.project_id`/`llm.location`相当の環境変数で
切り替えられます。その場合のIAM付与とAPI有効化はTerraformに別途追加してください。

## 前提

- Google Cloudの請求先を紐付けたプロジェクト
- `gcloud`とTerraform 1.8以上
- MinifluxがCloud RunからHTTPSで到達可能であること
- Terraformを実行するアカウントにProject IAM Admin、Service Usage Admin、
  Cloud Run Admin、Firestore Admin、Secret Manager Admin、IAP Admin相当の権限

## 1. 基盤とシークレット入れ物を作る

```bash
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform apply \
  -target=google_project_service.required \
  -target=google_artifact_registry_repository.containers \
  -target=google_firestore_database.default \
  -target=google_secret_manager_secret.miniflux_api_key \
  -target=google_secret_manager_secret.email_password \
  -target=google_secret_manager_secret.discord_webhook_url \
  -target=google_secret_manager_secret.llm_api_key \
  -target=google_secret_manager_secret.embedding_api_key \
  -target=google_secret_manager_secret.news_api_key
```

値をコマンドライン引数に含めず、標準入力から各シークレットの初回バージョンを
追加します。Cloud Run Jobと各サービスは6つすべてを`latest`で参照するため、バージョンが
0件のシークレットが1つでもあると手順3の`terraform apply`が失敗します。
`news-api-key`は`news-agent`の固定キーで、32文字以上のランダムな値にします
（`openssl rand -hex 32`の出力など。改行を含めないこと）。

```bash
gcloud secrets versions add news-miniflux-api-key --data-file=-
gcloud secrets versions add news-email-password --data-file=-
gcloud secrets versions add news-discord-webhook-url --data-file=-
gcloud secrets versions add news-llm-api-key --data-file=-
gcloud secrets versions add news-embedding-api-key --data-file=-
printf '%s' "$(openssl rand -hex 32)" | gcloud secrets versions add news-api-key --data-file=-
```

## 2. コンテナをビルドする

各リポジトリのルートで、同じコミットSHAをタグにして実行します。

```bash
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_TAG=COMMIT_SHA
```

生成された2つのイメージURLを`terraform.tfvars`の`viewer_image`と
`summarizer_image`へ設定します。`latest`ではなくコミットタグまたはdigestを指定します。

## 3. 全リソースを反映する

```bash
terraform plan
terraform apply
```

Google Workspace組織に属さない個人プロジェクトや、組織外の家族アカウントを使う場合、
初回だけCloud RunのSecurity画面からIAPのOAuth同意画面をExternalとして構成し、
認証情報を自動生成します。その後の許可ユーザーは`viewer_users`で管理できます。

## 4. データ移行と動作確認

まず認証済みのローカル環境からドライランします。

```bash
uv run python scripts/migrate_sqlite_to_firestore.py \
  --database data/news_summarizer.db \
  --project YOUR_PROJECT_ID \
  --dry-run
```

件数を確認後、`--dry-run`を外して移行します。次にCloud Run Jobを手動実行し、
Firestore、Discord、Miniflux既読化を確認してからSchedulerを運用開始します。

```bash
gcloud run jobs execute news-summarizer \
  --region asia-northeast1 \
  --wait
```

## コストガード

- Viewerと`news-agent`はそれぞれ最小0・最大1インスタンス。`news-agent`の検索は検索語ごとに
  embeddingの従量課金が発生する
- Jobは1タスク、失敗時の自動再試行なし
- Schedulerは既定で1日1回（`schedule`で変更可）
- Artifact Registryは最新3世代を保持し、14日超を削除
- `billing_account_id`設定時は月額2,000円を既定予算として50%、90%、100%で通知
- Firestore PITRは初期状態では無効。SQLite原本は切替後もしばらく保管
