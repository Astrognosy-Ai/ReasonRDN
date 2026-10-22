"""Tests for tokenized, ranked local recall.

These exercise the real ``rdn.client`` implementation against a temporary
SQLite store. Sample notes are generic so anyone can run them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from rdn.client import RDNClient, _tokenize_query


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenizer:
    def test_bare_acronym(self):
        assert _tokenize_query("CDN") == ["cdn"]

    def test_multi_word_phrase(self):
        assert _tokenize_query("Blue Green Deployment") == ["blue", "green", "deployment"]

    def test_question_keeps_only_content_words(self):
        result = _tokenize_query("what does CDN stand for")
        assert "cdn" in result
        assert "stand" in result
        for stopword in ("what", "does", "for"):
            assert stopword not in result

    def test_longer_question(self):
        result = _tokenize_query("what should I check first in the database migration plan?")
        for word in ("check", "first", "database", "migration", "plan"):
            assert word in result
        for stopword in ("what", "should", "i", "the", "in"):
            assert stopword not in result

    def test_compound_hyphen_token(self):
        result = _tokenize_query("ISSUE-025")
        assert "issue-025" in result
        assert "issue" in result
        assert "025" in result

    def test_semver_token(self):
        result = _tokenize_query("0.6.1")
        assert "0.6.1" in result
        assert "0" in result
        assert "6" in result
        assert "1" in result

    def test_all_stopwords_returns_empty(self):
        assert _tokenize_query("what is the") == []

    def test_empty_string(self):
        assert _tokenize_query("") == []

    def test_dedup(self):
        assert _tokenize_query("CDN cdn CDN").count("cdn") == 1


# ---------------------------------------------------------------------------
# Recall against a temporary store
# ---------------------------------------------------------------------------


SAMPLE_NOTES: List[Dict[str, Any]] = [
    {
        "content": "CDN means content delivery network: cache static files near users.",
        "tags": ["cdn", "naming", "canon"],
        "project": "demo",
    },
    {
        "content": "Image uploads skip the CDN and go straight to storage.",
        "tags": ["uploads", "cdn"],
        "project": "demo",
    },
    {
        "content": "Database migration plan: back up first, then run migrations in a transaction.",
        "tags": ["database", "migration"],
        "project": "demo",
    },
    {
        "content": "ISSUE-025 build cache configuration notes.",
        "tags": ["issue", "config"],
        "project": "demo",
    },
]


def _client(tmp_path: Path, monkeypatch) -> RDNClient:
    monkeypatch.setattr(RDNClient, "_discover_local_node_via_port", lambda self: None)
    monkeypatch.setattr(RDNClient, "_check_health", lambda self: False)
    return RDNClient(db_path=tmp_path / "recall.db")


@pytest.fixture()
def sample_client(tmp_path, monkeypatch) -> RDNClient:
    client = _client(tmp_path, monkeypatch)
    for note in SAMPLE_NOTES:
        result = client.remember(note["content"], tags=note["tags"], project=note["project"])
        assert result["status"] == "remembered"
    return client


def recall(client: RDNClient, query, project=None, limit=10, tags=None):
    return client.recall(query=query, tags=tags, project=project, limit=limit)


class TestRecallFunctionality:
    def test_bare_acronym_returns_hits(self, sample_client):
        assert len(recall(sample_client, "CDN")) == 2

    def test_exact_phrase_returns_hit(self, sample_client):
        results = recall(sample_client, "content delivery network")
        assert any("content delivery network" in r["content"] for r in results)

    def test_question_returns_hits(self, sample_client):
        results = recall(sample_client, "what does CDN stand for")
        assert any("CDN" in r["content"] for r in results)

    def test_single_word_returns_hit(self, sample_client):
        results = recall(sample_client, "migration")
        assert any("migration" in r["content"].lower() for r in results)

    def test_longer_question_returns_hit(self, sample_client):
        results = recall(sample_client, "what should I check first in the database migration plan?")
        assert any("Database migration plan" in r["content"] for r in results)

    def test_exact_phrase_ranks_first(self, sample_client):
        results = recall(sample_client, "content delivery network")
        assert "content delivery network" in results[0]["content"]

    def test_stopword_only_does_not_explode(self, sample_client):
        results = recall(sample_client, "what is the")
        assert isinstance(results, list)
        assert len(results) == 0

    def test_empty_query_returns_all(self, sample_client):
        assert len(recall(sample_client, "", limit=100)) == 4

    def test_limit_respected(self, sample_client):
        assert len(recall(sample_client, "CDN", limit=1)) == 1

    def test_project_filter(self, sample_client):
        assert len(recall(sample_client, "CDN", project="demo")) == 2

    def test_project_filter_nonexistent(self, sample_client):
        assert len(recall(sample_client, "CDN", project="otherproject")) == 0

    def test_ranking_rarer_token_scores_higher(self, tmp_path, monkeypatch):
        client = _client(tmp_path, monkeypatch)
        client.remember("water is important for plants and also for animals", project="demo")
        client.remember("water and also zebrafrost compound is unique", project="demo")
        results = recall(client, "zebrafrost water")
        assert len(results) == 2
        assert "zebrafrost" in results[0]["content"]

    def test_output_shape_preserved(self, sample_client):
        required = {"address", "project", "deposited_at", "content", "tags", "meta", "source"}
        for result in recall(sample_client, "CDN"):
            assert required.issubset(result.keys())

    def test_tags_filter(self, sample_client):
        results = recall(sample_client, "CDN", tags=["canon"])
        assert len(results) == 1
        assert "canon" in results[0]["tags"]
