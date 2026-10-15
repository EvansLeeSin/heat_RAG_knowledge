"""Docling 解析适配器（设计文档 §5.3）。

首版实现并调用 DoclingParser，仅用 HTML 验证。
Docling 不负责 Embedding、数据库写入、权限或最终回答。
保留解析器替换能力（DocumentParser 接口）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PARSER_VERSION = "docling-v1"


@dataclass
class ParsedBlock:
    """标准化结构块：标题 / 段落 / 表格。"""

    kind: str  # heading | paragraph | table
    text: str
    level: int = 0  # heading 级别


@dataclass
class ParsedDocument:
    title: str | None
    blocks: list[ParsedBlock] = field(default_factory=list)


class DocumentParser:
    def parse(self, source_path: str) -> ParsedDocument:  # pragma: no cover - 接口
        raise NotImplementedError


class DoclingParser(DocumentParser):
    """基于 Docling 的 HTML 解析实现。"""

    def __init__(self) -> None:
        from docling.document_converter import DocumentConverter  # 延迟导入

        self._converter = DocumentConverter()

    def parse(self, source_path: str) -> ParsedDocument:
        result = self._converter.convert(source_path)
        doc = result.document

        blocks: list[ParsedBlock] = []
        title: str | None = None
        for item, _ in doc.iterate_items():
            label = str(getattr(item, "label", "") or "")
            text = str(getattr(item, "text", "") or "").strip()
            if not text:
                continue
            if label.startswith("SECTION") or label.startswith("TITLE") or label.startswith("DOC"):
                level = 1
                if title is None:
                    title = text
                blocks.append(ParsedBlock(kind="heading", text=text, level=level))
            elif label.startswith("TABLE"):
                # 表格导出为 Markdown 文本块
                try:
                    table_text = item.export_to_markdown(doc)
                except Exception:
                    table_text = text
                blocks.append(ParsedBlock(kind="table", text=table_text))
            else:
                blocks.append(ParsedBlock(kind="paragraph", text=text))
        return ParsedDocument(title=title, blocks=blocks)
