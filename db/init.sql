-- 数据库初始化（阶段一）
-- 元数据 + 全文索引 + pgvector（设计文档 §5.5）

CREATE EXTENSION IF NOT EXISTS vector;

-- 首版物理表结构由 SQLAlchemy 模型（app/models/__init__.py）定义，
-- 后续通过迁移工具固定。此处仅确保 pgvector 扩展可用。

-- 阶段二~四表结构（幂等）
-- 文档/版本/Chunk 由 SQLAlchemy 模型定义，此处用 DO 块幂等建表不现实；
-- 改为迁移服务执行 create_all。此文件仅负责扩展与索引。

CREATE EXTENSION IF NOT EXISTS pg_trgm;
