"""Docker Compose 起動時に Miniflux の API キーを自動発行するヘルパー。

`MINIFLUX_API_KEY` が未設定のとき、`MINIFLUX_ADMIN_USERNAME` / `MINIFLUX_ADMIN_PASSWORD`
の Basic 認証で Miniflux の `POST /v1/api-keys` を叩き、発行されたトークンを標準出力に
1行だけ印字する（`docker/entrypoint.sh` が `$(...)` で拾って `MINIFLUX_API_KEY` に設定する）。
失敗しても標準エラーへ警告を出すだけで、ここでは終了コード 0 のまま戻る。API キーが
結局空のままなら main.py 側の Miniflux 認証エラーとして自然に失敗させる。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from base64 import b64encode


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def create_miniflux_api_key(base_url: str) -> str | None:
    username = env("MINIFLUX_ADMIN_USERNAME")
    password = env("MINIFLUX_ADMIN_PASSWORD")
    if not username or not password:
        print(
            "MINIFLUX_ADMIN_USERNAME / MINIFLUX_ADMIN_PASSWORD が未設定のため"
            "API キーの自動発行をスキップします",
            file=sys.stderr,
        )
        return None

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/api-keys",
        data=json.dumps({"description": "news-summarizer-docker"}).encode("utf-8"),
        headers={
            "Authorization": "Basic "
            + b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii"),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("token")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"Miniflux API キーの発行に失敗しました: {exc}", file=sys.stderr)
        return None


def main() -> None:
    base_url = env("MINIFLUX_BASE_URL", "http://miniflux:8080")
    token = create_miniflux_api_key(base_url)
    if token:
        print(token)


if __name__ == "__main__":
    main()
