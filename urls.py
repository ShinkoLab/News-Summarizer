"""記事URLの正規化と重複判定キーの生成。

同じ記事が複数のフィードから配信されると、Miniflux は別々の entry として
渡してくる。entry ID だけを重複排除キーにしていると同一記事が何度も保存されるため、
URL を正規化したうえでハッシュ化したキーで突き合わせる。
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 記事の同一性に関与しないクエリパラメータ。ここに載っているものだけを落とす。
# クエリを丸ごと捨てると `?id=123` のように本文を特定するパラメータまで消え、
# 無関係な記事が同一キーに潰れてしまう。
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "cmpid",
        "cmp",
        "ref",
        "ref_src",
        "source",
        "spm",
        "igshid",
    }
)

# 前方一致で落とすパラメータ。BBC の `at_medium=RSS&at_campaign=rss` が実データの主犯。
_TRACKING_PREFIXES = ("utm_", "at_", "__")


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def normalize_url(url: str | None) -> str | None:
    """記事URLを重複判定用に正規化する。

    - scheme / host を小文字化し、host 先頭の `www.` を除去する
    - fragment を除去する
    - トラッキングパラメータを除去し、残りをキー順に並べ直す
    - パスの末尾スラッシュを除去する（ルート `/` は残す）

    URL が無い（メール記事）場合や解析できない場合は None を返す
    ＝ URL による重複排除の対象外にする。
    """
    if not url:
        return None

    stripped = url.strip()
    if not stripped:
        return None

    try:
        parts = urlsplit(stripped)
    except ValueError:
        return None

    # scheme も host も無いものは相対URL等。正規化しても突き合わせに使えない。
    if not parts.scheme or not parts.netloc:
        return None

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    query = urlencode(sorted(query_pairs))

    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def url_key(url: str | None) -> str | None:
    """正規化URLの sha256 を返す。URLが無い/解析できない場合は None。

    Firestore のドキュメントIDとしてそのまま使えるよう、
    `outputs.firestore_database.make_document_id()` と同じ sha256 hexdigest 形式にする。
    """
    normalized = normalize_url(url)
    if normalized is None:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
