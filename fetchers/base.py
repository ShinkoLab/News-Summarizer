from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import List
from models import Article


def to_utc(value: datetime) -> datetime:
    """タイムゾーン付きのUTCにそろえる。

    フェッチャーごとに naive / aware が混ざると、同じ実行内で日時を比較した時点で
    TypeError になる（`unify_group_categories()` の同票処理など）。naive は UTC とみなす:
    RFC 2822 の `-0000`（`parsedate_to_datetime` が naive を返す唯一のケース）は
    「UTCだが送信元の時差は不明」という意味であり、Miniflux はオフセットを常に付ける。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FetchError(RuntimeError):
    """Source fetch failed; articles obtained before failure remain usable.

    Successful fetches return a list, including an empty list for no new articles.
    Message-local parse failures are handled by the fetcher, not this exception.
    """

    def __init__(self, message: str, articles: list[Article] | None = None):
        super().__init__(message)
        self.articles = list(articles) if articles is not None else []


class BaseFetcher(ABC):
    @abstractmethod
    def fetch(self) -> List[Article]:
        """Return new articles (possibly empty); raise FetchError on source failure."""
        pass
