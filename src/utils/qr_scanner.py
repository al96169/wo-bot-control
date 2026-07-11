"""
QR 扫描模块
使用 OpenCV QRCodeDetector 从摄像头帧中检测并解码二维码
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


class QRScanner:
    """QR 扫描器：从摄像头帧中检测二维码"""

    def __init__(self, camera_manager=None, logger: logging.Logger | None = None):
        self.camera_manager = camera_manager
        self.logger = logger or logging.getLogger(__name__)
        self._task: asyncio.Task | None = None
        self._running = False
        self._detector = None
        try:
            import cv2

            self._cv2 = cv2
            self._detector = cv2.QRCodeDetector()
        except ImportError:
            self._cv2 = None
            self._detector = None

    def is_available(self) -> bool:
        """检查 QR 扫描是否可用（需要 OpenCV 和摄像头）"""
        return self._detector is not None and self.camera_manager is not None

    async def scan_once(self, timeout: float = 120.0) -> str | None:
        """扫描一次 QR 码，返回解码后的原始字符串

        Args:
            timeout: 扫描超时时间（秒）
        Returns:
            QR 码原始内容字符串，未找到则返回 None
        """
        if not self.is_available():
            if self.logger:
                self.logger.warning("[QR] Scanner not available (no OpenCV or camera)")
            return None

        # 确保摄像头流已启动
        try:
            await self.camera_manager.start_stream(0)
            if self.logger:
                self.logger.info("[QR] Camera stream started for scanning")
        except Exception as e:
            if self.logger:
                self.logger.warning(f"[QR] Failed to start camera stream: {e}")

        deadline = asyncio.get_event_loop().time() + timeout
        scan_interval = 0.2  # 200ms 扫描间隔
        frame_count = 0
        last_log_time = asyncio.get_event_loop().time()

        if self.logger:
            self.logger.info(f"[QR] Starting scan, timeout={timeout}s")

        while asyncio.get_event_loop().time() < deadline:
            # 检查是否被取消
            try:
                await asyncio.sleep(0)  # 让出控制权，响应取消
            except asyncio.CancelledError:
                if self.logger:
                    self.logger.info("[QR] Scan cancelled")
                return None

            try:
                frame = self.camera_manager.get_frame(0)
                if frame is None:
                    await asyncio.sleep(scan_interval)
                    continue

                frame_count += 1

                # OpenCV QRCodeDetector
                ret, decoded_info, points, _ = self._detector.detectAndDecodeMulti(frame)
                if ret and decoded_info:
                    for info in decoded_info:
                        if not info:
                            continue
                        # 返回原始字符串，由 binding_manager 解析
                        if self.logger:
                            self.logger.info(f"[QR] Detected QR code: {info[:120]}")
                        return info

                # 每 5 秒输出一次扫描状态
                now = asyncio.get_event_loop().time()
                if now - last_log_time > 5:
                    if self.logger:
                        self.logger.info(f"[QR] Scanning... {frame_count} frames checked")
                    last_log_time = now
            except asyncio.CancelledError:
                if self.logger:
                    self.logger.info("[QR] Scan cancelled during frame read")
                return None
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"[QR] Scan frame error: {e}")

            await asyncio.sleep(scan_interval)

        if self.logger:
            self.logger.info(f"[QR] Scan timeout, {frame_count} frames checked, no QR code detected")
        return None

    async def scan_background(
        self,
        on_found: callable,
        timeout: float = 120.0,
    ) -> None:
        """后台扫描 QR 码，找到后调用回调

        Args:
            on_found: 找到 QR 码时的异步回调，接收解码后的 JSON 数据
            timeout: 扫描超时时间（秒）
        """
        self._running = True

        async def _scan_loop():
            try:
                data = await self.scan_once(timeout)
                if data and self._running:
                    await on_found(data)
            finally:
                self._running = False

        self._task = asyncio.create_task(_scan_loop())

    def stop(self) -> None:
        """停止后台扫描"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
