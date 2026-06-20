"""
WebRTC 服务管理
- 信令协商（SDP offer/answer）
- ICE candidate 中继（绕过 aiortc on_icecandidate 不触发的 bug）
- DataChannel 双工通信
- 摄像头视频流推送（CameraVideoTrack）
- 连接生命周期管理
"""

import asyncio
import logging

import cv2
import numpy as np
from av import VideoFrame
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc import RTCDataChannel, VideoStreamTrack, RTCIceCandidate

STUN_SERVER = "stun:stun.l.google.com:19302"

# ===== Monkey-patch: 修复 aioice 0.6.18 + Python 3.7 ICE 连通性问题 =====
# 在 Python 3.7 上，aioice 的 STUN 连通性检查 transport 会变成 NoneType，
# 导致 ICE 永远停留在 "new" 状态。这里用 set_selected_pair 绕过检查。
_PATCH_LOG = logging.getLogger("wobot")
_PENDING_CANDIDATES: list = []  # 模块级：存放待发送的 ICE candidate 信息
_PENDING_CALLBACKS: dict = {}   # 模块级：client_id -> send_callback，供 monkey-patch 发送候选


def _build_candidate_str(lc) -> str:
    """从 aioice LocalCandidate 构建 SDP candidate 字符串"""
    return (
        f"candidate:{lc.foundation} {lc.component} "
        f"udp {lc.priority} {lc.host} {lc.port} typ {getattr(lc, 'type', 'host')}"
    )


def _schedule_flush_candidates(component: int):
    """调度发送所有已收集的 ICE candidate（从任意 client_id 发送）"""
    loop = asyncio.get_event_loop()
    loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_flush_all_candidates()))


try:
    import aioice
    from aioice.ice import Connection as AioiceConnection
    _aioice_connect_original = AioiceConnection.connect

    async def _aioice_connect_patched(self):
        if not self._local_candidates_end:
            raise ConnectionError("Local candidates gathering was not performed")
        if self.remote_username is None or self.remote_password is None:
            raise ConnectionError("Remote username or password is missing")

        # 等待远端 candidate（通过 WebSocket 信令异步到达）
        for _ in range(100):
            if self._remote_candidates:
                break
            await asyncio.sleep(0.1)

        if not self._remote_candidates:
            raise ConnectionError("No remote candidates received")

        # 找到第一个兼容的 candidate pair 并强制选中
        for remote_cand in self._remote_candidates:
            for protocol in self._protocols:
                if protocol.local_candidate.can_pair_with(remote_cand):
                    self.set_selected_pair(
                        component=protocol.local_candidate.component,
                        local_foundation=protocol.local_candidate.foundation,
                        remote_foundation=remote_cand.foundation,
                    )
                    # 收集服务端 ICE candidate，以便信令发送给客户端
                    lc = protocol.local_candidate
                    cand = _build_candidate_str(lc)
                    _PENDING_CANDIDATES.append(cand)
                    _PATCH_LOG.info(
                        "ICE forced: local=%s:%d remote=%s:%d component=%d",
                        lc.host, lc.port,
                        remote_cand.host, remote_cand.port,
                        lc.component,
                    )
                    # 通过模块级回调立即发送（不依赖 on_icecandidate 事件）
                    _schedule_flush_candidates(lc.component)
                    return  # 成功返回 → start() 会将 ICE state 设为 "completed"

        raise ConnectionError("No compatible candidate pair found")

    AioiceConnection.connect = _aioice_connect_patched  # type: ignore[method-assign]
    _PATCH_LOG.info("aioice Connection.connect() PATCHED for Python 3.7 ICE fix")
except Exception as _patch_err:
    _PATCH_LOG.warning("aioice monkey-patch FAILED: %s", _patch_err)


