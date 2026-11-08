"""数据模型：配置快照、单群运行状态与执行结果。

本模块只依赖标准库，便于脱离 AstrBot 做逻辑测试。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_umos(value: Any) -> tuple[str, ...]:
    """把配置里的 UMO 列表规整成去重、去空白的元组。"""
    if value is None:
        items: Iterable[Any] = []
    elif isinstance(value, str):
        items = [part.strip() for part in value.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]

    ordered: list[str] = []
    for item in items:
        text = _as_text(item)
        if text and text not in ordered:
            ordered.append(text)
    return tuple(ordered)


@dataclass
class RuntimeConfig:
    """一次配置解析后的快照，字段均为已校验的简单类型。"""

    group_umos: tuple[str, ...] = ()
    silence_minutes: int = 10
    check_interval_min: int = 5
    check_interval_max: int = 10
    base_probability: float = 0.1
    failure_increment: float = 0.1
    normal_extra_prompt: str = ""
    inject_history: bool = True
    enable_screenshot: bool = False
    screenshot_probability: float = 0.5
    screenshot_extra_prompt: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> tuple["RuntimeConfig", list[str]]:
        """解析并收敛配置，返回 (快照, 被修正项说明)。"""
        if isinstance(raw, Mapping):
            data: Mapping[str, Any] = raw
        else:
            # 兼容少数版本把配置包装成非 Mapping 的对象
            try:
                data = dict(raw)  # type: ignore[arg-type]
            except Exception:
                data = {}
        issues: list[str] = []

        def bounded_int(key: str, default: int, low: int, high: int) -> int:
            value = _as_int(data.get(key, default), default)
            fixed = min(max(value, low), high)
            if fixed != value:
                issues.append(f"{key}={value} 超出范围 {low}~{high}，已修正为 {fixed}")
            return fixed

        def bounded_float(key: str, default: float) -> float:
            value = _as_float(data.get(key, default), default)
            fixed = min(max(value, 0.0), 1.0)
            if abs(fixed - value) > 1e-9:
                issues.append(f"{key}={value} 超出范围 0~1，已修正为 {fixed}")
            return fixed

        silence = bounded_int("silence_minutes", 10, 1, 120)
        low = bounded_int("check_interval_min", 5, 1, 60)
        high = bounded_int("check_interval_max", 10, 1, 60)
        if low > high:
            issues.append(f"check_interval_min({low}) 大于 check_interval_max({high})，已自动交换")
            low, high = high, low

        config = cls(
            group_umos=_as_umos(data.get("group_umos")),
            silence_minutes=silence,
            check_interval_min=low,
            check_interval_max=high,
            base_probability=bounded_float("base_probability", 0.1),
            failure_increment=bounded_float("failure_increment", 0.1),
            normal_extra_prompt=_as_text(data.get("normal_extra_prompt")),
            inject_history=_as_bool(data.get("inject_history"), True),
            enable_screenshot=_as_bool(data.get("enable_screenshot"), False),
            screenshot_probability=bounded_float("screenshot_probability", 0.5),
            screenshot_extra_prompt=_as_text(data.get("screenshot_extra_prompt")),
        )
        return config, issues


@dataclass
class GroupState:
    """单个群聊的独立运行状态。

    所有时间戳均为 time.monotonic() 的读数，避免系统时间跳变影响计时。
    """

    umo: str
    # 实际收发使用的 UMO：从群消息中学到或经平台实例规范化后的值
    effective_umo: str = ""
    last_activity: float = field(default_factory=time.monotonic)
    candidate: bool = False
    round: int = 0
    fail_count: int = 0
    next_check_at: float | None = None
    last_trigger_at: float | None = None
    trigger_count: int = 0
    last_branch: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    closed: bool = False

    def mark_message(self) -> None:
        """收到新消息：重置静默计时、退出候选态、连续失败次数 B 归零。"""
        self.last_activity = time.monotonic()
        self.candidate = False
        self.round = 0
        self.next_check_at = None
        self.fail_count = 0
        self.wake.set()

    def probability(self, config: RuntimeConfig) -> float:
        """当前实际判定概率：min(1, A + B × C)。"""
        value = config.base_probability + self.fail_count * config.failure_increment
        return min(1.0, max(0.0, value))


@dataclass
class TriggerResult:
    """一次主动消息执行结果。"""

    ok: bool
    branch: str = ""
    chars: int = 0
    elapsed: float = 0.0
    error: str = ""
    note: str = ""


def format_duration(seconds: float) -> str:
    """把秒数格式化成便于阅读的时长。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
