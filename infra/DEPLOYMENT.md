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
- Embedding: OpenCode Zen が embedding 未提供のため、OpenRouter Embeddings API
  （`https://openrouter.ai/api/v1`、`openai/text-embedding-3-small`）を利用。
  APIキーは https://openrouter.ai/settings/keys で発行し、`news-embedding-api-key`
  シークレットに登録する。
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

`project_id`、`viewer_users`、`llm_base_url`、`llm_model`、`miniflux_base_url`
などを実際の値に。カテゴリ一覧はTerraformの管理対象ではなく、リポジトリ直下の
`categories.yaml`（コンテナイメージに同梱）が唯一の正になっている。
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
  -target=google_secret_manager_secret.embedding_api_key \
  -out=plan_stage1.tfplan
terraform apply plan_stage1.tfplan
```

### 5. Secret Managerに値を投入

```bash
printf '%s' "実際の値" | gcloud secrets versions add news-miniflux-api-key --project=<PROJECT_ID> --data-file=-
printf '%s' "実際の値" | gcloud secrets versions add news-llm-api-key --project=<PROJECT_ID> --data-file=-
printf '%s' "実際の値" | gcloud secrets versions add news-embedding-api-key --project=<PROJECT_ID> --data-file=-
printf '%s' "実際の値" | gcloud secrets versions add news-discord-webhook-url --project=<PROJECT_ID> --data-file=-
printf '%s' "unused-placeholder" | gcloud secrets versions add news-email-password --project=<PROJECT_ID> --data-file=-
```

**注意**: `python3 -c "print(key)" | gcloud secrets versions add ...` のように
`print()` を使うと末尾に改行が付与され、HTTPヘッダーに使われた際に
`httpx.LocalProtocolError: Illegal header value b'...\n'` で落ちる。
`print(key, end="")` にするか `printf '%s'` を使うこと。

### 6. コンテナイメージのビルド＆プッシュ

各リポジトリのルートで実行する。**両リポジトリともリリースタグ**（`v2.2.0` / `v1.1.0` 等）を
イメージタグに使う。Artifact Registry を見ればどのリリースが動いているか分かり、
CHANGELOG・リリースとの対応も1対1になる。リリースを切らない検証ビルドはコミット短縮SHAでよい。

```bash
# News-Summarizer（リリース時）
gcloud builds submit --project=<PROJECT_ID> --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_TAG=v2.2.0

