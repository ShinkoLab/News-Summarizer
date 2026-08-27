#!/usr/bin/env python3
"""パイプラインが長時間止まっていた後の滞留バックログを、手動で切り捨てるスクリプト。

Minifluxの未読エントリとPOP3の未取得メールのうち、指定日時より前のものを
まとめて「既読化」「削除」する。パイプライン自体は1回の実行が
summarizer.max_articles_per_run（既定100、本番は200）で頭打ちされる設計なので、
数日分のバックログが溜まっても勝手に暴走することはないが、それでも「古い記事を
律儀に全部要約する」ために何度も実行が必要になり、LLM呼び出しの実費と時間が
かさむ。障害復旧などで大量に溜め込んでしまったときに、これを使って
「今日より前のものは追わない」と割り切るために使う。

安全のため既定はドライラン。実際に既読化・削除するには --execute を明示し、
確認プロンプトで yes と答える必要がある（--yes で確認をスキップ可能）。

Usage:
    # 既定（今日0:00 JST より前）のドライラン。件数だけ確認する
    uv run python3 scripts/skip_stale_backlog.py

    # 実際に既読化・削除する
    uv run python3 scripts/skip_stale_backlog.py --execute

    # 日時・対象ソースを指定する
    uv run python3 scripts/skip_stale_backlog.py --before 2026-08-25 --source miniflux --execute
"""
from __future__ import annotations

import argparse
import email
import email.utils
import poplib
import sys
from datetime import datetime
from email.policy import default
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# scripts/ 配下はリポジトリルートを PYTHONPATH に含めずに実行されることが多いため
# （`uv run python3 scripts/skip_stale_backlog.py` 単体では config/logger が
# 見つからない）、ここでリポジトリルートを明示的に sys.path へ追加する。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as config_module  # noqa: E402
from config import reload_config  # noqa: E402
from logger import get_logger  # noqa: E402

logger = get_logger(__name__)

JST = ZoneInfo("Asia/Tokyo")

# Miniflux PUT /v1/entries に一度に渡すentry_ids数の上限（ペイロード肥大化を避ける）。
_MARK_READ_CHUNK_SIZE = 100

# GET /v1/entries の1ページあたりの取得件数。
_MINIFLUX_PAGE_SIZE = 200


def _today_jst_midnight() -> datetime:
    now_jst = datetime.now(JST)
    return now_jst.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_miniflux_datetime(value: str) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Miniflux
# ---------------------------------------------------------------------------

def find_stale_miniflux_entries(before: datetime) -> list[dict]:
    """cutoff より前に公開された未読エントリを、古い順にすべて集める。

    /v1/entries は published_at 昇順で返るため、cutoff 以降のエントリに
    到達した時点で走査を打ち切れる（それより後ろは全部 cutoff 以降のため）。
    """
    miniflux_cfg = config_module.config.miniflux
    if miniflux_cfg is None:
        return []

    base_url = miniflux_cfg.base_url
    headers = {"X-Auth-Token": miniflux_cfg.api_key}
    offset = 0
    stale: list[dict] = []

    while True:
        response = httpx.get(
            f"{base_url}/v1/entries",
            params={
                "status": "unread",
                "order": "published_at",
                "direction": "asc",
                "limit": _MINIFLUX_PAGE_SIZE,
                "offset": offset,
            },
            headers=headers,
            timeout=60.0,
        )
        response.raise_for_status()
        entries = response.json().get("entries", [])
        if not entries:
            break

        reached_cutoff = False
        for entry in entries:
            published_at = _parse_miniflux_datetime(entry.get("published_at", ""))
            if published_at is not None and published_at >= before:
                reached_cutoff = True
                break
            stale.append(entry)

        if reached_cutoff or len(entries) < _MINIFLUX_PAGE_SIZE:
            break
        offset += len(entries)

    return stale


def mark_miniflux_entries_read(entry_ids: list[int]) -> None:
    if not entry_ids:
        return
    miniflux_cfg = config_module.config.miniflux
    base_url = miniflux_cfg.base_url
    headers = {"X-Auth-Token": miniflux_cfg.api_key}

    for i in range(0, len(entry_ids), _MARK_READ_CHUNK_SIZE):
        chunk = entry_ids[i : i + _MARK_READ_CHUNK_SIZE]
        response = httpx.put(
            f"{base_url}/v1/entries",
            headers=headers,
            json={"entry_ids": chunk, "status": "read"},
            timeout=30.0,
        )
        response.raise_for_status()


# ---------------------------------------------------------------------------
# POP3
# ---------------------------------------------------------------------------

def _connect_pop3():
    email_cfg = config_module.config.email
    if email_cfg.use_ssl:
        server = poplib.POP3_SSL(email_cfg.host, email_cfg.port)
    else:
        server = poplib.POP3(email_cfg.host, email_cfg.port)
    server.user(email_cfg.username)
    server.pass_(email_cfg.password)
    return server


def _uidl_map(server) -> dict[str, int]:
    _, listings, _ = server.uidl()
    uidl_map: dict[str, int] = {}
    for listing in listings:
        try:
            msg_num_str, uidl = listing.decode("utf-8").split(" ")
        except ValueError:
            continue
        uidl_map[uidl] = int(msg_num_str)
    return uidl_map


