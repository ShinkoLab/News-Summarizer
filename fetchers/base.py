from abc import ABC, abstractmethod
from typing import List
from models import Article

class BaseFetcher(ABC):
    @abstractmethod
    def fetch(self) -> List[Article]:
        """新規記事を取得してArticleのリストを返す"""
        pass
