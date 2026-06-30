"""
客户端一键喊话模块
接收客户端录音/电话音频，在机器人端播放。

协议: 4字节头长度(big-endian) + JSON头 + 音频二进制数据
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile

from ..extension.base import ExtensionModule


class VoiceBroadcastController(ExtensionModule):
    """喊话控制器 —— 接收客户端音频并在机器人端播放"""

    def __init__(self, service_manager=None, power_policy=None, logger=None):
        super().__init__("voice_broadcast", logger=logger)
        self._service_manager = service_manager
        self._power_policy = power_policy
        self._playing: bool = False
        self._phone_active: bool = False
        self._current_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    async def start(self):
        self.running = True
        self.enabled = True
        if self.logger:
            self.logger.info("VoiceBroadcastController started")

    async def stop(self):
        self.running = False
        self.enabled = False
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
        self._playing = False
        self._phone_active = False
        if self.logger:
            self.logger.info("VoiceBroadcastController stopped")

    # ---------- 命令处理 ----------

    async def handle_command(self, command: str, data: dict) -> dict:
        if command == "status":
            return {
                "type": "voice_broadcast_status",
                "data": {
                    "playing": self._playing,
                    "phone_active": self._phone_active,
                },
            }
        return {"type": "error", "data": {"code": 400, "message": f"Unknown command: {command}"}}

    # ---------- 核心播放逻辑 ----------

    async def play_audio(self, audio_data: bytes, mode: str) -> dict:
        """播放客户端发来的音频

        Args:
            audio_data: 原始音频二进制数据（Opus/WebM 编码）
            mode: 'record'（录音发送）或 'phone'（电话模式）

        Returns:
            状态字典
        """

        # 省电模式下电话模式不可用（喊话正常可用）
        if mode == "phone" and self._power_policy and self._power_policy.is_eco:
            return {
                "type": "voice_broadcast_ack",
                "data": {
                    "success": False,
                    "message": "省电模式下电话功能不可用",
                    "mode": mode,
                },
            }

        if not audio_data:
            return {
                "type": "voice_broadcast_ack",
                "data": {"success": False, "message": "音频数据为空", "mode": mode},
            }

        # 异步播放，不阻塞消息循环
        # 电话模式：chunk 还在播放则跳过，避免不断取消对方导致无声
        if mode == "phone" and self._current_task and not self._current_task.done():
            return {
                "type": "voice_broadcast_ack",
                "data": {"success": True, "message": "播放中", "mode": mode},
            }

        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

        self._current_task = asyncio.ensure_future(self._do_play(audio_data, mode))
        return {
            "type": "voice_broadcast_ack",
            "data": {"success": True, "message": "音频已接收，开始播放", "mode": mode},
        }

    async def _do_play(self, audio_data: bytes, mode: str):
        """实际播放逻辑"""
        tmp_path = None
        music_was_playing = False

        try:
            self._playing = True
            if mode == "phone":
                self._phone_active = True

            # 暂停音乐播放（跨进程）
            music_was_playing = await self._pause_music()

            # 写入临时文件
            suffix = ".webm"  # Opus in WebM container from MediaRecorder
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                f.write(audio_data)
                tmp_path = f.name

            if self.logger:
                self.logger.info(f"Voice broadcast: mode={mode}, size={len(audio_data)} bytes, file={tmp_path}")

            # ffmpeg 解码 Opus/WebM → WAV → aplay 播放（ALSA 输出到 USB 声卡）
            stderr = ""
            if shutil.which("ffmpeg"):
                # 通过 shell 管道: ffmpeg 解码 → aplay 播放
                import shlex

                cmd_str = f"ffmpeg -i {shlex.quote(tmp_path)} -f wav -loglevel error pipe:1 | aplay -q"
                proc = await asyncio.create_subprocess_shell(
                    cmd_str,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode != 0 and self.logger:
                    stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
                    self.logger.warning(f"voice playback pipe exited with code {proc.returncode}: {stderr.strip()}")
            else:
                # 无 ffmpeg 时直接 aplay（不支持 WebM，仅 PCM/WAV）
                proc = await asyncio.create_subprocess_exec(
                    "aplay",
                    "-q",
                    tmp_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if proc.returncode != 0 and self.logger:
                    stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
                    self.logger.warning(f"aplay exited with code {proc.returncode}: {stderr.strip()}")

        except asyncio.CancelledError:
            if self.logger:
                self.logger.info("Voice playback cancelled")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Voice playback error: {e}")
        finally:
            self._playing = False
            if mode == "phone":
                self._phone_active = False

            # 恢复音乐播放
            if music_was_playing:
                await self._resume_music()

            # 清理临时文件
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ---------- 音乐协调 ----------

    async def _pause_music(self) -> bool:
        """暂停音乐播放，返回是否确实暂停了"""
        if not self._service_manager:
            return False

        try:
            svc = self._service_manager.get_service_status("music_player")
            if not svc or svc.get("status") != "running":
                return False

            result = await self._service_manager.send_subprocess_command("music_player", "status", {})

            # 检查是否正在播放
            is_playing = False
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    is_playing = data.get("state") == "playing" or data.get("playing") is True

            if is_playing:
                if self.logger:
                    self.logger.info("Pausing music for voice broadcast")
                await self._service_manager.send_subprocess_command("music_player", "pause", {})
                # 等待暂停生效
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
                self.logger.info("Resuming music after voice broadcast")
            await self._service_manager.send_subprocess_command("music_player", "resume", {})
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Failed to resume music (non-fatal): {e}")

    # ---------- 电话模式生命周期 ----------

    def is_phone_active(self) -> bool:
        return self._phone_active

    def is_playing(self) -> bool:
        return self._playing
