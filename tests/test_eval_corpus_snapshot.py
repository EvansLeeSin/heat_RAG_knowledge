"""第一项：离线快照与证据映射回归测试；不连接数据库或外部模型。"""

from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from eval.corpus_map import (
    CorpusValidationError, build_snapshot, canonical_hash, fetch_chunks,
    load_bindings, load_snapshot, prepare_cases, resolve, validate_snapshot,
)
from eval.export_snapshot import export_database, read_active_corpus, rows_to_snapshot, save_snapshot

ROOT = Path(__file__).resolve().parents[1]


def document(doc_id="doc-a", version=2):
    return {"documentId": doc_id, "version": version, "title": "测试文档"}


def chunk(chunk_id="chunk-a", doc_id="doc-a", version=2, index=0, content="应当现场核查压力"):
    return {"chunkId": chunk_id, "documentId": doc_id, "version": version,
            "chunkIndex": index, "content": content}


def document_row(doc_id="doc-a", version=2):
    return {
        "document_id": doc_id, "active_version": version, "version": version,
        "title": "测试文档", "publisher": None, "source_url": None,
        "document_type": "OTHER", "region": "北京市", "applicability": None,
        "effective_status": "TO_VERIFY", "publication_status": None,
        "published_at": None, "effective_from": None, "effective_to": None,
        "access_scope": "PUBLIC", "project_ids": [], "content_hash": "source-hash",
        "parser_version": "p1",
    }


def chunk_row(index=0, version=2):
    return {
        "chunk_id": f"chunk-{index}", "document_id": "doc-a", "version": version,
        "chunk_index": index, "section_path": None, "article_no": None,
        "heading": None, "content": f"证据内容 {index}", "content_hash": None,
        "token_count": 10, "access_scope": "PUBLIC", "project_ids": [],
        "tokenizer_version": "t1", "has_search_vector": True,
        "embedding_model": "test-model", "embedding_version": "e1",
        "index_version": "i1", "has_embedding": True,
    }


def reseal(snapshot):
    """结构测试重算外层校验，确保不是仅靠整体哈希发现问题。"""
    snapshot["corpusHash"] = canonical_hash({"documents": snapshot["documents"], "chunks": snapshot["chunks"]})
    snapshot["snapshotId"] = canonical_hash({k: v for k, v in snapshot.items() if k != "snapshotId"})
    return snapshot


class SnapshotTests(unittest.TestCase):
    def test_roundtrip_without_network_and_document_metadata(self):
        snapshot = build_snapshot([document()], [chunk()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            save_snapshot(snapshot, path)
            with patch("urllib.request.urlopen", side_effect=AssertionError("不能调用检索")):
                self.assertEqual(load_snapshot(path), snapshot)
                chunks = fetch_chunks(path)
            self.assertEqual(chunks[0]["title"], "测试文档")
            self.assertEqual(snapshot["documents"][0]["chunkCount"], 1)

    def test_missing_path_does_not_use_remote_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CorpusValidationError, "KNOWLEDGE_EVAL_SNAPSHOT"):
                fetch_chunks()

    def test_legacy_search_result_list_is_rejected(self):
        with self.assertRaises(CorpusValidationError):
            validate_snapshot([chunk()])

    def test_content_tampering_is_rejected(self):
        snapshot = build_snapshot([document()], [chunk()])
        snapshot["chunks"][0]["content"] = "被修改"
        with self.assertRaisesRegex(CorpusValidationError, "整体哈希"):
            validate_snapshot(snapshot)
        with self.assertRaisesRegex(CorpusValidationError, "内容哈希"):
            validate_snapshot(reseal(snapshot))

    def test_count_mismatch_is_rejected(self):
        snapshot = build_snapshot([document()], [chunk()])
        snapshot["documents"][0]["chunkCount"] = 2
        with self.assertRaisesRegex(CorpusValidationError, "数量"):
            validate_snapshot(reseal(snapshot))

    def test_total_count_and_document_list_are_checked(self):
        for key, value in (("chunkCount", 20), ("documentCount", 20), ("documentIds", ["wrong"])):
            snapshot = build_snapshot([document()], [chunk()])
            snapshot[key] = value
            with self.assertRaises(CorpusValidationError):
                validate_snapshot(reseal(snapshot))

    def test_zero_chunks_duplicate_ids_and_inactive_versions_are_rejected(self):
        invalid = [([], [chunk()]), ([document()], []),
                   ([document(), document()], [chunk()]),
                   ([document()], [chunk(), chunk()]),
                   ([document()], [chunk(version=1)]),
                   ([document()], [chunk(), chunk(chunk_id="another", index=0)])]
        for docs, chunks in invalid:
            with self.subTest(docs=docs, chunks=chunks):
                with self.assertRaises(CorpusValidationError):
                    build_snapshot(docs, chunks)

    def test_corpus_hash_stable_across_exports_and_input_order(self):
        docs = [document("doc-a"), document("doc-b")]
        chunks = [chunk(), chunk("chunk-b", "doc-b")]
        first = build_snapshot(docs, chunks)
        second = build_snapshot(list(reversed(docs)), list(reversed(chunks)))
        self.assertEqual(first["corpusHash"], second["corpusHash"])
        self.assertNotEqual(first["snapshotId"], second["snapshotId"])

    def test_save_never_overwrites_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            original = build_snapshot([document()], [chunk()])
            save_snapshot(original, path)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save_snapshot(original, path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])


