"""临时诊断接口（仅用于 V1 验收排查，验证后移除）。

用途：直接读取数据库真实状态（access_scope / project_ids / chunk 数 / 活动版本），
避免仅凭检索结果反推存储内容。需要管理 Key。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.auth import require_management_key
from app.core.database import get_session

router = APIRouter()


@router.get("/debug/document", dependencies=[Depends(require_management_key)])
def debug_document(documentId: str, session: Session = Depends(get_session)) -> dict:
    rows = session.execute(
        text(
            "SELECT document_id, version, title, access_scope, project_ids, "
            " pg_typeof(project_ids)::text AS project_ids_type, "
            " jsonb_typeof(project_ids)::text AS project_ids_json_type "
            "FROM knowledge_document WHERE document_id = :d ORDER BY version"
        ),
        {"d": documentId},
    ).mappings().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    chunks = session.execute(
        text(
            "SELECT version, count(*) AS n, "
            " count(*) FILTER (WHERE search_vector IS NOT NULL) AS with_tsv "
            "FROM knowledge_chunk WHERE document_id = :d GROUP BY version ORDER BY version"
        ),
        {"d": documentId},
    ).mappings().all()
    active = session.execute(
        text("SELECT active_version FROM document_active_version WHERE document_id = :d"),
        {"d": documentId},
    ).scalar()
    return {
        "documentId": documentId,
        "activeVersion": active,
        "documents": [dict(r) for r in rows],
        "chunks": [dict(r) for r in chunks],
    }


@router.get("/debug/scope-probe", dependencies=[Depends(require_management_key)])
def debug_scope_probe(projectIds: str = "PRJ-ALPHA", session: Session = Depends(get_session)) -> dict:
    """直接验证范围谓词：`project_ids ?| array[...]` 的真实行为。"""
    ids = [p for p in projectIds.split(",") if p]
    overlap = session.execute(
        text(
            "SELECT d.document_id, d.access_scope, d.project_ids, "
            " (d.project_ids ?| CAST(:ids AS text[])) AS overlaps "
            "FROM knowledge_document d WHERE d.access_scope = 'PROJECT'"
        ),
        {"ids": ids},
    ).mappings().all()
    probe = session.execute(
        text("SELECT CAST(:ids AS text[]) AS ids, pg_typeof(CAST(:ids AS text[]))::text AS t"),
        {"ids": ids},
    ).mappings().first()
    return {
        "requested": ids,
        "castProbe": dict(probe) if probe else None,
        "projectDocuments": [dict(r) for r in overlap],
    }


@router.get("/debug/kw-sql", dependencies=[Depends(require_management_key)])
def debug_kw_sql(q: str = "项目专属供热负责人统一调度", projectIds: str = "PRJ-ALPHA",
                 session: Session = Depends(get_session)) -> dict:
    """复现关键词检索 SQL，逐行观察范围过滤是否命中项目文档。"""
    from app.services.tokenizer import tokenize_to_query

    ids = [p for p in projectIds.split(",") if p]
    tsquery = tokenize_to_query(q)
    out: dict = {"tsquery": tsquery, "authorizedProjectIds": ids}

    out["noScope"] = [dict(r) for r in session.execute(
        text("SELECT c.document_id, d.access_scope, d.project_ids FROM knowledge_chunk c "
             "JOIN document_active_version a ON a.document_id = c.document_id AND a.active_version = c.version "
             "JOIN knowledge_document d ON d.document_id = c.document_id AND d.version = c.version "
             "WHERE c.search_vector @@ to_tsquery('simple', :q) ORDER BY d.document_id"),
        {"q": tsquery},
    ).mappings().all()]

    # 与 _scope_filter 完全一致的形式（array[:project_ids]）
    out["withScope_arrayParam"] = [dict(r) for r in session.execute(
        text("SELECT c.document_id, d.access_scope, d.project_ids FROM knowledge_chunk c "
             "JOIN document_active_version a ON a.document_id = c.document_id AND a.active_version = c.version "
             "JOIN knowledge_document d ON d.document_id = c.document_id AND d.version = c.version "
             "WHERE c.search_vector @@ to_tsquery('simple', :q) "
             "AND (d.access_scope = 'PUBLIC' OR (d.access_scope = 'PROJECT' AND d.project_ids ?| array[:project_ids])) "
             "ORDER BY d.document_id"),
        {"q": tsquery, "project_ids": ids},
    ).mappings().all()]

    # 显式 CAST 形式
    out["withScope_cast"] = [dict(r) for r in session.execute(
        text("SELECT c.document_id, d.access_scope, d.project_ids FROM knowledge_chunk c "
             "JOIN document_active_version a ON a.document_id = c.document_id AND a.active_version = c.version "
             "JOIN knowledge_document d ON d.document_id = c.document_id AND d.version = c.version "
             "WHERE c.search_vector @@ to_tsquery('simple', :q) "
             "AND (d.access_scope = 'PUBLIC' OR (d.access_scope = 'PROJECT' AND d.project_ids ?| CAST(:project_ids AS text[]))) "
             "ORDER BY d.document_id"),
        {"q": tsquery, "project_ids": ids},
    ).mappings().all()]
    return out
