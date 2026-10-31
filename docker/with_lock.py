"""排他ロックを取ってからコマンドに exec する薄いラッパー（SQLite 構成の二重実行防止）。

SQLite バックエンドには実行ロック（Firestore の `execution_lock()` 相当）が無いため、
`summarizer`（run-loop）と `summarizer-once` が同時に走ると同じ記事を二重に要約・投稿して
しまう。`docker/entrypoint.sh` は main.py をこのラッパー経由で起動し、共有 volume 上の
ロックファイルに `flock` をかけて直列化する。

    with_lock.py LOCKFILE MODE WAIT_SECONDS -- COMMAND [ARGS...]

- MODE `skip`: ロックが取れなければ待たずに終了コード 75 で戻る（run-loop 用。次の周回に回す）
- MODE `wait`: 最大 WAIT_SECONDS 秒待つ。取れなければ終了コード 75（run-once 用）
- ロックを取れたら `os.execvp` でコマンドに置き換わる。ロックの fd は継承されるので
  コマンドの終了（または強制終了）で自動的に解放され、シグナルもそのままコマンドに届く
- ロックファイルを開けない（読み取り専用の環境など）ときは警告して、ロックなしで実行する
"""

from __future__ import annotations

import fcntl
import os
import sys
import time

EXIT_LOCK_UNAVAILABLE = 75


def main(argv: list[str]) -> int:
    if len(argv) < 6 or argv[4] != "--":
        print("usage: with_lock.py LOCKFILE skip|wait WAIT_SECONDS -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    lock_path, mode, wait_arg, command = argv[1], argv[2], argv[3], argv[5:]
    try:
        wait_seconds = max(0, int(wait_arg))
    except ValueError:
        wait_seconds = 0

    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as exc:
        print(f"[lock] ロックファイル {lock_path} を開けません（{exc}）。ロックなしで実行します", file=sys.stderr)
        os.execvp(command[0], command)

    deadline = time.monotonic() + wait_seconds
    announced = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if not isinstance(exc, BlockingIOError):
                # ロック未対応のファイルシステム等。「実行中」と誤認して永久にスキップしない。
                print(f"[lock] flock が使えません（{exc}）。ロックなしで実行します", file=sys.stderr)
                os.execvp(command[0], command)
            if mode == "skip":
                print("[lock] 別の実行が進行中のため、今回はスキップします", file=sys.stderr)
                return EXIT_LOCK_UNAVAILABLE
            if time.monotonic() >= deadline:
                print(
                    f"[lock] {wait_seconds} 秒待っても別の実行が終わりませんでした。中止します",
                    file=sys.stderr,
                )
                return EXIT_LOCK_UNAVAILABLE
            if not announced:
                print("[lock] 別の実行が進行中です。終わるまで待ちます", file=sys.stderr)
                announced = True
            time.sleep(1)

    os.set_inheritable(fd, True)
    os.execvp(command[0], command)
    return 0  # 到達しない


if __name__ == "__main__":
    sys.exit(main(sys.argv))
