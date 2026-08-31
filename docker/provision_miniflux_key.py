"""Docker Compose 起動時に Miniflux の API キーを自動発行するヘルパー。

`MINIFLUX_API_KEY` が未設定のとき、`MINIFLUX_ADMIN_USERNAME` / `MINIFLUX_ADMIN_PASSWORD`
の Basic 認証で Miniflux の `POST /v1/api-keys` を叩き、発行されたトークンを標準出力に
1行だけ印字する（`docker/entrypoint.sh` が `$(...)` で拾って `MINIFLUX_API_KEY` に設定する）。
失敗しても標準エラーへ警告を出すだけで、ここでは終了コード 0 のまま戻る。API キーが
結局空のままなら main.py 側の Miniflux 認証エラーとして自然に失敗させる。

Miniflux の `GET /v1/api-keys` は発行済みトークンの値を含めて返す（動作確認済み）ため、
`DESCRIPTION` に一致する既存キーがあればそれをそのまま使い回し、無ければ新規作成する。
これによりコンテナ再起動のたびに無効な API キーが積み上がることもない。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from base64 import b64encode

DESCRIPTION = "news-summarizer-docker"


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _request(method: str, url: str, auth_header: str, body: dict | None = None) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": auth_header, "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def find_existing_key(base_url: str, auth_header: str) -> str | None:
    # Miniflux 起動直後は疎通できてもまだ 502 の HTML を返す等、JSON として
    # 壊れた応答が返ることがある。ここで未捕捉例外を出すとスクリプト全体が
    # 落ちて `docker/entrypoint.sh` 側のリトライの1回分が診断メッセージ無しに
    # 消費されてしまうため、他の失敗経路と同じく警告を出して None に倒す。
    try:
        keys = _request("GET", f"{base_url}/v1/api-keys", auth_header)
    except Exception as exc:  # noqa: BLE001 - 何が起きても既存キー無しとして続行する
        print(f"既存の Miniflux API キー一覧の取得に失敗しました: {exc}", file=sys.stderr)
        return None

    if not isinstance(keys, list):
        return None

    for key in keys:
        if isinstance(key, dict) and key.get("description") == DESCRIPTION and key.get("token"):
            return key["token"]
    return None


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

    base_url = base_url.rstrip("/")
    auth_header = "Basic " + b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")

    existing = find_existing_key(base_url, auth_header)
    if existing:
        return existing

    try:
        payload = _request("POST", f"{base_url}/v1/api-keys", auth_header, {"description": DESCRIPTION})
        return payload.get("token") if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 - find_existing_key と同じ理由
        print(f"Miniflux API キーの発行に失敗しました: {exc}", file=sys.stderr)
        return None


def main() -> None:
    base_url = env("MINIFLUX_BASE_URL", "http://miniflux:8080")
    token = create_miniflux_api_key(base_url)
    if token:
        print(token)


if __name__ == "__main__":
    main()
