"""服务配置。

所有配置通过环境变量注入（见设计文档 §11.3），
凭据禁止写入 Git、镜像或示例 Compose。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 服务 ----
    service_name: str = "knowledge-service"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"

    # ---- 认证（查询 / 管理 密钥分离，设计文档 §12）----
    knowledge_query_api_key: str = "dev-query-key"
    knowledge_management_api_key: str = "dev-management-key"

    # ---- 数据库 / 队列 ----
    database_url: str = "postgresql+psycopg://knowledge:knowledge@localhost:5432/knowledge"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ---- 持久化存储（设计文档 §6.1）----
    storage_root: str = "./data"

    # ---- Embedding（外部 API，供应商选型在阶段四验证）----
    embedding_provider: str = "openai"  # openai（OpenAI 兼容，含 SiliconFlow）/ gemini / none（关闭向量通道）
    embedding_api_base: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimension: int = 0
    embedding_version: str = "v1"
    index_version: str = "v1"

    # ---- 处理约束 ----
    max_file_size_mb: int = 50
    task_timeout_seconds: int = 3600

    # ---- 运维 / 诊断 ----
    # 诊断接口（/debug/*）默认关闭：它们直接查询库表，仅供标定阈值、排查范围隔离等运维场景
    # 临时开放。启用方式：环境变量 DEBUG_ENDPOINTS=1。
    debug_endpoints: bool = False

    # ---- 无答案判断（设计文档 §5.5、§16 第 6 条）----
    # 语义相似度下限：低于此值视为无相关证据。
    # 阈值由 eval/calibrate_threshold.py 在标注集上扫描得出（41 正样本 / 40 负样本，2026-09-28 实测）：
    #   正样本「正确答案 chunk」相似度 min=0.517，负样本「全库最高」相似度 max=0.504，
    #   完全分离区间 (0.504, 0.517)，取区间下沿 0.51 可在不误杀真答案的前提下拒答全部负样本。
    # 阈值随 Embedding 模型与语料变化，更换模型后必须重跑标定脚本。
    no_evidence_min_similarity: float = 0.51
    # 关键词 ts_rank_cd 下限：命中但排序权重过低同样视为弱证据
    no_evidence_min_ts_rank: float = 0.02


@lru_cache
def get_settings() -> Settings:
    return Settings()
