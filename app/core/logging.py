"""统一日志配置。

日志不记录凭据或不必要的文档敏感正文（设计文档 §12）。
"""

from __future__ import annotations

import logging
import sys

from app.core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
        force=True,
    )
    # 降低常见噪声库日志级别
    for name in ("urllib3", "celery.utils.log"):
        logging.getLogger(name).setLevel(logging.WARNING)
