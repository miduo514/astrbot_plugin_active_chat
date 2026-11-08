"""单群状态机：静默等待 → 候选 → 概率判定 → 主动消息。

每个已配置群聊一个实例、一个常驻协程，状态互不共享。
计时使用 time.monotonic()，避免系统时间跳变。
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

from astrbot.api import logger

from .logutil import log_exc
from .models import GroupState, RuntimeConfig, TriggerResult

# 发送失败后的退避时间，避免平台断线时高频重试
BACKOFF_SECONDS = 30.0
# 需求要求：文字发出后等待 1 秒再发截图
SCREENSHOT_IMAGE_DELAY = 1.0


class GroupWorker:
    """负责单个群聊的完整生命周期。"""

    def __init__(self, manager: Any, state: GroupState):
        self.manager = manager
        self.state = state

    # ---------------- 主循环 ----------------

    async def run(self) -> None:
        logger.debug(f"[active_chat][{self.state.umo}] 状态机启动")
        try:
            while not self.state.closed:
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log_exc(f"[active_chat][{self.state.umo}] 状态机异常，5 秒后重试")
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            logger.debug(f"[active_chat][{self.state.umo}] 状态机被取消")
            raise
        finally:
            self.state.closed = True

    async def _cycle(self) -> None:
        state = self.state
        config: RuntimeConfig = self.manager.config
        umo = state.umo

        # ---------- 阶段 1：静默等待 ----------
        deadline = state.last_activity + config.silence_minutes * 60
        if await self._sleep_until(deadline):
            return
        if state.closed:
            return
        # 到期后二次确认，规避「恰好到期瞬间来消息」的竞态
        if time.monotonic() - state.last_activity < config.silence_minutes * 60:
            return

        state.candidate = True
        state.round = 0
        state.next_check_at = None
        logger.info(f"[active_chat][{umo}] 静默 {config.silence_minutes} 分钟达成，进入候选状态")

        # ---------- 阶段 2：候选期周期判定 ----------
        while state.candidate and not state.closed:
            config = self.manager.config
            low = min(config.check_interval_min, config.check_interval_max)
            high = max(config.check_interval_min, config.check_interval_max)
            wait_seconds = random.uniform(low, high) * 60.0
            state.next_check_at = time.monotonic() + wait_seconds

            woken = await self._sleep_until(state.next_check_at)
            state.next_check_at = None
            if state.closed:
                return
            if woken or not state.candidate:
                # 收到新消息（或已退出候选），回到静默等待
                state.candidate = False
                return

            state.round += 1
            probability = state.probability(config)
            roll = random.random()
            success = roll < probability
            logger.info(
                f"[active_chat][{umo}] 第 {state.round} 次判定"
                f" | p={probability:.3f}"
                f"（A={config.base_probability:.2f} + B={state.fail_count} × C={config.failure_increment:.2f}）"
                f" | roll={roll:.3f} | {'成功' if success else '失败'}"
            )

            if not success:
                state.fail_count += 1
                self.manager.store.update_group(umo, fail_count=state.fail_count)
                continue

            result = await self.execute(source="auto")
            if result.ok:
                state.fail_count = 0
                self.manager.store.update_group(umo, fail_count=0)
                state.candidate = False
                return

            # 判定成功但发送失败：按决策 D2 视为本轮失败，退避后继续候选
            state.fail_count += 1
            self.manager.store.update_group(umo, fail_count=state.fail_count)
            logger.warning(
                f"[active_chat][{umo}] 主动消息发送失败，{BACKOFF_SECONDS:.0f}s 后重试：{result.error}"
            )
            state.next_check_at = time.monotonic() + BACKOFF_SECONDS
            if await self._sleep_until(state.next_check_at):
                state.candidate = False
                state.next_check_at = None
                return
            continue

    async def _sleep_until(self, deadline: float) -> bool:
        """等待到 deadline；被新消息唤醒时返回 True。"""
        state = self.state
        while True:
            if state.closed:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(state.wake.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return False
            state.wake.clear()
            return True

    # ---------------- 执行一次主动消息 ----------------

    async def execute(self, source: str) -> TriggerResult:
        state = self.state
        manager = self.manager
        config: RuntimeConfig = manager.config
        started = time.monotonic()

        # 发送前先确认平台实例可用：context.send_message 找不到平台时只打 WARN
        # 并丢弃消息，这里提前拦截，避免状态机误判为「已发送」。
        send_umo, umo_error = manager.resolve_send_umo(state)
        if umo_error:
            return self._fail(
                started,
                f"{umo_error}；请在目标群发送 /sid 复制完整 UMO，"
                f"或在 /主动消息状态 中查看「最近收到消息的会话」",
            )
        session_umo = state.effective_umo or state.umo
        umo = send_umo
        if umo != session_umo:
            logger.info(f"[active_chat][{umo}] 发送会话已校正（消息事件 UMO 为 {session_umo}）")
        logger.info(f"[active_chat][{umo}] 开始执行主动消息 source={source}")

        async with state.lock:
            async with manager.semaphore:
                try:
                    provider_id = await manager.compat.provider_id(session_umo)
                except Exception as exc:
                    return self._fail(started, f"获取 LLM Provider 失败：{exc}")
                if not provider_id:
                    return self._fail(started, "未找到可用的 LLM Provider，请先在 AstrBot 中配置对话模型")

                try:
                    persona_prompt, persona_id = await manager.compat.persona_prompt(session_umo)
                except Exception as exc:
                    logger.warning(f"[active_chat][{umo}] 读取人格失败，将仅使用额外提示词：{exc}")
                    persona_prompt, persona_id = "", ""

                history: list[dict[str, Any]] = []
                if config.inject_history:
                    try:
                        history = await manager.compat.history(session_umo)
                    except Exception as exc:
                        logger.warning(f"[active_chat][{umo}] 读取对话历史失败，按无历史处理：{exc}")

                note = ""
                # ---------- 截屏分支（可选） ----------
                if config.enable_screenshot and random.random() < config.screenshot_probability:
                    screenshot_result: TriggerResult | None = None
                    try:
                        screenshot_result = await self._screenshot_flow(
                            config, umo, provider_id, persona_prompt, history, started
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log_exc(f"[active_chat][{umo}] 截屏分支异常，回退普通主动消息")
                        screenshot_result = None
                    if screenshot_result is not None:
                        self._after_success(screenshot_result, source, persona_id)
                        return screenshot_result
                    note = "截屏分支失败，已回退普通主动消息"
                    logger.warning(f"[active_chat][{umo}] {note}")

                # ---------- 普通主动消息 ----------
                try:
                    text = await manager.composer.compose(
                        provider_id=provider_id,
                        persona_prompt=persona_prompt,
                        extra_prompt=config.normal_extra_prompt,
                        history=history,
                    )
                    if not text:
                        return self._fail(started, "LLM 返回空内容")
                    await manager.sender.send_text(umo, text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_exc(f"[active_chat][{umo}] 生成或发送普通主动消息失败")
                    return self._fail(started, f"发送失败：{exc}")

                result = TriggerResult(
                    ok=True,
                    branch="normal",
                    chars=len(text),
                    elapsed=time.monotonic() - started,
                    note=note,
                )
                self._after_success(result, source, persona_id)
                return result

    async def _screenshot_flow(
        self,
        config: RuntimeConfig,
        umo: str,
        provider_id: str,
        persona_prompt: str,
        history: list[dict[str, Any]],
        started: float,
    ) -> TriggerResult | None:
        """截屏 → 理解 → 生成 → 发文字 → 等 1 秒 → 发图；失败返回 None 表示回退。"""
        capture = self.manager.capture
        if not capture.available():
            logger.warning(f"[active_chat][{umo}] 截屏组件不可用，回退普通主动消息")
            return None

        image_path = await asyncio.to_thread(capture.capture, umo)
        if not image_path:
            logger.warning(f"[active_chat][{umo}] 截屏失败或图片无效，回退普通主动消息")
            return None

        description = await self.manager.composer.describe_image(provider_id, image_path)
        if not description:
            logger.warning(f"[active_chat][{umo}] 截屏内容理解失败，回退普通主动消息")
            return None
        logger.info(f"[active_chat][{umo}] 截屏理解完成：{description[:80]}")

        text = await self.manager.composer.compose(
            provider_id=provider_id,
            persona_prompt=persona_prompt,
            extra_prompt=config.screenshot_extra_prompt,
            history=history,
            image_description=description,
        )
        if not text:
            logger.warning(f"[active_chat][{umo}] 截屏主动消息生成空内容，回退普通主动消息")
            return None

        await self.manager.sender.send_text(umo, text)
        await asyncio.sleep(SCREENSHOT_IMAGE_DELAY)
        try:
            await self.manager.sender.send_image(umo, image_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log_exc(f"[active_chat][{umo}] 截屏图片发送失败，文字已发出")
            return TriggerResult(
                ok=True,
                branch="screenshot",
                chars=len(text),
                elapsed=time.monotonic() - started,
                note="图片发送失败，仅发送了文字",
            )

        return TriggerResult(
            ok=True,
            branch="screenshot",
            chars=len(text),
            elapsed=time.monotonic() - started,
        )

    # ---------------- 收尾 ----------------

    def _after_success(self, result: TriggerResult, source: str, persona_id: str) -> None:
        state = self.state
        now = time.monotonic()
        state.trigger_count += 1
        state.last_trigger_at = now
        state.last_branch = result.branch
        # 主动消息本身也是一次「新消息」，重置静默计时，避免紧接着再次触发
        state.last_activity = now
        self.manager.store.update_group(
            state.umo,
            trigger_count=state.trigger_count,
            last_branch=result.branch,
            last_trigger_at=int(time.time()),
        )
        logger.info(
            f"[active_chat][{state.umo}] 主动消息已发送"
            f" source={source} branch={result.branch} chars={result.chars}"
            f" elapsed={result.elapsed:.1f}s persona={persona_id or 'default'}"
            + (f" note={result.note}" if result.note else "")
        )

    def _fail(self, started: float, error: str) -> TriggerResult:
        logger.warning(f"[active_chat][{self.state.umo}] 主动消息失败：{error}")
        return TriggerResult(
            ok=False,
            branch="none",
            elapsed=time.monotonic() - started,
            error=error,
        )
