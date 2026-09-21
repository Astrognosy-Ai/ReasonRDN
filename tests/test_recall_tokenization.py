"""
Tests for the tokenized recall fix (reason-rdn 0.6.1+).

Run against both environments:
  .rdnenv/bin/pytest  rdn_patch/test_recall_tokenization.py -v
  .mcpenv/bin/pytest  rdn_patch/test_recall_tokenization.py -v

The live DB at /agent/.reason-rdn/private-node/warf-node.db is used as
a fixture source for integration tests.  The unit tests create an in-memory
SQLite DB so they are independent of the live node.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import tempfile
import os
from typing import Any, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Inline copies of the helpers (so tests can import without the full package
# import chain, which has relative imports and optional deps).
# ---------------------------------------------------------------------------

_RECALL_STOPWORDS: frozenset = frozenset({
    "what", "who", "how", "why", "when", "where", "which",
    "does", "do", "did", "is", "are", "should", "could", "would", "can",
    "i", "me", "my", "the", "a", "an", "of", "for", "to", "in", "on",
    "and", "or",
})


def _tokenize_query(query: str) -> List[str]:
    raw_parts = re.split(r"[^\w.\-]+", query.lower())
    tokens: List[str] = []
    seen: set = set()

    def _add(tok: str) -> None:
        tok = tok.strip(".-")
        if tok and tok not in _RECALL_STOPWORDS and tok not in seen:
            tokens.append(tok)
            seen.add(tok)

    for part in raw_parts:
        _add(part)
        if "-" in part or "." in part:
            for sub in re.split(r"[.-]", part):
                _add(sub)
    return tokens


def _idf_score(
    tokens: List[str], all_haystacks: List[str], n_docs: int
) -> Dict[str, float]:
    weights: Dict[str, float] = {}
    for tok in tokens:
        df = sum(1 for h in all_haystacks if tok in h) or 1
        weights[tok] = math.log(n_docs / df)
    return weights


def _recall_local_impl(
    db_path: str,
    query: Optional[str],
    tags: Optional[List[str]],
    project: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Pure-Python replica of the patched _recall_local, for isolated testing."""
    raw_query = (query or "").strip()
    needle = raw_query.lower()
    tokens = _tokenize_query(raw_query) if raw_query else []
    use_token_search = bool(tokens)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT address, domain, deposited_at, metadata_json "
            "FROM warf_artifacts WHERE 1=1"
        )
        params: List[Any] = []
        if project:
            sql += " AND domain = ?"
            params.append(project)
        sql += " ORDER BY deposited_at DESC LIMIT ?"
        params.append(limit * 10)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    parsed: List[tuple] = []
    all_haystacks: List[str] = []
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"])
        except Exception:
            continue
        haystack = " ".join(
            [
                row["address"] or "",
                row["domain"] or "",
                row["deposited_at"] or "",
                json.dumps(meta, ensure_ascii=True),
            ]
        ).lower()
        parsed.append((row, meta, haystack))
        all_haystacks.append(haystack)

    n_docs = len(parsed) or 1
    idf = _idf_score(tokens, all_haystacks, n_docs) if use_token_search else {}

    scored: List[tuple] = []
    for row, meta, haystack in parsed:
        if use_token_search:
            matched = [tok for tok in tokens if tok in haystack]
            if not matched:
                continue
            score = sum(idf[tok] for tok in matched)
            if needle and needle in haystack:
                score += 5.0
            tags_str = " ".join(meta.get("tags", []) or []).lower()
            score += 0.5 * sum(1 for tok in matched if tok in tags_str)
        else:
            if needle and needle not in haystack:
                continue
            score = 0.0

        if tags:
            entry_tags = meta.get("tags", []) or []
            if not any(t in entry_tags for t in tags) and not any(
                t in (row["address"] or "") for t in tags
            ):
                continue

        scored.append(
            (
                score,
                {
                    "address": row["address"],
                    "project": row["domain"],
                    "deposited_at": row["deposited_at"],
                    "content": meta.get("content", ""),
                    "tags": meta.get("tags", []),
                    "meta": meta,
                    "source": "local",
                },
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


# ---------------------------------------------------------------------------
# Helpers for the in-memory DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE warf_artifacts (
    artifact_id TEXT PRIMARY KEY,
    address TEXT NOT NULL UNIQUE,
    domain TEXT NOT NULL,
    category TEXT NOT NULL,
    task TEXT NOT NULL,
    deposited_at TEXT NOT NULL,
    audit_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL
)
"""


def _make_db(artifacts: List[Dict[str, Any]]) -> str:
    """Write artifacts to a temp SQLite file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA)
    for i, a in enumerate(artifacts):
        meta = {
            "content": a.get("content", ""),
            "tags": a.get("tags", []),
        }
        conn.execute(
            "INSERT INTO warf_artifacts VALUES (?,?,?,?,?,?,?,?)",
            (
                f"id-{i}",
                a.get("address", f"reason://test/handoff/h-{i:04d}"),
                a.get("domain", "testproject"),
                "handoff",
                "test",
                "2026-01-01T00:00:00+00:00",
                "deadbeef",
                json.dumps(meta),
            ),
        )
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# Unit tests – tokenizer
# ---------------------------------------------------------------------------


