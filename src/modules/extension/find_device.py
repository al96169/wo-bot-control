"""
寻找设备扩展模块
触发声光提示，帮助用户在 Web 控制端快速定位机器人位置。

- 若存在音频播放设备：循环播放提示音
- 若存在灯光设备（Rosmaster RGB LED）：闪烁
- 持续 30 秒后自动停止，或收到停止指令提前结束
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

from ..extension.base import ExtensionModule

# 预生成的高响度方波提示音文件路径
_BEEP_WAV = os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets", "wobot_beep.wav")

# 寻找设备持续时长（秒）
FIND_DURATION = 30
# LED 闪烁周期（秒），每个周期亮/灭各一次
LED_TOGGLE_INTERVAL = 0.5
# 提示音播放间隔（秒）
BEEP_INTERVAL = 1.5  # 每组"滴滴滴"提示音的间隔（秒）


class FindDeviceController(ExtensionModule):
    """寻找设备控制器 —— 触发声光提示定位机器人"""

    def __init__(self, bot: Any | None = None, service_manager=None, power_policy=None, logger=None):
        super().__init__("find_device", logger=logger)
        self._bot = bot  # Rosmaster 实例（用于 RGB LED 控制，可能为 None）
        self._service_manager = service_manager
        self._power_policy = power_policy
        self._active: bool = False
        self._task: asyncio.Task | None = None
        self._started_at: float = 0.0
        self._music_paused: bool = False

    # ---------- 生命周期 ----------

    async def start(self):
        self.running = True
        self.enabled = True
        if self.logger:
            self.logger.info("FindDeviceController started")

    async def stop(self):
        await self.stop_find()
        self.running = False
        self.enabled = False
        if self.logger:
            self.logger.info("FindDeviceController stopped")

    async def handle_command(self, command: str, data: dict) -> dict:
        if command == "status":
            return {"type": "find_device_status", "data": self.get_status()}
        return {"type": "error", "data": {"code": 400, "message": f"Unknown command: {command}"}}

    # ---------- 寻找设备控制 ----------

    async def start_find(self) -> dict:
        """启动声光提示"""
        # 已在寻找中：重置计时（取消旧任务后重新开始）
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._active = True
        import time

        self._started_at = time.monotonic()
        self._task = asyncio.ensure_future(self._find_loop())
        if self.logger:
            self.logger.info(f"Find device started (duration={FIND_DURATION}s)")
        return self.get_status()

    async def stop_find(self) -> dict:
        """停止声光提示并恢复设备"""
        was_active = self._active
        self._active = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # 确保灯关闭、音乐恢复（任务可能被外部 cancel，finally 已处理，这里兜底）
        await self._restore_devices()
        if was_active and self.logger:
            self.logger.info("Find device stopped")
        return self.get_status()

    def get_status(self) -> dict:
        """获取当前状态"""
        import time

        remaining: float = 0
        if self._active and self._started_at:
            elapsed = time.monotonic() - self._started_at
            remaining = max(0, FIND_DURATION - elapsed)
        return {
            "active": self._active,
            "remaining": round(remaining, 1),
            "duration": FIND_DURATION,
            "has_light": self._has_light(),
            "has_sound": shutil.which("aplay") is not None,
        }

    # ---------- 核心循环 ----------

    async def _find_loop(self):
        """声光提示主循环：LED 闪烁 + 循环提示音，持续 FIND_DURATION 秒"""
        import time

        self._music_paused = await self._pause_music()
        try:
            start = time.monotonic()
            next_beep = start  # 立即播放第一声
            led_on = False

            while self._active:
                elapsed = time.monotonic() - start
                if elapsed >= FIND_DURATION:
                    break

                # LED 闪烁
                led_on = not led_on
                if led_on:
                    await self._set_led(255, 0, 0)  # 红色
                else:
                    await self._set_led(0, 0, 0)  # 灭

                # 提示音（每 BEEP_INTERVAL 秒一次）
                now = time.monotonic()
                if now >= next_beep:
                    await self._play_beep()
                    next_beep = now + BEEP_INTERVAL

                await asyncio.sleep(LED_TOGGLE_INTERVAL)

        except asyncio.CancelledError:
            if self.logger:
                self.logger.info("Find device loop cancelled")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Find device loop error: {e}", exc_info=True)
        finally:
            self._active = False
            await self._restore_devices()

    async def _restore_devices(self):
        """恢复设备状态：关闭灯光、恢复音乐"""
        await self._set_led(0, 0, 0)
        if self._music_paused:
            await self._resume_music()
            self._music_paused = False

    # ---------- 灯光控制 ----------

    def _has_light(self) -> bool:
        """是否存在灯光设备"""
        return self._bot is not None and hasattr(self._bot, "set_colorful_lamps")

    async def _set_led(self, r: int, g: int, b: int) -> None:
        """设置 RGB LED 颜色（通过 Rosmaster 串口，对所有 LED 设置相同颜色）"""
        if not self._has_light():
            return
        try:
            loop = asyncio.get_event_loop()

            def _set_all_lamps():
                # 亚博 Rosmaster 有 4 个 RGB LED（编号 1-4），统一设置颜色
                if self._bot is None:
                    return
                for led_id in range(1, 5):
                    self._bot.set_colorful_lamps(led_id, r, g, b)

            await loop.run_in_executor(None, _set_all_lamps)
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Set LED failed (non-fatal): {e}")

    # ---------- 提示音 ----------

    async def _play_beep(self) -> None:
        """播放预生成的"滴滴滴"高响度方波提示音，最大音量，失败不影响寻找流程"""
        if not shutil.which("aplay") or not os.path.isfile(_BEEP_WAV):
            return
        try:
            # 预设 wobot_local 音量为 100%（最大）
            amixer_proc = await asyncio.create_subprocess_shell(
                'amixer -D wobot_local sset "WoBot Local" 100% -q',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await amixer_proc.wait()
            # 播放预生成的高响度方波提示音（双频叠加 1000+2500Hz，峰值 0.95）
            proc = await asyncio.create_subprocess_shell(
                f'aplay -q -D wobot_local "{_BEEP_WAV}"',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception:
            pass  # 提示音失败不影响寻找

    # ---------- 音乐协调 ----------

    async def _pause_music(self) -> bool:
        """暂停音乐播放（避免与提示音冲突），返回是否确实暂停了"""
        if not self._service_manager:
            return False
        try:
            svc = self._service_manager.get_service_status("music_player")
            if not svc or svc.get("status") != "running":
                return False
            result = await self._service_manager.send_subprocess_command("music_player", "status", {})
            is_playing = False
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    is_playing = data.get("state") == "playing" or data.get("playing") is True
            if is_playing:
                if self.logger:
                    self.logger.info("Pausing music for find device")
                await self._service_manager.send_subprocess_command("music_player", "pause", {})
                await asyncio.sleep(0.3)
                return True
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Failed to pause music (non-fatal): {e}")
        return False

    async def _resume_music(self):
        """恢复音乐播放"""
        if not self._service_manager:
            return
        try:
            if self.logger:
                self.logger.info("Resuming music after find device")
            await self._service_manager.send_subprocess_command("music_player", "resume", {})
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Failed to resume music (non-fatal): {e}")
