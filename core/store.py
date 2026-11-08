"""持久化：data/plugin_data/astrbot_plugin_active_chat/state.json。

只保存观测/调试数据（累计触发次数、上次分支等），运行期的计时器与
连续失败次数 B 在重启后归零，避免停机一夜后一启动就按高概率连发。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

PLUGIN_NAME = "astrbot_plugin_active_chat"
STATE_VERSION = 1


def ensure_data_dir() -> Path:
    """返回插件数据目录（不存在则创建）。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:
        base = Path("data")

    path = base / "plugin_data" / PLUGIN_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        path = Path("data") / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
    return path


class StateStore:
    """极小的 JSON 状态仓库：写入节流 + 临时文件原子替换。"""

    FLUSH_DELAY = 5.0

    def __init__(self, data_dir: Path, log: Any = None):
        self.directory = Path(data_dir)
        self.path = self.directory / "state.json"
        self._log = log
        self._data: dict[str, Any] = {
            "version": STATE_VERSION,
            "updated_at": 0,
            "groups": {},
        }
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        self._load()

    # ---------------- 读取 ----------------

    def _load(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            groups = raw.get("groups")
            if isinstance(groups, dict):
                self._data["groups"] = groups
            self._data["version"] = raw.get("version", STATE_VERSION)
            self._data["updated_at"] = raw.get("updated_at", 0)
        except Exception as exc:
            self._warn(f"读取状态文件失败，将从空状态开始：{exc}")

    def get_group(self, umo: str) -> dict[str, Any]:
        entry = self._data["groups"].get(umo)
        return dict(entry) if isinstance(entry, dict) else {}

    # ---------------- 写入 ----------------

    def update_group(self, umo: str, **fields: Any) -> None:
        groups = self._data.setdefault("groups", {})
        if not isinstance(groups, dict):
            groups = {}
            self._data["groups"] = groups
        entry = groups.setdefault(umo, {})
        if not isinstance(entry, dict):
            entry = {}
            groups[umo] = entry
        entry.update(fields)
        entry["updated_at"] = int(time.time())
        self._data["updated_at"] = int(time.time())
        self.mark_dirty()

    def mark_dirty(self) -> None:
        self._dirty = True
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.create_task(self._flush_later())
        except RuntimeError:
            # 没有运行中的事件循环（例如卸载阶段），交给 flush() 同步落盘
            self._flush_task = None

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self.FLUSH_DELAY)
        except asyncio.CancelledError:
            raise
        if self._dirty:
            await asyncio.to_thread(self._write_sync)

    def _write_sync(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception as exc:
            self._warn(f"写入状态文件失败：{exc}")

    def flush(self) -> None:
        if self._dirty:
            self._write_sync()

    async def aclose(self) -> None:
        task = self._flush_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        self._flush_task = None
        self.flush()

    # ---------------- 杂项 ----------------

    def _warn(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log.warning(f"[active_chat] {message}")
        except Exception:
            pass
