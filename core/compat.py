"""AstrBot API 适配层。

不同 AstrBot 小版本之间，LLM 调用、人格读取、全局配置读取的细节存在差异。
本模块把这些差异收敛到一处：运行时探测能力，缺失时降级并只告警一次，
绝不让插件因为某个可选 API 不可用而整体崩溃。
"""

from __future__ import annotations

import base64
import inspect
import json
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain

_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        logger.warning(message)
    except Exception:
        pass


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except Exception:
        return {}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _supports(fn: Any, name: str) -> bool:
    """判断可调用对象是否接受某个关键字参数（含 **kwargs 的情况）。"""
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    if name in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _completion_text(resp: Any) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp.strip()
    for attribute in ("completion_text", "text", "content"):
        value = getattr(resp, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(resp, dict):
        for key in ("completion_text", "text", "content"):
            value = resp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _persona_prompt(persona: Any) -> str:
    if persona is None:
        return ""
    if isinstance(persona, dict):
        return str(persona.get("prompt") or persona.get("system_prompt") or "").strip()
    for attribute in ("system_prompt", "prompt"):
        value = getattr(persona, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class AstrBotCompat:
    """对 AstrBot Context 的薄封装。"""

    def __init__(self, context: Any):
        self.context = context

    # ---------------- 全局配置 ----------------

    def global_config(self, umo: str | None = None) -> dict[str, Any]:
        context = self.context
        for getter in (
            (lambda: context.get_config(umo=umo)) if umo else None,
            lambda: context.get_config(),
        ):
            if getter is None:
                continue
            try:
                value = getter()
            except Exception:
                continue
            data = _as_dict(value)
            if data:
                return data
        return self._read_cmd_config()

    def segmented_settings(self, umo: str) -> dict[str, Any]:
        config = self.global_config(umo)
        platform_settings = config.get("platform_settings")
        if not isinstance(platform_settings, dict):
            return {}
        segmented = platform_settings.get("segmented_reply")
        return segmented if isinstance(segmented, dict) else {}

    def _read_cmd_config(self) -> dict[str, Any]:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            path = Path(get_astrbot_data_path()) / "cmd_config.json"
        except Exception:
            path = Path("data") / "cmd_config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    # ---------------- 平台实例 ----------------

    def platform_instances(self) -> list[tuple[str, str]]:
        """返回 [(平台实例 ID, 适配器类型名), ...]；拿不到时返回空列表。"""
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return []
        try:
            insts = getattr(manager, "platform_insts", None)
        except Exception:
            return []
        if not isinstance(insts, (list, tuple)):
            return []

        result: list[tuple[str, str]] = []
        for inst in insts:
            try:
                meta = inst.meta()
            except Exception:
                continue
            platform_id = str(getattr(meta, "id", "") or "").strip()
            platform_name = str(getattr(meta, "name", "") or "").strip()
            if platform_id or platform_name:
                result.append((platform_id, platform_name))
        return result

    def canonical_umo(self, umo: str) -> tuple[str, str]:
        """把 UMO 第一段（平台标识）规范成平台实例 ID。

        真实 UMO 的第一段是 WebUI 里的平台实例 ID（例如 default），
        而适配器类型（例如 aiocqhttp）并不是实例 ID，直接使用会导致
        context.send_message 找不到平台。返回 (修正后的 UMO, 说明)。
        """
        text = (umo or "").strip()
        parts = text.split(":")
        if len(parts) < 3:
            return text, ""

        prefix = parts[0]
        rest = ":".join(parts[1:])
        instances = self.platform_instances()
        if not instances:
            return text, ""

        ids = {platform_id for platform_id, _ in instances if platform_id}
        if prefix in ids:
            return text, ""

        by_name = [platform_id for platform_id, name in instances if name == prefix and platform_id]
        if len(by_name) == 1:
            return (
                f"{by_name[0]}:{rest}",
                f"第一段是适配器类型而非平台实例 ID，已自动改用实例 {by_name[0]}",
            )
        if len(instances) == 1 and instances[0][0]:
            return (
                f"{instances[0][0]}:{rest}",
                f"平台标识无效，已自动改用唯一平台实例 {instances[0][0]}",
            )
        return text, ""

    def platform_known(self, umo: str) -> tuple[bool, str]:
        """校验 UMO 第一段能否对应到平台实例；无法判断时视为通过。

        context.send_message 是按平台实例 ID 查找的，因此有实例 ID 时只认 ID；
        只有旧版本平台元数据没有 ID 时，才退化为按适配器类型名匹配。
        若连平台列表都拿不到，再退一步直接问 PlatformManager.get_inst。
        """
        prefix = (umo or "").split(":")[0]
        instances = self.platform_instances()

        if instances:
            ids = {platform_id for platform_id, _ in instances if platform_id}
            if ids:
                if prefix in ids:
                    return True, ""
                known = "、".join(pid for pid, _ in instances if pid)
                return False, f"找不到平台实例 {prefix}，当前可用：{known or '未知'}"

            names = {name for _, name in instances if name}
            if prefix in names:
                return True, ""
            known = "、".join(name for _, name in instances if name)
            return False, f"找不到平台 {prefix}，当前可用：{known or '未知'}"

        manager = getattr(self.context, "platform_manager", None)
        getter = getattr(manager, "get_inst", None)
        if callable(getter) and prefix:
            try:
                if getter(prefix) is not None:
                    return True, ""
                return False, f"找不到平台实例 {prefix}"
            except Exception:
                return True, ""
        return True, ""

    # ---------------- Provider ----------------

    async def provider_id(self, umo: str) -> str:
        getter = getattr(self.context, "get_current_chat_provider_id", None)
        if getter is None:
            _warn_once(
                "provider_id",
                "[active_chat] 当前 AstrBot 版本没有 get_current_chat_provider_id，无法确定对话模型",
            )
            return ""

        value: Any = None
        found = False
        for call in (lambda: getter(umo=umo), lambda: getter(umo)):
            try:
                value = call()
                found = True
                break
            except TypeError:
                continue
            except Exception as exc:
                logger.warning(f"[active_chat] 获取当前对话 Provider 失败：{exc}")
                return ""
        if not found:
            return ""
        try:
            return str(await _maybe_await(value) or "")
        except Exception as exc:
            logger.warning(f"[active_chat] 获取当前对话 Provider 失败：{exc}")
            return ""

    # ---------------- 人格与历史 ----------------

    async def persona_prompt(self, umo: str) -> tuple[str, str]:
        """返回 (system prompt, 人格标识)。"""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return "", ""

        persona_id = ""
        try:
            conversation_manager = getattr(self.context, "conversation_manager", None)
            if conversation_manager is not None:
                conversation_id = await _maybe_await(
                    conversation_manager.get_curr_conversation_id(umo)
                )
                if conversation_id:
                    conversation = await _maybe_await(
                        conversation_manager.get_conversation(umo, conversation_id)
                    )
                    persona_id = str(getattr(conversation, "persona_id", "") or "")
        except Exception as exc:
            logger.debug(f"[active_chat] 读取会话人格失败：{exc}")

        if persona_id:
            try:
                persona = await _maybe_await(manager.get_persona(persona_id))
                prompt = _persona_prompt(persona)
                if prompt:
                    return prompt, persona_id
            except Exception as exc:
                logger.warning(f"[active_chat] 读取人格 {persona_id} 失败，回退默认人格：{exc}")

        try:
            try:
                default = manager.get_default_persona_v3(umo)
            except TypeError:
                default = manager.get_default_persona_v3()
            default = await _maybe_await(default)
            return _persona_prompt(default), "default"
        except Exception as exc:
            logger.debug(f"[active_chat] 读取默认人格失败：{exc}")
            return "", ""

    async def history(self, umo: str, limit: int = 20, max_chars: int = 4000) -> list[dict[str, Any]]:
        """读取当前会话的 LLM 对话历史，按条数与字符预算裁剪。"""
        conversation_manager = getattr(self.context, "conversation_manager", None)
        if conversation_manager is None:
            return []

        try:
            conversation_id = await _maybe_await(
                conversation_manager.get_curr_conversation_id(umo)
            )
            if not conversation_id:
                return []
            conversation = await _maybe_await(
                conversation_manager.get_conversation(umo, conversation_id)
            )
            raw = getattr(conversation, "history", "") or ""
        except Exception as exc:
            _warn_once("history", f"[active_chat] 读取对话历史失败，将按无历史生成：{exc}")
            return []

        if isinstance(raw, str):
            if not raw.strip():
                return []
            try:
                data = json.loads(raw)
            except Exception:
                return []
        elif isinstance(raw, list):
            data = raw
        else:
            return []

        if not isinstance(data, list):
            return []

        cleaned: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            if role not in {"user", "assistant"}:
                continue
            content = item.get("content")
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        parts.append(str(part.get("text") or ""))
                    elif isinstance(part, str):
                        parts.append(part)
                content = "".join(parts)
            if not isinstance(content, str) or not content.strip():
                continue
            cleaned.append({"role": role, "content": content.strip()})

        if not cleaned:
            return []

        cleaned = cleaned[-max(limit, 1) :]
        kept: list[dict[str, Any]] = []
        total = 0
        for item in reversed(cleaned):
            total += len(item["content"])
            if kept and total > max_chars:
                break
            kept.append(item)
        return list(reversed(kept))

    # ---------------- LLM ----------------

    async def llm_text(
        self,
        provider_id: str,
        prompt: str,
        system_prompt: str = "",
        contexts: list[dict[str, Any]] | None = None,
    ) -> str:
        generate = getattr(self.context, "llm_generate", None)
        if generate is None:
            _warn_once("llm_generate", "[active_chat] 当前 AstrBot 版本缺少 llm_generate，无法生成主动消息")
            return ""

        kwargs: dict[str, Any] = {"chat_provider_id": provider_id, "prompt": prompt}
        if system_prompt:
            if _supports(generate, "system_prompt"):
                kwargs["system_prompt"] = system_prompt
            else:
                kwargs["prompt"] = f"{system_prompt}\n\n{prompt}"
                _warn_once(
                    "system_prompt",
                    "[active_chat] 当前 llm_generate 不支持 system_prompt，已将其合并进用户提示词",
                )
        if contexts:
            if _supports(generate, "contexts"):
                kwargs["contexts"] = contexts
            else:
                _warn_once(
                    "contexts",
                    "[active_chat] 当前 llm_generate 不支持 contexts，本次主动消息不会注入对话历史",
                )

        resp = await generate(**kwargs)
        return _completion_text(resp)

    async def llm_vision(self, provider_id: str, prompt: str, image_path: str) -> str:
        generate = getattr(self.context, "llm_generate", None)
        if generate is None:
            return ""

        image_key = ""
        for candidate in ("image_urls", "images", "image"):
            if _supports(generate, candidate):
                image_key = candidate
                break
        if not image_key:
            _warn_once(
                "image_param",
                "[active_chat] 当前 llm_generate 不支持图片输入，截屏主动消息将回退为普通主动消息",
            )
            return ""

        last_error = ""
        for reference in self._image_refs(image_path):
            kwargs: dict[str, Any] = {
                "chat_provider_id": provider_id,
                "prompt": prompt,
                image_key: [reference] if image_key != "image" else reference,
            }
            try:
                resp = await generate(**kwargs)
            except Exception as exc:
                last_error = str(exc)
                continue
            text = _completion_text(resp)
            if text:
                return text
        if last_error:
            logger.warning(f"[active_chat] 图片理解请求失败：{last_error}")
        return ""

    @staticmethod
    def _image_refs(image_path: str) -> list[str]:
        path = Path(image_path)
        refs = [str(path)]
        try:
            data = path.read_bytes()
            if 0 < len(data) <= 8 * 1024 * 1024:
                refs.append("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
        except Exception:
            pass
        return refs

    # ---------------- 发送 ----------------

    async def send_text(self, umo: str, text: str) -> None:
        body = (text or "").strip()
        if not body:
            return
        chain = None
        try:
            chain = MessageChain().message(body)
        except Exception:
            chain = None
        if chain is None:
            chain = MessageChain(chain=[Comp.Plain(body)])
        await self.context.send_message(umo, chain)

    async def send_image(self, umo: str, image_path: str) -> None:
        chain = None
        try:
            chain = MessageChain().file_image(str(image_path))
        except Exception:
            chain = None
        if chain is None:
            try:
                chain = MessageChain(chain=[Comp.Image.fromFileSystem(str(image_path))])
            except Exception:
                chain = MessageChain()
                chain.chain = [Comp.Image.fromFileSystem(str(image_path))]
        await self.context.send_message(umo, chain)
