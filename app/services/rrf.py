"""RRF 融合（设计文档 §5.7）。

关键词和向量分别召回并排序，按 RRF 融合、按 chunkId 去重，再返回 topK。
不直接相加两种通路的原始分数。RRF 分数不是概率或置信度。
候选数量与 RRF 参数经样本验证后配置。
"""

from __future__ import annotations

from dataclasses import dataclass, field

RRF_K = 60  # 经典 RRF 常数，经样本验证后可调整


@dataclass
class RankedItem:
    chunk_id: str
    item: object  # SearchResultItem
    rank: int
    score: float


def rrf_fuse(
    *ranked_lists: list[RankedItem],
    top_k: int = 5,
    k: int = RRF_K,
) -> list[RankedItem]:
    """按 RRF 融合多个排序列表，按 chunkId 去重，返回 topK。

    RRF 公式：score(d) = Σ 1 / (k + rank_i(d))
    """
    fused: dict[str, dict] = {}
    for ranked in ranked_lists:
        for entry in ranked:
            slot = fused.setdefault(
                entry.chunk_id,
                {"item": entry.item, "score": 0.0, "best_rank": entry.rank},
            )
            slot["score"] += 1.0 / (k + entry.rank)
            slot["best_rank"] = min(slot["best_rank"], entry.rank)

    merged = [
        RankedItem(
            chunk_id=cid,
            item=slot["item"],
            rank=slot["best_rank"],
            score=slot["score"],
        )
        for cid, slot in fused.items()
    ]
    merged.sort(key=lambda e: e.score, reverse=True)
    return merged[:top_k]
