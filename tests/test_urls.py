import pytest

from urls import normalize_url, url_key


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-url", "/relative/path"])
def test_normalize_url_returns_none_for_unusable_input(value):
    """URLとして突き合わせに使えないものは None（＝重複判定の対象外）にする。"""
    assert normalize_url(value) is None
    assert url_key(value) is None


def test_tracking_params_are_stripped():
    """実データで重複の主犯だったBBCのRSSパラメータが素のURLと同一視される。"""
    tracked = "https://www.bbc.co.uk/news/articles/cre40875r9vo?at_medium=RSS&at_campaign=rss"
    plain = "https://www.bbc.co.uk/news/articles/cre40875r9vo"
    assert normalize_url(tracked) == normalize_url(plain)
    assert url_key(tracked) == url_key(plain)


@pytest.mark.parametrize(
    "tracked",
    [
        "https://example.com/a?utm_source=rss&utm_medium=feed",
        "https://example.com/a?fbclid=abc",
        "https://example.com/a?ref=newsletter",
        "https://example.com/a?__twitter_impression=true",
    ],
)
def test_known_tracking_params_are_dropped(tracked):
    assert normalize_url(tracked) == "https://example.com/a"


def test_meaningful_query_is_preserved():
    """クエリを丸ごと捨てると別記事が同一キーに潰れるため、既知のもの以外は残す。"""
    assert normalize_url("https://example.com/view?id=123") == "https://example.com/view?id=123"
    assert url_key("https://example.com/view?id=123") != url_key("https://example.com/view?id=124")


def test_meaningful_query_survives_alongside_tracking_params():
    assert (
        normalize_url("https://example.com/view?utm_source=rss&id=123")
        == "https://example.com/view?id=123"
    )


def test_query_order_does_not_change_the_key():
    assert url_key("https://example.com/a?x=1&y=2") == url_key("https://example.com/a?y=2&x=1")


def test_host_scheme_www_and_fragment_are_normalized():
    assert (
        normalize_url("HTTPS://WWW.Example.COM/News/Article#section")
        == "https://example.com/News/Article"
    )


def test_trailing_slash_is_stripped_but_root_is_kept():
    assert url_key("https://example.com/a/") == url_key("https://example.com/a")
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_path_case_is_significant():
    """ホスト名と違いパスは大小を区別するサイトがあるため正規化しない。"""
    assert url_key("https://example.com/A") != url_key("https://example.com/a")


def test_url_key_is_a_sha256_hexdigest():
    key = url_key("https://example.com/a")
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)
