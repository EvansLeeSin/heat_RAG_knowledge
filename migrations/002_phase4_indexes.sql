-- 阶段四索引迁移（设计文档 §5.5、§5.6）
-- 前置：db/init.sql 已启用 pgvector 扩展

-- 全文索引：Chunk search_vector（simple 分词，jieba 预分词后写入）
CREATE INDEX IF NOT EXISTS ix_chunk_search_vector
    ON knowledge_chunk USING GIN (search_vector);

-- 向量索引：cosine 距离（ivfflat；数据量增长后可按样本评估换 hnsw 与 lists 参数）
CREATE INDEX IF NOT EXISTS ix_embedding_vector_cosine
    ON knowledge_embedding USING ivfflat (vector vector_cosine_ops) WITH (lists = 100);

-- 范围过滤辅助索引
CREATE INDEX IF NOT EXISTS ix_document_scope
    ON knowledge_document (access_scope);
