"""第二项：标准案例、版本、原文审核、条件透传与边界测试；没有真实网络/数据库。"""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from eval.corpus_map import prepare_cases, build_snapshot
from eval.http_client import HttpOutcome
from eval.dataset import (
    DEFAULT_DATASET, DatasetValidationError, check_result_contract, document_allowed,
    load_dataset, request_body, sha256_file, source_text, validate_case, validate_snapshot_corpus,
)
from eval import run_eval, calibrate_threshold, load_corpus, export_snapshot

ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "eval/datasets/boundaries-v1/manifest.json"


def fixture_chunks(dataset):
    """一文档一chunk的离线测试替身，不冒充真实解析/入库产物。"""
    chunks = []
    for i, entry in enumerate(dataset.manifest["corpus"]):
        metadata = {**deepcopy(entry["metadata"]), "version": 1, "sourceContentHash": entry["sha256"]}
        chunks.append({**metadata, "chunkId": f"test-chunk-{i}", "chunkIndex": 0,
                       "content": source_text(dataset.path.parent / entry["file"]),
                       "documentMetadata": metadata})
    return chunks


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset(DEFAULT_DATASET)
        self.case = deepcopy(self.dataset.cases[0])

    def test_all_historical_cases_preserved_and_revision_ledger_complete(self):
        audit = json.loads((DEFAULT_DATASET.parent / "audit.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["legacy_sha256"], sha256_file(ROOT / "eval/annotations.jsonl"))
        self.assertEqual(len(self.dataset.cases), 81)
        self.assertEqual(audit["removed_count"], 0)
        self.assertEqual(audit["revised_count"], 13)
        self.assertEqual(len(audit["cases"]), 81)
        self.assertEqual({c["case_id"] for c in self.dataset.cases}, {c["case_id"] for c in audit["cases"]})
        self.assertEqual(sum(c["action"] == "REVISED" for c in audit["cases"]), 13)
        self.assertEqual([sum(c["type"] == kind for c in self.dataset.cases) for kind in ("K", "S", "N")], [29, 12, 40])
        self.assertEqual({c["split"] for c in self.dataset.cases}, {"development"})

    def test_revised_queries_and_unique_penalty_evidence(self):
        cases = {c["case_id"]: c for c in self.dataset.cases}
        self.assertNotIn("报修之后没人管", cases["BJ-S-007"]["query"])
        self.assertIn("工作组", cases["BJ-K-023"]["query"])
        self.assertIn("2.1.2", cases["BJ-K-023"]["note"])
        proof = cases["BJ-K-005"]["expected"]["evidence"][0]
        text = source_text(self.dataset.path.parent / self.dataset.manifest["corpus"][0]["file"])
        self.assertEqual(text.count(proof["anchor"]), 1)
        self.assertEqual(len(cases["BJ-K-026"]["expected"]["evidence"]), 6)

    def test_schema_rejects_unknown_fields_bad_enums_types_and_evidence(self):
        mutations = [
            lambda c: c.update(case_id=""), lambda c: c.update(query=" "),
            lambda c: c.update(schema_version=True), lambda c: c.update(split="test"),
            lambda c: c.update(type="UNKNOWN"), lambda c: c.update(dataset_version="latest"),
            lambda c: c.update(tags=["same", "same"]), lambda c: c.update(unrecognized=True),
            lambda c: c["request"].update(region=""),
            lambda c: c["request"].update(effectiveStatus=["VALID"]),
            lambda c: c["request"].update(documentTypes=["TYPO"]),
            lambda c: c["request"].update(authorizedProjectIds="EVAL-A"),
            lambda c: c["request"].update(authorizedProjectIds=["A", "A"]),
            lambda c: c["request"].update(topK=999),
            lambda c: c["expected"].update(evidence=[]),
            lambda c: c["expected"].update(behavior="NO_EVIDENCE"),
            lambda c: c["expected"]["evidence"][0].update(version=True),
            lambda c: c["expected"]["evidence"][0].update(document_id="x" * 33),
            lambda c: c["expected"].update(forbidden_document_ids=[c["expected"]["evidence"][0]["document_id"]]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                case = deepcopy(self.case)
                mutate(case)
                with self.assertRaises(DatasetValidationError):
                    validate_case(case)

    def test_request_body_does_not_modify_case_or_allow_reserved_overrides(self):
        original = deepcopy(self.case)
        body = request_body(self.case, 10, "VECTOR")
        self.assertEqual(body["retrievalMode"], "VECTOR")
        self.assertEqual(body["topK"], 10)
        self.assertEqual(self.case, original)
        body["authorizedProjectIds"].append("OTHER")
        self.assertEqual(self.case["request"]["authorizedProjectIds"], [])

    def test_bad_json_duplicate_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(DatasetValidationError, "重复"):
                load_dataset(path)

    def test_tampered_file_missing_source_duplicate_case_and_pending_review(self):
        # 复制资产再变更，不修改仓库中的已版本化文件。
        for mutation in ("hash", "duplicate", "pending", "version", "source", "missing_anchor", "scope"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "eval"
                shutil.copytree(ROOT / "eval/corpus", root / "corpus")
                shutil.copytree(DEFAULT_DATASET.parent, root / "datasets/beijing-v1")
                path = root / "datasets/beijing-v1/manifest.json"
                manifest = json.loads(path.read_text(encoding="utf-8"))
                case_path = path.parent / "cases.jsonl"
                cases = [json.loads(line) for line in case_path.read_text(encoding="utf-8").splitlines()]
                if mutation == "hash":
                    cases[0]["query"] += "（未经版本登记的修改）"
                elif mutation == "duplicate":
                    cases[1]["case_id"] = cases[0]["case_id"]
                elif mutation == "pending":
                    cases[0]["review_status"] = "NEEDS_REVIEW"
                elif mutation == "version":
                    cases[0]["dataset_version"] = "2.0.0"
                elif mutation == "missing_anchor":
                    cases[0]["expected"]["evidence"][0]["anchor"] = "正文不存在的内容"
                elif mutation == "scope":
                    cases[0]["request"]["region"] = "上海市"
                elif mutation == "source":
                    source = path.parent / manifest["corpus"][0]["file"]
                    source.write_text("被修改", encoding="utf-8")
                case_path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
                if mutation != "hash":
                    manifest["cases_sha256"] = sha256_file(case_path)
                    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(DatasetValidationError):
                    load_dataset(path)
                if mutation == "pending":
                    inspected = load_dataset(path, require_ready=False)
                    self.assertEqual(len(inspected.cases), 81)
                    with self.assertRaises(ValueError):
                        prepare_cases(inspected.cases, fixture_chunks(inspected), dataset=inspected)

    def test_base_dataset_full_offline_preflight_preserves_denominator(self):
        chunks = fixture_chunks(self.dataset)
        pos, neg = prepare_cases(self.dataset.cases, chunks, dataset=self.dataset)
        self.assertEqual((len(pos), len(neg)), (41, 40))
        self.assertTrue(all(c["_relevant"] for c in pos))
        self.assertTrue(all("_relevant" not in c for c in self.dataset.cases))


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset(BOUNDARIES)
        self.chunks = fixture_chunks(self.dataset)
        self.cases = {c["case_id"]: c for c in self.dataset.cases}

    def test_boundary_assets_separate_and_all_conditions_have_positive_and_negative_cases(self):
        self.assertEqual(len(self.dataset.cases), 16)
        self.assertEqual(len(self.dataset.manifest["corpus"]), 7)
        ids = {d["metadata"]["documentId"] for d in self.dataset.manifest["corpus"]}
        self.assertTrue(all(len(d) <= 32 for d in ids))
        base_ids = {d["metadata"]["documentId"] for d in load_dataset().manifest["corpus"]}
        self.assertFalse(ids & base_ids)
        for tag in ("region_filter", "status_filter", "type_filter", "project_scope", "null_region"):
            types = {c["type"] for c in self.dataset.cases if tag in c["tags"]}
            self.assertEqual(types, {"K", "N"}, tag)
        pos, neg = prepare_cases(self.dataset.cases, self.chunks, dataset=self.dataset)
        self.assertEqual((len(pos), len(neg)), (9, 7))
        self.assertTrue(all(c["expected"]["excluded_evidence"] for c in neg))

    def test_same_article_in_many_documents_still_resolves_specific_document(self):
        pos, _ = prepare_cases(self.dataset.cases, self.chunks, dataset=self.dataset)
        cases = {c["case_id"]: c for c in pos}
        self.assertEqual(cases["BOUNDARY-001"]["_relevant"], {"test-chunk-0"})
        self.assertEqual(cases["BOUNDARY-002"]["_relevant"], {"test-chunk-1"})

    def test_region_null_does_not_match_applicability_and_project_scope_is_fail_closed(self):
        metadata = self.dataset.manifest["corpus"][-1]["metadata"]
        self.assertEqual(metadata["applicability"], "北京市")
        self.assertFalse(document_allowed(metadata, self.cases["BOUNDARY-015"]["request"]))
        project_a = self.dataset.manifest["corpus"][4]["metadata"]
        self.assertTrue(document_allowed(project_a, self.cases["BOUNDARY-009"]["request"]))
        self.assertFalse(document_allowed(project_a, self.cases["BOUNDARY-010"]["request"]))
        self.assertFalse(document_allowed(project_a, self.cases["BOUNDARY-011"]["request"]))

    def test_missing_excluded_document_and_scope_metadata_drift_abort_preflight(self):
        with self.assertRaises(ValueError):
            prepare_cases(self.dataset.cases, self.chunks[:-1], dataset=self.dataset)
        chunks = deepcopy(self.chunks)
        chunks[4]["documentMetadata"]["accessScope"] = "PUBLIC"
        with self.assertRaises(ValueError):
            prepare_cases(self.dataset.cases, chunks, dataset=self.dataset)

    def test_snapshot_source_hash_drift_and_extra_document_rejected(self):
        chunks = deepcopy(self.chunks)
        chunks[0]["documentMetadata"]["sourceContentHash"] = "different-source"
        with self.assertRaises(DatasetValidationError):
            validate_snapshot_corpus(self.dataset, chunks)
        chunks = deepcopy(self.chunks)
        chunks.append({**chunks[0], "documentId": "outside"})
        with self.assertRaises(DatasetValidationError):
            validate_snapshot_corpus(self.dataset, chunks)

    def test_forbidden_document_return_is_hard_failure(self):
        case = self.cases["BOUNDARY-010"]
        forbidden = case["expected"]["forbidden_document_ids"][0]
        with self.assertRaises(DatasetValidationError):
            check_result_contract(case, [{"documentId": forbidden}])
        check_result_contract(case, [])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.dataset = load_dataset(BOUNDARIES)
        self.chunks = fixture_chunks(self.dataset)
        pos, neg = prepare_cases(self.dataset.cases, self.chunks, dataset=self.dataset)
        self.prepared = pos + neg
        self.by_input = {(c["query"], json.dumps(c["request"], sort_keys=True)): c for c in self.prepared}
        self.calls = []

    def serve(self, query, top_k=10, mode="HYBRID", request=None):
        self.calls.append((query, top_k, mode, deepcopy(request)))
        case = self.by_input[(query, json.dumps(request, sort_keys=True))]
        if case["type"] == "N":
            return [], 0.01, {"noEvidence": True}
        doc_id = case["expected"]["evidence"][0]["document_id"]
        return [{"chunkId": sorted(case["_relevant"])[0], "documentId": doc_id, "documentVersion": 1, "score": 0.1}], 0.01, {"noEvidence": False}

    def test_return_outside_snapshot_or_changed_chunk_is_blocked(self):
        case = self.prepared[0]
        doc_id = case["expected"]["evidence"][0]["document_id"]
        with self.assertRaises(DatasetValidationError):
            check_result_contract(case, [{"documentId": "outside", "chunkId": "outside"}])
        with self.assertRaises(DatasetValidationError):
            check_result_contract(case, [{"documentId": doc_id, "chunkId": "new-version-chunk"}])

    def test_http_payload_includes_all_case_conditions(self):
        case = next(c for c in self.prepared if c["case_id"] == "BOUNDARY-016")
        response = {"results": [], "noEvidence": True}
        with patch.dict(os.environ, {"KNOWLEDGE_BASE": "http://test.invalid", "KNOWLEDGE_QUERY_KEY": "test-only-key"}), \
                patch("urllib.request.build_opener") as opener:
            opened = opener.return_value.open
            opened.return_value.__enter__.return_value.status = 200
            opened.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            run_eval.search_case(case, top_k=17, mode="VECTOR")
        body = json.loads(opened.call_args.args[0].data)
        self.assertEqual(body, {"query": case["query"], **case["request"], "topK": 17, "retrievalMode": "VECTOR"})
        self.assertNotIn("expected", body)
        self.assertNotIn("evidence", body)
        self.assertNotIn("case_id", body)

    def test_run_eval_warmup_positive_and_negative_all_forward_conditions(self):
        def request(body):
            results, latency, response = self.serve(body["query"], top_k=body["topK"], mode=body["retrievalMode"],
                                                  request={k: body[k] for k in self.prepared[0]["request"]})
            return HttpOutcome("SUCCEEDED", latency, 200, {**response, "results": results})
        with tempfile.TemporaryDirectory() as directory:
            snapshot = build_snapshot([c["documentMetadata"] for c in self.chunks], self.chunks)
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            argv = ["--dataset", str(BOUNDARIES), "--snapshot", str(snapshot_path), "--base", "http://test.invalid",
                    "--output-dir", str(Path(directory) / "runs"), "--warmup"]
            with patch.dict(os.environ, {"KNOWLEDGE_QUERY_KEY": "test-only-key"}), \
                    patch.object(run_eval.SearchClient, "request", side_effect=request), redirect_stdout(io.StringIO()):
                self.assertEqual(run_eval.main(argv), 0)
        self.assertEqual(len(self.calls), 16 * 3 * 2)
        for query, top_k, mode, request in self.calls:
            self.assertIn((query, json.dumps(request, sort_keys=True)), self.by_input)
            self.assertEqual(top_k, 10)

    def test_calibration_recommendation_is_blocked_without_searching(self):
        with patch.object(run_eval.SearchClient, "request") as request, redirect_stdout(io.StringIO()):
            self.assertEqual(calibrate_threshold.main([str(BOUNDARIES)]), 2)
            self.assertEqual(calibrate_threshold.main(["--audit"]), 0)
        request.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_preflight_failure_blocks_every_search_call(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "snapshot.json"
            snapshot_path.write_text(json.dumps(build_snapshot([c["documentMetadata"] for c in self.chunks[:-1]],
                                                               self.chunks[:-1])), encoding="utf-8")
            with patch.dict(os.environ, {"KNOWLEDGE_QUERY_KEY": "test-only-key"}), \
                    patch.object(run_eval.SearchClient, "request") as request, patch.object(sys, "stderr", io.StringIO()):
                code = run_eval.main(["--dataset", str(BOUNDARIES), "--snapshot", str(snapshot_path),
                                      "--base", "http://test.invalid", "--output-dir", str(Path(directory) / "runs")])
            self.assertEqual(code, 2)
            request.assert_not_called()

    def test_new_upload_path_requires_explicit_environment_and_credentials(self):
        scenarios = [
            ["load_corpus", "--dataset", str(BOUNDARIES)],
            ["load_corpus", "--dataset", str(BOUNDARIES), "--base", "http://test.invalid"],
            ["load_corpus", "--dataset", str(BOUNDARIES), "--base", "http://test.invalid", "--confirm-test-environment"],
        ]
        for argv in scenarios:
            with self.subTest(argv=argv), patch.object(sys, "argv", argv), \
                    patch.dict(os.environ, {}, clear=True), patch.object(load_corpus, "upload") as upload, \
                    redirect_stdout(io.StringIO()), patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    load_corpus.main()
                upload.assert_not_called()

    def test_manifest_corpus_items_have_absolute_paths_and_do_not_mutate_metadata(self):
        items = self.dataset.corpus_items()
        self.assertEqual(len(items), 7)
        self.assertTrue(all(Path(i["file"]).is_absolute() and Path(i["file"]).exists() for i in items))
        items[0]["metadata"]["title"] = "changed"
        self.assertNotEqual(self.dataset.manifest["corpus"][0]["metadata"]["title"], "changed")

    def test_export_dataset_selector_uses_full_whitelist_without_connecting(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "snapshot.json"
            argv = ["export", "--dataset", str(BOUNDARIES), "--out", str(out)]
            with patch.object(sys, "argv", argv), patch.dict(os.environ, {"KNOWLEDGE_EVAL_DATABASE_URL": "postgresql://test"}), \
                    patch.object(export_snapshot, "export_database", return_value={"documentCount": 7, "chunkCount": 7, "corpusHash": "test"}) as export, \
                    patch.object(export_snapshot, "save_snapshot"), redirect_stdout(io.StringIO()):
                export_snapshot.main()
            self.assertEqual(set(export.call_args.args[1]), {e["metadata"]["documentId"] for e in self.dataset.manifest["corpus"]})

    def test_generation_is_repeatable(self):
        hashes = {p: sha256_file(p) for p in (DEFAULT_DATASET, BOUNDARIES)}
        result = subprocess.run([sys.executable, str(ROOT / "eval/datasets/build_v1.py")], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hashes, {p: sha256_file(p) for p in hashes})


if __name__ == "__main__":
    unittest.main()
