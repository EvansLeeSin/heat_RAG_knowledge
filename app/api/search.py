"""检索 API（设计文档 §9.3）。

authorizedProjectIds 是可信服务调用方声明的授权上下文，不是终端用户可自行设置的条件。
分数只用于排序，不代表置信度；无可用证据返回空结果；依赖故障明确返回错误。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_query_key
from app.core.database import get_session
from app.services.context import KnowledgeContext, build_context
from app.services.search import SearchRequest, SearchResultItem, search

router = APIRouter(prefix="/knowledge", tags=["search"])


class SearchBody(BaseModel):
    query: str = Field(min_length=1)
    region: str | None = None
    documentTypes: list[str] = Field(default_factory=list)
    effectiveStatus: list[str] = Field(default_factory=list)
    authorizedProjectIds: list[str] = Field(default_factory=list)
    topK: int = Field(default=5, ge=1, le=50)
    retrievalMode: str = Field(default="HYBRID", pattern="^(HYBRID|KEYWORD|VECTOR)$")
    contextCharBudget: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        description="可选的上下文正文总字符预算（Unicode码点）；不含元数据，不是token数",
    )


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultItem]
    # 无相关证据判断（设计文档 §5.5）：为 True 时 results 为空，调用方不应用近邻充当答案
    noEvidence: bool = False
    # 检索元信息：通路命中数、降级说明、置信门禁统计
    meta: dict = Field(default_factory=dict)
    # 仅在请求预算时返回；results 始终保留原始检索正文与分数。
    context: KnowledgeContext | None = None


@router.post(
    "/search",
    response_model=SearchResponse,
    response_model_exclude_unset=True,
    dependencies=[Depends(require_query_key)],
)
def search_knowledge(body: SearchBody, session: Session = Depends(get_session)) -> SearchResponse:
    request = SearchRequest(
        query=body.query,
        region=body.region,
        documentTypes=body.documentTypes,
        effectiveStatus=body.effectiveStatus,
        authorizedProjectIds=body.authorizedProjectIds,
        topK=body.topK,
        retrievalMode=body.retrievalMode,
    )
    try:
        results, meta = search(session, request)
        context = (
            build_context(results, body.contextCharBudget)
            if body.contextCharBudget is not None else None
        )
    except Exception as exc:
        # 依赖故障明确返回错误，不伪装为无结果
        raise HTTPException(status_code=503, detail="search backend unavailable") from exc
    response = SearchResponse(
        query=body.query,
        results=results,
        noEvidence=bool(meta.get("noEvidence")),
        meta=meta,
    )
    if context is not None:
        response.context = context
    # 检索降级说明同时通过响应头透出，便于网关/日志观测
    if meta.get("degraded"):
        response.__dict__.setdefault("_degraded", meta["degraded"])
    return response
