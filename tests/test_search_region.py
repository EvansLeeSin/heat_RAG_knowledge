"""Regression tests for exact, fail-closed region filtering."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.search import (
    SearchRequest,
    exact_article_match,
    keyword_search,
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
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return FakeResult(self.rows)


def _row() -> dict:
    return {
        "chunk_id": "chunk-1",
        "document_id": "document-1",
        "version": 1,
        "title": "Beijing test document",
        "section_path": "Section 1",
        "article_no": "第十二条",
        "content": "Test clause content",
        "source_url": None,
        "publisher": None,
        "published_at": None,
        "effective_status": "TO_VERIFY",
        "applicability": "北京市",
        "distance": 0.1,
        "rank": 0.5,
    }


class RegionFilterTests(unittest.TestCase):
    def assert_region_filter(self, session: FakeSession, expected_region: str):
        self.assertEqual(len(session.calls), 1)
        statement, params = session.calls[0]
        where_clause = statement.split("WHERE", 1)[1]
        self.assertIn("d.region = :region", where_clause)
        self.assertNotIn("d.applicability", where_clause)
        self.assertNotIn("IS NULL", where_clause.upper())
        self.assertNotIn("COALESCE", where_clause.upper())
        self.assertEqual(params["region"], expected_region)

    def test_vector_uses_document_region_not_applicability(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="test query", region="火星", retrievalMode="VECTOR")
        with patch("app.services.search.embed_query", return_value=[0.1]):
            vector_search(session, request)
        self.assert_region_filter(session, "火星")

    def test_keyword_filters_region_in_sql(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="test query", region="北京市")
        with patch("app.services.search.tokenize_to_query", return_value="test"):
            keyword_search(session, request)
        self.assert_region_filter(session, "北京市")

    def test_exact_article_filters_region_in_sql(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="第十二条", region="北京市")
        exact_article_match(session, request, "第十二条")
        self.assert_region_filter(session, "北京市")

    def test_missing_region_preserves_unfiltered_behavior(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="test query", region=None)
        with patch("app.services.search.tokenize_to_query", return_value="test"):
            results = keyword_search(session, request)
        statement, params = session.calls[0]
        self.assertNotIn("d.region = :region", statement)
        self.assertNotIn("region", params)
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