class CameraVideoTrack(VideoStreamTrack):
    """从 CameraManager 读取帧并转换为 WebRTC 视频轨"""

    kind = "video"

    def __init__(self, camera_manager, camera_id=0, fps=30, logger=None):
        super().__init__()
        self.camera_manager = camera_manager
        self.camera_id = camera_id
        self.fps = fps
        self.logger = logger
        self._timestamp = 0
        self._frame_count = 0

    async def recv(self):
        """aiortc 每帧调用一次，返回 av.VideoFrame"""
        pts, time_base = await self.next_timestamp()

        frame = self.camera_manager.get_frame(self.camera_id)
        if frame is None:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            self._frame_count += 1
            if self._frame_count == 1 and self.logger:
                self.logger.warning(f"CameraVideoTrack(cam={self.camera_id}): no frame from camera, using black")
        else:
            frame = frame.copy()
            self._frame_count += 1
            if self._frame_count == 1 and self.logger:
                self.logger.info(
                    f"CameraVideoTrack(cam={self.camera_id}): first frame "
                    f"shape={frame.shape}, mean=({frame.mean():.0f})"
                )
            elif self._frame_count % 30 == 0 and self.logger:
                self.logger.info(
                    f"CameraVideoTrack(cam={self.camera_id}): frame #{self._frame_count} shape={frame.shape}, ok"
                )

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

    async def _send_candidates_to_client(self, client_id: str, send_callback):
        """将模块级 _PENDING_CANDIDATES 通过信令发送给客户端"""
        global _PENDING_CANDIDATES
        pending = _PENDING_CANDIDATES[:]
        _PENDING_CANDIDATES = []
        for cand_str in pending:
            payload = {
                "type": "webrtc_ice_candidate",
                "data": {
                    "candidate": cand_str,
                    "sdpMid": "0",
                    "sdpMLineIndex": 0,
                },
            }
            try:
                await send_callback(payload)
                self.logger.info(f"[{client_id}] Sent host ICE candidate: {cand_str[:80]}")
            except Exception as e:
                self.logger.error(f"[{client_id}] Failed to send ICE candidate: {e}")

    # ---- 模块级 ICE 刷新函数 ----


