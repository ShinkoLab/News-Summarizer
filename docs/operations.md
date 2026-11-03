# 運用

## 失敗したときの振る舞い

エラーはソース単位・記事単位で切り離していて、一部が失敗しても実行は続く。

| 失敗したもの | 振る舞い |
| --- | --- |
| Miniflux / POP3 への接続 | そのソースを飛ばして、もう片方の記事で続ける |
| メール1通の解析 | そのメールだけを飛ばす。試行回数に数える（[メール](#メール)） |
| 記事1件の要約 | その記事だけを飛ばす。RSS の記事は未読のまま残り、次回また取得される |
| グルーピング | グループなしで続ける |
| ダイジェストの全体の概要 | 概要を空にして続ける |
| ダイジェスト全体 | 空のダイジェストで続ける。個別要約は保存する |
| Firestore への一部のコミット | その分の記事を次回に回す |
| Discord への投稿 | 異常終了。保存済みの記事と未完了の outbox は保持し、次回再試行する |
| 新しい記事が無い | 保留配信と処理済みメールの削除を再試行してから終了する |

### 実行全体を失敗とするとき

記事単位で切り離しているだけだと、LLM の API キーが切れていても実行は「正常終了」になり、
Cloud Run は成功として記録する。そこで次のときは `RunFailedError` を出し、`main.py` は
終了コード 1 で終わる。

- 有効な取得対象ソースがすべて失敗し、部分取得の記事も無い（正常に取得して0件だった場合とは区別）
- 要約しようとした記事が1件も要約できなかった
- 要約はできたが、DB に1件も保存できなかった（DB に書く実行のとき）
- Discord の投稿または配信進捗の保存に失敗した（新着0件の復旧処理も含む）

取得・要約・保存の部分失敗だけなら、1件でも保存できれば終了コードは 0。
SQLite などの `save_batch()` が例外を投げた場合も `RunFailedError` として終了コード 1 にする。
保存後の想定外の出力例外でも異常終了するが、確認済みの保存結果は保持する。

コンテナの `run-loop`（Docker Compose の `summarizer`）は、終了コード 1 でも止まらずに次の間隔で
また実行する。`run-once` は終了コードをそのまま返す。

## メール

POP3 SSL はシステムの信頼済み CA で証明書とホスト名を検証する。接続と各ソケット操作の
タイムアウトは SSL / 非SSL とも固定 30 秒（実行全体の上限ではない）。認証失敗時はソケットを
閉じる。自己署名証明書を使う場合は信頼ストアを適切に設定し、検証を無効化しない。

- 処理したメールは `processed_emails` に UIDL を記録し、次回からは取得しない
- 取得したのに保存できなかったメールは、毎回まるごと取得し直すことになる。そこで試行回数を
  `email_attempts` に数え、`email.max_fetch_attempts`（既定 3）回に達したら諦めて処理済みにする
- 件数の上限で次回に繰り越しただけのメールは、試していないので数えない
- [実行全体の失敗](#実行全体を失敗とするとき)は数えない。原因は API キーや DB の障害など環境にあるので、
  数えると障害のあいだにすべてのメールが上限に達してしまう。ただし解析に失敗したメールは取得の直後に
  数えるので、実行全体が失敗しても数える
- `RETR` の失敗は接続の問題として扱い、残りの取得を止める。失敗前に取得できた記事は処理を続け、
  取得できなかったメールの試行回数には数えない
- 保存例外や処理段階の想定外の中断は、個別メールの失敗とは判定せず試行回数を消費しない。
  保存後の出力が失敗しても確認済みの保存結果は失わず、未保存メールは削除しない。
  正常に完了した処理の部分失敗と、解析できなかったメールの従来の試行上限は変わらない

メールは既定ではサーバーから消さない。`email.delete_after_processing: true`
（環境変数は `EMAIL_DELETE_AFTER_PROCESSING`）にすると、次の2種類だけを削除する。

- 保存できたメール
- 試行回数の上限に達して諦めたメール

今回失敗しただけのメールはサーバーに残し、次回また試す。POP3 のメッセージ番号は接続ごとに
変わるので、削除は別の接続を開いて UIDL から番号を引き直してから行う。途中でエラーが出たら
`QUIT` を送らずに切断するので、削除マークは破棄される。`QUIT` の応答を受信できなかった場合は
削除が確定したか不明なこともあるが、次回は残った UIDL だけを対象にする。
削除失敗でサーバーに残った処理済み UIDL は、次回の UIDL 走査で本文を再取得せずに収集し、
新着0件でも削除を再試行する。未処理のメールをこの後片付けに混ぜない。
`--dry-run` では `--output db` で保存を強制しても削除しない。

## Discord 配信の復旧

DB と Discord を両方出力する実行では、保存済みの投稿 payload を outbox に保持する。
通常の次回実行は取得前に配信を再開するため、新着がなくても復旧し、LLM を再実行しない。
起動時の配信に失敗しても取得・新規保存と POP3 の後片付けは続け、最後に終了コード1で報告する
（新着0件でも後片付け後に報告）。投稿単位で成功を記録し、後続投稿の失敗で確認済みの先頭投稿まで
送り直さない。Discord Webhook は `wait=true` で投稿し、保存確認を待った HTTP 成功後に
進捗を記録する（`thread_id` など既存の URL クエリは保持する）。
Firestore が部分保存になった場合も payload は生成バッチ全体のまま固定するので、未保存記事が
次回以降に再取得されると別バッチのダイジェストにも含まれ得る。

- 1回の drain は最大10配信を対象にし、取得前と新規保存後の最大2回実行する。1配信は複数の
  HTTP 投稿を含み得る。429以外の投稿失敗では、その配信の後続投稿を止め、上限内で他の配信を
  続けてからエラーを報告する。429ではその drain を即座に止める。即時再送や sleep は行わない。
- 再試行の待機は30秒から指数的に増やし、最大6時間。HTTP 429 は応答本文の `retry_after`、
  または `Retry-After` の秒数がそれより長ければ、その待機時間を使う（6時間を超える指定も尊重）。
  **未完了配信のどれかに未来の `next_attempt_at` がある間は、同じ Webhook を使う全配信を停止**する。
  新規バッチの投稿も迂回させない保守的な共有クールダウンであり、429以外の失敗にも適用する。
  クールダウン中の実行は HTTP を送らず保留するため、それ自体を新たな投稿失敗とはしない。
- 未完了の総数は100配信、1配信は512 KiBまで。恒久的な HTTP 400 なども自動で破棄しないため、
  障害を放置してキューが埋まると**新規記事の保存も止まる**。下の手順で原因を調べて復旧する。
- Webhook URL やトークンは outbox に保存しない。再試行先は**現在設定されている Webhook**。
  送り先を切り替えると保留分も新しい送り先へ届く。URL 未設定なら保留したままで送らない。
- `--dry-run` と `--dry-run --output db` は保留分を送らない。Discord のみを強制する
  `--dry-run --output discord` は DB を書かず直接投稿するため、永続的な復旧対象にならない。
  `--output all`（または `--output discord db`）なら両方の出力が有効で outbox を使う。
- 配信方式は **at-least-once（重複の可能性あり）**。Discord が受理した直後のタイムアウトや、
  成功応答後・進捗保存前の停止では同じ投稿を再送することがある。Webhook に冪等キーは無く、
  exactly-once は保証しない。手動再送や outbox の改変・削除も重複／欠落の原因になる。

### 恒久的な失敗の調査・手動対応

1. ログの HTTP エラー、現在の Webhook 設定、該当 outbox の配信 `id`・`batch_id`・`payloads`・
   `next_payload`・`attempts`・`next_attempt_at` を確認する。URL や秘密情報を共有ログに貼らない。
2. 認証・権限・送り先などを直せるなら修正し、クールダウン後の通常実行で再開する。
   payload が恒久的に不正なら、まず実行を止めて該当行／文書をバックアップし、原因を修正した
   コードと保持済み payload の扱いを確認する（コードだけ直しても固定済み payload は変わらない）。
3. 再送できない内容を破棄すると判断した場合に限り、バックアップした配信 ID を照合して対象の
   outbox 行／文書だけを削除する。**未送信の残りは失われる**。記事や処理済み UIDL は削除しない。
   `next_payload` を安易に増減させると欠落や重複を起こすため、進捗を飛ばしてエラーを隠さない。

完了した outbox は削除する（送信履歴の監査台帳ではない）。導入以前の投稿失敗を履歴から
自動復元することはない。SQLite を直接起動する場合も、以下の実行直列化を守る。

## 二重実行の防止

同じ記事を二重に要約・投稿しないよう、DB に書く実行は1つずつに限る。`--dry-run`（DB に書かない）は
ロックを取らない。

- **Firestore**: `pipelineLocks/current` のドキュメントを、期限付き（2 時間）のロックとして使う。
  強制終了した実行のロックが残ったときの戻し方は `infra/DEPLOYMENT.md` にある
- **SQLite**（Docker Compose）: SQLite の側にはロックが無いので、`docker/with_lock.py` が `/data` 上の
  ロックファイルを `flock` で取ってから `main.py` を起動する
  - `run-once` は先の実行が終わるまで待つ。`NEWS_SUMMARIZER_LOCK_WAIT_SECONDS`（既定 3600 秒）を
    過ぎたら諦めて、終了コード 75 で終わる
  - `run-loop` は、ほかの実行中ならその周回を飛ばす
  - ロックを取れないファイルシステム（一部の NFS など）では、警告を出してロックなしで実行する
  - `flock(1)` コマンドを使わないのは、子プロセスにシグナルを伝えず、macOS に無いため

ローカルで `uv run python main.py` を直接実行するときは、SQLite でもロックを取らない。

## ログ

ログは標準エラー出力にだけ出す（ファイルには書かない）。量は `logging.level`
（環境変数は `LOG_LEVEL`）で変える。Docker Compose では `docker compose logs -f summarizer` で見る。

Discord の配信失敗ログは例外クラスと HTTP ステータスだけを出し、Webhook URL を含み得る
例外本文や traceback は出さない。`httpx` / `httpcore` は URL を INFO / DEBUG に記録するため、
`logging.level` や `--debug` にかかわらず WARNING 以上（設定が ERROR 以上ならそのレベル）にする。

## 運用スクリプト

`scripts/` にある、一度だけ使う道具。使い方は `uv run python -m scripts.<名前> --help` で確認できる。

| スクリプト | 使う場面 |
| --- | --- |
| `skip_stale_backlog.py` | 障害で長く止まったあと、溜まった古い未読記事・メールを要約せずに既読化・削除する。既定はドライランで、`--execute` を付けて確認に答えたときだけ変更する |
| `migrate_sqlite_to_firestore.py` | SQLite の履歴を Firestore へ移す |
| `backfill_embedding_vectors.py` | Firestore の `embedding` を配列から `Vector` 型に変換する。Viewer の意味検索で使えるようにするため。何度実行しても同じ結果になる |

Google Cloud 構成の構築と、実際に起きた問題の対処は `infra/README.md` と `infra/DEPLOYMENT.md` にある。

## Read-only dry runs

During `--dry-run` without an explicit DB output (`--output db` or `--output all`), SQLite reads
a private in-memory snapshot opened from the source with `mode=ro`. Schema
initialization and legacy migrations affect only that snapshot; a missing database
and its parent directory are not created. The snapshot rejects writes and requires
memory proportional to the database size. Committed WAL data is included.
Explicit DB output retains normal initialization, migration and persistence.
Firestore dry runs continue to skip the execution lease and all persistence;
fetching and LLM calls still occur unless replaced by test providers.
