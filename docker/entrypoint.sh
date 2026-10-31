#!/bin/sh
set -eu

PYTHON="/app/.venv/bin/python"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# main.py 自体の argparse 既定値（--source all）と合わせる。Cloud Run Job は
# NEWS_SUMMARIZER_SOURCE を設定していないため、ここを rss 等にすると
# email ソースの処理が本番から静かに消える。
SOURCE="${NEWS_SUMMARIZER_SOURCE:-all}"

# MINIFLUX_BASE_URL の既定値はここで一度だけ確定させ、provision_miniflux_key.py と
# main.py の両方に同じ値を見せる（単一の真実源）。ただし main.py 側へは API キーが
# 確定した後にだけ export する。config.py は MINIFLUX_* が1つでもあると miniflux
# セクションを作り、api_key 欠落で起動時に落ちるため、Miniflux を使わない
# （メールのみの）実行に base_url だけを渡してはいけない。
MINIFLUX_BASE_URL_INPUT="${MINIFLUX_BASE_URL:-}"
MINIFLUX_DEFAULT_BASE_URL="${MINIFLUX_BASE_URL:-http://miniflux:8080}"
# Compose は未設定でも MINIFLUX_BASE_URL を（空か値で）渡してくる。元の環境変数は消しておき、
# API キーが確定するまで main.py や SQLite 初期化の子プロセスに見せない。
unset MINIFLUX_BASE_URL

# 正の整数でなければ既定値にフォールバックし、警告を出す。
# MINIFLUX_KEY_PROVISION_RETRIES と NEWS_SUMMARIZER_INTERVAL_SECONDS の
# どちらも同じ形の検証が必要なため関数化する。
validate_positive_int() {
  # $1=値 $2=環境変数名（メッセージ用） $3=既定値
  case "$1" in
    ''|*[!0-9]*|0|00*|0[0-9]*)
      echo "[entrypoint] ${2}=\"$1\" は不正な値です。既定の${3}を使います" >&2
      echo "$3"
      ;;
    *)
      echo "$1"
      ;;
  esac
}

# 主要な処理をすべてバックグラウンドで起動して trap 経由で SIGTERM/SIGINT を
# 転送する。単純に前面で呼ぶだけだと、このスクリプト（PID 1）がシグナルを
# 受けても子プロセスには伝わらず、docker stop / compose down のたびに猶予
# 時間いっぱい待たされた末に SIGKILL される。
child_pid=""
terminating=0
forward_signal() {
  terminating=1
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
}
trap forward_signal TERM INT

# バックグラウンドで1コマンド実行し、完了を待つ。$child_pid を trap から
# 見えるようにすることで、実行中に届いた SIGTERM/SIGINT を即座に転送できる
# （main.py・Miniflux APIキー取得・sleep のすべてで共通して使う）。
run_bg() {
  "$@" &
  child_pid=$!
  # "$@ &" から child_pid=$! までのごく短い間にシグナルが届くと、trap は
  # terminating=1 にはするものの child_pid がまだ空で kill できない
  # （forward_signal 自体は再度呼ばれない）。ここで改めてチェックし、
  # 取りこぼしていたら今すぐ転送する。
  if [ "$terminating" -eq 1 ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
  # `wait` を単独で書くと、子が非ゼロ終了／シグナルで中断された場合に set -e が
  # ここでシェルごと落としてしまう（下の再 wait に到達しない）。`||` で受ける。
  rc=0
  wait "$child_pid" || rc=$?
  # trap が発火すると wait は子の終了を待たずに即座に戻る（status > 128）。
  # そのまま PID 1 が終了すると子が SIGKILL されクリーンアップ（実行ロックの
  # 解放・書き込み途中の DB など）が行われないため、もう一度 wait して子の
  # 本当の終了を待つ。
  if [ "$terminating" -eq 1 ] && [ "$rc" -gt 128 ] && kill -0 "$child_pid" 2>/dev/null; then
    wait "$child_pid" 2>/dev/null || rc=$?
  fi
  child_pid=""
  return "$rc"
}

# 再試行までの待ち時間（秒）。通常の間隔より長くならない。
short_retry_seconds() {
  # $1=通常の間隔 $2=上限（秒）
  if [ "$1" -lt "$2" ]; then echo "$1"; else echo "$2"; fi
}

# 終了要求が来ていたらここで抜ける。set -e は `||`/`if` の条件式の中では
# 効かないため、各ステップの後で明示的にチェックする。
exit_if_terminating() {
  if [ "$terminating" -eq 1 ]; then
    # 中断された run-once を「成功」に見せない（128+SIGTERM）。一時ファイルも消す。
    if [ -n "${key_tmp:-}" ]; then
      rm -f "$key_tmp"
    fi
    exit 143
  fi
}

# MINIFLUX_API_KEY が空で管理者情報があるときだけ API キーを発行して export する。
# 起動時に1回だけでなく run-loop の各周回の頭でも呼ぶ（キーが空のままなら
# 次の周回で再挑戦できる。Miniflux の起動が遅れて初回が全滅しても、コンテナを
# 再起動しない限り永遠に認証エラーになる、という状態を避ける）。
# 実効ソース（--source 引数 > NEWS_SUMMARIZER_SOURCE）。メールのみなら Miniflux は不要。
effective_source() {
  prev=""
  for arg in "$@"; do
    case "$arg" in
      --source=*) echo "${arg#--source=}"; return ;;
    esac
    if [ "$prev" = "--source" ]; then echo "$arg"; return; fi
    prev="$arg"
  done
  echo "$SOURCE"
}

