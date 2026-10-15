"""第五项：真实CLI报告的离线集成测试；HTTP/语料快照为替身。"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_eval_metrics as fixtures
from eval import report_eval, score_eval, run_eval
from eval.corpus_map import build_snapshot, prepare_cases, chunks_from_snapshot
from eval.dataset import load_dataset, sha256_file
from eval.http_client import HttpOutcome


class ReportTests(unittest.TestCase):
    def setUp(self):
        fixtures.OfflineScoringTests.setUp(self)
        self.home = self.root
        self.original_snapshot = deepcopy(self.snapshot)
        self.original_dataset = fixtures.DATASET
        self.output = self.home / "reports"
        self.batch_dirs = []

    def make_batch(self, name, handler=None, *, modes=("KEYWORD",), warmup=False, top_k=10, snapshot=None, dataset=None, scoring_error=False):
        self.root = self.home / name
        self.root.mkdir()
        dataset = dataset or self.original_dataset
        self.dataset = load_dataset(dataset)
        self.snapshot = deepcopy(snapshot or self.original_snapshot)
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_path.write_text(json.dumps(self.snapshot, ensure_ascii=False), encoding="utf-8")
        pos, neg = prepare_cases(self.dataset.cases, chunks_from_snapshot(self.snapshot), dataset=self.dataset)
        self.cases = {c["case_id"]: c for c in pos+neg}
        self.by_input = {(c["query"], json.dumps(c["request"], sort_keys=True)): c for c in pos+neg}
        self.chunk_map = {c["chunkId"]: c for c in self.snapshot["chunks"]}
        self.calls = []
        self.run_dir = self.root / "runs/source"
        with patch.object(fixtures, "DATASET", dataset):
            fixtures.OfflineScoringTests.make_run(self, handler, modes=modes, warmup=warmup, top_k=top_k)
        with redirect_stdout(io.StringIO()):
            if scoring_error:
                def broken(*a):
                    raise RuntimeError("private judge details")
                with patch.dict(score_eval.RANK_METRICS, {"recall_at_5": (5, broken)}):
                    status = score_eval.main(["--run-dir", str(self.run_dir), "--score-id", "v1"])
            else:
                status = score_eval.main(["--run-dir", str(self.run_dir), "--score-id", "v1"])
        self.assertIn(status, (0, 1))
        path = self.run_dir / "scores/v1"
        self.batch_dirs.append(path)
        return path

    def report(self, candidate, baseline=None, extra=(), report_id="test"):
        args = ["--score-dir", str(candidate), "--output-dir", str(self.output), "--report-id", report_id, *extra]
        if baseline:
            args += ["--baseline-score-dir", str(baseline)]
        with patch.dict(os.environ, {}, clear=True), patch.object(run_eval.SearchClient, "request") as request, \
                patch("urllib.request.build_opener") as opener, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = report_eval.main(args)
        request.assert_not_called()
        opener.assert_not_called()
        return status

    def outputs(self, report_id="test"):
        path = self.output / report_id
        return (json.loads((path / "manifest.json").read_text(encoding="utf-8")),
                json.loads((path / "summary.json").read_text(encoding="utf-8")),
                (path / "report.md").read_text(encoding="utf-8"))

    def change_score(self, path, name, change, *, update_hash=True):
        file = path / name
        if name == "scores.jsonl":
            value = [json.loads(line) for line in file.read_text().splitlines()]
            change(value)
            file.write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in value), encoding="utf-8")
        else:
            value = json.loads(file.read_text())
            change(value)
            file.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        if update_hash and name != "manifest.json":
            manifest_file = path / "manifest.json"
            manifest = json.loads(manifest_file.read_text())
            manifest["artifact_sha256"][name] = sha256_file(file)
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    def change_source(self, batch, change):
        file = batch.parent.parent / "manifest.json"
        manifest = json.loads(file.read_text())
        change(manifest)
        file.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.change_score(batch, "manifest.json", lambda m: m["source_sha256"].update({"manifest.json": sha256_file(file)}))

    def alternative_dataset(self, *, remove_case=False):
        path = self.home / "alternative/manifest.json"
        path.parent.mkdir()
        manifest = deepcopy(load_dataset(self.original_dataset).manifest)
        manifest["description"] += " alternative"
        for entry in manifest["corpus"]:
            entry["file"] = str((self.original_dataset.parent / entry["file"]).resolve())
        rows = deepcopy(load_dataset(self.original_dataset).cases)
        if remove_case:
            rows.pop()
        file = path.parent / manifest["case_file"]
        file.write_text("".join(json.dumps(row, ensure_ascii=False)+"\n" for row in rows), encoding="utf-8")
        manifest.update(cases_sha256=sha256_file(file), case_count=len(rows))
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    def test_single_report_is_offline_immutable_and_has_all_cases(self):
        batch = self.make_batch("candidate")
        inputs = {p: p.read_bytes() for p in batch.parent.parent.rglob("*") if p.is_file()}
        self.assertEqual(self.report(batch), 0)
        manifest, summary, text = self.outputs()
        self.assertEqual(manifest["status"], "COMPLETED")
        self.assertFalse(manifest["release_gate_evaluated"])
        self.assertEqual(summary["kind"], "SINGLE_RUN")
        self.assertEqual(len(summary["cases"]), 16)
        self.assertEqual(summary["by_mode"]["KEYWORD"]["overall"]["quality"]["recall_at_5"]["value"], 1)
        self.assertIn("development", text)
        self.assertTrue(summary["by_mode"]["KEYWORD"]["by_tag"])
        self.assertEqual(summary["cases"][0]["first_relevant_rank"], 1)
        self.assertEqual(manifest["artifact_sha256"]["report.md"], sha256_file(self.output / "test/report.md"))
        self.assertTrue(all(p.read_bytes() == raw for p, raw in inputs.items()))

    def test_multimode_single_report_does_not_pool_modes(self):
        batch = self.make_batch("candidate", modes=run_eval.MODES, warmup=True)
        self.assertEqual(self.report(batch), 0)
        _, summary, _ = self.outputs()
        self.assertEqual(len(summary["cases"]), 48)
        self.assertEqual(summary["candidate"]["excluded_warmup_executions"], 48)
        self.assertEqual(set(summary["by_mode"]), set(run_eval.MODES))
        self.assertTrue(all(g["overall"]["expected_count"] == 16 for g in summary["by_mode"].values()))
        self.assertTrue(all(g["overall"]["latency_success_seconds"]["p95"] == 1 for g in summary["by_mode"].values()))

    def test_single_selects_explicit_mode(self):
        batch = self.make_batch("candidate", modes=run_eval.MODES)
        self.assertEqual(self.report(batch, extra=("--mode", "VECTOR")), 0)
        self.assertEqual(set(self.outputs()[1]["by_mode"]), {"VECTOR"})
        self.assertEqual(self.report(batch, extra=("--mode", "HYBRID"), report_id="other"), 0)

    def test_unknown_mode_cannot_be_selected(self):
        batch = self.make_batch("candidate")
        self.assertEqual(self.report(batch, extra=("--mode", "VECTOR")), 2)
        self.assertFalse(self.output.exists())

    def test_repeated_report_is_never_overwritten_but_new_id_allowed(self):
        batch = self.make_batch("candidate")
        self.assertEqual(self.report(batch), 0)
        before = (self.output / "test/report.md").read_bytes()
        self.assertEqual(self.report(batch), 2)
        self.assertEqual((self.output / "test/report.md").read_bytes(), before)
        self.assertEqual(self.report(batch, report_id="again"), 0)

    def test_same_execution_comparison_warns_not_independent(self):
        batch = self.make_batch("candidate")
        self.assertEqual(self.report(batch, batch), 0)
        _, summary, text = self.outputs()
        self.assertIn("SAME_EXECUTION_NOT_AN_INDEPENDENT_EXPERIMENT", summary["compatibility"]["warnings"])
        self.assertEqual(summary["case_change_counts"], {"UNCHANGED": 16})
        self.assertEqual(summary["overall"]["quality_deltas"]["recall_at_5"]["delta"], 0)
        self.assertIn("不输出单一总分", text)

    def test_improvements_and_regressions_are_paired_by_case(self):
        def missing(case, body, response, latency):
            if case["case_id"] == "BOUNDARY-001":
                response.update(results=[], noEvidence=True)
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        a = self.make_batch("baseline", missing)
        b = self.make_batch("candidate")
        self.assertEqual(self.report(b, a), 0)
        summary = self.outputs()[1]
        row = next(r for r in summary["cases"] if r["case_id"] == "BOUNDARY-001")
        self.assertEqual(row["quality_change"], "IMPROVED")
        self.assertEqual(row["metrics"]["recall_at_5"]["delta"], 1)
        self.assertEqual(row["metrics"]["positive_no_evidence_rate"]["delta"], -1)
        self.assertEqual(row["metrics"]["positive_no_evidence_rate"]["change"], "IMPROVED")
        self.assertEqual(self.report(a, b, report_id="reverse"), 0)
        self.assertEqual(next(r for r in self.outputs("reverse")[1]["cases"] if r["case_id"] == "BOUNDARY-001")["quality_change"], "REGRESSED")

    def test_negative_refusal_improvement_direction_is_higher(self):
        case_id = next(c["case_id"] for c in self.cases.values() if c["type"] == "N" and any(
            chunk["documentId"] not in set(c["_scope_forbidden"]) | set(c["expected"]["forbidden_document_ids"])
            for chunk in self.snapshot["chunks"]))
        def false_evidence(case, body, response, latency):
            if case["case_id"] == case_id:
                public = next(c for c in self.snapshot["chunks"] if c["documentId"] not in
                              set(case["_scope_forbidden"]) | set(case["expected"]["forbidden_document_ids"]))
                response.update(noEvidence=False, results=[{"chunkId": public["chunkId"], "documentId": public["documentId"],
                                                          "documentVersion": 1, "score": .2}])
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        a = self.make_batch("baseline", false_evidence)
        b = self.make_batch("candidate")
        self.assertEqual(self.report(b, a), 0)
        row = next(r for r in self.outputs()[1]["cases"] if r["case_id"] == case_id)
        self.assertEqual(row["metrics"]["negative_no_evidence_rate"]["change"], "IMPROVED")
        self.assertEqual(row["metrics"]["mrr_at_10"]["change"], "NOT_APPLICABLE")

    def test_failures_unknowns_keep_complete_denominator_not_zero(self):
        def timeout(case, body, response, latency):
            return HttpOutcome("TIMEOUT", 5, error={"code": "TIMEOUT"}) if case["type"] == "N" else HttpOutcome("SUCCEEDED", latency, 200, response)
        a = self.make_batch("baseline")
        b = self.make_batch("candidate", timeout)
        self.assertEqual(self.report(b, a), 1)
        summary = self.outputs()[1]
        value = summary["overall"]["quality_deltas"]["negative_no_evidence_rate"]
        self.assertIsNone(value["candidate"])
        self.assertIsNone(value["delta"])
        self.assertEqual(value["change"], "UNKNOWN")
        self.assertEqual(value["candidate_coverage"]["expected_count"], 7)
        self.assertEqual(summary["case_change_counts"]["INCOMPLETE"], 7)
        self.assertEqual(summary["engineering_change_counts"]["LOST_SUCCESS"], 7)
        self.assertIn("INCOMPLETE_OR_DIFFERENT_SUCCESS_COHORT", summary["compatibility"]["latency_blockers"])
        self.assertIsNone(summary["latency_deltas_descriptive_only"]["latency_success_seconds"]["p95"]["delta"])
        self.assertEqual(self.report(a, b, report_id="recovery"), 1)
        recovered = self.outputs("recovery")[1]
        self.assertEqual(recovered["engineering_change_counts"]["RECOVERED"], 7)
        self.assertEqual(recovered["case_change_counts"]["INCOMPLETE"], 7)
        self.assertNotIn("IMPROVED", recovered["case_change_counts"])

    def test_vector_empty_health_unknown_is_not_quality_improvement(self):
        def ambiguous(case, body, response, latency):
            if case["type"] == "N":
                response["meta"] = {}
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        a = self.make_batch("baseline", modes=("VECTOR",))
        b = self.make_batch("candidate", ambiguous, modes=("VECTOR",))
        self.assertEqual(self.report(b, a), 1)
        summary = self.outputs()[1]
        self.assertEqual(summary["overall"]["candidate"]["success_rate"]["value"], 1)
        self.assertEqual(summary["overall"]["quality_deltas"]["negative_no_evidence_rate"]["change"], "UNKNOWN")
        self.assertEqual(summary["case_change_counts"]["INCOMPLETE"], 7)

    def test_partial_contract_abort_not_excluded_from_report(self):
        def forbidden(case, body, response, latency):
            private = self.snapshot["chunks"][4]
            response.update(noEvidence=False, results=[{"chunkId": private["chunkId"], "documentId": private["documentId"], "documentVersion": 1, "score": .1}])
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        batch = self.make_batch("candidate", forbidden)
        self.assertEqual(self.report(batch), 1)
        summary = self.outputs()[1]
        self.assertEqual(len(summary["cases"]), 16)
        counts = summary["by_mode"]["KEYWORD"]["overall"]["execution_status_counts"]
        self.assertEqual(counts, {"CONTRACT_ERROR": 1, "NOT_EXECUTED": 15})

    def test_scoring_error_preserved_not_replaced_with_recomputed_success(self):
        batch = self.make_batch("candidate", scoring_error=True)
        self.assertEqual(self.report(batch), 1)
        group = self.outputs()[1]["by_mode"]["KEYWORD"]["overall"]
        self.assertEqual(group["score_status_counts"]["SCORING_ERROR"], 9)
        self.assertIsNone(group["quality"]["recall_at_5"]["value"])
        self.assertEqual(group["success_rate"]["value"], 1)

    def test_topk_difference_warns_and_only_known_metrics_compare(self):
        a = self.make_batch("baseline", top_k=10)
        b = self.make_batch("candidate", top_k=5)
        self.assertEqual(self.report(b, a), 1)
        summary = self.outputs()[1]
        self.assertTrue(summary["compatibility"]["recorded_inputs_comparable"])
        self.assertIn("REQUEST_TOPK_CHANGED_CHECK_EACH_METRIC_COVERAGE", summary["compatibility"]["warnings"])
        self.assertEqual(summary["overall"]["quality_deltas"]["recall_at_5"]["delta"], 0)
        self.assertIsNone(summary["overall"]["quality_deltas"]["mrr_at_10"]["delta"])

    def test_different_dataset_blocks_deltas_even_if_numbers_equal(self):
        a = self.make_batch("baseline")
        b = self.make_batch("candidate", dataset=self.alternative_dataset())
        self.assertEqual(self.report(b, a), 1)
        summary = self.outputs()[1]
        self.assertIn("DATASET_CHANGED", summary["compatibility"]["blockers"])
        self.assertIsNone(summary["overall"]["quality_deltas"]["recall_at_5"]["delta"])
        self.assertEqual(summary["case_change_counts"], {"BLOCKED": 16})

    def test_added_or_removed_case_is_unpaired_not_silently_dropped(self):
        a = self.make_batch("baseline")
        b = self.make_batch("candidate", dataset=self.alternative_dataset(remove_case=True))
        self.assertEqual(self.report(b, a), 1)
        summary = self.outputs()[1]
        self.assertEqual(len(summary["cases"]), 16)
        self.assertEqual(summary["case_change_counts"]["UNPAIRED"], 1)

    def test_rechunking_or_id_change_blocks_comparison(self):
        a = self.make_batch("baseline")
        chunks = deepcopy(self.original_snapshot["chunks"])
        for chunk in chunks:
            chunk["chunkId"] += "-new"
        snapshot = build_snapshot(self.original_snapshot["documents"], chunks)
        b = self.make_batch("candidate", snapshot=snapshot)
        self.assertEqual(self.report(b, a), 1)
        policy = self.outputs()[1]["compatibility"]
        self.assertIn("CORPUS_OR_CHUNK_IDENTITY_CHANGED", policy["blockers"])
        self.assertIn("GOLD_CHUNKS_CHANGED", policy["blockers"])

    def test_same_corpus_reexport_timestamp_is_not_quality_blocker(self):
        a = self.make_batch("baseline")
        snapshot = build_snapshot(self.original_snapshot["documents"], self.original_snapshot["chunks"])
        b = self.make_batch("candidate", snapshot=snapshot)
        self.assertEqual(self.report(b, a), 0)
        policy = self.outputs()[1]["compatibility"]
        self.assertTrue(policy["recorded_inputs_comparable"])
        self.assertIn("SNAPSHOT_EXPORT_ID_CHANGED", policy["warnings"])

    def test_index_change_allowed_with_identical_text_identity_and_gold(self):
        a = self.make_batch("baseline")
        chunks = deepcopy(self.original_snapshot["chunks"])
        for chunk in chunks:
            chunk.update(embeddingModel="candidate-model", indexVersion="v2", hasEmbedding=True)
        snapshot = build_snapshot(self.original_snapshot["documents"], chunks)
        b = self.make_batch("candidate", snapshot=snapshot)
        self.assertEqual(self.report(b, a), 0)
        policy = self.outputs()[1]["compatibility"]
        self.assertTrue(policy["recorded_inputs_comparable"])
        self.assertIn("SNAPSHOT_CONTENT_OR_INDEX_METADATA_CHANGED", policy["warnings"])

    def test_runner_change_blocks_fair_comparison(self):
        a = self.make_batch("baseline")
        b = self.make_batch("candidate")
        self.change_source(b, lambda m: m["runner_source_sha256"].update({"run_eval.py": "0"*64}))
        self.assertEqual(self.report(b, a), 1)
        self.assertIn("RUNNER_CHANGED", self.outputs()[1]["compatibility"]["blockers"])

    def test_warmup_change_blocks_latency_not_quality(self):
        a = self.make_batch("baseline")
        b = self.make_batch("candidate", warmup=True)
        self.assertEqual(self.report(b, a), 0)
        policy = self.outputs()[1]["compatibility"]
        self.assertTrue(policy["recorded_inputs_comparable"])
        self.assertIn("WARMUP_CHANGED", policy["latency_blockers"])
        self.assertIsNone(self.outputs()[1]["latency_deltas_descriptive_only"]["latency_success_seconds"]["p50"]["delta"])

    def test_failed_warmup_blocks_latency_without_penalizing_formal_quality(self):
        def first_timeout(case, body, response, latency):
            return HttpOutcome("TIMEOUT", 5, error={"code": "TIMEOUT"}) if len(self.calls) == 1 else HttpOutcome("SUCCEEDED", latency, 200, response)
        a = self.make_batch("baseline", warmup=True)
        b = self.make_batch("candidate", first_timeout, warmup=True)
        self.assertEqual(self.report(b, a), 0)
        summary = self.outputs()[1]
        self.assertEqual(summary["candidate"]["warmup_outcome_counts"]["TIMEOUT"], 1)
        self.assertEqual(summary["overall"]["candidate"]["success_rate"]["value"], 1)
        self.assertIn("WARMUP_FAILURE_CACHE_STATE_UNVERIFIED", summary["compatibility"]["latency_blockers"])

    def test_timeout_configuration_change_blocks_latency(self):
        a = self.make_batch("baseline")
        b = self.make_batch("candidate")
        self.change_source(b, lambda m: m["execution"].update(timeout_seconds=30))
        self.assertEqual(self.report(b, a), 0)
        self.assertIn("TIMEOUT_CHANGED", self.outputs()[1]["compatibility"]["latency_blockers"])

    def test_multiple_metrics_can_move_in_opposite_directions(self):
        case = {"case_id": "test", "type": "K", "tags": ["test"], "_relevant": {"a", "b"}}
        def row(ranked):
            return score_eval.score_case(case, {"sequence": 1, "outcome": {"status": "SUCCEEDED", "latency_seconds": 1,
                "body": {"results": [{"chunkId": c} for c in ranked], "noEvidence": False}}}, "KEYWORD", 10)
        value = report_eval.paired_change(row(["a", "x", "x", "x", "x", "b"]), row(["x", "a", "b"]), False)
        self.assertEqual(value["quality_change"], "MIXED")
        self.assertEqual(value["metrics"]["recall_at_5"]["change"], "IMPROVED")
        self.assertEqual(value["metrics"]["mrr_at_10"]["change"], "REGRESSED")

    def test_version_change_blocks_quality_even_with_same_ids_and_scores(self):
        a = self.make_batch("baseline")
        docs, chunks = deepcopy(self.original_snapshot["documents"]), deepcopy(self.original_snapshot["chunks"])
        for doc in docs:
            doc["version"] = 2
        for chunk in chunks:
            chunk["version"] = 2
        def correct_version(case, body, response, latency):
            for result in response["results"]:
                result["documentVersion"] = 2
            return HttpOutcome("SUCCEEDED", latency, 200, response)
        b = self.make_batch("candidate", correct_version, snapshot=build_snapshot(docs, chunks))
        self.assertEqual(self.report(b, a), 1)
        self.assertIn("CORPUS_OR_CHUNK_IDENTITY_CHANGED", self.outputs()[1]["compatibility"]["blockers"])

    def test_relocated_dataset_is_verified_by_original_hash(self):
        dataset = self.alternative_dataset()
        batch = self.make_batch("candidate", dataset=dataset)
        moved = self.home / "moved-dataset"
        shutil.move(dataset.parent, moved)
        self.assertEqual(self.report(batch), 2)
        self.assertEqual(self.report(batch, extra=("--dataset", str(moved / "manifest.json"))), 0)
        (moved / "cases.jsonl").write_bytes((moved / "cases.jsonl").read_bytes()+b"\n")
        self.assertEqual(self.report(batch, extra=("--dataset", str(moved / "manifest.json")), report_id="changed"), 2)

    def test_mode_order_cache_context_blocks_latency(self):
        batch = self.make_batch("candidate", modes=run_eval.MODES)
        self.assertEqual(self.report(batch, batch, extra=("--mode", "HYBRID", "--baseline-mode", "VECTOR")), 0)
        summary = self.outputs()[1]
        self.assertIn("MODE_ORDER_OR_CACHE_CONTEXT_CHANGED", summary["compatibility"]["latency_blockers"])
        self.assertTrue(summary["compatibility"]["recorded_inputs_comparable"])

    def test_multi_mode_comparison_requires_explicit_selection(self):
        batch = self.make_batch("candidate", modes=run_eval.MODES)
        self.assertEqual(self.report(batch, batch), 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.report(batch, batch, extra=("--mode", "KEYWORD", "--baseline-mode", "KEYWORD")), 0)

    def test_score_hash_and_recomputed_values_reject_tampering(self):
        batch = self.make_batch("candidate")
        self.change_score(batch, "scores.jsonl", lambda rows: rows[0]["metrics"].update(recall_at_5=.5), update_hash=False)
        self.assertEqual(self.report(batch), 2)
        self.change_score(batch, "scores.jsonl", lambda rows: None)
        self.assertEqual(self.report(batch), 2)
        self.assertFalse(self.output.exists())

    def test_metrics_aggregate_and_manifest_are_not_trusted_blindly(self):
        batch = self.make_batch("candidate")
        self.change_score(batch, "metrics.json", lambda m: m["by_mode"]["KEYWORD"]["quality"]["recall_at_5"].update(value=.5))
        self.assertEqual(self.report(batch), 2)
        self.assertFalse(self.output.exists())

    def test_duplicate_reordered_and_missing_score_rows_rejected(self):
        batch = self.make_batch("candidate")
        original = (batch / "scores.jsonl").read_bytes()
        for mutate in (lambda rows: rows.pop(), lambda rows: rows.reverse(), lambda rows: rows.__setitem__(1, deepcopy(rows[0]))):
            (batch / "scores.jsonl").write_bytes(original)
            self.change_score(batch, "scores.jsonl", mutate)
            self.assertEqual(self.report(batch), 2)
        self.assertFalse(self.output.exists())

    def test_running_old_policy_changed_code_and_wrong_split_batches_rejected(self):
        batch = self.make_batch("candidate")
        file = batch / "manifest.json"
        original = file.read_bytes()
        for mutate in (lambda m: m.update(status="PREPARING"), lambda m: m.update(metric_policy_version="old"),
                       lambda m: m["scorer_source_sha256"].update({"metrics.py": "0"*64}),
                       lambda m: m.update(dataset_split="acceptance"), lambda m: m.update(expected_measure_executions=15)):
            file.write_bytes(original)
            self.change_score(batch, "manifest.json", mutate)
            self.assertEqual(self.report(batch), 2)
        self.assertFalse(self.output.exists())

    def test_changed_source_or_snapshot_blocks_report(self):
        batch = self.make_batch("candidate")
        file = batch.parent.parent / "executions.jsonl"
        file.write_bytes(file.read_bytes()+b"\n")
        self.assertEqual(self.report(batch), 2)
        self.assertFalse(self.output.exists())

    def test_relocated_original_dataset_and_snapshot_need_identical_hashes(self):
        batch = self.make_batch("candidate")
        # 快照可以搬迁；题库与语料路径不改，仍须校验所有原文件。
        original = self.snapshot_path
        moved = self.home / "moved.json"
        shutil.move(original, moved)
        self.assertEqual(self.report(batch), 2)
        self.assertEqual(self.report(batch, extra=("--snapshot", str(moved))), 0)
        moved.write_bytes(moved.read_bytes()+b"\n")
        self.assertEqual(self.report(batch, extra=("--snapshot", str(moved)), report_id="bad"), 2)

    def test_write_failure_leaves_preparing_not_completed(self):
        batch = self.make_batch("candidate")
        with patch.object(report_eval.os, "replace", side_effect=OSError("disk unavailable")):
            self.assertEqual(self.report(batch), 2)
        self.assertEqual(json.loads((self.output / "test/manifest.json").read_text())["status"], "PREPARING")

    def test_input_change_while_rendering_cannot_publish_completed_report(self):
        batch = self.make_batch("candidate")
        original = report_eval.render_report
        def changed(summary):
            file = batch / "scores.jsonl"
            file.write_bytes(file.read_bytes()+b"\n")
            return original(summary)
        with patch.object(report_eval, "render_report", side_effect=changed):
            self.assertEqual(self.report(batch), 2)
        self.assertEqual(json.loads((self.output / "test/manifest.json").read_text())["status"], "PREPARING")

    def test_reports_cannot_be_written_inside_source_run(self):
        batch = self.make_batch("candidate")
        self.assertEqual(self.report(batch, extra=("--output-dir", str(batch.parent.parent))), 2)
        self.assertFalse((batch.parent.parent / "test").exists())

    def test_credentials_are_redacted_and_markdown_data_is_escaped(self):
        batch = self.make_batch("candidate")
        self.change_source(batch, lambda m: m["target"].update(declared_config={"api_key": "private-key", "description": "private-key"}, key_env="CUSTOM_KEY"))
        with patch.dict(os.environ, {"CUSTOM_KEY": "private-key"}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(report_eval.main(["--score-dir", str(batch), "--output-dir", str(self.output), "--report-id", "safe",
                                               "--label", "<script>[x](javascript:1)| private-key"]), 0)
        _, summary, text = self.outputs("safe")
        self.assertNotIn("private-key", json.dumps(summary))
        self.assertNotIn("private-key", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("\\[x\\]", text)

    def test_redaction_cannot_corrupt_report_identity_or_hashes(self):
        batch = self.make_batch("candidate")
        with patch.dict(os.environ, {"KNOWLEDGE_QUERY_KEY": "retrieval-report-v1.0.0"}, clear=True), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(report_eval.main(["--score-dir", str(batch), "--output-dir", str(self.output),
                                               "--report-id", "unsafe"]), 2)
        self.assertFalse(self.output.exists())

    def test_invalid_id_and_orphan_baseline_flags_do_not_publish(self):
        batch = self.make_batch("candidate")
        self.assertEqual(self.report(batch, report_id="../unsafe"), 2)
        self.assertEqual(self.report(batch, extra=("--baseline-mode", "VECTOR")), 2)
        self.assertFalse(self.output.exists())

    def test_nonfinite_exponent_duplicate_json_and_false_boolean_count_rejected(self):
        batch = self.make_batch("candidate")
        file = batch / "manifest.json"
        original = file.read_bytes()
        file.write_bytes(original.replace(b'"expected_measure_executions": 16', b'"expected_measure_executions": 1e999'))
        self.assertEqual(self.report(batch), 2)
        file.write_bytes(original.replace(b'"expected_measure_executions": 16', b'"expected_measure_executions": true'))
        self.assertEqual(self.report(batch), 2)
        file.write_bytes(original.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'))
        self.assertEqual(self.report(batch), 2)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