async def _flush_all_candidates():
    """模块级：将 _PENDING_CANDIDATES 发送给所有注册的客户端"""
    global _PENDING_CANDIDATES
    pending = _PENDING_CANDIDATES[:]
    _PENDING_CANDIDATES = []
    for client_id, send_callback in list(_PENDING_CALLBACKS.items()):
        for cand_str in pending:
            payload = {
                "type": "webrtc_ice_candidate",
                "data": {
                    "candidate": cand_str,
                    "sdpMid": "0",
                    "sdpMLineIndex": 0,
                },
            }
            try:
                await send_callback(payload)
                _PATCH_LOG.info(f"[{client_id}] Sent host ICE candidate: {cand_str[:80]}")
            except Exception as e:
                _PATCH_LOG.error(f"[{client_id}] Failed to send ICE candidate: {e}")


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

    async def _send_candidates_to_client(self, client_id: str, send_callback):
        """将模块级 _PENDING_CANDIDATES 通过信令发送给客户端"""
        global _PENDING_CANDIDATES
        pending = _PENDING_CANDIDATES[:]
        _PENDING_CANDIDATES = []
        for cand_str in pending:
            payload = {
                "type": "webrtc_ice_candidate",
                "data": {
                    "candidate": cand_str,
                    "sdpMid": "",
                    "sdpMLineIndex": 0,
                },
            }
            try:
                await send_callback(payload)
                self.logger.info(f"[{client_id}] Sent host ICE candidate: {cand_str[:80]}")
            except Exception as e:
                self.logger.error(f"[{client_id}] Failed to send ICE candidate: {e}")

    async def create_peer_connection(self, client_id: str, sdp_offer: str, send_callback=None) -> str:
        """处理客户端的 SDP offer，返回 SDP answer"""
        # ===== 重置全局状态：清除上一次连接的残留 =====
        global _PENDING_CANDIDATES
        # 清除所有已失效的回调（只保留即将注册的 client_id）
        stale = [cid for cid in _PENDING_CALLBACKS if cid != client_id]
        for cid in stale:
            _PENDING_CALLBACKS.pop(cid, None)
            self.logger.info(f"[{client_id}] Cleared stale callback: {cid}")
        _PENDING_CANDIDATES = []

        # 注册回调必须在 setRemoteDescription 之前，因为 ICE monkey-patch
        # 在 setRemoteDescription 期间就会触发，否则 _flush_all_candidates 找不到客户端
        if send_callback:
            _PENDING_CALLBACKS[client_id] = send_callback
            self.logger.info(f"[{client_id}] Callback registered for ICE candidates")
        # 使用 STUN 辅助 ICE 连通（局域网仍以 host candidates 为主）
        pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])])
        )

        # Cleanup old connection if client reconnects
        old_pc = self._connections.get(client_id)
        if old_pc:
            self.logger.info(f"[{client_id}] Old connection found, cleaning up before reconnect")
            await self._cleanup_connection(client_id)

        self._connections[client_id] = pc

        @pc.on("icecandidate")
        async def on_icecandidate(candidate):
            """将服务端 ICE candidate 发回客户端"""
            if candidate and send_callback:
                try:
                    cand_str = candidate.candidate if hasattr(candidate, "candidate") else str(candidate)
                    if cand_str:
                        payload = {
                            "type": "webrtc_ice_candidate",
                            "data": {
                                "candidate": cand_str,
                                "sdpMid": getattr(candidate, "sdpMid", None) or "",
                                "sdpMLineIndex": getattr(candidate, "sdpMLineIndex", None) or 0,
                            },
                        }
                        await send_callback(payload)
                        self.logger.info(f"[{client_id}] Sent ICE candidate: {cand_str[:80]}")
                except Exception as e:
                    self.logger.error(f"[{client_id}] Failed to send ICE candidate: {e}")

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

        # Process offer
        offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
        await pc.setRemoteDescription(offer)
        self.logger.info(f"[{client_id}] setRemoteDescription done")

        # 添加视频轨（为每个实际检测到的摄像头创建一个）
        if self.camera_manager:
            for cam_id in sorted(self.camera_manager.cameras.keys()):
                try:
                    await self.camera_manager.start_stream(cam_id)
                except Exception as e:
                    self.logger.warning(f"[{client_id}] Camera {cam_id} start failed: {e}")

            self._video_tracks[client_id] = {}
            for cam_id in sorted(self.camera_manager.cameras.keys()):
                try:
                    video_track = CameraVideoTrack(self.camera_manager, camera_id=cam_id, fps=10, logger=self.logger)
                    pc.addTrack(video_track)
                    self._video_tracks[client_id][cam_id] = video_track
                    self.logger.info(f"[{client_id}] Video track added (camera {cam_id})")
                except Exception as e:
                    self.logger.warning(f"[{client_id}] Failed to add video track camera {cam_id}: {e}")

        # aiortc 0.9.10 workaround: 客户端 offer 不含 video 时，手动填充默认编解码器 / MID / 载荷类型
        try:
            import copy as _copy
            from aiortc import rtp as _rtp
            from aiortc.codecs import MEDIA_CODECS
            from aiortc.rtcpeerconnection import HEADER_EXTENSIONS

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
            self.logger.warning(f"[{client_id}] aiortc workaround error (non-fatal): {e}")

        # Create answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # ===== ICE 候选提取 & SDP 注入 =====
        # aiortc 的 on_icecandidate 从不触发（Python 3.7 + aioice 兼容性 bug）
        # 方案：aiortc setLocalDescription 已写入候选到 SDP，但地址可能是 .local mDNS
        # 直接读取 SDP 中的 a=candidate 行并替换 .local → 真实 IP
        sdp = pc.localDescription.sdp

        # 记录原始候选用于诊断
        orig_cands = [l for l in sdp.split("\r\n") if l.startswith("a=candidate")]
        self.logger.info(f"[{client_id}] Original SDP candidates ({len(orig_cands)}): {[c[:80] for c in orig_cands]}")

        # 替换 .local 为真实 IP
        server_ip = "192.168.1.47"
        new_lines = []
        replaced_count = 0
        ice_lite_inserted = False
        for line in sdp.split("\r\n"):
            if line.startswith("a=candidate") and ".local" in line:
                parts = line.split()
                if len(parts) >= 6:
                    old_ip = parts[4]
                    parts[4] = server_ip
                    replaced = " ".join(parts)
                    self.logger.info(f"[{client_id}] SDP ice .local→IP: {old_ip}→{server_ip}")
                    line = replaced
                    replaced_count += 1
            # 在第一个 m= 行之前插入 ice-lite（服务端不执行主动检查，由浏览器单侧验证）
            if not ice_lite_inserted and line.startswith("m="):
                new_lines.append("a=ice-lite")
                ice_lite_inserted = True
            new_lines.append(line)
        sdp = "\r\n".join(new_lines)

        injected_count = replaced_count
        self.logger.info(
            f"[{client_id}] ICE SDP: replaced .local→IP count={injected_count}, "
            f"SDP has candidate={'a=candidate' in sdp}"
        )

        # 诊断：记录 answer SDP 是否包含 DataChannel
        has_app = "m=application" in (sdp)
        self.logger.info(f"[{client_id}] Answer SDP has DataChannel: {has_app}")

        # SCTP 协商完成后创建服务端 DataChannel
        if has_app:
            try:
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
            except Exception as e:
                self.logger.warning(f"[{client_id}] Server DC creation failed: {e}")

        self.logger.info(f"WebRTC peer connection created for {client_id}")

        return sdp

    async def _deferred_send_candidates(self, client_id: str, send_callback):
        """（已废弃：改为 monkey-patch 直接触发 _flush_all_candidates）"""
        pass

    async def add_ice_candidate(self, client_id: str, candidate: str, sdp_mid: str, sdp_mline_index: int):
        pc = self._connections.get(client_id)
        if not pc:
            self.logger.warning(f"[{client_id}] No peer connection, dropping ICE candidate")
            return
        try:
            # 从 candidate SDP 字符串解析字段
            # 格式: "candidate:<foundation> <component> <protocol> <priority> <ip> <port> typ <type> [其他...]"
            parts = candidate.strip().split()
            if len(parts) >= 8 and parts[0].startswith("candidate:"):
                foundation = parts[0].split(":", 1)[1]
                component = int(parts[1])
                protocol = parts[2].lower()
                priority = int(parts[3])
                ip = parts[4]
                port = int(parts[5])
                cand_type = parts[7] if len(parts) >= 8 else "host"
                ice = RTCIceCandidate(
                    component=component,
                    foundation=foundation,
                    ip=ip,
                    port=port,
                    priority=priority,
                    protocol=protocol,
                    type=cand_type,
                    sdpMid=sdp_mid if sdp_mid else None,
                    sdpMLineIndex=sdp_mline_index if sdp_mline_index is not None else 0,
                )
                pc.addIceCandidate(ice)
                self.logger.info(
                    f"[{client_id}] ICE candidate added: {cand_type} {ip}:{port} sdpMid={sdp_mid} mline={sdp_mline_index}"
                )
            else:
                self.logger.warning(f"[{client_id}] Invalid candidate format: {candidate[:80]}")
        except Exception as e:
            self.logger.error(f"[{client_id}] Failed to add ICE candidate: {e}")

    async def _on_dc_message(self, client_id: str, msg):
        """处理 DataChannel 消息"""
        import json
        try:
            data = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
        except Exception:
            data = {"type": "raw", "data": str(msg)}

        if self.message_handler:
            await self.message_handler(client_id, data)

    def _on_dc_close(self, client_id: str):
        self.logger.info(f"[{client_id}] DataChannel closed")
        self._data_channels.pop(client_id, None)

    async def _cleanup_connection(self, client_id: str):
        """清理连接（WebSocket 断开或 ICE 失败时调用）"""
        _PENDING_CALLBACKS.pop(client_id, None)
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
        # 同时清除模块级候选队列，防止残留数据影响下次连接
        global _PENDING_CANDIDATES
        _PENDING_CANDIDATES = []
        self.logger.info(f"[{client_id}] Cleaned up connection ({len(tracks)} video tracks, pending_candidates cleared)")

    async def send_message(self, client_id: str, data):
        """通过 DataChannel 发送消息"""
        dc = self._data_channels.get(client_id)
        if dc and dc.readyState == "open":
            import json
            dc.send(json.dumps(data) if isinstance(data, dict) else data)

    def get_connection_count(self) -> int:
        return len(self._connections)