class EvidenceTests(unittest.TestCase):
    def test_same_text_in_other_document_cannot_match(self):
        chunks = [chunk(), chunk("chunk-b", "doc-b")]
        self.assertEqual(resolve("现场核查", chunks, "doc-a"), ["chunk-a"])
        with self.assertRaises(CorpusValidationError):
            resolve("现场核查", chunks, "doc-missing")

    def test_overlap_in_same_version_allowed_but_cross_version_rejected(self):
        chunks = [chunk(), chunk("chunk-b", index=1)]
        self.assertEqual(resolve("现场核查", chunks, "doc-a"), ["chunk-a", "chunk-b"])
        chunks.append(chunk("old", version=1, index=0))
        with self.assertRaisesRegex(CorpusValidationError, "跨版本"):
            resolve("现场核查", chunks, "doc-a")
        self.assertEqual(resolve("现场核查", chunks, "doc-a", version=1), ["old"])

    def test_partial_anchor_failure_aborts_whole_preflight(self):
        cases = [{"query": "问题", "type": "K", "anchors": ["现场核查", "缺失"]}]
        original = copy.deepcopy(cases)
        with self.assertRaisesRegex(CorpusValidationError, "证据未命中"):
            prepare_cases(cases, [chunk()], {"现场核查": "doc-a", "缺失": "doc-a"})
        self.assertEqual(cases, original)

    def test_unbound_anchor_and_empty_positive_are_rejected(self):
        for anchors in (["现场核查"], []):
            with self.assertRaises(CorpusValidationError):
                prepare_cases([{"query": "问题", "type": "S", "anchors": anchors}], [chunk()], {})

    def test_negative_cannot_have_positive_evidence(self):
        with self.assertRaises(CorpusValidationError):
            prepare_cases([{"query": "问题", "type": "N", "anchors": ["现场核查"]}], [chunk()], {})

    def test_success_preserves_cases_and_negative_count(self):
        cases = [{"query": "问题", "type": "K", "anchors": ["现场核查"]},
                 {"query": "域外", "type": "N", "anchors": []}]
        original = copy.deepcopy(cases)
        positives, negatives = prepare_cases(cases, [chunk()], {"现场核查": "doc-a"})
        self.assertEqual(positives[0]["_relevant"], {"chunk-a"})
        self.assertEqual(len(negatives), 1)
        self.assertEqual(cases, original)

    def test_duplicate_bindings_fail_instead_of_guessing_document(self):
        data = {"schemaVersion": 1, "documents": [
            {"documentId": "a", "anchors": ["同一证据"]},
            {"documentId": "b", "anchors": ["同一证据"]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(CorpusValidationError, "重复绑定"):
                load_bindings(path)

    def test_existing_annotations_have_complete_explicit_bindings(self):
        bindings = load_bindings()
        with (ROOT / "eval/annotations.jsonl").open(encoding="utf-8") as f:
            cases = [json.loads(line) for line in f if line.strip() and not line.startswith("#")]
        anchors = {a for case in cases for a in case["anchors"]}
        self.assertEqual(anchors, set(bindings))
        self.assertEqual(len(set(bindings.values())), 3)
        # 这里只验证绑定完整性，不冒充真实数据库证据预检。

    def test_runner_preflight_and_retired_calibration_never_search(self):
        with patch.object(sys, "path", [str(ROOT / "eval"), *sys.path]):
            for name in ("run_eval", "calibrate_threshold"):
                module = importlib.import_module(name)
                if name == "run_eval":
                    import io
                    with tempfile.TemporaryDirectory() as directory, \
                            patch.dict(os.environ, {"KNOWLEDGE_QUERY_KEY": "test-only-key"}), \
                            patch.object(module, "load_inputs", side_effect=ValueError("证据预检失败")), \
                            patch.object(module.SearchClient, "request") as request, patch.object(sys, "stderr", io.StringIO()):
                        code = module.main(["--base", "http://test.invalid", "--snapshot", "unused.json",
                                            "--output-dir", directory])
                        self.assertEqual(code, 2)
                        request.assert_not_called()
                else:
                    import io
                    with patch("urllib.request.build_opener") as opener, \
                            patch.object(sys, "stdout", io.StringIO()):
                        self.assertEqual(module.main([]), 2)
                    opener.assert_not_called()


class ExportTests(unittest.TestCase):
    def test_rows_include_every_chunk_beyond_search_topk(self):
        snapshot = rows_to_snapshot([document_row()], [chunk_row(i) for i in range(75)], ["doc-a"])
        self.assertEqual(snapshot["chunkCount"], 75)
        self.assertEqual(snapshot["documents"][0]["chunkCount"], 75)
        self.assertEqual(snapshot["chunks"][-1]["indexVersion"], "i1")
        self.assertNotIn("vector", snapshot["chunks"][0])
        self.assertNotIn("storagePath", snapshot["documents"][0])
        validate_snapshot(snapshot)

    def test_missing_document_and_dangling_pointer_rejected(self):
        with self.assertRaisesRegex(CorpusValidationError, "没有活动版本"):
            rows_to_snapshot([], [], ["missing"])
        row = document_row()
        row["version"] = None
        with self.assertRaisesRegex(CorpusValidationError, "活动指针"):
            rows_to_snapshot([row], [], ["doc-a"])

    def test_query_is_bound_unlimited_and_active_version_only(self):
        from sqlalchemy.dialects import postgresql
        connection = MagicMock()
        connection.execute.return_value.mappings.return_value.all.side_effect = [
            [document_row()], [chunk_row(i) for i in range(75)]]
        snapshot = read_active_corpus(connection, ["doc-a"])
        self.assertEqual(snapshot["chunkCount"], 75)
        self.assertEqual(connection.execute.call_count, 2)
        for call in connection.execute.call_args_list:
            statement, params = call.args
            sql = str(statement.compile(dialect=postgresql.dialect()))
            self.assertNotIn("LIMIT", sql.upper())
            self.assertNotIn("<=>", sql)
            self.assertIn("active_version", sql)
            self.assertIn("IN", sql)
            self.assertNotIn("doc-a", sql)
            self.assertEqual(params, {"document_ids": ["doc-a"]})

    def test_read_only_repeatable_read_is_first_command(self):
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        expected = build_snapshot([document()], [chunk()])

        def assert_transaction_started_before_read(conn, ids):
            self.assertEqual(connection.execute.call_count, 1)
            self.assertEqual(str(connection.execute.call_args.args[0]),
                             "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return expected

        with patch("sqlalchemy.create_engine", return_value=engine), \
                patch("eval.export_snapshot.read_active_corpus", side_effect=assert_transaction_started_before_read) as read:
            actual = export_database("postgresql://test@localhost/test", ["doc-a", "doc-a"])
        self.assertIs(actual, expected)
        self.assertEqual(str(connection.execute.call_args.args[0]),
                         "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        read.assert_called_once_with(connection, ["doc-a"])
        engine.dispose.assert_called_once()

    def test_failure_disposes_engine_and_empty_whitelist_rejected(self):
        engine = MagicMock()
        with patch("sqlalchemy.create_engine", return_value=engine), \
                patch("eval.export_snapshot.read_active_corpus", side_effect=CorpusValidationError("失败")):
            with self.assertRaises(CorpusValidationError):
                export_database("postgresql://test@localhost/test", ["doc-a"])
        engine.dispose.assert_called_once()
        with self.assertRaises(CorpusValidationError):
            export_database("postgresql://test@localhost/test", [])
        with self.assertRaises(CorpusValidationError):
            export_database("sqlite://", ["doc-a"])

    def test_cli_requires_explicit_database_and_hides_credentials_on_failure(self):
        env = {k: v for k, v in os.environ.items() if k != "KNOWLEDGE_EVAL_DATABASE_URL"}
        command = [sys.executable, str(ROOT / "eval/export_snapshot.py"), "--document-id", "doc-a", "--out", "unused.json"]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("KNOWLEDGE_EVAL_DATABASE_URL", result.stderr)
        env["KNOWLEDGE_EVAL_DATABASE_URL"] = "not-a-url-password-secret"
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(env["KNOWLEDGE_EVAL_DATABASE_URL"], result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
