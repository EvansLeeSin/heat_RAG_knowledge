"""中文分词服务（设计文档 §5.6）。

jieba + 供热领域词典；同一分词与归一化规则同时用于文档 Chunk 与用户问题。
分词器接口保留，记录词典/分词规则版本；规则变化时支持重建全文索引。
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

TOKENIZER_VERSION = "jieba-v1"
DOMAIN_DICT_PATH = Path(__file__).resolve().parents[2] / "config" / "domain_dict.txt"

# 条款号、设备型号、标准编号保留精确匹配（不切分）
_EXACT_PATTERN = re.compile(
    r"([第][一二三四五六七八九十百千0-9]+[条章款项]|GB[/T]*\s*[0-9.]+|CJJ[/T]*\s*[0-9.]+|[A-Z]{1,6}[-]?[0-9]{1,6}[A-Z0-9]*)"
)


@lru_cache
def _get_jieba():
    import jieba

    if DOMAIN_DICT_PATH.exists():
        jieba.load_userdict(str(DOMAIN_DICT_PATH))
        logger.info("loaded domain dict: %s", DOMAIN_DICT_PATH)
    return jieba


# 纯标点/符号 token（不参与全文索引）
_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """分词：保留精确匹配词（条款号/标准编号）+ jieba 分词，小写归一化。"""
    jieba = _get_jieba()
    tokens: list[str] = []
    last = 0

    def _cut(segment: str) -> list[str]:
        out = []
        for word in jieba.cut(segment):
            word = word.strip()
            if word:
                out.append(word.lower())
        return out

    # 精确匹配片段（条款号/设备型号/标准编号）整块保留，不交与 jieba 切分；
    # 其余文本逐段分词，避免占位符被 jieba 破坏。
    for m in _EXACT_PATTERN.finditer(text):
        if m.start() > last:
            tokens.extend(_cut(text[last : m.start()]))
        tokens.append(m.group(1).strip().lower())
        last = m.end()
    if last < len(text):
        tokens.extend(_cut(text[last:]))

    return [t for t in tokens if t.strip() and not _PUNCT_RE.match(t)]


def tokenize_to_query(text: str) -> str:
    """构造安全的 PostgreSQL 全文查询表达式（plainto_tsquery 风格的 AND 连接）。

    输入仅作为分词结果参与查询，特殊字符不会注入 SQL（参数化查询）。
    """
    tokens = tokenize(text)
    return " & ".join(tokens) if tokens else ""
