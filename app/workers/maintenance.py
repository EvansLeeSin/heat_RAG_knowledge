"""运维维护脚本（非业务路径，不在 API 中暴露）。

用于测试环境清理与重建：清空知识库全部文档、版本、片段、向量与入库任务，
保留表结构。生产环境使用需显式确认（见 celery_app 的 marker 机制）。

设计文档 §11.3 要求 V1 不提供文档删除 API；本模块仅供运维在容器内执行，
不构成对外接口。
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from app.core.database import get_engine

logger = logging.getLogger(__name__)

# 清理顺序：先删依赖方（embedding → chunk → active_version → job → document）
_TABLES_IN_ORDER = (
    "knowledge_embedding",
    "knowledge_chunk",
    "document_active_version",
    "ingestion_job",
    "knowledge_document",
)


def purge_all_knowledge(confirm: str = "") -> dict:
    """清空全部知识数据（保留表结构与扩展）。

    必须先传入 confirm="PURGE" 才执行，避免误触发。
    """
    if confirm != "PURGE":
        raise ValueError("purge_all_knowledge requires confirm='PURGE'")

    engine = get_engine()
    deleted: dict[str, int] = {}
    with engine.begin() as conn:
        for table in _TABLES_IN_ORDER:
            result = conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - 表名为常量白名单
            deleted[table] = result.rowcount or 0
    logger.warning("purge_all_knowledge done: %s", deleted)
    return {"ok": True, "deleted": deleted}
