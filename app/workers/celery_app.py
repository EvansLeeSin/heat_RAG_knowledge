"""Celery 应用（设计文档 §5.2 knowledge-worker）。

Redis 用于任务分发，PostgreSQL 保存可查询的任务状态。
Celery 不保证任务只执行一次，幂等在应用与数据库层保证（阶段二实现）。
"""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "knowledge-worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # 单任务超时（设计文档 §5.2）
    task_soft_time_limit=settings.task_timeout_seconds,
    task_time_limit=settings.task_timeout_seconds + 60,
    # Worker 重启后不重新取走未确认任务由 acks_late + 可见性超时配合
    task_acks_late=True,
    broker_transport_options={"visibility_timeout": settings.task_timeout_seconds},
    worker_prefetch_multiplier=1,
)


# 运维：worker 就绪后全量重建一次索引（幂等；用于补齐历史文档向量）。
# REBUILD_ON_START=0 可显式关闭；重建完成后建议移除本段。
import logging
import os

from celery.signals import worker_ready

logger = logging.getLogger(__name__)


@worker_ready.connect
def _rebuild_on_start(sender=None, **_kwargs):
    if os.environ.get("REBUILD_ON_START", "") != "1":
        return
    try:
        from app.workers.rebuild import rebuild_indexes

        logger.warning("REBUILD_ON_START=1: starting index rebuild")
        result = rebuild_indexes()
        logger.warning("REBUILD_ON_START finished: %s", result)
    except Exception as exc:  # noqa: BLE001 - 运维任务不阻断 worker 启动
        logger.exception("REBUILD_ON_START failed: %s", exc)


@worker_ready.connect
def _purge_on_start(sender=None, **_kwargs):
    """测试环境清库：仅当标记文件存在时执行一次，执行后自动删除标记。

    用标记文件而非环境变量，因为 docker restart 不会重读 Compose 的 env，
    而绑定挂载的代码卷可以对主机侧直接改文件。
    """
    marker = os.environ.get("PURGE_MARKER", "/app/PURGE_MARKER")
    if not os.path.exists(marker):
        return
    try:
        from app.workers.maintenance import purge_all_knowledge

        logger.warning("PURGE_MARKER found: purging all knowledge data")
        result = purge_all_knowledge(confirm="PURGE")
        os.remove(marker)
        logger.warning("PURGE_ON_START finished: %s", result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("PURGE_ON_START failed: %s", exc)
