"""截屏能力：优先 mss，其次 Pillow.ImageGrab。

设计要点：
- 任何失败（无桌面会话、锁屏、权限不足、依赖缺失、抓取到全黑帧）都只
  返回 None，由调用方回退为普通主动消息，绝不向上抛异常打断状态机。
- capture() 是同步阻塞调用，调用方需用 asyncio.to_thread 包裹。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any


class ScreenCapture:
    """屏幕截图与临时文件管理。"""

    KEEP = 20
    MIN_BYTES = 4096

    def __init__(self, directory: Path, log: Any = None):
        self.directory = Path(directory)
        self._log = log
        self._backend: str | None = None
        self._probed = False
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._warn(f"创建截图目录失败：{exc}")

    # ---------------- 能力探测 ----------------

    def available(self) -> bool:
        if not self._probed:
            self._probe()
        return self._backend is not None

    def _probe(self) -> None:
        self._probed = True
        try:
            import mss  # noqa: F401

            self._backend = "mss"
            return
        except Exception:
            pass
        try:
            from PIL import ImageGrab  # noqa: F401

            self._backend = "pillow"
            return
        except Exception:
            self._backend = None
            self._warn("未安装 mss/Pillow，截屏主动消息不可用，将始终发送普通主动消息")

    # ---------------- 截图 ----------------

    def capture(self, umo: str) -> str | None:
        """抓取主显示器并返回本地图片路径；失败返回 None。"""
        if not self.available():
            return None

        target = self._target_path(umo)
        backends = [self._backend]
        if self._backend == "mss":
            backends.append("pillow")

        errors: list[str] = []
        for backend in backends:
            if not backend:
                continue
            try:
                if backend == "mss":
                    self._grab_mss(target)
                elif backend == "pillow":
                    self._grab_pillow(target)
                else:
                    continue
                if target.exists() and target.stat().st_size > 0:
                    break
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
                self._safe_unlink(target)

        if not target.exists():
            self._warn(f"截屏失败（{'; '.join(errors) or '无可用后端'}），本次回退普通主动消息")
            return None

        if self._looks_blank(target):
            self._warn("截屏结果疑似全黑（可能处于锁屏或无桌面会话），本次回退普通主动消息")
            self._safe_unlink(target)
            return None

        self._cleanup()
        return str(target)

    def _grab_mss(self, target: Path) -> None:
        import mss
        import mss.tools

        with mss.mss() as sct:
            monitors = sct.monitors
            # monitors[0] 是全部显示器拼接，monitors[1] 是主显示器
            monitor = monitors[1] if len(monitors) > 1 else monitors[0]
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=str(target))

    def _grab_pillow(self, target: Path) -> None:
        from PIL import ImageGrab

        image = ImageGrab.grab()
        image.save(str(target), "PNG")

    # ---------------- 文件管理 ----------------

    def _target_path(self, umo: str) -> Path:
        digest = hashlib.sha1(umo.encode("utf-8")).hexdigest()[:12]
        return self.directory / f"{digest}_{int(time.time() * 1000)}.png"

    def _looks_blank(self, path: Path) -> bool:
        """判断是否全黑帧：有 Pillow 时看灰度极值，否则退化为文件大小启发式。"""
        try:
            from PIL import Image

            with Image.open(path) as image:
                extrema = image.convert("L").getextrema()
            return bool(extrema) and extrema[0] == extrema[-1]
        except Exception:
            try:
                return path.stat().st_size < self.MIN_BYTES
            except OSError:
                return True

    def _cleanup(self) -> None:
        try:
            files = sorted(
                (p for p in self.directory.glob("*.png") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for stale in files[self.KEEP :]:
                self._safe_unlink(stale)
        except Exception:
            pass

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def _warn(self, message: str) -> None:
        if self._log is None:
            return
        try:
            self._log.warning(f"[active_chat] {message}")
        except Exception:
            pass
