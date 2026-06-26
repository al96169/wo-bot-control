"""
音乐播放子服务
负责本地音频播放 + 网络推流播放 (DLNA/UPnP, AirPlay, RTMP/HLS)
由主服务 ServiceManager 以子进程方式启动，通过 stdin/stdout JSON 行协议通信。

协议:
  输入 (stdin):  {"id": "<request_id>", "cmd": "<command>", "params": {...}}
  输出 (stdout): {"id": "<request_id>", "type": "<response_type>", "data": {...}}

依赖:
  - mpg123: 本地 MP3 播放
  - amixer: 音量控制
  - minidlna: DLNA/UPnP 推流 (sudo apt-get install minidlna)
  - shairport-sync: AirPlay 推流 (sudo apt-get install shairport-sync)
  - ffmpeg: RTMP/HLS 推流
"""

import asyncio
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("music_player")

# ---- 配置 ----
# 媒体目录：优先从实际用户 home 获取（systemd 环境 HOME 可能是 / 或 /root）
def _resolve_media_dir() -> str:
    import pwd
    # 方案 A: 查找真实登录用户 (trae / ubuntu / jetson) 的 home
    for username in ["trae", "ubuntu", "jetson"]:
        try:
            home = pwd.getpwnam(username).pw_dir
            d = os.path.join(home, "media/music")
            if os.path.isdir(d):
                return d
        except (KeyError, FileNotFoundError):
            continue
    # 方案 B: expanduser（适用于有正常 HOME 的场景）
    expanded = os.path.expanduser("~/media/music")
    if not expanded.startswith("~") and os.path.isdir(expanded):
        return expanded
    # 方案 C: 硬编码兜底
    return "/home/trae/media/music"

DEFAULT_MEDIA_DIR = _resolve_media_dir()
DEFAULT_STREAM_NAME = "Wo-Bot"
DLNA_RENDERER_PORT = 49452  # gmediarender 端口范围: 49152-65535
RTMP_PORT = 1935


