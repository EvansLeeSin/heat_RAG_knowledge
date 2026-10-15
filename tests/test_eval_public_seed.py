"""冻结的三题开发集、真实快照与显式HTML正文读取；完全离线。"""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from eval.corpus_map import CorpusValidationError, chunks_from_snapshot, load_snapshot, prepare_cases
from eval.dataset import DEFAULT_DATASET, DatasetValidationError, load_dataset, source_text

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/datasets/public-seed-beijing-v1/manifest.json"


class PublicSeedDatasetTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset(DATASET)
        self.snapshot = load_snapshot(DATASET.parent / "snapshot.json")
        self.chunks = chunks_from_snapshot(self.snapshot)

    def test_versioned_development_cases_and_original_datasets_remain_separate(self):
        self.assertEqual(self.dataset.manifest["dataset_version"], "1.0.0")
        self.assertEqual(self.dataset.manifest["split"], "development")
        self.assertEqual([c["type"] for c in self.dataset.cases], ["K", "K", "N"])
        self.assertEqual([len(c["expected"]["evidence"]) for c in self.dataset.cases], [2, 1, 0])
        self.assertEqual(len(load_dataset(DEFAULT_DATASET).cases), 81)
        self.assertEqual(len(load_dataset(ROOT / "eval/datasets/boundaries-v1/manifest.json").cases), 16)
        for case in self.dataset.cases:
            self.assertEqual(case["request"]["region"], "北京市")
            self.assertEqual(case["request"]["effectiveStatus"], ["TO_VERIFY", "REFERENCE_ONLY"])
            self.assertEqual(case["request"]["authorizedProjectIds"], [])

    def test_actual_snapshot_maps_exactly_to_user_approved_gold(self):
        self.assertEqual((self.snapshot["documentCount"], self.snapshot["chunkCount"]), (3, 10))
        positives, negatives = prepare_cases(self.dataset.cases, self.chunks, dataset=self.dataset)
        bindings = json.loads((DATASET.parent / "gold-bindings.json").read_text(encoding="utf-8"))
        self.assertEqual(bindings["snapshot_id"], self.snapshot["snapshotId"])
        self.assertEqual({c["case_id"]: sorted(c["_relevant"]) for c in positives + negatives}, bindings["bindings"])
        self.assertEqual([len(c["_relevant"]) for c in positives + negatives], [2, 1, 0])
        self.assertTrue(all(c["hasEmbedding"] for c in self.chunks))
        self.assertTrue(all(len(c["_corpus_chunk_ids"]) == 10 for c in positives + negatives))

    def test_uploaded_source_bytes_and_dates_are_not_rewritten(self):
        documents = {d["documentId"]: d for d in self.snapshot["documents"]}
        for entry in self.dataset.manifest["corpus"]:
            document = documents[entry["metadata"]["documentId"]]
            self.assertEqual(document["sourceContentHash"], entry["sha256"])
            self.assertEqual(entry["metadata"]["sourceTextFormat"], "html-body")
            self.assertIsNone(document["publishedAt"])
            self.assertIsNone(document["effectiveFrom"])
            self.assertIsNone(document["effectiveTo"])

    def test_modified_case_file_does_not_silently_replace_frozen_version(self):
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory) / "dataset"
            shutil.copytree(DATASET.parent, copied)
            with (copied / "cases.jsonl").open("a", encoding="utf-8") as stream:
                stream.write('{}\n')
            with self.assertRaisesRegex(DatasetValidationError, "哈希"):
                load_dataset(copied / "manifest.json")

    def test_snapshot_corruption_and_document_metadata_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = deepcopy(self.snapshot)
            broken["chunks"][0]["content"] += "未经登记的修改"
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(CorpusValidationError):
                load_snapshot(path)
        chunks = deepcopy(self.chunks)
        for chunk in chunks:
            chunk["documentMetadata"]["effectiveStatus"] = "CURRENT"
        with self.assertRaisesRegex(DatasetValidationError, "元数据"):
            prepare_cases(self.dataset.cases, chunks, dataset=self.dataset)


class ExplicitHtmlBodyTests(unittest.TestCase):
    def source(self, text, text_format="mainText"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.html"
            path.write_text(text, encoding="utf-8")
            return source_text(path, text_format=text_format)

    def test_body_requires_explicit_opt_in_and_old_main_text_behavior_remains(self):
        html = '<html><head><title>页标题</title></head><body><p>正文证据</p></body></html>'
        with self.assertRaisesRegex(DatasetValidationError, "mainText"):
            self.source(html)
        self.assertEqual(self.source(html, "html-body"), "正文证据")
        self.assertEqual(self.source('<div>导航</div><div id="mainText">旧正文</div>页脚'), "旧正文")

    def test_body_ignores_head_scripts_styles_and_noscript(self):
        html = ('<html><head><title>标题</title></head><body>'
                '<style>不应进入证据</style><script>不应进入证据</script>'
                '<noscript><div>不应进入证据</div></noscript><p>有效证据</p>'
                '</body><div>页外文本</div></html>')
        self.assertEqual(self.source(html, "html-body"), "有效证据")

    def test_unknown_format_and_missing_body_are_rejected(self):
        for value in ("unknown", None, [], {}):
            with self.subTest(value=value), self.assertRaises(DatasetValidationError):
                self.source('<body>正文</body>', value)
        with self.assertRaisesRegex(DatasetValidationError, "html-body"):
            self.source('<div id="mainText">没有body</div>', "html-body")


if __name__ == "__main__":
    unittest.main()
