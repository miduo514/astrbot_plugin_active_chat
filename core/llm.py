"""提示词组装与文案清洗。

提示词结构严格遵循需求：
  system = 人格 system prompt + （可选）额外提示词
  固定任务指令放在 user prompt，避免污染 system 中「人格 → 额外提示词」的顺序
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from astrbot.api import logger

MAX_REPLY_CHARS = 500
MAX_DESCRIPTION_CHARS = 400

NORMAL_TASK_PROMPT = (
    "（这是一次没有新用户消息时的主动发言）请结合最近的聊天内容，"
    "用你当前的人格，在群里自然地开一个话头。\n"
    "要求：\n"
    "1. 口语化、简短，不超过 60 个字；\n"
    "2. 不要提及自己是 AI、机器人、模型，也不要提及定时、系统、主动消息等字眼；\n"
    "3. 不要使用 Markdown、列表或占位符；\n"
    "4. 只输出要发送的内容本身，不要加引号或解释。"
)

SCREENSHOT_TASK_PROMPT = (
    "（这是一次没有新用户消息时的主动发言）下面是当前电脑屏幕内容的客观描述：\n"
    "---\n"
    "{description}\n"
    "---\n"
    "请结合你的人格和最近的聊天内容，用一条自然、口语化的群聊消息，"
    "聊一聊屏幕上正在做的事或由此引发的相关话题。\n"
    "要求：\n"
    "1. 不超过 60 个字；\n"
    "2. 不要暴露「截屏」「截图」「监控」等字眼，也不要逐条罗列屏幕内容；\n"
    "3. 不要提及自己是 AI，不要使用 Markdown；\n"
    "4. 只输出要发送的内容本身。"
)

# 用户填写了额外提示词时，用它覆盖内置任务提示词，这里只保留最基本的输出格式要求
OVERRIDE_TASK_PROMPT = (
    "请严格按上面的要求，直接输出要发送到群里的消息内容；"
    "不要输出解释、引号、Markdown 或多余的前后缀。"
)

VISION_PROMPT = (
    "请客观、具体地描述这张电脑屏幕截图的内容，包括可见的应用、窗口标题、"
    "主要文字、当前正在进行的操作和整体氛围。不要评价隐私，不要推测用户身份。"
    "控制在 200 字以内，只输出描述本身。"
)


def build_system_prompt(persona_prompt: str, extra_prompt: str) -> str:
    parts: list[str] = []
    if persona_prompt and persona_prompt.strip():
        parts.append(persona_prompt.strip())
    if extra_prompt and extra_prompt.strip():
        parts.append(extra_prompt.strip())
    return "\n\n".join(parts)


def build_task_prompt(extra_prompt: str, image_description: str = "") -> str:
    """构造 user prompt。

    用户填写了额外提示词时，内置任务提示词（NORMAL_TASK_PROMPT /
    SCREENSHOT_TASK_PROMPT）不再生效，由用户的额外提示词覆盖；
    人格提示词仍保留在 system prompt 中。
    """
    extra = (extra_prompt or "").strip()
    description = (image_description or "").strip()

    if extra:
        parts = [OVERRIDE_TASK_PROMPT]
        if description:
            parts.append(f"当前屏幕内容描述：\n{description}")
        return "\n\n".join(parts)

    if description:
        return SCREENSHOT_TASK_PROMPT.format(description=description)
    return NORMAL_TASK_PROMPT


def clean_text(raw: str, limit: int = MAX_REPLY_CHARS) -> str:
    """去掉代码块围栏、首尾引号、多余空行，并按长度截断。"""
    text = (raw or "").strip()
    if not text:
        return ""

    if text.startswith("```"):
        text = text.strip("`").strip()
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower() in {"", "text", "markdown", "md", "plain", "plaintext"}:
                text = rest

    text = text.strip().strip('"“”「」『』\'').strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


class MessageComposer:
    """负责生成主动消息文本，以及对截屏内容的理解。"""

    def __init__(self, compat: Any):
        self.compat = compat

    async def compose(
        self,
        provider_id: str,
        persona_prompt: str,
        extra_prompt: str = "",
        history: Sequence[dict[str, Any]] | None = None,
        image_description: str = "",
    ) -> str:
        system_prompt = build_system_prompt(persona_prompt, extra_prompt)
        prompt = build_task_prompt(extra_prompt, image_description)

        raw = await self.compat.llm_text(
            provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
            contexts=list(history) if history else None,
        )
        return clean_text(raw)

    async def describe_image(self, provider_id: str, image_path: str) -> str:
        try:
            raw = await self.compat.llm_vision(provider_id, VISION_PROMPT, image_path)
        except Exception as exc:
            logger.warning(f"[active_chat] 截屏内容理解异常：{exc}")
            return ""
        return clean_text(raw, limit=MAX_DESCRIPTION_CHARS)
