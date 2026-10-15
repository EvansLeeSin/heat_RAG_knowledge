"""健康检查接口（阶段一交付项）。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.database import check_database

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    service: str
    database: bool
    version: str


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    db_ok = check_database()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        service=settings.service_name,
        database=db_ok,
        version="0.1.0",
    )
