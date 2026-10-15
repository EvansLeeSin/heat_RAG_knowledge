"""上传与任务相关的请求/响应模型（对应设计文档 §9）。

响应中的字段为标准化引用元数据，不暴露服务器路径或内部向量。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

EFFECTIVE_STATUSES = {"CURRENT", "TO_VERIFY", "SUPERSEDED", "REFERENCE_ONLY"}
ACCESS_SCOPES = {"PUBLIC", "PROJECT"}
DOCUMENT_TYPES = {
    "EMERGENCY_PLAN",
    "MANAGEMENT_MEASURE",
    "OPERATION_REGULATION",
    "EQUIPMENT_MANUAL",
    "OTHER",
}


class DocumentMetadata(BaseModel):
    """上传元数据（§9.1 示例结构）。"""

    documentId: str | None = None
    title: str = Field(min_length=1)
    publisher: str | None = None
    sourceUrl: str | None = None
    documentType: str = "OTHER"
    region: str | None = None
    applicability: str | None = None
    effectiveStatus: str = "TO_VERIFY"
    publicationStatus: str | None = None
    publishedAt: str | None = None
    effectiveFrom: str | None = None
    effectiveTo: str | None = None
    accessScope: str = "PUBLIC"
    projectIds: list[str] = Field(default_factory=list)


class IngestionJobAccepted(BaseModel):
    jobId: str
    documentId: str
    documentVersion: int
    status: str


class IngestionJobStatus(BaseModel):
    jobId: str
    documentId: str | None
    documentVersion: int | None
    status: str
    attemptCount: int
    errorCode: str | None = None
    errorMessage: str | None = None
    startedAt: str | None = None
    finishedAt: str | None = None
