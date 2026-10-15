"""Returned scores must match their types without changing raw evidence gates."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.api.search import SearchResponse
from app.services.rrf import RankedItem, RRF_K
from app.services.search import SearchRequest, SearchResultItem, search


def ranked(cid: str, method: str, rank: int, score: float) -> RankedItem:
    item = SearchResultItem(
        chunkId=cid, documentId="test-document", documentVersion=1,
        title="Test document", sectionPath=None, articleNo=None,
        content=f"Content for {cid}", sourceUrl=None, publisher=None,
        publishedAt=None, effectiveStatus="TO_VERIFY", applicability=None,
        retrievalMethod=method,
        scoreType={"KEYWORD": "TS_RANK_CD", "VECTOR": "COSINE_DISTANCE", "EXACT": "EXACT_MATCH"}[method],
        score=score,
    )
    return RankedItem(chunk_id=cid, item=item, rank=rank, score=score)


class ReturnedRrfScoreTests(unittest.TestCase):
    def execute(self, keywords, vectors, *, mode="HYBRID", top_k=10,
                query="供热", exact=(), min_rank=0.02):
        settings = SimpleNamespace(no_evidence_min_similarity=0.51,
                                   no_evidence_min_ts_rank=min_rank)
        request = SearchRequest(query=query, retrievalMode=mode, topK=top_k,
                                effectiveStatus=["TO_VERIFY"])
        with (
            patch("app.services.search.keyword_search", return_value=keywords) as kw,
            patch("app.services.search.vector_search", return_value=vectors) as vec,
            patch("app.services.search.exact_article_match", return_value=list(exact)),
            patch("app.services.search.get_settings", return_value=settings),
        ):
            results, meta = search(None, request)
        if mode == "KEYWORD":
            vec.assert_not_called()
        if mode == "VECTOR":
            kw.assert_not_called()
        return results, meta

    def test_shared_chunk_returns_actual_rrf_in_api_serialization(self):
        kw = ranked("A", "KEYWORD", 1, 0.375)
        vec = ranked("A", "VECTOR", 1, 0.2)
        results, meta = self.execute([kw], [vec])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].retrievalMethod, "HYBRID")
        self.assertEqual(results[0].scoreType, "RRF")
        self.assertAlmostEqual(results[0].score, 2 / (RRF_K + 1))
        body = SearchResponse(query="供热", results=results, meta=meta).model_dump(mode="json")
        self.assertAlmostEqual(body["results"][0]["score"], 2 / (RRF_K + 1))
        # RankedItem.score is the original channel signal, not the output score.
        self.assertEqual(kw.score, 0.375)
        self.assertEqual(vec.score, 0.2)

    def test_mixed_membership_scores_order_and_chunk_deduplication(self):
        keywords = [ranked("A", "KEYWORD", 1, 0.9), ranked("B", "KEYWORD", 2, 0.8),
                    ranked("C", "KEYWORD", 3, 0.7)]
        vectors = [ranked("B", "VECTOR", 1, 0.1), ranked("D", "VECTOR", 2, 0.2),
                   ranked("A", "VECTOR", 3, 0.3)]
        results, _ = self.execute(keywords, vectors)
        self.assertEqual([r.chunkId for r in results], ["B", "A", "D", "C"])
        expected = {"B": 1/62 + 1/61, "A": 1/61 + 1/63, "D": 1/62, "C": 1/63}
        for item in results:
            self.assertEqual(item.scoreType, "RRF")
            self.assertAlmostEqual(item.score, expected[item.chunkId])
        self.assertEqual([r.score for r in results], sorted([r.score for r in results], reverse=True))

    def test_keyword_mode_keeps_original_score(self):
        results, _ = self.execute([ranked("A", "KEYWORD", 1, 0.375)], [], mode="KEYWORD")
        self.assertEqual((results[0].retrievalMethod, results[0].scoreType, results[0].score),
                         ("KEYWORD", "TS_RANK_CD", 0.375))

    def test_vector_mode_keeps_original_distance(self):
        results, _ = self.execute([], [ranked("A", "VECTOR", 1, 0.33)], mode="VECTOR")
        self.assertEqual((results[0].retrievalMethod, results[0].scoreType, results[0].score),
                         ("VECTOR", "COSINE_DISTANCE", 0.33))

    def test_hybrid_keyword_only_keeps_original_score(self):
        results, meta = self.execute([ranked("A", "KEYWORD", 1, 0.375)], [])
        self.assertEqual((results[0].scoreType, results[0].score), ("TS_RANK_CD", 0.375))
        self.assertEqual(meta["degraded"], "vector channel skipped")

    def test_hybrid_vector_only_keeps_original_distance(self):
        results, meta = self.execute([], [ranked("A", "VECTOR", 1, 0.33)])
        self.assertEqual((results[0].scoreType, results[0].score), ("COSINE_DISTANCE", 0.33))
        self.assertEqual(meta["degraded"], "keyword channel empty")

    def test_strong_keyword_gate_uses_raw_rank_not_small_rrf(self):
        results, meta = self.execute([ranked("A", "KEYWORD", 1, 0.375)],
                                     [ranked("A", "VECTOR", 1, 0.95)], min_rank=0.1)
        self.assertEqual(len(results), 1)
        self.assertLess(results[0].score, 0.1)
        self.assertEqual(meta["gate"]["passed"], 1)
        self.assertEqual(meta["gate"]["bestTsRank"], 0.375)
        self.assertEqual(meta["gate"]["bestSimilarity"], 0.05)

    def test_weak_raw_signals_do_not_pass_because_rrf_exceeds_keyword_gate(self):
        results, meta = self.execute([ranked("A", "KEYWORD", 1, 0.001)],
                                     [ranked("A", "VECTOR", 1, 0.99)])
        self.assertGreater(2 / (RRF_K + 1), 0.02)
        self.assertEqual(results, [])
        self.assertTrue(meta["noEvidence"])
        self.assertEqual(meta["gate"]["filtered"], 1)
        self.assertEqual(meta["gate"]["bestTsRank"], 0.001)
        self.assertEqual(meta["gate"]["bestSimilarity"], 0.01)

    def test_strong_vector_gate_still_accepts_weak_keyword(self):
        results, meta = self.execute([ranked("A", "KEYWORD", 1, 0.001)],
                                     [ranked("A", "VECTOR", 1, 0.2)])
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].score, 2 / (RRF_K + 1))
        self.assertEqual(meta["gate"]["bestSimilarity"], 0.8)

    def test_exact_result_keeps_score_and_placement(self):
        exact = ranked("exact", "EXACT", 1, 1.0).item
        results, _ = self.execute([ranked("A", "KEYWORD", 1, 0.375)],
                                  [ranked("A", "VECTOR", 1, 0.2)],
                                  query="第十二条", exact=[exact])
        self.assertEqual([r.chunkId for r in results], ["exact", "A"])
        self.assertEqual((results[0].retrievalMethod, results[0].scoreType, results[0].score),
                         ("EXACT", "EXACT_MATCH", 1.0))
        self.assertAlmostEqual(results[1].score, 2 / (RRF_K + 1))

    def test_topk_still_truncates_in_fused_order(self):
        results, _ = self.execute([ranked("A", "KEYWORD", 1, 0.375)],
                                  [ranked("B", "VECTOR", 1, 0.2), ranked("A", "VECTOR", 2, 0.3)],
                                  top_k=1)
        self.assertEqual([r.chunkId for r in results], ["A"])
        self.assertAlmostEqual(results[0].score, 1/61 + 1/62)

    def test_empty_channels_still_return_no_evidence(self):
        results, meta = self.execute([], [])
        self.assertEqual(results, [])
        self.assertTrue(meta["noEvidence"])


if __name__ == "__main__":
    unittest.main()
