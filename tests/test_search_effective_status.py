"""Regression tests for effectiveStatus filtering in every retrieval path."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.rrf import RankedItem
from app.services.search import (
    SearchRequest,
    SearchResultItem,
    exact_article_match,
    keyword_search,
    search,
    vector_search,
)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None
        self.params = None

    def execute(self, statement, params=None):
        self.statement = str(statement)
        self.params = params or {}
        return FakeResult(self.rows)


def _row(status: str = "TO_VERIFY") -> dict:
    return {
        "chunk_id": "chunk-1",
        "document_id": "document-1",
        "version": 1,
        "title": "Test document",
        "section_path": "Section 1",
        "article_no": "第十二条",
        "content": "Test clause content",
        "source_url": None,
        "publisher": None,
        "published_at": None,
        "effective_status": status,
        "applicability": None,
        "distance": 0.1,
        "rank": 0.5,
    }


def _item(status: str = "TO_VERIFY", method: str = "KEYWORD") -> SearchResultItem:
    return SearchResultItem(
        chunkId="chunk-1",
        documentId="document-1",
        documentVersion=1,
        title="Test document",
        sectionPath="Section 1",
        articleNo="第十二条",
        content="Test clause content",
        sourceUrl=None,
        publisher=None,
        publishedAt=None,
        effectiveStatus=status,
        applicability=None,
        retrievalMethod=method,
        scoreType="TS_RANK_CD",
        score=0.5,
    )


class EffectiveStatusFilterTests(unittest.TestCase):
    def assert_status_filter_applied(self, session: FakeSession, statuses: list[str]):
        self.assertIn(
            "d.effective_status = ANY(CAST(:effective_status AS VARCHAR[]))",
            session.statement,
        )
        self.assertEqual(session.params["effective_status"], statuses)

    def test_vector_query_filters_status_in_sql(self):
        session = FakeSession([_row()])
        request = SearchRequest(
            query="test query",
            effectiveStatus=["SUPERSEDED"],
            retrievalMode="VECTOR",
        )
        with patch("app.services.search.embed_query", return_value=[0.1]):
            vector_search(session, request)

        self.assert_status_filter_applied(session, ["SUPERSEDED"])

    def test_keyword_query_filters_status_in_sql(self):
        session = FakeSession([_row()])
        request = SearchRequest(
            query="test query",
            effectiveStatus=["SUPERSEDED"],
            retrievalMode="KEYWORD",
        )
        with patch("app.services.search.tokenize_to_query", return_value="test"):
            keyword_search(session, request)

        self.assert_status_filter_applied(session, ["SUPERSEDED"])

    def test_exact_article_query_filters_status_in_sql(self):
        session = FakeSession([_row()])
        request = SearchRequest(
            query="第十二条",
            effectiveStatus=["SUPERSEDED"],
        )
        exact_article_match(session, request, "第十二条")

        self.assert_status_filter_applied(session, ["SUPERSEDED"])

    def test_empty_status_list_preserves_unfiltered_behavior(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="test query", effectiveStatus=[])
        with patch("app.services.search.tokenize_to_query", return_value="test"):
            results = keyword_search(session, request)

        self.assertNotIn("d.effective_status = ANY", session.statement)
        self.assertNotIn("effective_status", session.params)
        self.assertEqual(len(results), 1)

    def test_final_filter_blocks_a_nonmatching_channel_result(self):
        request = SearchRequest(
            query="test query",
            effectiveStatus=["SUPERSEDED"],
            retrievalMode="KEYWORD",
        )
        ranked = RankedItem(chunk_id="chunk-1", item=_item("TO_VERIFY"), rank=1, score=0.5)
        with patch("app.services.search.keyword_search", return_value=[ranked]):
            results, meta = search(FakeSession([]), request)

        self.assertEqual(results, [])
        self.assertTrue(meta["noEvidence"])

    def test_final_filter_blocks_an_exact_match_bypass(self):
        request = SearchRequest(
            query="第十二条",
            effectiveStatus=["SUPERSEDED"],
            retrievalMode="KEYWORD",
        )
        with (
            patch("app.services.search.keyword_search", return_value=[]),
            patch("app.services.search.exact_article_match", return_value=[_item("TO_VERIFY", "EXACT")]),
        ):
            results, meta = search(FakeSession([]), request)

        self.assertEqual(results, [])
        self.assertTrue(meta["noEvidence"])


if __name__ == "__main__":
    unittest.main()
