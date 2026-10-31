#!/bin/sh
set -eu

PYTHON="/app/.venv/bin/python"
# main.py 自体の argparse 既定値（--source all）と合わせる。Cloud Run Job は
# NEWS_SUMMARIZER_SOURCE を設定していないため、ここを rss 等にすると
# email ソースの処理が本番から静かに消える。
SOURCE="${NEWS_SUMMARIZER_SOURCE:-all}"

# MINIFLUX_BASE_URL の既定値はここで一度だけ確定させ、provision_miniflux_key.py と
# main.py の両方に同じ値を見せる（単一の真実源）。ただし main.py 側へは API キーが
# 確定した後にだけ export する。config.py は MINIFLUX_* が1つでもあると miniflux
# セクションを作り、api_key 欠落で起動時に落ちるため、Miniflux を使わない
# （メールのみの）実行に base_url だけを渡してはいけない。
MINIFLUX_DEFAULT_BASE_URL="${MINIFLUX_BASE_URL:-http://miniflux:8080}"

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
provision_api_key() {
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
      run_bg env "MINIFLUX_BASE_URL=$MINIFLUX_DEFAULT_BASE_URL" "$PYTHON" /app/docker/provision_miniflux_key.py > "$key_tmp" || true
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
  if [ -n "${MINIFLUX_API_KEY:-}" ]; then
    export MINIFLUX_BASE_URL="$MINIFLUX_DEFAULT_BASE_URL"
  elif [ "$SOURCE" != "email" ]; then
    # キーが無いと miniflux 設定そのものが作られず、RSS 取得は「設定が見つかりません」と
    # ログに出るだけで終了コード 0 になる。見落とされないよう、ここで明示する。
    echo "[entrypoint] Miniflux API キーがありません。RSS は取得されません（MINIFLUX_API_KEY か管理者情報を確認してください）" >&2
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
    require_llm_model
    interval="$(validate_positive_int "${NEWS_SUMMARIZER_INTERVAL_SECONDS:-3600}" NEWS_SUMMARIZER_INTERVAL_SECONDS 3600)"
    while true; do
      provision_api_key
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
    require_llm_model
    provision_api_key
    run_summarizer "$@"
    ;;
  -*)
    require_llm_model
    provision_api_key
    run_summarizer "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
