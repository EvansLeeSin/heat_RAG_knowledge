"""读取第三项执行产物，离线生成分项指标；不调用检索/模型、不修改旧运行。

python eval/score_eval.py --run-dir eval/runs/<run_id>
可用 --dataset / --snapshot 指向搬迁后的原文件，但哈希必须与执行时相同。
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import platform
from pathlib import Path
import re
import sys
import uuid

try:
    from . import metrics
    from .dataset import sha256_file
    from .http_client import Redactor, _protocol_error
    from .run_eval import MODES, configure_console, load_inputs, _identity_contract
except ImportError:
    import metrics
    from dataset import sha256_file
    from http_client import Redactor, _protocol_error
    from run_eval import MODES, configure_console, load_inputs, _identity_contract

STATUSES = {"SUCCEEDED", "TIMEOUT", "NETWORK_ERROR", "HTTP_ERROR", "INVALID_RESPONSE",
            "CONTRACT_ERROR", "INTERRUPTED", "INTERNAL_ERROR"}
FINAL = {"COMPLETED", "COMPLETED_WITH_ERRORS", "ABORTED_CONTRACT", "INTERRUPTED", "FAILED"}
RANK_METRICS = {"recall_at_5": (5, metrics.recall_at_k), "mrr_at_10": (10, metrics.mrr_at_k),
                "ndcg_at_10": (10, metrics.ndcg_at_k)}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _object(pairs):
    obj = {}
    for k, v in pairs:
        require(k not in obj, "输入JSON有重复字段")
        obj[k] = v
    return obj


def decode(raw):
    return json.loads(raw, object_pairs_hook=_object,
                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError("输入JSON有非有限数值")))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_run(run_dir: Path, dataset_path=None, snapshot_path=None):
    """复核数据来源、计划网格和原始记录；不信任记录里的gold标签直接打分。"""
    manifest_file, records_file = run_dir / "manifest.json", run_dir / "executions.jsonl"
    manifest_raw, records_raw = manifest_file.read_bytes(), records_file.read_bytes()
    manifest = decode(manifest_raw)
    require(isinstance(manifest, dict) and type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1, "不支持的运行清单格式")
    require(manifest.get("status") in FINAL, "只能评分已结束且通过数据预检的运行；不读取运行中/预检失败的目录")
    require(isinstance(manifest.get("run_id"), str) and bool(manifest["run_id"]), "运行编号无效")
    inputs = manifest["inputs"]
    dataset, snapshot, cases, dataset_hash, snapshot_hash = load_inputs(
        Path(dataset_path or inputs["dataset_path"]), Path(snapshot_path or inputs["snapshot_path"]))
    source_dataset, source_snapshot = manifest["dataset"], manifest["snapshot"]
    require(dataset_hash == source_dataset["manifest_sha256"]
            and dataset.manifest["cases_sha256"] == source_dataset["cases_sha256"]
            and dataset.manifest["dataset_version"] == source_dataset["dataset_version"]
            and dataset.manifest["dataset_id"] == source_dataset["dataset_id"]
            and dataset.manifest["corpus"] == source_dataset["corpus"]
            and len(cases) == source_dataset["case_count"], "评测集与执行时不一致")
    require(snapshot_hash == source_snapshot["file_sha256"]
            and snapshot["snapshotId"] == source_snapshot["snapshot_id"]
            and snapshot["corpusHash"] == source_snapshot["corpus_hash"], "语料快照与执行时不一致")
    execution = manifest["execution"]
    modes, top_k, warmup = execution["modes"], execution["top_k"], execution["warmup"]
    require(isinstance(modes, list) and modes and all(m in MODES for m in modes)
            and len(modes) == len(set(modes)), "运行检索模式无效")
    require(type(top_k) is int and 1 <= top_k <= 50 and type(warmup) is bool
            and execution["order"] == "phase-mode-dataset_order" and execution["retries"] == 0,
            "不支持的执行配置/顺序/重试口径")
    grid = [(phase, mode, c) for phase in (["WARMUP", "MEASURE"] if warmup else ["MEASURE"])
            for mode in modes for c in cases]
    require(type(manifest["planned_executions"]) is int and manifest["planned_executions"] == len(grid),
            "计划执行数与完整案例网格不一致")
    lines = records_raw.decode("utf-8").splitlines()
    require(all(line.strip() for line in lines), "执行文件有空行；不静默跳过损坏记录")
    records = [decode(line) for line in lines]
    require(len(records) <= len(grid) and manifest["recorded_executions"] == len(records)
            and manifest["remaining_executions"] == len(grid) - len(records), "执行数与清单不一致")
    identities = {c["chunkId"]: (c["documentId"], c["version"]) for c in snapshot["chunks"]}
    measure = {}
    for sequence, (row, (phase, mode, case)) in enumerate(zip(records, grid), 1):
        require(isinstance(row, dict) and type(row.get("schema_version")) is int and row["schema_version"] == 1,
                "执行记录格式无效")
        require(type(row["sequence"]) is int and row["sequence"] == sequence
                and row["phase"] == phase and row["mode"] == mode and row["case_id"] == case["case_id"]
                and row["run_id"] == manifest["run_id"] and row["attempt"] == 1,
                "执行顺序/编号错误、重复或缺失；不去重后继续打分")
        require(row["request"] == {"query": case["query"], **case["request"], "topK": top_k, "retrievalMode": mode}
                and row["expected"] == case["expected"] and row["relevant_chunk_ids"] == sorted(case["_relevant"])
                and row["dataset_version"] == dataset.manifest["dataset_version"]
                and row["case_type"] == case["type"] and row["tags"] == case["tags"],
                "执行请求或标准证据与版本化案例不一致（包括脱敏导致的身份损坏）")
        outcome = row["outcome"]
        require(isinstance(outcome, dict) and outcome.get("status") in STATUSES, "未知执行状态")
        latency = outcome.get("latency_seconds")
        require(type(latency) in (int, float) and math.isfinite(latency) and latency >= 0, "耗时非有限数值或为负")
        body = outcome.get("body")
        violation = False
        if isinstance(body, dict) and isinstance(body.get("results"), list):
            try:
                _identity_contract(case, body["results"], identities)
            except ValueError:
                violation = True
        require(violation == (outcome["status"] == "CONTRACT_ERROR"), "权限/快照契约违反与原始状态不一致")
        if outcome["status"] == "SUCCEEDED":
            require(type(outcome.get("http_status")) is int and 200 <= outcome["http_status"] < 300
                    and outcome.get("error") is None and _protocol_error(body) is None,
                    "成功记录实际是HTTP/响应格式故障")
        if phase == "MEASURE":
            measure[(mode, case["case_id"])] = row
    counts = dict(Counter(r["outcome"]["status"] for r in records))
    require(manifest["outcome_counts"] == counts, "执行状态计数与清单不一致")
    attempted = manifest["attempted_executions"]
    require(type(attempted) is int and len(records) <= attempted <= min(len(grid), len(records) + 1), "尝试数无效")
    pending = None
    if attempted > len(records):
        phase, mode, case = grid[len(records)]
        active = manifest.get("in_flight")
        require(isinstance(active, dict) and active.get("sequence") == attempted and active.get("phase") == phase
                and active.get("mode") == mode and active.get("case_id") == case["case_id"], "未落盘请求标记与计划不一致")
        if phase == "MEASURE":
            pending = (mode, case["case_id"])
    if manifest["status"] == "COMPLETED":
        require(len(records) == len(grid) and set(counts) == {"SUCCEEDED"}, "完成状态隐藏了失败或未执行案例")
    if manifest["status"] == "COMPLETED_WITH_ERRORS":
        require(len(records) == len(grid) and any(s != "SUCCEEDED" for s in counts), "带故障完成状态不一致")
    if manifest["status"] == "ABORTED_CONTRACT":
        require(bool(records) and records[-1]["outcome"]["status"] == "CONTRACT_ERROR", "契约中止缺少异常记录")
    require(not any(r["outcome"]["status"] in {"CONTRACT_ERROR", "INTERRUPTED", "INTERNAL_ERROR"} for r in records[:-1]),
            "执行器在致命故障后继续运行，记录不符合契约")
    return manifest, cases, measure, pending, records, {manifest_file: digest(manifest_raw), records_file: digest(records_raw)}


def score_case(case, row, mode, top_k, *, unrecorded=False):
    positive = case["type"] != "N"
    result = {"schema_version": 1, "case_id": case["case_id"], "mode": mode, "case_type": case["type"],
              "tags": case["tags"], "relevant_chunk_count": len(case["_relevant"]),
              "execution_sequence": row["sequence"] if row else None,
              "execution_status": row["outcome"]["status"] if row else "UNRECORDED_ATTEMPT" if unrecorded else "NOT_EXECUTED",
              "score_status": "EXECUTION_FAILED" if row else "NOT_EXECUTED",
              "latency_seconds": row["outcome"]["latency_seconds"] if row else None,
              "degradation_status": "UNKNOWN", "observed_no_evidence": None,
              "no_evidence_reliable": False, "no_evidence_reason": "NO_SUCCESSFUL_RESPONSE",
              "returned_count": None, "unique_returned_count": None, "duplicate_count": None,
              "metrics": {**{name: None for name in RANK_METRICS}, "positive_no_evidence": None, "negative_no_evidence": None},
              "unavailable": {name: "NO_SUCCESSFUL_RESPONSE" if positive else "NOT_APPLICABLE" for name in RANK_METRICS}}
    if not row or row["outcome"]["status"] != "SUCCEEDED":
        return result
    body = row["outcome"]["body"]
    ranked = [r["chunkId"] for r in body["results"]]
    result.update(score_status="SCORED", observed_no_evidence=body["noEvidence"], returned_count=len(ranked),
                  unique_returned_count=len(set(ranked)), duplicate_count=len(ranked)-len(set(ranked)),
                  degradation_status=metrics.degradation(body.get("meta", {}), mode))
    reliable, reason = metrics.reliable_no_evidence(body, mode)
    result.update(no_evidence_reliable=reliable, no_evidence_reason=reason)
    try:
        if reliable:
            result["metrics"]["positive_no_evidence" if positive else "negative_no_evidence"] = int(body["noEvidence"])
        if positive:
            for name, (k, function) in RANK_METRICS.items():
                if top_k < k:
                    result["unavailable"][name] = "REQUEST_TOPK_TOO_SMALL"
                else:
                    value = function(ranked, case["_relevant"], k)
                    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1, "评分函数输出非法数值")
                    result["metrics"][name] = value
                    result["unavailable"].pop(name, None)
    except Exception as exc:
        # 评分失败与业务失败不同；不丢案例，不把未知分数伪装成0分。
        result.update(score_status="SCORING_ERROR", scoring_error={"code": type(exc).__name__, "message": "评分实现异常"})
        result["metrics"] = {name: None for name in result["metrics"]}
        result["unavailable"] = {name: "SCORING_ERROR" for name in RANK_METRICS}
    return result


def latency_stats(rows):
    values = [r["latency_seconds"] for r in rows if r["latency_seconds"] is not None]
    return {"sample_count": len(values), "p50": metrics.percentile(values, .5), "p95": metrics.percentile(values, .95)}


def aggregate(rows):
    positive = [r for r in rows if r["case_type"] != "N"]
    negative = [r for r in rows if r["case_type"] == "N"]
    succeeded = [r for r in rows if r["execution_status"] == "SUCCEEDED"]
    degraded = [r for r in succeeded if r["degradation_status"] == "REPORTED_DEGRADATION"]
    count = len(rows)
    quality = {name: metrics.bounded_mean([r["metrics"][name] for r in positive]) for name in RANK_METRICS}
    quality["positive_no_evidence_rate"] = metrics.bounded_mean([r["metrics"]["positive_no_evidence"] for r in positive])
    quality["negative_no_evidence_rate"] = metrics.bounded_mean([r["metrics"]["negative_no_evidence"] for r in negative])
    applicable = [m for m in quality.values() if m["expected_count"]]
    return {"expected_count": count, "positive_count": len(positive), "negative_count": len(negative),
            "execution_status_counts": dict(Counter(r["execution_status"] for r in rows)),
            "score_status_counts": dict(Counter(r["score_status"] for r in rows)),
            "success_rate": {"value": len(succeeded)/count if count else None, "numerator": len(succeeded), "denominator": count,
                             "definition": "HTTP_AND_RESPONSE_PROTOCOL_SUCCESS_NOT_CHANNEL_HEALTH"},
            "reported_degradation_rate": {"value": len(degraded)/count if count else None, "numerator": len(degraded),
                                          "denominator": count, "observed_success_count": len(succeeded)},
            "quality": quality, "quality_complete": bool(applicable) and all(m["complete"] for m in applicable),
            "latency_success_seconds": latency_stats(succeeded),
            "latency_nondegraded_success_seconds": latency_stats([r for r in succeeded if r["degradation_status"] != "REPORTED_DEGRADATION"]),
            "latency_all_recorded_attempts_seconds": latency_stats(rows),
            "duplicate_result_executions": sum(bool(r["duplicate_count"]) for r in succeeded),
            "unverified_no_evidence_executions": sum(not r["no_evidence_reliable"] for r in succeeded)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--dataset", type=Path)
    ap.add_argument("--snapshot", type=Path)
    ap.add_argument("--score-id", help="可选不可覆盖的评分批次编号")
    args = ap.parse_args(argv)
    redactor = Redactor(*(os.environ.get(k, "") for k in
                          ("KNOWLEDGE_QUERY_KEY", "KNOWLEDGE_MANAGEMENT_KEY", "KNOWLEDGE_EVAL_DATABASE_URL")))
    try:
        score_id = args.score_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", score_id)), "score-id格式无效")
        run_dir = args.run_dir.resolve()
        source, cases, measured, pending, records, source_hashes = read_run(run_dir, args.dataset, args.snapshot)
        custom_key = source.get("target", {}).get("key_env")
        if isinstance(custom_key, str):
            redactor = Redactor(*redactor.secrets, os.environ.get(custom_key, ""))
        modes, top_k = source["execution"]["modes"], source["execution"]["top_k"]
        rows = [score_case(c, measured.get((mode, c["case_id"])), mode, top_k,
                           unrecorded=pending == (mode, c["case_id"])) for mode in modes for c in cases]
        by_mode = {mode: aggregate([r for r in rows if r["mode"] == mode]) for mode in modes}
        # 相同执行结果可重评，不能覆盖；不生成第五项的比较报告/质量发布门禁。
        complete = all(group["quality_complete"] for group in by_mode.values())
        manifest = {"schema_version": 1, "score_id": score_id, "run_id": source["run_id"],
                    "created_at": datetime.now(timezone.utc).isoformat(), "status": "COMPLETED" if complete else "COMPLETED_WITH_UNAVAILABLE_METRICS",
                    "metric_policy_version": metrics.POLICY_VERSION, "source_run_status": source["status"],
                    "runtime": {"python": platform.python_version(), "platform": platform.system()},
                    "source_sha256": {p.name: h for p, h in source_hashes.items()},
                    "dataset_id": source["dataset"]["dataset_id"], "dataset_version": source["dataset"]["dataset_version"],
                    "dataset_split": source["dataset"]["split"],
                    "dataset_manifest_sha256": source["dataset"]["manifest_sha256"],
                    "snapshot_file_sha256": source["snapshot"]["file_sha256"],
                    "scorer_source_sha256": {name: sha256_file(Path(__file__).with_name(name)) for name in
                                              ("score_eval.py", "metrics.py", "run_eval.py", "dataset.py", "corpus_map.py", "http_client.py")},
                    "expected_measure_executions": len(rows), "recorded_measure_executions": len(measured),
                    "excluded_warmup_executions": sum(r["phase"] == "WARMUP" for r in records),
                    "warmup_outcome_counts": dict(Counter(r["outcome"]["status"] for r in records if r["phase"] == "WARMUP")),
                    "quality_complete": complete, "production_answer_quality_evaluated": False,
                    "warnings": ["降级率仅为服务声明，不是实际通路故障率；当前接口不能可靠区分向量空命中与Embedding不可用。",
                                 "P50/P95只描述本次测量；模式固定顺序可能共享缓存，不代表独立冷启动性能。",
                                 "指标未知时value为null，附完整分母、覆盖率和界限；observed_subset_mean不是完整实验得分。",
                                 "Recall以chunk为单位，重切分会改变金标准chunk数量；不能直接当作原文证据覆盖率。"]}
        require(all(sha256_file(p) == h for p, h in source_hashes.items()), "评分期间运行产物发生变化")
        destination = run_dir / "scores" / score_id
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        def write_json(name, value):
            with (destination / name).open("x", encoding="utf-8", newline="\n") as f:
                json.dump(redactor.value(value), f, ensure_ascii=False, indent=2, allow_nan=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        # 先PREPARING，最后原子写完成清单；IO错误不能留下伪“评分完成”。
        write_json("manifest.json", {**manifest, "status": "PREPARING"})
        with (destination / "scores.jsonl").open("x", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(redactor.value(row), ensure_ascii=False, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        write_json("metrics.json", {"schema_version": 1, "metric_policy_version": metrics.POLICY_VERSION, "by_mode": by_mode})
        manifest["artifact_sha256"] = {name: sha256_file(destination / name) for name in ("scores.jsonl", "metrics.json")}
        require(all(sha256_file(p) == h for p, h in source_hashes.items()), "发布评分前运行产物发生变化")
        write_json("manifest.json.tmp", manifest)
        os.replace(destination / "manifest.json.tmp", destination / "manifest.json")
        print(redactor.text(f"评分完成：{destination}；{manifest['status']}。这不是质量发布通过结论。"))
        return 0 if complete else 1
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(redactor.text(f"评分中止：{exc}"), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"评分器异常：{type(exc).__name__}；未发布完成评分清单", file=sys.stderr)
        return 2


if __name__ == "__main__":
    configure_console()
    raise SystemExit(main())
