"""docker/entrypoint.sh のディスパッチ・終了コード・ループ継続のテスト。

`PYTHON=` を差し替えたコピーを実行し、スタブが受け取った引数を記録して確認する
（本物の main.py・ネットワークには触れない）。
"""

from __future__ import annotations

import shutil
import signal
import sys
import subprocess
import time
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


@pytest.fixture
def sandbox(tmp_path):
    log = tmp_path / "calls.log"
    stub = tmp_path / "fakepy"
    # 引数を1行で記録し、EXIT_CODE_FILE にあるコード（無ければ 0）で終了する。
    stub.write_text(
        '#!/bin/sh\necho "$@" >> "%s"\n'
        'echo "${MINIFLUX_BASE_URL-unset}" > "%s"\n'
        'if [ -f "%s" ]; then exit "$(cat "%s")"; fi\nexit 0\n'
        % (log, tmp_path / "base_url", tmp_path / "rc", tmp_path / "rc")
    )
    stub.chmod(0o755)
    script = tmp_path / "entrypoint.sh"
    script.write_text(
        ENTRYPOINT.read_text().replace('PYTHON="/app/.venv/bin/python"', f'PYTHON="{stub}"')
    )
    # entrypoint.sh は同じディレクトリのヘルパー（ロックのラッパー・OPML）を参照する
    for name in ("with_lock.py", "import_starter_feeds.py", "feeds.opml"):
        shutil.copy(ENTRYPOINT.parent / name, tmp_path / name)
    return tmp_path, script, log


def _env(tmp_path, **extra):
    # 鍵の自動発行を飛ばす（MINIFLUX_API_KEY があれば provision は走らない）。
    # ロックのラッパーは本物の Python で動かし、ロックファイルは tmp に置く。
    return {
        "PATH": "/usr/bin:/bin",
        "MINIFLUX_API_KEY": "k",
        "MINIFLUX_BUNDLED": "1",  # 既定は同梱モード。既存 Miniflux のテストは "" で上書きする
        "NEWS_SUMMARIZER_LOCK_FILE": str(tmp_path / "lock"),
        "NEWS_SUMMARIZER_LOCK_PYTHON": sys.executable,
        "MINIFLUX_STARTER_FEEDS_MARKER": str(tmp_path / "marker"),
        **extra,
    }


def _main_calls(log):
    """スタブの呼び出し記録のうち main.py の起動だけを返す。"""
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line.startswith("main.py")]


def test_base_url_not_exported_without_api_key(sandbox):
    """メールのみの実行に MINIFLUX_BASE_URL だけを渡すと config.py が起動時に落ちる。"""
    tmp_path, script, _ = sandbox
    env = _env(tmp_path)
    env.pop("MINIFLUX_API_KEY")
    subprocess.run(["sh", str(script), "run-once", "--source", "email"], env=env, timeout=20)
    assert (tmp_path / "base_url").read_text().strip() == "unset"


def test_base_url_defaulted_with_api_key(sandbox):
    tmp_path, script, _ = sandbox
    subprocess.run(["sh", str(script), "run-once"], env=_env(tmp_path), timeout=20)
    assert (tmp_path / "base_url").read_text().strip() == "http://miniflux:8080"


def test_run_once_adds_default_source_and_forwards_args(sandbox):
    tmp_path, script, log = sandbox
    result = subprocess.run(["sh", str(script), "run-once", "--dry-run"], env=_env(tmp_path), timeout=20)
    assert result.returncode == 0
    assert _main_calls(log) == ["main.py --source all --dry-run"]


def test_explicit_source_is_not_overridden(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once", "--source", "email"],
        env=_env(tmp_path, NEWS_SUMMARIZER_SOURCE="rss"),
        timeout=20,
    )
    assert _main_calls(log) == ["main.py --source email"]


def test_run_once_propagates_nonzero_exit(sandbox):
    tmp_path, script, _ = sandbox
    (tmp_path / "rc").write_text("1")
    result = subprocess.run(["sh", str(script), "run-once"], env=_env(tmp_path), timeout=20)
    assert result.returncode == 1