# News-Viewer（リリース時）
cd ../News-Viewer
gcloud builds submit --project=<PROJECT_ID> --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_TAG=v1.1.0
```

> News-Viewer は v1.0.2 まではコミット短縮SHAをイメージタグにしていた（`viewer:2c56edc`）。
> v1.1.0 のリリースからリリースタグに揃えている。

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

その後、カテゴリ定義を `categories.yaml`（イメージ同梱）に一本化した際に
`SUMMARIZER_CATEGORIES` は廃止した。同種の再発を防ぐため、カテゴリが空の場合は
設定読み込み時点で `ValidationError` として起動を止めるようにしてある
（`config.py` の `CategoryTaxonomy`）。

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

### 7. `llm.parameters` / `summarizer.steps.*.parameters`（temperature・reasoning_effort）も環境変数から渡せなかった

上記1・2と同じ種類の不具合。ローカル `config.yaml` の `llm.parameters`
（temperature, max_tokens）や `summarizer.steps.<step>.parameters`
（temperature, reasoning_effort）を編集しても、Cloud Run側には対応する環境変数
マッピングが存在せず、デプロイ済みジョブは常に `parameters={}`（全ステップ
パラメータ無指定）のまま動作していた。`LLM_TEMPERATURE` / `LLM_MAX_TOKENS` /
`GROUPER_TEMPERATURE` / `GROUPER_REASONING_EFFORT` /
`SUMMARIZER_TEMPERATURE` / `SUMMARIZER_REASONING_EFFORT` /
`DIGEST_REASONING_EFFORT` を追加して解消（詳細は次節）。
なお `llm.extra_body`（Ollama専用の `repeat_penalty` 等）は元々ローカル
Ollamaモデル向けの設定でありCloud Runの環境変数マッピング対象外のため、
これは今後も未対応のままでよい。

### 8. `individual_max_length` / `similarity_threshold` も環境変数から渡せなかった

上記1・2・7と同じ種類の不具合で、v2.0.0 のデプロイ前調査で発覚した。
ローカル `config.yaml` で調整した以下2つに環境変数マッピングが無く、
Cloud Run はコード既定値のまま動作していた。

| 設定 | ローカルの調整値 | Cloud Run 実効値 |
|---|---|---|
| `summarizer.individual_max_length` | 500 | 200（コード既定） |
| `summarizer.steps.grouper.similarity_threshold` | 0.7 | 0.85（コード既定） |

`individual_max_length` は個別記事の要約文字数で、それがそのまま
カテゴリ別ダイジェスト生成の入力になる。
`similarity_threshold` はコサイン類似度の閾値で、0.85 では 0.7 よりさらに
クラスタリングが効かなくなる。

修正前後の実測（同一モデル `gpt-5.6-luna`、Firestore の `summary_text` 長）:

| | 件数 | 平均 | 中央値 | 最大 | 200字超 |
|---|---|---|---|---|---|
| 修正前（上限200） | 56 | 75 | 76 | 132 | 0件 |
| 修正後（上限500） | 80 | 100 | 91 | 259 | 5件 |

上限は上限であって目標値ではなく、モデルは指示値よりかなり短く書く。
そのため効果は「2.5倍」ではなく中央値で 76→91字、最大 132→259字に留まる。
とはいえ 200 が実際に天井として効いていた記事はあり（修正後は5件が200字を超えた）、
設定が届いていること自体は前提として揃えておく必要がある。
`SUMMARIZER_INDIVIDUAL_MAX_LENGTH` / `GROUPER_SIMILARITY_THRESHOLD` を追加して解消。

同じ取りこぼしが4回起きているのは、`config.py` にフィールドを足しても
`_apply_environment_overrides()` を更新しなければローカルでは何も壊れず、
Cloud Run 上でだけ静かに既定値へ落ちるため。**設定フィールドを追加・調整したら、
環境変数マッピングと `infra/main.tf` の `env` ブロックまでセットで対応すること。**

### 9. 記事数が増えると Firestore の一括保存が `Transaction too big` で失敗する

v2.0.1 のデプロイ検証で、80記事の実行が保存段階で落ちた。

```
google.api_core.exceptions.InvalidArgument: 400 Transaction too big. Decrease transaction size.
```

`outputs/firestore_database.py` の `save_batch()` は書き込み**件数**だけを
`_MAX_BATCH_WRITES = 450` で見張っており、81 writes なので上限は通過していた。
落ちていたのはサイズの方だが、原因はドキュメント本体ではなく**インデックス**だった。

- ドキュメント本体は1件 17.6 KiB、80件でも 1.37 MiB（トランザクション上限 10 MiB の 1/7）
- `embedding` は 1536要素の配列で、Firestore は**配列の各要素を個別にインデックスする**。
  1ドキュメントあたり約1536エントリ、80件で約12万エントリになり、
  この書き込みがトランザクションサイズに算入されて上限を超えていた
- 56件は成功して80件で失敗したという境界とも整合する

`infra/main.tf` の `google_firestore_field.article_summary_embedding` で
`embedding` を単一フィールドインデックスの対象から外して解消
（`index_config {}` が「インデックスを作らない」の意味）。
類似記事の統合はアプリ側がベクトルを読み出して numpy でコサイン類似度を計算しており、
Firestore のインデックスは元から使っていないため、機能的な影響はない。

**この変更でベクトルデータは失われない。** 消えるのはインデックスエントリだけで、
ドキュメントの `embedding` フィールドはそのまま残る（適用後に実測して
137件中56件・1536次元が変更前と同数で保持されていることを確認済み）。
`deletion_policy = "DELETE"` はリソース削除時に既定のインデックス設定へ戻す意味であり、
データ削除ではない。

apply には再インデックスが走るため6分程度かかる。
コード変更は不要で `terraform apply` だけで反映される。

> 残っていた「全件を1トランザクションでコミットするため、失敗すると要約の
> LLM呼び出しが丸ごと無駄になる」という弱点（今回は80件ぶんを2回消費した）は
> issue #26 で解消済み。`save_batch()` は書き込み件数（`_MAX_BATCH_WRITES` = 450）と
> 推定サイズ（`_MAX_CHUNK_BYTES` = 1 MiB）の両方で複数コミットに分割し、
> 途中のチャンクが失敗しても例外を投げずに残りを続行する。
> サイズ上限を10 MiBぎりぎりに置かないのは、`estimate_document_size()` が概算で
> 実測より3割ほど小さく出ることに加え、**1コミットあたりの件数が増えるほど
> 失敗時に失う記事が増える**ため。1件 17.6 KiB として1コミット約60〜76件、
> 今回落ちた80件の実行なら2コミット、`max_articles_per_run` の上限200件でも
> 3コミットに分かれる。
> バッチドキュメントは**最後に**コミットするため、記事チャンクの失敗が
> 「記事はあるのにバッチが無い」（Viewer から永久に見えない）状態を作ることはない。
> ただし最後のバッチドキュメント書き込み自体が失敗した場合はこの状態になりうる。
> そのときは `batch_id` を含む `logger.error` が出るので、
> `batches/<batch_id>` を手で作れば該当記事が Viewer に出る。
> 部分保存になった場合、バッチドキュメントの `status` は `"partial"`、
> `total_articles` は実際に保存できた件数になる。
> 未保存の記事は既読化されないため、次回の実行で再取得される。

### 10. 取得したが保存しなかった記事が毎回の取得に載り続けた

issue #27。重複防止（`is_article_processed()`）は効いていたが、
**「取得したが今回保存しなかった記事」を取得元から外す経路が無かった**ため、
そういう記事は毎回 fetch に載り続けていた。重複データは生まれないので実害は
小さいが、取得コストが単調増加する。

- **RSS**: 既読化の対象が「今回 `save_batch()` に渡ったもの」に限られており、
  「DB保存済みだが Miniflux 上は未読」の記事が永久に滞留していた。
  重複除外の直後（早期 return より前）に既読化するよう変更。定常状態は
  「全件が処理済みで除外され、早期 return する」経路なので、
  ここで既読化しないと滞留は解消されない
- **Email**: POP3 のメールはサーバから削除しないため、保存に至らないメールは
  毎回フル RETR される。`email_attempts` / `emailAttempts` に試行回数を記録し、
  `email.max_fetch_attempts`（既定3、`EMAIL_MAX_FETCH_ATTEMPTS`）に達したら
  処理済み扱いにして打ち切る
- 併せて `max_articles_per_run` の打ち切りをソース間ラウンドロビンに変更した。
  従来は RSS → Email の順に並べた先頭から切っていたため、
  RSS だけで枠が埋まると Email が永久に処理されなかった

### 11. `gpt-5.6-luna` が `/v1/chat/completions` から `/v1/responses` 専用に切り替わり全リクエストが500になった

2026-08-27、Viewerが2026-08-23から更新されていないことに気づいて調査した。
`gcloud logging read` で直近の実行を追うと、8/23 22:00 UTC の実行以降
すべての記事要約が `openai.InternalServerError: 500 - Internal server error`
で失敗し、「要約に成功した記事がありませんでした」のまま何も保存せず
`exit(0)` していた（**Cloud Run Job・Cloud Schedulerの実行一覧は0件保存でも
成功扱いになるため、一見しただけでは異常に気づけない**）。

実際のAPIキーで直接叩いて切り分けたところ、同じキー・同じ
`https://opencode.ai/zen/go/v1` でも `glm-5.3` や `kimi-k3` は200が返り、
`gpt-5.6-luna` だけが500になることを確認。OpenCode Zenの公式ドキュメント
（`https://opencode.ai/docs/en/go/#endpoints`）を見ると、`gpt-5.6-luna` と
`grok-4.6` は `/v1/chat/completions`（OpenAI-compatible）ではなく
`/v1/responses`（OpenAI Responses API 形式）専用に変わっていた。うちの
コードは `client.chat.completions.create()` しか呼んでいなかったため、
形式の合わないリクエストを送り続けて500になっていたと推測される
（以前は動いていたので、OpenCode Zen側がある時点で互換シムを外した
ものと見られる）。

