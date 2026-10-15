"""FastAPI 入口（knowledge-api）。

阶段二+三：上传、任务查询与检索接口已接入。向量与 RRF 融合在阶段四实现。
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api import debug, health, ingestion, search
from app.core.config import get_settings
from app.core.logging import configure_logging

configure_logging()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Heat Knowledge Service",
        description="供热知识服务：文档解析、索引与检索（独立于 heat-parent）",
        version="0.1.0",
    )
    app.include_router(health.router, tags=["health"])
    app.include_router(ingestion.router, tags=["ingestion"])
    app.include_router(search.router, tags=["search"])
    # 诊断接口默认不挂载：它们绕过业务层直查库表，不应出现在常规部署的服务面上
    if get_settings().debug_endpoints:
        app.include_router(debug.router, tags=["debug"])
    return app


app = create_app()
