"""HTML をプレーンテキストへ変換するユーティリティ（標準ライブラリのみ）。

Miniflux から取得する RSS 記事本文は HTML そのままで、タグや属性（class, href,
style 等）が大量に含まれる。これをそのまま LLM に渡すと入力トークンが無駄に
膨らむ（実測で生 HTML 約 5,200 トークンに対し、テキスト化すると約 980 トークン）。
そこで LLM 入力前に本文だけを抽出して軽量化する。

方針:
- script / style 等、本文でない要素は中身ごと破棄する
- 段落・リスト・見出しなどのブロック要素は改行として残し、構造を保つ
- リンクの href や画像の alt/src は出力しない（トークン削減のため）
- 壊れた HTML でも例外を出さない（失敗時は正規表現による簡易除去へフォールバック）
"""

import re
from html import unescape
from html.parser import HTMLParser

# 中身ごと破棄する要素（本文として意味を持たない）
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "svg", "template", "head", "iframe"}
)

# 改行の境界として扱うブロック要素
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "table", "blockquote", "pre", "section", "article",
        "header", "footer", "figure", "figcaption", "hr", "dd", "dt", "dl",
    }
)


class _TextExtractor(HTMLParser):
    """HTML からテキストだけを取り出すパーサー。"""

    def __init__(self) -> None:
        # convert_charrefs=True で &amp; / &nbsp; / &#x27; などをデコードする
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        # skip 対象要素のネスト深さ。入れ子や閉じ忘れでもテキストが漏れないよう
        # 単純なカウンタで管理する
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <br/> や <svg/> のような自己終了タグ。skip 要素は開始・終了を
        # 相殺させて深さを変えない
        if tag in _SKIP_TAGS:
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _normalize_whitespace(text: str) -> str:
    """行内の空白を1つにまとめ、各行を strip し、空行を除去する。"""
    text = text.replace("\xa0", " ")
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _fallback_strip(html: str) -> str:
    """パーサーが失敗した場合の簡易タグ除去。"""
    text = re.sub(r"<[^>]+>", " ", html)
    return _normalize_whitespace(unescape(text))


def html_to_text(html: str) -> str:
    """HTML をプレーンテキストに変換する。

    タグは除去して本文のみを残し、ブロック要素は改行、li は "- " 付きの行にする。
    例外は送出せず、変換に失敗した場合は正規表現による除去にフォールバックする。
    """
    if not html:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
        return _normalize_whitespace(parser.get_text())
    except Exception:
        return _fallback_strip(html)
