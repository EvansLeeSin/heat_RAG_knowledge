"""检索指标 V1：按原始排名位置计分，重复chunk占位但不重复得分。纯离线。"""
from __future__ import annotations

import math

POLICY_VERSION = "retrieval-metrics-v1.0.0"


def _inputs(ranked, relevant, k):
    if type(k) is not int or k < 1:
        raise ValueError("指标截断k必须为正整数")
    if not isinstance(ranked, (list, tuple)) or not all(isinstance(x, str) and x for x in ranked):
        raise ValueError("排名必须为非空chunkId组成的列表（允许空列表）")
    if not isinstance(relevant, (list, tuple, set, frozenset)) or not all(isinstance(x, str) and x for x in relevant):
        raise ValueError("标准证据必须为chunkId集合（允许空集）")
    return set(relevant)


def recall_at_k(ranked, relevant, k=5) -> float | None:
    gold = _inputs(ranked, relevant, k)
    return len(set(ranked[:k]) & gold) / len(gold) if gold else None


def mrr_at_k(ranked, relevant, k=10) -> float | None:
    gold = _inputs(ranked, relevant, k)
    if not gold:
        return None
    return next((1.0 / rank for rank, chunk in enumerate(ranked[:k], 1) if chunk in gold), 0.0)


def ndcg_at_k(ranked, relevant, k=10) -> float | None:
    gold = _inputs(ranked, relevant, k)
    if not gold:
        return None
    seen, gains = set(), []
    for index, chunk in enumerate(ranked[:k]):
        if chunk in gold and chunk not in seen:
            gains.append(1.0 / math.log2(index + 2))
        seen.add(chunk)
    ideal = math.fsum(1.0 / math.log2(index + 2) for index in range(min(len(gold), k)))
    return math.fsum(gains) / ideal


def percentile(values, q) -> float | None:
    """线性插值分位数（type 7）：位置=(N-1)*q；P50为通常的中位数。"""
    if type(q) not in (int, float) or not math.isfinite(q) or not 0 <= q <= 1:
        raise ValueError("分位q必须为0..1的有限数值")
    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values):
        raise ValueError("耗时必须为非负有限数值")
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def bounded_mean(values) -> dict:
    """完整分母的[0,1]指标；未知值不删除，不冒充真实0/1，不只汇报成功子集均值。"""
    if not all(v is None or (type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1) for v in values):
        raise ValueError("质量指标必须为0..1或null")
    known = [v for v in values if v is not None]
    total, unknown = len(values), len(values) - len(known)
    return {"value": sum(known) / total if total and not unknown else None,
            "expected_count": total, "scored_count": len(known), "unscored_count": unknown,
            "complete": bool(total) and unknown == 0,
            "observed_subset_mean": sum(known) / len(known) if known else None,
            "full_set_bounds": [sum(known) / total, (sum(known) + unknown) / total] if total else None}


def degradation(meta: dict, mode: str) -> str:
    """仅解释服务声明，非通路故障检测器；主动禁用非选定通路不算降级。"""
    raw = meta.get("degraded")
    if not raw:
        return "NOT_REPORTED"
    inactive = {"KEYWORD": {"vector"}, "VECTOR": {"keyword"}, "HYBRID": set()}[mode]
    if isinstance(raw, dict) and all(k in inactive and v in ("disabled_by_mode", "disabled") for k, v in raw.items()):
        return "MODE_DISABLED_ONLY"
    return "REPORTED_DEGRADATION"


def reliable_no_evidence(body: dict, mode: str) -> tuple[bool, str | None]:
    """不把Embedding失败伪装出的空结果当作已验证的正确拒答。"""
    if not body["noEvidence"]:
        return True, None
    if degradation(body.get("meta", {}), mode) == "REPORTED_DEGRADATION":
        return False, "CHANNEL_STATE_AMBIGUOUS_OR_DEGRADED"
    if mode == "KEYWORD":
        return True, None
    # 当前服务vector_search把EmbeddingError转为空列表；没有gate时无法证明向量通路正常。
    meta = body.get("meta", {})
    gate = meta.get("gate")
    best = gate.get("bestSimilarity") if isinstance(gate, dict) else None
    if type(best) not in (int, float) or not math.isfinite(best) or not -1 <= best <= 1:
        return False, "VECTOR_EMPTY_HEALTH_UNVERIFIED"
    return True, None
