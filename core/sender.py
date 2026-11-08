"""消息发送：按 AstrBot 全局分段回复配置拆分文本后逐条发送。

配置来源：当前会话所属配置文件的 platform_settings.segmented_reply，
字段语义与 AstrBot 官方分段回复保持一致：
  enable / only_llm_result / interval_method(random|log) / interval /
  log_base / words_count_threshold / regex / content_cleanup_rule
"""

from __future__ import annotations

import asyncio
import math
import random
import re
from typing import Any

DEFAULT_SPLIT_REGEX = r".*?[。？！~…]+|.+$"


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def split_segments(text: str, settings: dict[str, Any]) -> list[str]:
    """按全局分段回复配置把文本切成若干段；不满足条件时返回整条文本。"""
    body = (text or "").strip()
    if not body:
        return []

    if not bool(settings.get("enable", False)):
        return [body]

    threshold = _as_int(settings.get("words_count_threshold"), 150)
    if len(body) < max(threshold, 1):
        return [body]

    cleanup_rule = str(settings.get("content_cleanup_rule") or "")
    if cleanup_rule:
        try:
            body = re.sub(cleanup_rule, "", body)
        except re.error:
            pass
        body = body.strip()
        if not body:
            return []

    pattern = str(settings.get("regex") or DEFAULT_SPLIT_REGEX)
    try:
        raw_segments = re.findall(pattern, body, flags=re.S)
    except re.error:
        return [body]

    segments = [segment.strip() for segment in raw_segments if segment and segment.strip()]
    if len(segments) < 2:
        return [body]

    # 切分丢失过多内容时宁可整条发送，避免吞字
    if sum(len(segment) for segment in segments) < len(body) * 0.6:
        return [body]
    return segments


def segment_delay(segment: str, settings: dict[str, Any]) -> float:
    """计算两条分段消息之间的等待秒数。"""
    method = str(settings.get("interval_method") or "random").strip().lower()

    if method == "log":
        base = _as_float(settings.get("log_base"), 2.6)
        base = max(base, 1.01)
        delay = math.log(max(len(segment), 2), base)
        return max(0.3, min(10.0, delay))

    raw_interval = str(settings.get("interval") or "1.5,3.5")
    try:
        parts = [float(part) for part in raw_interval.split(",")[:2]]
    except (TypeError, ValueError):
        parts = []
    if len(parts) == 2:
        low, high = parts
    else:
        low, high = 1.5, 3.5
    if low > high:
        low, high = high, low
    return max(0.0, random.uniform(low, high))


class MessageSender:
    """对外提供 send_text / send_image。"""

    def __init__(self, compat: Any):
        self.compat = compat

    async def send_text(self, umo: str, text: str) -> int:
        """发送文本（可能被拆成多条），返回实际发送条数。"""
        body = (text or "").strip()
        if not body:
            return 0

        settings = self.compat.segmented_settings(umo)
        segments = split_segments(body, settings)
        sent = 0
        for index, segment in enumerate(segments):
            await self.compat.send_text(umo, segment)
            sent += 1
            if index < len(segments) - 1:
                await asyncio.sleep(segment_delay(segment, settings))
        return sent

    async def send_image(self, umo: str, image_path: str) -> None:
        await self.compat.send_image(umo, image_path)