def _get_local_ip() -> str:
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _get_interface_name() -> str:
    """获取活跃的网络接口名（用于 UPnP 广播），返回如 wlan0 / eth0"""
    try:
        import subprocess
        result = subprocess.run(
            ["ip", "route", "get", "8.8.8.8"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=5,
        )
        parts = result.stdout.strip().split()
        if "dev" in parts:
            idx = parts.index("dev")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    except Exception:
        pass
    return "eth0"  # 默认有线


def _detect_usb_audio_device() -> str:
    """检测 USB 音频设备，返回 ALSA softvol 设备名 (独立音量控制)，未找到则返回 default"""
    try:
        import re
        with open("/proc/asound/cards", "r") as f:
            content = f.read()
        # 优先匹配 USB 设备，通过 softvol → dmix 实现独立音量控制
        for m in re.finditer(r"^\s*(\d+)\s*\[(\w+)\s*\].*USB", content, re.MULTILINE):
            return "wobot_local"
    except Exception:
        pass
    return "default"


def _detect_usb_card_number() -> int:
    """检测 USB 声卡编号 (如 2)，用于 amixer 音量控制。未找到返回 -1"""
    try:
        import re
        with open("/proc/asound/cards", "r") as f:
            content = f.read()
        for m in re.finditer(r"^\s*(\d+)\s*\[(\w+)\s*\].*USB", content, re.MULTILINE):
            return int(m.group(1))
    except Exception:
        pass
    return -1


def _detect_mixer_control(card_number: int) -> str:
    """检测声卡上的可用音量控制器名称，优先 Master → PCM → Speaker"""
    try:
        import subprocess
        result = subprocess.run(
            ["amixer", "-c", str(card_number), "scontrols"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            timeout=5,
        )
        output = result.stdout
        for ctrl in ["Master", "PCM", "Speaker", "Headphone"]:
            if ctrl in output:
                return ctrl
    except Exception:
        pass
    return "PCM"  # 默认尝试 PCM


class MusicPlayer:
    """音乐播放器核心"""

    def __init__(self, media_dir: str = DEFAULT_MEDIA_DIR):
        self.media_dir = Path(media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)

        # USB 声卡信息（用于音量控制）
        self._usb_card = _detect_usb_card_number()
        self._mixer_ctrl = _detect_mixer_control(self._usb_card) if self._usb_card >= 0 else "PCM"

        # 播放状态
        self._status = "stopped"  # stopped | playing | paused
        self._current_track: Optional[Dict[str, Any]] = None
        self._current_process = None  # type: Optional[asyncio.subprocess.Process]
        self._monitor_task = None  # type: Optional[asyncio.Task]  # 用于取消旧的播放监控任务
        self._volume = 75  # 默认音量 75%
        self._position = 0.0  # 当前播放位置（秒）
        self._playlist: List[Dict[str, Any]] = []  # 播放队列
        self._start_time = 0.0

        # 推流状态 — 支持多个服务同时运行
        self._streaming_services: set = set()  # 当前活跃的推流服务类型
        self._stream_processes: Dict[str, asyncio.subprocess.Process] = {}  # stream_type → 进程
        self._stream_ports: Dict[str, int] = {}  # stream_type → 端口
        self._active_source: Optional[str] = None  # 当前活跃音源 (Last-one-wins): "local"/"dlna"/"airplay"/None
        self._airplay_watchdog_started = False
        self._airplay_watchdog_task = None  # type: Optional[asyncio.Task]
        self._dlna_watchdog_task = None     # type: Optional[asyncio.Task]

        # gmediarender DLNA 渲染器子进程
        self._gmediarender_proc: Optional[asyncio.subprocess.Process] = None

        # DLNA 播放位置跟踪（用于前端进度条）
        self._dlna_position = 0.0       # gmediarender 上报的播放位置（秒）
        self._dlna_query_time = 0.0     # 上次查询位置的时间戳（用于推算实时位置）

    # ---- 歌曲管理 ----

    def list_songs(self) -> List[Dict[str, Any]]:
        """列出媒体文件夹中的歌曲"""
        songs = []
        supported = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma"}
        if self.media_dir.exists():
            for f in sorted(self.media_dir.iterdir()):
                if f.is_file() and f.suffix.lower() in supported:
                    songs.append({
                        "name": f.stem,
                        "filename": f.name,
                        "path": str(f),
                        "size": f.stat().st_size,
                        "format": f.suffix.lower().lstrip("."),
                    })
        return songs

    async def _get_duration(self, filepath: str) -> float:
        """用 ffprobe 获取音频文件的实际时长（秒）"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                filepath,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0 and stdout:
                return float(stdout.decode().strip())
        except (asyncio.TimeoutError, ValueError, Exception):
            pass
        return 0.0

    # ---- 播放控制 ----

    async def play(self, filename: Optional[str] = None) -> Dict[str, Any]:
        """播放指定歌曲或恢复播放 (Last-one-wins: 会挂起推流服务)"""
        # 如果指定了文件名，先加载到当前曲目
        if filename:
            filepath = self.media_dir / filename
            if not filepath.exists():
                return {"error": f"文件不存在: {filename}"}
            stat = filepath.stat()
            duration = await self._get_duration(str(filepath))
            self._current_track = {
                "name": filepath.stem,
                "filename": filepath.name,
                "path": str(filepath),
                "size": stat.st_size,
                "format": filepath.suffix.lower().lstrip("."),
                "duration": duration,
            }
            self._position = 0.0

        if not self._current_track:
            # 从播放列表取第一首
            if self._playlist:
                self._current_track = self._playlist.pop(0)
                self._position = 0.0
            else:
                return {"error": "没有选中歌曲且播放列表为空"}

        # 确保当前曲目不在播放列表中（避免 next_track 重复弹出同一首）
        # 当 play(filename) 显示指定歌曲时，该曲目可能仍在 _playlist 中
        if self._current_track:
            cur_name = self._current_track.get("filename", "")
            for i, t in enumerate(self._playlist):
                if t.get("filename") == cur_name:
                    self._playlist.pop(i)
                    break

        # 保存当前曲目引用，防止 _stop_process() 期间被 _monitor_playback 置为 None
        track = self._current_track

        # Last-one-wins: 本地播放抢占，将其他音源静音（不杀进程，保持连接）
        await self._set_active_source("local")

        # 停止当前播放
        await self._stop_process()

        # 停止后再次确认曲目没被清除（防御性检查）
        if not self._current_track:
            self._current_track = track

        # 启动 mpg123 播放
        try:
            cmd = [
                "mpg123",
                "-q",  # 安静模式
                "--buffer", "65536",  # 64KB 缓冲区（减少 underrun 噪音）
                "--preload", "0.5",   # 预加载 0.5 秒
                "-o", "alsa",
                "-a", _detect_usb_audio_device(),
            ]
            if self._position > 0.5:
                # 安全跳转：用 ffprobe 获取的实际时长做边界保护
                duration = track.get("duration", 0)
                if duration > 0:
                    safe_pos = min(self._position, max(0, duration - 2))
                else:
                    safe_pos = self._position
                skip_frames = int(safe_pos * 38)  # ~38 frames/sec for MP3
                if skip_frames > 0:
                    cmd += ["-k", str(skip_frames)]

            cmd.append(track["path"])

            self._current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._status = "playing"
            self._start_time = time.time()

            # 后台监控进程结束 (Python 3.6 兼容: ensure_future)
            self._monitor_task = asyncio.ensure_future(self._monitor_playback())

            return self.get_status()
        except FileNotFoundError:
            return {"error": "mpg123 未安装，请执行: sudo apt-get install -y mpg123"}
        except Exception as e:
            self._status = "stopped"
            return {"error": f"播放失败: {e}"}

    async def pause(self) -> dict:
        """暂停播放"""
        if self._status == "playing":
            # 记录当前播放位置
            elapsed = time.time() - self._start_time
            self._position += elapsed
            self._start_time = time.time()
            await self._stop_process()
            self._status = "paused"
        return self.get_status()

    async def resume(self) -> dict:
        """恢复播放"""
        if self._status == "paused" and self._current_track:
            return await self.play()
        return self.get_status()

    async def stop(self) -> dict:
        """停止播放 (Last-one-wins: 取消音源限制，恢复所有服务)"""
        await self._stop_process()
        self._position = 0.0
        self._current_track = None
        self._status = "stopped"
        # Last-one-wins: 本地播放结束，取消活跃音源限制
        await self._set_active_source(None)
        return self.get_status()

    async def seek(self, position: float) -> dict:
        """跳转到指定位置（秒）。
        
        使用 mpg123 -k 帧跳过实现跳转。有实际时长时做安全边界保护，
        防止跳过 EOF 导致 mpg123 异常退出和音乐中断。
        """
        self._position = max(0, position)
        if self._status in ("playing", "paused") and self._current_track:
            was_playing = self._status == "playing"
            if was_playing:
                return await self.play()
            else:
                return {"status": self._status, "position": self._position}
        return self.get_status()

    async def next_track(self) -> dict:
        """播放下一首"""
        if self._playlist:
            self._current_track = self._playlist.pop(0)
            self._position = 0.0
            return await self.play()
        await self.stop()
        return self.get_status()

    async def previous_track(self) -> dict:
        """重新播放当前歌曲"""
        self._position = 0.0
        if self._current_track:
            return await self.play()
        return self.get_status()

    # ---- 音量控制 ----

    async def set_volume(self, volume: int) -> dict:
        """设置音量 0-100"""
        volume = max(0, min(100, volume))
        self._volume = volume
        # 优先在 USB 声卡上设置，降级到全局
        cards_to_try = []
        if self._usb_card >= 0:
            cards_to_try.append(self._usb_card)
        cards_to_try.append(-1)  # -1 表示不指定卡号（默认声卡）

        ctrls = [self._mixer_ctrl] if self._mixer_ctrl else ["Master", "PCM", "Speaker"]

        success = False
        for card in cards_to_try:
            for ctrl in ctrls:
                try:
                    cmd = ["amixer", "-q"]
                    if card >= 0:
                        cmd += ["-c", str(card)]
                    cmd += ["sset", ctrl, f"{volume}%"]
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda c=cmd: subprocess.run(
                            c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                        ),
                    )
                    success = True
                    break
                except Exception:
                    continue
            if success:
                break

        if not success:
            logger.warning(f"无法设置 USB 声卡音量，card={self._usb_card}")

        return {"volume": self._volume}

    async def get_volume(self) -> int:
        """获取当前音量"""
        # 优先从 USB 声卡读取
        cards_to_try = []
        if self._usb_card >= 0:
            cards_to_try.append(self._usb_card)
        cards_to_try.append(-1)

        ctrls = [self._mixer_ctrl] if self._mixer_ctrl else ["Master", "PCM"]
        for card in cards_to_try:
            for ctrl in ctrls:
                try:
                    cmd = ["amixer"]
                    if card >= 0:
                        cmd += ["-c", str(card)]
                    cmd += ["sget", ctrl]
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda c=cmd: subprocess.run(
                            c, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=5,
                        ),
                    )
                    for line in result.stdout.split("\n"):
                        if "Playback" in line and "%" in line:
                            import re
                            m = re.search(r"\[(\d+)%\]", line)
                            if m:
                                self._volume = int(m.group(1))
                                return self._volume
                except Exception:
                    continue
        return self._volume

    # ---- 播放队列 ----

    async def playlist_add(self, filename: str) -> dict:
        """添加歌曲到播放队列"""
        filepath = self.media_dir / filename
        if not filepath.exists():
            return {"error": f"文件不存在: {filename}"}
        stat = filepath.stat()
        duration = await self._get_duration(str(filepath))
        track = {
            "name": filepath.stem,
            "filename": filepath.name,
            "path": str(filepath),
            "size": stat.st_size,
            "format": filepath.suffix.lower().lstrip("."),
            "duration": duration,
        }
        self._playlist.append(track)
        return {"playlist": self._playlist}

    def playlist_remove(self, index: int) -> dict:
        """从播放队列移除歌曲"""
        if 0 <= index < len(self._playlist):
            self._playlist.pop(index)
        return {"playlist": self._playlist}

    def playlist_clear(self) -> dict:
        """清空播放队列"""
        self._playlist.clear()
        return {"playlist": self._playlist}

    # ---- 推流控制 ----

    async def _set_active_source(self, source: Optional[str]) -> None:
        """Last-one-wins: 将指定音源设为 100%，其他音源静音 (0%)
        
        Args:
            source: "local" / "dlna" / "airplay" / None (全部恢复 100%)
        
        通过 ALSA softvol 独立控制每个音源的音量，不杀进程，保持手机连接不断开。
        """
        all_sources = {"local": "WoBot Local", "dlna": "WoBot DLNA", "airplay": "WoBot AirPlay"}
        card = str(self._usb_card) if self._usb_card >= 0 else "2"
        try:
            for key, mixer_name in all_sources.items():
                vol = "100%" if (source is None or key == source) else "0%"
                proc = await asyncio.create_subprocess_exec(
                    "amixer", "-c", card, "sset", mixer_name, vol,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            self._active_source = source
            logger.info(f"Last-one-wins: 活跃音源 = {source or '无（全开放）'}")
        except Exception as e:
            logger.error(f"设置活跃音源失败: {e}")

    async def _watch_airplay_signal(self) -> None:
        """后台监控 AirPlay 播放信号，检测到时将 AirPlay 设为活跃音源 (Last-one-wins)"""
        signal_file = "/tmp/wobot-airplay-start"
        last_mtime = 0.0
        while True:
            try:
                await asyncio.sleep(2)
                if os.path.exists(signal_file):
                    mtime = os.path.getmtime(signal_file)
                    if mtime > last_mtime:
                        last_mtime = mtime
                        logger.info("检测到 AirPlay 开始播放，设为活跃音源 (Last-one-wins)")
                        await self._stop_process()  # 停本地 mpg123
                        self._status = "playing"
                        self._current_track = None
                        self._position = 0.0
                        self._start_time = time.time()  # 推流播放开始时间，用于前端进度条
                        await self._set_active_source("airplay")
                else:
                    if last_mtime > 0:
                        logger.info("AirPlay 结束，取消活跃音源限制")
                        await self._set_active_source(None)
                        self._status = "stopped"
                    last_mtime = 0.0
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AirPlay 信号监控异常: {e}")

    async def _query_dlna_state(self, ip: str, port: int) -> Optional[str]:
        """通过 UPnP SOAP 查询 DLNA 播放状态（返回 TransportState 或 None）"""
        soap_body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '</u:GetTransportInfo>'
            '</s:Body>'
            '</s:Envelope>'
        )
        req = urllib.request.Request(
            f"http://{ip}:{port}/upnp/control/rendertransport1",
            data=soap_body.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"',
            },
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = resp.read().decode("utf-8")
        match = re.search(r"<CurrentTransportState>(\w+)</CurrentTransportState>", body)
        return match.group(1) if match else None

    async def _send_dlna_play(self) -> bool:
        """向 gmediarender 发送 Play SOAP 命令，用于 EOS 后恢复播放以触发酷狗切歌"""
        local_ip = _get_local_ip()
        dlna_port = DLNA_RENDERER_PORT
        soap_body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '<Speed>1</Speed>'
            '</u:Play>'
            '</s:Body>'
            '</s:Envelope>'
        )
        try:
            req = urllib.request.Request(
                f"http://{local_ip}:{dlna_port}/upnp/control/rendertransport1",
                data=soap_body.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#Play"',
                },
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                resp.read()
            logger.info("DLNA Play 命令已发送")
            return True
        except Exception as e:
            logger.warning(f"发送 DLNA Play 命令失败: {e}")
            return False

    async def _watch_dlna_signal(self) -> None:
        """后台监控 DLNA 播放状态（通过 UPnP GetTransportInfo），检测 PLAYING 转变时抢占 (Last-one-wins)
        同时查询 DLNA 播放位置，用于前端进度条显示。"""
        prev_state: Optional[str] = "NO_MEDIA_PRESENT"
        local_ip = _get_local_ip()
        dlna_port = DLNA_RENDERER_PORT
        while True:
            try:
                await asyncio.sleep(1)  # 1 秒轮询，提升进度更新频率
                state = await self._query_dlna_state(local_ip, dlna_port)
                if state is None:
                    continue
                if state != prev_state:
                    if state == "PLAYING" and prev_state != "PLAYING":
                        logger.info("检测到 DLNA 开始播放，设为活跃音源 (Last-one-wins)")
                        await self._stop_process()  # 停本地 mpg123
                        self._status = "playing"
                        self._current_track = None
                        self._position = 0.0
                        self._start_time = time.time()  # 推流播放开始时间，用于前端进度条
                        await self._set_active_source("dlna")
                    elif prev_state == "PLAYING" and state != "PLAYING":
                        logger.info(f"DLNA 播放结束 (state={state})，取消活跃音源限制")
                        await self._set_active_source(None)
                        self._status = "stopped"
                        self._dlna_position = 0.0
                    prev_state = state
                else:
                    prev_state = state
                # 查询 DLNA 播放位置（PLAYING 时每次循环都查，用于前端进度条）
                if state == "PLAYING":
                    pos_secs = await self._query_dlna_position(local_ip, dlna_port)
                    if pos_secs is not None:
                        self._dlna_position = pos_secs
                        self._dlna_query_time = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DLNA 状态监控异常: {e}")

    async def _query_dlna_position(self, ip: str, port: int) -> Optional[float]:
        """通过 UPnP SOAP 查询 DLNA 当前播放位置（秒），返回 None 表示查询失败"""
        soap_body = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID>'
            '</u:GetPositionInfo>'
            '</s:Body>'
            '</s:Envelope>'
        )
        try:
            req = urllib.request.Request(
                f"http://{ip}:{port}/upnp/control/rendertransport1",
                data=soap_body.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetPositionInfo"',
                },
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                body = resp.read().decode("utf-8")
            # 解析 RelTime (HH:MM:SS 格式)
            match = re.search(r"<RelTime>(\d{1,2}):(\d{2}):(\d{2})</RelTime>", body)
            if match:
                h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
                return float(h * 3600 + m * 60 + s)
            return None
        except Exception:
            return None

    async def stream_start(self, stream_type: str = "dlna") -> dict:
        """启动推流服务 (DLNA/UPnP, AirPlay, RTMP)，支持多服务同时运行"""
        if stream_type in self._streaming_services:
            return {"error": f"推流服务 {stream_type} 已在运行"}

        try:
            if stream_type == "dlna":
                result = await self._start_dlna()
            elif stream_type == "airplay":
                result = await self._start_airplay()
            elif stream_type == "rtmp":
                result = await self._start_rtmp()
            else:
                return {"error": f"不支持的推流类型: {stream_type}"}

            if result.get("error"):
                return result

            self._streaming_services.add(stream_type)
            return {
                "streaming": True,
                "stream_type": stream_type,
                "stream_port": self._stream_ports.get(stream_type, 0),
                "host": _get_local_ip(),
            }
        except Exception as e:
            logger.error(f"启动推流失败: {e}")
            return {"error": f"启动推流失败: {e}"}

    async def stream_stop(self, stream_type: Optional[str] = None) -> dict:
        """停止推流服务，不指定则停止全部"""
        stopped = []
        if stream_type:
            types_to_stop = [stream_type] if stream_type in self._streaming_services else []
        else:
            types_to_stop = list(self._streaming_services)

        for stype in types_to_stop:
            await self._stop_stream_process(stype)
            self._streaming_services.discard(stype)
            stopped.append(stype)

        logger.info(f"停止推流服务: {stopped}")
        return {
            "streaming": len(self._streaming_services) > 0,
            "active_services": list(self._streaming_services),
            "stopped": stopped,
        }

    # ---- 推流内部实现 ----

    async def _start_dlna(self) -> dict:
        """启动 DLNA/UPnP 推流 — 使用 gmediarender（成熟的开源 UPnP 渲染器，自带音量/进度/切歌）"""
        try:
            if self._gmediarender_proc and self._gmediarender_proc.returncode is None:
                logger.info("gmediarender already running, reusing")
                return {}

            # 杀掉 PulseAudio 释放 ALSA 声卡
            try:
                subprocess.run(
                    ["pkill", "-9", "pulseaudio"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                )
            except Exception:
                pass

            card = str(self._usb_card) if self._usb_card >= 0 else "2"

            # gmediarender: 成熟的 C 语言 DLNA 渲染器
            #   --gstout-audiosink/--gstout-audiodevice: 指定 ALSA 设备和输出
            #   --gstout-initial-volume-db 0.0: 初始音量 100%
            #   --mime-filter audio: 只接受音频流
            #   --logfile: 独立日志文件，不依赖 systemd-journald
            proc = await asyncio.create_subprocess_exec(
                "/usr/local/bin/gmediarender",
                "-f", DEFAULT_STREAM_NAME,          # 设备名: Wo-Bot
                "-p", str(DLNA_RENDERER_PORT),       # UPnP 端口
                "-I", "wlan0",                    # 绑定无线网卡（0.0.0.0 不兼容 libupnp）
                "--mime-filter", "audio",            # 只接受音频
                "--gstout-audiosink", "alsasink",    # ALSA 输出
                "--gstout-audiodevice", "wobot_dlna", # softvol 设备
                "--gstout-videosink", "fakesink",    # 丢弃视频
                "--gstout-initial-volume-db", "0.0", # 初始 100% 音量
                "--logfile", "/tmp/wobot-gmediarender.log",
                env={**os.environ, "UPNP_ENABLE_IPV6": "0"},  # 禁用 IPv6 避免绑定冲突
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._gmediarender_proc = proc
            self._stream_processes["dlna"] = proc
            self._stream_ports["dlna"] = DLNA_RENDERER_PORT

            # 恢复 DLNA softvol 为 100%
            await asyncio.create_subprocess_exec(
                "amixer", "-c", card, "sset", "WoBot DLNA", "100%",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # 启动 DLNA 状态监控（轮询 GetTransportInfo，Last-one-wins）
            if not self._dlna_watchdog_task or self._dlna_watchdog_task.done():
                self._dlna_watchdog_task = asyncio.ensure_future(self._watch_dlna_signal())
                logger.info("DLNA 状态监控已启动")

            logger.info(f"gmediarender 已启动，名称: {DEFAULT_STREAM_NAME}，端口 {DLNA_RENDERER_PORT}")
            return {}
        except FileNotFoundError:
            return {"error": "gmediarender 未安装，请执行: sudo apt-get install -y gmediarender"}
        except Exception as e:
            logger.error(f"gmediarender 启动失败: {e}")
            return {"error": f"DLNA 渲染器启动失败: {e}"}

    async def _start_airplay(self) -> dict:
        """启动 AirPlay 推流 — 使用 shairport-sync (ALSA dmix 输出，硬件混音器控制音量)"""
        try:
            # 使用 ALSA dmix 输出，与 gmediarender 共享 USB 声卡
            # 动态检测 USB 声卡编号用于硬件混音器控制
            # shairport-sync 默认找 Master 控制，USB 声卡只有 PCM，需显式指定
            # -B/-E 钩子: AirPlay 开始/停止播放时通知 music_player (Last-one-wins)
            card_num = self._usb_card if self._usb_card >= 0 else 2
            hook_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts")
            proc = await asyncio.create_subprocess_exec(
                "shairport-sync",
                "-a", DEFAULT_STREAM_NAME,   # 设备名: Wo-Bot
                "-o", "alsa",                # ALSA 后端
                "-B", f"{hook_dir}/airplay-start.sh",   # 开始播放钩子
                "-E", f"{hook_dir}/airplay-stop.sh",    # 停止播放钩子
                "--", "-d", "wobot_airplay",   # softvol → dmix (独立音量控制, 与 DLNA 共享声卡)
                "-m", f"hw:{card_num}",      # 混音器设备: 动态检测 USB 声卡编号
                "-c", "PCM",                 # 混音器控制: USB 声卡仅有的 PCM 控制器
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stream_processes["airplay"] = proc
            self._stream_ports["airplay"] = 5000  # shairport-sync 默认 AirPlay 端口
            # 重启后恢复 AirPlay softvol 为 100%（防止之前被抢占静音后重启导致无声）
            card = str(card_num)
            await asyncio.create_subprocess_exec(
                "amixer", "-c", card, "sset", "WoBot AirPlay", "100%",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info(f"shairport-sync 已启动，设备名: {DEFAULT_STREAM_NAME}，输出: ALSA dmix")

            # 启动 AirPlay 信号监控（只启动一次）
            if not self._airplay_watchdog_started:
                self._airplay_watchdog_started = True
                self._airplay_watchdog_task = asyncio.ensure_future(self._watch_airplay_signal())
                logger.info("AirPlay 信号监控已启动")

            return {}
        except FileNotFoundError:
            return {"error": "shairport-sync 未安装，请执行: sudo apt-get install -y shairport-sync"}
        except Exception as e:
            return {"error": f"shairport-sync 启动失败: {e}"}

    def _kill_orphaned_streams(self) -> None:
        """清理上一次运行残留的孤儿推流进程（防止新实例启动时端口冲突和混音）
        使用 SIGKILL 强制终止并轮询确认进程退出，最多等待 5 秒。"""
        for proc_name, match_pattern in [
            ("gmediarender", "gmediarender"),
            ("shairport-sync", "shairport-sync"),
        ]:
            try:
                # 先尝试 SIGTERM 优雅退出
                subprocess.run(
                    ["pkill", "-f", match_pattern],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3,
                )
            except Exception:
                pass
            # 等待最多 5 秒确认进程退出
            deadline = time.time() + 5
            while time.time() < deadline:
                result = subprocess.run(
                    ["pgrep", "-f", match_pattern],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    logger.info(f"已清理孤儿进程: {proc_name}")
                    break
                time.sleep(0.5)
            else:
                # 超时，使用 SIGKILL 强制终止
                logger.warning(f"孤儿进程 {proc_name} 未响应 SIGTERM，使用 SIGKILL")
                try:
                    subprocess.run(
                        ["pkill", "-9", "-f", match_pattern],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                except Exception:
                    pass
                time.sleep(0.5)
                logger.info(f"孤儿进程 {proc_name} 已强制终止")

        # 清理残留的 AirPlay 信号文件（防止 watchdog 误判）
        signal_file = "/tmp/wobot-airplay-start"
        if os.path.exists(signal_file):
            try:
                os.remove(signal_file)
                logger.info("已清理残留 AirPlay 信号文件")
            except Exception:
                pass

    async def _detect_and_reuse_streams(self) -> None:
        """检测已有推流进程（gmediarender/shairport-sync）并复用，避免重启服务时
        DLNA 设备反复出现消失（#14）和 AirPlay 连接断开（#13）。
        
        如果进程已存在：注册到 _streaming_services，启动对应的状态监控。
        如果进程不存在：清理残留信号文件。
        """
        # 检测 gmediarender（DLNA）
        try:
            result = await asyncio.create_subprocess_exec(
                "pgrep", "-f", "gmediarender",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=3)
            if result.returncode == 0 and stdout.strip():
                logger.info("检测到已有 gmediarender 进程，复用 DLNA 推流服务")
                self._streaming_services.add("dlna")
                self._stream_ports["dlna"] = DLNA_RENDERER_PORT
                # 启动 DLNA 状态监控
                if not self._dlna_watchdog_task or self._dlna_watchdog_task.done():
                    self._dlna_watchdog_task = asyncio.ensure_future(self._watch_dlna_signal())
                    logger.info("DLNA 状态监控已启动（复用已有进程）")
            else:
                logger.info("未检测到 gmediarender 进程，将重新启动 DLNA")
        except Exception as e:
            logger.warning(f"检测 gmediarender 进程异常: {e}")

        # 检测 shairport-sync（AirPlay）
        try:
            result = await asyncio.create_subprocess_exec(
                "pgrep", "-f", "shairport-sync",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(result.communicate(), timeout=3)
            if result.returncode == 0 and stdout.strip():
                logger.info("检测到已有 shairport-sync 进程，复用 AirPlay 推流服务")
                self._streaming_services.add("airplay")
                self._stream_ports["airplay"] = 5000
                # 启动 AirPlay 信号监控
                if not self._airplay_watchdog_started:
                    self._airplay_watchdog_started = True
                    self._airplay_watchdog_task = asyncio.ensure_future(self._watch_airplay_signal())
                    logger.info("AirPlay 信号监控已启动（复用已有进程）")
            else:
                logger.info("未检测到 shairport-sync 进程，将重新启动 AirPlay")
                signal_file = "/tmp/wobot-airplay-start"
                if os.path.exists(signal_file):
                    try:
                        os.remove(signal_file)
                        logger.info("已清理残留 AirPlay 信号文件")
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"检测 shairport-sync 进程异常: {e}")

        logger.info(f"推流进程检测完成，活跃服务: {self._streaming_services}")

    async def _start_rtmp(self) -> dict:
        """启动 RTMP/HLS 推流 — 使用 ffmpeg 作为 RTMP 服务器"""
        try:
            # ffmpeg 监听 RTMP 端口，将推流音频输出到 ALSA
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-loglevel", "quiet",
                "-listen", "1",
                "-f", "flv",
                "-i", f"rtmp://0.0.0.0:{RTMP_PORT}/live",
                "-f", "alsa",
                _detect_usb_audio_device(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stream_processes["rtmp"] = proc
            self._stream_ports["rtmp"] = RTMP_PORT
            logger.info(f"RTMP 服务器已启动，端口 {RTMP_PORT}，推流地址: rtmp://{_get_local_ip()}:{RTMP_PORT}/live")
            return {}
        except FileNotFoundError:
            return {"error": "ffmpeg 未安装，请执行: sudo apt-get install -y ffmpeg"}
        except Exception as e:
            return {"error": f"RTMP 服务器启动失败: {e}"}

    async def _stop_stream_process(self, stream_type: str) -> None:
        """停止指定推流子进程"""
        proc = self._stream_processes.pop(stream_type, None)
        self._stream_ports.pop(stream_type, None)
        self._streaming_services.discard(stream_type)
        if not proc:
            return

        try:
            proc.terminate()
            try:
                # shairport-sync 需要更多时间发送 mDNS goodbye 后才能优雅退出
                # gmediarender 需要时间从 SSDP 网络注销
                timeout = 10 if stream_type == "airplay" else 5
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                logger.info(f"推流进程 ({stream_type}) 已优雅退出")
            except asyncio.TimeoutError:
                logger.warning(f"推流进程 ({stream_type}) 未响应 SIGTERM，强制 SIGKILL")
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        except Exception as e:
            logger.error(f"停止推流进程失败 ({stream_type}): {e}")

        # 清理 DLNA 相关资源
        if stream_type == "dlna":
            self._gmediarender_proc = None
            # 停止 DLNA 状态监控
            if self._dlna_watchdog_task and not self._dlna_watchdog_task.done():
                self._dlna_watchdog_task.cancel()
                try:
                    await self._dlna_watchdog_task
                except asyncio.CancelledError:
                    pass
                self._dlna_watchdog_task = None
            # 恢复 PulseAudio
            try:
                subprocess.run(
                    ["/sbin/runuser", "-l", "jetson", "-c",
                     "pulseaudio --start --log-target=syslog"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                )
            except Exception:
                pass
            # 清理临时 PID 文件
            try:
                pid_file = os.path.join(tempfile.gettempdir(), "wobot-gmediarender.pid")
                if os.path.exists(pid_file):
                    os.remove(pid_file)
            except Exception:
                pass

    # ---- 状态 ----

    def get_status(self) -> dict:
        """获取当前播放状态。DLNA 播放时返回 gmediarender 上报的实时位置。"""
        if self._active_source == "dlna" and self._dlna_query_time > 0:
            # DLNA 播放中：用查询位置 + 时间差推算实时位置
            dlna_elapsed = time.time() - self._dlna_query_time
            current_position = self._dlna_position + dlna_elapsed
            status = "playing"
        else:
            current_position = self._position
            if self._status == "playing":
                current_position += time.time() - self._start_time
            status = self._status

        return {
            "status": status,
            "volume": self._volume,
            "position": round(current_position, 1),
            "current_track": self._current_track,
            "playlist": self._playlist,
            "streaming": len(self._streaming_services) > 0,
            "active_services": list(self._streaming_services),
            "active_source": self._active_source,
        }

    # ---- 内部方法 ----

    async def _stop_process(self) -> None:
        """停止当前播放进程"""
        # 先取消旧的播放监控任务，防止它在进程被杀后误清 _current_track
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None

        if self._current_process:
            try:
                self._current_process.terminate()
                try:
                    await asyncio.wait_for(self._current_process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    self._current_process.kill()
                    await self._current_process.wait()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.error(f"停止播放进程失败: {e}")
            self._current_process = None

    async def _monitor_playback(self) -> None:
        """监控播放进程，播放完毕后自动播放下一首"""
        proc = self._current_process
        if not proc:
            return
        try:
            # 读取 stderr 以捕获错误日志（避免管道阻塞）
            stderr_task = asyncio.ensure_future(proc.stderr.read()) if proc.stderr else None
            returncode = await proc.wait()
            if stderr_task:
                err_output = (await stderr_task).decode(errors="replace").strip()
                if err_output:
                    logger.warning(f"mpg123 stderr: {err_output}")

            # 确认进程没有被替换（防止过期监控干扰新播放）
            if proc is not self._current_process:
                return

            # 非零退出码表示 mpg123 异常退出（如 -k 跳帧越界），不自动切歌
            if returncode != 0:
                logger.warning(f"mpg123 exited with code {returncode}, stopping playback")
                self._status = "stopped"
                self._current_track = None
                self._position = 0.0
                return

            # 播放结束
            if self._status == "playing":
                elapsed = time.time() - self._start_time
                self._position += elapsed
                self._start_time = time.time()

                # 自动播放下一首
                if self._playlist:
                    self._current_track = self._playlist.pop(0)
                    self._position = 0.0
                    # 异步调度 play()，避免在当前 monitor 任务内调用
                    # _stop_process() 取消 self._monitor_task 导致的死锁
                    # （play() -> _stop_process() -> cancel/await 当前任务 = 事件循环死锁）
                    asyncio.ensure_future(self.play())
                else:
                    self._status = "stopped"
                    self._current_track = None
                    self._position = 0.0
                    # Last-one-wins: 本地播放列表放完，取消活跃音源限制
                    await self._set_active_source(None)
        except Exception as e:
            logger.error(f"播放监控异常: {e}")

    async def shutdown(self) -> None:
        """清理所有子进程（mpg123、gmediarender、shairport-sync 等）。
        推流服务作为音乐服务的子进程，跟随音乐服务启停。
        """
        logger.info("MusicPlayer shutdown: 清理所有子进程...")
        await self._stop_process()  # 停止本地 mpg123
        # 停止所有推流服务（通过子进程管理）
        for stype in list(self._streaming_services):
            logger.info(f"MusicPlayer shutdown: 停止 {stype} 推流进程...")
            await self._stop_stream_process(stype)
        # 兜底：pkill 强制清理，确保残留进程被终止
        # （解决 shairport-sync 不响应 SIGTERM 的问题）
        self._kill_orphaned_streams()
        logger.info("MusicPlayer shutdown 完成")


# ---- 命令处理 ----

_player: Optional[MusicPlayer] = None


def get_player() -> MusicPlayer:
    global _player
    if _player is None:
        _player = MusicPlayer()
    return _player


async def handle_command(cmd: str, params: dict) -> dict:
    """处理单条命令，返回响应 dict"""
    player = get_player()

    try:
        if cmd == "ping":
            return {"type": "pong", "data": {}}

        elif cmd == "list_songs":
            songs = player.list_songs()
            return {"type": "music_list", "data": {"songs": songs}}

        elif cmd == "play":
            filename = params.get("filename")
            return {"type": "music_status", "data": await player.play(filename)}

        elif cmd == "pause":
            return {"type": "music_status", "data": await player.pause()}

        elif cmd == "stop":
            return {"type": "music_status", "data": await player.stop()}

        elif cmd == "resume":
            return {"type": "music_status", "data": await player.resume()}

        elif cmd == "next":
            return {"type": "music_status", "data": await player.next_track()}

        elif cmd == "previous":
            return {"type": "music_status", "data": await player.previous_track()}

        elif cmd == "seek":
            position = float(params.get("position", 0))
            return {"type": "music_status", "data": await player.seek(position)}

        elif cmd == "set_volume":
            volume = int(params.get("volume", 75))
            return {"type": "music_volume", "data": await player.set_volume(volume)}

        elif cmd == "get_volume":
            vol = await player.get_volume()
            return {"type": "music_volume", "data": {"volume": vol}}

        elif cmd == "get_status":
            return {"type": "music_status", "data": player.get_status()}

        elif cmd == "playlist_add":
            filename = params.get("filename", "")
            return {"type": "music_playlist", "data": await player.playlist_add(filename)}

        elif cmd == "playlist_remove":
            index = int(params.get("index", -1))
            return {"type": "music_playlist", "data": player.playlist_remove(index)}

        elif cmd == "playlist_clear":
            return {"type": "music_playlist", "data": player.playlist_clear()}

        elif cmd == "stream_start":
            stream_type = params.get("stream_type", "dlna")
            return {"type": "music_stream", "data": await player.stream_start(stream_type)}

        elif cmd == "stream_stop":
            return {"type": "music_stream", "data": await player.stream_stop()}

        else:
            return {"type": "error", "data": {"code": 400, "message": f"Unknown command: {cmd}"}}

    except Exception as e:
        logger.error(f"Command '{cmd}' failed: {e}")
        return {"type": "error", "data": {"code": 500, "message": str(e)}}


async def _reader_loop():
    """从 stdin 逐行读取 JSON 命令，处理后写入 stdout"""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line = await reader.readline()
            if not line:
                logger.info("stdin closed, exiting")
                break

            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            try:
                msg = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from stdin: {line_str[:100]}")
                continue

            req_id = msg.get("id", "")
            cmd = msg.get("cmd", "")
            params = msg.get("params", {})

            result = await handle_command(cmd, params)
            result["id"] = req_id

            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Reader loop error: {e}")
            break

    logger.info("Music Player sub-service stopped")


async def main():
    """子服务主入口"""
    logger.info("Music Player sub-service started")
    logger.info(f"Media directory: {DEFAULT_MEDIA_DIR}")

    # 清理上一次运行残留的孤儿推流进程（防止混音和端口冲突）
    # _kill_orphaned_streams 内部已包含等待逻辑（最多 5 秒确认进程退出）
    player = get_player()
    player._kill_orphaned_streams()

    # 自动启动推流服务，作为音乐服务的子进程管理
    try:
        dlna_result = await player.stream_start("dlna")
        if dlna_result.get("error"):
            logger.warning(f"DLNA 自动启动失败: {dlna_result['error']}")
        else:
            logger.info("DLNA (gmediarender) 已自动启动")
    except Exception as e:
        logger.warning(f"DLNA 自动启动异常: {e}")

    try:
        airplay_result = await player.stream_start("airplay")
        if airplay_result.get("error"):
            logger.warning(f"AirPlay 自动启动失败: {airplay_result['error']}")
        else:
            logger.info("AirPlay (shairport-sync) 已自动启动")
    except Exception as e:
        logger.warning(f"AirPlay 自动启动异常: {e}")

    try:
        await _reader_loop()
    finally:
        # 服务停止时清理所有子进程（mpg123/gmediarender/shairport-sync 等）
        await player.shutdown()

    logger.info("Music Player sub-service stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # SIGTERM handler: 立即终止进程，子进程清理由 service_manager.stop_service() 的 pkill 兜底
    # （asyncio 中 sys.exit(0) 无法可靠触发 finally 块的 shutdown()，#5/#6 通过 service_manager 层解决）
    def _sigterm_handler(_signum, _frame):
        os._exit(0)
    signal.signal(signal.SIGTERM, _sigterm_handler)
    asyncio.run(main())
