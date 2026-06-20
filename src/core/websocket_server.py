"""
WebSocket 信令服务器
用于: 设备发现握手 + WebRTC 信令交换 (SDP/ICE) + 业务消息传输 + 摄像头视频流
当 WebRTC DataChannel 不可用时，所有业务消息通过 WebSocket 传输
"""

import asyncio
import json
import uuid

import websockets

# 兼容 websockets 9.x 和 11+
try:
    from websockets.asyncio.server import ServerConnection
except ImportError:
    # websockets 9.x 兼容
    from websockets import WebSocketServerProtocol as ServerConnection


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
        config: dict = None,
        logger=None,
    ):
        self.host = host
        self.port = port
        self.message_handler = message_handler
        self.robot_info = robot_info
        self.webrtc_service = webrtc_service
        self.gimbal_controller = gimbal_controller
        self.config = config or {}
        self.logger = logger
        self._server = None
        self._clients: set[ServerConnection] = set()
        self._ws_clients: dict[str, ServerConnection] = {}  # client_id -> WebSocket 连接
        self._ws_broadcast_task: asyncio.Task = None

    async def start(self):
        """启动 WebSocket 信令服务器"""
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=10,
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

    async def _handle_client(self, websocket: ServerConnection, path=None):
        """处理客户端连接"""
        remote = websocket.remote_address
        client_id = str(uuid.uuid4())[:8]

        # ---- 安全认证 ----
        auth_enabled = self.config.get("security", {}).get("auth_enabled", False)

        if auth_enabled:
            # 检查 URL query 参数中的 token
            token_from_url = None
            if hasattr(websocket, "request") and hasattr(websocket.request, "query"):
                try:
                    from urllib.parse import parse_qs

                    qs = parse_qs(websocket.request.query)
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

        self._clients.add(websocket)
        self._ws_clients[client_id] = websocket
        if self.logger:
            self.logger.info(
                f"Signaling client connected: {remote} ({client_id}){', authenticated' if auth_enabled else ''}"
            )

        # 构建 features 列表
        features = ["websocket", "exec", "motion", "system", "camera"]
        if self.webrtc_service:
            features.append("webrtc")
        else:
            features.append("websocket-fallback")  # 标记 WebSocket 可处理业务消息
        if getattr(self, "gimbal_controller", None):
            features.append("gimbal")

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
                self.logger.info(f"[{client_id}] Received ICE candidate from client: {cand_text[:80]}")
                await self.webrtc_service.add_ice_candidate(
                    client_id,
                    candidate=cand_text,
                    sdp_mid=msg_data.get("sdpMid", ""),
                    sdp_mline_index=msg_data.get("sdpMLineIndex", 0),
                )
                # Workaround: 客户端 host candidate 使用 mDNS .local 地址（如 xxxx.local），
                # Jetson 端无法解析这些主机名导致 ICE 连通失败。
                # 用 WebSocket 连接的真实 IP 构造额外 host candidate
                if ".local" in cand_text and remote:
                    client_ip = remote[0] if isinstance(remote, tuple) else str(remote)
                    sdp_mid = msg_data.get("sdpMid", "") or "0"
                    sdp_mline = msg_data.get("sdpMLineIndex", 0)
                    # 从原 candidate 中提取 port, protocol, priority
                    parts = cand_text.strip().split()
                    if len(parts) >= 8 and parts[0].startswith("candidate:"):
                        try:
                            port = parts[5]
                            protocol = parts[2]
                            # 构造新 foundation（避免与原 candidate 冲突）
                            new_foundation = f"h{parts[0].split(':', 1)[1]}"
                            new_cand = f"candidate:{new_foundation} 1 {protocol} 2122252543 {client_ip} {port} typ host"
                            self.logger.info(f"[{client_id}] Adding extra host candidate with real IP: {new_cand[:80]}")
                            await self.webrtc_service.add_ice_candidate(
                                client_id,
                                candidate=new_cand,
                                sdp_mid=sdp_mid,
                                sdp_mline_index=sdp_mline,
                            )
                        except Exception as e:
                            self.logger.warning(f"[{client_id}] Failed to add extra host candidate: {e}")
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

    async def _start_ws_status_broadcast(self, interval: float = 1.0):
        """启动 WebSocket 状态广播（向所有已连接客户端广播系统状态）"""
        if self._ws_broadcast_task and not self._ws_broadcast_task.done():
            return  # 已经在运行

        async def _broadcast():
            while self._ws_clients:
                await asyncio.sleep(interval)
                try:
                    status = await self.message_handler._handle_get_status({})
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
