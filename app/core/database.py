"""数据库连接与会话管理。

PostgreSQL（pgvector）是任务状态与知识元数据的权威来源。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine = None
_session_factory = None


def get_engine():
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.database_url, pool_pre_ping=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session() -> Generator[Session, None, None]:
    get_engine()
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database() -> bool:
    """健康检查用：验证数据库连通。"""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
