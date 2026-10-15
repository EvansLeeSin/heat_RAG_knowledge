"""检索服务（设计文档 §4.1、§5.6、§5.7）。

关键词通路（jieba + tsvector + GIN）在召回候选时应用
权限（公共/项目范围）、默认版本及适用范围条件，不在最终 topK 后过滤。

向量通路与 RRF 融合在阶段四实现。
RRF 分数不是概率或置信度；无可用证据返回空结果。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.embedding import EmbeddingError, embed_query
from app.services.rrf import RankedItem, rrf_fuse
from app.services.tokenizer import tokenize_to_query

logger = logging.getLogger(__name__)


@dataclass
class SearchRequest:
    query: str
    region: str | None = None
    documentTypes: list[str] = field(default_factory=list)
    effectiveStatus: list[str] = field(default_factory=list)
    authorizedProjectIds: list[str] = field(default_factory=list)
    topK: int = 5
    # 检索通路：HYBRID（默认，双通路 RRF 融合）/ KEYWORD（仅关键词）/ VECTOR（仅向量）
    retrievalMode: str = "HYBRID"


@dataclass
class SearchResultItem:
    chunkId: str
    documentId: str
    documentVersion: int
    title: str
    sectionPath: str | None
    articleNo: str | None
    content: str
    sourceUrl: str | None
    publisher: str | None
    publishedAt: str | None
    effectiveStatus: str
    applicability: str | None
    retrievalMethod: str
    scoreType: str
    score: float


def _scope_filter(request: SearchRequest) -> str:
    """公共/项目范围过滤（两条通路均在召回时应用，不越权）。"""
    if request.authorizedProjectIds:
        # 公共文档或与授权项目集合存在交集的项目文档
        # psycopg3 下必须显式 CAST：`array[:project_ids]` 会被 SQLAlchemy 渲染成二维数组
        # （ARRAY['PRJ-A']），`?|` 永远匹配失败，导致 PROJECT 文档对**所有**调用方不可检索。
        return (
            " AND (d.access_scope = 'PUBLIC' OR "
            "(d.access_scope = 'PROJECT' AND d.project_ids ?| CAST(:project_ids AS text[])))"
        )
    # 空项目集合只能检索公共资料
    return " AND d.access_scope = 'PUBLIC'"


def _effective_status_filter(request: SearchRequest) -> str:
    """仅在调用方显式给出状态时限制文档有效状态。"""
    if not request.effectiveStatus:
        return ""
    return " AND d.effective_status = ANY(CAST(:effective_status AS VARCHAR[]))"


def _document_type_filter(request: SearchRequest) -> str:
    """仅在调用方显式给出类型时限制文档类型，且在候选排序前过滤。"""
    if not request.documentTypes:
        return ""
    return " AND d.document_type = ANY(CAST(:document_types AS VARCHAR[]))"


def _region_filter(request: SearchRequest) -> str:
    """按文档明确标注的地域精确过滤；region 为 NULL 时不会匹配。"""
    if not request.region:
        return ""
    return " AND d.region = :region"


def vector_search(session: Session, request: SearchRequest, candidate_k: int | None = None) -> list[RankedItem]:
    """向量通路：外部 Embedding → pgvector cosine 召回，召回时应用过滤（§4.1）。

    Embedding 未配置或调用失败时返回空列表，由上层明确提示，不伪装为无结果。
    """
    if not request.query.strip():
        return []
    try:
        query_vector = embed_query(request.query)
    except EmbeddingError as exc:
        logger.warning("vector channel skipped: %s", exc)
        return []
    k = candidate_k or max(request.topK * 3, 20)

    sql = text(
        "SELECT c.chunk_id, d.document_id, d.version, d.title, c.section_path, c.article_no, "
        " c.content, d.source_url, d.publisher, d.published_at, d.effective_status, d.applicability, "
        " e.vector <=> CAST(:vec AS vector) AS distance "
        "FROM knowledge_chunk c "
        "JOIN knowledge_embedding e ON e.chunk_id = c.chunk_id "
        "JOIN document_active_version a ON a.document_id = c.document_id AND a.active_version = c.version "
        "JOIN knowledge_document d ON d.document_id = c.document_id AND d.version = c.version "
        "WHERE 1 = 1"
        + _scope_filter(request)
        + _effective_status_filter(request)
        + _document_type_filter(request)
        + _region_filter(request)
        + " ORDER BY distance ASC LIMIT :k"
    )
    params = {
        "vec": str(query_vector),
        "k": k,
        "project_ids": request.authorizedProjectIds or [""],
    }
    if request.effectiveStatus:
        params["effective_status"] = request.effectiveStatus
    if request.documentTypes:
        params["document_types"] = request.documentTypes
    if request.region:
        params["region"] = request.region
    rows = session.execute(sql, params).mappings().all()

    ranked = []
    for rank, row in enumerate(rows, start=1):
        item = _to_item(row, retrieval_method="VECTOR", score_type="COSINE_DISTANCE", score=float(row["distance"]))
        ranked.append(RankedItem(chunk_id=item.chunkId, item=item, rank=rank, score=float(row["distance"])))
    return ranked


def keyword_search(session: Session, request: SearchRequest, candidate_k: int | None = None) -> list[RankedItem]:
    """关键词通路：jieba 分词 → tsquery → GIN 索引召回，召回时应用过滤。"""
    if not request.query.strip():
        return []
    tsquery = tokenize_to_query(request.query)
    if not tsquery:
        return []
    k = candidate_k or max(request.topK * 3, 20)

    sql = text(
        "SELECT c.chunk_id, d.document_id, d.version, d.title, c.section_path, c.article_no, "
        " c.content, d.source_url, d.publisher, d.published_at, d.effective_status, d.applicability, "
        " ts_rank_cd(c.search_vector, to_tsquery('simple', :q), 32) AS rank "
        "FROM knowledge_chunk c "
        "JOIN document_active_version a ON a.document_id = c.document_id AND a.active_version = c.version "
        "JOIN knowledge_document d ON d.document_id = c.document_id AND d.version = c.version "
        "WHERE c.search_vector @@ to_tsquery('simple', :q)"
        + _scope_filter(request)
        + _effective_status_filter(request)
        + _document_type_filter(request)
        + _region_filter(request)
        + " ORDER BY rank DESC LIMIT :k"
    )
    params: dict = {
        "q": tsquery,
        "k": k,
        "project_ids": request.authorizedProjectIds or [""],
    }
    if request.effectiveStatus:
        params["effective_status"] = request.effectiveStatus
    if request.documentTypes:
        params["document_types"] = request.documentTypes
    if request.region:
        params["region"] = request.region
    rows = session.execute(sql, params).mappings().all()

    ranked = []
    for rank, row in enumerate(rows, start=1):
        item = _to_item(row, retrieval_method="KEYWORD", score_type="TS_RANK_CD", score=float(row["rank"]))
        ranked.append(RankedItem(chunk_id=item.chunkId, item=item, rank=rank, score=float(row["rank"])))
    return ranked


def exact_article_match(session: Session, request: SearchRequest, article_no: str) -> list[SearchResultItem]:
    """条款号、设备型号、标准编号精确匹配能力（§5.6）。"""
    sql = text(
        "SELECT c.chunk_id, d.document_id, d.version, d.title, c.section_path, c.article_no, "
        " c.content, d.source_url, d.publisher, d.published_at, d.effective_status, d.applicability "
        "FROM knowledge_chunk c "
        "JOIN document_active_version a ON a.document_id = c.document_id AND a.active_version = c.version "
        "JOIN knowledge_document d ON d.document_id = c.document_id AND d.version = c.version "
        "WHERE c.article_no = :article"
        + _scope_filter(request)
        + _effective_status_filter(request)
        + _document_type_filter(request)
        + _region_filter(request)
        + " LIMIT :k"
    )
    params = {
        "article": article_no,
        "k": request.topK,
        "project_ids": request.authorizedProjectIds or [""],
    }
    if request.effectiveStatus:
        params["effective_status"] = request.effectiveStatus
    if request.documentTypes:
        params["document_types"] = request.documentTypes
    if request.region:
        params["region"] = request.region
    rows = session.execute(sql, params).mappings().all()
    return [_to_item(row, retrieval_method="EXACT", score_type="EXACT_MATCH", score=1.0) for row in rows]


def _to_item(row, retrieval_method: str, score_type: str, score: float) -> SearchResultItem:
    from datetime import datetime

    published = row["published_at"]
    return SearchResultItem(
        chunkId=row["chunk_id"],
        documentId=row["document_id"],
        documentVersion=row["version"],
        title=row["title"],
        sectionPath=row["section_path"],
        articleNo=row["article_no"],
        content=row["content"],
        sourceUrl=row["source_url"],
        publisher=row["publisher"],
        publishedAt=published.isoformat() if isinstance(published, datetime) else None,
        effectiveStatus=row["effective_status"],
        applicability=row["applicability"],
        retrievalMethod=retrieval_method,
        scoreType=score_type,
        score=score,
    )


def _evidence_signals(
    keyword_ranked: list[RankedItem], vector_ranked: list[RankedItem]
) -> tuple[dict[str, float], dict[str, float]]:
    """按 chunkId 汇总两条通路的证据强度（取该 chunk 在各通路的最优值）。

    向量通路：score 为 cosine 距离（越小越近），转为相似度 similarity = 1 - distance；
    关键词通路：score 为 ts_rank_cd（越大越相关）。
    """
    similarity: dict[str, float] = {}
    for it in vector_ranked:
        sim = 1.0 - float(it.score)
        if sim > similarity.get(it.chunk_id, -1.0):
            similarity[it.chunk_id] = sim
    ts_rank: dict[str, float] = {}
    for it in keyword_ranked:
        rank = float(it.score)
        if rank > ts_rank.get(it.chunk_id, -1.0):
            ts_rank[it.chunk_id] = rank
    return similarity, ts_rank


def search(session: Session, request: SearchRequest) -> tuple[list[SearchResultItem], dict]:
    """首版检索入口：关键词 + 向量双通路，RRF 融合、按 chunkId 去重（§4.1、§5.7）。

    返回（融合结果, 检索元信息）。无相关证据时返回空结果；
    向量通路不可用时降级为关键词通路并在元信息中说明。
    """
    import re

    mode = (request.retrievalMode or "HYBRID").upper()
    if mode == "KEYWORD":
        keyword_ranked = keyword_search(session, request)
        vector_ranked = []
    elif mode == "VECTOR":
        keyword_ranked = []
        vector_ranked = vector_search(session, request)
    else:
        keyword_ranked = keyword_search(session, request)
        vector_ranked = vector_search(session, request)

    channels = [l for l in (keyword_ranked, vector_ranked) if l]
    meta = {"keywordHits": len(keyword_ranked), "vectorHits": len(vector_ranked)}

    if channels:
        fused = rrf_fuse(*channels, top_k=request.topK * 3)
        results = [entry.item for entry in fused]
        # 双路结果返回实际融合分数；RankedItem.score 保留原始通路信号供门槛判断。
        # 单路仍返回原关键词分数/向量距离，不改变已有接口含义。
        if len(channels) > 1:
            for entry in fused:
                item = entry.item
                item.retrievalMethod = "HYBRID"
                item.scoreType = "RRF"
                item.score = entry.score
        elif len(channels) == 1:
            only = channels[0]
            for item in results:
                item.retrievalMethod = "KEYWORD" if only is keyword_ranked else "VECTOR"
                item.scoreType = "TS_RANK_CD" if only is keyword_ranked else "COSINE_DISTANCE"
        if len(channels) == 1 and vector_ranked and not keyword_ranked and mode == "HYBRID":
            meta["degraded"] = "keyword channel empty"
        elif len(channels) == 1 and keyword_ranked and not vector_ranked and mode == "HYBRID":
            meta["degraded"] = "vector channel skipped"
    else:
        results = []

    # 条款号显式出现在查询中时，附加精确匹配结果（去重后置顶）
    m = re.search(r"第[一二三四五六七八九十百千0-9]+条", request.query)
    if m:
        existing = {r.content for r in results}
        for item in exact_article_match(session, request, m.group(0)):
            if item.content not in existing:
                results.insert(0, item)
                existing.add(item.content)

    # SQL 召回阶段已按状态过滤；最终再做一次防御性过滤，确保精确匹配或
    # 后续新增检索通路不会绕过调用方指定的有效状态。
    if request.effectiveStatus:
        allowed_statuses = set(request.effectiveStatus)
        results = [item for item in results if item.effectiveStatus in allowed_statuses]

    # ---- 无答案判断（设计文档 §5.5、§16 第 6 条）----
    # 向量近邻永远存在，不能仅因召回到近邻就认定有答案：
    # 以证据强度门槛过滤，两条通路都不达门槛时返回空结果（noEvidence=True）。
    if results:
        settings = get_settings()
        min_similarity = settings.no_evidence_min_similarity
        min_ts_rank = settings.no_evidence_min_ts_rank
        similarity, ts_rank = _evidence_signals(keyword_ranked, vector_ranked)

        accepted: list[SearchResultItem] = []
        for item in results:
            # 条款号精确匹配是显式证据，直接采纳
            if item.retrievalMethod == "EXACT":
                accepted.append(item)
                continue
            sim = similarity.get(item.chunkId)
            rank = ts_rank.get(item.chunkId)
            if sim is not None and sim >= min_similarity:
                accepted.append(item)
                continue
            if rank is not None and rank >= min_ts_rank:
                accepted.append(item)

        # bestSimilarity / bestTsRank 是过滤前的最佳证据强度，用于阈值标定与线上观测：
        # 阈值随 Embedding 模型变化，需要能观测到“未过滤时的真实分数”才能重新标定。
        meta["gate"] = {
            "minSimilarity": min_similarity,
            "minTsRank": min_ts_rank,
            "bestSimilarity": round(max(similarity.values()), 4) if similarity else None,
            "bestTsRank": round(max(ts_rank.values()), 4) if ts_rank else None,
            "passed": len(accepted),
            "filtered": len(results) - len(accepted),
        }
        if not accepted:
            meta["noEvidence"] = True
            logger.info(
                "no evidence for query=%r (candidates=%d, best similarity=%s, best ts_rank=%s)",
                request.query,
                len(results),
                max(similarity.values()) if similarity else None,
                max(ts_rank.values()) if ts_rank else None,
            )
        results = accepted
    else:
        # 两条通路都无命中，同样是无证据
        meta["noEvidence"] = True

    return results[: request.topK], meta
