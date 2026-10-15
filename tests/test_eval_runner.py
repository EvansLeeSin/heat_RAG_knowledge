"""第三项：实际CLI/HTTP适配的离线测试。传输层为mock，不访问业务服务。"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from eval import run_eval, load_corpus
from eval.corpus_map import build_snapshot, prepare_cases, canonical_hash, content_hash
from eval.dataset import load_dataset, sha256_file, source_text
from eval.http_client import ConfigurationError, NoRedirectHandler, Redactor, SearchClient

ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT / "eval/datasets/boundaries-v1/manifest.json"
KEY = "test-only-evaluation-secret-7391"


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = load_dataset(DATASET_PATH)
        docs, chunks = [], []
        for i, entry in enumerate(self.dataset.manifest["corpus"]):
            metadata = {**deepcopy(entry["metadata"]), "version": 1, "sourceContentHash": entry["sha256"]}
            docs.append(metadata)
            chunks.append({"chunkId": f"test-{i}", "documentId": metadata["documentId"], "version": 1,
                           "chunkIndex": 0, "content": source_text(self.dataset.path.parent / entry["file"]),
                           "embeddingModel": "test-model", "embeddingVersion": "e1", "indexVersion": "i1"})
        self.snapshot = build_snapshot(docs, chunks)
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_path.write_text(json.dumps(self.snapshot, ensure_ascii=False), encoding="utf-8")
        expanded = run_eval.chunks_from_snapshot(self.snapshot)
        pos, neg = prepare_cases(self.dataset.cases, expanded, dataset=self.dataset)
        self.by_input = {(c["query"], json.dumps(c["request"], sort_keys=True)): c for c in pos+neg}
        self.requests = []
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def response(self, request_body):
        conditions = {k: request_body[k] for k in ("region", "documentTypes", "effectiveStatus", "authorizedProjectIds")}
        case = self.by_input[(request_body["query"], json.dumps(conditions, sort_keys=True))]
        results = [] if case["type"] == "N" else [{"chunkId": sorted(case["_relevant"])[0],
                   "documentId": case["expected"]["evidence"][0]["document_id"], "documentVersion": 1, "score": 0.12}]
        return {"query": request_body["query"], "results": results, "noEvidence": case["type"] == "N", "meta": {}}

    def invoke(self, handler=None, extra=None, *, env=None, run_id="fixed-run"):
        argv = ["--dataset", str(DATASET_PATH), "--snapshot", str(self.snapshot_path), "--base", "http://test.invalid",
                "--output-dir", str(self.root / "runs"), "--modes", "KEYWORD"]
        if run_id is not None:
            argv += ["--run-id", run_id]
        argv += extra or []

        def transport(req, timeout):
            body = json.loads(req.data)
            self.requests.append((body, timeout, req.get_header("X-api-key")))
            payload = handler(body) if handler else self.response(body)
            if isinstance(payload, dict):
                payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = payload
            return response
        with patch.dict(os.environ, env if env is not None else {"KNOWLEDGE_QUERY_KEY": KEY}, clear=True), \
                patch("urllib.request.build_opener") as opener, redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            opener.return_value.open.side_effect = transport
            return run_eval.main(argv)

    def artifacts(self, run_id="fixed-run"):
        root = self.root / "runs" / run_id
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        records = [json.loads(line) for line in (root / "executions.jsonl").read_text(encoding="utf-8").splitlines()]
        return manifest, records

    def test_success_keeps_every_case_real_payload_and_input_hashes(self):
        self.assertEqual(self.invoke(), 0)
        manifest, records = self.artifacts()
        self.assertEqual(manifest["status"], "COMPLETED")
        self.assertEqual(manifest["dataset"]["manifest_sha256"], sha256_file(DATASET_PATH))
        self.assertEqual(manifest["snapshot"]["file_sha256"], sha256_file(self.snapshot_path))
        self.assertEqual(manifest["snapshot"]["snapshot_id"], self.snapshot["snapshotId"])
        self.assertEqual(manifest["snapshot"]["index_metadata"]["embeddingModel"], ["test-model"])
        self.assertEqual(manifest["outcome_counts"], {"SUCCEEDED": 16})
        self.assertEqual(manifest["remaining_executions"], 0)
        self.assertEqual([r["case_id"] for r in records], [c["case_id"] for c in self.dataset.cases])
        self.assertEqual([r["sequence"] for r in records], list(range(1, 17)))
        for record, (body, timeout, key) in zip(records, self.requests):
            self.assertEqual(body, record["request"])
            self.assertEqual(key, KEY)
            self.assertEqual(timeout, 120)
            self.assertFalse({"expected", "case_id", "relevant_chunk_ids"} & set(body))
            self.assertTrue(math.isfinite(record["outcome"]["latency_seconds"]))
            self.assertGreaterEqual(record["outcome"]["latency_seconds"], 0)
        self.assertEqual(set(p.name for p in (self.root / "runs/fixed-run").iterdir()), {"manifest.json", "executions.jsonl"})

    def test_modes_topk_timeout_and_warmup_are_explicit_and_all_calls_recorded(self):
        self.assertEqual(self.invoke(extra=["--modes", "VECTOR", "HYBRID", "--top-k", "7", "--timeout", "2.5", "--warmup"]), 0)
        manifest, records = self.artifacts()
        self.assertEqual(len(records), 64)
        self.assertEqual([r["phase"] for r in records], ["WARMUP"] * 32 + ["MEASURE"] * 32)
        self.assertEqual([r["mode"] for r in records[:32]], ["VECTOR"] * 16 + ["HYBRID"] * 16)
        self.assertTrue(all(body["topK"] == 7 and timeout == 2.5 for body, timeout, _ in self.requests))
        self.assertEqual(manifest["execution"]["retries"], 0)

    def test_recoverable_failures_are_not_refusals_and_following_cases_continue(self):
        failures = [
            ("TIMEOUT", lambda: (_ for _ in ()).throw(TimeoutError(KEY))),
            ("TIMEOUT", lambda: (_ for _ in ()).throw(URLError(TimeoutError(KEY)))),
            ("NETWORK_ERROR", lambda: (_ for _ in ()).throw(URLError("endpoint " + KEY))),
            ("HTTP_ERROR", lambda: (_ for _ in ()).throw(HTTPError("http://test.invalid", 503, KEY, {}, io.BytesIO(json.dumps({"detail": KEY}).encode())))),
            ("INVALID_RESPONSE", lambda: b"not-json " + KEY.encode()),
            ("INVALID_RESPONSE", lambda: b"\xff\xfe"),
            ("INVALID_RESPONSE", lambda: {"results": [], "noEvidence": "true"}),
            ("INVALID_RESPONSE", lambda: {"results": [], "noEvidence": False}),
            ("INVALID_RESPONSE", lambda: {"results": [{"chunkId": "", "documentId": "", "documentVersion": 0, "score": True}], "noEvidence": False}),
            ("INVALID_RESPONSE", lambda: b'{"results":[],"noEvidence":true,"score":NaN}'),
            ("INVALID_RESPONSE", lambda: b'{"results":[],"noEvidence":true,"noEvidence":false}'),
        ]
        for i, (status, fail) in enumerate(failures):
            run_id = f"recover-{i}"
            before = len(self.requests)
            def handler(body):
                return fail() if len(self.requests) == before+1 else self.response(body)
            with self.subTest(status=status, index=i):
                self.assertEqual(self.invoke(handler, run_id=run_id), 1)
                manifest, records = self.artifacts(run_id)
                self.assertEqual(manifest["status"], "COMPLETED_WITH_ERRORS")
                self.assertEqual(len(records), 16)
                self.assertEqual(records[0]["outcome"]["status"], status)
                self.assertIsNotNone(records[0]["outcome"]["error"])
                self.assertEqual(records[-1]["outcome"]["status"], "SUCCEEDED")
                self.assertEqual(manifest["outcome_counts"]["SUCCEEDED"], 15)
                self.assertNotIn(KEY, (self.root / "runs" / run_id / "executions.jsonl").read_text())

    def test_degraded_http_success_preserves_meta_without_hiding_it(self):
        def handler(body):
            response = self.response(body)
            response["meta"] = {"degraded": {"vector": "unavailable"}, "bestSimilarity": 0.67}
            return response
        self.assertEqual(self.invoke(handler), 0)
        manifest, records = self.artifacts()
        self.assertEqual(manifest["degraded_executions"], 16)
        self.assertTrue(all(r["degraded"] for r in records))
        self.assertEqual(records[0]["outcome"]["body"]["meta"]["bestSimilarity"], 0.67)

    def test_forbidden_project_response_is_saved_before_abort(self):
        project = self.snapshot["documents"][4]
        project_chunk = next(c for c in self.snapshot["chunks"] if c["documentId"] == project["documentId"])
        def handler(body):
            response = self.response(body)
            if len(self.requests) == 10:  # 无项目授权的BOUNDARY-010。
                response.update(noEvidence=False, results=[{"chunkId": project_chunk["chunkId"], "documentId": project["documentId"],
                                                           "documentVersion": 1, "score": 0.1}])
            return response
        self.assertEqual(self.invoke(handler), 2)
        manifest, records = self.artifacts()
        self.assertEqual(manifest["status"], "ABORTED_CONTRACT")
        self.assertEqual(len(records), 10)
        self.assertEqual(manifest["remaining_executions"], 6)
        self.assertEqual(records[-1]["outcome"]["status"], "CONTRACT_ERROR")
        self.assertEqual(records[-1]["outcome"]["body"]["results"][0]["documentId"], project["documentId"])

    def test_malformed_response_cannot_bypass_project_scope_abort(self):
        project_chunk = next(c for c in self.snapshot["chunks"] if c["documentId"] == self.snapshot["documents"][4]["documentId"])
        def handler(body):
            response = self.response(body)
            # noEvidence=true且非空，本已是INVALID_RESPONSE，仍须发现越权并中止。
            response.update(noEvidence=True, results=[{"chunkId": project_chunk["chunkId"],
                            "documentId": project_chunk["documentId"], "documentVersion": 1, "score": 0.1}])
            return response
        self.assertEqual(self.invoke(handler), 2)
        manifest, records = self.artifacts()
        self.assertEqual(manifest["status"], "ABORTED_CONTRACT")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["outcome"]["status"], "CONTRACT_ERROR")

    def test_snapshot_identity_change_and_external_chunks_abort(self):
        for run_id, mutate in [("wrong-version", lambda r: r.update(documentVersion=99)),
                               ("outside", lambda r: r.update(chunkId="not-in-snapshot")),
                               ("wrong-parent", lambda r: r.update(documentId=self.snapshot["documents"][1]["documentId"]))]:
            def handler(body):
                response = self.response(body)
                mutate(response["results"][0])
                return response
            self.assertEqual(self.invoke(handler, run_id=run_id), 2)
            manifest, records = self.artifacts(run_id)
            self.assertEqual(manifest["status"], "ABORTED_CONTRACT")
            self.assertEqual(len(records), 1)

    def test_preflight_bad_anchor_creates_failure_manifest_with_no_requests(self):
        damaged = deepcopy(self.snapshot)
        damaged["chunks"][0]["content"] = "无标准证据"
        damaged["chunks"][0]["contentHash"] = content_hash("无标准证据")
        damaged["corpusHash"] = canonical_hash({"documents": damaged["documents"], "chunks": damaged["chunks"]})
        damaged["snapshotId"] = canonical_hash({k: v for k, v in damaged.items() if k != "snapshotId"})
        self.snapshot_path.write_text(json.dumps(damaged), encoding="utf-8")
        self.assertEqual(self.invoke(), 2)
        manifest, records = self.artifacts()
        self.assertEqual(manifest["status"], "PRECHECK_FAILED")
        self.assertEqual(records, [])
        self.assertEqual(self.requests, [])

    def test_missing_key_and_invalid_base_never_make_network_requests(self):
        self.assertEqual(self.invoke(env={}), 2)
        self.assertFalse((self.root / "runs").exists())
        for base in ("https://user:" + KEY + "@host", "ftp://host", "http://host?key=" + KEY, "http://host#fragment"):
            self.assertEqual(self.invoke(extra=["--base", base]), 2)
        self.assertEqual(self.requests, [])
        self.assertNotIn(KEY, self.stderr.getvalue())

    def test_existing_run_directory_never_overwritten(self):
        self.assertEqual(self.invoke(), 0)
        before = {p.name: p.read_bytes() for p in (self.root / "runs/fixed-run").iterdir()}
        calls = len(self.requests)
        self.assertEqual(self.invoke(), 2)
        after = {p.name: p.read_bytes() for p in (self.root / "runs/fixed-run").iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(len(self.requests), calls)

    def test_automatic_run_ids_unique_and_selected_modes_cover_all_cases(self):
        self.assertEqual(self.invoke(extra=["--modes", "KEYWORD", "VECTOR", "HYBRID"], run_id=None), 0)
        self.assertEqual(self.invoke(run_id=None), 0)
        paths = list((self.root / "runs").iterdir())
        self.assertEqual(len(paths), 2)
        counts = sorted(len((p / "executions.jsonl").read_text().splitlines()) for p in paths)
        self.assertEqual(counts, [16, 48])

    def test_credentials_redacted_in_response_config_errors_and_console(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"commit": "test-commit", "api_key": KEY, "password": "config-secret",
                                           "nested": {"note": KEY}}))
        def handler(body):
            response = self.response(body)
            response.update(debug=KEY, credentials={"anything": KEY}, nested={"Authorization": "other-bearer", "note": KEY})
            return response
        self.assertEqual(self.invoke(handler, ["--target-config", str(config_path)]), 0)
        manifest, records = self.artifacts()
        text = "".join(p.read_text() for p in (self.root / "runs/fixed-run").iterdir()) + self.stdout.getvalue() + self.stderr.getvalue()
        self.assertNotIn(KEY, text)
        self.assertNotIn("config-secret", text)
        self.assertNotIn("other-bearer", text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(manifest["target"]["declared_config_sha256"], sha256_file(config_path))
        self.assertTrue(all("commit" not in body for body, _, _ in self.requests))

    def test_previous_record_is_flushed_before_next_request(self):
        def handler(body):
            records = (self.root / "runs/fixed-run/executions.jsonl").read_text().splitlines()
            self.assertEqual(len(records), len(self.requests)-1)
            manifest = json.loads((self.root / "runs/fixed-run/manifest.json").read_text())
            self.assertEqual(manifest["in_flight"]["sequence"], len(self.requests))
            return self.response(body)
        self.assertEqual(self.invoke(handler), 0)

    def test_interrupt_and_internal_failure_preserve_partial_artifacts(self):
        for run_id, error, code, status in [("interrupt", KeyboardInterrupt(), 130, "INTERRUPTED"),
                                            ("internal", RuntimeError(KEY), 1, "FAILED")]:
            def handler(body):
                raise error
            self.assertEqual(self.invoke(handler, run_id=run_id), code)
            manifest, records = self.artifacts(run_id)
            self.assertEqual(manifest["status"], status)
            self.assertEqual(len(records), 1)
            self.assertNotIn(KEY, json.dumps(records))

    def test_output_write_failure_stops_requests_and_preserves_inflight_marker(self):
        with patch.object(run_eval.RunStore, "append", side_effect=OSError("disk failed " + KEY)):
            self.assertEqual(self.invoke(), 1)
        manifest, records = self.artifacts()
        self.assertEqual(manifest["status"], "FAILED")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(manifest["attempted_executions"], 1)
        self.assertEqual(manifest["recorded_executions"], 0)
        self.assertIsNotNone(manifest["in_flight"])
        self.assertEqual(records, [])
        self.assertNotIn(KEY, json.dumps(manifest))

    def test_cli_parameter_boundaries_fail_before_network(self):
        for i, flags in enumerate((["--top-k", "0"], ["--top-k", "51"], ["--timeout", "nan"],
                                   ["--timeout", "0"], ["--run-id", "../escape"], ["--modes", "VECTOR", "VECTOR"])):
            self.assertEqual(self.invoke(extra=list(flags), run_id=f"invalid-{i}"), 2)
        self.assertEqual(self.requests, [])


class ClientSafetyTests(unittest.TestCase):
    def test_no_import_time_environment_or_default_endpoint_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigurationError):
                SearchClient.from_environment()
        with patch.dict(os.environ, {"KNOWLEDGE_BASE": "http://test.invalid", "CUSTOM_QUERY_KEY": KEY}, clear=True):
            client = SearchClient.from_environment(key_env="CUSTOM_QUERY_KEY")
            self.assertEqual(client.api_key, KEY)
            self.assertNotIn(KEY, repr(client))
        with patch.dict(os.environ, {"KNOWLEDGE_QUERY_KEY": KEY}, clear=True):
            with self.assertRaises(ConfigurationError):
                run_eval.search("问题")

    def test_redirection_cannot_forward_query_or_management_credentials(self):
        handler = NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://another.invalid"))
        with patch("urllib.request.build_opener") as opener:
            client = SearchClient("http://test.invalid", KEY)
            opener.return_value.open.side_effect = HTTPError(client.endpoint, 302, "redirect", {}, io.BytesIO(b"redirect"))
            outcome = client.request({"query": "test"})
            self.assertEqual(outcome.status, "HTTP_ERROR")
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertIsInstance(opener.call_args.args[0], NoRedirectHandler)

    def test_legacy_upload_no_longer_has_default_target_or_management_key(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(load_corpus, "upload") as upload, \
                patch.object(os.sys, "argv", ["load_corpus", "--confirm-test-environment"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                load_corpus.main()
            upload.assert_not_called()

    def test_no_target_from_environment_without_explicit_selection(self):
        with patch.dict(os.environ, {"KNOWLEDGE_QUERY_KEY": KEY}, clear=True):
            with self.assertRaises(ConfigurationError):
                SearchClient.from_environment()
        with patch.dict(os.environ, {"KNOWLEDGE_BASE": "http://test.invalid", "KNOWLEDGE_QUERY_KEY": KEY}, clear=True):
            with self.assertRaises(ConfigurationError):
                SearchClient.from_environment("")

    def test_sanitizer_removes_nested_fields_and_non_json_echoes(self):
        redactor = Redactor(KEY)
        result = redactor.value({"X-API-Key": "unlisted", "data": [{"PASSWORD": "unlisted", "body": "echo " + KEY}]})
        self.assertNotIn("unlisted", json.dumps(result))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertNotIn("another-secret", redactor.text("X-API-Key: another-secret"))
        self.assertNotIn("another-secret", redactor.text('"authorization": "Bearer another-secret"'))
        self.assertEqual(run_eval.parser().parse_args([]).modes, ["KEYWORD", "VECTOR", "HYBRID"])


if __name__ == "__main__":
    unittest.main()
