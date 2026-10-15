"""持久化存储封装（设计文档 §6.1）。

目录结构：
    <storage-root>/raw/      原始文件
    <storage-root>/parsed/   标准化 Markdown / JSON

使用服务端生成的存储键，不接受任意服务器路径。
文件不放在容器临时目录，也不提交 Git（.gitignore 需排除 data/）。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.core.config import get_settings

RAW_DIR = "raw"
PARSED_DIR = "parsed"


class StorageService:
    def __init__(self, storage_root: str | None = None) -> None:
        self.root = Path(storage_root or get_settings().storage_root)

    def ensure_dirs(self) -> None:
        (self.root / RAW_DIR).mkdir(parents=True, exist_ok=True)
        (self.root / PARSED_DIR).mkdir(parents=True, exist_ok=True)

    def new_raw_key(self, suffix: str) -> str:
        """生成服务端存储键，例如 raw/2026/ab/<uuid>.html。"""
        safe_suffix = suffix.lstrip(".").replace("/", "_").replace("\\", "_")
        return f"{RAW_DIR}/{uuid.uuid4().hex}.{safe_suffix}"

    def resolve(self, storage_key: str) -> Path:
        """将服务端存储键解析为路径，拒绝路径穿越。"""
        base = self.root.resolve()
        target = (base / storage_key).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"invalid storage key: {storage_key}")
        return target

    def save_raw(self, content: bytes, suffix: str) -> str:
        self.ensure_dirs()
        key = self.new_raw_key(suffix)
        path = self.resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return key