# ソースが rss のみで鍵が得られなかったら 1 を返す（run-once の異常終了に使う）。
provision_api_key() {
  needs_miniflux=1
  if [ "$(effective_source "$@")" = "email" ]; then
    needs_miniflux=0
  fi
  if [ "$needs_miniflux" -eq 1 ] && [ -z "${MINIFLUX_API_KEY:-}" ] && [ -n "${MINIFLUX_ADMIN_USERNAME:-}" ] && [ -n "${MINIFLUX_ADMIN_PASSWORD:-}" ]; then
    # 管理者アカウント情報が最初から無い場合は provision_miniflux_key.py が
    # 即座に諦めて空を返す（ネットワークエラーではないので待っても変わらない）。
    # ここで先にチェックしておかないと、5回×5秒＝約25秒を意味もなく浪費する。
    #
    # Miniflux 側の起動完了（healthcheck）と competing しうるので、初回起動直後の
    # 失敗だけで諦めずに何度か再試行する。ここで諦めると run-loop の耐障害性
    # （main.py 自体の再試行）があっても、認証キーが永遠に空のままになる。
    #
    # 出力を $(...) で直接受け取ると run_bg がサブシェルの中で実行されてしまい、
    # そこで設定した child_pid が親シェル（trap 側）から見えず、シグナル転送が
    # 効かなくなる。一時ファイル経由で受け取ることで run_bg を親シェルのまま呼ぶ。
    key_tmp="$(mktemp)"
    attempt=0
    max_attempts="$(validate_positive_int "${MINIFLUX_KEY_PROVISION_RETRIES:-5}" MINIFLUX_KEY_PROVISION_RETRIES 5)"
    while [ "$attempt" -lt "$max_attempts" ]; do
      run_bg env "MINIFLUX_BASE_URL=$MINIFLUX_DEFAULT_BASE_URL" "$PYTHON" "$SCRIPT_DIR/provision_miniflux_key.py" > "$key_tmp" || true
      exit_if_terminating
      provisioned_key="$(cat "$key_tmp")"
      if [ -n "$provisioned_key" ]; then
        MINIFLUX_API_KEY="$provisioned_key"
        export MINIFLUX_API_KEY
        break
      fi
      attempt=$((attempt + 1))
      if [ "$attempt" -lt "$max_attempts" ]; then
        echo "[entrypoint] Miniflux API キーの取得に失敗しました（${attempt}/${max_attempts}）。5秒後に再試行します" >&2
        run_bg sleep 5 || true
        exit_if_terminating
      fi
    done
    rm -f "$key_tmp"
  fi
  if [ "$needs_miniflux" -eq 1 ] && [ "${MINIFLUX_BUNDLED:-}" != "1" ] && [ -z "$MINIFLUX_BASE_URL_INPUT" ]; then
    # 既存 Miniflux 接続では、同梱用の既定 URL（http://miniflux:8080）は存在しないホスト。
    echo "[entrypoint] MINIFLUX_BASE_URL が未設定です。既存の Miniflux に接続するには .env に設定してください" >&2
    # メールが動ける all は警告だけで続行する（下のキー欠落の分岐と同じ条件）。
    src="$(effective_source "$@")"
    if [ "$src" = "rss" ] || { [ "$src" = "all" ] && [ -z "${EMAIL_HOST:-}" ]; }; then
      return 1
    fi
    # base_url なしの api_key だけを main.py に見せると config.py が落ちる。メール側だけ動かす。
    unset MINIFLUX_API_KEY
    return 0
  fi
  if [ -n "${MINIFLUX_API_KEY:-}" ]; then
    export MINIFLUX_BASE_URL="$MINIFLUX_DEFAULT_BASE_URL"
  elif [ "$needs_miniflux" -eq 1 ]; then
    # キーが無いと miniflux 設定そのものが作られず、RSS 取得は「設定が見つかりません」と
    # ログに出るだけで終了コード 0 になる。見落とされないよう、ここで明示する。
    echo "[entrypoint] Miniflux API キーがありません。RSS は取得されません（MINIFLUX_API_KEY を設定してください。同梱 Miniflux なら管理者情報から自動発行されます）" >&2
    # RSS だけを指定した実行は何も処理できないので異常終了させる。all も、メールが
    # 設定されていない（EMAIL_HOST が空）なら同じ。メールが動けるときだけ警告で続行する。
    src="$(effective_source "$@")"
    if [ "$src" = "rss" ] || { [ "$src" = "all" ] && [ -z "${EMAIL_HOST:-}" ]; }; then
      return 1
    fi
  fi
}