def find_stale_email_uidls(before: datetime) -> list[tuple[str, str]]:
    """cutoff より前の日付のメールのUIDLを集める。(uidl, 件名) のリストを返す。

    本文は不要なので TOP でヘッダーのみ取得し、フルRETRのコストを避ける。
    """
    if config_module.config.email is None:
        return []

    server = _connect_pop3()
    stale: list[tuple[str, str]] = []
    try:
        for uidl, msg_num in _uidl_map(server).items():
            _, lines, _ = server.top(msg_num, 0)
            msg = email.message_from_bytes(b"\r\n".join(lines), policy=default)
            date_str = msg.get("Date")
            if not date_str:
                continue
            msg_date = email.utils.parsedate_to_datetime(date_str)
            if msg_date is None:
                continue
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=JST)
            if msg_date < before:
                stale.append((uidl, str(msg.get("Subject", "(件名なし)"))))
    finally:
        server.quit()

    return stale


def delete_email_uidls(uidls: list[str]) -> int:
    """指定UIDLのメールを削除する。削除できた件数を返す。

    POP3のメッセージ番号はセッションごとに振り直されるため、削除専用に
    新しいセッションを開いてUIDL→番号のマップを取り直す
    （fetchers/email_fetcher.py の EmailFetcher.delete_messages と同じ設計）。
    DELEはQUITで初めて確定するため、途中で失敗したらQUITせずソケットを
    閉じて1通も消さない。
    """
    if not uidls:
        return 0

    server = _connect_pop3()
    deleted = 0
    try:
        uidl_map = _uidl_map(server)
        for uidl in uidls:
            msg_num = uidl_map.get(uidl)
            if msg_num is None:
                logger.warning("削除対象のメールがサーバに見つかりませんでした (uidl: %s)", uidl)
                continue
            server.dele(msg_num)
            deleted += 1
        server.quit()
    except Exception:
        server.close()
        raise

    return deleted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minifluxの未読・POP3の未取得メールのうち、指定日時より前のものを既読化・削除する。"
    )
    parser.add_argument(
        "--before",
        type=str,
        default=None,
        help="この日時より前のものを対象にする（例: 2026-08-25 または 2026-08-25T12:00:00）。"
        "未指定時は本日0:00 JST",
    )
    parser.add_argument(
        "--source",
        choices=["miniflux", "email", "all"],
        default="all",
        help="対象ソース（既定: all）",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="実際に既読化・削除を実行する。未指定時はドライラン（件数確認のみ）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="--execute 実行時の確認プロンプトをスキップする",
    )
    parser.add_argument("--config", type=str, default="config.yaml", help="設定ファイルのパス")
    args = parser.parse_args()

    if args.config != "config.yaml":
        reload_config(args.config)

    if args.before:
        before = datetime.fromisoformat(args.before)
        if before.tzinfo is None:
            before = before.replace(tzinfo=JST)
    else:
        before = _today_jst_midnight()

    print(f"対象カットオフ: {before.isoformat()} より前")
    print(f"モード: {'実行' if args.execute else 'ドライラン'}")
    print()

    if args.source in ("miniflux", "all"):
        if config_module.config.miniflux is None:
            print("[Miniflux] 設定が見つからないためスキップします。")
        else:
            entries = find_stale_miniflux_entries(before)
            print(f"[Miniflux] 対象の未読エントリ: {len(entries)}件")
            for entry in entries[:10]:
                print(f"  - {entry.get('published_at')}  {entry.get('title', '')[:60]}")
            if len(entries) > 10:
                print(f"  ...ほか{len(entries) - 10}件")

            if args.execute and entries:
                if not args.yes:
                    answer = input(f"{len(entries)}件を既読化します。よろしいですか？ [y/N]: ")
                    if answer.strip().lower() != "y":
                        print("[Miniflux] キャンセルしました。")
                        entries = []
                if entries:
                    mark_miniflux_entries_read([e["id"] for e in entries])
                    print(f"[Miniflux] {len(entries)}件を既読化しました。")
            elif not args.execute:
                print("[Miniflux] ドライランのため既読化は行いません（--execute で実行）。")
        print()

    if args.source in ("email", "all"):
        if config_module.config.email is None:
            print("[Email] 設定が見つからないためスキップします。")
        else:
            stale = find_stale_email_uidls(before)
            print(f"[Email] 対象の未取得メール: {len(stale)}件")
            for uidl, subject in stale[:10]:
                print(f"  - {subject[:60]}")
            if len(stale) > 10:
                print(f"  ...ほか{len(stale) - 10}件")

            if args.execute and stale:
                if not args.yes:
                    answer = input(f"{len(stale)}件をサーバから削除します。よろしいですか？ [y/N]: ")
                    if answer.strip().lower() != "y":
                        print("[Email] キャンセルしました。")
                        stale = []
                if stale:
                    deleted = delete_email_uidls([uidl for uidl, _ in stale])
                    print(f"[Email] {deleted}件を削除しました。")
            elif not args.execute:
                print("[Email] ドライランのため削除は行いません（--execute で実行）。")


if __name__ == "__main__":
    main()
