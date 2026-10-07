"""
音频输出总开关（T017 一键静音）

在硬件混音器层面静音 USB 声卡，从而一次性关闭**所有**音源输出
（本地播放 / DLNA / AirPlay / 蓝牙等），因为三路 softvol PCM
（wobot_local / wobot_dlna / wobot_airplay）最终都汇入
`plug:dmix:<USB_CARD>` 再经过声卡混音器。

设计要点:
  - 不依赖 PulseAudio，直接使用 amixer（alsa-utils）
  - 无 USB 声卡 / 无 amixer 时优雅降级，is_available() 返回 False
  - 所有子进程调用均带超时，避免拖死事件循环
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess

logger = logging.getLogger("wobot.audio_output")

# 优先使用的混音器控制器（能整体闸断输出的优先）
PREFERRED_CONTROLS = ("Master", "PCM", "Speaker", "Headphone", "Digital")

# 单次 amixer 调用超时（秒）
_AMIXER_TIMEOUT = 5


class AudioOutput:
    """音频输出总开关（硬件混音器静音）

    用法:
        audio = AudioOutput()
        await audio.set_mute(True)     # 静音所有输出
        await audio.set_mute(False)    # 恢复
    """

    def __init__(self, card: int | None = None, logger_: logging.Logger | None = None):
        self.logger = logger_ or logger
        self._card = card if card is not None else self._detect_usb_card()
        self._controls: list[str] = []
        self._muted = False
        self._detect_controls()

    # ---------- 探测 ----------

    @staticmethod
    def _detect_usb_card() -> int:
        """检测 USB 声卡编号（如 2），未找到返回 -1"""
        try:
            with open("/proc/asound/cards") as f:
                content = f.read()
            for m in re.finditer(r"^\s*(\d+)\s*\[(\w+)\s*\].*USB", content, re.MULTILINE):
                return int(m.group(1))
        except Exception as exc:  # /proc/asound 不存在（非 Linux / 无 ALSA）
            logger.debug("未检测到 /proc/asound/cards: %s", exc)
        return -1

    def _card_arg(self) -> list[str]:
        return ["-c", str(self._card)] if self._card >= 0 else []

    def _amixer_available(self) -> bool:
        return shutil.which("amixer") is not None

    def _detect_controls(self) -> None:
        """探测可用于静音的混音器控制器"""
        self._controls = []
        if not self._amixer_available():
            self.logger.warning("未找到 amixer（alsa-utils），一键静音不可用")
            return

        available = self._list_controls()
        if not available:
            self.logger.warning("声卡 card %s 上未发现可用混音器控制器", self._card)
            return

        # 优先使用能整体闸断输出的控制器；否则退化为全部控制器
        preferred = [c for c in PREFERRED_CONTROLS if c in available]
        self._controls = preferred if preferred else available
        self.logger.info("音频静音控制器已就绪: card=%s controls=%s", self._card, ",".join(self._controls))

    def _list_controls(self) -> list[str]:
        """读取声卡上的简单混音器控制器列表"""
        try:
            result = subprocess.run(
                ["amixer"] + self._card_arg() + ["scontrols"],
                capture_output=True,
                text=True,
                timeout=_AMIXER_TIMEOUT,
            )
        except Exception as exc:
            self.logger.warning("读取混音器控制器失败: %s", exc)
            return []

        # 输出形如: Simple mixer control 'PCM',0
        return re.findall(r"Simple mixer control '([^']+)'", result.stdout or "")

    # ---------- 状态 ----------

    def is_available(self) -> bool:
        """一键静音是否可用（有 amixer + 至少一个控制器）"""
        return bool(self._controls)

    def get_state(self) -> dict:
        """返回当前静音状态（同步读取硬件）"""
        state = {
            "enabled": self._muted,
            "available": self.is_available(),
            "card": self._card,
            "controls": list(self._controls),
        }
        if not self.is_available():
            state["enabled"] = False
            return state

        muted_flags = []
        for ctrl in self._controls:
            flag = self._read_control_mute(ctrl)
            if flag is not None:
                muted_flags.append(flag)

        if muted_flags:
            # 全部控制器都处于 mute 才算静音
            self._muted = all(muted_flags)
            state["enabled"] = self._muted
        return state

    def _read_control_mute(self, ctrl: str) -> bool | None:
        """读取单个控制器的静音状态，无法判断返回 None"""
        try:
            result = subprocess.run(
                ["amixer"] + self._card_arg() + ["sget", ctrl],
                capture_output=True,
                text=True,
                timeout=_AMIXER_TIMEOUT,
            )
        except Exception as exc:
            self.logger.debug("读取控制器 %s 状态失败: %s", ctrl, exc)
            return None

        output = result.stdout or ""
        # 只关心 Playback 段的 [on]/[off]
        playback = output.split("Capture", 1)[0]
        if "[off]" in playback:
            return True
        if "[on]" in playback:
            return False
        return None

    # ---------- 控制 ----------

    async def set_mute(self, enabled: bool) -> dict:
        """静音/取消静音所有音频输出

        Args:
            enabled: True 静音，False 恢复

        Returns:
            dict: 含 enabled/available/card/controls/failed 字段
        """
        if not self.is_available():
            return {
                "enabled": False,
                "available": False,
                "card": self._card,
                "controls": [],
                "failed": [],
                "error": "设备不支持一键静音（缺少 amixer 或混音器控制器）",
            }

        action = "mute" if enabled else "unmute"
        failed = []
        for ctrl in self._controls:
            ok = await self._apply_control(ctrl, action)
            if not ok:
                failed.append(ctrl)

        # 只有全部成功才认为状态已切换
        if failed:
            self.logger.warning("一键静音部分失败: action=%s failed=%s", action, failed)
            state = self.get_state()
            state["failed"] = failed
            state["error"] = "部分控制器设置失败: " + ",".join(failed)
            return state

        self._muted = enabled
        self.logger.info("一键静音: %s（%s）", "ON" if enabled else "OFF", ",".join(self._controls))
        state = self.get_state()
        state["failed"] = []
        return state

    async def _apply_control(self, ctrl: str, action: str) -> bool:
        """对单个控制器执行 mute/unmute"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "amixer",
                *self._card_arg(),
                "-q",
                "sset",
                ctrl,
                action,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_AMIXER_TIMEOUT)
        except asyncio.TimeoutError:
            self.logger.warning("amixer %s %s 超时", action, ctrl)
            return False
        except Exception as exc:
            self.logger.warning("amixer %s %s 失败: %s", action, ctrl, exc)
            return False

        if proc.returncode != 0:
            self.logger.warning(
                "amixer %s %s 返回 %s: %s",
                action,
                ctrl,
                proc.returncode,
                (stderr or b"").decode(errors="replace").strip(),
            )
            return False
        return True

    async def toggle(self) -> dict:
        """翻转静音状态"""
        current = self.get_state()
        return await self.set_mute(not current.get("enabled", False))
