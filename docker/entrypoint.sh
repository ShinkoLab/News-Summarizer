#!/bin/sh
set -eu

PYTHON="/app/.venv/bin/python"
# main.py 自体の argparse 既定値（--source all）と合わせる。Cloud Run Job は
# NEWS_SUMMARIZER_SOURCE を設定していないため、ここを rss 等にすると
# email ソースの処理が本番から静かに消える。
SOURCE="${NEWS_SUMMARIZER_SOURCE:-all}"

if [ -z "${MINIFLUX_API_KEY:-}" ]; then
  provisioned_key="$("$PYTHON" /app/docker/provision_miniflux_key.py || true)"
  if [ -n "$provisioned_key" ]; then
    MINIFLUX_API_KEY="$provisioned_key"
    export MINIFLUX_API_KEY
  fi
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
    "$PYTHON" main.py "$@"
  else
    "$PYTHON" main.py --source "$SOURCE" "$@"
  fi
}

if [ "$#" -eq 0 ]; then
  set -- run-once
fi

case "$1" in
  run-loop)
    shift
    interval="${NEWS_SUMMARIZER_INTERVAL_SECONDS:-3600}"
    while true; do
      # main.py が失敗しても set -e でループごと落ちないようにする。
      # 一過性の障害（LLM/DBの一時エラー等）でコンテナが停止し、次回間隔まで
      # 何も実行されなくなるのを防ぐ。
      run_summarizer "$@" || echo "[entrypoint] main.py が異常終了しました。次の間隔まで待って再試行します" >&2
      sleep "$interval"
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
