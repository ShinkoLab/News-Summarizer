"""同梱 Miniflux にスターターフィード（docker/feeds.opml）を取り込むヘルパー。

    import_starter_feeds.py OPML_PATH MARKER_PATH

Miniflux ユーザーのフィードが 0 件のときだけ `POST /v1/import` で OPML を取り込む。
すでにフィードがあれば何もしない。一度取り込んだ（またはフィードがあると確認した）後は
MARKER_PATH を作り、以降はユーザーがフィードを全部消しても再取り込みしない。

失敗してもここでは警告を出すだけで終了コード 0 のまま戻る（要約本体を止めない）。
取り込んだが最初の記事を待ちきれなかったときだけ終了コード 10。
認証は `MINIFLUX_API_KEY`（X-Auth-Token）、接続先は `MINIFLUX_BASE_URL`。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# 取り込み直後の最初の取得を待つ上限（秒）。Miniflux はバックグラウンドでフィードを取得するため、
# 待たずに main.py を走らせると最初の実行が 0 件になる。
FIRST_FETCH_WAIT_SECONDS = 90
# 最初の記事を待ちきれなかったときの終了コード（entrypoint.sh が短い間隔で再試行する）
FIRST_FETCH_PENDING_EXIT = 10


def _request(method: str, url: str, token: str, body: bytes | None = None, content_type: str = "application/json"):
    request = urllib.request.Request(
        url,
        data=body,
        headers={"X-Auth-Token": token, "Content-Type": content_type},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _touch(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except OSError as exc:
        print(f"[starter-feeds] マーカーを作れませんでした: {exc}", file=sys.stderr)


def import_starter_feeds(base_url: str, token: str, opml_path: Path, marker: Path, wait_seconds: int = FIRST_FETCH_WAIT_SECONDS) -> bool:
    """取り込み後、最初の記事が届いたら True。待ちきれなかった場合は False（取り込み不要な場合も True）。"""
    base_url = base_url.rstrip("/")
    feeds = _request("GET", f"{base_url}/v1/feeds", token)
    if not isinstance(feeds, list):
        print("[starter-feeds] フィード一覧を取得できませんでした。取り込みをスキップします", file=sys.stderr)
        return True
    if feeds:
        print(f"[starter-feeds] すでに {len(feeds)} 件のフィードがあるため取り込みません", file=sys.stderr)
        _touch(marker)
        return True

    _request("POST", f"{base_url}/v1/import", token, opml_path.read_bytes(), "application/xml")
    _touch(marker)
    imported = _request("GET", f"{base_url}/v1/feeds", token)
    count = len(imported) if isinstance(imported, list) else "?"
    print(f"[starter-feeds] スターターフィードを {count} 件取り込みました。Miniflux の画面で自由に追加・削除できます", file=sys.stderr)

    # 取り込んだだけではエントリがまだ無いので、更新を依頼して最初の取得を待つ。
    try:
        _request("PUT", f"{base_url}/v1/feeds/refresh", token)
    except Exception as exc:  # noqa: BLE001
        print(f"[starter-feeds] フィードの更新依頼に失敗しました（次の自動更新を待ちます）: {exc}", file=sys.stderr)
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        entries = _request("GET", f"{base_url}/v1/entries?status=unread&limit=1", token)
        if isinstance(entries, dict) and entries.get("total", 0) > 0:
            print(f"[starter-feeds] 最初の記事を取得済み（未読 {entries['total']} 件）", file=sys.stderr)
            return True
        time.sleep(3)
    print("[starter-feeds] 最初の記事の取得待ちがタイムアウトしました。次の周回で拾われます", file=sys.stderr)
    return False


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: import_starter_feeds.py OPML_PATH MARKER_PATH", file=sys.stderr)
        return 0
    token = os.environ.get("MINIFLUX_API_KEY", "")
    base_url = os.environ.get("MINIFLUX_BASE_URL", "http://miniflux:8080")
    opml_path = Path(argv[1])
    if not token or not opml_path.is_file():
        return 0
    try:
        if not import_starter_feeds(base_url, token, opml_path, Path(argv[2])):
            return FIRST_FETCH_PENDING_EXIT
    except Exception as exc:  # noqa: BLE001 - 失敗は警告のみ
        print(f"[starter-feeds] 取り込みに失敗しました（無視して続行）: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
