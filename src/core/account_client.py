"""
帐号服务器客户端 (Account Client)
负责与 wo-bot-account 设备管理 API 通信：
- 设备注册（HMAC 认证）
- 心跳上报
- 绑定证明签发（客户端请求 → 机器人生成 HMAC 证明）
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from core.binding_manager import BindingManager

# ----------------------------------------------------------------
# 常量
# ----------------------------------------------------------------
HEARTBEAT_INTERVAL = 60  # 心跳间隔（秒）
BINDING_PROOF_TTL = 300  # 绑定证明有效期（5 分钟）


class AccountClient:
    """帐号服务器客户端

    集成到 WoBotControl 中，在 binding.enabled + account.enabled 时激活。
    """

    def __init__(
        self,
        server_url: str,
        robot_secret: str,
        binding_manager: BindingManager,
        device_id: str,
        robot_name: str = "",
        logger: logging.Logger | None = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.robot_secret = robot_secret
        self.binding_manager = binding_manager
        self.device_id = device_id
        self.robot_name = robot_name
        self.logger = logger or logging.getLogger(__name__)

        self._session: aiohttp.ClientSession | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._registered = False

    # ----------------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------------

    async def start(self) -> None:
        """启动：注册设备 + 开始心跳"""
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        await self._register()
        if self._registered:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """停止：取消心跳 + 关闭 HTTP 会话"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            self._session = None

    # ----------------------------------------------------------------
    # 设备注册
    # ----------------------------------------------------------------

    async def _register(self) -> None:
        """向帐号服务器注册设备"""
        timestamp = int(time.time() * 1000)  # 毫秒级时间戳（与 Node.js Date.now() 一致）
        payload: dict[str, Any] = {
            "robotId": self.device_id,
            "robotName": self.robot_name,
            "timestamp": timestamp,
        }
        # 包含 clientTokenHash（如果存在绑定关系）
        bindings = self.binding_manager.get_bindings() if self.binding_manager else []
        if bindings:
            # 取出第一个绑定 client 的 token hash
            first_binding = bindings[0]
            client_token = first_binding.get("token", "")
            if client_token:
                payload["clientTokenHash"] = hashlib.sha256(client_token.encode()).hexdigest()

        signature = self._sign(str(payload["robotId"]), timestamp)

        assert self._session is not None
        try:
            async with self._session.post(
                f"{self.server_url}/api/devices/register",
                json=payload,
                headers={
                    "X-Robot-Id": self.device_id,
                    "X-Timestamp": str(timestamp),
                    "X-Signature": signature,
                },
            ) as resp:
                if resp.status in (200, 201):
                    self._registered = True
                    self.logger.info(f"[Account] Device registered: {self.device_id}")
                else:
                    body = await resp.text()
                    self.logger.error(f"[Account] Registration failed ({resp.status}): {body[:200]}")
        except aiohttp.ClientError as e:
            self.logger.error(f"[Account] Registration error: {e}")

    # ----------------------------------------------------------------
    # 心跳
    # ----------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """心跳循环：每 HEARTBEAT_INTERVAL 秒上报一次"""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                await self._send_heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.warning(f"[Account] Heartbeat error: {e}")

    async def _send_heartbeat(self) -> None:
        """发送心跳"""
        timestamp = int(time.time() * 1000)
        signature = self._sign(self.device_id, timestamp)

        assert self._session is not None
        try:
            async with self._session.post(
                f"{self.server_url}/api/devices/heartbeat",
                json={"robotId": self.device_id, "timestamp": timestamp},
                headers={
                    "X-Robot-Id": self.device_id,
                    "X-Timestamp": str(timestamp),
                    "X-Signature": signature,
                },
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    self.logger.warning(f"[Account] Heartbeat failed ({resp.status}): {body[:100]}")
        except aiohttp.ClientError as e:
            self.logger.warning(f"[Account] Heartbeat connection error: {e}")

    # ----------------------------------------------------------------
    # 绑定证明签发
    # ----------------------------------------------------------------

    async def generate_binding_proof(self, account_id: str, client_id: str) -> dict | None:
        """生成 HMAC-SHA256 绑定证明（客户端发起绑定时调用）

        Args:
            account_id: 用户在 Logto 中的 userId
            client_id: 客户端持久 ID

        Returns:
            {"payload": {...}, "proof": "hex..."} 或 None（失败时）
        """
        # 检查绑定关系是否匹配
        bindings = self.binding_manager.get_bindings() if self.binding_manager else []
        client_binding = None
        for b in bindings:
            if b.get("clientId") == client_id:
                client_binding = b
                break
        if not client_binding:
            self.logger.warning(f"[Account] No binding found for client {client_id}")
            return None

        client_token = client_binding.get("token", "")
        client_token_hash = hashlib.sha256(client_token.encode()).hexdigest() if client_token else None

        nonce = hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]
        now_ms = int(time.time() * 1000)

        payload = {
            "robotId": self.device_id,
            "clientId": client_id,
            "clientTokenHash": client_token_hash,
            "accountId": account_id,
            "nonce": nonce,
            "expiresAt": now_ms + BINDING_PROOF_TTL * 1000,
        }

        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        proof = hmac.new(
            self.robot_secret.encode("utf-8"),
            payload_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        self.logger.info(
            f"[Account] Generated binding proof for client={client_id}, account={account_id}, nonce={nonce}"
        )

        return {"payload": payload, "proof": proof}

    # ----------------------------------------------------------------
    # 工具方法
    # ----------------------------------------------------------------

    def _sign(self, robot_id: str, timestamp: int) -> str:
        """生成 HMAC-SHA256 签名"""
        message = f"{robot_id}:{timestamp}"
        return hmac.new(
            self.robot_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @classmethod
    def from_config(
        cls,
        config: dict,
        binding_manager: BindingManager,
        device_id: str,
        logger: logging.Logger | None = None,
    ) -> AccountClient | None:
        """从配置文件创建 AccountClient 实例

        配置文件结构:
            account:
                enabled: true
                server_url: "https://account.example.com"
        """
        account_cfg = config.get("account", {})
        if not account_cfg.get("enabled", False):
            return None

        server_url = account_cfg.get("server_url", "")
        if not server_url:
            if logger:
                logger.warning("[Account] Enabled but server_url is empty")
            return None

        # 读取 ROBOT_SECRET
        config_dir = Path(__file__).parent.parent.parent / "config"
        secret = account_cfg.get("secret", "") or config.get("binding", {}).get("secret", "")
        if not secret:
            secret_file = config_dir / ".binding_secret"
            if secret_file.exists():
                secret = secret_file.read_text(encoding="utf-8").strip()
        if not secret:
            if logger:
                logger.error("[Account] ROBOT_SECRET not found (set binding.secret or create config/.binding_secret)")
            return None

        robot_name = config.get("robot", {}).get("name", "")

        return cls(
            server_url=server_url,
            robot_secret=secret,
            binding_manager=binding_manager,
            device_id=device_id,
            robot_name=robot_name,
            logger=logger,
        )
