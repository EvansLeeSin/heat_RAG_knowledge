"""上传并创建入库任务 / 查询任务（设计文档 §9.1、§9.2）。

仅面向可信管理调用方（management key）。
API 不在 HTTP 请求中同步执行解析或向量化：保存文件、登记任务后返回任务 ID。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.auth import require_management_key
from app.api.schemas import (
    EFFECTIVE_STATUSES,
    ACCESS_SCOPES,
    DocumentMetadata,
    IngestionJobAccepted,
    IngestionJobStatus,
)
from app.core.config import get_settings
from app.core.database import get_session
from app.services.idempotency import content_hash
from app.services.storage import StorageService

router = APIRouter(prefix="/knowledge", tags=["ingestion"])

ALLOWED_SUFFIXES = {".html", ".htm"}


def _parse_metadata(metadata_raw: str) -> DocumentMetadata:
    try:
        data = json.loads(metadata_raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid metadata JSON: {exc}")
    meta = DocumentMetadata(**data)
    if meta.effectiveStatus not in EFFECTIVE_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid effectiveStatus")
    if meta.accessScope not in ACCESS_SCOPES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid accessScope")
    if meta.accessScope == "PROJECT" and not meta.projectIds:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "accessScope PROJECT requires projectIds",
        )
    return meta


def _allocate_version(session: Session, document_id: str) -> int:
    row = session.execute(
        text("SELECT COALESCE(MAX(version), 0) + 1 FROM knowledge_document WHERE document_id = :d"),
        {"d": document_id},
    ).scalar()
    return int(row)


@router.post(
    "/ingestion-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestionJobAccepted,
    dependencies=[Depends(require_management_key)],
)
def create_ingestion_job(
    file: UploadFile = File(...),
    metadata: str = Form(...),
    session: Session = Depends(get_session),
) -> IngestionJobAccepted:
    settings = get_settings()
    suffix = ("." + file.filename.rsplit(".", 1)[-1].lower()) if file.filename and "." in file.filename else ""
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unsupported file type: {suffix or 'unknown'}")

    data = file.file.read()
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty file")
    if len(data) > settings.max_file_size_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")

    meta = _parse_metadata(metadata)

    # 保存原始文件（持久化目录，服务端存储键）
    storage = StorageService()
    storage_key = storage.save_raw(data, suffix.lstrip("."))

    # 文档与版本分配
    document_id = meta.documentId or __import__("uuid").uuid4().hex
    version = _allocate_version(session, document_id)

    # 登记待处理文档版本（不覆盖历史版本；首次入库默认不可检索直至成功激活）
    session.execute(
        text(
            "INSERT INTO knowledge_document "
            "(id, document_id, version, title, publisher, source_url, document_type, region, "
            " applicability, effective_status, publication_status, access_scope, project_ids, "
            " content_hash, storage_path, captured_at, created_at, updated_at) "
            "VALUES (replace(gen_random_uuid()::text, '-', ''), :doc, :ver, :title, :publisher, :source_url, :doc_type, "
            " :region, :applicability, :effective_status, :publication_status, :access_scope, "
            " CAST(:project_ids AS jsonb), :content_hash, :storage_path, now(), now(), now())"
        ),
        {
            "doc": document_id,
            "ver": version,
            "title": meta.title,
            "publisher": meta.publisher,
            "source_url": meta.sourceUrl,
            "doc_type": meta.documentType,
            "region": meta.region,
            "applicability": meta.applicability,
            "effective_status": meta.effectiveStatus,
            "publication_status": meta.publicationStatus,
            "access_scope": meta.accessScope,
            "project_ids": json.dumps(meta.projectIds),
            "content_hash": content_hash(data),
            "storage_path": storage_key,
        },
    )

    # 登记任务（PENDING，由 Celery 异步分发）
    job_id = __import__("uuid").uuid4().hex
    session.execute(
        text(
            "INSERT INTO ingestion_job "
            "(id, job_id, document_id, document_version, status, source_path, content_hash, "
            " attempt_count, created_at, updated_at) "
            "VALUES (replace(gen_random_uuid()::text, '-', ''), :job, :doc, :ver, 'PENDING', :path, :hash, 0, now(), now())"
        ),
        {
            "job": job_id,
            "doc": document_id,
            "ver": version,
            "path": storage_key,
            "hash": content_hash(data),
        },
    )

    # 经 Redis 分发任务（失败时任务仍为 PENDING，可由恢复机制重投）
    from app.workers import tasks as worker_tasks

    worker_tasks.process_ingestion_job.delay(job_id)

    return IngestionJobAccepted(
        jobId=job_id,
        documentId=document_id,
        documentVersion=version,
        status="PENDING",
    )


@router.get(
    "/ingestion-jobs/{job_id}",
    response_model=IngestionJobStatus,
    dependencies=[Depends(require_management_key)],
)
def get_ingestion_job(job_id: str, session: Session = Depends(get_session)) -> IngestionJobStatus:
    from datetime import datetime, timezone

    row = session.execute(
        text(
            "SELECT job_id, document_id, document_version, status, attempt_count, "
            "error_code, error_message, started_at, finished_at "
            "FROM ingestion_job WHERE job_id = :job"
        ),
        {"job": job_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")

    def _iso(dt):
        return dt.astimezone(timezone.utc).isoformat() if isinstance(dt, datetime) else None

    return IngestionJobStatus(
        jobId=row["job_id"],
        documentId=row["document_id"],
        documentVersion=row["document_version"],
        status=row["status"],
        attemptCount=row["attempt_count"],
        errorCode=row["error_code"],
        errorMessage=row["error_message"],
        startedAt=_iso(row["started_at"]),
        finishedAt=_iso(row["finished_at"]),
    )
