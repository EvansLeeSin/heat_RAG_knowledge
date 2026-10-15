"""离线基线/对比报告：显式选择评分批次，复核来源，不调用服务或模型。

python eval/report_eval.py --score-dir eval/runs/<run>/scores/<score>
python eval/report_eval.py --score-dir <候选批次> --baseline-score-dir <基线批次>
多模式比较须显式选择 --mode 与 --baseline-mode；不自动选最新批次或最优模式。
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import uuid

try:
    from . import metrics, score_eval
    from .corpus_map import canonical_hash
    from .dataset import sha256_file
    from .http_client import Redactor
    from .run_eval import MODES, configure_console, load_inputs
except ImportError:
    import metrics, score_eval
    from corpus_map import canonical_hash
    from dataset import sha256_file
    from http_client import Redactor
    from run_eval import MODES, configure_console, load_inputs

POLICY_VERSION = "retrieval-report-v1.0.0"
DEFAULT_REPORTS = Path(__file__).parent / "reports"
SCORER_FILES = ("score_eval.py", "metrics.py", "run_eval.py", "dataset.py", "corpus_map.py", "http_client.py")
QUALITY = {"recall_at_5": ("recall_at_5", True), "mrr_at_10": ("mrr_at_10", True),
           "ndcg_at_10": ("ndcg_at_10", True), "positive_no_evidence_rate": ("positive_no_evidence", False),
           "negative_no_evidence_rate": ("negative_no_evidence", True)}
INDEX_FIELDS = {"embeddingModel", "embeddingVersion", "indexVersion", "tokenizerVersion", "hasEmbedding", "hasSearchVector", "tokenCount"}
CHANGES = ("IMPROVED", "REGRESSED", "UNCHANGED", "UNKNOWN", "NOT_APPLICABLE", "BLOCKED", "UNPAIRED")
CHANGE_LABELS = {"IMPROVED": "改善", "REGRESSED": "退化", "UNCHANGED": "未变", "UNKNOWN": "未知",
                 "NOT_APPLICABLE": "不适用", "BLOCKED": "不可比", "UNPAIRED": "无配对",
                 "INCOMPLETE": "数据不完整", "MIXED": "有改善也有退化", "BOTH_SUCCESS": "双方成功",
                 "RECOVERED": "执行恢复成功", "LOST_SUCCESS": "候选未成功", "BOTH_NOT_SUCCESS": "双方未成功"}
require = score_eval.require


def finite_json(value):
    if isinstance(value, dict):
        for item in value.values():
            finite_json(item)
    elif isinstance(value, list):
        for item in value:
            finite_json(item)
    elif type(value) is float:
        require(math.isfinite(value), "JSON包含非有限数值")


def equivalent(left, right):
    """复算浮点容差1e-12；布尔值不冒充数字，结构/字段集合不得改变。"""
    if type(left) in (int, float) and type(right) in (int, float):
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equivalent(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
    return left == right


def read_json(path, hashes):
    raw = path.read_bytes()
    hashes[path] = score_eval.digest(raw)
    value = score_eval.decode(raw)
    finite_json(value)
    return value


@dataclass
class Batch:
    path: Path
    source: dict
    manifest: dict
    cases: list
    rows: list
    groups: dict
    snapshot: dict
    records: dict
    hashes: dict

    def selected(self, mode):
        return [r for r in self.rows if r["mode"] == mode]


def load_batch(path, dataset_path=None, snapshot_path=None):
    path = Path(path).resolve()
    require(path.parent.name == "scores", "score-dir须为运行目录下scores/<score_id>的完整批次")
    hashes = {}
    manifest = read_json(path / "manifest.json", hashes)
    require(isinstance(manifest, dict) and type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1 and manifest.get("status") in
            {"COMPLETED", "COMPLETED_WITH_UNAVAILABLE_METRICS"}, "评分批次未完成或格式不支持")
    require(manifest.get("score_id") == path.name and manifest.get("metric_policy_version") == metrics.POLICY_VERSION,
            "评分编号或指标版本不支持")
    live_code = {name: sha256_file(Path(__file__).with_name(name)) for name in SCORER_FILES}
    require(manifest.get("scorer_source_sha256") == live_code,
            "评分实现与当前可复核版本不同；请用原评分版本生成报告或显式重评，不自动混用")
    hashes.update({Path(__file__).with_name(name).resolve(): h for name, h in live_code.items()})
    metric_file = read_json(path / "metrics.json", hashes)
    raw = (path / "scores.jsonl").read_bytes()
    hashes[path / "scores.jsonl"] = score_eval.digest(raw)
    lines = raw.decode("utf-8").splitlines()
    require(all(line.strip() for line in lines), "评分记录有空行")
    rows = [score_eval.decode(line) for line in lines]
    finite_json(rows)
    require(manifest.get("artifact_sha256") == {name: hashes[path / name] for name in ("scores.jsonl", "metrics.json")},
            "评分产物哈希不匹配")
    run_dir = path.parent.parent
    source, cases, measured, pending, records, run_hashes = score_eval.read_run(run_dir, dataset_path, snapshot_path)
    finite_json(source)
    finite_json(records)
    hashes.update(run_hashes)
    require(manifest.get("source_sha256") == {p.name: h for p, h in run_hashes.items()}, "评分对应的执行产物发生变化")
    dataset_file = Path(dataset_path or source["inputs"]["dataset_path"]).resolve()
    snapshot_file = Path(snapshot_path or source["inputs"]["snapshot_path"]).resolve()
    dataset, snapshot, checked_cases, dataset_hash, snapshot_hash = load_inputs(dataset_file, snapshot_file)
    require(dataset_hash == source["dataset"]["manifest_sha256"] and snapshot_hash == source["snapshot"]["file_sha256"]
            and dataset.manifest["split"] == source["dataset"]["split"], "复核时题库/快照/split发生变化")
    require([sorted(c["_relevant"]) for c in checked_cases] == [sorted(c["_relevant"]) for c in cases], "金标准映射变化")
    hashes.update({dataset_file: dataset_hash, snapshot_file: snapshot_hash,
                   (dataset_file.parent / dataset.manifest["case_file"]).resolve(): dataset.manifest["cases_sha256"]})
    for entry in dataset.manifest["corpus"]:
        hashes[(dataset_file.parent / entry["file"]).resolve()] = entry["sha256"]
    modes, top_k = source["execution"]["modes"], source["execution"]["top_k"]
    grid = [(mode, case) for mode in modes for case in cases]
    require(len(rows) == len(grid), "评分丢失完整计划案例或有重复记录")
    for row, (mode, case) in zip(rows, grid):
        require(isinstance(row, dict), "评分记录不是对象")
        expected = score_eval.score_case(case, measured.get((mode, case["case_id"])), mode, top_k,
                                         unrecorded=pending == (mode, case["case_id"]))
        if row.get("score_status") == "SCORING_ERROR":
            error = row.get("scoring_error")
            require(expected["execution_status"] == "SUCCEEDED" and isinstance(error, dict)
                    and set(error) == {"code", "message"} and isinstance(error["code"], str) and error["code"]
                    and error["message"] == "评分实现异常", "评分故障记录非法")
            # 历史运行时故障不能假装复现成功；只复核其身份/状态及所有未知值。
            expected.update(score_status="SCORING_ERROR", scoring_error=error,
                            metrics={name: None for name in expected["metrics"]},
                            unavailable={name: "SCORING_ERROR" for name in score_eval.RANK_METRICS})
        require(equivalent(row, expected), "逐题评分与执行/金标准/当前口径不一致")
    groups = {mode: score_eval.aggregate([r for r in rows if r["mode"] == mode]) for mode in modes}
    require(equivalent(metric_file, {"schema_version": 1, "metric_policy_version": metrics.POLICY_VERSION, "by_mode": groups}),
            "评分汇总与完整逐题记录不一致")
    complete = all(g["quality_complete"] for g in groups.values())
    checks = {"run_id": source["run_id"], "source_run_status": source["status"],
              "dataset_id": dataset.manifest["dataset_id"], "dataset_version": dataset.manifest["dataset_version"],
              "dataset_split": dataset.manifest["split"], "dataset_manifest_sha256": dataset_hash,
              "snapshot_file_sha256": snapshot_hash, "expected_measure_executions": len(grid),
              "recorded_measure_executions": len(measured),
              "excluded_warmup_executions": sum(r["phase"] == "WARMUP" for r in records),
              "warmup_outcome_counts": dict(Counter(r["outcome"]["status"] for r in records if r["phase"] == "WARMUP")),
              "quality_complete": complete, "production_answer_quality_evaluated": False,
              "status": "COMPLETED" if complete else "COMPLETED_WITH_UNAVAILABLE_METRICS"}
    require(all(equivalent(manifest.get(k), v) for k, v in checks.items()), "评分清单的身份/分母/完成状态不一致")
    for chunk in snapshot["chunks"]:
        for key in INDEX_FIELDS:
            value = chunk.get(key)
            if value is None:
                continue
            if key in {"hasEmbedding", "hasSearchVector"}:
                valid = type(value) is bool
            elif key == "tokenCount":
                valid = type(value) is int and value >= 0
            else:
                valid = isinstance(value, str)
            require(valid, "快照索引字段类型无效")
    index_metadata = {key: sorted({c[key] for c in snapshot["chunks"] if isinstance(c.get(key), str)})
                      for key in ("embeddingModel", "embeddingVersion", "indexVersion", "tokenizerVersion")}
    require(source["snapshot"]["index_metadata"] == index_metadata, "执行清单的索引元数据与快照不一致")
    return Batch(path, source, manifest, cases, rows, groups, snapshot,
                 {r["sequence"]: r for r in records}, hashes)


def grouped(rows):
    return {"overall": score_eval.aggregate(rows),
            "by_case_type": {kind: score_eval.aggregate([r for r in rows if r["case_type"] == kind])
                             for kind in sorted({r["case_type"] for r in rows})},
            "by_tag": {tag: score_eval.aggregate([r for r in rows if tag in r["tags"]])
                       for tag in sorted({t for r in rows for t in r["tags"]})}}


def reference(batch, label):
    token_counts = [c["tokenCount"] for c in batch.snapshot["chunks"] if c.get("tokenCount") is not None]
    return {"label": label, "score_dir": str(batch.path), "run_id": batch.source["run_id"],
            "score_id": batch.manifest["score_id"], "run_status": batch.source["status"],
            "dataset": batch.source["dataset"], "snapshot": batch.source["snapshot"],
            "execution": batch.source["execution"], "target": batch.source["target"],
            "index_availability": {key: dict(Counter(str(c.get(key)) for c in batch.snapshot["chunks"]))
                                   for key in ("hasEmbedding", "hasSearchVector")},
            "token_count_range": {"minimum": min(token_counts) if token_counts else None,
                                  "maximum": max(token_counts) if token_counts else None, "observed_count": len(token_counts)},
            "runner_source_sha256": batch.source["runner_source_sha256"],
            "source_run_sha256": batch.manifest["source_sha256"],
            "metric_policy_version": batch.manifest["metric_policy_version"],
            "excluded_warmup_executions": batch.manifest["excluded_warmup_executions"],
            "warmup_outcome_counts": batch.manifest["warmup_outcome_counts"],
            "quality_complete": batch.manifest["quality_complete"]}


def case_view(batch, row):
    case = next(c for c in batch.cases if c["case_id"] == row["case_id"])
    record = None
    # 排名只用于诊断；不用响应score重排、不复制全文证据或错误正文。
    if row["execution_sequence"] is not None:
        record = batch.records.get(row["execution_sequence"])
    ranked = [r["chunkId"] for r in record["outcome"]["body"]["results"]] if record and row["execution_status"] == "SUCCEEDED" else None
    return {"case_id": case["case_id"], "query": case["query"], "request_conditions": case["request"],
            "case_type": case["type"], "tags": case["tags"], "mode": row["mode"],
            "gold_chunk_ids": sorted(case["_relevant"]), "ranked_chunk_ids": ranked,
            "first_relevant_rank": next((i for i, chunk in enumerate(ranked or [], 1) if chunk in case["_relevant"]), None),
            "score": row}


def evidence_signature(batch):
    # 容许索引策略变化，但文本、chunk身份/切分、文档版本及权限等必须相同。
    documents = sorted(({k: v for k, v in d.items() if k not in INDEX_FIELDS} for d in batch.snapshot["documents"]),
                       key=lambda d: d["documentId"])
    chunks = sorted(({k: v for k, v in c.items() if k not in INDEX_FIELDS} for c in batch.snapshot["chunks"]),
                    key=lambda c: (c["documentId"], c["chunkIndex"], c["chunkId"]))
    return canonical_hash({"documents": documents, "chunks": chunks})


def compatibility(baseline, candidate, baseline_mode, mode):
    blockers, warnings, latency_blockers = [], [], []
    if baseline.source["dataset"] != candidate.source["dataset"]:
        blockers.append("DATASET_CHANGED")
    if baseline.manifest["scorer_source_sha256"] != candidate.manifest["scorer_source_sha256"]:
        blockers.append("SCORER_CHANGED")
    if baseline.source["runner_source_sha256"] != candidate.source["runner_source_sha256"]:
        blockers.append("RUNNER_CHANGED")
    if evidence_signature(baseline) != evidence_signature(candidate):
        blockers.append("CORPUS_OR_CHUNK_IDENTITY_CHANGED")
    gold_a = {c["case_id"]: sorted(c["_relevant"]) for c in baseline.cases}
    gold_b = {c["case_id"]: sorted(c["_relevant"]) for c in candidate.cases}
    if gold_a != gold_b:
        blockers.append("GOLD_CHUNKS_CHANGED")
    if baseline.source["snapshot"]["corpus_hash"] != candidate.source["snapshot"]["corpus_hash"]:
        warnings.append("SNAPSHOT_CONTENT_OR_INDEX_METADATA_CHANGED")
    if baseline.source["snapshot"]["snapshot_id"] != candidate.source["snapshot"]["snapshot_id"]:
        warnings.append("SNAPSHOT_EXPORT_ID_CHANGED")
    a, b = baseline.source["execution"], candidate.source["execution"]
    if a["top_k"] != b["top_k"]:
        warnings.append("REQUEST_TOPK_CHANGED_CHECK_EACH_METRIC_COVERAGE")
    if a["warmup"] != b["warmup"]:
        latency_blockers.append("WARMUP_CHANGED")
    if a["timeout_seconds"] != b["timeout_seconds"]:
        latency_blockers.append("TIMEOUT_CHANGED")
    if any(any(status != "SUCCEEDED" for status in batch.manifest["warmup_outcome_counts"])
           for batch in (baseline, candidate)):
        latency_blockers.append("WARMUP_FAILURE_CACHE_STATE_UNVERIFIED")
    if len(a["modes"]) > 1 or len(b["modes"]) > 1:
        if a["modes"] != b["modes"] or a["modes"].index(baseline_mode) != b["modes"].index(mode):
            latency_blockers.append("MODE_ORDER_OR_CACHE_CONTEXT_CHANGED")
    success_a = {r["case_id"] for r in baseline.selected(baseline_mode) if r["execution_status"] == "SUCCEEDED"}
    success_b = {r["case_id"] for r in candidate.selected(mode) if r["execution_status"] == "SUCCEEDED"}
    if success_a != success_b or len(success_a) != len(baseline.cases) or len(success_b) != len(candidate.cases):
        latency_blockers.append("INCOMPLETE_OR_DIFFERENT_SUCCESS_COHORT")
    if baseline.source["target"] != candidate.source["target"]:
        warnings.append("TARGET_OR_DECLARED_CONFIG_CHANGED_NO_CAUSAL_ATTRIBUTION")
    if not baseline.source["target"].get("declared_config") or not candidate.source["target"].get("declared_config"):
        warnings.append("TARGET_CONFIG_NOT_DECLARED")
    if baseline.manifest["source_sha256"] == candidate.manifest["source_sha256"]:
        warnings.append("SAME_EXECUTION_NOT_AN_INDEPENDENT_EXPERIMENT")
    warnings.append("REMOTE_CONFIG_AND_HARDWARE_NOT_VERIFIED")
    return {"recorded_inputs_comparable": not blockers, "blockers": blockers, "warnings": warnings,
            "latency_blockers": blockers + latency_blockers,
            "scope": "RECORDED_DATA_ONLY_NOT_PROOF_OF_FAIR_ENVIRONMENT",
            "overall_quality_verdict": "NOT_PROVIDED", "statistical_significance": "NOT_TESTED"}


def delta(a, b, higher, blocked=False, applicable=True):
    value = None if blocked or a is None or b is None or not applicable else b-a
    change = ("NOT_APPLICABLE" if not applicable else "BLOCKED" if blocked else "UNKNOWN" if value is None
              else "UNCHANGED" if abs(value) <= 1e-12 else "IMPROVED" if (value > 0) == higher else "REGRESSED")
    return {"baseline": a, "candidate": b, "delta": value, "higher_is_better": higher, "change": change}


def quality_deltas(a, b, blocked):
    return {name: {**delta(a[name]["value"], b[name]["value"], higher, blocked,
                           bool(a[name]["expected_count"] and b[name]["expected_count"])),
                   "baseline_coverage": a[name], "candidate_coverage": b[name]}
            for name, (_, higher) in QUALITY.items()}


def paired_change(a, b, blocked):
    positive_a, positive_b = a["case_type"] != "N", b["case_type"] != "N"
    changes = {name: delta(a["metrics"][field], b["metrics"][field], higher, blocked,
                          (positive_a and positive_b) if name != "negative_no_evidence_rate" else not positive_a and not positive_b)
               for name, (field, higher) in QUALITY.items()}
    states = {d["change"] for d in changes.values()} - {"NOT_APPLICABLE"}
    quality = ("BLOCKED" if blocked else "INCOMPLETE" if "UNKNOWN" in states else
               "MIXED" if {"IMPROVED", "REGRESSED"} <= states else "IMPROVED" if "IMPROVED" in states else
               "REGRESSED" if "REGRESSED" in states else "UNCHANGED")
    sa, sb = a["execution_status"] == "SUCCEEDED", b["execution_status"] == "SUCCEEDED"
    engineering = "BOTH_SUCCESS" if sa and sb else "RECOVERED" if sb else "LOST_SUCCESS" if sa else "BOTH_NOT_SUCCESS"
    return {"quality_change": quality, "engineering_change": engineering, "metrics": changes}


def group_comparison(a, b, blocked):
    baseline, candidate = score_eval.aggregate(a), score_eval.aggregate(b)
    return {"baseline": baseline, "candidate": candidate,
            "quality_deltas": quality_deltas(baseline["quality"], candidate["quality"], blocked)}


def build_summary(candidate, label, modes, baseline=None, baseline_label=None, baseline_mode=None):
    common = {"schema_version": 1, "report_policy_version": POLICY_VERSION,
              "kind": "COMPARISON" if baseline else "SINGLE_RUN",
              "candidate": reference(candidate, label), "answer_quality_evaluated": False,
              "warnings": ["本报告不提供自动发布门禁、统计显著性或强因果结论。",
                           "未知指标不按0分处理；子集均值不是完整得分，上下界不是置信区间。",
                           "标签可重叠，按标签分组的样本数不能相加作为总数。",
                           "基线由操作者选择；标签不证明已验收或代表生产配置。",
                           "耗时仅为HTTP观测；远端配置、环境及缓存状态未经独立验证。"]}
    if candidate.source["dataset"]["split"] != "acceptance":
        common["warnings"].append("当前题库为development开发集，不是冻结验收集。")
    if baseline and baseline.source["dataset"]["split"] != "acceptance":
        common["warnings"].append("基线题库为development开发集，不是冻结验收集。")
    if baseline is None:
        return {**common, "by_mode": {m: grouped(candidate.selected(m)) for m in modes},
                "cases": [case_view(candidate, r) for r in candidate.rows if r["mode"] in modes]}
    mode = modes[0]
    policy = compatibility(baseline, candidate, baseline_mode, mode)
    a, b = baseline.selected(baseline_mode), candidate.selected(mode)
    rows_a, rows_b = {r["case_id"]: r for r in a}, {r["case_id"]: r for r in b}
    paired = []
    for case_id in list(rows_a) + [key for key in rows_b if key not in rows_a]:
        ra, rb = rows_a.get(case_id), rows_b.get(case_id)
        change = paired_change(ra, rb, not policy["recorded_inputs_comparable"]) if ra and rb else {
            "quality_change": "UNPAIRED", "engineering_change": "UNPAIRED", "metrics": {}}
        paired.append({"case_id": case_id, "baseline": case_view(baseline, ra) if ra else None,
                       "candidate": case_view(candidate, rb) if rb else None, **change})
    group = group_comparison(a, b, not policy["recorded_inputs_comparable"])
    engineering = {name: delta(group["baseline"][name]["value"], group["candidate"][name]["value"],
                              name == "success_rate", not policy["recorded_inputs_comparable"])
                   for name in ("success_rate", "reported_degradation_rate")}
    latency = {key: {q: delta(group["baseline"][key][q], group["candidate"][key][q], False,
                             bool(policy["latency_blockers"])) for q in ("p50", "p95")}
               for key in ("latency_success_seconds",)}
    by_type = {kind: group_comparison([r for r in a if r["case_type"] == kind], [r for r in b if r["case_type"] == kind],
                                     not policy["recorded_inputs_comparable"])
               for kind in sorted({r["case_type"] for r in a+b})}
    by_tag = {tag: group_comparison([r for r in a if tag in r["tags"]], [r for r in b if tag in r["tags"]],
                                   not policy["recorded_inputs_comparable"])
              for tag in sorted({t for r in a+b for t in r["tags"]})}
    return {**common, "baseline": reference(baseline, baseline_label), "baseline_mode": baseline_mode,
            "candidate_mode": mode, "compatibility": policy, "overall": group, "engineering_deltas": engineering,
            "latency_deltas_descriptive_only": latency, "by_case_type": by_type, "by_tag": by_tag,
            "cases": paired, "case_change_counts": dict(Counter(r["quality_change"] for r in paired)),
            "engineering_change_counts": dict(Counter(r["engineering_change"] for r in paired)),
            "metric_change_counts": {name: {state: sum(r["metrics"].get(name, {}).get("change", "UNPAIRED") == state for r in paired)
                                                   for state in CHANGES} for name in QUALITY}}


def md(value):
    text = str(value)
    text = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", text)


def number(value):
    return "未知/不适用" if value is None else f"{value:.6g}"


def metric_table(group):
    lines = ["| 指标 | 正式均值 | 已评分/完整分母 | 未知值界限（非置信区间） |",
             "|---|---:|---:|---|"]
    for name, stat in group["quality"].items():
        lines.append(f"| {md(name)} | {number(stat['value'])} | {stat['scored_count']}/{stat['expected_count']} | {md(stat['full_set_bounds'])} |")
    lines += [f"\nHTTP/协议成功率：{number(group['success_rate']['value'])}；声明降级率：{number(group['reported_degradation_rate']['value'])}。",
              f"成功请求耗时（秒）：P50={number(group['latency_success_seconds']['p50'])}，P95={number(group['latency_success_seconds']['p95'])}，样本={group['latency_success_seconds']['sample_count']}。",
              f"执行状态：{md(group['execution_status_counts'])}；评分状态：{md(group['score_status_counts'])}。"]
    return lines


def render_report(summary):
    candidate = summary["candidate"]
    lines = ["# 检索评测报告", "", f"类型：{summary['kind']}；候选/当前实验：{md(candidate['label'])}。",
             f"题库：{md(candidate['dataset']['dataset_id'])} / {md(candidate['dataset']['dataset_version'])}；split={md(candidate['dataset']['split'])}。",
             f"运行：{md(candidate['run_id'])}；评分批次：{md(candidate['score_id'])}；原运行状态：{md(candidate['run_status'])}。",
             f"执行配置：{md(candidate['execution'])}。",
             f"索引元数据：{md(candidate['snapshot']['index_metadata'])}。",
             f"索引覆盖声明：{md(candidate['index_availability'])}；tokenCount范围：{md(candidate['token_count_range'])}。",
             f"目标与操作者配置声明（未验证远端）：{md(candidate['target'])}。",
             f"暖机状态计数：{md(candidate['warmup_outcome_counts'])}。",
             "", "## 阅读限制", *[f"- {md(w)}" for w in summary["warnings"]]]
    if summary["kind"] == "SINGLE_RUN":
        for mode, groups in summary["by_mode"].items():
            lines += ["", f"## {mode}：整体", *metric_table(groups["overall"])]
            for dimension in ("by_case_type", "by_tag"):
                for name, group in groups[dimension].items():
                    lines += ["", f"### {dimension} / {md(name)}", *metric_table(group)]
        lines += ["", "## 全部正式案例（含失败/未知）", "| 模式 | 案例 | 类型 | 问题 | 执行状态 | 评分状态 | 无证据声明/可判定 | Recall@5 | MRR@10 | nDCG@10 |",
                  "|---|---|---|---|---|---|---|---:|---:|---:|"]
        for entry in summary["cases"]:
            row = entry["score"]
            lines.append("| " + " | ".join([entry["mode"], md(entry["case_id"]), row["case_type"], md(entry["query"]),
                                              row["execution_status"], row["score_status"],
                                              f"{row['observed_no_evidence']}/{row['no_evidence_reliable']}",
                                              *["不适用" if row["case_type"] == "N" else number(row["metrics"][name])
                                                for name in score_eval.RANK_METRICS]]) + " |")
    else:
        policy = summary["compatibility"]
        lines += ["", "## 比较条件", f"基线：{md(summary['baseline']['label'])} / {summary['baseline_mode']}；候选：{summary['candidate_mode']}。",
                  f"基线题库：{md(summary['baseline']['dataset']['dataset_id'])} / {md(summary['baseline']['dataset']['dataset_version'])} / {md(summary['baseline']['dataset']['split'])}。",
                  f"基线执行配置：{md(summary['baseline']['execution'])}。",
                  f"基线索引元数据：{md(summary['baseline']['snapshot']['index_metadata'])}。",
                  f"基线索引覆盖声明：{md(summary['baseline']['index_availability'])}；tokenCount范围：{md(summary['baseline']['token_count_range'])}。",
                  f"基线目标与配置声明：{md(summary['baseline']['target'])}。",
                  f"基线暖机状态计数：{md(summary['baseline']['warmup_outcome_counts'])}。",
                  f"按留档信息可比：{policy['recorded_inputs_comparable']}（不是环境公平性证明）。",
                  f"阻断项：{md(policy['blockers'])}。", f"警告：{md(policy['warnings'])}。",
                  f"耗时比较阻断项：{md(policy['latency_blockers'])}。", "不输出单一总分或“整体显著提升”结论。",
                  "", "## 基线整体", *metric_table(summary["overall"]["baseline"]),
                  "", "## 候选整体", *metric_table(summary["overall"]["candidate"])]
        def changes_table(deltas):
            result = ["| 指标 | 基线 | 候选 | 差值（候选-基线） | 判断 |", "|---|---:|---:|---:|---|"]
            for name, value in deltas.items():
                result.append(f"| {md(name)} | {number(value['baseline'])} | {number(value['candidate'])} | {number(value['delta'])} | {CHANGE_LABELS[value['change']]} |")
            return result
        lines += ["", "## 完整分母的指标变化", *changes_table(summary["overall"]["quality_deltas"]),
                  "", "## 工程变化（不等同通路健康）", *changes_table(summary["engineering_deltas"]),
                  "", "## 耗时变化（仅描述性观测）", *changes_table(summary["latency_deltas_descriptive_only"]["latency_success_seconds"])]
        for dimension in ("by_case_type", "by_tag"):
            for name, group in summary[dimension].items():
                lines += ["", f"### {dimension} / {md(name)}", *changes_table(group["quality_deltas"])]
        lines += ["", f"质量变化计数：{md(summary['case_change_counts'])}。",
                  f"工程变化计数：{md(summary['engineering_change_counts'])}。",
                  "", "## 全部案例配对", "| 案例 | 问题 | 基线执行 | 候选执行 | 质量变化 | 工程变化 | 指标差值/未知 |",
                  "|---|---|---|---|---|---|---|"]
        for entry in summary["cases"]:
            a, b = entry["baseline"], entry["candidate"]
            changes = "; ".join(f"{name}: {number(value['delta'])} ({CHANGE_LABELS[value['change']]})" for name, value in entry["metrics"].items())
            lines.append("| " + " | ".join([md(entry["case_id"]), md((b or a)["query"]),
                                              a["score"]["execution_status"] if a else "UNPAIRED",
                                              b["score"]["execution_status"] if b else "UNPAIRED", CHANGE_LABELS[entry["quality_change"]],
                                              CHANGE_LABELS[entry["engineering_change"]], md(changes)]) + " |")
    lines += ["", "## 回查", "summary.json包含条件、gold/返回chunkId、首个正确证据排名、逐题状态、覆盖及来源路径。原请求/响应留在原运行目录；本报告不复制全文证据。",
              "", "报告不修改原执行或评分数据；Langfuse不是生成这些指标和报告的必要条件。", ""]
    return "\n".join(lines)


def choose_mode(batch, explicit):
    modes = batch.source["execution"]["modes"]
    require(explicit in modes if explicit else len(modes) == 1, "比较多模式批次须明确选择mode，不自动挑最好/最新模式")
    return explicit or modes[0]


def verify_unchanged(hashes):
    require(all(sha256_file(path) == expected for path, expected in hashes.items()), "报告生成期间输入/源码发生变化")


def publish(destination, summary, manifest, hashes, redactor):
    verify_unchanged(hashes)
    require(equivalent(redactor.value(manifest), manifest), "凭据脱敏会损坏清单身份/哈希；请勿在编号/路径使用凭据")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    def write_json(name, data, *, redact=True):
        with (destination / name).open("x", encoding="utf-8", newline="\n") as f:
            json.dump(redactor.value(data) if redact else data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    write_json("manifest.json", {**manifest, "status": "PREPARING"})
    sanitized = redactor.value(summary)
    write_json("summary.json", sanitized, redact=False)
    with (destination / "report.md").open("x", encoding="utf-8", newline="\n") as f:
        f.write(render_report(sanitized))
        f.flush()
        os.fsync(f.fileno())
    manifest["artifact_sha256"] = {name: sha256_file(destination / name) for name in ("summary.json", "report.md")}
    require(equivalent(redactor.value(manifest), manifest), "凭据脱敏会损坏产物哈希；未发布完成清单")
    verify_unchanged(hashes)
    write_json("manifest.json.tmp", {**manifest, "status": "COMPLETED"})
    os.replace(destination / "manifest.json.tmp", destination / "manifest.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    ap.add_argument("--score-dir", required=True, type=Path)
    ap.add_argument("--baseline-score-dir", type=Path)
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--baseline-mode", choices=MODES)
    ap.add_argument("--dataset", type=Path)
    ap.add_argument("--snapshot", type=Path)
    ap.add_argument("--baseline-dataset", type=Path)
    ap.add_argument("--baseline-snapshot", type=Path)
    ap.add_argument("--label", default="当前实验")
    ap.add_argument("--baseline-label", default="操作者指定基线")
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_REPORTS)
    ap.add_argument("--report-id")
    args = ap.parse_args(argv)
    redactor = Redactor(*(os.environ.get(name, "") for name in
                          ("KNOWLEDGE_QUERY_KEY", "KNOWLEDGE_MANAGEMENT_KEY", "KNOWLEDGE_EVAL_DATABASE_URL")))
    try:
        require(args.baseline_score_dir is not None or not any((args.baseline_mode, args.baseline_dataset, args.baseline_snapshot)),
                "基线参数须配合baseline-score-dir")
        require(all(isinstance(s, str) and s.strip() and len(s) <= 120 for s in (args.label, args.baseline_label)), "label须为1..120字符")
        report_id = args.report_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
        require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", report_id)), "report-id格式无效")
        candidate = load_batch(args.score_dir, args.dataset, args.snapshot)
        baseline = load_batch(args.baseline_score_dir, args.baseline_dataset, args.baseline_snapshot) if args.baseline_score_dir else None
        batches = [candidate] + ([baseline] if baseline else [])
        hashes = {}
        for batch in batches:
            custom_key = batch.source.get("target", {}).get("key_env")
            if isinstance(custom_key, str):
                redactor = Redactor(*redactor.secrets, os.environ.get(custom_key, ""))
            for path, expected in batch.hashes.items():
                require(path not in hashes or hashes[path] == expected, "同一路径的两个输入版本冲突")
                hashes[path] = expected
        hashes[Path(__file__).resolve()] = sha256_file(Path(__file__))
        if baseline:
            modes = [choose_mode(candidate, args.mode)]
            baseline_mode = choose_mode(baseline, args.baseline_mode)
        else:
            require(args.mode is None or args.mode in candidate.groups, "所选模式不在评分批次")
            modes = [args.mode] if args.mode else list(candidate.groups)
            baseline_mode = None
        summary = build_summary(candidate, args.label, modes, baseline, args.baseline_label, baseline_mode)
        destination = (args.output_dir / report_id).resolve()
        require(all(not destination.is_relative_to(b.path.parent.parent) for b in batches), "报告必须输出到原运行目录之外")
        manifest = {"schema_version": 1, "report_id": report_id, "report_policy_version": POLICY_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(), "kind": summary["kind"],
                    "runtime": {"python": platform.python_version(), "platform": platform.system()},
                    "inputs_sha256": {str(p): h for p, h in hashes.items()},
                    "selected_modes": modes, "baseline_mode": baseline_mode,
                    "source_roles": "OPERATOR_SELECTED_NOT_AUTOMATIC_ACCEPTANCE",
                    "quality_comparison_blocked": bool(baseline and summary["compatibility"]["blockers"]),
                    "answer_quality_evaluated": False, "release_gate_evaluated": False}
        publish(destination, summary, manifest, hashes, redactor)
        blocked = manifest["quality_comparison_blocked"]
        unavailable = (any(not candidate.groups[m]["quality_complete"] for m in modes) or
                       bool(baseline and not baseline.groups[baseline_mode]["quality_complete"]))
        print(redactor.text(f"报告已保存：{destination}；{'比较受阻' if blocked else '有未知指标' if unavailable else '报告完成'}。不是发布通过结论。"))
        return 1 if blocked or unavailable else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(redactor.text(f"报告中止：{exc}"), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"报告工具异常：{type(exc).__name__}；未发布完成清单", file=sys.stderr)
        return 2


if __name__ == "__main__":
    configure_console()
    raise SystemExit(main())
