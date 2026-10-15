"""外部 Embedding 客户端（设计文档 §5.4）。

文档片段与检索问题均使用同一套外部 Embedding 模型配置，与聊天模型分离。
支持两种 provider（EMBEDDING_PROVIDER）：
- `openai`：OpenAI 兼容接口（EMBEDDING_API_BASE + EMBEDDING_API_KEY + EMBEDDING_MODEL），
  适用于 Qwen3-Embedding / BGE-M3 本地部署（vLLM/Ollama/TEI）与各云 API；
  实测覆盖：SiliconFlow（https://api.siliconflow.cn/v1，Qwen/Qwen3-Embedding-8B，支持 dimensions 降维）；
- `gemini`：官方 google-genai SDK（临时验证通道，EMBEDDING_API_KEY + EMBEDDING_MODEL），
  gemini-embedding-001 原生区分 RETRIEVAL_QUERY / RETRIEVAL_DOCUMENT task type，
  非 3072 维时 SDK 返回未归一化向量，本模块统一 L2 归一化。

具体供应商/模型通过中文供热资料选型验证确定（EMBEDDING_PROVIDER_RESEARCH.md）。
记录：embeddingProvider / embeddingModel / embeddingDimension /
embeddingVersion / indexVersion。首版只启用一个模型，不混用版本。
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from dataclasses import dataclass

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_client: httpx.Client | None = None


class EmbeddingError(Exception):
    pass


@dataclass
class EmbeddingConfig:
    provider: str
    api_base: str
    api_key: str
    model: str
    dimension: int
    version: str


def get_embedding_config() -> EmbeddingConfig:
    settings = get_settings()
    provider = (settings.embedding_provider or "openai").lower()
    if provider not in ("openai", "gemini", "none"):
        raise EmbeddingError(f"unsupported EMBEDDING_PROVIDER: {provider}")
    if provider == "none":
        # 显式关闭向量通道（如部署环境无法访问外部 Embedding API）：仅关键词检索
        return EmbeddingConfig(
            provider="none", api_base="", api_key="", model="",
            dimension=0, version=settings.embedding_version,
        )
    if provider == "gemini":
        if not settings.embedding_api_key or not settings.embedding_model:
            raise EmbeddingError(
                "gemini embedding not configured (EMBEDDING_API_KEY / EMBEDDING_MODEL)"
            )
    elif not settings.embedding_api_base or not settings.embedding_model:
        raise EmbeddingError(
            "embedding provider not configured (EMBEDDING_API_BASE / EMBEDDING_MODEL)"
        )
    return EmbeddingConfig(
        provider=provider,
        api_base=settings.embedding_api_base.rstrip("/"),
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        dimension=settings.embedding_dimension,
        version=settings.embedding_version,
    )


def _l2_normalize(vector: list[float]) -> list[float]:
    """L2 归一化（gemini-embedding-001 非 3072 维时官方要求手动归一化）。"""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


def _embed_gemini_with_timeout(texts: list[str], task_type: str, timeout_s: int = 90) -> list[list[float]]:
    """带整体超时的 Gemini 调用：网络不可达时快速失败（调用方降级），避免无限挂起。"""
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_embed_gemini, texts, task_type)
    try:
        return future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        # 不等待卡住的线程（wait=False），否则超时后仍会阻塞
        pool.shutdown(wait=False)
        raise EmbeddingError(
            f"gemini embedding timed out after {timeout_s}s (network unreachable?)"
        ) from exc
    finally:
        pool.shutdown(wait=False)


def _embed_gemini(texts: list[str], task_type: str) -> list[list[float]]:
    """官方 google-genai SDK（gemini-embedding-001）。

    检索任务区分 task type：RETRIEVAL_QUERY（问题）/ RETRIEVAL_DOCUMENT（文档）。
    输出维度经 EMBEDDING_DIMENSION 指定（128-3072），返回后统一 L2 归一化。
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise EmbeddingError("google-genai package not installed") from exc

    config = get_embedding_config()
    try:
        import httpx
        client = genai.Client(
            api_key=config.api_key,
            http_options=types.HttpOptions(timeout=120 * 1000),
        )
        result = client.models.embed_content(
            model=config.model,
            contents=texts,
            config=types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=config.dimension or None,
            ),
        )
        if not result.embeddings:
            raise EmbeddingError("gemini returned no embeddings")
        vectors = [list(e.values) for e in result.embeddings]
        vectors = [_l2_normalize(v) for v in vectors]
        if config.dimension and len(vectors[0]) != config.dimension:
            raise EmbeddingError(
                f"dimension mismatch: got {len(vectors[0])}, expected {config.dimension}"
            )
        return vectors
    except EmbeddingError:
        raise
    except Exception as exc:
        logger.exception("gemini embedding request failed")
        raise EmbeddingError(str(exc)) from exc


def _http_client() -> httpx.Client:
    """模块级连接池：复用 TCP/TLS 连接，避免每次请求重复 DNS 解析与握手
    （NAS 环境实测：不复用时 P95 延迟抖动至 2~10s，复用后稳定在亚秒级）。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=120.0),
        )
    return _client


# 查询向量 LRU 缓存：同一检索问题不重复调外部 API（嵌入内容不变，可安全缓存）
_query_cache: OrderedDict[str, list[float]] = OrderedDict()
_QUERY_CACHE_MAX = 512


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量生成文档向量（RETRIEVAL_DOCUMENT）。内部资料接入前需确认允许出网（§5.4）。"""
    config = get_embedding_config()
    if not texts:
        return []
    if config.provider == "none":
        raise EmbeddingError("embedding disabled (EMBEDDING_PROVIDER=none)")
    if config.provider == "gemini":
        return _embed_gemini_with_timeout(texts, "RETRIEVAL_DOCUMENT")
    try:
        resp = _http_client().post(
            f"{config.api_base}/embeddings",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json={"model": config.model, "input": texts, **(
                {"dimensions": config.dimension} if config.dimension else {}
            )},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # OpenAI 兼容：data 按 index 排序返回
        data.sort(key=lambda d: d["index"])
        vectors = [d["embedding"] for d in data]
        if config.dimension and vectors and len(vectors[0]) != config.dimension:
            raise EmbeddingError(
                f"dimension mismatch: got {len(vectors[0])}, expected {config.dimension}"
            )
        return vectors
    except EmbeddingError:
        raise
    except Exception as exc:
        logger.exception("embedding request failed")
        raise EmbeddingError(str(exc)) from exc


def embed_query(text: str) -> list[float]:
    """检索问题向量。gemini 用 RETRIEVAL_QUERY task type；其余与文档片段同一模型配置。"""
    config = get_embedding_config()
    if config.provider == "none":
        raise EmbeddingError("embedding disabled (EMBEDDING_PROVIDER=none)")
    if config.provider == "gemini":
        return _embed_gemini_with_timeout([text], "RETRIEVAL_QUERY")[0]
    cached = _query_cache.get(text)
    if cached is not None:
        _query_cache.move_to_end(text)
        return cached
    vector = embed_texts([text])[0]
    _query_cache[text] = vector
    _query_cache.move_to_end(text)
    while len(_query_cache) > _QUERY_CACHE_MAX:
        _query_cache.popitem(last=False)
    return vector
