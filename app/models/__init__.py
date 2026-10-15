"""SQLAlchemy ORM 模型（对应设计文档 §8 建议数据模型）。

逻辑字段设计，物理约束与迁移在实现中逐步固定。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    ForeignKey,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from pgvector.sqlalchemy import Vector


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class KnowledgeDocument(Base):
    """文档版本（knowledge_document，§8.1）。"""

    __tablename__ = "knowledge_document"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)

    title: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    applicability: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_status: Mapped[str] = mapped_column(String(32), default="TO_VERIFY")
    publication_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    access_scope: Mapped[str] = mapped_column(String(16), default="PUBLIC")
    project_ids: Mapped[list] = mapped_column(JSONB, default=list)

    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KnowledgeChunk(Base):
    """知识片段（knowledge_chunk，§8.2）。"""

    __tablename__ = "knowledge_chunk"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    chunk_id: Mapped[str] = mapped_column(String(32), unique=True, default=new_id)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)

    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    article_no: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    access_scope: Mapped[str] = mapped_column(String(16), default="PUBLIC")
    project_ids: Mapped[list] = mapped_column(JSONB, default=list)

    search_vector = mapped_column(TSVECTOR, nullable=True)
    tokenizer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_chunk_doc_version_index", "document_id", "version", "chunk_index"),
        Index("ix_chunk_scope", "access_scope"),
    )


class KnowledgeEmbedding(Base):
    """Chunk 向量（pgvector，§5.4）。

    embeddingDimension 经选型验证后固定；向量维度变化时重建索引并记录 indexVersion。
    首版只启用一个模型，不混用版本（经 indexVersion 标记）。
    """

    __tablename__ = "knowledge_embedding"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(32), ForeignKey("knowledge_chunk.chunk_id", ondelete="CASCADE"), unique=True)
    vector = mapped_column(Vector(1024), nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(128))
    embedding_version: Mapped[str] = mapped_column(String(32), default="v1")
    index_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_embedding_chunk", "chunk_id"),
    )


class DocumentActiveVersion(Base):
    """文档默认版本指针（§7.1、§8.1：每份文档只有一个默认检索版本）。

    事务切换保证并发更新时默认版本一致。
    """

    __tablename__ = "document_active_version"

    document_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    active_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IngestionJob(Base):
    """入库任务（ingestion_job，§8.4）。

    PENDING → PARSING → CHUNKING → EMBEDDING → INDEXING → SUCCEEDED
    任一阶段失败 → FAILED（可重试）
    """

    __tablename__ = "ingestion_job"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(32), unique=True, default=new_id)
    document_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    document_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)

    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tokenizer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    index_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
