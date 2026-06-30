"""
WebSocket 信令服务器
用于: 设备发现握手 + WebRTC 信令交换 (SDP/ICE) + 业务消息传输 + 摄像头视频流
当 WebRTC DataChannel 不可用时，所有业务消息通过 WebSocket 传输
"""

from __future__ import annotations

import asyncio
import json
import struct
import uuid
from typing import Any

import websockets

from core.service_manager import SERVICE_DEFINITIONS

# 协议版本：服务端与客户端协商的通信协议版本号
# 仅递增，不做后向兼容的大版本变更
PROTOCOL_VERSION = 1

# 兼容 websockets 9.x 和 11+
try:
    from websockets.asyncio.server import ServerConnection
except ImportError:
    # websockets 9.x 兼容
    from websockets import WebSocketServerProtocol as ServerConnection  # type: ignore[no-redef]


class WebSocketServer:
    """WebSocket 信令服务器"""

    def __init__(
        self,
        host: str,
        port: int,
        message_handler,
        robot_info: dict,
        webrtc_service=None,
        gimbal_controller=None,
        service_manager=None,
        config: dict | None = None,
        logger=None,
    ):
        self.host = host
        self.port = port
        self.message_handler = message_handler
        self.robot_info = robot_info
        self.webrtc_service = webrtc_service
        self.gimbal_controller = gimbal_controller
        self.service_manager = service_manager
        self.config = config or {}
        self.logger = logger
        self._server: websockets.Server | None = None
        self._clients: set[ServerConnection] = set()
        self._ws_clients: dict[str, ServerConnection] = {}  # client_id -> WebSocket 连接
        self._client_real_ips: dict[str, str] = {}  # client_id -> 真实客户端 IP（来自代理传递的 client_ip 参数）
        self._ws_broadcast_task: asyncio.Task[Any] | None = None

    async def start(self):
        """启动 WebSocket 信令服务器"""
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=5,
            ping_timeout=5,
            max_size=2**20,
            origins=None,  # 允许所有 Origin（浏览器跨域 WebSocket 需要）
        )
        if self.logger:
            self.logger.info(f"WebSocket signaling server started on ws://{self.host}:{self.port}")

    async def serve_forever(self):
        if self._server:
            await self._server.wait_closed()

    async def stop(self):
        if self._ws_broadcast_task and not self._ws_broadcast_task.done():
            self._ws_broadcast_task.cancel()
            try:
                await self._ws_broadcast_task
            except asyncio.CancelledError:
                pass
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self.logger:
            self.logger.info("WebSocket signaling server stopped")

    async def broadcast_message(self, message: dict) -> None:
        """向所有已连接客户端广播消息"""
        dead = []
        payload = json.dumps(message)
        for cid, ws in list(self._ws_clients.items()):
            try:
                await ws.send(payload)
            except Exception:
                dead.append(cid)
        for cid in dead:
            self._ws_clients.pop(cid, None)
            self._clients.discard(self._ws_clients.get(cid))

    async def _handle_client(self, websocket: ServerConnection, path=None):
        """处理客户端连接"""
        remote = websocket.remote_address
        client_id = str(uuid.uuid4())[:8]

        # ---- 安全认证 ----
        auth_enabled = self.config.get("security", {}).get("auth_enabled", False)

        if auth_enabled:
            # 检查 URL query 参数中的 token
            token_from_url = None
            if hasattr(websocket, "request") and hasattr(websocket.request, "query"):  # type: ignore[union-attr]
                try:
                    from urllib.parse import parse_qs

                    qs = parse_qs(websocket.request.query)  # type: ignore[union-attr]
                    token_from_url = qs.get("token", [None])[0]
                except Exception:
                    pass

            expected_token = self.config.get("security", {}).get("token", "")
            if not expected_token:
                if self.logger:
                    self.logger.warning(f"Auth enabled but no token configured, rejecting {remote}")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "data": {"code": 403, "message": "Server misconfigured: no token set"},
                        }
                    )
                )
                await websocket.close()
                return

            authenticated = False

            # 方式1: URL query token
            if token_from_url and token_from_url == expected_token:
                authenticated = True

            # 方式2: 首条消息为 auth
            if not authenticated:
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=10)
                    msg = json.loads(raw)
                    if msg.get("type") == "auth" and msg.get("data", {}).get("token") == expected_token:
                        authenticated = True
                        # auth 成功，继续正常流程
                    else:
                        if self.logger:
                            self.logger.warning(f"Auth failed for {remote}")
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "data": {"code": 401, "message": "Authentication failed"},
                                }
                            )
                        )
                        await websocket.close()
                        return
                except asyncio.TimeoutError:
                    if self.logger:
                        self.logger.warning(f"Auth timeout for {remote}")
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "error",
                                "data": {"code": 401, "message": "Authentication timeout"},
                            }
                        )
                    )
                    await websocket.close()
                    return
                except Exception:
                    await websocket.send(
                        json.dumps(
                            {
                                "type": "error",
                                "data": {"code": 400, "message": "Bad auth request"},
                            }
                        )
                    )
                    await websocket.close()
                    return

        # ---- 版本兼容性检查 ----
        # 安全要求: 不透露服务端协议版本，不告知客户端是因版本问题被拒
        # 通过 WebSocket URL query 参数 ?protocol_version=N 传递（连接时即确定，无竞态）
        compat_config = self.config.get("compatibility", {})
        min_protocol = compat_config.get("min_protocol_version", 1)
        reject_newer = compat_config.get("reject_newer", False)

        # 调试模式：可强制提升最低版本用于测试
        debug_config = self.config.get("debug", {})
        debug_force_min = debug_config.get("force_min_protocol_version", 0)
        if debug_force_min > 0:
            min_protocol = debug_force_min

        # 从 URL query 参数读取 client_protocol
        # 兼容新旧 websockets API: 新版有 request.query，旧版有 path
        client_protocol = None
        try:
            from urllib.parse import parse_qs

            query_string = ""
            if hasattr(websocket, "request") and hasattr(websocket.request, "query"):  # type: ignore[union-attr]
                query_string = websocket.request.query or ""  # type: ignore[union-attr]
            elif hasattr(websocket, "path"):
                # websockets 9.x / legacy: path 包含 query string
                raw_path = websocket.path or ""
                if "?" in raw_path:
                    query_string = raw_path.split("?", 1)[1]

            if query_string:
                qs = parse_qs(query_string)
                pv_str = qs.get("protocol_version", [None])[0]
                if pv_str is not None:
                    client_protocol = int(pv_str)
                # 提取代理传递的真实客户端 IP（用于修复 mDNS .local 解析）
                client_ip_from_proxy = qs.get("client_ip", [None])[0]
                if client_ip_from_proxy:
                    self._client_real_ips[client_id] = client_ip_from_proxy
                    if self.logger:
                        self.logger.info(f"[{client_id}] Client real IP from proxy: {client_ip_from_proxy}")
        except Exception:
            pass

        if client_protocol is None:
            if self.logger:
                self.logger.info(f"Client {remote} rejected: no protocol_version in URL")
            await websocket.close(4001, "Connection refused")
            return

        if client_protocol < min_protocol:
            if self.logger:
                self.logger.info(f"Client {remote} rejected: protocol version {client_protocol} < min {min_protocol}")
            await websocket.close(4001, "Connection refused")
            return

        if reject_newer and client_protocol > PROTOCOL_VERSION:
            if self.logger:
                self.logger.info(
                    f"Client {remote} rejected: protocol version {client_protocol} > server {PROTOCOL_VERSION}"
                )
            await websocket.close(4001, "Connection refused")
            return

        self._clients.add(websocket)
        self._ws_clients[client_id] = websocket
        if self.logger:
            self.logger.info(
                f"Signaling client connected: {remote} ({client_id})"
                f"{', authenticated' if auth_enabled else ''}"
                f", protocol={client_protocol}"
            )

        # 构建 features 列表
        features = ["websocket", "exec", "motion", "system", "camera"]
        if self.webrtc_service:
            features.append("webrtc")
        else:
            features.append("websocket-fallback")  # 标记 WebSocket 可处理业务消息
        if getattr(self, "gimbal_controller", None):
            features.append("gimbal")
        # 检查舞蹈控制器
        dance_ctrl = getattr(self.message_handler, "dance_controller", None)
        if dance_ctrl:
            features.append("dance")
        # 检查音乐播放服务是否配置
        if "music_player" in SERVICE_DEFINITIONS:
            features.append("music")
        # 检查喊话服务（检查 message_handler 上的控制器）
        voice_ctrl = getattr(self.message_handler, "voice_broadcast_controller", None)
        if voice_ctrl:
            features.append("voice_broadcast")

        try:
            # 发送握手消息（设备发现兼容）
            await websocket.send(
                json.dumps(
                    {
                        "type": "connected",
                        "data": {**self.robot_info, "features": features},
                    }
                )
            )

            async for raw in websocket:
                if isinstance(raw, bytes):
                    # 二进制消息：语音喊话音频数据
                    await self._handle_binary_message(websocket, client_id, remote, raw)
                else:
                    try:
                        msg = json.loads(raw)
                        await self._process_message(websocket, client_id, remote, msg)
                    except json.JSONDecodeError:
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "error",
                                    "data": {"code": 400, "message": "Invalid JSON"},
                                }
                            )
                        )

        except (websockets.exceptions.ConnectionClosed, websockets.ConnectionClosed):
            pass
        finally:
            self._clients.discard(websocket)
            self._ws_clients.pop(client_id, None)
            self._client_real_ips.pop(client_id, None)
            if self.webrtc_service:
                await self.webrtc_service._cleanup_connection(client_id)
            if self.logger:
                self.logger.info(f"Signaling client disconnected: {remote}")

    async def _process_message(self, websocket: ServerConnection, client_id: str, remote: tuple, msg: dict):
        """处理信令消息"""
        msg_type = msg.get("type", "")
        msg_data = msg.get("data", {})

        # ---- WebRTC 信令 ----
        if msg_type == "webrtc_offer":
            if not self.webrtc_service:
                await websocket.send(
                    json.dumps({"type": "error", "data": {"code": 503, "message": "WebRTC not available"}})
                )
                return
            sdp = msg_data.get("sdp", "")
            try:
                # 创建发送回调，让 WebRTCService 能将 ICE candidates 发回客户端
                async def send_to_client(payload: dict):
                    await websocket.send(json.dumps(payload))

                answer_sdp = await self.webrtc_service.create_peer_connection(
                    client_id, sdp, send_callback=send_to_client
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "webrtc_answer",
                            "data": {"sdp": answer_sdp},
                        }
                    )
                )
                # 注意：不发送额外的 ICE candidate。猴子补丁仅用于强制服务端 pair，
                # SDP Answer 中的 a=candidate 行已包含浏览端需要的所有候选地址。
                # 从 _PENDING_CANDIDATES 提取的 candidate 端口与 set_selected_pair
                # 实际选中的 protocol 端口不一致，会导致 DTLS 连通失败。
            except Exception as e:
                import traceback

                if self.logger:
                    self.logger.error(f"WebRTC offer 处理失败: {e}\n{traceback.format_exc()}")
                await websocket.send(
                    json.dumps(
                        {
                            "type": "error",
                            "data": {"code": 500, "message": f"WebRTC negotiation failed: {e}"},
                        }
                    )
                )
            return

        if msg_type == "webrtc_ice_candidate":
            if self.webrtc_service:
                cand_text = msg_data.get("candidate", "")
                sdp_mid = msg_data.get("sdpMid", "")
                sdp_mline = msg_data.get("sdpMLineIndex", 0)
                self.logger.info(f"[{client_id}] Received ICE candidate from client: {cand_text[:80]}")

                client_real_ip = self._client_real_ips.get(client_id, "")

                # mDNS .local 双候选策略：
                # Jetson mDNS 解析 .local 始终返回 192.168.1.53（Mac 代理 IP），
                # 不能直接替换。改为保留原始 .local 候选，同时追加一个真实 IP 候选。
                # 真实 IP 候选用更高优先级（2122252543），且先添加到 _remote_candidates，
                # 确保猴子补丁优先尝试真实 IP -> 浏览器。
                if ".local" in cand_text and client_real_ip:
                    parts = cand_text.strip().split()
                    if len(parts) >= 6 and ".local" in parts[4]:
                        # 用更高优先级构造真实 IP candidate（放在 .local 之前尝试）
                        new_foundation = "h" + parts[0].replace("candidate:", "")[:9]
                        ip_cand = (
                            f"candidate:{new_foundation} {parts[1]} {parts[2]} "
                            f"2122252543 {client_real_ip} {parts[5]} typ host"
                        )
                        self.logger.info(
                            f"[{client_id}] Adding host candidate with real IP first: {client_real_ip}:{parts[5]}"
                        )
                        # 先添加真实 IP 候选（猴子补丁按添加顺序尝试，优先命中）
                        await self.webrtc_service.add_ice_candidate(
                            client_id,
                            candidate=ip_cand,
                            sdp_mid=sdp_mid,
                            sdp_mline_index=sdp_mline,
                        )
                        # 再添加原始 .local 候选作为兜底
                        await self.webrtc_service.add_ice_candidate(
                            client_id,
                            candidate=cand_text,
                            sdp_mid=sdp_mid,
                            sdp_mline_index=sdp_mline,
                        )
                        return

                # 非 .local 候选（srflx/relay）或没有真实 IP，直接添加
                await self.webrtc_service.add_ice_candidate(
                    client_id,
                    candidate=cand_text,
                    sdp_mid=sdp_mid,
                    sdp_mline_index=sdp_mline,
                )
            return

        # ---- subscribe：启动 WebSocket 状态广播 ----
        if msg_type == "subscribe":
            self.logger.info(f"[{client_id}] Client subscribed via WebSocket, starting status broadcast")
            await self._start_ws_status_broadcast(self.config.get("status", {}).get("update_interval", 1.0))
            return

        if msg_type == "unsubscribe":
            return

        # ---- ping 兼容 ----
        if msg_type == "ping":
            await websocket.send(json.dumps({"type": "pong", "data": {"ts": msg_data.get("ts", 0)}}))
            return

        # ---- 所有业务消息通过 WebSocket 处理 ----
        try:
            result = await self.message_handler.handle(msg_type, msg_data)
            if result and isinstance(result, dict):
                await websocket.send(json.dumps(result))
        except Exception as e:
            if self.logger:
                self.logger.error(f"Message handling error: {e}")
            await websocket.send(
                json.dumps(
                    {
                        "type": "error",
                        "data": {"code": 500, "message": str(e)},
                    }
                )
            )

    async def _handle_binary_message(self, websocket: ServerConnection, client_id: str, remote: tuple, raw: bytes):
        """处理二进制 WebSocket 消息（语音喊话音频）

        协议格式：
          [4 bytes: JSON 头长度 uint32 big-endian]
          [N bytes: JSON 头 {"type": "voice_broadcast", "data": {"mode": "record"}}]
          [剩余 bytes: 音频二进制数据]
        """
        try:
            if len(raw) < 4:
                await websocket.send(
                    json.dumps({"type": "error", "data": {"code": 400, "message": "Binary message too short"}})
                )
                return

            header_len = struct.unpack(">I", raw[:4])[0]
            if header_len < 2 or 4 + header_len > len(raw):
                await websocket.send(
                    json.dumps({"type": "error", "data": {"code": 400, "message": "Invalid binary header length"}})
                )
                return

            header_json = raw[4 : 4 + header_len].decode("utf-8")
            audio_data = raw[4 + header_len :]

            msg = json.loads(header_json)
            msg_type = msg.get("type", "")

            if msg_type != "voice_broadcast":
                await websocket.send(
                    json.dumps(
                        {"type": "error", "data": {"code": 400, "message": f"Unknown binary msg type: {msg_type}"}}
                    )
                )
                return

            msg_data = msg.get("data", {})
            msg_data["_audio_data"] = audio_data

            # 委托给 message_handler 处理
            result = await self.message_handler.handle(msg_type, msg_data)
            if result and isinstance(result, dict):
                await websocket.send(json.dumps(result))

        except Exception as e:
            if self.logger:
                self.logger.error(f"Binary message handling error: {e}")
            await websocket.send(json.dumps({"type": "error", "data": {"code": 500, "message": str(e)}}))

    async def _start_ws_status_broadcast(self, interval: float = 1.0):
        """启动 WebSocket 状态广播（向所有已连接客户端广播系统状态）"""
        if self._ws_broadcast_task and not self._ws_broadcast_task.done():
            return  # 已经在运行

        async def _broadcast():
            while self._ws_clients:
                await asyncio.sleep(interval)
                try:
                    status = await self.message_handler._handle_get_status({})
                    # 附加服务状态信息
                    if self.service_manager:
                        services_status = self.service_manager.get_all_services_status()
                        status["data"]["services"] = services_status
                    # 广播给所有已连接 WebSocket 客户端
                    dead = []
                    payload = json.dumps(status)
                    for cid, ws in list(self._ws_clients.items()):
                        try:
                            await ws.send(payload)
                        except Exception:
                            dead.append(cid)
                    for cid in dead:
                        self._ws_clients.pop(cid, None)
                        self._clients.discard(self._ws_clients.get(cid))
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"WS status broadcast error: {e}")

        self._ws_broadcast_task = asyncio.create_task(_broadcast())
        if self.logger:
            self.logger.info("WebSocket status broadcast started")