class TestTokenizer:
    def test_bare_acronym(self):
        assert _tokenize_query("PCF") == ["pcf"]

    def test_multi_word_phrase(self):
        assert _tokenize_query("Positional Correlation Fields") == [
            "positional",
            "correlation",
            "fields",
        ]

    def test_interrogative_query_pcf(self):
        # "what does … stand for" — only content words survive
        result = _tokenize_query("what does PCF stand for")
        assert "pcf" in result
        assert "stand" in result
        assert "what" not in result
        assert "does" not in result
        assert "for" not in result

    def test_interrogative_query_water(self):
        result = _tokenize_query("what should I lead with in the water utility pitch?")
        assert "water" in result
        assert "utility" in result
        assert "pitch" in result
        assert "lead" in result
        # stopwords stripped
        assert "what" not in result
        assert "should" not in result
        assert "i" not in result
        assert "the" not in result
        assert "in" not in result

    def test_compound_hyphen_token(self):
        result = _tokenize_query("PILOT-025")
        assert "pilot-025" in result
        assert "pilot" in result
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
        result = _tokenize_query("PCF pcf PCF")
        assert result.count("pcf") == 1


# ---------------------------------------------------------------------------
# Integration tests – _recall_local_impl against an in-memory DB
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_db():
    artifacts = [
        {
            "content": "PCF expands to Positional Correlation Fields: geometric operations.",
            "tags": ["pcf", "naming", "canon"],
            "domain": "astrognosy",
        },
        {
            "content": "Laminar cyber detection uses quantized profile, NOT PCF.",
            "tags": ["laminar", "pcf", "claim-boundary"],
            "domain": "astrognosy",
        },
        {
            "content": "Watershed 250 leave-behind: lead with authority boundary in water utility sales.",
            "tags": ["watershed", "sales", "water"],
            "domain": "astrognosy",
        },
        {
            "content": "PILOT-025 raw-tile-to-packet configuration notes.",
            "tags": ["pilot", "config"],
            "domain": "astrognosy",
        },
    ]
    path = _make_db(artifacts)
    yield path
    os.unlink(path)


def recall(db, query, project=None, limit=10, tags=None):
    return _recall_local_impl(db, query, tags, project, limit)