中華系LLM（Zhipu/Moonshot/DeepSeek/Xiaomi/Meituan/Tencent系がOpenCode Zenの
`/v1/chat/completions` 対応モデルのほぼ全て）は政治的に機微な話題の要約で
表現がぼやける事例があったため避けたい方針があり、`gpt-5.6-luna` を
使い続けるために `summarizer/llm_client.py` に Responses API
（`client.responses.parse` / `client.responses.create`）専用の呼び出し
パスを追加した（`llm.provider: "openai_responses"`）。あわせて分かったこと:

- Responses API の `text.format`（`json_schema`）は `gpt-5.6-luna` で
  正しく機能する。Chat Completions側で `LLM_STRUCTURED_OUTPUT=false`
  にしていたのは別の問題（OpenCode Zen (go) がMarkdown混じりのテキストを
  返す）で、Responses APIには影響しない
- `gpt-5.6-luna` は推論モデルのため `temperature` パラメータを送ると
  `400 Unsupported parameter: 'temperature' is not supported with this
  model` になる。既存の `llm.thinking` /
  `llm.disable_temperature_with_thinking`（`build_step_params()` が
  thinking かつ disable_temperature_with_thinking のとき temperature を
  除去する仕組み）がまさにこの用途で存在していたが、Cloud Run向けの
  環境変数マッピング（`LLM_THINKING` / `DISABLE_TEMPERATURE_WITH_THINKING`）
  が抜けていたため常にデフォルト値（thinking無効）で動いていた。
  これも他の設定フィールドと同じ「フィールドはあるのにマッピングが無い」
  パターン（#1・#2・#7・#8 参照）なので追加した
