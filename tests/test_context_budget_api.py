"""In-process API tests: no database, embedding, external HTTP, or real model."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.search import router
from app.core.database import get_session
from app.services.search import SearchResultItem


def item(cid: str, content: str) -> SearchResultItem:
    return SearchResultItem(
        chunkId=cid, documentId="document-1", documentVersion=1,
        title="供热参考资料", sectionPath=None, articleNo=None,
        content=content, sourceUrl=None, publisher=None, publishedAt=None,
        effectiveStatus="REFERENCE_ONLY", applicability="北京市",
        retrievalMethod="HYBRID", scoreType="RRF", score=0.032,
    )


class ContextBudgetApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.session = object()
        app.dependency_overrides[get_session] = lambda: self.session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        settings = SimpleNamespace(knowledge_query_api_key="test-only-query",
                                   knowledge_management_api_key="test-only-management")
        auth_settings = patch("app.api.auth.get_settings", return_value=settings)
        auth_settings.start()
        self.addCleanup(auth_settings.stop)
        search_patch = patch("app.api.search.search")
        self.search = search_patch.start()
        self.addCleanup(search_patch.stop)
        self.results = [item("A", "甲乙丙"), item("B", "丁戊己庚"), item("C", "辛壬")]
        self.meta = {"noEvidence": False, "gate": {"bestSimilarity": 0.8}}
        self.search.return_value = (self.results, self.meta)
        self.headers = {"X-API-Key": "test-only-query"}

    def post(self, **fields):
        return self.client.post("/knowledge/search", headers=self.headers,
                                json={"query": "供热", **fields})

    def test_no_budget_preserves_legacy_response_including_null_item_fields(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "query": "供热", "results": [asdict(value) for value in self.results],
            "noEvidence": False, "meta": self.meta,
        })
        self.assertNotIn("context", response.json())

    def test_explicit_null_budget_also_uses_legacy_response(self):
        response = self.post(contextCharBudget=None)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("context", response.json())

    def test_context_is_separate_from_unchanged_raw_results(self):
        before = [asdict(value) for value in self.results]
        response = self.post(contextCharBudget=5)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["results"], before)
        self.assertEqual([asdict(value) for value in self.results], before)
        self.assertEqual(body["meta"], self.meta)
        self.assertFalse(body["noEvidence"])
        context = body["context"]
        self.assertEqual(set(context), {"snippets", "truncated", "omittedChunks"})
        self.assertEqual([value["content"] for value in context["snippets"]], ["甲乙丙", "丁戊"])
        self.assertEqual([value["truncated"] for value in context["snippets"]], [False, True])
        self.assertTrue(context["truncated"])
        self.assertEqual(context["omittedChunks"], 1)
        self.assertEqual(context["snippets"][0]["effectiveStatus"], "REFERENCE_ONLY")
        self.assertEqual(context["snippets"][0]["score"], self.results[0].score)
        self.assertIsNone(context["snippets"][0]["sourceUrl"])

    def test_under_budget_preserves_full_bodies_and_reports_no_omission(self):
        response = self.post(contextCharBudget=12000)
        context = response.json()["context"]
        self.assertFalse(context["truncated"])
        self.assertEqual(context["omittedChunks"], 0)
        self.assertEqual([value["content"] for value in context["snippets"]],
                         [value.content for value in self.results])

    def test_exact_boundary_reports_omission_without_partial_body(self):
        context = self.post(contextCharBudget=3).json()["context"]
        self.assertTrue(context["truncated"])
        self.assertEqual(context["omittedChunks"], 2)
        self.assertFalse(context["snippets"][0]["truncated"])

    def test_budget_does_not_change_retrieval_request_filters_or_k(self):
        response = self.post(contextCharBudget=1, region="北京市", documentTypes=["OTHER"],
                             effectiveStatus=["REFERENCE_ONLY"], authorizedProjectIds=["PRJ-A"],
                             topK=10, retrievalMode="KEYWORD")
        self.assertEqual(response.status_code, 200)
        self.search.assert_called_once()
        session, request = self.search.call_args.args
        self.assertIs(session, self.session)
        self.assertEqual(asdict(request), {
            "query": "供热", "region": "北京市", "documentTypes": ["OTHER"],
            "effectiveStatus": ["REFERENCE_ONLY"], "authorizedProjectIds": ["PRJ-A"],
            "topK": 10, "retrievalMode": "KEYWORD",
        })
        self.assertEqual(len(response.json()["results"]), 3)

    def test_retrieval_no_evidence_remains_no_evidence_with_empty_context(self):
        meta = {"noEvidence": True, "gate": {"reason": "below threshold"}}
        self.search.return_value = ([], meta)
        body = self.post(contextCharBudget=1).json()
        self.assertTrue(body["noEvidence"])
        self.assertEqual(body["results"], [])
        self.assertEqual(body["context"], {"snippets": [], "truncated": False, "omittedChunks": 0})
        self.assertEqual(body["meta"], meta)

    def test_budget_omission_is_not_reported_as_retrieval_no_evidence(self):
        body = self.post(contextCharBudget=1).json()
        self.assertFalse(body["noEvidence"])
        self.assertEqual(body["context"]["omittedChunks"], 2)
        self.assertTrue(body["context"]["truncated"])
        self.assertEqual(body["context"]["snippets"][0]["content"], "甲")

    def test_degradation_and_evidence_gate_metadata_are_preserved(self):
        meta = {**self.meta, "degraded": "vector channel skipped"}
        self.search.return_value = (self.results, meta)
        body = self.post(contextCharBudget=1).json()
        self.assertEqual(body["meta"], meta)
        self.assertFalse(body["noEvidence"])

    def test_context_deduplication_does_not_rewrite_raw_results(self):
        self.results.insert(1, item("A", "重复副本"))
        body = self.post(contextCharBudget=3).json()
        self.assertEqual(len(body["results"]), 4)
        self.assertEqual(len(body["context"]["snippets"]), 1)
        self.assertEqual(body["context"]["omittedChunks"], 2)
        self.assertEqual(body["context"]["snippets"][0]["content"], "甲乙丙")

    def test_code_point_budget_survives_json_serialization(self):
        self.search.return_value = ([item("A", "😀𠀀甲")], self.meta)
        body = self.post(contextCharBudget=2).json()
        self.assertEqual(body["results"][0]["content"], "😀𠀀甲")
        self.assertEqual(body["context"]["snippets"][0]["content"], "😀𠀀")
        self.assertTrue(body["context"]["truncated"])
        self.assertEqual(body["context"]["omittedChunks"], 0)

    def test_invalid_budgets_are_422_and_never_call_retrieval(self):
        for budget in (0, -1, True, False, 1.0, 1.5, "10", "", [], {}):
            with self.subTest(budget=budget):
                response = self.post(contextCharBudget=budget)
                self.assertEqual(response.status_code, 422)
                self.assertIn("contextCharBudget", response.text)
        self.search.assert_not_called()

    def test_backend_failure_is_503_not_empty_evidence(self):
        self.search.side_effect = RuntimeError("test-only-sensitive-detail")
        response = self.post(contextCharBudget=5)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "search backend unavailable"})
        self.assertNotIn("test-only-sensitive-detail", response.text)

    def test_context_failure_is_503_not_silent_unbudgeted_fallback(self):
        with patch("app.api.search.build_context", side_effect=RuntimeError("test-only-detail")):
            response = self.post(contextCharBudget=5)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("test-only-detail", response.text)
        self.assertNotIn("results", response.json())

    def test_query_key_authentication_is_not_bypassed_by_budget(self):
        for headers, status in (({}, 401), ({"X-API-Key": "wrong"}, 403),
                                ({"X-API-Key": "test-only-management"}, 403)):
            with self.subTest(headers=headers):
                response = self.client.post("/knowledge/search", headers=headers,
                                            json={"query": "供热", "contextCharBudget": 1})
                self.assertEqual(response.status_code, status)
        self.search.assert_not_called()

    def test_openapi_exposes_optional_integer_budget_and_context(self):
        schemas = self.client.get("/openapi.json").json()["components"]["schemas"]
        request = schemas["SearchBody"]
        self.assertNotIn("contextCharBudget", request.get("required", []))
        budget_types = request["properties"]["contextCharBudget"]["anyOf"]
        self.assertIn({"type": "integer", "minimum": 1.0}, budget_types)
        self.assertIn("context", schemas["SearchResponse"]["properties"])
        self.assertEqual(set(schemas["KnowledgeContext"]["properties"]),
                         {"snippets", "truncated", "omittedChunks"})


if __name__ == "__main__":
    unittest.main()