class TestRecallFunctionality:
    def test_bare_acronym_pcf_returns_hits(self, sample_db):
        results = recall(sample_db, "PCF")
        assert len(results) == 2

    def test_exact_phrase_returns_hit(self, sample_db):
        results = recall(sample_db, "Positional Correlation Fields")
        assert len(results) >= 1
        assert any("Positional Correlation Fields" in r["content"] for r in results)

    def test_interrogative_pcf_returns_hits(self, sample_db):
        results = recall(sample_db, "what does PCF stand for")
        assert len(results) >= 1
        assert any("PCF" in r["content"] for r in results)

    def test_watershed_bare_returns_hit(self, sample_db):
        results = recall(sample_db, "Watershed")
        assert len(results) >= 1
        assert any("Watershed" in r["content"] for r in results)

    def test_interrogative_water_utility_returns_hit(self, sample_db):
        results = recall(sample_db, "what should I lead with in the water utility pitch?")
        assert len(results) >= 1
        # Watershed artifact must be in results
        assert any("Watershed" in r["content"] for r in results)

    def test_exact_phrase_ranks_first(self, sample_db):
        """Exact-phrase bonus ensures the verbatim-match artifact sorts first."""
        results = recall(sample_db, "Positional Correlation Fields")
        assert "Positional Correlation Fields" in results[0]["content"]

    def test_stopword_only_does_not_explode(self, sample_db):
        """All-stopword query falls back to substring; 'what is the' matches nothing."""
        results = recall(sample_db, "what is the")
        assert isinstance(results, list)
        assert len(results) == 0

    def test_empty_query_returns_all(self, sample_db):
        results = recall(sample_db, "", limit=100)
        assert len(results) == 4

    def test_limit_respected(self, sample_db):
        results = recall(sample_db, "PCF", limit=1)
        assert len(results) == 1

    def test_project_filter(self, sample_db):
        results = recall(sample_db, "PCF", project="astrognosy")
        assert len(results) == 2

    def test_project_filter_nonexistent(self, sample_db):
        results = recall(sample_db, "PCF", project="otherproject")
        assert len(results) == 0

    def test_ranking_rarer_token_scores_higher(self):
        """An artifact with a rarer-token match should outrank one with common token."""
        # 'zebrafrost' appears only in artifact B; 'water' appears in A and B
        artifacts = [
            {"content": "water is important for plants and also for animals", "tags": []},
            {"content": "water and also zebrafrost compound is unique", "tags": []},
        ]
        db = _make_db(artifacts)
        try:
            results = recall(db, "zebrafrost water")
            assert len(results) == 2
            # Artifact B has zebrafrost (rare) so should rank first
            assert "zebrafrost" in results[0]["content"]
        finally:
            os.unlink(db)

    def test_output_shape_preserved(self, sample_db):
        """Each result must have exactly the expected keys."""
        results = recall(sample_db, "PCF")
        required_keys = {"address", "project", "deposited_at", "content", "tags", "meta", "source"}
        for r in results:
            assert required_keys.issubset(r.keys()), f"Missing keys: {required_keys - r.keys()}"

    def test_tags_filter(self, sample_db):
        results = recall(sample_db, "PCF", tags=["canon"])
        assert len(results) == 1
        assert "canon" in results[0]["tags"]


# ---------------------------------------------------------------------------
# Live DB integration tests (skipped if DB not present)
# ---------------------------------------------------------------------------

LIVE_DB = "/agent/.reason-rdn/private-node/warf-node.db"


@pytest.mark.skipif(not os.path.exists(LIVE_DB), reason="Live DB not present")
class TestLiveDB:
    def test_pcf_bare(self):
        results = recall(LIVE_DB, "PCF")
        assert len(results) >= 2

    def test_positional_correlation_fields(self):
        results = recall(LIVE_DB, "Positional Correlation Fields")
        assert len(results) >= 1

    def test_interrogative_pcf(self):
        results = recall(LIVE_DB, "what does PCF stand for")
        assert len(results) >= 1

    def test_watershed_bare(self):
        results = recall(LIVE_DB, "Watershed")
        assert len(results) >= 1

    def test_interrogative_water_utility(self):
        results = recall(LIVE_DB, "what should I lead with in the water utility pitch?")
        assert len(results) >= 1
        assert any("Watershed" in r["content"] or "water" in r["content"].lower() for r in results)

    def test_exact_phrase_ranks_first(self):
        results = recall(LIVE_DB, "Positional Correlation Fields")
        assert "Positional" in results[0]["content"] or "positional" in results[0]["content"].lower()

    def test_limit_live(self):
        results = recall(LIVE_DB, "PCF", limit=1)
        assert len(results) == 1
