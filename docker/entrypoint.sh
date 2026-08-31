#!/bin/sh
set -eu

PYTHON="/app/.venv/bin/python"
# main.py 自体の argparse 既定値（--source all）と合わせる。Cloud Run Job は
# NEWS_SUMMARIZER_SOURCE を設定していないため、ここを rss 等にすると
# email ソースの処理が本番から静かに消える。
SOURCE="${NEWS_SUMMARIZER_SOURCE:-all}"

# MINIFLUX_BASE_URL の既定値はここで一度だけ確定させ、以降 export して
# provision_miniflux_key.py・main.py の両方に同じ値を見せる。
# provision_miniflux_key.py 側にも独自の既定値があったが、ここで未設定のまま
# だと「キー発行は（自前の既定値で）成功したのに、config.py 側は
# MINIFLUX_BASE_URL 環境変数そのものが無いため miniflux.base_url が必須項目
# 欠落で起動時に落ちる」というズレが起きうる。単一の真実源にする。
export MINIFLUX_BASE_URL="${MINIFLUX_BASE_URL:-http://miniflux:8080}"

# 正の整数でなければ既定値にフォールバックし、警告を出す。
# MINIFLUX_KEY_PROVISION_RETRIES と NEWS_SUMMARIZER_INTERVAL_SECONDS の
# どちらも同じ形の検証が必要なため関数化する。
validate_positive_int() {
  # $1=値 $2=環境変数名（メッセージ用） $3=既定値
  case "$1" in
    ''|*[!0-9]*|0)
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
  wait "$child_pid"
  rc=$?
  child_pid=""
  return "$rc"
}

# 終了要求が来ていたらここで抜ける。set -e は `||`/`if` の条件式の中では
# 効かないため、各ステップの後で明示的にチェックする。
exit_if_terminating() {
  if [ "$terminating" -eq 1 ]; then
    exit 0
  fi
}

if [ -z "${MINIFLUX_API_KEY:-}" ] && [ -n "${MINIFLUX_ADMIN_USERNAME:-}" ] && [ -n "${MINIFLUX_ADMIN_PASSWORD:-}" ]; then
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
    run_bg "$PYTHON" /app/docker/provision_miniflux_key.py > "$key_tmp" || true
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

run_summarizer() {
  if has_source_arg "$@"; then
    run_bg "$PYTHON" main.py "$@"
  else
    run_bg "$PYTHON" main.py --source "$SOURCE" "$@"
  fi
}

if [ "$#" -eq 0 ]; then
  set -- run-once
fi

case "$1" in
  run-loop)
    shift
    interval="$(validate_positive_int "${NEWS_SUMMARIZER_INTERVAL_SECONDS:-3600}" NEWS_SUMMARIZER_INTERVAL_SECONDS 3600)"
    while true; do
      # main.py が失敗しても set -e でループごと落ちないようにする。
      # 一過性の障害（LLM/DBの一時エラー等）でコンテナが停止し、次回間隔まで
      # 何も実行されなくなるのを防ぐ。SIGTERM/SIGINT による意図的な停止は
      # 「異常終了」扱いにせず、そのままループを抜けて終了する。
      if ! run_summarizer "$@"; then
        exit_if_terminating
        echo "[entrypoint] main.py が異常終了しました。次の間隔まで待って再試行します" >&2
      fi
      exit_if_terminating
      run_bg sleep "$interval" || true
      exit_if_terminating
    done
    ;;
  run-once)
    shift
    run_summarizer "$@"
    ;;
  -*)
    run_summarizer "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