# 変数が「設定されているが空」のときは LLM_MODEL の入れ忘れ（Compose は未設定でも
# 空文字を渡す）。空のモデル名で LLM を叩き続けて延々と失敗するより、起動時に落とす。
# `down` / `ps` など他の compose コマンドを巻き込まないよう、Compose 側ではなくここで検査する。
require_llm_model() {
  if [ "${LLM_MODEL-unset}" = "" ]; then
    echo "[entrypoint] LLM_MODEL が空です。.env に OpenAI 互換エンドポイントのモデル名を設定してください" >&2
    exit 1
  fi
}

has_source_arg() {
  for arg in "$@"; do
    case "$arg" in
      --source|--source=*)
        return 0
        ;;
    esac
  done
  return 1
}

# SQLite には実行ロックが無い（Firestore の execution_lock() 相当が無い）ので、summarizer
# （run-loop）と summarizer-once が同時に走ると二重に要約・投稿してしまう。main.py は
# /data 上のロックファイル（docker/with_lock.py）経由で直列化する。
# $1=wait|skip（取れないとき待つ / すぐ諦める）。取れなければ終了コード 75。
LOCK_FILE="${NEWS_SUMMARIZER_LOCK_FILE:-/data/.news-summarizer.lock}"
# テストで PYTHON をスタブに差し替えても、ロックのラッパーだけは本物の Python で動かす。
LOCK_RUNNER="${NEWS_SUMMARIZER_LOCK_PYTHON:-$PYTHON}"
run_summarizer() {
  lock_mode="$1"
  shift
  lock_wait="$(validate_positive_int "${NEWS_SUMMARIZER_LOCK_WAIT_SECONDS:-3600}" NEWS_SUMMARIZER_LOCK_WAIT_SECONDS 3600)"
  if has_source_arg "$@"; then
    set -- main.py "$@"
  else
    set -- main.py --source "$SOURCE" "$@"
  fi
  run_bg "$LOCK_RUNNER" "$SCRIPT_DIR/with_lock.py" "$LOCK_FILE" "$lock_mode" "$lock_wait" -- "$PYTHON" "$@"
}

# SQLite のスキーマを起動時に作っておく。Viewer は DB ファイルが無いとエラーになるので、
# 最初のパイプライン実行（ローカル LLM だと数分）が終わる前でも空の DB を見せられるようにする。
init_database() {
  if [ "${DATABASE_BACKEND:-sqlite}" = "sqlite" ] && [ -n "${SQLITE_DATABASE_PATH:-}" ]; then
    # MINIFLUX_* は base_url と api_key が揃わないと config.py が落ちる。DB の初期化には
    # 不要なので、ここでは Miniflux の設定を見せない。
    run_bg env -u MINIFLUX_API_KEY -u MINIFLUX_BASE_URL "$PYTHON" -c "from outputs.database import create_database; create_database()" || \
      echo "[entrypoint] SQLite スキーマの初期化に失敗しました（main.py の実行時に再試行されます）" >&2
    exit_if_terminating
  fi
}

