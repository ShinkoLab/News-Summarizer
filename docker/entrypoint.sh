#!/bin/sh
set -eu

PYTHON="/app/.venv/bin/python"
# main.py 自体の argparse 既定値（--source all）と合わせる。Cloud Run Job は
# NEWS_SUMMARIZER_SOURCE を設定していないため、ここを rss 等にすると
# email ソースの処理が本番から静かに消える。
SOURCE="${NEWS_SUMMARIZER_SOURCE:-all}"

# main.py をバックグラウンドで起動して trap 経由で SIGTERM/SIGINT を転送する。
# 単純に "$PYTHON" main.py ... を前面で呼ぶだけだと、このスクリプト（PID 1）が
# シグナルを受けても子プロセスには伝わらず、docker stop / compose down のたびに
# 猶予時間いっぱい待たされた末に SIGKILL される。
child_pid=""
forward_signal() {
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
}
trap forward_signal TERM INT

if [ -z "${MINIFLUX_API_KEY:-}" ]; then
  # Miniflux 側の起動完了（healthcheck）と competing しうるので、初回起動直後の
  # 失敗だけで諦めずに何度か再試行する。ここで諦めると run-loop の耐障害性
  # （main.py 自体の再試行）があっても、認証キーが永遠に空のままになる。
  attempt=0
  max_attempts="${MINIFLUX_KEY_PROVISION_RETRIES:-5}"
  while [ "$attempt" -lt "$max_attempts" ]; do
    provisioned_key="$("$PYTHON" /app/docker/provision_miniflux_key.py || true)"
    if [ -n "$provisioned_key" ]; then
      MINIFLUX_API_KEY="$provisioned_key"
      export MINIFLUX_API_KEY
      break
    fi
    attempt=$((attempt + 1))
    if [ "$attempt" -lt "$max_attempts" ]; then
      echo "[entrypoint] Miniflux API キーの取得に失敗しました（${attempt}/${max_attempts}）。5秒後に再試行します" >&2
      sleep 5
    fi
  done
fi

has_source_arg() {
  for arg in "$@"; do
    if [ "$arg" = "--source" ]; then
      return 0
    fi
  done
  return 1
}

run_summarizer() {
  if has_source_arg "$@"; then
    "$PYTHON" main.py "$@" &
  else
    "$PYTHON" main.py --source "$SOURCE" "$@" &
  fi
  child_pid=$!
  wait "$child_pid"
  rc=$?
  child_pid=""
  return "$rc"
}

if [ "$#" -eq 0 ]; then
  set -- run-once
fi

case "$1" in
  run-loop)
    shift
    interval="${NEWS_SUMMARIZER_INTERVAL_SECONDS:-3600}"
    case "$interval" in
      ''|*[!0-9]*)
        echo "[entrypoint] NEWS_SUMMARIZER_INTERVAL_SECONDS=\"$interval\" は不正な値です。既定の3600秒を使います" >&2
        interval=3600
        ;;
    esac
    while true; do
      # main.py が失敗しても set -e でループごと落ちないようにする。
      # 一過性の障害（LLM/DBの一時エラー等）でコンテナが停止し、次回間隔まで
      # 何も実行されなくなるのを防ぐ。
      run_summarizer "$@" || echo "[entrypoint] main.py が異常終了しました。次の間隔まで待って再試行します" >&2
      sleep "$interval" &
      child_pid=$!
      wait "$child_pid"
      child_pid=""
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
