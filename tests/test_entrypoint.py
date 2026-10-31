"""docker/entrypoint.sh のディスパッチ・終了コード・ループ継続のテスト。

`PYTHON=` を差し替えたコピーを実行し、スタブが受け取った引数を記録して確認する
（本物の main.py・ネットワークには触れない）。
"""

from __future__ import annotations

import shutil
import signal
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
    return tmp_path, script, log


def _env(**extra):
    # 鍵の自動発行を飛ばす（MINIFLUX_API_KEY があれば provision は走らない）
    return {"PATH": "/usr/bin:/bin", "MINIFLUX_API_KEY": "k", **extra}


def test_base_url_not_exported_without_api_key(sandbox):
    """メールのみの実行に MINIFLUX_BASE_URL だけを渡すと config.py が起動時に落ちる。"""
    tmp_path, script, _ = sandbox
    env = _env()
    env.pop("MINIFLUX_API_KEY")
    subprocess.run(["sh", str(script), "run-once", "--source", "email"], env=env, timeout=20)
    assert (tmp_path / "base_url").read_text().strip() == "unset"


def test_base_url_defaulted_with_api_key(sandbox):
    tmp_path, script, _ = sandbox
    subprocess.run(["sh", str(script), "run-once"], env=_env(), timeout=20)
    assert (tmp_path / "base_url").read_text().strip() == "http://miniflux:8080"


def test_run_once_adds_default_source_and_forwards_args(sandbox):
    _, script, log = sandbox
    result = subprocess.run(["sh", str(script), "run-once", "--dry-run"], env=_env(), timeout=20)
    assert result.returncode == 0
    assert log.read_text().strip() == "main.py --source all --dry-run"


def test_explicit_source_is_not_overridden(sandbox):
    _, script, log = sandbox
    subprocess.run(
        ["sh", str(script), "run-once", "--source", "email"],
        env=_env(NEWS_SUMMARIZER_SOURCE="rss"),
        timeout=20,
    )
    assert log.read_text().strip() == "main.py --source email"


def test_run_once_propagates_nonzero_exit(sandbox):
    tmp_path, script, _ = sandbox
    (tmp_path / "rc").write_text("1")
    result = subprocess.run(["sh", str(script), "run-once"], env=_env(), timeout=20)
    assert result.returncode == 1


def test_run_loop_keeps_looping_after_failure(sandbox):
    tmp_path, script, log = sandbox
    (tmp_path / "rc").write_text("1")
    proc = subprocess.Popen(
        ["sh", str(script), "run-loop"], env=_env(NEWS_SUMMARIZER_INTERVAL_SECONDS="1")
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if log.exists() and len(log.read_text().splitlines()) >= 2:
                break
            time.sleep(0.2)
        assert proc.poll() is None, "run-loop must survive main.py exiting non-zero"
        assert len(log.read_text().splitlines()) >= 2
    finally:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)


@pytest.mark.parametrize("bad", ["0", "00", "abc", "-5", ""])
def test_invalid_interval_falls_back(sandbox, bad):
    _, script, _ = sandbox
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
    proc = subprocess.Popen(["sh", str(script), "run-once"], env=_env())
    time.sleep(1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) != 0


def test_empty_llm_model_fails_fast(sandbox):
    _, script, log = sandbox
    result = subprocess.run(["sh", str(script), "run-once"], env=_env(LLM_MODEL=""), timeout=20)
    assert result.returncode == 1
    assert not log.exists()


def test_run_once_without_key_exits_nonzero_for_rss(sandbox):
    tmp_path, script, log = sandbox
    env = _env(NEWS_SUMMARIZER_SOURCE="rss")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 1
    assert not log.exists()


def test_source_all_without_key_still_runs_for_email(sandbox):
    _, script, log = sandbox
    env = _env(NEWS_SUMMARIZER_SOURCE="all")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 0
    assert log.exists()


def test_email_only_run_needs_no_key(sandbox):
    _, script, log = sandbox
    env = _env(NEWS_SUMMARIZER_SOURCE="email")
    env.pop("MINIFLUX_API_KEY")
    result = subprocess.run(["sh", str(script), "run-once"], env=env, timeout=20)
    assert result.returncode == 0
    assert log.exists()