# 同梱 Miniflux にだけスターターフィードを初回取り込みする。
# MINIFLUX_IMPORT_STARTER_FEEDS: 空なら同梱（MINIFLUX_BUNDLED=1）で true、既存 Miniflux では false。
# 既存 Miniflux に取り込みたいときだけ明示的に true にする。フィードが 0 件のときだけ
# 取り込み、一度済んだらマーカーで再取り込みしない。失敗しても警告のみ。
import_starter_feeds() {
  [ -n "${MINIFLUX_API_KEY:-}" ] || return 0
  [ "$(effective_source "$@")" = "email" ] && return 0
  # --dry-run / --help は Miniflux を書き換えない約束なので、取り込みもしない。
  for arg in "$@"; do
    case "$arg" in
      --dry-run|-h|--help) return 0 ;;
    esac
  done
  flag="${MINIFLUX_IMPORT_STARTER_FEEDS:-}"
  if [ -z "$flag" ]; then
    if [ "${MINIFLUX_BUNDLED:-}" = "1" ]; then flag=true; else flag=false; fi
  fi
  case "$flag" in
    true|TRUE|True|1|yes|on) ;;
    *) return 0 ;;
  esac
  marker="${MINIFLUX_STARTER_FEEDS_MARKER:-/data/.starter-feeds-imported}"
  [ -e "$marker" ] && return 0
  import_rc=0
  run_bg env "MINIFLUX_BASE_URL=$MINIFLUX_DEFAULT_BASE_URL" "$PYTHON" "$SCRIPT_DIR/import_starter_feeds.py" "$SCRIPT_DIR/feeds.opml" "$marker" || import_rc=$?
  exit_if_terminating
  # 終了コード 10 = 取り込んだが最初の記事がまだ届いていない。run-loop は短い間隔で取り直す。
  if [ "$import_rc" -eq 10 ]; then
    first_fetch_pending=1
  fi
}

if [ "$#" -eq 0 ]; then
  set -- run-once
fi

case "$1" in
  run-loop)
    shift
    require_llm_model
    interval="$(validate_positive_int "${NEWS_SUMMARIZER_INTERVAL_SECONDS:-3600}" NEWS_SUMMARIZER_INTERVAL_SECONDS 3600)"
    init_database
    while true; do
      wait_seconds="$interval"
      first_fetch_pending=0
      # 鍵が得られない周回は main.py を走らせても何も処理できない（成功に見えて空振りする）ので
      # 飛ばし、間隔を待たずに短い間隔で鍵の取得を再挑戦する。
      if ! provision_api_key "$@"; then
        wait_seconds="$(short_retry_seconds "$interval" 300)"  # 認証エラー等が続いても叩きすぎない
      else
        import_starter_feeds "$@"
        # main.py が失敗しても set -e でループごと落ちないようにする。
        # 一過性の障害（LLM/DBの一時エラー等）でコンテナが停止し、次回間隔まで
        # 何も実行されなくなるのを防ぐ。SIGTERM/SIGINT による意図的な停止は
        # 「異常終了」扱いにせず、そのままループを抜けて終了する。
        # ロックが取れない（summarizer-once が実行中）ときは待たずにこの周回を飛ばし、
        # 次の間隔（数時間）ではなく短い間隔で取り直す。
        rc=0
        run_summarizer skip "$@" || rc=$?
        exit_if_terminating
        if [ "$rc" -eq 75 ]; then
          echo "[entrypoint] 別の実行が進行中のため今回の周回をスキップしました。すぐに再試行します" >&2
          wait_seconds="$(short_retry_seconds "$interval" 60)"
        elif [ "$rc" -ne 0 ]; then
          echo "[entrypoint] main.py が異常終了しました。次の間隔まで待って再試行します" >&2
        fi
        if [ "$first_fetch_pending" -eq 1 ] && [ "$rc" -eq 0 ]; then
          echo "[entrypoint] スターターフィードの最初の記事がまだ届いていないため、短い間隔で再試行します" >&2
          wait_seconds="$(short_retry_seconds "$interval" 120)"
        fi
      fi
      run_bg sleep "$wait_seconds" || true
      exit_if_terminating
    done
    ;;
  run-once)
    shift
    require_llm_model
    init_database
    # 鍵が無いまま走らせると RSS 0 件で「成功」に見える。run-once は異常終了にする。
    provision_api_key "$@" || exit 1
    import_starter_feeds "$@"
    run_summarizer wait "$@"
    ;;
  -*)
    require_llm_model
    init_database
    provision_api_key "$@" || exit 1
    import_starter_feeds "$@"
    run_summarizer wait "$@"
    ;;
  *)
    # 任意コマンドの素通し。冒頭で隠した MINIFLUX_BASE_URL を元に戻す。
    if [ -n "$MINIFLUX_BASE_URL_INPUT" ]; then export MINIFLUX_BASE_URL="$MINIFLUX_BASE_URL_INPUT"; fi
    exec "$@"
    ;;
esac
