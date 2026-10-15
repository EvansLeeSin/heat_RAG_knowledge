"""文档版本生命周期服务（设计文档 §7.1）。

同一文档更新时新增版本；新版本全部成功后原子切换默认检索版本；
新版本失败或处理中，旧默认版本继续服务；首次入库失败的文档不可检索。
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def activate_document_version(session: Session, document_id: str, version: int) -> None:
    """原子切换默认检索版本（事务内调用）。

    使用独立指针表 document_active_version，保证每份文档只有一个默认版本。
    """
    session.execute(
        text(
            "INSERT INTO document_active_version (document_id, active_version, updated_at) "
            "VALUES (:doc, :ver, now()) "
            "ON CONFLICT (document_id) DO UPDATE "
            "SET active_version = EXCLUDED.active_version, updated_at = now()"
        ),
        {"doc": document_id, "ver": version},
    )
    logger.info("activated document %s version %s", document_id, version)


def get_active_version(session: Session, document_id: str) -> int | None:
    row = session.execute(
        text("SELECT active_version FROM document_active_version WHERE document_id = :doc"),
        {"doc": document_id},
    ).scalar()
    return row


def deactivate_document_version(session: Session, document_id: str) -> None:
    """首次入库失败的文档不可检索：删除指针（如存在）。"""
    session.execute(
        text("DELETE FROM document_active_version WHERE document_id = :doc"),
        {"doc": document_id},
    )