- `infra/variables.tf` に `llm_provider` / `llm_structured_output` /
  `llm_thinking` / `llm_disable_temperature_with_thinking` を追加し、
  従来 `infra/main.tf` にハードコードしていた `LLM_PROVIDER="openai"` /
  `LLM_STRUCTURED_OUTPUT="false"` をterraform変数化した

## 設定変更手順

### LLM設定を変更する（プロバイダ・モデル・エンドポイント）

1. ローカル `config.yaml` の `llm.*` を更新し、疎通確認する
   （`openai` SDK の `chat.completions.create` で実際にテストすること。
   `curl`/`urllib` 単体での成功はCloudflare越しの疎通確認にはなるが、
   Structured Output対応可否までは分からない）。
2. `infra/terraform.tfvars` の `llm_base_url` / `llm_model` を更新。
3. **エンドポイントが `/v1/chat/completions` と `/v1/responses` のどちらを
   要求するか確認する**（プロバイダのドキュメントを見る。前者が既定の
   `llm_provider = "openai"` で、後者は `llm_provider = "openai_responses"`
   に切り替える必要がある。誤った形式のまま気づかないと#11のように
   全リクエストが失敗し続ける）。
4. 新しいエンドポイントがStructured Output（`response_format` /
   `text_format` によるJSON Schema指定）に対応しているか確認する。
   対応していなければ `llm_structured_output = false`（既定値）のままに
   する（対応していれば `true` に変更して呼び出し回数を削減できる）。
5. 推論モデル（GPT-5系・o1系等）で `temperature` パラメータがエラーになる
   場合は `llm_thinking = true` と `llm_disable_temperature_with_thinking
   = true` を設定する。
6. APIキーが変わる場合は `gcloud secrets versions add news-llm-api-key
   --project=<PROJECT_ID> --data-file=-` で新バージョンを追加
   （`print(key, end="")` または `printf '%s'` で改行を含めないこと）。
7. `terraform apply` を実行（Job定義の環境変数が更新される）。

### LLMパラメータ（temperature・reasoning_effort）を変更する

`terraform.tfvars` に以下を追記して `terraform apply`（省略時のデフォルトは
`infra/variables.tf` 参照。例はローカル `config.yaml` の設定値に揃えてある）:

```hcl
llm_temperature             = 0.7   # グローバルデフォルト
llm_max_tokens              = 8192
grouper_temperature         = 0.2
grouper_reasoning_effort    = "low"
summarizer_temperature      = 0.3
summarizer_reasoning_effort = "low"
digest_reasoning_effort     = "medium"
```

`reasoning_effort` は reasoning model（GPT-5系等）のみ有効なパラメータ。
非対応モデルに切り替えた場合は空文字や未設定にする（`infra/main.tf` の
該当 `env` ブロックを削除するか、値を空文字にして送らないようにする）。

### 要約の長さ・グルーピングの閾値を変更する

同じく `terraform.tfvars` に追記して `terraform apply`:

```hcl
summarizer_individual_max_length = 500   # 個別記事の要約文字数（ダイジェストの素材）
grouper_similarity_threshold     = 0.7   # embedding グルーピングのコサイン類似度閾値
```

`infra/variables.tf` のデフォルトはローカル `config.yaml` の値に揃えてあるため、
同じ値で運用するなら `terraform.tfvars` への記載は不要。

### カテゴリ一覧を変更する

リポジトリ直下の `categories.yaml` を編集し、**イメージを再ビルドしてデプロイ**する
（`terraform apply` は不要）。このファイルはローカル実行・Cloud Run実行の双方が
読む唯一の定義元で、カテゴリ名だけでなく各カテゴリの定義文・判定の原則・
タイブレーク規則・フォールバック値も含む。内容がそのまま要約プロンプトに
注入されるため、変更はイメージのビルドを伴う（＝イメージと分類挙動が1対1で対応する）。

