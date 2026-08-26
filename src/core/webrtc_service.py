"""
WebRTC 服务管理
- 信令协商（SDP offer/answer）
- ICE candidate 中继（绕过 aiortc on_icecandidate 不触发的 bug）
- DataChannel 双工通信
- 摄像头视频流推送（CameraVideoTrack）
- 连接生命周期管理
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

import cv2
import numpy as np
from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from av import VideoFrame

from .aioice_patch import apply as _apply_aioice_patch
from .aioice_patch import clear_pending_candidates, get_pending_candidates

STUN_SERVER = "stun:stun.l.google.com:19302"

# 启动时应用猴子补丁（全局一次）
_apply_aioice_patch()


def _resolve_server_ip(config: dict) -> str:
    """解析服务端对外公告 IP

    优先级: config.server.advertised_ip > 非 0.0.0.0 的 host > 自动探测本机局域网 IP
    """
    server_cfg = config.get("server", {})
    advertised = server_cfg.get("advertised_ip", "")
    if advertised:
        return advertised

    host = server_cfg.get("host", "0.0.0.0")
    if host and host != "0.0.0.0":
        return host

    # 自动探测: 连接外部地址获取本机局域网 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class CameraVideoTrack(VideoStreamTrack):
    """从 CameraManager 读取帧并转换为 WebRTC 视频轨

    aiortc 的 next_timestamp() 内置 30fps pacing（VIDEO_PTIME=1/30），
    每次 recv() 调用会自动 sleep 到下一帧时刻。
    摄像头以 10fps 采样时，30fps pacing 会自然产生重复帧，不影响播放流畅度。
    不再使用自实现的丢帧逻辑——那会与 aiortc 内部时钟冲突，
    导致 RTP 时间戳超前于真实时间，浏览器无限等待"未来帧" → 画面冻结。

    支持动态画质切换：通过 set_quality() 调整输出分辨率，无需断开连接。
    """

    kind = "video"

    # 画质 → (width, height) 映射（直播流专用）
    QUALITY_RESOLUTION_MAP = {
        "high": None,  # 使用配置页的原生分辨率
        "medium": (640, 480),
        "low": (480, 360),
    }

    def __init__(self, camera_manager, camera_id=0, fps=30, native_resolution=None, logger=None, client_id=""):
        super().__init__()
        self.camera_manager = camera_manager
        self.camera_id = camera_id
        self.fps = fps
        self.logger = logger
        self.client_id = client_id
        self._frame_count = 0
        # 画质控制 — 默认 medium(640x480)：Jetson Nano 软编码双路 1280x960 会 CPU 过载
        # 导致延迟严重 + USB 读取超时失败。用户手动切 high 时才回原生分辨率。
        self._quality_mode = "medium"
        self._native_width = native_resolution.get("width", 640) if native_resolution else 640
        self._native_height = native_resolution.get("height", 480) if native_resolution else 480
        self._target_width = 640
        self._target_height = 480
        self._resize_needed = self._target_width != self._native_width or self._target_height != self._native_height

    def get_quality_info(self) -> dict:
        return {
            "mode": self._quality_mode,
            "resolution": {"width": self._target_width, "height": self._target_height},
            "native_resolution": {"width": self._native_width, "height": self._native_height},
        }

    def set_quality(self, mode: str) -> dict:
        """动态切换画质，返回新的画质信息"""
        if mode not in ("auto", "high", "medium", "low"):
            mode = "high"
        self._quality_mode = mode
        res = self.QUALITY_RESOLUTION_MAP.get(mode)
        if res is None:
            self._target_width = self._native_width
            self._target_height = self._native_height
        else:
            self._target_width, self._target_height = res
        self._resize_needed = self._target_width != self._native_width or self._target_height != self._native_height
        info = self.get_quality_info()
        if self.logger:
            self.logger.info(
                f"[{self.client_id}] CameraVideoTrack(cam={self.camera_id}): "
                f"quality={mode} target={self._target_width}x{self._target_height}"
            )
        return info

    async def recv(self):
        # aiortc 内置 30fps pacing：next_timestamp() 会 sleep 到下一帧时刻
        pts, time_base = await self.next_timestamp()

        frame = self.camera_manager.get_frame(self.camera_id)
        if frame is None:
            frame = np.zeros((self._target_height, self._target_width, 3), dtype=np.uint8)
            self._frame_count += 1
            if self._frame_count == 1 and self.logger:
                self.logger.warning(
                    f"[{self.client_id}] CameraVideoTrack(cam={self.camera_id}): no frame from camera, using black"
                )
        else:
            frame = frame.copy()
            self._frame_count += 1
            if self._frame_count == 1 and self.logger:
                self.logger.info(
                    f"[{self.client_id}] CameraVideoTrack(cam={self.camera_id}): "
                    f"first frame shape={frame.shape}, mean=({frame.mean():.0f})"
                )
            elif self._frame_count % 300 == 0 and self.logger:
                self.logger.debug(
                    f"[{self.client_id}] CameraVideoTrack(cam={self.camera_id}): "
                    f"frame #{self._frame_count} shape={frame.shape}, ok"
                )

        # 动态分辨率缩放
        if self._resize_needed and (frame.shape[1] != self._target_width or frame.shape[0] != self._target_height):
            frame = cv2.resize(frame, (self._target_width, self._target_height))

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        video_frame.pts = pts
        video_frame.time_base = time_base

        return video_frame


class WebRTCService:
    """WebRTC 服务管理"""

    def __init__(self, message_handler, camera_manager=None, robot_info=None, config=None, logger=None):
        self.message_handler = message_handler
        self.camera_manager = camera_manager
        self.robot_info = robot_info or {}
        self.config = config or {}
        self.logger = logger or logging.getLogger("wobot")
        self._connections: dict[str, RTCPeerConnection] = {}
        self._data_channels: dict[str, RTCDataChannel] = {}
        self._video_tracks: dict[str, dict[int, CameraVideoTrack]] = {}
        self._cleaning_up: set[str] = set()

        # 画质管理
        self._stream_quality_mode: dict[str, str] = {}  # client_id → auto/high/medium/low
        self._auto_negotiators: dict[str, asyncio.Task] = {}  # client_id → auto-negotiation task

        # 服务端对外公告 IP（用于 SDP ICE candidate 中替换 .local）
        self._server_ip = _resolve_server_ip(self.config)
        self.logger.info(f"WebRTC server IP resolved: {self._server_ip}")

    # ---------- 画质管理 ----------

    def set_client_quality(self, client_id: str, mode: str) -> dict | None:
        """设置指定客户端的画质模式，返回画质信息（含所有 track）"""
        tracks = self._video_tracks.get(client_id, {})
        if not tracks:
            return None
        self._stream_quality_mode[client_id] = mode
        tracks_info: dict[int, object] = {}
        result: dict[str, object] = {"mode": mode, "tracks": tracks_info}
        for cam_id, track in tracks.items():
            info = track.set_quality(mode)
            tracks_info[cam_id] = info
        # 自动模式启动协商引擎，手动模式停止
        if mode == "auto":
            self._start_auto_negotiation(client_id)
        else:
            self._stop_auto_negotiation(client_id)
        return result

    def get_client_quality(self, client_id: str) -> str:
        return self._stream_quality_mode.get(client_id, "high")

    def record_rtt(self, client_id: str, rtt_ms: float):
        """记录客户端 RTT（由 ping/pong 回调）"""
        setattr(self, f"_last_rtt_{client_id}", rtt_ms)

    def _start_auto_negotiation(self, client_id: str):
        """启动自动画质协商后台任务"""
        if client_id in self._auto_negotiators:
            return
        self._auto_negotiators[client_id] = asyncio.ensure_future(self._auto_negotiate_loop(client_id))
        self.logger.info(f"[{client_id}] Auto quality negotiation started")

    def _stop_auto_negotiation(self, client_id: str):
        task = self._auto_negotiators.pop(client_id, None)
        if task and not task.done():
            task.cancel()
            self.logger.info(f"[{client_id}] Auto quality negotiation stopped")

    async def _auto_negotiate_loop(self, client_id: str):
        """自动协商循环：每 3 秒评估连接质量，调整画质"""
        # 降级/升级防抖计数器
        degrade_streak = 0  # 连续满足降级条件的次数
        upgrade_streak = 0  # 连续满足升级条件的次数
        current_level = "high"  # 当前生效档位
        DEGRADE_THRESHOLD = 1  # 连续 1 次即降级（≈3秒）
        UPGRADE_THRESHOLD = 2  # 连续 2 次即升级（≈6秒）

        try:
            while True:
                await asyncio.sleep(3)
                if self._stream_quality_mode.get(client_id) != "auto":
                    break  # 用户手动切走了

                # 获取连接质量（优先用 ICE stats，回退到 DC RTT）
                quality_level = await self._eval_connection_quality(client_id)
                if quality_level is None:
                    continue

                # 当前档位等值映射
                levels = ["low", "medium", "high"]
                cur_idx = levels.index(current_level) if current_level in levels else 2
                target_idx = levels.index(quality_level) if quality_level in levels else 2

                if target_idx < cur_idx:
                    # 需要降级
                    degrade_streak += 1
                    upgrade_streak = 0
                    if degrade_streak >= DEGRADE_THRESHOLD and current_level != quality_level:
                        self.logger.info(f"[{client_id}] Auto degrade: {current_level} → {quality_level}")
                        current_level = quality_level
                        self._apply_quality_to_tracks(client_id, current_level)
                        await self._notify_quality_changed(client_id, current_level)
                        degrade_streak = 0
                elif target_idx > cur_idx:
                    # 需要升级
                    upgrade_streak += 1
                    degrade_streak = 0
                    if upgrade_streak >= UPGRADE_THRESHOLD and current_level != quality_level:
                        self.logger.info(f"[{client_id}] Auto upgrade: {current_level} → {quality_level}")
                        current_level = quality_level
                        self._apply_quality_to_tracks(client_id, current_level)
                        await self._notify_quality_changed(client_id, current_level)
                        upgrade_streak = 0
                else:
                    degrade_streak = 0
                    upgrade_streak = 0
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.error(f"[{client_id}] Auto-negotiation error: {e}", exc_info=True)

    async def _eval_connection_quality(self, client_id: str) -> str | None:
        """评估连接质量，返回 high/medium/low 档位"""
        pc = self._connections.get(client_id)
        if not pc:
            return None

        # 通过 DC ping 评估 RTT
        dc = self._data_channels.get(client_id)
        rtt_ms = None
        if dc and dc.readyState == "open":
            try:
                t0 = time.time()
                # 发送 ping，等待 pong（pong 由 message_handler 处理）
                dc.send(f'{{"type":"ping","data":{{"ts":{t0:.6f}}}}}')
                # RTT 近似：用上次心跳的 RTT（如果有的话）
                rtt_ms = getattr(self, f"_last_rtt_{client_id}", None)
            except Exception:
                pass

        # 根据 RTT 判断
        if rtt_ms is not None:
            if rtt_ms < 50:
                return "high"
            elif rtt_ms < 150:
                return "medium"
            else:
                return "low"

        # 无 RTT 数据：DC 直连（局域网）→ 高画质，信令中继 → 中画质
        if dc and dc.readyState == "open":
            return "high"
        return "medium"

    def _apply_quality_to_tracks(self, client_id: str, level: str):
        """将画质应用到所有 video track（不发送通知）"""
        tracks = self._video_tracks.get(client_id, {})
        for track in tracks.values():
            track.set_quality(level)

    async def _notify_quality_changed(self, client_id: str, mode: str):
        """通知前端自动协商触发的画质变更"""
        tracks = self._video_tracks.get(client_id, {})
        info = None
        for track in tracks.values():
            info = track.get_quality_info()
            break
        if info:
            msg = {
                "type": "camera_stream_quality_changed",
                "data": {
                    "mode": mode,
                    "resolution": info["resolution"],
                    "reason": "auto",
                },
            }
            dc = self._data_channels.get(client_id)
            if dc and dc.readyState == "open":
                dc.send(json.dumps(msg))
            # 也通过 message_handler 发送（兼容 WS 路径）
            if self.message_handler:
                try:
                    await self.message_handler.handle("camera_stream_quality_changed", msg["data"])
                except Exception:
                    pass

    # ---------- ICE candidate 发送 ----------

    async def _flush_candidates(self, client_id: str, send_callback) -> int:
        """发送暂存的 ICE candidate 到客户端。

        必须在 send_callback(webrtc_answer) 之后调用，确保客户端已 setRemoteDescription。
        返回发送的 candidate 数量。
        """
        pending = get_pending_candidates()
        if not pending:
            return 0

        for cand_str in pending:
            payload = {
                "type": "webrtc_ice_candidate",
                "data": {"candidate": cand_str, "sdpMid": "", "sdpMLineIndex": 0},
            }
            try:
                await send_callback(payload)
                self.logger.info(f"[{client_id}] Sent host ICE candidate (post-answer): {cand_str[:80]}")
            except Exception as e:
                self.logger.error(f"[{client_id}] Failed to send ICE candidate: {e}", exc_info=True)

        self.logger.info(f"[{client_id}] ICE candidates flushed ({len(pending)} candidates)")
        return len(pending)

    # ---------- SDP 后处理 ----------

    def _postprocess_sdp(self, client_id: str, sdp: str) -> str:
        """SDP 回答后处理: 注入 ice-lite / 替换 .local → 真实 IP"""

        orig_cands = [line for line in sdp.split("\r\n") if line.startswith("a=candidate")]
        self.logger.info(f"[{client_id}] Original SDP candidates ({len(orig_cands)}): {[c[:80] for c in orig_cands]}")

        new_lines = []
        ice_lite_inserted = False
        replaced_count = 0

        for line in sdp.split("\r\n"):
            # .local mDNS 替换为真实 IP
            if line.startswith("a=candidate") and ".local" in line:
                parts = line.split()
                if len(parts) >= 6:
                    old_ip = parts[4]
                    parts[4] = self._server_ip
                    line = " ".join(parts)
                    replaced_count += 1
                    self.logger.info(f"[{client_id}] SDP ice .local→IP: {old_ip}→{self._server_ip}")

            # 在第一个 m= 行之前注入 a=ice-lite
            # 配合 aioice monkey-patch: 服务端不执行主动连通性检查，
            # 由浏览器单侧验证 ICE 连通性
            if not ice_lite_inserted and line.startswith("m="):
                new_lines.append("a=ice-lite")
                ice_lite_inserted = True

            new_lines.append(line)

        sdp = "\r\n".join(new_lines)

        self.logger.info(
            f"[{client_id}] ICE SDP postprocess: .local→IP={replaced_count}, "
            f"ice-lite={'yes' if ice_lite_inserted else 'no'}"
        )
        return sdp

    # ---------- 编解码器回填 ----------

    def _ensure_transceiver_codecs(self, client_id: str, pc: RTCPeerConnection) -> None:
        """aiortc 0.9.10 workaround: 客户端 offer 不含 video 时，手动填充默认编解码器"""
        try:
            import copy as _copy

            from aiortc import rtp as _rtp
            from aiortc.rtcpeerconnection import HEADER_EXTENSIONS, MEDIA_CODECS

            dynamic_pt = _rtp.DYNAMIC_PAYLOAD_TYPES.start
            for t in pc._RTCPeerConnection__transceivers:
                if t.mid is None:
                    t.mid = pc._RTCPeerConnection__nextAvailableMid()
                if not t._codecs:
                    codecs = []
                    raw_codecs = list(MEDIA_CODECS.get(t.kind, []))

                    def _codec_sort_key(c):
                        name = c.name.upper() if hasattr(c, "name") else ""
                        if "VP8" in name:
                            return 0
                        if "H264" in name:
                            return 1
                        if "VP9" in name:
                            return 2
                        return 3

                    raw_codecs.sort(key=_codec_sort_key)
                    for codec in raw_codecs:
                        codec = _copy.deepcopy(codec)
                        if codec.payloadType is None:
                            codec.payloadType = dynamic_pt
                            dynamic_pt += 1
                        codecs.append(codec)
                    t._codecs = codecs
                if not t._headerExtensions:
                    t._headerExtensions = list(HEADER_EXTENSIONS.get(t.kind, []))
                if t._offerDirection is None:
                    t._offerDirection = "sendrecv"
        except Exception as e:
            self.logger.warning(f"[{client_id}] aiortc codec backfill error (non-fatal): {e}")

    # ---------- 视频轨设置 ----------

    def _physical_first_cam_ids(self) -> list[int]:
        """物理摄像头优先排序（克隆摄像头共享源帧）。

        若先启动克隆摄像头（shared_from 指向源），其源尚未启动时会被当作
        物理设备打开，抢占同一个传感器/设备，导致真实摄像头流启动失败（黑屏）。
        先启动物理摄像头，克隆走 SharedCameraStream 共享分支即可。
        """
        keys = sorted(self.camera_manager.cameras.keys())
        return [c for c in keys if not self.camera_manager.cameras[c].get("shared_from")] + [
            c for c in keys if self.camera_manager.cameras[c].get("shared_from")
        ]

    async def _setup_video_tracks(self, client_id: str, pc: RTCPeerConnection) -> None:
        """为每个摄像头创建视频轨并绑定到 transceiver"""
        if not self.camera_manager:
            return

        for cam_id in self._physical_first_cam_ids():
            try:
                await self.camera_manager.start_stream(cam_id)
            except Exception as e:
                self.logger.warning(f"[{client_id}] Camera {cam_id} start failed: {e}")

        self._video_tracks[client_id] = {}

        transceivers = list(pc._RTCPeerConnection__transceivers)
        video_transceivers = [t for t in transceivers if t.kind == "video"]
        self.logger.info(
            f"[{client_id}] Found {len(video_transceivers)} video transceivers (total={len(transceivers)})"
        )

        cam_ids = self._physical_first_cam_ids()

        # 多客户端场景降低帧率，减少 Jetson Nano VP8 软编码 CPU 压力
        # 注意: 此时新连接已加入 _connections，所以 len(_connections) 包含当前客户端
        client_count = len(self._connections)
        track_fps = 5 if client_count > 1 else 10

        # 从 camera_manager 获取原生分辨率，传给 CameraVideoTrack 用于画质缩放
        native_res = self.camera_manager.resolution

        for i, cam_id in enumerate(cam_ids):
            if i >= len(video_transceivers):
                self.logger.warning(
                    f"[{client_id}] No transceiver for camera {cam_id} "
                    f"(only {len(video_transceivers)} video transceivers for {len(cam_ids)} cameras)"
                )
                continue

            transceiver = video_transceivers[i]
            transceiver.direction = "sendonly"
            self.logger.info(
                f"[{client_id}] Transceiver[{i}] → sendonly (mid={transceiver.mid}, kind={transceiver.kind})"
            )

            try:
                video_track = CameraVideoTrack(
                    self.camera_manager,
                    camera_id=cam_id,
                    fps=track_fps,
                    native_resolution=native_res,
                    logger=self.logger,
                    client_id=client_id,
                )
                self._video_tracks[client_id][cam_id] = video_track

                if transceiver.sender:
                    transceiver.sender.replaceTrack(video_track)
                    self.logger.info(
                        f"[{client_id}] Video track on transceiver[{i}] (camera {cam_id}, via replaceTrack)"
                    )
                else:
                    self.logger.warning(
                        f"[{client_id}] Transceiver[{i}] no sender, fallback to addTrack for camera {cam_id}"
                    )
                    pc.addTrack(video_track)
            except Exception as e:
                self.logger.warning(f"[{client_id}] Failed to setup video track camera {cam_id}: {e}")

    # ---------- 服务端 DataChannel ----------

    def _create_server_data_channel(self, client_id: str, pc: RTCPeerConnection) -> None:
        """创建服务端 DataChannel 用于命令下发"""
        server_dc = pc.createDataChannel("wobot-control-srv")

        @server_dc.on("open")
        def on_open():
            self.logger.info(f"[{client_id}] Server DataChannel opened!")
            self._data_channels[client_id] = server_dc

        @server_dc.on("message")
        def on_msg(msg):
            asyncio.ensure_future(self._on_dc_message(client_id, msg))

        @server_dc.on("close")
        def on_close():
            self.logger.info(f"[{client_id}] Server DataChannel closed")
            if self._data_channels.get(client_id) is server_dc:
                asyncio.ensure_future(self._on_dc_close(client_id))

        self.logger.info(f"[{client_id}] Server-side DC created: {server_dc.label}")

    # ---------- 核心: 创建 PeerConnection ----------

    async def create_peer_connection(
        self, client_id: str, sdp_offer: str, send_callback=None, turn: dict | None = None
    ) -> str:
        """处理客户端的 SDP offer，返回 SDP answer

        turn: 信令服务器下发的 TURN 凭证 {host, username, credential, ttl}。
        远程（4G 手机）场景必须配置，否则手机 relay 候选无法与本机匹配。
        """
        ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
        if turn and turn.get("host") and turn.get("username") and turn.get("credential"):
            host = turn["host"]
            ice_servers.append(
                RTCIceServer(urls=[f"turn:{host}:3478", f"turn:{host}:3478?transport=tcp"], username=turn["username"], credential=turn["credential"])
            )
            self.logger.info(f"[{client_id}] TURN configured: turn:{host}:3478")

        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))

        # 清理旧连接（幂等安全）
        old_pc = self._connections.get(client_id)
        if old_pc:
            self.logger.info(f"[{client_id}] Old connection found, cleaning up before reconnect")
            await self._cleanup_connection(client_id)

        self._connections[client_id] = pc

        # ---- 事件注册 ----

        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            if candidate and send_callback:
                try:
                    cand_str = candidate.candidate if hasattr(candidate, "candidate") else str(candidate)
                    if cand_str:
                        await send_callback(
                            {
                                "type": "webrtc_ice_candidate",
                                "data": {
                                    "candidate": cand_str,
                                    "sdpMid": getattr(candidate, "sdpMid", None) or "",
                                    "sdpMLineIndex": getattr(candidate, "sdpMLineIndex", None) or 0,
                                },
                            }
                        )
                        self.logger.info(f"[{client_id}] Sent ICE candidate: {cand_str[:80]}")
                except Exception as e:
                    self.logger.error(f"[{client_id}] Failed to send ICE candidate: {e}", exc_info=True)

        @pc.on("datachannel")
        def on_datachannel(channel: RTCDataChannel):
            self.logger.info(f"DataChannel opened by client {client_id}: {channel.label}")
            self._data_channels[client_id] = channel
            channel.on("message", lambda msg: asyncio.ensure_future(self._on_dc_message(client_id, msg)))
            channel.on("close", lambda: self._on_dc_close(client_id))

        @pc.on("iceconnectionstatechange")
        def on_ice_state_change():
            state = pc.iceConnectionState
            self.logger.info(f"[{client_id}] ICE connection state: {state}")
            if state in ("failed", "closed", "disconnected"):
                asyncio.ensure_future(self._cleanup_connection(client_id))

        @pc.on("connectionstatechange")
        def on_connection_state_change():
            state = pc.connectionState
            dtls_state = (
                pc._RTCPeerConnection__dtlsTransport.state
                if hasattr(pc, "_RTCPeerConnection__dtlsTransport")
                else "N/A"
            )
            self.logger.info(
                f"[{client_id}] Connection state: {state}, ICE: {pc.iceConnectionState}, DTLS: {dtls_state}"
            )
            if state in ("failed", "closed"):
                self.logger.warning(f"[{client_id}] Connection failed/closed, cleaning up")
                asyncio.ensure_future(self._cleanup_connection(client_id))

        # ---- SDP 协商 ----

        offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
        await pc.setRemoteDescription(offer)
        self.logger.info(f"[{client_id}] setRemoteDescription done")

        # 设置视频轨（在 createAnswer 之前，确保 SDP 包含 m=video）
        await self._setup_video_tracks(client_id, pc)

        # 回填编解码器
        self._ensure_transceiver_codecs(client_id, pc)

        # 生成并处理 answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        sdp = self._postprocess_sdp(client_id, pc.localDescription.sdp)

        # 诊断: 确认 answer SDP 关键媒体行
        has_app = "m=application" in sdp
        has_video = "m=video" in sdp
        self.logger.info(f"[{client_id}] Answer SDP: DataChannel={has_app}, Video={has_video}")
        if has_video:
            vid_lines = [
                line for line in sdp.split("\r\n") if line.startswith("m=video") or line.startswith("a=rtpmap")
            ]
            self.logger.info(f"[{client_id}] Answer SDP video lines: {vid_lines[:5]}")

        # 服务端 DataChannel（SCTP 协商完成后）
        if has_app:
            try:
                self._create_server_data_channel(client_id, pc)
            except Exception as e:
                self.logger.warning(f"[{client_id}] Server DC creation failed: {e}")

        self.logger.info(f"WebRTC peer connection created for {client_id}")

        # 3 秒后诊断 DTLS 状态
        asyncio.ensure_future(self._diag_dtls_state(client_id, pc))

        return sdp

    # ---------- ICE candidate 外部接口 ----------

    async def flush_pending_ice_candidates(self, client_id: str, send_callback) -> int:
        """发送 monkey-patch 期间收集的 ICE candidate（由 websocket_server 在 answer 之后调用）"""
        return await self._flush_candidates(client_id, send_callback)

    async def add_ice_candidate(self, client_id: str, candidate: str, sdp_mid: str, sdp_mline_index: int):
        """添加远端 ICE candidate"""
        pc = self._connections.get(client_id)
        if not pc:
            self.logger.warning(f"[{client_id}] No peer connection, dropping ICE candidate")
            return
        try:
            parts = candidate.strip().split()
            if len(parts) >= 8 and parts[0].startswith("candidate:"):
                foundation = parts[0].split(":", 1)[1]
                component = int(parts[1])
                protocol = parts[2].lower()
                priority = int(parts[3])
                ip = parts[4]
                port = int(parts[5])
                cand_type = parts[7]

                related_addr = None
                related_port = None
                for i, p in enumerate(parts[8:], 8):
                    if p == "raddr" and i + 1 < len(parts):
                        related_addr = parts[i + 1]
                    elif p == "rport" and i + 1 < len(parts):
                        related_port = int(parts[i + 1])

                ice = RTCIceCandidate(
                    foundation=foundation,
                    component=component,
                    ip=ip,
                    port=port,
                    priority=priority,
                    protocol=protocol,
                    type=cand_type,
                    relatedAddress=related_addr,
                    relatedPort=related_port,
                    sdpMid=sdp_mid if sdp_mid else None,
                    sdpMLineIndex=sdp_mline_index if sdp_mline_index is not None else None,
                )
                result = pc.addIceCandidate(ice)
                if result is not None and hasattr(result, "__await__"):
                    await result
                self.logger.info(f"[{client_id}] ICE candidate added: {ip}:{port} ({cand_type})")
            else:
                self.logger.warning(f"[{client_id}] Invalid candidate format: {candidate[:60]}")
        except Exception as e:
            self.logger.error(f"[{client_id}] Failed to add ICE candidate: {e}", exc_info=True)

    # ---------- DataChannel 消息 ----------

    async def _on_dc_message(self, client_id: str, msg):
        import json
        import struct

        if isinstance(msg, str):
            try:
                data = json.loads(msg)
            except Exception:
                self.logger.warning(f"[{client_id}] Invalid JSON in DataChannel")
                return
        elif isinstance(msg, bytes):
            # 尝试先当 JSON 文本解析
            try:
                data = json.loads(msg.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # 二进制消息：解析混合协议 [4B header 长度(大端)] [JSON header] [payload]
                try:
                    if len(msg) < 4:
                        self.logger.warning(f"[{client_id}] DC binary too short: {len(msg)} bytes")
                        return
                    header_len = struct.unpack(">I", msg[:4])[0]
                    if 4 + header_len > len(msg):
                        self.logger.warning(f"[{client_id}] DC binary header length mismatch: {header_len}")
                        return
                    header_json = msg[4 : 4 + header_len].decode("utf-8")
                    header = json.loads(header_json)
                    msg_type = header.get("type", "raw")
                    msg_data = header.get("data", {})
                    msg_data["_audio_data"] = msg[4 + header_len :]

                    if self.message_handler:
                        result = await self.message_handler.handle(msg_type, msg_data)
                        if result and isinstance(result, dict):
                            await self.send_message(client_id, result)
                    return
                except Exception as e:
                    self.logger.error(f"[{client_id}] Failed to parse DC binary: {e}", exc_info=True)
                    return
        else:
            return

        if self.message_handler:
            msg_type = data.get("type", "raw")
            msg_data = data.get("data", {})
            # 设置当前 DC 上下文，供 message_handler 分块发送
            self.message_handler._dc_client_id = client_id
            self.message_handler._webrtc_service = self
            result = await self.message_handler.handle(msg_type, msg_data)
            # 清理上下文
            self.message_handler._dc_client_id = None
            self.message_handler._webrtc_service = None
            # 将响应通过 DataChannel 发回客户端
            if result and isinstance(result, dict):
                await self.send_message(client_id, result)

    def _on_dc_close(self, client_id: str):
        self.logger.info(f"[{client_id}] DataChannel closed")
        self._data_channels.pop(client_id, None)

    # ---------- DTLS 诊断 ----------

    async def _diag_dtls_state(self, client_id: str, pc):
        """连接建立 3 秒后检查 DTLS 状态"""
        await asyncio.sleep(3)
        try:
            # 检查每个 transceiver 的 DTLS 状态
            transceivers = getattr(pc, "_RTCPeerConnection__transceivers", [])
            for i, t in enumerate(transceivers):
                dtls = getattr(t, "_transport", None)
                if dtls:
                    ice = getattr(dtls, "transport", None)
                    ice_state = getattr(ice, "state", "?") if ice else "?"
                    dtls_state = getattr(dtls, "state", "?")
                    encrypted = getattr(dtls, "encrypted", None)
                    self.logger.info(
                        f"[{client_id}] DTLS diag: transceiver[{i}] "
                        f"kind={getattr(t, 'kind', '?')} "
                        f"ICE={ice_state} DTLS={dtls_state} encrypted={encrypted}"
                    )
            # 检查 SCTP / DataChannel
            sctp = getattr(pc, "_RTCPeerConnection__sctp", None)
            if sctp:
                sctp_dtls = getattr(sctp, "transport", None)
                if sctp_dtls:
                    sctp_ice = getattr(sctp_dtls, "transport", None)
                    sctp_ice_state = getattr(sctp_ice, "state", "?") if sctp_ice else "?"
                    sctp_dtls_state = getattr(sctp_dtls, "state", "?")
                    self.logger.info(f"[{client_id}] DTLS diag: SCTP ICE={sctp_ice_state} DTLS={sctp_dtls_state}")
        except Exception as e:
            self.logger.warning(f"[{client_id}] DTLS diag failed: {e}")

    # ---------- 连接清理 ----------

    async def _cleanup_connection(self, client_id: str):
        """清理连接（WebSocket 断开或 ICE 失败时调用），幂等安全"""

        if client_id in self._cleaning_up:
            self.logger.info(f"[{client_id}] Cleanup already in progress, skipping")
            return
        self._cleaning_up.add(client_id)

        try:
            pc = self._connections.pop(client_id, None)
            if pc:
                try:
                    await pc.close()
                except Exception:
                    pass

            self._data_channels.pop(client_id, None)

            tracks = self._video_tracks.pop(client_id, {})
            for cam_id, track in tracks.items():
                try:
                    track.stop()
                except Exception:
                    pass
                try:
                    await self.camera_manager.stop_stream(cam_id)
                except Exception:
                    pass

            # 停止画质自动协商
            self._stop_auto_negotiation(client_id)
            self._stream_quality_mode.pop(client_id, None)

            # 仅当没有其他活跃连接时才清除全局候选队列
            if len(self._connections) == 0:
                clear_pending_candidates()
                self.logger.info(
                    f"[{client_id}] Cleaned up ({len(tracks)} video tracks, "
                    f"pending_candidates cleared, no other connections)"
                )
            else:
                other_ids = list(self._connections.keys())
                self.logger.info(
                    f"[{client_id}] Cleaned up ({len(tracks)} video tracks, other connections: {other_ids})"
                )
        finally:
            self._cleaning_up.discard(client_id)

    # ---------- 消息发送与状态 ----------

    async def send_message(self, client_id: str, data):
        """通过 DataChannel 发送消息"""
        dc = self._data_channels.get(client_id)
        if dc and dc.readyState == "open":
            import json

            dc.send(json.dumps(data) if isinstance(data, dict) else data)

    def get_open_data_channels(self) -> dict[str, RTCDataChannel]:
        """返回所有已打开（readyState=open）的 DataChannel，供 WS 触发的分块传输使用"""
        return {
            cid: dc
            for cid, dc in self._data_channels.items()
            if dc is not None and getattr(dc, "readyState", "") == "open"
        }

    async def broadcast_message(self, data):
        """向所有 DataChannel 客户端广播消息"""
        import json

        payload = json.dumps(data) if isinstance(data, dict) else data
        dead = []
        for client_id, dc in list(self._data_channels.items()):
            try:
                if dc and dc.readyState == "open":
                    dc.send(payload)
                else:
                    dead.append(client_id)
            except Exception:
                dead.append(client_id)
        for cid in dead:
            self._data_channels.pop(cid, None)

    def get_connection_count(self) -> int:
        return len(self._connections)

    async def stop(self):
        """停止 WebRTC 服务，清理所有连接"""
        self.logger.info("Stopping WebRTC service...")
        for client_id in list(self._connections.keys()):
            await self._cleanup_connection(client_id)
        self.logger.info("WebRTC service stopped")
