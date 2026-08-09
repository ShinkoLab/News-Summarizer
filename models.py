from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class SaveResult:
    """`save_batch()` の結果。

    Firestore は保存を複数コミットに分割するため、実行が「全部成功」か
    「全部失敗」かの2択ではなくなった。既読化・処理済みマークの対象を
    実際に永続化できた記事だけに絞るために、何が保存されたかを返す。
    """

    batch_id: int | None                                # 1件も保存できなければ None
    saved: list[tuple[str, str]] = field(default_factory=list)  # (source_type, source_id)
    failed: int = 0                                     # 保存できなかった記事数

    @property
    def is_partial(self) -> bool:
        return self.failed > 0


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
    category: str            # カテゴリ名
    summary: str             # カテゴリ全体を散文でまとめた本文（1〜2段落）
    highlights: list[str]    # 特筆すべきトピック（各1文、最大 MAX_HIGHLIGHTS 件）
    article_count: int       # 記事数（highlights の件数とは一致しない）

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
