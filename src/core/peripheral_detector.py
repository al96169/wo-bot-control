"""
外设检测模块
检测机器人可用外设（显示器、摄像头、音频输出、云台），用于确定可用的绑定认证方式
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path


class PeripheralDetector:
    """外设检测器：检测机器人可用外设，返回支持的绑定认证方式"""

    # 认证方式优先级排序（qr_scan 已禁用，改为待定开发）
    METHOD_PRIORITY = ["display", "tts", "gimbal"]

    def __init__(self, config: dict | None = None, camera_manager=None, gimbal_controller=None, logger=None):
        self.config = config or {}
        self.camera_manager = camera_manager
        self.gimbal_controller = gimbal_controller
        self.logger = logger or logging.getLogger(__name__)
        # 缓存检测结果（启动时检测一次，后续可手动刷新）
        self._cache: dict[str, bool] | None = None

    def detect_display(self) -> bool:
        """检查 /sys/class/drm/ 下是否有已连接的显示器"""
        try:
            drm_path = Path("/sys/class/drm")
            if not drm_path.exists():
                return False
            for card_dir in drm_path.iterdir():
                if not card_dir.name.startswith("card"):
                    continue
                status_file = card_dir / "status"
                if status_file.exists():
                    status = status_file.read_text().strip()
                    if status == "connected":
                        return True
            return False
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Display detection error: {e}")
            return False

    def detect_camera(self) -> bool:
        """检查摄像头是否可用（优先使用 camera_manager，其次检查 /dev/video*）"""
        if self.camera_manager:
            cameras = getattr(self.camera_manager, "cameras", None)
            if cameras:
                return True
        try:
            for i in range(10):
                if os.path.exists(f"/dev/video{i}"):
                    return True
            return False
        except Exception:
            return False

    def detect_speaker(self) -> bool:
        """检查 ALSA 音频输出设备是否可用"""
        try:
            result = subprocess.run(
                ["aplay", "-l"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return "card" in result.stdout.lower()
        except FileNotFoundError:
            # aplay 不存在，尝试检查 /proc/asound
            return Path("/proc/asound/cards").exists()
        except Exception:
            return False

    def detect_gimbal(self) -> bool:
        """检查云台是否启用"""
        gimbal_config = self.config.get("gimbal", {})
        if not gimbal_config.get("enabled", False):
            return False
        if self.gimbal_controller:
            return True
        return False

    def detect_all(self) -> dict[str, bool]:
        """检测所有外设，返回状态字典"""
        self._cache = {
            "display": self.detect_display(),
            "camera": self.detect_camera(),
            "speaker": self.detect_speaker(),
            "gimbal": self.detect_gimbal(),
        }
        if self.logger:
            self.logger.info(f"[Peripheral] Detection result: {self._cache}")
        return self._cache

    def get_available_methods(self) -> list[str]:
        """返回可用的认证方式列表，按优先级排序

        返回的列表受两层限制：
        1. 外设硬件检测（display/tts/gimbal 需要对应硬件）
        2. 配置开关（config.yaml binding.methods）
        """
        if self._cache is None:
            self.detect_all()

        status = self._cache or {}
        binding_config = self.config.get("binding", {})
        methods_config = binding_config.get("methods", {})

        def _config_enabled(method: str) -> bool:
            return bool(methods_config.get(method, True))

        methods = []

        # display: 需要显示器 + 配置开关
        if status.get("display") and _config_enabled("display"):
            methods.append("display")

        # tts: 需要音频输出 + 配置开关
        if status.get("speaker") and _config_enabled("tts"):
            methods.append("tts")

        # qr_scan: 需要摄像头（暂时禁用），受配置开关控制
        if status.get("camera") and _config_enabled("qr_scan"):
            methods.append("qr_scan")

        # gimbal: 需要云台 + 配置开关
        if status.get("gimbal") and _config_enabled("gimbal"):
            methods.append("gimbal")

        # password: 需要 password_enabled + 配置开关
        if binding_config.get("password_enabled", False) and _config_enabled("password"):
            methods.append("password")

        # share_code: 无硬件依赖，仅受配置开关控制
        if _config_enabled("share_code"):
            methods.append("share_code")

        return methods
