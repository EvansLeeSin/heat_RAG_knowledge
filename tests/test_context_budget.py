"""Character-budget fallback must preserve raw evidence and retrieval order."""

from __future__ import annotations

from dataclasses import asdict, replace
import random
import unittest

from app.services.context import build_context
from app.services.search import SearchResultItem


def item(cid: str, content: str, **changes) -> SearchResultItem:
    source = SearchResultItem(
        chunkId=cid, documentId="document-1", documentVersion=1,
        title="供热参考资料", sectionPath="第一章", articleNo="第十二条",
        content=content, sourceUrl="https://example.org/heating",
        publisher="测试发布单位", publishedAt=None,
        effectiveStatus="REFERENCE_ONLY", applicability="北京市",
        retrievalMethod="HYBRID", scoreType="RRF", score=0.032,
    )
    return replace(source, **changes)


class ContextBudgetTests(unittest.TestCase):
    def test_under_budget_keeps_full_bodies_order_and_all_metadata(self):
        results = [item("B", "第二段"), item("A", "第一段")]
        before = [asdict(value) for value in results]
        context = build_context(results, 100)
        self.assertEqual(context.omittedChunks, 0)
        self.assertFalse(context.truncated)
        for original, snippet in zip(before, context.snippets):
            self.assertEqual(asdict(snippet), {**original, "truncated": False})
        self.assertEqual([asdict(value) for value in results], before)
        self.assertIsNot(context.snippets[0], results[0])

    def test_full_body_over_old_1200_limit_is_not_shortened(self):
        body = "证" * 1308
        context = build_context([item("A", body)], 2000)
        self.assertEqual(context.snippets[0].content, body)
        self.assertFalse(context.truncated)

    def test_total_over_old_3600_limit_is_retained_when_budget_allows(self):
        results = [item(str(index), "文" * 1200) for index in range(10)]
        context = build_context(results, 12000)
        self.assertEqual(len(context.snippets), 10)
        self.assertEqual(sum(len(value.content) for value in context.snippets), 12000)
        self.assertFalse(context.truncated)
        self.assertEqual(context.omittedChunks, 0)

    def test_exact_total_budget_does_not_mark_truncation(self):
        context = build_context([item("A", "甲乙"), item("B", "丙丁戊")], 5)
        self.assertEqual([value.content for value in context.snippets], ["甲乙", "丙丁戊"])
        self.assertFalse(context.truncated)
        self.assertTrue(all(not value.truncated for value in context.snippets))

    def test_last_body_is_a_prefix_and_later_chunks_are_omitted(self):
        results = [item("A", "甲乙丙"), item("B", "丁戊己庚"), item("C", "辛壬")]
        before = [asdict(value) for value in results]
        context = build_context(results, 5)
        self.assertEqual([value.content for value in context.snippets], ["甲乙丙", "丁戊"])
        self.assertEqual([value.truncated for value in context.snippets], [False, True])
        self.assertTrue(context.truncated)
        self.assertEqual(context.omittedChunks, 1)
        self.assertEqual([asdict(value) for value in results], before)

    def test_exact_chunk_boundary_still_marks_whole_chunk_omission(self):
        context = build_context([item("A", "甲乙"), item("B", "丙丁")], 2)
        self.assertEqual(len(context.snippets), 1)
        self.assertFalse(context.snippets[0].truncated)
        self.assertTrue(context.truncated)
        self.assertEqual(context.omittedChunks, 1)

    def test_partial_final_chunk_is_not_counted_as_wholly_omitted(self):
        context = build_context([item("A", "甲乙丙")], 1)
        self.assertEqual(context.snippets[0].content, "甲")
        self.assertTrue(context.snippets[0].truncated)
        self.assertTrue(context.truncated)
        self.assertEqual(context.omittedChunks, 0)

    def test_does_not_skip_longer_high_ranked_body_to_fit_shorter_one(self):
        context = build_context([item("A", "甲" * 10), item("B", "乙")], 2)
        self.assertEqual([value.chunkId for value in context.snippets], ["A"])
        self.assertEqual(context.snippets[0].content, "甲甲")
        self.assertEqual(context.omittedChunks, 1)

    def test_duplicate_identity_keeps_first_occurrence_without_spending_budget(self):
        results = [item("A", "甲乙"), item("A", "不同副本"), item("B", "丙丁")]
        context = build_context(results, 4)
        self.assertEqual([value.content for value in context.snippets], ["甲乙", "丙丁"])
        self.assertFalse(context.truncated)
        self.assertEqual(context.omittedChunks, 0)
        self.assertEqual(len(results), 3)

    def test_omitted_count_uses_unique_identities(self):
        results = [item("A", "甲乙"), item("B", "丙丁"), item("B", "丙丁")]
        context = build_context(results, 2)
        self.assertEqual(context.omittedChunks, 1)

    def test_document_and_version_are_part_of_identity_not_just_chunk_id(self):
        results = [item("A", "甲"), item("A", "乙", documentVersion=2),
                   item("A", "丙", documentId="document-2")]
        context = build_context(results, 3)
        self.assertEqual([value.content for value in context.snippets], ["甲", "乙", "丙"])
        self.assertFalse(context.truncated)

    def test_equal_text_in_distinct_chunks_is_not_identity_deduplicated(self):
        context = build_context([item("A", "相同"), item("B", "相同")], 4)
        self.assertEqual(len(context.snippets), 2)

    def test_unicode_code_points_not_utf16_units_or_bytes(self):
        context = build_context([item("A", "甲😀乙𠀀丙"), item("B", "丁")], 4)
        self.assertEqual(context.snippets[0].content, "甲😀乙𠀀")
        self.assertEqual(len(context.snippets[0].content), 4)
        self.assertEqual(context.omittedChunks, 1)
        self.assertTrue(context.truncated)

    def test_budget_covers_only_body_not_title_or_source(self):
        source = item("A", "甲", title="长标题" * 100)
        context = build_context([source], 1)
        self.assertEqual(context.snippets[0].title, source.title)
        self.assertEqual(context.snippets[0].sourceUrl, source.sourceUrl)
        self.assertFalse(context.truncated)

    def test_whitespace_and_line_breaks_are_preserved_and_counted(self):
        context = build_context([item("A", " 甲\n乙 ")], 4)
        self.assertEqual(context.snippets[0].content, " 甲\n乙")
        self.assertTrue(context.truncated)

    def test_empty_results_produce_empty_complete_context(self):
        context = build_context([], 1)
        self.assertEqual(asdict(context), {"snippets": [], "truncated": False, "omittedChunks": 0})

    def test_invalid_budgets_are_rejected_without_coercion(self):
        for budget in (None, 0, -1, True, False, "10", 1.0):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                build_context([item("A", "甲")], budget)

    def test_empty_candidate_body_is_not_presented_as_available_evidence(self):
        with self.assertRaises(ValueError):
            build_context([item("A", "")], 1)

    def test_deterministic_budget_and_prefix_invariants(self):
        rng = random.Random(781)
        for case in range(100):
            results = [item(str(index), "".join(rng.choices("甲乙😀𠀀 \n", k=rng.randint(1, 40))))
                       for index in range(rng.randint(0, 12))]
            budget = rng.randint(1, 150)
            before = [asdict(value) for value in results]
            context = build_context(results, budget)
            with self.subTest(case=case):
                original_size = sum(len(value.content) for value in results)
                self.assertEqual(sum(len(value.content) for value in context.snippets),
                                 min(original_size, budget))
                self.assertEqual(context.truncated, original_size > budget)
                self.assertEqual(context.omittedChunks, len(results) - len(context.snippets))
                self.assertEqual([value.chunkId for value in context.snippets],
                                 [value.chunkId for value in results[:len(context.snippets)]])
                for original, snippet in zip(results, context.snippets):
                    self.assertTrue(original.content.startswith(snippet.content))
                    self.assertEqual(snippet.truncated, len(snippet.content) < len(original.content))
                self.assertEqual([asdict(value) for value in results], before)


if __name__ == "__main__":
    unittest.main()
