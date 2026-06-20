"""
WebRTC 服务
通过 aiortc 提供 DataChannel（业务消息）+ 视频流
WebSocket 仅用于信令（SDP/ICE 交换）
"""

import asyncio
import json
import logging

import numpy as np

try:
    from aiortc import (
        RTCConfiguration,
        RTCDataChannel,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
        VideoStreamTrack,
    )
    from av import VideoFrame

    WEBRTC_AVAILABLE = True
except ImportError:
    WEBRTC_AVAILABLE = False
    raise

# Monkey-patch: aiortc 0.9.10 缺失的属性导致 createAnswer 失败
# 当客户端 offer 不含 video media 时，手动填充默认值
try:
    from aiortc.rtcrtptransceiver import RTCRtpTransceiver

    for attr in ["_codecs", "_headerExtensions", "_offerDirection"]:
        if not hasattr(RTCRtpTransceiver, attr):
            setattr(RTCRtpTransceiver, attr, None)
except Exception:
    pass

STUN_SERVER = "stun:stun.l.google.com:19302"

# ===== Monkey-patch: 修复 aioice 0.6.18 + Python 3.7 ICE 连通性问题 =====
# 在 Python 3.7 上，aioice 的 STUN 连通性检查 transport 会变成 NoneType，
# 导致 ICE 永远停留在 "new" 状态。这里用 set_selected_pair 绕过检查。
_PATCH_LOG = logging.getLogger("wobot")
try:
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
                    _PATCH_LOG.info(
                        "ICE forced: local=%s:%d remote=%s:%d component=%d",
                        protocol.local_candidate.host,
                        protocol.local_candidate.port,
                        remote_cand.host,
                        remote_cand.port,
                        protocol.local_candidate.component,
                    )
                    return  # 成功返回 → start() 会将 ICE state 设为 "completed"

        raise ConnectionError("No compatible candidate pair found")

    AioiceConnection.connect = _aioice_connect_patched
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
        import cv2

        pts, time_base = await self.next_timestamp()

        frame = self.camera_manager.get_frame(self.camera_id)
        if frame is None:
            # 无画面时返回黑帧
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

        # OpenCV BGR → RGB
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
        self.logger = logger
        self._connections: dict[str, RTCPeerConnection] = {}  # client_id -> PC
        self._data_channels: dict[str, RTCDataChannel] = {}  # client_id -> channel
        self._video_tracks: dict[str, dict[int, CameraVideoTrack]] = {}  # client_id -> {camera_id: track}
        self._status_task = None

    async def create_peer_connection(self, client_id: str, sdp_offer: str, send_callback=None) -> str:
        """处理客户端的 SDP offer，返回 SDP answer
        send_callback: async function(payload_dict) 用于发送信令消息给客户端
        """
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
                    # RTCIceCandidate 的 candidate 属性是 SDP 字符串
                    cand_str = candidate.candidate if hasattr(candidate, "candidate") else str(candidate)
                    # 过滤掉空 candidate（表示 ICE 收集完成）
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
        # 先确保所有摄像头流已启动（热恢复，O(1)）
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
        # H.264 优先：iPad/Safari 只支持 H.264，VP8 优先会导致平板黑屏
        try:
            import copy as _copy

            from aiortc import rtp as _rtp
            from aiortc.codecs import MEDIA_CODECS
            from aiortc.rtcpeerconnection import HEADER_EXTENSIONS

            dynamic_pt = _rtp.DYNAMIC_PAYLOAD_TYPES.start
            for t in pc._RTCPeerConnection__transceivers:
                # 1) 分配 MID（createAnswer 不会自动分配，但 BUNDLE 需要）
                # 使用 __nextAvailableMid() 避免与 SCTP 数据通道的 MID 重复
                if t.mid is None:
                    t.mid = pc._RTCPeerConnection__nextAvailableMid()
                # 2) 深拷贝编解码器并分配动态载荷类型（MEDIA_CODECS 的 payloadType 为 None）
                # H.264 排在 VP8 前面，优先匹配平板/Safari
                if not t._codecs:
                    codecs = []
                    raw_codecs = list(MEDIA_CODECS.get(t.kind, []))

                    # 排序: VP8 优先（通用兼容性最好），然后 H.264
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
                # 3) 填充默认 headerExtensions
                if not t._headerExtensions:
                    t._headerExtensions = list(HEADER_EXTENSIONS.get(t.kind, []))
                # 4) 设置方向
                if t._offerDirection is None:
                    t._offerDirection = "sendrecv"
        except Exception as e:
            self.logger.warning(f"[{client_id}] aiortc workaround error (non-fatal): {e}")

        # Create answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # 诊断：记录 answer SDP 是否包含 DataChannel
        has_app = "m=application" in (pc.localDescription.sdp or "")
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
        return pc.localDescription.sdp

    async def add_ice_candidate(self, client_id: str, candidate: str, sdp_mid: str, sdp_mline_index: int):
        pc = self._connections.get(client_id)
        if not pc:
            self.logger.warning(f"[{client_id}] No peer connection, dropping ICE candidate")
            return
        try:
            from aiortc import RTCIceCandidate

            # aiortc 0.9.10: RTCIceCandidate(component, foundation, ip, port, priority, protocol, type, ...)
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
                # parts[6] == "typ", parts[7] == type
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
                # aiortc 0.9.10 的 addIceCandidate 是同步方法，不是 async
                pc.addIceCandidate(ice)
                self.logger.info(
                    f"[{client_id}] ICE candidate added: {cand_type} {ip}:{port} sdpMid={sdp_mid} mline={sdp_mline_index}"
                )
            else:
                self.logger.warning(f"[{client_id}] Invalid candidate format: {candidate[:80]}")
        except Exception as e:
            self.logger.error(f"[{client_id}] Failed to add ICE candidate: {e}")

    async def _on_dc_message(self, client_id: str, message):
        """DataChannel 收到消息 → 转发给 MessageHandler"""
        try:
            msg = json.loads(message) if isinstance(message, str) else json.loads(message.decode())
            msg_type = msg.get("type", "")
            msg_data = msg.get("data", {})

            # subscribe/unsubscribe 在 DataChannel 层处理
            if msg_type == "subscribe":
                # 客户端订阅 → 启动状态广播
                if not self._status_task:
                    await self.start_status_broadcast(self.config.get("status", {}).get("update_interval", 1.0))
                return
            if msg_type == "unsubscribe":
                return

            response = await self.message_handler.handle(msg_type, msg_data)
            if response:
                await self._send_to_client(client_id, response)

            # camera start → 切换 WebRTC 视频源
            if msg_type == "camera" and msg_data.get("action") in ("start", "switch"):
                camera_id = msg_data.get("camera_id", 0)
                await self.switch_camera(client_id, camera_id)
        except Exception as e:
            self.logger.error(f"DataChannel message error from {client_id}: {e}")

    async def _send_to_client(self, client_id: str, msg: dict):
        """向指定客户端的 DataChannel 发送消息"""
        channel = self._data_channels.get(client_id)
        if channel and channel.readyState == "open":
            try:
                channel.send(json.dumps(msg))
            except Exception as e:
                self.logger.warning(f"Failed to send to {client_id}: {e}")

    async def _on_dc_close(self, client_id: str):
        """DataChannel 关闭"""
        self.logger.info(f"DataChannel closed for {client_id}")
        await self._cleanup_connection(client_id)

    async def _cleanup_connection(self, client_id: str):
        """清理连接"""
        pc = self._connections.pop(client_id, None)
        if pc:
            try:
                await pc.close()
            except Exception:
                pass
        self._data_channels.pop(client_id, None)
        tracks = self._video_tracks.pop(client_id, {})
        self.logger.info(f"[{client_id}] Cleaned up connection ({len(tracks)} video tracks)")

    async def switch_camera(self, client_id: str, camera_id: int):
        """切换视频轨道的摄像头源（如果存在对应 track 则切换其 camera_id）"""
        tracks = self._video_tracks.get(client_id, {})
        if camera_id in tracks:
            # 该 camera_id 已有独立 track，无需切换
            self.logger.info(f"[{client_id}] Camera {camera_id} already has dedicated track")
        elif tracks:
            # 如果请求的 camera_id 没有独立 track，使用第一个可用的 track 切换
            # （兼容只有单个 track 的旧逻辑）
            first_track = next(iter(tracks.values()))
            first_track.camera_id = camera_id
            self.logger.info(f"[{client_id}] Switched first available track to camera {camera_id}")

    async def send_to_all(self, msg: dict):
        """向所有已连接客户端广播消息"""
        dead = []
        for client_id, channel in self._data_channels.items():
            if channel.readyState == "open":
                try:
                    channel.send(json.dumps(msg))
                except Exception:
                    dead.append(client_id)
        for cid in dead:
            await self._cleanup_connection(cid)

    async def start_status_broadcast(self, interval: float = 1.0):
        """启动状态广播（通过 DataChannel）"""

        async def _broadcast():
            while True:
                try:
                    await asyncio.sleep(interval)
                    status = await self.message_handler._handle_get_status({})
                    await self.send_to_all(status)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    self.logger.error(f"Status broadcast error: {e}")

        self._status_task = asyncio.create_task(_broadcast())

    async def stop_status_broadcast(self):
        if self._status_task:
            self._status_task.cancel()
            try:
                await self._status_task
            except asyncio.CancelledError:
                pass

    async def stop(self):
        """停止所有连接"""
        await self.stop_status_broadcast()
        for client_id in list(self._connections.keys()):
            await self._cleanup_connection(client_id)
        self.logger.info("WebRTC service stopped")
