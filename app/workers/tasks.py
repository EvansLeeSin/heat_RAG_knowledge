"""Worker 任务：入库处理流水线（阶段二：解析与 Chunk）。

PENDING → PARSING → CHUNKING → SUCCEEDED（阶段二范围）
EMBEDDING → INDEXING → 全量激活与版本切换在阶段三/四实现。

幂等与恢复（设计文档 §5.2）：
- PostgreSQL advisory lock 保证同一任务不并发重复处理；
- 失败任务标记 FAILED（可重试），半成品不进入默认检索范围；
- acks_late + 可见性超时：Worker 中断后任务可安全重投。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.services.chunker import chunk_blocks
from app.services.document_version import activate_document_version
from app.services.embedding import EmbeddingError, embed_texts, get_embedding_config
from app.services.idempotency import acquire_job_lock, chunk_hash, release_job_lock
from app.services.parser import PARSER_VERSION, DoclingParser
from app.services.storage import StorageService
from app.services.tokenizer import TOKENIZER_VERSION, tokenize
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_parser = None

# 标题行清洗：折叠空白、去掉“（简本）”等括号补充与实际换行
_WS_RE = re.compile(r"\s+")
_BRACKET_SUFFIX_RE = re.compile(r"[（(][^）)]*[）)]\s*$")


def _clean_inline(text: str) -> str:
    """将标题压缩为单行，去除结尾的括号补充（如“应急预案(简本)”→“应急预案”）。"""
    title = _BRACKET_SUFFIX_RE.sub("", _WS_RE.sub("", text or "")).strip()
    return title


def _get_parser() -> DoclingParser:
    global _parser
    if _parser is None:
        _parser = DoclingParser()
    return _parser


def _engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def _set_status(conn, job_id: str, status: str, error_code: str | None = None, error_message: str | None = None) -> None:
    conn.execute(
        text(
            "UPDATE ingestion_job SET status = CAST(:status AS VARCHAR), "
            "error_code = CAST(:code AS VARCHAR), error_message = CAST(:message AS TEXT), "
            "started_at = COALESCE(started_at, CASE WHEN CAST(:status AS VARCHAR) = 'PARSING' THEN now() END), "
            "finished_at = CASE WHEN CAST(:status AS VARCHAR) IN ('SUCCEEDED','FAILED') THEN now() ELSE finished_at END, "
            "updated_at = now() WHERE job_id = :job"
        ),
        {"status": status, "code": error_code, "message": error_message, "job": job_id},
    )


@celery_app.task(
    name="workers.process_ingestion_job",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def process_ingestion_job(self, job_id: str) -> dict:
    """处理一个入库任务：解析 → 清洗 → 条款切分 → 写入 Chunk。"""
    engine = _engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT j.job_id, j.document_id, j.document_version, j.source_path, j.content_hash, j.status, "
                "       d.title AS document_title "
                "FROM ingestion_job j "
                "LEFT JOIN knowledge_document d "
                "  ON d.document_id = j.document_id AND d.version = j.document_version "
                "WHERE j.job_id = :job"
            ),
            {"job": job_id},
        ).mappings().first()

    if row is None:
        logger.error("job %s not found", job_id)
        return {"ok": False, "error": "job not found"}
    if row["status"] == "SUCCEEDED":
        # 幂等：已成功任务不重复处理
        return {"ok": True, "jobId": job_id, "skipped": "already succeeded"}

    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as lock_session:
        if not acquire_job_lock(lock_session, job_id):
            # 并发保护：另一 Worker 正在处理，放弃本此次投递
            return {"ok": True, "jobId": job_id, "skipped": "locked by another worker"}
    try:
        return _process(job_id, dict(row))
    finally:
        with engine.begin() as conn:
            release_job_lock(conn, job_id)


def _process(job_id: str, row: dict) -> dict:
    document_id = row["document_id"]
    version = row["document_version"]
    source_path = row["source_path"]

    storage = StorageService()
    absolute_path = str(storage.resolve(source_path))

    engine = _engine()
    try:
        # ---- PARSING ----
        with engine.begin() as conn:
            _set_status(conn, job_id, "PARSING")
        parser = _get_parser()
        parsed = parser.parse(absolute_path)

        # 幂等：同文档版本相同内容已处理过则跳过重复入库
        with engine.begin() as conn:
            existing = conn.execute(
                text(
                    "SELECT 1 FROM knowledge_chunk "
                    "WHERE document_id = :doc AND version = :ver LIMIT 1"
                ),
                {"doc": document_id, "ver": version},
            ).scalar()
        if existing:
            with engine.begin() as conn:
                _set_status(conn, job_id, "SUCCEEDED")
            return {"ok": True, "jobId": job_id, "skipped": "chunks already exist"}

        # ---- CHUNKING ----
        with engine.begin() as conn:
            _set_status(conn, job_id, "CHUNKING")
        chunks = chunk_blocks(parsed.blocks)
        if not chunks:
            raise ValueError("no chunks produced from document")

        # sectionPath 兜底：无章节结构的文档（如办法/指导意见类）以解析标题（其次元数据标题）
        # 作为章节根，保证每个 Chunk 都能定位到“某文档 / 第 X 条”，而非章节为空。
        default_section = _clean_inline(parsed.title) or _clean_inline(row.get("document_title") or "")
        if default_section:
            for chunk in chunks:
                if not chunk.section_path:
                    chunk.section_path = default_section

        # ---- INDEXING：先删旧 chunk 并写入新 chunk，再生成向量并插入。
        # 顺序不能颠倒：embedding 依赖新 chunk 的 chunk_id，
        # 若先插 embedding 再删旧 chunk，会被外键级联清空。
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM knowledge_chunk WHERE document_id = CAST(:doc AS VARCHAR) AND version = CAST(:ver AS INTEGER)"),
                {"doc": document_id, "ver": version},
            )
            for index, chunk in enumerate(chunks):
                tokens = " ".join(tokenize(chunk.content))
                conn.execute(
                    text(
                        "INSERT INTO knowledge_chunk "
                        "(id, chunk_id, document_id, version, chunk_index, section_path, "
                        " article_no, heading, content, content_hash, token_count, "
                        " access_scope, project_ids, search_vector, tokenizer_version, created_at) "
                        "SELECT replace(gen_random_uuid()::text, '-', ''), replace(gen_random_uuid()::text, '-', ''), "
                        " CAST(:doc AS VARCHAR), CAST(:ver AS INTEGER), CAST(:idx AS INTEGER), "
                        " CAST(:section AS TEXT), CAST(:article AS VARCHAR), CAST(:heading AS TEXT), "
                        " CAST(:content AS TEXT), CAST(:hash AS VARCHAR), CAST(:token_count AS INTEGER), "
                        " d.access_scope, d.project_ids, "
                        " to_tsvector('simple', CAST(:tokens AS TEXT)), CAST(:tokenizer_version AS VARCHAR), now() "
                        "FROM knowledge_document d WHERE d.document_id = CAST(:doc AS VARCHAR) "
                        "AND d.version = CAST(:ver AS INTEGER)"
                    ),
                    {
                        "doc": document_id,
                        "ver": version,
                        "idx": index,
                        "section": chunk.section_path,
                        "article": chunk.article_no,
                        "heading": chunk.heading,
                        "content": chunk.content,
                        "hash": chunk_hash(chunk.content),
                        "token_count": len(tokens.split()),
                        "tokens": tokens,
                        "tokenizer_version": TOKENIZER_VERSION,
                    },
                )
            conn.execute(
                text(
                    "UPDATE knowledge_document SET parser_version = CAST(:pv AS VARCHAR), parsed_storage_path = CAST(:pp AS TEXT), "
                    "updated_at = now() WHERE document_id = CAST(:doc AS VARCHAR) AND version = CAST(:ver AS INTEGER)"
                ),
                {
                    "pv": PARSER_VERSION,
                    "pp": f"parsed/{document_id}/v{version}.json",
                    "doc": document_id,
                    "ver": version,
                },
            )
        # ---- EMBEDDING（外部 API；未配置或失败时降级为仅关键词检索）----
        with engine.begin() as conn:
            _set_status(conn, job_id, "EMBEDDING")
        try:
            vectors = embed_texts([c.content for c in chunks])
        except EmbeddingError as exc:
            logger.warning("embedding skipped for job %s: %s", job_id, exc)
            vectors = []

        with engine.begin() as conn:
            for chunk, vector in zip(chunks, vectors):
                conn.execute(
                    text(
                        "INSERT INTO knowledge_embedding "
                        "(id, chunk_id, vector, embedding_model, embedding_version, created_at) "
                        "SELECT replace(gen_random_uuid()::text, '-', ''), c.chunk_id, CAST(:vec AS vector), :model, :ev, now() "
                        "FROM knowledge_chunk c WHERE c.document_id = CAST(:doc AS VARCHAR) "
                        "AND c.version = CAST(:ver AS INTEGER) "
                        "AND c.chunk_index = CAST(:idx AS INTEGER)"
                    ),
                    {
                        "vec": str(vector),
                        "model": get_embedding_config().model,
                        "ev": get_embedding_config().version,
                        "doc": document_id,
                        "ver": version,
                        "idx": chunks.index(chunk),
                    },
                )

            # 原子切换默认检索版本（§7.1：全部成功后才激活）
            activate_document_version(conn, document_id, version)
            _set_status(conn, job_id, "SUCCEEDED")

        logger.info("job %s succeeded: %d chunks", job_id, len(chunks))
        return {"ok": True, "jobId": job_id, "chunks": len(chunks)}

    except Exception as exc:
        logger.exception("job %s failed", job_id)
        # 失败半成品不进入默认检索范围：Chunk 已在事务内回滚
        with engine.begin() as conn:
            _set_status(conn, job_id, "FAILED", "PROCESSING_ERROR", str(exc)[:2000])
        raise
