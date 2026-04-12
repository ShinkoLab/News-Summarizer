from dataclasses import dataclass
from datetime import datetime
from pydantic import BaseModel, Field

@dataclass
class Article:
    """各ソースから取得した記事の共通フォーマット"""
    source_type: str       # "rss" | "email"
    source_id: str         # ソース固有のID（Miniflux entry_id / POP3 UIDL）
    title: str             # 記事タイトル
    content: str           # 記事本文
    url: str | None        # 記事URL（メールの場合はNone）
    published_at: datetime # 公開日時
    fetched_at: datetime   # 取得日時
    feed_title: str | None # フィード名（RSSの場合）


class ArticleGroup(BaseModel):
    """類似記事のグループ"""
    group_id: int                # グループID
    topic: str                   # トピック（短い説明）
    article_indices: list[int]   # グループに属する記事のインデックス

class GroupingResult(BaseModel):
    """グルーピング全体の結果"""
    groups: list[ArticleGroup]


class ArticleSummary(BaseModel):
    """個別記事の要約"""
    title: str          # 要約タイトル（日本語）
    summary: str        # 要約本文（100〜200文字、日本語）
    keywords: list[str] # キーワード（3〜5個）
    category: str       # カテゴリ（設定ファイルの categories から選択）


class CategoryDigest(BaseModel):
    """カテゴリ別ダイジェスト"""
    category: str           # カテゴリ名
    articles: list[str]     # 記事の箇条書き（各1〜2文）
    article_count: int      # 記事数

class TopicLabel(BaseModel):
    """クラスタへのトピック名付与結果"""
    group_id: int  # クラスタID（clustering のラベル番号と対応）
    topic: str     # トピック名（日本語、15文字以内）

class TopicNamingResult(BaseModel):
    """全クラスタのトピック命名結果"""
    topics: list[TopicLabel]


class DigestResult(BaseModel):
    """ダイジェスト全体"""
    overview: str                    # 全体概要（2〜3文）
    categories: list[CategoryDigest] # カテゴリ別ダイジェスト
    total_articles: int              # 総記事数
    generated_at: datetime = Field(default_factory=datetime.now)  # 生成日時
