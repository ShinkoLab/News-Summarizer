from __future__ import annotations

from unittest.mock import patch

from fetchers import html_text
from fetchers.html_text import html_to_text


def test_タグを除去してテキストは残る():
    """a / span / strong などのタグは除去され、内側のテキストが残ること。"""
    html = '<p>Hello <a href="https://example.com">world</a> <strong>now</strong></p>'
    assert html_to_text(html) == "Hello world now"


def test_hrefや画像のsrc_altは出力されない():
    """リンク先URLや画像の alt/src は出力に含まれないこと。"""
    html = '<p><a href="https://example.com/x">link</a><img src="a.png" alt="ALT"></p>'
    text = html_to_text(html)
    assert text == "link"
    assert "example.com" not in text
    assert "ALT" not in text


def test_script_style_noscriptの中身は破棄される():
    """script / style / noscript の中身が出力に漏れないこと。"""
    html = (
        "<p>before</p>"
        "<script>var x = 1;</script>"
        "<style>.a { color: red; }</style>"
        "<noscript>enable js</noscript>"
        "<p>after</p>"
    )
    text = html_to_text(html)
    assert text == "before\nafter"
    for leaked in ("var x", "color", "enable js"):
        assert leaked not in text


def test_入れ子のskip要素でもテキストが漏れない():
    """skip 要素が入れ子になっていても内側のテキストが漏れないこと。"""
    html = (
        "<div>keep1</div>"
        "<svg><g><title>svg-title</title><svg>inner</svg>tail</g></svg>"
        "<noscript><style>.x{}</style>ns-text</noscript>"
        "<div>keep2</div>"
    )
    text = html_to_text(html)
    assert text == "keep1\nkeep2"


def test_head_iframe_templateの中身は破棄される():
    """head / iframe / template の中身が破棄されること。"""
    html = (
        "<html><head><title>T</title></head><body>"
        "<iframe src='x'>fallback</iframe>"
        "<template><p>tpl</p></template>"
        "<p>body</p></body></html>"
    )
    assert html_to_text(html) == "body"


def test_エンティティがデコードされる():
    """&amp; / &nbsp; / &#x27; などが復号されること。"""
    assert html_to_text("<p>A &amp; B &lt;tag&gt; &#x27;q&#x27; &quot;d&quot;</p>") == (
        "A & B <tag> 'q' \"d\""
    )
    assert html_to_text("<p>a&nbsp;b</p>") == "a b"


def test_ブロックタグは改行になりliはハイフン付き():
    """ブロック要素で改行され、li は "- " で始まること。"""
    html = "<h1>Title</h1><p>one</p><p>two</p><ul><li>a</li><li>b</li></ul>"
    assert html_to_text(html) == "Title\none\ntwo\n- a\n- b"


def test_brで改行される():
    """br が改行として扱われること。"""
    assert html_to_text("line1<br>line2<br/>line3") == "line1\nline2\nline3"


def test_空白の正規化():
    """連続空白・nbsp が1つにまとまり、3連続以上の改行が出ないこと。"""
    html = "<p>a   \t  b\xa0\xa0c</p>\n\n\n<div></div><div>  </div><p>  d  </p>"
    text = html_to_text(html)
    assert text == "a b c\nd"
    assert "\n\n\n" not in text
    assert text == text.strip()


def test_壊れたHTMLでも例外を出さない():
    """閉じ忘れタグや単独の < > があっても例外にならないこと。"""
    samples = [
        "<p>unclosed <b>bold",
        "a < b and c > d",
        "<div><p>x</div></p></span>",
        "<<<>>>",
        "<a href='x",
        "<script>never closed",
        "<p attr=>text</p>",
    ]
    for s in samples:
        assert isinstance(html_to_text(s), str)
    assert html_to_text("<p>unclosed <b>bold") == "unclosed bold"
    assert "a" in html_to_text("a < b and c > d")


def test_パーサーが例外を投げた場合は正規表現にフォールバックする():
    """HTMLParser が例外を出しても、タグ除去したテキストが返ること。"""
    with patch.object(html_text._TextExtractor, "feed", side_effect=RuntimeError("boom")):
        assert html_to_text("<p>Fall &amp;  back</p>") == "Fall & back"


def test_プレーンテキストはそのまま通る():
    """タグを含まないテキストは空白正規化のみで通過すること。"""
    assert html_to_text("just plain text") == "just plain text"
    assert html_to_text("  plain   text \n\n second line  ") == "plain text\nsecond line"
    assert html_to_text("日本語のテキスト") == "日本語のテキスト"


def test_空文字列は空文字列を返す():
    """空文字列・空白のみは空文字列になること。"""
    assert html_to_text("") == ""
    assert html_to_text("   \n\t ") == ""


def test_現実的な記事スニペット():
    """典型的な記事HTMLが読みやすい複数段落のテキストになること。"""
    html = """
    <article>
      <h2>新モデル発表</h2>
      <p>同社は&nbsp;<a href="https://example.com/news">新モデル</a>を発表した。</p>
      <figure>
        <img src="https://example.com/a.png" alt="model photo" width="600">
        <figcaption>発表会の様子</figcaption>
      </figure>
      <p>主な特徴は次の通り。</p>
      <ul>
        <li>高速な <b>推論</b></li>
        <li>低コスト</li>
      </ul>
      <script>trackView();</script>
      <style>.x { display: none }</style>
    </article>
    """
    assert html_to_text(html) == (
        "新モデル発表\n"
        "同社は 新モデルを発表した。\n"
        "発表会の様子\n"
        "主な特徴は次の通り。\n"
        "- 高速な 推論\n"
        "- 低コスト"
    )
