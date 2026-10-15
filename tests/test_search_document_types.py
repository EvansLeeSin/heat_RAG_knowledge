"""Regression tests for documentTypes filtering in all retrieval channels."""

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
        "title": "Test document",
        "section_path": "Section 1",
        "article_no": "第十二条",
        "content": "Test clause content",
        "source_url": None,
        "publisher": None,
        "published_at": None,
        "effective_status": "TO_VERIFY",
        "applicability": None,
        "distance": 0.1,
        "rank": 0.5,
    }


class DocumentTypesFilterTests(unittest.TestCase):
    def assert_type_filter(self, session: FakeSession, expected_types: list[str]):
        self.assertEqual(len(session.calls), 1)
        statement, params = session.calls[0]
        self.assertIn(
            "d.document_type = ANY(CAST(:document_types AS VARCHAR[]))",
            statement,
        )
        self.assertEqual(params["document_types"], expected_types)

    def test_vector_filter_is_applied_before_candidate_limit_and_combines_scope(self):
        session = FakeSession([_row()])
        request = SearchRequest(
            query="test query",
            documentTypes=["EQUIPMENT_MANUAL"],
            authorizedProjectIds=["PRJ-ALPHA"],
            retrievalMode="VECTOR",
        )
        with patch("app.services.search.embed_query", return_value=[0.1]):
            vector_search(session, request)

        self.assert_type_filter(session, ["EQUIPMENT_MANUAL"])
        statement, params = session.calls[0]
        self.assertIn("d.project_ids ?| CAST(:project_ids AS text[])", statement)
        self.assertEqual(params["project_ids"], ["PRJ-ALPHA"])
        self.assertLess(statement.index("d.document_type = ANY"), statement.index("LIMIT :k"))

    def test_keyword_filter_is_applied_in_sql_without_per_row_lookup(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="test query", documentTypes=["MANAGEMENT_MEASURE"])
        with patch("app.services.search.tokenize_to_query", return_value="test"):
            keyword_search(session, request)

        self.assert_type_filter(session, ["MANAGEMENT_MEASURE"])

    def test_exact_article_filter_is_applied_in_sql(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="第十二条", documentTypes=["EMERGENCY_PLAN"])
        exact_article_match(session, request, "第十二条")

        self.assert_type_filter(session, ["EMERGENCY_PLAN"])

    def test_empty_document_types_preserves_unfiltered_behavior(self):
        session = FakeSession([_row()])
        request = SearchRequest(query="test query", documentTypes=[])
        with patch("app.services.search.tokenize_to_query", return_value="test"):
            results = keyword_search(session, request)

        statement, params = session.calls[0]
        self.assertNotIn("d.document_type = ANY", statement)
        self.assertNotIn("document_types", params)
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
