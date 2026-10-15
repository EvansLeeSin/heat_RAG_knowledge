"""第四项：确定性指标与真实CLI产物的离线评分；不连接业务/模型/数据库。"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eval import calibrate_threshold, metrics, run_eval, score_eval
from eval.corpus_map import build_snapshot, prepare_cases, chunks_from_snapshot
from eval.dataset import load_dataset, source_text, sha256_file
from eval.http_client import HttpOutcome

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "eval/datasets/boundaries-v1/manifest.json"


class MetricTests(unittest.TestCase):
    def test_perfect_and_irrelevant_ranking(self):
        for fn in (metrics.recall_at_k, metrics.mrr_at_k, metrics.ndcg_at_k):
            self.assertEqual(fn(["a", "b"], {"a", "b"}, 10), 1)
            self.assertEqual(fn([str(i) for i in range(10)], {str(i) for i in range(10)}, 10), 1)
            self.assertEqual(fn(["x"], {"a", "b"}, 10), 0)

    def test_partial_recall_uses_all_gold_not_k_as_denominator(self):
        self.assertEqual(metrics.recall_at_k(["a", "b"], {"a", "b", "c", "d"}, 1), .25)

    def test_duplicates_cannot_inflate_recall_or_ndcg_and_consume_slots(self):
        self.assertEqual(metrics.recall_at_k(["a", "a", "b"], {"a", "b"}, 2), .5)
        self.assertAlmostEqual(metrics.ndcg_at_k(["a", "a", "b"], {"a", "b"}, 3), 1.5 / (1+1/math.log2(3)))
        self.assertEqual(metrics.mrr_at_k(["x", "x", "a"], {"a"}, 10), 1/3)
        self.assertLessEqual(metrics.ndcg_at_k(["a"]*20, {"a"}, 10), 1)

    def test_mrr_stops_at_ten(self):
        self.assertEqual(metrics.mrr_at_k(["x"]*10+["a"], {"a"}, 10), 0)
        self.assertEqual(run_eval.mrr(["x"]*10+["a"], {"a"}), 0)
        self.assertEqual(metrics.mrr_at_k(["x"]*9+["a"], {"a"}, 10), .1)

    def test_empty_results_zero_but_empty_gold_is_not_applicable(self):
        for fn in (metrics.recall_at_k, metrics.mrr_at_k, metrics.ndcg_at_k):
            self.assertEqual(fn([], {"a"}, 10), 0)
            self.assertIsNone(fn(["a"], set(), 10))

    def test_invalid_k_and_identifiers_are_rejected(self):
        for fn in (metrics.recall_at_k, metrics.mrr_at_k, metrics.ndcg_at_k):
            for k in (0, -1, True, 1.5):
                with self.assertRaises(ValueError):
                    fn(["a"], {"a"}, k)
            with self.assertRaises(ValueError):
                fn([None], {"a"}, 10)

    def test_percentiles_empty_single_two_and_twenty(self):
        self.assertIsNone(metrics.percentile([], .95))
        self.assertIsNone(run_eval.p95([]))
        self.assertEqual(metrics.percentile([4], .95), 4)
        self.assertEqual(metrics.percentile([1, 3], .5), 2)
        self.assertAlmostEqual(metrics.percentile([1, 3], .95), 2.9)
        self.assertEqual(metrics.percentile(list(range(1, 21)), .5), 10.5)
        self.assertAlmostEqual(run_eval.p95(list(range(1, 21))), 19.05)

    def test_nonfinite_negative_boolean_latency_is_rejected(self):
        for value in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(ValueError):
                metrics.percentile([value], .95)
        with self.assertRaises(ValueError):
            metrics.percentile([1], 2)

    def test_unknowns_keep_full_denominator_and_primary_value_is_null(self):
        stats = metrics.bounded_mean([1, None])
        self.assertIsNone(stats["value"])
        self.assertEqual(stats["expected_count"], 2)
        self.assertEqual(stats["scored_count"], 1)
        self.assertEqual(stats["observed_subset_mean"], 1)
        self.assertEqual(stats["full_set_bounds"], [.5, 1])
        self.assertEqual(metrics.bounded_mean([0, 1])["value"], .5)
        self.assertIsNone(metrics.bounded_mean([])["value"])
        self.assertFalse(metrics.bounded_mean([])["complete"])

    def test_normal_mode_disabling_is_not_degradation(self):
        self.assertEqual(metrics.degradation({"degraded": {"vector": "disabled_by_mode"}}, "KEYWORD"), "MODE_DISABLED_ONLY")
        self.assertEqual(metrics.degradation({"degraded": {"keyword": "disabled"}}, "VECTOR"), "MODE_DISABLED_ONLY")
        self.assertEqual(metrics.degradation({"degraded": {"vector": "disabled"}}, "HYBRID"), "REPORTED_DEGRADATION")
        self.assertEqual(metrics.degradation({"degraded": "keyword channel empty"}, "HYBRID"), "REPORTED_DEGRADATION")

    def test_vector_empty_without_gate_is_unverified_not_correct_refusal(self):
        body = {"results": [], "noEvidence": True, "meta": {"vectorHits": 0}}
        for mode in ("VECTOR", "HYBRID"):
            self.assertFalse(metrics.reliable_no_evidence(body, mode)[0])
        self.assertTrue(metrics.reliable_no_evidence(body, "KEYWORD")[0])
        body["meta"]["gate"] = {"bestSimilarity": .2}
        self.assertTrue(metrics.reliable_no_evidence(body, "VECTOR")[0])
        body["meta"]["degraded"] = "vector channel skipped"
        self.assertFalse(metrics.reliable_no_evidence(body, "VECTOR")[0])
        body["noEvidence"] = False
        self.assertTrue(metrics.reliable_no_evidence(body, "VECTOR")[0])

    def test_empty_positive_only_and_negative_only_groups(self):
        empty = score_eval.aggregate([])
        self.assertIsNone(empty["success_rate"]["value"])
        self.assertIsNone(empty["latency_success_seconds"]["p95"])
        self.assertFalse(empty["quality_complete"])
        case = {"case_id": "TEST-001", "type": "K", "tags": ["test"], "_relevant": {"a"}}
        row = {"sequence": 1, "outcome": {"status": "SUCCEEDED", "latency_seconds": 1,
               "body": {"results": [{"chunkId": "a"}], "noEvidence": False}}}
        positive = score_eval.aggregate([score_eval.score_case(case, row, "KEYWORD", 10)])
        self.assertIsNone(positive["quality"]["negative_no_evidence_rate"]["value"])
        self.assertTrue(positive["quality_complete"])
        case.update(type="N", _relevant=set())
        row["outcome"]["body"] = {"results": [], "noEvidence": True}
        negative = score_eval.aggregate([score_eval.score_case(case, row, "KEYWORD", 10)])
        self.assertIsNone(negative["quality"]["recall_at_5"]["value"])
        self.assertEqual(negative["quality"]["negative_no_evidence_rate"]["value"], 1)
        self.assertTrue(negative["quality_complete"])

    def test_invalid_gate_signals_do_not_verify_health(self):
        for value in (None, True, "0.2", float("nan"), 1.2):
            self.assertFalse(metrics.reliable_no_evidence({"noEvidence": True, "meta": {"gate": {"bestSimilarity": value}}}, "VECTOR")[0])


class OfflineScoringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dataset = load_dataset(DATASET)
        docs, chunks = [], []
        for index, entry in enumerate(self.dataset.manifest["corpus"]):
            metadata = {**deepcopy(entry["metadata"]), "version": 1, "sourceContentHash": entry["sha256"]}
            docs.append(metadata)
            chunks.append({"chunkId": f"metric-{index}", "documentId": metadata["documentId"], "version": 1,
                           "chunkIndex": 0, "content": source_text(self.dataset.path.parent / entry["file"])})
        self.snapshot = build_snapshot(docs, chunks)
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_path.write_text(json.dumps(self.snapshot, ensure_ascii=False), encoding="utf-8")
        pos, neg = prepare_cases(self.dataset.cases, chunks_from_snapshot(self.snapshot), dataset=self.dataset)
        self.cases = {c["case_id"]: c for c in pos+neg}
        self.positive_count, self.negative_count = len(pos), len(neg)
        self.by_input = {(c["query"], json.dumps(c["request"], sort_keys=True)): c for c in pos+neg}
        self.chunk_map = {c["chunkId"]: c for c in self.snapshot["chunks"]}
        self.run_dir = self.root / "runs/source"
        self.calls = []

    def make_run(self, handler=None, *, modes=("KEYWORD",), warmup=False, top_k=10):
        argv = ["--dataset", str(DATASET), "--snapshot", str(self.snapshot_path), "--base", "http://test.invalid",
                "--modes", *modes, "--top-k", str(top_k), "--output-dir", str(self.root / "runs"), "--run-id", "source"]
        if warmup:
            argv += ["--warmup"]
        def request(body):
            self.calls.append(body)
            conditions = {k: body[k] for k in ("region", "documentTypes", "effectiveStatus", "authorizedProjectIds")}
            case = self.by_input[(body["query"], json.dumps(conditions, sort_keys=True))]
            results = [{"chunkId": chunk, "documentId": self.chunk_map[chunk]["documentId"], "documentVersion": 1,
                        "score": .1, "scoreType": "TS_RANK_CD" if body["retrievalMode"] == "KEYWORD" else "COSINE_DISTANCE"} for chunk in sorted(case["_relevant"])[:top_k]]
            response = {"results": results, "noEvidence": case["type"] == "N", "meta": {
                "keywordHits": 1 if body["retrievalMode"] != "VECTOR" else 0,
                "vectorHits": 1 if body["retrievalMode"] != "KEYWORD" else 0,
                "gate": {"bestSimilarity": (.2 if case["type"] == "N" else .9) if body["retrievalMode"] != "KEYWORD" else None,
                         "bestTsRank": .01, "passed": len(results), "filtered": int(case["type"] == "N")}}}
            latency = 100.0 if warmup and len(self.calls) <= len(self.cases)*len(modes) else 1.0
            return handler(case, body, response, latency) if handler else HttpOutcome("SUCCEEDED", latency, 200, response)
        with patch.dict(os.environ, {"KNOWLEDGE_QUERY_KEY": "test-only-key-8101"}, clear=True), \
                patch.object(run_eval.SearchClient, "request", side_effect=request), redirect_stdout(io.StringIO()):
            return run_eval.main(argv)

    def score(self, score_id="v1", extra=None):
        with patch.dict(os.environ, {}, clear=True), patch.object(run_eval.SearchClient, "request") as request, \
                patch("urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = score_eval.main(["--run-dir", str(self.run_dir), "--score-id", score_id, *(extra or [])])
        request.assert_not_called()
        opener.assert_not_called()
        return status

    def outputs(self, score_id="v1"):
        path = self.run_dir / "scores" / score_id
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        groups = json.loads((path / "metrics.json").read_text(encoding="utf-8"))["by_mode"]
        rows = [json.loads(line) for line in (path / "scores.jsonl").read_text(encoding="utf-8").splitlines()]
        return manifest, groups, rows

    def edit_records(self, mutation):
        path = self.run_dir / "executions.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        mutation(rows)
        path.write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in rows), encoding="utf-8")

    def test_successful_offline_scoring_and_immutable_source(self):
        self.assertEqual(self.make_run(), 0)
        before = {name: (self.run_dir / name).read_bytes() for name in ("manifest.json", "executions.jsonl")}
        self.assertEqual(self.score(), 0)
        manifest, groups, rows = self.outputs()
        group = groups["KEYWORD"]
        self.assertEqual(len(rows), 16)
        self.assertEqual(group["success_rate"]["value"], 1)
        for name in score_eval.RANK_METRICS:
            self.assertEqual(group["quality"][name]["value"], 1)
            self.assertEqual(group["quality"][name]["expected_count"], self.positive_count)
        self.assertEqual(group["quality"]["positive_no_evidence_rate"]["value"], 0)
        self.assertEqual(group["quality"]["negative_no_evidence_rate"]["value"], 1)
        self.assertEqual(manifest["metric_policy_version"], metrics.POLICY_VERSION)
        self.assertEqual(manifest["artifact_sha256"]["scores.jsonl"], sha256_file(self.run_dir / "scores/v1/scores.jsonl"))
        self.assertTrue(all(before[name] == (self.run_dir / name).read_bytes() for name in before))
        self.assertTrue(all(r["metrics"]["mrr_at_10"] is None for r in rows if r["case_type"] == "N"))

    def test_multiple_modes_and_warmup_do_not_contaminate_scores_or_latency(self):
        self.assertEqual(self.make_run(modes=("KEYWORD", "VECTOR", "HYBRID"), warmup=True), 0)
        self.assertEqual(self.score(), 0)
        manifest, groups, rows = self.outputs()
        self.assertEqual(manifest["excluded_warmup_executions"], 48)
        self.assertEqual(len(rows), 48)
        self.assertEqual(set(groups), set(run_eval.MODES))
        self.assertTrue(all(g["expected_count"] == 16 and g["latency_success_seconds"]["p95"] == 1 for g in groups.values()))

    def test_failed_warmup_is_audited_but_not_scored_as_measurement_failure(self):
        def handler(case, body, response, latency):
            if len(self.calls) == 1:
                return HttpOutcome("TIMEOUT", 5, error={"code": "TIMEOUT"})
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        self.assertEqual(self.make_run(handler, warmup=True), 1)
        self.assertEqual(self.score(), 0)
        manifest, groups, rows = self.outputs()
        self.assertEqual(manifest["warmup_outcome_counts"]["TIMEOUT"], 1)
        self.assertEqual(groups["KEYWORD"]["success_rate"]["value"], 1)
        self.assertEqual(len(rows), 16)

    def test_timeout_on_negatives_cannot_be_correct_refusal(self):
        def handler(case, body, response, latency):
            return HttpOutcome("TIMEOUT", 5, error={"code": "TIMEOUT"}) if case["type"] == "N" else HttpOutcome("SUCCEEDED", latency, 200, response)
        self.assertEqual(self.make_run(handler), 1)
        self.assertEqual(self.score(), 1)
        _, groups, rows = self.outputs()
        group = groups["KEYWORD"]
        self.assertEqual(group["success_rate"]["value"], self.positive_count/16)
        self.assertEqual(group["latency_success_seconds"]["p95"], 1)
        self.assertGreater(group["latency_all_recorded_attempts_seconds"]["p95"], 1)
        metric = group["quality"]["negative_no_evidence_rate"]
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["expected_count"], self.negative_count)
        self.assertEqual(metric["full_set_bounds"], [0, 1])
        self.assertTrue(all(r["score_status"] == "EXECUTION_FAILED" for r in rows if r["case_type"] == "N"))

    def test_partial_contract_abort_retains_all_planned_cases(self):
        private = next(c for c in self.snapshot["chunks"] if self.snapshot["documents"][4]["documentId"] == c["documentId"])
        def handler(case, body, response, latency):
            response.update(noEvidence=False, results=[{"chunkId": private["chunkId"], "documentId": private["documentId"], "documentVersion": 1, "score": .1}])
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        self.assertEqual(self.make_run(handler), 2)
        self.assertEqual(self.score(), 1)
        _, groups, rows = self.outputs()
        self.assertEqual(len(rows), 16)
        self.assertEqual(groups["KEYWORD"]["execution_status_counts"], {"CONTRACT_ERROR": 1, "NOT_EXECUTED": 15})
        self.assertFalse(groups["KEYWORD"]["quality_complete"])

    def test_insufficient_requested_topk_is_na_but_short_valid_response_is_scored(self):
        self.assertEqual(self.make_run(top_k=5), 0)
        self.assertEqual(self.score(), 1)
        _, groups, rows = self.outputs()
        self.assertEqual(groups["KEYWORD"]["quality"]["recall_at_5"]["value"], 1)
        self.assertIsNone(groups["KEYWORD"]["quality"]["mrr_at_10"]["value"])
        self.assertTrue(all(r["unavailable"]["mrr_at_10"] == "REQUEST_TOPK_TOO_SMALL" for r in rows if r["case_type"] != "N"))

    def test_duplicate_results_are_audited_not_compressed(self):
        def handler(case, body, response, latency):
            if response["results"]:
                response["results"] = response["results"][:1]*3
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        self.assertEqual(self.make_run(handler), 0)
        self.assertEqual(self.score(), 0)
        _, groups, rows = self.outputs()
        self.assertEqual(groups["KEYWORD"]["duplicate_result_executions"], self.positive_count)
        self.assertTrue(all(0 <= r["metrics"]["ndcg_at_10"] <= 1 for r in rows if r["case_type"] != "N"))
        self.assertTrue(all(r["duplicate_count"] == 2 for r in rows if r["case_type"] != "N"))

    def test_unverified_vector_empty_is_not_a_successful_negative(self):
        def handler(case, body, response, latency):
            if case["type"] == "N":
                response["meta"] = {"vectorHits": 0, "keywordHits": 0}
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        self.assertEqual(self.make_run(handler, modes=("VECTOR",)), 0)
        self.assertEqual(self.score(), 1)
        _, groups, rows = self.outputs()
        self.assertEqual(groups["VECTOR"]["success_rate"]["value"], 1)
        self.assertIsNone(groups["VECTOR"]["quality"]["negative_no_evidence_rate"]["value"])
        self.assertEqual(groups["VECTOR"]["unverified_no_evidence_executions"], self.negative_count)
        self.assertTrue(all(r["no_evidence_reason"] == "VECTOR_EMPTY_HEALTH_UNVERIFIED" for r in rows if r["case_type"] == "N"))

    def test_degradation_is_reported_not_inferred_as_definite_outage(self):
        def handler(case, body, response, latency):
            response["meta"]["degraded"] = "keyword channel empty"
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        self.assertEqual(self.make_run(handler, modes=("HYBRID",)), 0)
        self.assertEqual(self.score(), 1)
        _, groups, _ = self.outputs()
        self.assertEqual(groups["HYBRID"]["reported_degradation_rate"]["value"], 1)
        self.assertEqual(groups["HYBRID"]["quality"]["recall_at_5"]["value"], 1)
        self.assertIsNone(groups["HYBRID"]["quality"]["negative_no_evidence_rate"]["value"])

    def test_scoring_exception_and_nonfinite_score_differ_from_execution_failure(self):
        self.assertEqual(self.make_run(), 0)
        for i, function in enumerate((lambda *a: (_ for _ in ()).throw(RuntimeError("private details")), lambda *a: float("nan"))):
            with patch.dict(score_eval.RANK_METRICS, {"recall_at_5": (5, function)}):
                self.assertEqual(self.score(f"bad-{i}"), 1)
            _, groups, rows = self.outputs(f"bad-{i}")
            self.assertEqual(groups["KEYWORD"]["success_rate"]["value"], 1)
            self.assertEqual(groups["KEYWORD"]["score_status_counts"]["SCORING_ERROR"], self.positive_count)
            self.assertTrue(all(r["execution_status"] == "SUCCEEDED" for r in rows))
            self.assertNotIn("private details", (self.run_dir / f"scores/bad-{i}/scores.jsonl").read_text())

    def test_existing_score_batch_is_never_overwritten_and_can_rescore(self):
        self.assertEqual(self.make_run(), 0)
        self.assertEqual(self.score(), 0)
        original = (self.run_dir / "scores/v1/scores.jsonl").read_bytes()
        self.assertEqual(self.score(), 2)
        self.assertEqual(original, (self.run_dir / "scores/v1/scores.jsonl").read_bytes())
        self.assertEqual(self.score("v2"), 0)

    def test_changed_snapshot_bytes_abort_before_publication(self):
        self.assertEqual(self.make_run(), 0)
        self.snapshot_path.write_bytes(self.snapshot_path.read_bytes()+b"\n")
        self.assertEqual(self.score(), 2)
        self.assertFalse((self.run_dir / "scores").exists())

    def test_corrupt_duplicate_reordered_or_changed_gold_records_abort(self):
        self.assertEqual(self.make_run(), 0)
        source = (self.run_dir / "executions.jsonl").read_bytes()
        mutations = [lambda rows: rows[0].update(relevant_chunk_ids=[]),
                     lambda rows: rows[0]["request"].update(topK=5),
                     lambda rows: rows.__setitem__(1, deepcopy(rows[0])),
                     lambda rows: rows.reverse(),
                     lambda rows: rows[0]["outcome"].update(latency_seconds=-1),
                     lambda rows: rows[0]["outcome"]["body"].update(noEvidence="true"),
                     lambda rows: rows[0]["outcome"]["body"]["results"][0].update(chunkId="foreign")]
        for mutation in mutations:
            (self.run_dir / "executions.jsonl").write_bytes(source)
            self.edit_records(mutation)
            self.assertEqual(self.score(), 2)
            self.assertFalse((self.run_dir / "scores").exists())

    def test_running_and_false_completed_source_are_rejected(self):
        self.assertEqual(self.make_run(), 0)
        path = self.run_dir / "manifest.json"
        original = json.loads(path.read_text())
        for mutation in (lambda m: m.update(status="RUNNING"), lambda m: m.update(recorded_executions=99),
                         lambda m: m["dataset"].update(cases_sha256="changed")):
            changed = deepcopy(original)
            mutation(changed)
            path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertEqual(self.score(), 2)

    def test_source_change_during_scoring_aborts_before_publication(self):
        self.assertEqual(self.make_run(), 0)
        original_function = score_eval.score_case
        calls = []
        def changing(*a, **kw):
            if not calls:
                path = self.run_dir / "manifest.json"
                path.write_bytes(path.read_bytes()+b"\n")
            calls.append(1)
            return original_function(*a, **kw)
        with patch.object(score_eval, "score_case", side_effect=changing):
            self.assertEqual(self.score(), 2)
        self.assertFalse((self.run_dir / "scores").exists())

    def test_io_failure_cannot_publish_completed_manifest(self):
        self.assertEqual(self.make_run(), 0)
        with patch.object(score_eval.os, "replace", side_effect=OSError("disk unavailable")):
            self.assertEqual(self.score(), 2)
        manifest = json.loads((self.run_dir / "scores/v1/manifest.json").read_text())
        self.assertEqual(manifest["status"], "PREPARING")

    def test_unrecorded_inflight_measure_attempt_is_distinct_from_not_executed(self):
        self.assertEqual(self.make_run(), 0)
        self.edit_records(lambda rows: rows.pop())
        path = self.run_dir / "manifest.json"
        manifest = json.loads(path.read_text())
        last_case = self.dataset.cases[-1]
        manifest.update(status="FAILED", recorded_executions=15, remaining_executions=1, outcome_counts={"SUCCEEDED": 15},
                        in_flight={"sequence": 16, "phase": "MEASURE", "mode": "KEYWORD", "case_id": last_case["case_id"]})
        path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertEqual(self.score(), 1)
        _, groups, rows = self.outputs()
        self.assertEqual(rows[-1]["execution_status"], "UNRECORDED_ATTEMPT")
        self.assertEqual(groups["KEYWORD"]["success_rate"]["denominator"], 16)

    def test_calibration_audit_is_offline_and_does_not_recommend_threshold(self):
        output = io.StringIO()
        with patch("urllib.request.build_opener") as opener, redirect_stdout(output):
            self.assertEqual(calibrate_threshold.main([]), 2)
            self.assertEqual(calibrate_threshold.main(["--audit"]), 0)
        opener.assert_not_called()
        self.assertIn("BLOCKED_POST_GATE_DATA", output.getvalue())
        self.assertNotIn("推荐阈值：", output.getvalue())


if __name__ == "__main__":
    unittest.main()
