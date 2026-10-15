"""API Key 认证（设计文档 §12）。

查询与管理操作使用不同密钥；查询密钥不能执行管理操作。
"""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.core.config import get_settings


def _check(key_header: str | None, expected: str, kind: str) -> None:
    if not key_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {kind} API key",
        )
    if key_header != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"invalid {kind} API key",
        )


def require_query_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    _check(x_api_key, get_settings().knowledge_query_api_key, "query")


def require_management_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    _check(x_api_key, get_settings().knowledge_management_api_key, "management")
