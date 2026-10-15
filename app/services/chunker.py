"""清洗与章节/条款切分（设计文档 §6.2）。

切分：文档 → 章节 → 条款 → 条款内自然段。
完整条款尽量同 Chunk，过长条款按自然段拆分；保留章节路径与条款号。
起点：普通段落约 600–1200 中文字符、重叠约 100–200 字符，非固定验收值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.parser import ParsedBlock

MIN_CHUNK_CHARS = 600
MAX_CHUNK_CHARS = 1200
OVERLAP_CHARS = 150

# 条款号模式：第 X 条 / 一、二、 / （一）。
# 不包含裸数字编号（如 "4."）：那类编号已由下方 _as_section_heading 作为章节标题处理，
# 保留在条款号里会产出无意义的 article_no（如 "4."）。
ARTICLE_RE = re.compile(r"^(第[一二三四五六七八九十百千0-9]+条|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）)")

# 章节标题模式：形如 "2 组织机构与职责"、"2.1 应急指挥机构" 的编号小节（应急预案类文档）。
# Docling 可能不将其识别为 heading，需在此提升为章节层级，否则 section_path 为空。
NUMERIC_SECTION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)*)[\s\u3000]+(\S.*)$")
# 标题行长度上限，避免把正文中以数字开头的长句误判为标题
SECTION_HEADING_MAX_CHARS = 60


def _as_section_heading(text: str) -> tuple[str, int] | None:
    """识别编号小节标题，返回（标题文本, 层级）；不是标题则返回 None。"""
    if len(text) > SECTION_HEADING_MAX_CHARS:
        return None
    m = NUMERIC_SECTION_RE.match(text)
    if not m:
        return None
    level = m.group(1).count(".") + 1
    return text, level


@dataclass
class Chunk:
    content: str
    section_path: str
    article_no: str | None
    heading: str | None


def _split_long_text(text: str) -> list[str]:
    """过长条款按自然段拆分，带重叠。"""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    pieces = []
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) > MAX_CHUNK_CHARS:
            pieces.append(current)
            current = current[-OVERLAP_CHARS:] + "\n\n" + para
        else:
            current = f"{current}\n\n{para}" if current else para
        while len(current) > MAX_CHUNK_CHARS:
            pieces.append(current[:MAX_CHUNK_CHARS])
            current = current[MAX_CHUNK_CHARS - OVERLAP_CHARS :]
    if current.strip():
        pieces.append(current)
    return pieces


def clean_blocks(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    """去除导航、页脚等噪声块（启发式：极短或重复的导航样式文本）。"""
    noise_patterns = ("首页", "版权所有", "ICP备", "返回顶部", "分享到", "打印本页")
    cleaned = []
    for b in blocks:
        text = b.text.strip()
        if not text:
            continue
        if len(text) <= 2 and b.kind == "paragraph":
            continue
        if any(p in text for p in noise_patterns) and len(text) < 30:
            continue
        cleaned.append(ParsedBlock(kind=b.kind, text=text, level=b.level))
    return cleaned


def chunk_blocks(blocks: list[ParsedBlock]) -> list[Chunk]:
    """按 章节 → 条款 → 自然段 组装 Chunk。"""
    cleaned = clean_blocks(blocks)
    chunks: list[Chunk] = []

    section_stack: list[str] = []
    current_heading: str | None = None
    current_article: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, current_article
        if not buffer:
            return
        text = "\n\n".join(buffer).strip()
        if not text:
            buffer = []
            return
        section_path = " / ".join(section_stack) if section_stack else ""
        if len(text) < MIN_CHUNK_CHARS and chunks:
            # 过短内容并入上一个 Chunk（同章节时）
            prev = chunks[-1]
            if prev.section_path == section_path:
                merged = prev.content + "\n\n" + text
                if len(merged) <= MAX_CHUNK_CHARS * 1.2:
                    chunks[-1] = Chunk(
                        content=merged,
                        section_path=prev.section_path,
                        article_no=prev.article_no,
                        heading=prev.heading,
                    )
                    buffer = []
                    return
        pieces = _split_long_text(text)
        for piece in pieces:
            chunks.append(
                Chunk(
                    content=piece,
                    section_path=section_path,
                    article_no=current_article,
                    heading=current_heading,
                )
            )
        buffer = []

    for block in cleaned:
        if block.kind == "heading":
            flush()
            current_heading = block.text
            level = max(block.level, 1)
            del section_stack[level - 1:]
            section_stack.append(block.text)
            current_article = None
        else:
            text = block.text.strip()
            # 编号小节标题优先作为章节层级处理（应急预案等文档 Docling 不标记为 heading）
            section = _as_section_heading(text)
            if section is not None:
                flush()
                current_heading = section[0]
                del section_stack[section[1] - 1 :]
                section_stack.append(section[0])
                current_article = None
                continue
            is_article_start = bool(ARTICLE_RE.match(text))
            projected = len("\n\n".join(buffer)) + len(text)
            if is_article_start and buffer and projected > MIN_CHUNK_CHARS:
                flush()
                current_article = ARTICLE_RE.match(text).group(1)
            elif not buffer and is_article_start:
                current_article = ARTICLE_RE.match(text).group(1)
            buffer.append(text)
    flush()
    return chunks
