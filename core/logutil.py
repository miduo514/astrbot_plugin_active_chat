"""统一日志辅助。

AstrBot 的 logger 在不同版本下可能是 loguru 或标准 logging 包装，
这里统一使用 f-string 拼好完整文本，避免参数化格式差异；
异常栈则手动拼进消息，兼容两者。
"""

from __future__ import annotations

import traceback

from astrbot.api import logger


def log_exc(prefix: str) -> None:
    """记录当前正在处理的异常（含完整堆栈），不抛二次异常。"""
    detail = traceback.format_exc()
    try:
        logger.error(f"{prefix}\n{detail}")
    except Exception:
        pass
