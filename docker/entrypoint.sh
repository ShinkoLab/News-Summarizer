#!/bin/sh
set -eu

PYTHON="/app/.venv/bin/python"
SOURCE="${NEWS_SUMMARIZER_SOURCE:-rss}"

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
      run_summarizer "$@"
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