def test_run_loop_keeps_looping_after_failure(sandbox):
    tmp_path, script, log = sandbox
    (tmp_path / "rc").write_text("1")
    proc = subprocess.Popen(
        ["sh", str(script), "run-loop"], env=_env(tmp_path, NEWS_SUMMARIZER_INTERVAL_SECONDS="1")
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if len(_main_calls(log)) >= 2:
                break
            time.sleep(0.2)
        assert proc.poll() is None, "run-loop must survive main.py exiting non-zero"
        assert len(_main_calls(log)) >= 2
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.parametrize("bad", ["0", "00", "abc", "-5", ""])
def test_invalid_interval_falls_back(sandbox, bad):
    tmp_path, script, _ = sandbox
    # 不正値は既定 3600 にフォールバックする。ループ側を覗かず、関数だけ取り出して確かめる。
    out = subprocess.run(
        [
            "sh",
            "-c",
            f'eval "$(sed -n "/^validate_positive_int()/,/^}}/p" {script})"; '
            f'validate_positive_int "{bad}" X 3600 2>/dev/null',
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert out.stdout.strip() == "3600"


def test_sigterm_during_run_once_exits_nonzero(sandbox):
    tmp_path, script, _ = sandbox
    slow = tmp_path / "fakepy"
    slow.write_text("#!/bin/sh\nsleep 30 &\nwait\n")
    proc = subprocess.Popen(["sh", str(script), "run-once"], env=_env(tmp_path))
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) != 0


def test_empty_llm_model_fails_fast(sandbox):
    tmp_path, script, log = sandbox
    result = subprocess.run(["sh", str(script), "run-once"], env=_env(tmp_path, LLM_MODEL=""), timeout=20)
    assert result.returncode == 1
    assert _main_calls(log) == []


def test_run_once_without_key_exits_nonzero_for_rss(sandbox):
    tmp_path, script, log = sandbox
    env = _env(tmp_path, NEWS_SUMMARIZER_SOURCE="rss")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 1
    assert _main_calls(log) == []


def test_source_all_without_key_still_runs_for_email(sandbox):
    tmp_path, script, log = sandbox
    env = _env(tmp_path, NEWS_SUMMARIZER_SOURCE="all", EMAIL_HOST="pop.example.com")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 0
    assert _main_calls(log)


def test_source_all_without_key_and_without_email_fails(sandbox):
    """鍵もメールも無い all は何も処理できない。成功に見せず異常終了する。"""
    tmp_path, script, log = sandbox
    env = _env(tmp_path, NEWS_SUMMARIZER_SOURCE="all")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 1
    assert _main_calls(log) == []


def test_base_url_from_environment_not_leaked_without_api_key(sandbox):
    """Compose が渡す MINIFLUX_BASE_URL だけでは config.py が落ちるので、鍵が無ければ隠す。"""
    tmp_path, script, _ = sandbox
    env = _env(tmp_path, MINIFLUX_BASE_URL="http://host.docker.internal:8080", EMAIL_HOST="h")
    env.pop("MINIFLUX_API_KEY")
    subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert (tmp_path / "base_url").read_text().strip() == "unset"


def test_base_url_from_environment_is_used_with_api_key(sandbox):
    tmp_path, script, _ = sandbox
    subprocess.run(
        ["sh", str(script), "run-once"],
        env=_env(tmp_path, MINIFLUX_BASE_URL="http://host.docker.internal:8080"),
        timeout=20,
    )
    assert (tmp_path / "base_url").read_text().strip() == "http://host.docker.internal:8080"


def test_email_only_run_needs_no_key(sandbox):
    tmp_path, script, log = sandbox
    env = _env(tmp_path, NEWS_SUMMARIZER_SOURCE="email")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 0
    assert _main_calls(log)


# --- 実行ロック（SQLite には execution_lock() が無い） ---

WITH_LOCK = ENTRYPOINT.parent / "with_lock.py"


def _hold_lock(tmp_path, seconds=30):
    """別プロセスでロックを握って「実行中の main.py」を再現する。"""
    holder = subprocess.Popen(
        [sys.executable, str(WITH_LOCK), str(tmp_path / "lock"), "skip", "0", "--", "sleep", str(seconds)]
    )
    time.sleep(1)
    assert holder.poll() is None
    return holder


def test_with_lock_skip_returns_75_while_held(tmp_path):
    holder = _hold_lock(tmp_path)
    try:
        result = subprocess.run(
            [sys.executable, str(WITH_LOCK), str(tmp_path / "lock"), "skip", "0", "--", "echo", "ran"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 75
        assert "ran" not in result.stdout
    finally:
        holder.kill()
        holder.wait()


def test_with_lock_released_when_command_exits(tmp_path):
    first = subprocess.run(
        [sys.executable, str(WITH_LOCK), str(tmp_path / "lock"), "skip", "0", "--", "echo", "a"],
        capture_output=True, text=True, timeout=10,
    )
    second = subprocess.run(
        [sys.executable, str(WITH_LOCK), str(tmp_path / "lock"), "skip", "0", "--", "echo", "b"],
        capture_output=True, text=True, timeout=10,
    )
    assert (first.returncode, first.stdout.strip()) == (0, "a")
    assert (second.returncode, second.stdout.strip()) == (0, "b")


def test_with_lock_wait_times_out(tmp_path):
    holder = _hold_lock(tmp_path)
    try:
        result = subprocess.run(
            [sys.executable, str(WITH_LOCK), str(tmp_path / "lock"), "wait", "2", "--", "echo", "ran"],
            capture_output=True, text=True, timeout=15,
        )
        assert result.returncode == 75
        assert "ran" not in result.stdout
    finally:
        holder.kill()
        holder.wait()


def test_with_lock_runs_unlocked_when_lockfile_unwritable(tmp_path):
    result = subprocess.run(
        [sys.executable, str(WITH_LOCK), str(tmp_path / "missing" / "lock"), "skip", "0", "--", "echo", "ran"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "ran"


def test_run_once_waits_for_running_instance_then_runs(sandbox):
    tmp_path, script, log = sandbox
    holder = _hold_lock(tmp_path, seconds=3)
    started = time.time()
    result = subprocess.run(["sh", str(script), "run-once"], env=_env(tmp_path), timeout=30)
    assert result.returncode == 0
    assert time.time() - started >= 1.5, "run-once must wait while another run holds the lock"
    assert _main_calls(log) == ["main.py --source all"]
    holder.wait()


def test_run_once_fails_clearly_when_lock_wait_expires(sandbox):
    tmp_path, script, log = sandbox
    holder = _hold_lock(tmp_path)
    try:
        result = subprocess.run(
            ["sh", str(script), "run-once"],
            env=_env(tmp_path, NEWS_SUMMARIZER_LOCK_WAIT_SECONDS="2"),
            timeout=20,
        )
        assert result.returncode == 75
        assert _main_calls(log) == []
    finally:
        holder.kill()
        holder.wait()


def test_run_loop_skips_cycle_while_locked_and_keeps_running(sandbox):
    tmp_path, script, log = sandbox
    holder = _hold_lock(tmp_path, seconds=4)
    proc = subprocess.Popen(
        ["sh", str(script), "run-loop"],
        env=_env(tmp_path, NEWS_SUMMARIZER_INTERVAL_SECONDS="1"),
        stderr=subprocess.PIPE, text=True,
    )
    try:
        time.sleep(2)
        assert _main_calls(log) == [], "must not run main.py while the lock is held"
        holder.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline and not _main_calls(log):
            time.sleep(0.2)
        assert _main_calls(log), "run-loop must resume once the lock is free"
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        holder.kill()
    assert "スキップ" in proc.stderr.read()


def test_sigterm_during_locked_run_reaches_main_and_exits(sandbox):
    """ロック越しに起動した main.py にも SIGTERM が届き、PID 1 が待たされない。"""
    tmp_path, script, _ = sandbox
    (tmp_path / "fakepy").write_text("#!/bin/sh\nexec sleep 30\n")
    proc = subprocess.Popen(["sh", str(script), "run-once"], env=_env(tmp_path))
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) != 0
    # ロックが解放されている（ぶら下がった main.py が握り続けていない）
    result = subprocess.run(
        [sys.executable, str(WITH_LOCK), str(tmp_path / "lock"), "skip", "0", "--", "true"], timeout=10
    )
    assert result.returncode == 0


# --- 初期化・スターターフィード ---


def test_sqlite_schema_initialised_before_run(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once"],
        env=_env(tmp_path, DATABASE_BACKEND="sqlite", SQLITE_DATABASE_PATH="/data/x.db"),
        timeout=20,
    )
    calls = log.read_text().splitlines()
    assert calls[0].startswith("-c ") and "create_database" in calls[0]
    assert calls[-1].startswith("main.py")


def test_schema_not_initialised_without_sqlite_path(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(["sh", str(script), "run-once"], env=_env(tmp_path), timeout=20)
    assert not any(line.startswith("-c ") for line in log.read_text().splitlines())


def _starter_calls(log):
    return [line for line in log.read_text().splitlines() if "import_starter_feeds.py" in line]


def test_starter_feeds_imported_in_bundled_mode(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(["sh", str(script), "run-once"], env=_env(tmp_path, MINIFLUX_BUNDLED="1"), timeout=20)
    calls = _starter_calls(log)
    assert len(calls) == 1
    assert calls[0].endswith(f"feeds.opml {tmp_path / 'marker'}")


def test_starter_feeds_not_imported_for_external_miniflux(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once"],
        env=_env(tmp_path, MINIFLUX_BUNDLED="", MINIFLUX_BASE_URL="http://ext:8080"),
        timeout=20,
    )
    assert _starter_calls(log) == []


def test_starter_feeds_can_be_forced_for_external_miniflux(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once"],
        env=_env(tmp_path, MINIFLUX_BUNDLED="", MINIFLUX_BASE_URL="http://ext:8080", MINIFLUX_IMPORT_STARTER_FEEDS="true"),
        timeout=20,
    )
    assert len(_starter_calls(log)) == 1


def test_starter_feeds_can_be_disabled_in_bundled_mode(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once"],
        env=_env(tmp_path, MINIFLUX_BUNDLED="1", MINIFLUX_IMPORT_STARTER_FEEDS="false"),
        timeout=20,
    )
    assert _starter_calls(log) == []


def test_starter_feeds_skipped_when_marker_exists(sandbox):
    tmp_path, script, log = sandbox
    (tmp_path / "marker").touch()
    subprocess.run(["sh", str(script), "run-once"], env=_env(tmp_path, MINIFLUX_BUNDLED="1"), timeout=20)
    assert _starter_calls(log) == []


def test_starter_feeds_skipped_for_email_only(sandbox):
    tmp_path, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once", "--source", "email"], env=_env(tmp_path, MINIFLUX_BUNDLED="1"), timeout=20
    )
    assert _starter_calls(log) == []


def test_starter_feeds_never_attempted_without_api_key(sandbox):
    tmp_path, script, log = sandbox
    env = _env(tmp_path, MINIFLUX_BUNDLED="1", NEWS_SUMMARIZER_SOURCE="all", EMAIL_HOST="pop.example.com")
    env.pop("MINIFLUX_API_KEY")
    subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert _starter_calls(log) == []


@pytest.mark.parametrize("flag", ["--dry-run", "--help"])
def test_starter_feeds_not_imported_for_dry_run_or_help(sandbox, flag):
    tmp_path, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once", flag], env=_env(tmp_path, MINIFLUX_BUNDLED="1"), timeout=20
    )
    assert _starter_calls(log) == []
    assert _main_calls(log)


def test_run_loop_does_not_run_main_without_api_key(sandbox):
    """鍵が無いのに main.py を回すと「成功に見えて空振り」する。飛ばして短い間隔で再挑戦する。"""
    tmp_path, script, log = sandbox
    env = _env(tmp_path, NEWS_SUMMARIZER_SOURCE="rss", NEWS_SUMMARIZER_INTERVAL_SECONDS="1")
    env.pop("MINIFLUX_API_KEY")
    proc = subprocess.Popen(["sh", str(script), "run-loop"], env=env, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(3)
        assert proc.poll() is None
        assert _main_calls(log) == []
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    assert proc.stderr.read().count("API キーがありません") >= 2


def test_external_miniflux_requires_base_url(sandbox):
    """既存 Miniflux 接続（非同梱）で URL が空なら、同梱用の既定 URL に黙って向けず異常終了する。"""
    tmp_path, script, log = sandbox
    result = subprocess.run(
        ["sh", str(script), "run-once"], env=_env(tmp_path, MINIFLUX_BUNDLED=""), timeout=20
    )
    assert result.returncode == 1
    assert _main_calls(log) == []


def test_with_lock_runs_unlocked_when_flock_unsupported(tmp_path, monkeypatch):
    """flock が EAGAIN 以外で失敗する環境では「実行中」と誤認せずロックなしで実行する。"""
    import fcntl
    import importlib.util

    spec = importlib.util.spec_from_file_location("with_lock", WITH_LOCK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    execs = []
    monkeypatch.setattr(fcntl, "flock", lambda *a: (_ for _ in ()).throw(OSError(95, "not supported")))
    monkeypatch.setattr(mod.os, "execvp", lambda cmd, args: execs.append(args) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        mod.main(["with_lock.py", str(tmp_path / "lock"), "skip", "0", "--", "echo", "ran"])
    assert execs == [["echo", "ran"]]


def test_external_email_only_setup_without_base_url_still_runs(sandbox):
    tmp_path, script, log = sandbox
    env = _env(tmp_path, MINIFLUX_BUNDLED="", EMAIL_HOST="pop.example.com")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 0
    assert _main_calls(log)


def test_dry_run_bypasses_the_run_lock(sandbox):
    """--dry-run は書き込まないので、実行中のジョブを待たずに走る。"""
    tmp_path, script, log = sandbox
    holder = _hold_lock(tmp_path)
    try:
        result = subprocess.run(["sh", str(script), "run-once", "--dry-run"], env=_env(tmp_path), timeout=10)
        assert result.returncode == 0
        assert _main_calls(log) == ["main.py --source all --dry-run"]
    finally:
        holder.kill()
        holder.wait()
