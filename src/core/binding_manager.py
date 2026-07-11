"""
绑定管理器
管理客户端与机器人的绑定关系，包括绑定会话、Token 生成、持久化和安全限制
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class BindingSession:
    """绑定会话：一次绑定认证流程的状态"""

    request_token: str
    ws_client_id: str  # WebSocket 连接 ID（服务端分配）
    user_client_id: str  # 客户端持久 ID（浏览器生成）
    client_name: str
    method: str  # "display" | "qr_scan" | "tts" | "gimbal"
    random_code: str
    created_at: float
    attempts: int = 0
    # 云台方式：随机序列和选项
    gimbal_sequence: list[str] | None = None
    gimbal_options: list[list[str]] | None = None
    # QR 扫描方式：期望的 QR 数据
    qr_expected: dict[str, Any] | None = None
    # 是否已完成（QR 扫描成功时自动标记）
    completed: bool = False


class BindingManager:
    """绑定管理器：管理绑定会话、持久化绑定关系、安全控制"""

    # 不需要认证即可访问的消息类型
    AUTH_ALLOWED_TYPES = frozenset({
        "ping",
        "subscribe",
        "unsubscribe",
        "connected",
        "bind_request",
        "bind_start",
        "bind_verify",
        "bind_replay",
        "bind_start_scan",
        "bind_cancel",
        "bind_methods",
        "bind_list",
        "bind_share_create",
        "bind_share_use",
    })

    def __init__(
        self,
        config_dir: Path,
        device_id: str,
        secret: str,
        logger: logging.Logger,
        max_clients: int = 10,
        max_failures: int = 5,
        cooldown_seconds: int = 300,
        session_timeout: int = 120,
    ):
        self._config_dir = config_dir
        self._bindings_file = config_dir / "bindings.json"
        self._device_id = device_id
        self._secret = secret
        self._logger = logger
        self._max_clients = max_clients
        self._max_failures = max_failures
        self._cooldown_seconds = cooldown_seconds
        self._session_timeout = session_timeout

        self._bindings: list[dict] = []
        self._sessions: dict[str, BindingSession] = {}  # request_token -> session
        self._failure_counts: dict[str, int] = {}  # ws_client_id -> failure count
        self._cooldowns: dict[str, float] = {}  # ws_client_id -> cooldown_until
        # 分享码: share_code -> {code, created_at, expires_at, used}
        self._share_codes: dict[str, dict] = {}

        self._load_bindings()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_bindings(self) -> None:
        """从 bindings.json 加载绑定记录"""
        if not self._bindings_file.exists():
            self._bindings = []
            self._save_bindings()
            return
        try:
            data = json.loads(self._bindings_file.read_text(encoding="utf-8"))
            self._bindings = data.get("bindings", [])
            if self._logger:
                self._logger.info(f"[Bind] Loaded {len(self._bindings)} bindings from {self._bindings_file}")
        except Exception as e:
            if self._logger:
                self._logger.error(f"[Bind] Failed to load bindings: {e}", exc_info=True)
            self._bindings = []

    def _save_bindings(self) -> None:
        """保存绑定记录到 bindings.json"""
        try:
            self._config_dir.mkdir(parents=True, exist_ok=True)
            data = {"bindings": self._bindings}
            self._bindings_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            if self._logger:
                self._logger.error(f"[Bind] Failed to save bindings: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Token 生成
    # ------------------------------------------------------------------

    def _generate_client_token(self, user_client_id: str) -> str:
        """用 ROBOT_SECRET 对 deviceId + clientId + timestamp 做 HMAC-SHA256 签名"""
        timestamp = str(int(time.time()))
        message = f"{self._device_id}:{user_client_id}:{timestamp}"
        signature = hmac.new(
            self._secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{timestamp}.{signature}"

    def _verify_client_token(self, user_client_id: str, client_token: str) -> bool:
        """验证 clientToken 签名是否有效"""
        try:
            parts = client_token.split(".", 1)
            if len(parts) != 2:
                return False
            timestamp_str, signature = parts
            # 检查时间戳是否为数字（不做过期检查，绑定后永久有效）
            int(timestamp_str)
            message = f"{self._device_id}:{user_client_id}:{timestamp_str}"
            expected = hmac.new(
                self._secret.encode("utf-8"),
                message.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def generate_secret() -> str:
        """自动生成 ROBOT_SECRET"""
        return secrets.token_hex(32)

    @staticmethod
    def generate_request_token() -> str:
        """生成 requestToken（会话标识）"""
        return secrets.token_hex(16)

    @staticmethod
    def generate_random_code(digits: int = 6) -> str:
        """生成随机数字码"""
        return "".join(str(random.randint(0, 9)) for _ in range(digits))

    # ------------------------------------------------------------------
    # 绑定状态检查
    # ------------------------------------------------------------------

    def is_bound(self, user_client_id: str, client_token: str) -> bool:
        """检查 clientId + clientToken 是否有效绑定"""
        if not user_client_id or not client_token:
            return False
        for binding in self._bindings:
            if binding.get("clientId") == user_client_id:
                stored_token = binding.get("clientToken", "")
                if stored_token == client_token and self._verify_client_token(user_client_id, client_token):
                    return True
        return False

    def check_bound_by_token(self, user_client_id: str, client_token: str) -> bool:
        """同 is_bound，语义别名"""
        return self.is_bound(user_client_id, client_token)

    def update_last_seen(self, user_client_id: str) -> None:
        """更新客户端最近连接时间"""
        now_iso = datetime.now(timezone.utc).isoformat()
        for binding in self._bindings:
            if binding.get("clientId") == user_client_id:
                binding["lastSeen"] = now_iso
                break
        self._save_bindings()

    # ------------------------------------------------------------------
    # 安全控制
    # ------------------------------------------------------------------

    def _check_cooldown(self, ws_client_id: str) -> bool:
        """检查客户端是否在冷却期。返回 True 表示在冷却中（不允许操作）"""
        cooldown_until = self._cooldowns.get(ws_client_id, 0)
        if cooldown_until > time.time():
            return True
        # 冷却期已过，清除记录
        if ws_client_id in self._cooldowns:
            del self._cooldowns[ws_client_id]
        if ws_client_id in self._failure_counts:
            del self._failure_counts[ws_client_id]
        return False

    def _record_failure(self, ws_client_id: str) -> None:
        """记录一次验证失败"""
        count = self._failure_counts.get(ws_client_id, 0) + 1
        self._failure_counts[ws_client_id] = count
        if count >= self._max_failures:
            self._cooldowns[ws_client_id] = time.time() + self._cooldown_seconds
            if self._logger:
                self._logger.warning(
                    f"[Bind] Client {ws_client_id} reached {self._max_failures} failures, "
                    f"cooldown {self._cooldown_seconds}s"
                )

    def can_add_binding(self) -> bool:
        """检查是否还可以添加绑定"""
        return len(self._bindings) < self._max_clients

    # ------------------------------------------------------------------
    # 会话管理
    # ------------------------------------------------------------------

    def create_session(
        self,
        ws_client_id: str,
        user_client_id: str,
        client_name: str,
    ) -> BindingSession:
        """创建绑定会话（bind_request 时调用）"""
        # 清理过期会话
        self._cleanup_expired_sessions()

        request_token = self.generate_request_token()
        session = BindingSession(
            request_token=request_token,
            ws_client_id=ws_client_id,
            user_client_id=user_client_id,
            client_name=client_name or "未命名设备",
            method="",  # 在 bind_start 时设置
            random_code="",  # 在 bind_start 时生成
            created_at=time.time(),
        )
        self._sessions[request_token] = session
        if self._logger:
            self._logger.info(f"[Bind] Session created: ws={ws_client_id}, client={user_client_id}, token={request_token[:16]}...")
        return session

    def get_session(self, request_token: str) -> BindingSession | None:
        """获取会话（同时检查是否过期）"""
        session = self._sessions.get(request_token)
        if session is None:
            return None
        if time.time() - session.created_at > self._session_timeout:
            del self._sessions[request_token]
            if self._logger:
                self._logger.info(f"[Bind] Session expired: {request_token[:16]}...")
            return None
        return session

    def start_method(self, request_token: str, method: str) -> BindingSession | None:
        """设置认证方式并生成随机码"""
        session = self.get_session(request_token)
        if session is None:
            return None

        session.method = method

        if method == "display":
            session.random_code = self.generate_random_code(6)
        elif method == "tts":
            session.random_code = self.generate_random_code(4)
        elif method == "qr_scan":
            # QR 扫描不需要 randomCode，机器人扫描客户端的 QR
            session.qr_expected = {
                "deviceId": self._device_id,
                "requestToken": request_token,
                "clientId": session.user_client_id,
                "clientName": session.client_name,
            }
            session.random_code = ""
        elif method == "gimbal":
            # 生成 4 步随机方向序列（上下左右）
            directions = ["上", "下", "左", "右"]
            session.gimbal_sequence = [random.choice(directions) for _ in range(4)]
            # 使用序列字符串作为验证码（直接比较序列内容）
            session.random_code = ",".join(session.gimbal_sequence)

        if self._logger:
            self._logger.info(
                f"[Bind] Method: {method}, requestToken={request_token[:16]}..., "
                f"randomCode={session.random_code}"
            )
            if method == "gimbal":
                self._logger.info(
                    f"[Bind] Gimbal sequence={session.gimbal_sequence}, "
                    f"correctCode={session.random_code}"
                )
        return session

    def verify(self, request_token: str, random_code: str, ws_client_id: str) -> dict:
        """验证随机码并创建绑定"""
        session = self.get_session(request_token)
        if session is None:
            return {"success": False, "error": "会话不存在或已过期"}

        # 检查是否来自同一 WebSocket 连接
        if session.ws_client_id != ws_client_id:
            return {"success": False, "error": "连接不匹配"}

        if session.completed:
            return {"success": False, "error": "会话已完成"}

        session.attempts += 1

        # 验证随机码
        if random_code != session.random_code:
            self._record_failure(ws_client_id)
            if self._logger:
                self._logger.info(
                    f"[Bind] Verify failed: clientId={session.user_client_id}, "
                    f"attempt={session.attempts}, "
                    f"received='{random_code}', expected='{session.random_code}', "
                    f"method={session.method}"
                )
            return {"success": False, "error": "验证码错误"}

        # 验证成功，创建绑定
        client_token = self._generate_client_token(session.user_client_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        binding = {
            "clientId": session.user_client_id,
            "clientName": session.client_name,
            "clientToken": client_token,
            "boundAt": now_iso,
            "lastSeen": now_iso,
        }
        # 如果已有相同 clientId 的绑定，替换
        self._bindings = [b for b in self._bindings if b.get("clientId") != session.user_client_id]
        self._bindings.append(binding)
        self._save_bindings()

        session.completed = True
        # 清理会话
        self._sessions.pop(request_token, None)

        if self._logger:
            self._logger.info(f"[Bind] Verified: clientId={session.user_client_id}, result=success")
        return {"success": True, "client_token": client_token, "binding": binding}

    def verify_qr(self, qr_data: dict, ws_client_id: str) -> dict:
        """QR 扫描验证（机器人扫描客户端的 QR 码后调用）"""
        request_token = qr_data.get("requestToken", "")
        session = self.get_session(request_token)
        if session is None:
            return {"success": False, "error": "会话不存在或已过期"}

        if session.method != "qr_scan":
            return {"success": False, "error": "当前方式不支持 QR 扫描"}

        if session.completed:
            return {"success": False, "error": "会话已完成"}

        # 验证 QR 数据
        if qr_data.get("deviceId") != self._device_id:
            return {"success": False, "error": "设备 ID 不匹配"}
        if qr_data.get("clientId") != session.user_client_id:
            return {"success": False, "error": "客户端 ID 不匹配"}

        # QR 验证成功，创建绑定
        client_token = self._generate_client_token(session.user_client_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        binding = {
            "clientId": session.user_client_id,
            "clientName": session.client_name,
            "clientToken": client_token,
            "boundAt": now_iso,
            "lastSeen": now_iso,
        }
        self._bindings = [b for b in self._bindings if b.get("clientId") != session.user_client_id]
        self._bindings.append(binding)
        self._save_bindings()

        session.completed = True
        self._sessions.pop(request_token, None)

        if self._logger:
            self._logger.info(f"[Bind] QR Verified: clientId={session.user_client_id}, result=success")
        return {"success": True, "client_token": client_token, "binding": binding}

    # ------------------------------------------------------------------
    # 绑定管理
    # ------------------------------------------------------------------

    def get_bindings(self) -> list[dict]:
        """获取所有绑定列表"""
        return self._bindings.copy()

    def remove_binding(self, user_client_id: str) -> bool:
        """移除指定客户端的绑定"""
        before = len(self._bindings)
        self._bindings = [b for b in self._bindings if b.get("clientId") != user_client_id]
        if len(self._bindings) < before:
            self._save_bindings()
            if self._logger:
                self._logger.info(f"[Bind] Removed binding: {user_client_id}")
            return True
        return False

    def remove_all_bindings(self) -> int:
        """移除所有绑定"""
        count = len(self._bindings)
        self._bindings = []
        self._save_bindings()
        if self._logger:
            self._logger.info(f"[Bind] Removed all {count} bindings")
        return count

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def _cleanup_expired_sessions(self) -> None:
        """清理过期会话"""
        now = time.time()
        expired = [token for token, s in self._sessions.items() if now - s.created_at > self._session_timeout]
        for token in expired:
            del self._sessions[token]
        if expired and self._logger:
            self._logger.info(f"[Bind] Cleaned up {len(expired)} expired sessions")

    def cleanup_session(self, request_token: str) -> None:
        """手动清理指定会话"""
        self._sessions.pop(request_token, None)

    def cleanup_client_sessions(self, ws_client_id: str) -> None:
        """清理指定 WebSocket 连接的所有会话"""
        tokens = [token for token, s in self._sessions.items() if s.ws_client_id == ws_client_id]
        for token in tokens:
            del self._sessions[token]

    # ------------------------------------------------------------------
    # 分享绑定码
    # ------------------------------------------------------------------

    def create_share_code(self) -> dict:
        """生成分享绑定码，有效期 2 分钟"""
        self._cleanup_expired_share_codes()
        # 生成 6 位字母数字混合码
        code = secrets.token_urlsafe(4).upper()[:6]
        now = time.time()
        expires_at = now + 120  # 2 分钟有效
        share_info = {
            "code": code,
            "created_at": now,
            "expires_at": expires_at,
            "used": False,
        }
        self._share_codes[code] = share_info
        if self._logger:
            self._logger.info(f"[Bind] Share code created: {code}, expires in 120s")
        return share_info

    def use_share_code(self, code: str, ws_client_id: str, user_client_id: str, client_name: str) -> dict:
        """使用分享码直接完成绑定（无需验证码）"""
        self._cleanup_expired_share_codes()
        code = (code or "").strip().upper()
        share_info = self._share_codes.get(code)
        if share_info is None:
            return {"success": False, "error": "分享码不存在"}
        if share_info["used"]:
            return {"success": False, "error": "分享码已使用"}
        if time.time() > share_info["expires_at"]:
            del self._share_codes[code]
            return {"success": False, "error": "分享码已过期"}

        # 检查绑定数量
        if not self.can_add_binding():
            return {"success": False, "error": "已达最大绑定数量"}

        # 直接创建绑定
        client_token = self._generate_client_token(user_client_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        binding = {
            "clientId": user_client_id,
            "clientName": client_name or "分享绑定设备",
            "clientToken": client_token,
            "boundAt": now_iso,
            "lastSeen": now_iso,
        }
        self._bindings = [b for b in self._bindings if b.get("clientId") != user_client_id]
        self._bindings.append(binding)
        self._save_bindings()

        # 标记已使用
        share_info["used"] = True
        del self._share_codes[code]

        if self._logger:
            self._logger.info(f"[Bind] Share code used: {code}, clientId={user_client_id}")
        return {"success": True, "client_token": client_token, "binding": binding}

    def _cleanup_expired_share_codes(self) -> None:
        """清理过期分享码"""
        now = time.time()
        expired = [code for code, info in self._share_codes.items() if now > info["expires_at"]]
        for code in expired:
            del self._share_codes[code]
        if expired and self._logger:
            self._logger.info(f"[Bind] Cleaned up {len(expired)} expired share codes")
