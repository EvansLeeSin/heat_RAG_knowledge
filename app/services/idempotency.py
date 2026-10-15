"""幂等与并发保护（设计文档 §5.2）。

Celery 不保证任务只执行一次。幂等判定包含文档归属（document_id + version）
与处理版本等上下文，不仅按文件 Hash 合并不同项目的文档。
"""

from __future__ import annotations

import hashlib

from sqlalchemy import text
from sqlalchemy.orm import Session


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chunk_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def acquire_job_lock(session: Session, job_id: str) -> bool:
    """PostgreSQL  advisory lock：保证同一任务不会被并发重复处理。"""
    locked = session.execute(
        text("SELECT pg_try_advisory_lock(hashtext(:key))"),
        {"key": f"ingestion-job:{job_id}"},
    ).scalar()
    return bool(locked)


def release_job_lock(session: Session, job_id: str) -> None:
    session.execute(
        text("SELECT pg_advisory_unlock(hashtext(:key))"),
        {"key": f"ingestion-job:{job_id}"},
    )


def find_existing_document(
    session: Session,
    content_hash_value: str,
    document_id: str,
    version: int,
) -> bool:
    """判定同一文档版本是否已按相同内容处理过（幂等判定含归属上下文）。"""
    row = session.execute(
        text(
            "SELECT 1 FROM knowledge_document "
            "WHERE document_id = :doc AND version = :ver AND content_hash = :hash "
            "LIMIT 1"
        ),
        {"doc": document_id, "ver": version, "hash": content_hash_value},
    ).scalar()
    return row is not None
