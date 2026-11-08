"""群聊状态表、监督循环与指令支撑。

- 每个已配置 UMO 对应一个 GroupState + 一个 GroupWorker 协程；
- 监督协程每 15 秒对账一次配置（热更新、增删群、参数修正）；
- 所有对外方法都保证「单个群出问题不影响其他群」。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Mapping

from astrbot.api import logger

from .logutil import log_exc
from .models import GroupState, RuntimeConfig, TriggerResult, format_duration
from .worker import GroupWorker

SUPERVISE_INTERVAL = 15.0


class ActiveChatManager:
    """管理所有群聊的状态机实例。"""

    def __init__(
        self,
        raw_config: Callable[[], Mapping[str, Any]],
        compat: Any,
        composer: Any,
        sender: Any,
        capture: Any,
        store: Any,
        max_concurrency: int = 2,
    ):
        self._raw_config = raw_config
        self.compat = compat
        self.composer = composer
        self.sender = sender
        self.capture = capture
        self.store = store
        self.semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))
        self.config = RuntimeConfig()
        self.states: dict[str, GroupState] = {}
        self.workers: dict[str, GroupWorker] = {}
        self._started = False
        self._stopping = False
        self._supervisor: asyncio.Task | None = None
        self._issues: set[str] = set()
        self._empty_warned = False

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._started or self._stopping:
            return
        # 没有运行中的事件循环时抛 RuntimeError，由调用方等待 on_astrbot_loaded
        asyncio.get_running_loop()
        self.reload_config()
        self._reconcile_sync()
        self._supervisor = asyncio.create_task(self._supervise(), name="active_chat:supervisor")
        self._started = True
        logger.info("[active_chat] 主动消息管理器已启动")

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True

        if self._supervisor is not None and not self._supervisor.done():
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            except Exception:
                log_exc("[active_chat] 停止监督循环时异常")
        self._supervisor = None

        tasks: list[asyncio.Task] = []
        for umo in list(self.states):
            state = self.states.pop(umo)
            self.workers.pop(umo, None)
            state.closed = True
            state.wake.set()
            if state.task is not None:
                state.task.cancel()
                tasks.append(state.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.store.aclose()
        except Exception:
            log_exc("[active_chat] 落盘状态时异常")

        self._started = False
        logger.info("[active_chat] 主动消息管理器已停止")

    async def _supervise(self) -> None:
        while not self._stopping:
            try:
                self.reload_config()
                self._reconcile_sync()
            except Exception:
                log_exc("[active_chat] 监督循环异常")
            try:
                await asyncio.sleep(SUPERVISE_INTERVAL)
            except asyncio.CancelledError:
                raise

    # ---------------- 配置 ----------------

    def reload_config(self) -> None:
        try:
            raw = self._raw_config() or {}
        except Exception as exc:
            logger.warning(f"[active_chat] 读取插件配置失败，沿用上一次配置：{exc}")
            return

        config, issues = RuntimeConfig.from_mapping(raw)
        self.config = config
        for issue in issues:
            if issue not in self._issues:
                logger.warning(f"[active_chat] 配置修正：{issue}")
        self._issues = set(issues)

    # ---------------- 状态表对账 ----------------

    def _reconcile_sync(self) -> None:
        configured = list(self.config.group_umos)
        configured_set = set(configured)

        for umo in list(self.states):
            if umo not in configured_set:
                self._drop_state(umo)

        for umo in configured:
            if umo not in self.states:
                self._create_state(umo)

        if not configured:
            if not self._empty_warned:
                logger.warning("[active_chat] 未配置任何群聊 UMO（group_umos 为空），主动消息不会触发")
                self._empty_warned = True
        else:
            self._empty_warned = False

    def _create_state(self, umo: str) -> None:
        state = GroupState(umo=umo)
        stats = self.store.get_group(umo)
        state.trigger_count = int(stats.get("trigger_count", 0) or 0)
        state.last_branch = str(stats.get("last_branch") or "")
        worker = GroupWorker(self, state)
        state.task = asyncio.create_task(worker.run(), name=f"active_chat:{umo}")
        self.states[umo] = state
        self.workers[umo] = worker
        logger.info(f"[active_chat][{umo}] 已加入监控")

    def _drop_state(self, umo: str) -> None:
        state = self.states.pop(umo, None)
        self.workers.pop(umo, None)
        if state is None:
            return
        state.closed = True
        state.wake.set()
        if state.task is not None:
            state.task.cancel()
        logger.info(f"[active_chat][{umo}] 已移出监控")

    # ---------------- 事件入口 ----------------

    def touch(self, umo: str) -> bool:
        """收到消息时刷新该群的静默计时。未配置的会话直接忽略。"""
        state = self.states.get(umo)
        if state is None:
            return False
        state.mark_message()
        return True

    async def execute_manual(self, state: GroupState) -> TriggerResult:
        worker = self.workers.get(state.umo)
        if worker is None:
            return TriggerResult(ok=False, branch="none", error="该群聊当前未在监控列表中")
        return await worker.execute(source="manual")

    # ---------------- 指令支撑 ----------------

    def render_status(self) -> str:
        config = self.config
        states = sorted(self.states.values(), key=lambda item: item.umo)
        screenshot_state = "开启" if config.enable_screenshot else "关闭"
        screenshot_runtime = "可用" if self.capture.available() else "不可用"

        lines = [f"【主动消息状态】共 {len(states)} 个群"]
        lines.append(
            f"阈值 X={config.silence_minutes}m ｜ 判定间隔 {config.check_interval_min}~{config.check_interval_max}m"
            f" ｜ A={config.base_probability:.2f} ｜ C={config.failure_increment:.2f}"
            f" ｜ 历史注入={'开' if config.inject_history else '关'}"
        )
        lines.append(
            f"截屏={screenshot_state}（P={config.screenshot_probability:.2f}，组件{screenshot_runtime}）"
        )

        if not states:
            lines.append("")
            lines.append("当前没有配置任何群聊 UMO，请在插件配置 group_umos 中添加。")
            return "\n".join(lines)

        now = time.monotonic()
        for index, state in enumerate(states, 1):
            silent = now - state.last_activity
            probability = state.probability(config)
            lines.append("")
            lines.append(f"{index}) {state.umo}")
            lines.append(
                f"   静默 {format_duration(silent)}（阈值 {config.silence_minutes}m）"
                f" ｜ 累计触发 {state.trigger_count} 次"
            )
            if state.candidate:
                if state.next_check_at is not None:
                    next_text = f"{format_duration(state.next_check_at - now)}后"
                else:
                    next_text = "正在执行"
                lines.append(f"   候选：是（已判定 {state.round} 次）｜ 下次判定：{next_text}")
            else:
                remaining = config.silence_minutes * 60 - silent
                next_text = f"{format_duration(remaining)}后" if remaining > 0 else "即将进入候选"
                lines.append(f"   候选：否 ｜ 距进入候选：{next_text}")
            lines.append(f"   B={state.fail_count} ｜ 当前概率 {probability:.2f}")
        return "\n".join(lines)

    def resolve_target(self, raw: str) -> tuple[GroupState | None, str]:
        text = (raw or "").strip()
        if not text:
            return None, "请提供目标群聊：序号或群聊 UMO。可先发送 /主动消息状态 查看。"
        states = sorted(self.states.values(), key=lambda item: item.umo)
        if not states:
            return None, "当前没有配置任何群聊 UMO，请先在插件配置 group_umos 中添加。"

        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(states):
                return states[index - 1], ""
            return None, f"序号超出范围，当前共 {len(states)} 个群。"

        for state in states:
            if state.umo == text:
                return state, ""

        fuzzy = [state for state in states if text in state.umo]
        if len(fuzzy) == 1:
            return fuzzy[0], ""
        if len(fuzzy) > 1:
            names = "、".join(state.umo for state in fuzzy[:5])
            return None, f"匹配到多个群聊，请使用更完整的 UMO：{names}"
        return None, "未找到匹配的群聊，请使用 /主动消息状态 查看已配置的 UMO。"

