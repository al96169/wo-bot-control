"""
客户端一键喊话模块
接收客户端录音/电话音频，在机器人端播放。

协议: 4字节头长度(big-endian) + JSON头 + 音频二进制数据
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import tempfile
import time

from ..extension.base import ExtensionModule


class VoiceBroadcastController(ExtensionModule):
    """喊话控制器 —— 接收客户端音频并在机器人端播放"""

    def __init__(self, service_manager=None, power_policy=None, logger=None):
        super().__init__("voice_broadcast", logger=logger)
        self._service_manager = service_manager
        self._power_policy = power_policy
        self._playing: bool = False
        self._phone_active: bool = False
        self._phone_beep_played: bool = False
        self._current_task: asyncio.Task | None = None
        # 电话模式顺序播放队列
        self._phone_queue: asyncio.Queue[tuple[bytes, float]] = asyncio.Queue()
        self._phone_playback_task: asyncio.Task | None = None

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
        if self._phone_playback_task and not self._phone_playback_task.done():
            self._phone_playback_task.cancel()
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

    async def play_audio(self, audio_data: bytes, mode: str, audio_format: str | None = None, sample_rate: int | None = None) -> dict:
        """播放客户端发来的音频

        Args:
            audio_data: 原始音频二进制数据（Opus/WebM 编码或原始 PCM）
            mode: 'record'（录音发送）或 'phone'（电话模式）
            audio_format: 'pcm_s16le' 表示原始 PCM（跳过 ffmpeg 解码）
            sample_rate: PCM 采样率

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

        if mode == "record":
            # 录音模式：一次性播放（可被新录音打断）
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
            self._current_task = asyncio.ensure_future(self._do_play_record(audio_data))
        else:
            # 电话模式：顺序播放队列，不打断当前播放
            if self.logger:
                fmt_info = f", format={audio_format}" if audio_format else ""
                self.logger.info(f"Phone audio queued: {len(audio_data)} bytes{fmt_info}")
            await self._phone_queue.put((audio_data, time.time(), audio_format, sample_rate))
            if self._phone_playback_task is None or self._phone_playback_task.done():
                self._phone_beep_played = False  # 新会话重置 beep
                self._phone_playback_task = asyncio.ensure_future(self._phone_playback_loop())

        return {
            "type": "voice_broadcast_ack",
            "data": {"success": True, "message": "音频已接收，开始播放", "mode": mode},
        }

    # ---------- 录音模式 ----------

    async def _do_play_record(self, audio_data: bytes):
        """录音模式：完整播放一段音频"""
        music_was_playing = False
        tmp_path = None

        try:
            self._playing = True
            music_was_playing = await self._pause_music()
            await self._play_beep("record")
            tmp_path = await self._write_temp(audio_data)
            await self._play_file(tmp_path)
        except asyncio.CancelledError:
            if self.logger:
                self.logger.info("Record playback cancelled")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Record playback error: {e}")
        finally:
            self._playing = False
            if music_was_playing:
                await self._resume_music()
            self._cleanup_temp(tmp_path)

    # ---------- 电话模式（持续流式播放）----------

    async def _phone_playback_loop(self):
        """电话模式播放循环：维持单一 aplay 进程。

        - Raw PCM 模式（format='pcm_s16le'）：直接写入 aplay stdin，无需 ffmpeg
        - WebM 模式（无 format 字段）：ffmpeg 解码 → 增量 PCM 写入
        """
        self._phone_active = True
        music_was_playing = False
        aplay_proc = None
        pcm_buffer = b""  # WebM 模式下累积已写入 aplay 的原始 PCM
        first_chunk = True  # 第一个 chunk 用于检测格式

        if self.logger:
            self.logger.info("Phone playback loop started")

        try:
            # 暂停音乐 + 提示音
            music_was_playing = await self._pause_music()
            if not self._phone_beep_played:
                await self._play_beep("phone")
                self._phone_beep_played = True

            # 启动持续播放的 aplay 进程（16-bit signed little-endian, 48kHz mono）
            aplay_proc = await asyncio.create_subprocess_exec(
                "aplay",
                "-q",
                "-D",
                "wobot_local",
                "-f",
                "S16_LE",
                "-r",
                "48000",
                "-c",
                "1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            while True:
                # 从队列取下一个音频块（5 秒无新数据则结束通话）
                try:
                    item = await asyncio.wait_for(self._phone_queue.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    if self.logger:
                        self.logger.info("Phone session ended (timeout)")
                    break

                audio_data = item[0]

                self._playing = True
                try:
                    # 自动识别格式：WebM 以 \x1a\x45\xdf\xa3 开头，其他视为原始 PCM
                    is_raw_pcm = audio_data[:4] != b"\x1a\x45\xdf\xa3"
                    if is_raw_pcm and aplay_proc.stdin:
                        # 原始 PCM：直接写入 aplay，无需 ffmpeg 解码
                        aplay_proc.stdin.write(audio_data)
                        await aplay_proc.stdin.drain()
                        if self.logger and first_chunk:
                            self.logger.info(
                                f"Phone raw PCM mode: writing {len(audio_data)}B directly to aplay"
                            )
                            first_chunk = False
                    else:
                        # WebM 模式：ffmpeg 解码 → 增量写入
                        full_pcm = await self._decode_to_pcm(audio_data)
                        if full_pcm and aplay_proc.stdin and len(full_pcm) > len(pcm_buffer):
                            new_pcm = full_pcm[len(pcm_buffer) :]
                            aplay_proc.stdin.write(new_pcm)
                            await aplay_proc.stdin.drain()
                            pcm_buffer = full_pcm
                            if self.logger and first_chunk:
                                self.logger.info(
                                    f"Phone WebM mode: webm={len(audio_data)}B, "
                                    f"pcm_total={len(full_pcm)}B, new={len(new_pcm)}B"
                                )
                                first_chunk = False
                        elif self.logger and first_chunk:
                            self.logger.warning(
                                f"Phone WebM skipped: webm={len(audio_data)}B, "
                                f"pcm_total={len(full_pcm) if full_pcm else 0}B, "
                                f"buffer={len(pcm_buffer)}B, stdin={aplay_proc.stdin is not None}"
                            )
                finally:
                    self._playing = False

        except asyncio.CancelledError:
            if self.logger:
                self.logger.info("Phone playback loop cancelled")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Phone playback error: {e}")
        finally:
            # 关闭 aplay stdin 让它自然结束（带超时强制 kill，防止进程残留）
            if aplay_proc:
                try:
                    if aplay_proc.stdin:
                        aplay_proc.stdin.close()
                    try:
                        await asyncio.wait_for(aplay_proc.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        if self.logger:
                            self.logger.warning("aplay did not exit, force killing")
                        aplay_proc.kill()
                        await aplay_proc.wait()
                except Exception:
                    pass
            self._phone_active = False
            self._phone_beep_played = False
            # 清空残余队列
            while not self._phone_queue.empty():
                try:
                    self._phone_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if music_was_playing:
                await self._resume_music()
            if self.logger:
                total_sec = len(pcm_buffer) / (48000 * 2) if pcm_buffer else 0
                self.logger.info(f"Phone playback loop ended (audio: {total_sec:.1f}s)")

    async def _decode_to_pcm(self, audio_data: bytes) -> bytes | None:
        """将 WebM 音频解码为原始 PCM（s16le, 48kHz, mono）

        Args:
            audio_data: 完整 WebM 文件数据

        Returns:
            原始 PCM 字节，或 None
        """
        if not shutil.which("ffmpeg"):
            return None

        tmp_path = None
        try:
            tmp_path = await self._write_temp(audio_data)

            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-i",
                tmp_path,
                "-f",
                "s16le",
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-loglevel",
                "error",
                "pipe:1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            pcm_out, pcm_err = await proc.communicate()

            if proc.returncode != 0:
                err_text = pcm_err.decode(errors="replace") if pcm_err else ""
                if self.logger and "Invalid data" not in err_text:
                    self.logger.debug(f"ffmpeg PCM decode warning: {err_text.strip()}")
                if not pcm_out:
                    return None

            return pcm_out

        except Exception as e:
            if self.logger:
                self.logger.error(f"PCM decode error: {e}")
            return None
        finally:
            self._cleanup_temp(tmp_path)

    # ---------- 底层文件播放 ----------

    async def _write_temp(self, audio_data: bytes) -> str:
        """写入临时文件，返回路径"""
        suffix = ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_data)
            return f.name

    async def _play_file(self, tmp_path: str):
        """ffmpeg 解码 → aplay 播放（阻塞直到播放完毕）"""
        if self.logger:
            self.logger.info(f"Playing audio file: {tmp_path} ({os.path.getsize(tmp_path)} bytes)")

        if shutil.which("ffmpeg"):
            cmd_str = f"ffmpeg -i {shlex.quote(tmp_path)} -f wav -loglevel error pipe:1 | aplay -q -D wobot_local"
            proc = await asyncio.create_subprocess_shell(
                cmd_str,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode != 0 and self.logger:
                stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
                self.logger.warning(f"Playback pipe exited with code {proc.returncode}: {stderr.strip()}")
        else:
            proc = await asyncio.create_subprocess_exec(
                "aplay",
                "-q",
                "-D",
                "wobot_local",
                tmp_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()

    def _cleanup_temp(self, tmp_path: str | None):
        """清理临时文件"""
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ---------- 通知提示音 ----------

    async def _play_beep(self, mode: str) -> None:
        """播放通知提示音，用 ffmpeg 生成短促的 sine 音调

        Args:
            mode: 'record' (叮咚) 或 'phone' (嘟)
        """
        if not shutil.which("ffmpeg"):
            return
        try:
            if mode == "record":
                # 录音模式：440→880Hz 双音 "叮咚"
                beep_cmd = (
                    "ffmpeg -f lavfi -i "
                    '"sine=frequency=440:duration=0.12,volume=1.0" '
                    "-f lavfi -i "
                    '"sine=frequency=880:duration=0.13,volume=1.0" '
                    '-filter_complex "[0][1]concat=n=2:v=0:a=1,volume=3.0" '
                    "-f wav -loglevel error pipe:1 | aplay -q -D wobot_local"
                )
            else:
                # 电话模式：1000Hz 单声 "嘟"
                beep_cmd = (
                    "ffmpeg -f lavfi -i "
                    '"sine=frequency=1000:duration=0.15,volume=1.0" '
                    '-af "volume=5.0" '
                    "-f wav -loglevel error pipe:1 | aplay -q -D wobot_local"
                )
            proc = await asyncio.create_subprocess_shell(
                beep_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
        except Exception:
            pass  # 提示音失败不影响喊话

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

            is_playing = False
            if isinstance(result, dict):
                data = result.get("data", result)
                if isinstance(data, dict):
                    is_playing = data.get("state") == "playing" or data.get("playing") is True

            if is_playing:
                if self.logger:
                    self.logger.info("Pausing music for voice broadcast")
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
