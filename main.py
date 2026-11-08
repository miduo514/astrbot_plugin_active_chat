"""AstrBot 插件入口：按群聊独立计时的主动消息。

职责边界：
- 只做装配（组装兼容层、状态表、发送器、截屏器）与 AstrBot 事件注册；
- 业务逻辑全部在 core/ 中，便于单独测试与复用。
"""

from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.compat import AstrBotCompat
from .core.llm import MessageComposer
from .core.logutil import log_exc
from .core.manager import ActiveChatManager
from .core.screenshot import ScreenCapture
from .core.sender import MessageSender
from .core.store import StateStore, ensure_data_dir

# 手动测试指令等待上限（含 LLM 生成 + 分段发送 + 截屏）
TEST_TIMEOUT = 180.0


class ActiveChatPlugin(Star):
    """群聊主动消息插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        data_dir = ensure_data_dir()
        self.store = StateStore(data_dir, logger)
        self.compat = AstrBotCompat(context)
        self.composer = MessageComposer(self.compat)
        self.sender = MessageSender(self.compat)
        self.capture = ScreenCapture(data_dir / "screenshots", logger)
        self.manager = ActiveChatManager(
            raw_config=self._raw_config,
            compat=self.compat,
            composer=self.composer,
            sender=self.sender,
            capture=self.capture,
            store=self.store,
            max_concurrency=2,
        )

        self._started = False
        self._ensure_started()

    # ---------------- 内部工具 ----------------

    def _raw_config(self):
        return self.config if self.config is not None else {}

    def _ensure_started(self) -> None:
        if self._started:
            return
        try:
            self.manager.start()
        except RuntimeError as exc:
            # 事件循环尚未就绪（少数加载时序），交给 on_astrbot_loaded 或首条消息兜底
            logger.debug(f"[active_chat] 事件循环尚未就绪，稍后启动：{exc}")
            return
        except Exception:
            # 启动失败绝不能让插件加载整体失败，留给后续事件重试
            log_exc("[active_chat] 启动主动消息管理器失败，稍后重试")
            return
        self._started = True

    # ---------------- 管理指令 ----------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("主动消息状态", alias={"active_status", "主动消息查看"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看各群聊的静默时长、候选状态、连续失败次数与当前判定概率。"""
        try:
            yield event.plain_result(self.manager.render_status())
        except Exception as exc:
            log_exc("[active_chat] 渲染状态失败")
            yield event.plain_result(f"读取主动消息状态失败：{exc}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("主动消息测试", alias={"active_test", "主动消息触发"})
    async def cmd_test(self, event: AstrMessageEvent, target: str = ""):
        """忽略计时与概率，立即在指定群聊触发一次主动消息（仍按 P 决定截屏分支）。"""
        raw = (target or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：/主动消息测试 <序号或群聊UMO>\n可先发送 /主动消息状态 查看目标列表。"
            )
            return

        state, error = self.manager.resolve_target(raw)
        if state is None:
            yield event.plain_result(error)
            return

        self._ensure_started()

        try:
            await event.send(event.plain_result(f"正在向 {state.umo} 触发主动消息，请稍候…"))
        except Exception:
            pass

        try:
            result = await asyncio.wait_for(self.manager.execute_manual(state), timeout=TEST_TIMEOUT)
        except asyncio.TimeoutError:
            yield event.plain_result(
                f"【主动消息测试】目标：{state.umo}\n结果：超时（超过 {TEST_TIMEOUT:.0f} 秒）"
            )
            return
        except Exception as exc:
            log_exc(f"[active_chat] 手动测试执行失败 umo={state.umo}")
            yield event.plain_result(f"【主动消息测试】目标：{state.umo}\n结果：失败\n原因：{exc}")
            return

        if result.ok:
            branch = "截屏主动消息" if result.branch == "screenshot" else "普通主动消息"
            lines = [
                f"【主动消息测试】目标：{state.umo}",
                f"分支：{branch} ｜ 字数：{result.chars} ｜ 耗时：{result.elapsed:.1f}s",
                "结果：已发送",
            ]
            if result.note:
                lines.append(f"备注：{result.note}")
        else:
            lines = [
                f"【主动消息测试】目标：{state.umo}",
                f"结果：失败 ｜ 耗时：{result.elapsed:.1f}s",
                f"原因：{result.error or '未知错误'}",
            ]
        yield event.plain_result("\n".join(lines))

    # ---------------- 事件监听 ----------------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """任何已配置群聊收到新消息，都重置该群的静默计时。"""
        try:
            if not self._started:
                self._ensure_started()
            self.manager.touch(event.unified_msg_origin)
        except Exception:
            log_exc("[active_chat] 记录群聊消息时间失败")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 初始化完成后确保状态机已启动（幂等）。"""
        self._ensure_started()

    # ---------------- 卸载 ----------------

    async def terminate(self):
        """插件被禁用/卸载/重载时停止全部协程并落盘。"""
        try:
            await self.manager.shutdown()
        except Exception:
            log_exc("[active_chat] 插件停止时清理失败")
        self._started = False
