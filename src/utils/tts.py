"""
TTS 工具模块
使用 espeak 系统命令将文本转为语音并播放
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path


class TTSEngine:
    """TTS 引擎：封装 espeak 系统命令，生成语音并播放"""

    def __init__(self, logger: logging.Logger | None = None, alsa_device: str = "wobot_local"):
        self.logger = logger or logging.getLogger(__name__)
        self._alsa_device = alsa_device
        self._available: bool | None = None

    def is_available(self) -> bool:
        """检查 espeak 和 aplay 是否可用"""
        if self._available is not None:
            return self._available
        self._available = shutil.which("espeak") is not None and shutil.which("aplay") is not None
        if not self._available and self.logger:
            self.logger.warning("[TTS] espeak or aplay not found, TTS disabled")
        return self._available

    async def speak(self, text: str, lang: str = "zh", speed: int = 160) -> bool:
        """将文本转为语音并播放

        Args:
            text: 要播报的文本
            lang: 语言代码（zh=中文, en=英文）
            speed: 语速（默认 160，espeak 默认 175）
        Returns:
            True 表示播放成功
        """
        if not self.is_available():
            if self.logger:
                self.logger.warning("[TTS] Not available, skipping speak")
            return False

        try:
            # 用 espeak 生成 WAV 到临时文件，再用 aplay 播放
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            # espeak 生成 WAV 文件
            proc = await asyncio.create_subprocess_exec(
                "espeak",
                "-v",
                lang,
                "-s",
                str(speed),
                "-a",
                "200",  # 振幅（默认 100，提高到 200 更响亮）
                "-w",
                tmp_path,
                text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                if self.logger:
                    self.logger.error(f"[TTS] espeak failed: {stderr.decode().strip()}")
                Path(tmp_path).unlink(missing_ok=True)
                return False

            # aplay 播放
            proc = await asyncio.create_subprocess_exec(
                "aplay",
                "-q",
                "-D",
                self._alsa_device,
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            Path(tmp_path).unlink(missing_ok=True)

            if proc.returncode != 0:
                if self.logger:
                    self.logger.error(f"[TTS] aplay failed: {stderr.decode().strip()}")
                return False

            if self.logger:
                self.logger.info(f"[TTS] Spoke: {text}")
            return True

        except Exception as e:
            if self.logger:
                self.logger.error(f"[TTS] speak error: {e}", exc_info=True)
            return False

    async def speak_pairing_code(self, code: str) -> bool:
        """播报配对数字，每个数字单独读出"""
        # 将 "1234" 转换为 "1, 2, 3, 4" 格式，每个数字清晰读出
        spaced = ", ".join(list(code))
        text = f"配对数字是, {spaced}"
        return await self.speak(text)

    async def speak_bind_success(self) -> bool:
        """播报绑定成功"""
        return await self.speak("绑定客户端成功")
