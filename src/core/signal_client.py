"""Signal client for wo-bot-control.

Connects to wo-bot-signal server via WebSocket, handles WebRTC signaling
for cross-network remote control.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import websockets


class SignalClient:
    """WebSocket client for wo-bot-signal server.

    Handles:
    - HMAC-SHA256 authentication (reuses ROBOT_SECRET from .binding_secret)
    - WebRTC signaling: call/answer/ice message relay
    - Auto-reconnect with exponential backoff (1s → 30s)
    """

    def __init__(
        self,
        server_url: str,
        robot_secret: str,
        device_id: str,
        webrtc_service=None,
        logger: Optional[logging.Logger] = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.robot_secret = robot_secret
        self.device_id = device_id
        self.webrtc_service = webrtc_service
        self.logger = logger or logging.getLogger(__name__)

        self._ws = None
        self._running = False
        self._reconnect_count = 0
        self._current_client_id = None  # Track the client being served

    @classmethod
    def from_config(
        cls,
        config: dict,
        webrtc_service=None,
        device_id: str = "",
        logger: Optional[logging.Logger] = None,
    ) -> "Optional[SignalClient]":
        """Create SignalClient from config dict."""
        signal_cfg = config.get("signal", {})
        if not signal_cfg.get("enabled", False):
            return None

        server_url = signal_cfg.get("server_url", "")
        if not server_url:
            if logger:
                logger.warning("[Signal] signal.server_url not configured")
            return None

        # Read ROBOT_SECRET (same as account_client)
        config_dir = Path(__file__).parent.parent.parent / "config"
        secret = signal_cfg.get("secret", "")
        if not secret:
            # Try binding.secret first
            secret = config.get("binding", {}).get("secret", "")
        if not secret:
            secret_file = config_dir / ".binding_secret"
            if secret_file.exists():
                secret = secret_file.read_text(encoding="utf-8").strip()

        if not secret:
            if logger:
                logger.error("[Signal] ROBOT_SECRET not found")
            return None

        # Get device_id from config or parameter
        if not device_id:
            device_id = config.get("robot", {}).get("id", "")
        if not device_id:
            if logger:
                logger.error("[Signal] device_id not configured")
            return None

        return cls(
            server_url=server_url,
            robot_secret=secret,
            device_id=device_id,
            webrtc_service=webrtc_service,
            logger=logger,
        )

    def _sign(self, timestamp: int) -> str:
        """Generate HMAC-SHA256 signature (same as account_client._sign)."""
        message = f"{self.device_id}:{timestamp}"
        return hmac.new(
            self.robot_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _build_connect_url(self) -> str:
        """Build WebSocket URL with auth params."""
        timestamp = int(time.time() * 1000)
        signature = self._sign(timestamp)
        params = urlencode({
            "role": "robot",
            "deviceId": self.device_id,
            "timestamp": str(timestamp),
            "signature": signature,
        })
        # server_url may be wss:// or ws://
        separator = "&" if "?" in self.server_url else "?"
        return f"{self.server_url}{separator}{params}"

    async def start(self):
        """Start the signal client with auto-reconnect."""
        self._running = True
        self.logger.info(f"[Signal] Starting, server={self.server_url}, device={self.device_id}")
        await self._connect_loop()

    async def stop(self):
        """Stop the signal client."""
        self._running = False
        if self._ws:
            await self._ws.close()
        self.logger.info("[Signal] Stopped")

    async def _connect_loop(self):
        """Connect with exponential backoff."""
        while self._running:
            try:
                url = self._build_connect_url()
                self.logger.info(f"[Signal] Connecting to {self.server_url}")

                async with websockets.connect(
                    url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._reconnect_count = 0
                    self.logger.info("[Signal] Connected to signal server")

                    # Send initial ping
                    await ws.send(json.dumps({"type": "ping"}))

                    # Message loop
                    async for raw_msg in ws:
                        await self._handle_message(raw_msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self.logger.warning(f"[Signal] Connection error: {e}")

            finally:
                self._ws = None
                self._current_client_id = None

            if not self._running:
                break

            # Exponential backoff: 1s → 2s → 4s → 8s → 16s → 30s
            delay = min(2 ** self._reconnect_count, 30)
            self._reconnect_count += 1
            self.logger.info(f"[Signal] Reconnecting in {delay}s (attempt {self._reconnect_count})")
            await asyncio.sleep(delay)

    async def _handle_message(self, raw_msg: str):
        """Handle incoming signal server message."""
        try:
            msg = json.loads(raw_msg)
        except json.JSONDecodeError:
            self.logger.warning(f"[Signal] Invalid JSON: {raw_msg[:100]}")
            return

        msg_type = msg.get("type", "")
        self.logger.debug(f"[Signal] Received: {msg_type}")

        if msg_type == "call":
            await self._handle_call(msg)
        elif msg_type == "ice":
            await self._handle_ice(msg)
        elif msg_type == "client-disconnect":
            await self._handle_client_disconnect(msg)
        elif msg_type == "kick":
            self.logger.info(f"[Signal] Kicked: {msg.get('reason', 'unknown')}")
            # Connection will close, reconnect loop will handle
        elif msg_type == "pong":
            pass  # Heartbeat response
        elif msg_type == "presence":
            pass  # Not relevant for robot
        else:
            self.logger.debug(f"[Signal] Unknown message type: {msg_type}")

    async def _handle_call(self, msg: dict):
        """Handle WebRTC call (SDP offer from client)."""
        if not self.webrtc_service:
            self.logger.warning("[Signal] WebRTC service not available")
            return

        client_id = msg.get("clientId", "signal-client")
        sdp_offer = msg.get("sdp", "")

        if not sdp_offer:
            self.logger.warning("[Signal] Call message missing SDP")
            return

        self._current_client_id = client_id
        self.logger.info(f"[Signal] Call from client={client_id}")

        try:
            # Create send callback for ICE candidates
            async def send_callback(payload: dict):
                if payload.get("type") == "webrtc_ice_candidate":
                    data = payload.get("data", {})
                    ice_msg = {
                        "type": "ice",
                        "clientId": client_id,
                        "candidate": {
                            "candidate": data.get("candidate", ""),
                            "sdpMid": data.get("sdpMid", ""),
                            "sdpMLineIndex": data.get("sdpMLineIndex", 0),
                        },
                    }
                    if self._ws:
                        await self._ws.send(json.dumps(ice_msg))
                        self.logger.debug(f"[Signal] Sent ICE candidate to client={client_id}")

            # Create peer connection and get SDP answer
            answer_sdp = await self.webrtc_service.create_peer_connection(
                client_id, sdp_offer, send_callback=send_callback
            )

            # Send answer back via signal server
            answer_msg = {
                "type": "answer",
                "clientId": client_id,
                "sdp": answer_sdp,
            }
            if self._ws:
                await self._ws.send(json.dumps(answer_msg))
                self.logger.info(f"[Signal] Sent answer to client={client_id}")

        except Exception as e:
            self.logger.error(f"[Signal] Call handling failed: {e}", exc_info=True)
            # Send error back
            if self._ws:
                error_msg = {
                    "type": "error",
                    "clientId": client_id,
                    "message": f"WebRTC negotiation failed: {e}",
                }
                await self._ws.send(json.dumps(error_msg))

    async def _handle_ice(self, msg: dict):
        """Handle ICE candidate from client."""
        if not self.webrtc_service:
            return

        client_id = msg.get("clientId", self._current_client_id or "signal-client")
        candidate_data = msg.get("candidate", {})

        # Extract candidate fields
        cand_text = candidate_data.get("candidate", "") if isinstance(candidate_data, dict) else str(candidate_data)
        sdp_mid = candidate_data.get("sdpMid", "") if isinstance(candidate_data, dict) else ""
        sdp_mline = candidate_data.get("sdpMLineIndex", 0) if isinstance(candidate_data, dict) else 0

        if not cand_text:
            return

        self.logger.debug(f"[Signal] ICE from client={client_id}: {cand_text[:80]}")

        try:
            await self.webrtc_service.add_ice_candidate(
                client_id,
                candidate=cand_text,
                sdp_mid=sdp_mid,
                sdp_mline_index=int(sdp_mline),
            )
        except Exception as e:
            self.logger.warning(f"[Signal] Failed to add ICE candidate: {e}")

    async def _handle_client_disconnect(self, msg: dict):
        """Handle client disconnect notification."""
        client_id = msg.get("clientId", "")
        if client_id and self.webrtc_service:
            self.logger.info(f"[Signal] Client disconnected: {client_id}")
            try:
                await self.webrtc_service._cleanup_connection(client_id)
            except Exception:
                pass
        if self._current_client_id == client_id:
            self._current_client_id = None