分類精度を調整したいだけなら、カテゴリ名を変えずに `description` や
`tiebreak_rules` の文言だけを直せばよい。コード変更は不要。

> **初回移行時の順序に注意**: `SUMMARIZER_CATEGORIES` 環境変数を削除する
> `terraform apply` は、`categories.yaml` を含むイメージがデプロイされた後に行うこと。
> 先に apply すると、旧イメージがカテゴリ一覧を空のまま起動し、全記事が
> カテゴリ検証に失敗して `category_max_retries` 回ぶんの LLM 呼び出しを空振りさせた上、
> すべて 未分類 になる。「イメージをpush → `summarizer_image` を更新して apply」の順で行う。

### 1回に処理する記事数を変更する

`terraform.tfvars` の**2つをセットで**変更して `terraform apply`。

```hcl
max_articles_per_run = 200   # 要約する上限（既定100、config.py の Field 制約で最大200）
miniflux_fetch_limit = 250   # Miniflux に要求する取得件数（既定100、最大1000）
```

**片方だけ上げても効かない。** Miniflux の `/v1/entries` は `limit` を指定しないと
サーバ既定の100件で打ち切る（`MaxEntryLimit` は1000）。この状態では
`max_articles_per_run` をいくつにしても入力が100件を超えないため、
繰り越しの警告すら一度も出ない——実際、2026-08 に未読が400件滞留していた原因が
これだった。逆に `miniflux_fetch_limit` だけ上げると、今度は
`max_articles_per_run` で切られて毎回繰り越し警告が出る。

`fetch_limit` を `max_articles_per_run` より多めに取るのは、取得後の重複除去
（既処理・同一URL）で毎回1割前後が落ちるため。溢れた分は既読化されないので
次回そのまま再取得され、捨てられることはない。

上限を上げるときに確認すること:

- **実行時間** — 90件で7〜9分が実績値。Cloud Run Job の `timeout` は 3600s
  （`main.tf`）。200件でも15〜20分程度で収まるが、要約は1記事ずつ逐次に
  LLM を呼ぶので件数にほぼ比例する
- **LLM コスト** — 記事数にほぼ比例して増える
- **ダイジェストの密度** — `digest_max_length` は3000字固定（環境変数マッピングが
  無くクラウドでは変更不可）。記事数を倍にしても総量は変わらないため、
  1記事あたりの露出は薄くなる。Discord の embed description 上限が4096字なので
  引き上げ余地も1000字程度しかない
- **未読の残数** — `fetchers/rss_fetcher.py` が Miniflux レスポンスの `total`
  （＝未読の全件数）を INFO ログに出す。「Minifluxの未読は412件、うち250件を
  取得しました（limit=250）」の形。この値が実行ごとに減っていれば、
  処理能力が流入を上回っている

処理能力がまだ足りない場合、`max_articles_per_run` は200が上限（`config.py` の
`Field(le=200)`）なので、次の手は後述の `schedule` に発火時刻を足して
実行回数を増やす方になる。

### Minifluxのエンドポイントを変更する

`terraform.tfvars` の `miniflux_base_url` を更新し `terraform apply`。
末尾に `/v1/` を含めないこと（コードが `{base_url}/v1/entries` を組み立てる）。
APIキーは `news-miniflux-api-key` シークレットに新バージョンを追加する。

### コード変更をデプロイに反映する

```bash
# 変更したリポジトリのルートで
# タグが打たれていればリリースタグ、検証ビルドはコミット短縮SHAになる
gcloud builds submit --project=<PROJECT_ID> --config cloudbuild.yaml \
  --substitutions=_REGION=asia-northeast1,_TAG=$(git describe --exact-match --tags 2>/dev/null || git rev-parse --short HEAD)
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

Cloud Scheduler の既定は `schedule = "0 7 * * *"`（Asia/Tokyo）で1日1回。
**本番環境は `terraform.tfvars` で `schedule = "0 7,19 * * *"` に上書きしており、
毎日7:00と19:00の2回自動実行される。** 発火回数を変えたいときは Scheduler ジョブを
増やすのではなく、この cron に時刻を足すのが基本（ジョブ名 `news-summarizer-daily`
は改名すると destroy/create になるため据え置いている）。
デプロイ直後にスケジュール時刻をまたぐと、修正前のイメージで自動実行されてしまう
ことがあるため、設定変更後は次回のスケジュール実行前に手動実行で動作確認しておくと安全。
