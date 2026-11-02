"""Embedding grouping keeps vectors and clusters when optional naming fails."""

import numpy as np
import pytest

from config import SummarizerStepConfig
from pipeline import RunOptions, group_pairs
from summarizer.grouper import group_summaries


@pytest.fixture
def embedding_config(mocker, minimal_app_config):
    minimal_app_config.llm.embedding_model = "test-embedding"
    minimal_app_config.summarizer.steps["grouper"] = SummarizerStepConfig(
        use_embeddings=True, similarity_threshold=0.85
    )
    mocker.patch("config.config", minimal_app_config)
    # Fail rather than accidentally contacting a real naming API in any test.
    mocker.patch("summarizer.grouper.get_client", side_effect=AssertionError("API forbidden"))
    return minimal_app_config


def test_empty_summaries_skip_clustering_and_naming(mocker, embedding_config):
    cluster = mocker.patch("summarizer.grouper.AgglomerativeClustering")
    naming = mocker.patch("summarizer.grouper._name_clusters")
    assert group_summaries([], np.empty((0, 2))).groups == []
    cluster.assert_not_called()
    naming.assert_not_called()


@pytest.mark.parametrize("debug", [False, True])
def test_singleton_skips_clustering_and_naming(
    mocker, embedding_config, make_article_summary, debug
):
    summary = make_article_summary(title="一件だけの記事のタイトルを短くして使います")
    cluster = mocker.patch("summarizer.grouper.AgglomerativeClustering")
    naming = mocker.patch("summarizer.grouper._name_clusters")
    result = group_summaries([summary], np.array([[1.0, 0.0]]), debug=debug)
    assert len(result.groups) == 1
    assert result.groups[0].group_id == 0
    assert result.groups[0].article_indices == [0]
    assert result.groups[0].topic == summary.title[:15]
    cluster.assert_not_called()
    naming.assert_not_called()


@pytest.mark.parametrize("debug", [False, True])
def test_naming_failure_preserves_real_clusters(
    mocker, embedding_config, make_article_summary, debug
):
    summaries = [make_article_summary(title=f"記事{i}") for i in range(3)]
    embeddings = np.array([[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]])
    mocker.patch("summarizer.grouper._name_clusters", side_effect=RuntimeError("naming down"))
    result = group_summaries(summaries, embeddings, debug=debug)
    assert [group.article_indices for group in result.groups] == [[0, 1], [2]]
    assert [group.topic for group in result.groups] == ["記事0", "記事2"]
    assert [group.group_id for group in result.groups] == [0, 1]


def test_successful_naming_is_used(mocker, embedding_config, make_article_summary):
    summaries = [make_article_summary() for _ in range(2)]
    naming = mocker.patch("summarizer.grouper._name_clusters", return_value={0: "共通トピック"})
    result = group_summaries(summaries, np.array([[1.0, 0.0], [1.0, 0.0]]))
    assert result.groups[0].article_indices == [0, 1]
    assert result.groups[0].topic == "共通トピック"
    naming.assert_called_once()


def test_pipeline_singleton_retains_embedding(
    mocker, embedding_config, sample_article, sample_summary
):
    embeddings = np.array([[1.0, 0.0]])
    mocker.patch("summarizer.embedder.get_embeddings", return_value=embeddings)
    naming = mocker.patch("summarizer.grouper._name_clusters")
    groups, actual = group_pairs([(sample_article, sample_summary)], RunOptions())
    assert actual is embeddings
    assert groups == {0: (0, sample_summary.title[:15])}
    naming.assert_not_called()


def test_pipeline_naming_failure_retains_embeddings_and_clusters(
    mocker, embedding_config, make_article, make_article_summary
):
    embeddings = np.array([[1.0, 0.0], [0.99, 0.01], [0.0, 1.0]])
    pairs = [(make_article(source_id=str(i)), make_article_summary(title=f"記事{i}")) for i in range(3)]
    mocker.patch("summarizer.embedder.get_embeddings", return_value=embeddings)
    mocker.patch("summarizer.grouper._name_clusters", side_effect=RuntimeError("naming down"))
    groups, actual = group_pairs(pairs, RunOptions())
    assert actual is embeddings
    assert groups == {0: (0, "記事0"), 1: (0, "記事0"), 2: (1, "記事2")}


def test_pipeline_clustering_failure_retains_embeddings(
    mocker, embedding_config, sample_article, sample_summary
):
    embeddings = np.array([[1.0, 0.0]])
    mocker.patch("summarizer.embedder.get_embeddings", return_value=embeddings)
    mocker.patch("pipeline.group_summaries", side_effect=RuntimeError("clustering failed"))
    groups, actual = group_pairs([(sample_article, sample_summary)], RunOptions())
    assert groups == {}
    assert actual is embeddings


def test_pipeline_embedding_failure_returns_no_vectors(
    mocker, embedding_config, sample_article, sample_summary
):
    mocker.patch("summarizer.embedder.get_embeddings", side_effect=RuntimeError("embedding down"))
    grouping = mocker.patch("pipeline.group_summaries")
    groups, embeddings = group_pairs([(sample_article, sample_summary)], RunOptions())
    assert groups == {}
    assert embeddings is None
    grouping.assert_not_called()
