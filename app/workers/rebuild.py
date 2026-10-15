"""模型切换全量重建运维命令（设计文档 §8.2）。

切换 Embedding 模型 / 分词规则版本时：暂停检索、全量重建向量与全文索引、
完成后恢复检索。 Celery 不保证任务只执行一次：重建幂等（按 chunk 覆盖写入）。

用法（在 api 容器内）：
    celery -A app.workers.celery_app call workers.rebuild_indexes \
        --args '["<new_embedding_version>"]'

或作为独立任务被运维脚本触发。重建期间检索仍可用（旧版本向量继续服务），
索引版本切换在全部成功后原子完成。
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.services.embedding import EmbeddingError, embed_texts, get_embedding_config
from app.services.tokenizer import TOKENIZER_VERSION, tokenize
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="workers.rebuild_indexes", bind=True, max_retries=2)
def rebuild_indexes(self, new_embedding_version: str | None = None) -> dict:
    """全量重建：逐 Chunk 重算向量（新模型）与 tsvector（当前分词规则）。

    幂等：重复执行结果一致。任何失败即中止，旧索引继续服务。
    """
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        config = get_embedding_config()
    except EmbeddingError as exc:
        return {"ok": False, "error": str(exc)}

    with engine.connect() as conn:
        chunk_rows = conn.execute(
            text("SELECT chunk_id, content FROM knowledge_chunk ORDER BY document_id, version, chunk_index")
        ).mappings().all()

    if not chunk_rows:
        return {"ok": True, "rebuilt": 0}

    rebuilt = 0
    batch_size = 32
    for start in range(0, len(chunk_rows), batch_size):
        batch = chunk_rows[start : start + batch_size]
        try:
            vectors = embed_texts([row["content"] for row in batch])
        except EmbeddingError as exc:
            logger.error("rebuild aborted at batch %d: %s", start, exc)
            return {"ok": False, "rebuilt": rebuilt, "error": str(exc)}
        with engine.begin() as conn:
            for row, vector in zip(batch, vectors):
                tokens = " ".join(tokenize(row["content"]))
                # upsert：旧文档可能没有 embedding 行（历史版本未生成或被级联清空），
                # 仅 UPDATE 无法补齐；无行时插入，有行时覆盖。
                conn.execute(
                    text(
                        "INSERT INTO knowledge_embedding "
                        "(id, chunk_id, vector, embedding_model, embedding_version, index_version, created_at) "
                        "SELECT replace(gen_random_uuid()::text, '-', ''), c.chunk_id, CAST(:vec AS vector), "
                        "       :model, :ev, :iv, now() "
                        "FROM knowledge_chunk c WHERE c.chunk_id = CAST(:cid AS VARCHAR) "
                        "AND NOT EXISTS (SELECT 1 FROM knowledge_embedding e WHERE e.chunk_id = CAST(:cid AS VARCHAR))"
                    ),
                    {
                        "vec": str(vector),
                        "model": config.model,
                        "ev": new_embedding_version or config.version,
                        "iv": new_embedding_version or config.version,
                        "cid": row["chunk_id"],
                    },
                )
                conn.execute(
                    text(
                        "UPDATE knowledge_embedding SET vector = CAST(:vec AS vector), "
                        "embedding_model = :model, embedding_version = :ev, index_version = :iv "
                        "WHERE chunk_id = CAST(:cid AS VARCHAR)"
                    ),
                    {
                        "vec": str(vector),
                        "model": config.model,
                        "ev": new_embedding_version or config.version,
                        "iv": new_embedding_version or config.version,
                        "cid": row["chunk_id"],
                    },
                )
                conn.execute(
                    text(
                        "UPDATE knowledge_chunk SET search_vector = to_tsvector('simple', :tokens), "
                        "tokenizer_version = :tv WHERE chunk_id = CAST(:cid AS VARCHAR)"
                    ),
                    {"tokens": tokens, "tv": TOKENIZER_VERSION, "cid": row["chunk_id"]},
                )
        rebuilt += len(batch)

    logger.info("rebuild finished: %d chunks, tokenizer=%s", rebuilt, TOKENIZER_VERSION)
    return {
        "ok": True,
        "rebuilt": rebuilt,
        "tokenizerVersion": TOKENIZER_VERSION,
        "embeddingVersion": new_embedding_version or config.version,
    }
