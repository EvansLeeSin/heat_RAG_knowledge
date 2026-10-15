"""可追溯检索执行器；仅记录执行，不自动调整阈值或生成质量对比报告。

python eval/run_eval.py --dataset eval/datasets/beijing-v1/manifest.json \
    --snapshot <快照文件> --base http://<隔离测试服务>:8000 \
    --modes KEYWORD VECTOR HYBRID --output-dir eval/runs

查询密钥只从 KNOWLEDGE_QUERY_KEY（或 --key-env 指定的环境变量）读取。
用法、文件格式和失败语义见 eval/RUNNER_GUIDE.md。
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys
import time
import uuid

try:
    from . import metrics
    from .corpus_map import chunks_from_snapshot, prepare_cases, validate_snapshot
    from .dataset import DEFAULT_DATASET, load_dataset, sha256_file, validate_request, check_result_contract
    from .http_client import ConfigurationError, HttpOutcome, Redactor, SearchClient
except ImportError:
    import metrics
    from corpus_map import chunks_from_snapshot, prepare_cases, validate_snapshot
    from dataset import DEFAULT_DATASET, load_dataset, sha256_file, validate_request, check_result_contract
    from http_client import ConfigurationError, HttpOutcome, Redactor, SearchClient

MODES = ("KEYWORD", "VECTOR", "HYBRID")
DEFAULT_RUNS = Path(__file__).parent / "runs"


def configure_console():
    # 恢复旧脚本的Windows UTF-8输出保护；仅CLI入口调用，不改导入方的流。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def search(query: str, top_k: int = 10, mode: str = "HYBRID", request: dict | None = None):
    """兼容阈值脚本的调用接口；按调用时环境取配置，不缓存或提供默认密钥。"""
    conditions = validate_request(request) if request is not None else {}
    return SearchClient.from_environment().search({"query": query, **conditions, "topK": top_k, "retrievalMode": mode})


def search_case(case: dict, top_k: int = 10, mode: str = "HYBRID"):
    results, latency, body = search(case["query"], top_k=top_k, mode=mode, request=case["request"])
    check_result_contract(case, results)
    return results, latency, body


# 兼容旧导入入口，但只保留一套V1口径；CLI执行本身仍不打分。
recall_at_k = metrics.recall_at_k
ndcg_at_k = metrics.ndcg_at_k


def mrr(ranked: list[str], relevant: set[str]) -> float | None:
    return metrics.mrr_at_k(ranked, relevant, 10)


def p95(values: list[float]) -> float | None:
    return metrics.percentile(values, .95)


class RunStore:
    def __init__(self, root: Path, run_id: str, manifest: dict, redactor: Redactor):
        self.path = root / run_id
        self.path.mkdir(mode=0o700, parents=True, exist_ok=False)  # 绝不复用已有目录，包括空目录。
        self.redactor = redactor
        self.manifest = manifest
        self.file = (self.path / "executions.jsonl").open("x", encoding="utf-8", newline="\n")
        try:
            self.save()
        except BaseException:
            self.file.close()
            raise

    def save(self):
        temp = self.path / "manifest.json.tmp"
        with temp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(self.redactor.value(self.manifest), f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, self.path / "manifest.json")

    def append(self, record):
        self.file.write(json.dumps(self.redactor.value(record), ensure_ascii=False, allow_nan=False) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())  # 下一条HTTP之前落盘，IO失败则停止而不是丢结果继续。

    def close(self):
        self.file.close()


def load_inputs(dataset_path: Path, snapshot_path: Path):
    before = sha256_file(dataset_path)
    dataset = load_dataset(dataset_path)
    if sha256_file(dataset_path) != before:
        raise ValueError("加载过程中评测集清单发生变化")
    raw = snapshot_path.read_bytes()
    snapshot = validate_snapshot(json.loads(raw))
    chunks = chunks_from_snapshot(snapshot)
    pos, neg = prepare_cases(dataset.cases, chunks, dataset=dataset)
    by_id = {c["case_id"]: c for c in pos + neg}
    cases = [by_id[c["case_id"]] for c in dataset.cases]  # 不改变题目顺序，不减少分母。
    return dataset, snapshot, cases, before, hashlib.sha256(raw).hexdigest()


def _identity_contract(case, results, chunk_identity):
    # 格式损坏的响应也不能绕过权限/数据边界检查；只对已给出的身份字段判断。
    docs = set(case["_corpus_document_ids"])
    forbidden = set(case["expected"]["forbidden_document_ids"]) | set(case["_scope_forbidden"])
    for result in results:
        if not isinstance(result, dict):
            continue
        doc_id, chunk_id, version = result.get("documentId"), result.get("chunkId"), result.get("documentVersion")
        if isinstance(doc_id, str) and doc_id and (doc_id not in docs or doc_id in forbidden):
            raise ValueError("返回禁止文档或快照外文档")
        if isinstance(chunk_id, str) and chunk_id:
            if chunk_id not in chunk_identity:
                raise ValueError("返回快照外chunk")
            parent, actual_version = chunk_identity[chunk_id]
            if parent in forbidden or (isinstance(doc_id, str) and doc_id and parent != doc_id):
                raise ValueError("返回chunk的文档身份/权限与快照不一致")
            if type(version) is int and version > 0 and actual_version != version:
                raise ValueError("返回chunk的活动版本与快照不一致")


def execute(store: RunStore, client: SearchClient, cases: list[dict], snapshot: dict, args) -> int:
    manifest = store.manifest
    phases = ["WARMUP", "MEASURE"] if args.warmup else ["MEASURE"]
    planned = len(cases) * len(args.modes) * len(phases)
    manifest.update(status="RUNNING", planned_executions=planned, remaining_executions=planned,
                    recorded_executions=0, started_execution_at=now())
    store.save()
    chunk_identity = {c["chunkId"]: (c["documentId"], c["version"]) for c in snapshot["chunks"]}
    counts, degraded, interrupted, fatal = Counter(), 0, False, False
    try:
        for phase in phases:
            for mode in args.modes:
                for case in cases:
                    body = {"query": case["query"], **case["request"], "topK": args.top_k, "retrievalMode": mode}
                    started_at = now()
                    sequence = sum(counts.values()) + 1
                    manifest.update(attempted_executions=sequence,
                                    in_flight={"sequence": sequence, "case_id": case["case_id"], "phase": phase,
                                               "mode": mode, "started_at": started_at})
                    store.save()
                    started = time.perf_counter()
                    try:
                        outcome = client.request(body)
                        if isinstance(outcome.body, dict) and isinstance(outcome.body.get("results"), list):
                            try:
                                _identity_contract(case, outcome.body["results"], chunk_identity)
                            except ValueError as exc:
                                outcome.status = "CONTRACT_ERROR"
                                outcome.error = {"code": "RESULT_SCOPE_OR_IDENTITY", "message": str(exc), "prior_error": outcome.error}
                                fatal = True
                    except KeyboardInterrupt:
                        outcome = HttpOutcome("INTERRUPTED", time.perf_counter() - started,
                                              error={"code": "INTERRUPTED", "message": "操作者中断执行"})
                        interrupted = True
                    except Exception as exc:
                        # 编程/未预期故障不当作可恢复的网络失败继续。
                        outcome = HttpOutcome("INTERNAL_ERROR", time.perf_counter() - started,
                                              error={"code": type(exc).__name__, "message": "执行器内部异常，已中止"})
                        fatal = True
                    meta = outcome.body.get("meta", {}) if isinstance(outcome.body, dict) else {}
                    degradation = meta.get("degraded") if isinstance(meta, dict) else None
                    record = {"schema_version": 1, "run_id": manifest["run_id"], "sequence": sequence,
                              "phase": phase, "mode": mode, "attempt": 1, "case_id": case["case_id"],
                              "dataset_version": manifest["dataset"]["dataset_version"],
                              "case_type": case["type"], "tags": case["tags"], "expected": case["expected"],
                              "relevant_chunk_ids": sorted(case["_relevant"]), "request": body,
                              "started_at": started_at, "finished_at": now(), "outcome": asdict(outcome),
                              "degraded": bool(degradation), "degradation": degradation}
                    store.append(record)
                    counts[outcome.status] += 1
                    degraded += bool(degradation)
                    manifest.update(recorded_executions=sum(counts.values()), outcome_counts=dict(counts),
                                    degraded_executions=degraded, remaining_executions=planned-sum(counts.values()), in_flight=None)
                    store.save()
                    print(store.redactor.text(f"[{phase}/{mode}] {case['case_id']} {outcome.status} {outcome.latency_seconds:.3f}s"))
                    if fatal or interrupted:
                        break
                if fatal or interrupted:
                    break
            if fatal or interrupted:
                break
    except KeyboardInterrupt:
        interrupted = True  # 调用之间/落盘之后中断也保留已完成记录。
    status = ("INTERRUPTED" if interrupted else "ABORTED_CONTRACT" if counts["CONTRACT_ERROR"]
              else "FAILED" if fatal else "COMPLETED_WITH_ERRORS" if any(k != "SUCCEEDED" and v for k, v in counts.items())
              else "COMPLETED")
    manifest.update(status=status, finished_at=now())
    store.save()
    return 130 if interrupted else 2 if counts["CONTRACT_ERROR"] else 1 if status in {"FAILED", "COMPLETED_WITH_ERRORS"} else 0


def parser():
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("dataset_path", nargs="?", help="兼容旧位置参数；推荐 --dataset")
    ap.add_argument("--dataset", help="标准评测集manifest，默认 beijing-v1")
    ap.add_argument("--snapshot", help="离线快照；或设置 KNOWLEDGE_EVAL_SNAPSHOT")
    ap.add_argument("--base", help="明确的测试服务base URL；或设置 KNOWLEDGE_BASE")
    ap.add_argument("--key-env", default="KNOWLEDGE_QUERY_KEY", help="查询密钥的环境变量名，不接受明文密钥参数")
    ap.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--warmup", action="store_true", help="先对所有案例/所选模式执行一遍并保存WARMUP记录")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_RUNS, help="运行目录的父目录，每次生成独立子目录")
    ap.add_argument("--run-id", help="可选固定运行编号；同名目录已存在则拒绝执行")
    ap.add_argument("--target-config", type=Path, help="可选被测部署配置JSON，仅留档，不发送/应用到服务")
    return ap


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    secrets = [os.environ.get(name, "") for name in
               (args.key_env, "KNOWLEDGE_QUERY_KEY", "KNOWLEDGE_MANAGEMENT_KEY", "KNOWLEDGE_EVAL_DATABASE_URL")]
    redactor = Redactor(*secrets)
    store = None
    try:
        if args.dataset and args.dataset_path:
            raise ConfigurationError("--dataset 与位置参数不能同时提供")
        if not 1 <= args.top_k <= 50 or len(args.modes) != len(set(args.modes)):
            raise ConfigurationError("top-k 必须为1..50，检索模式不能重复")
        run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", run_id):
            raise ConfigurationError("run-id 必须为1..96位字母、数字、下划线或连字符")
        snapshot_value = args.snapshot if args.snapshot is not None else os.environ.get("KNOWLEDGE_EVAL_SNAPSHOT")
        if not snapshot_value:
            raise ConfigurationError("请显式指定 --snapshot 或 KNOWLEDGE_EVAL_SNAPSHOT")
        client = SearchClient.from_environment(args.base, key_env=args.key_env, timeout=args.timeout)
        dataset_path = Path(args.dataset or args.dataset_path or DEFAULT_DATASET).resolve()
        snapshot_path = Path(snapshot_value).resolve()
        manifest = {"schema_version": 1, "run_id": run_id, "status": "PREPARING", "created_at": now(),
                    "target": {"base_url": client.base_url, "endpoint": client.endpoint, "key_env": args.key_env,
                               "configuration_provenance": "operator_supplied_not_remote_verified"},
                    "execution": {"modes": args.modes, "top_k": args.top_k, "timeout_seconds": args.timeout,
                                  "warmup": args.warmup, "order": "phase-mode-dataset_order", "retries": 0},
                    "inputs": {"dataset_path": str(dataset_path), "snapshot_path": str(snapshot_path)},
                    "runtime": {"python": platform.python_version(), "platform": platform.system()},
                    "runner_source_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in
                                             ("run_eval.py", "http_client.py", "corpus_map.py", "dataset.py", "metrics.py")},
                    "attempted_executions": 0, "recorded_executions": 0, "outcome_counts": {}, "degraded_executions": 0}
        store = RunStore(args.output_dir.resolve(), run_id, manifest, redactor)
        dataset, snapshot, cases, dataset_hash, snapshot_hash = load_inputs(dataset_path, snapshot_path)
        if args.target_config:
            raw_config = args.target_config.read_bytes()
            config = json.loads(raw_config, parse_constant=lambda s: (_ for _ in ()).throw(ValueError("配置包含非有限JSON数值")))
            if not isinstance(config, dict):
                raise ValueError("target-config 必须为JSON对象")
            manifest["target"]["declared_config"] = config
            manifest["target"]["declared_config_sha256"] = hashlib.sha256(raw_config).hexdigest()
        manifest["dataset"] = {"dataset_id": dataset.manifest["dataset_id"], "dataset_version": dataset.manifest["dataset_version"],
                               "split": dataset.manifest["split"], "case_count": len(cases), "manifest_sha256": dataset_hash,
                               "cases_sha256": dataset.manifest["cases_sha256"], "corpus": dataset.manifest["corpus"]}
        manifest["snapshot"] = {"snapshot_id": snapshot["snapshotId"], "corpus_hash": snapshot["corpusHash"],
                                "file_sha256": snapshot_hash, "created_at": snapshot["createdAt"],
                                "document_count": snapshot["documentCount"], "chunk_count": snapshot["chunkCount"],
                                "document_versions": [{"document_id": d["documentId"], "version": d["version"]} for d in snapshot["documents"]],
                                "index_metadata": {key: sorted({c[key] for c in snapshot["chunks"] if isinstance(c.get(key), str)})
                                                   for key in ("embeddingModel", "embeddingVersion", "indexVersion", "tokenizerVersion")}}
        result = execute(store, client, cases, snapshot, args)
        print(redactor.text(f"运行 {run_id}：{manifest['status']}；记录位于 {store.path}"))
        return result
    except (ConfigurationError, OSError, ValueError) as exc:
        if store is not None:
            store.manifest.update(status="FAILED" if store.manifest["status"] == "RUNNING" else "PRECHECK_FAILED",
                                  finished_at=now(), error={"code": type(exc).__name__, "message": redactor.text(str(exc))})
            try:
                store.save()
            except (OSError, ValueError):
                pass  # 已有JSONL仍保留；不发出后续请求。
        print(redactor.text(f"执行失败：{exc}"), file=sys.stderr)
        return 2 if store is None or store.manifest["status"] == "PRECHECK_FAILED" else 1
    except Exception as exc:
        if store is not None:
            store.manifest.update(status="FAILED", finished_at=now(),
                                  error={"code": type(exc).__name__, "message": "未预期的执行器异常，已停止"})
            try:
                store.save()
            except (OSError, ValueError):
                pass
        print(f"执行器异常：{type(exc).__name__}；未发出后续请求", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if store is not None:
            store.manifest.update(status="INTERRUPTED", finished_at=now())
            store.save()
        return 130
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    configure_console()
    raise SystemExit(main())
