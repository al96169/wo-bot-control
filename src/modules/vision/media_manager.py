"""
媒体管理模块
负责拍照、录像和图库文件管理。

协议设计参考: R00034 机器人录制视频与拍照功能

存储目录结构:
    {storage_dir}/photos/   — 照片文件 (JPEG)
    {storage_dir}/videos/   — 视频文件 (MP4)

文件命名格式:
    照片: {robot_name}-cam{camera_id}-{YYYYMMDD_HHmmss}.jpg
    视频: {robot_name}-cam{camera_id}-{YYYYMMDD_HHmmss}-{duration}s.mp4
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import cv2
import numpy as np

# ===== 常量 =====

# 画质 → FPS 映射
QUALITY_FPS_MAP: Dict[str, int] = {
    "high": 30,
    "medium": 20,
    "low": 15,
}

# 分辨率 → (width, height) 映射
RESOLUTION_MAP: Dict[str, tuple] = {
    "480p": (640, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}

# 缩略图尺寸
THUMBNAIL_WIDTH = 320
THUMBNAIL_HEIGHT = 240

# 录制参数
MAX_RECORDING_DURATION_S = 10 * 60      # 最大录制时长 10 分钟（10 段 × 1 分钟）
MIN_DISK_SPACE_BYTES = 500 * 1024 * 1024  # 最小剩余空间 500 MB
STATUS_PUSH_INTERVAL_S = 5               # 状态推送间隔 5 秒
JPEG_QUALITY = 85                        # 拍照 JPEG 质量
THUMBNAIL_JPEG_QUALITY = 80              # 缩略图 JPEG 质量

# 循环录制参数
DEFAULT_SEGMENT_DURATION_S = 60          # 默认单段时长 1 分钟
MAX_SEGMENTS = 10                        # 最大保留段数（兜底防溢出）
CLIENT_CHECK_INTERVAL_S = 5              # 客户端在线检查间隔


# ===== 模块级工具函数 =====


def _write_bytes(file_path: str, data: bytes) -> None:
    """写入二进制数据到文件"""
    with open(file_path, "wb") as f:
        f.write(data)


def _make_thumbnail_base64(frame: np.ndarray) -> str:
    """从帧生成缩略图 base64（320x240 JPEG）

    Args:
        frame: BGR 格式的图像帧

    Returns:
        base64 编码的 JPEG 缩略图字符串，失败时返回空字符串
    """
    thumbnail = cv2.resize(frame, (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT))
    success, encoded = cv2.imencode(
        ".jpg", thumbnail, [cv2.IMWRITE_JPEG_QUALITY, THUMBNAIL_JPEG_QUALITY]
    )
    if not success:
        return ""
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _make_thumbnail_from_file(file_path: str) -> str:
    """从图片文件生成缩略图 base64

    Args:
        file_path: 图片文件路径

    Returns:
        base64 编码的缩略图字符串，失败时返回空字符串
    """
    img = cv2.imread(file_path)
    if img is None:
        return ""
    return _make_thumbnail_base64(img)


def _make_video_cover_base64(video_path: str) -> str:
    """从视频文件提取第一帧作为封面缩略图 base64

    Args:
        video_path: 视频文件路径

    Returns:
        base64 编码的封面缩略图字符串，失败时返回空字符串
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return ""
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return ""
    return _make_thumbnail_base64(frame)


def _write_video_frame(
    writer: cv2.VideoWriter, frame: np.ndarray, width: int, height: int
) -> None:
    """将帧写入 VideoWriter，必要时缩放到目标分辨率

    Args:
        writer: OpenCV VideoWriter 实例
        frame: BGR 格式的图像帧
        width: 目标宽度
        height: 目标高度
    """
    h, w = frame.shape[:2]
    if h != height or w != width:
        frame = cv2.resize(frame, (width, height))
    writer.write(frame)


def _create_video_writer(
    file_path: str, fps: int, width: int, height: int
) -> Optional[cv2.VideoWriter]:
    """创建 VideoWriter，按优先级尝试多种编码器

    优先级: avc1 (H.264) → H264 → mp4v (MPEG-4)

    Args:
        file_path: 输出文件路径
        fps: 帧率
        width: 视频宽度
        height: 视频高度

    Returns:
        成功打开的 VideoWriter 实例，全部失败返回 None
    """
    for fourcc_str in ("avc1", "H264", "mp4v"):
        fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
        writer = cv2.VideoWriter(file_path, fourcc, fps, (width, height))
        if writer.isOpened():
            return writer
        writer.release()
    return None


def _get_dir_size(path: str) -> int:
    """计算目录总大小（字节）"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if not os.path.islink(fp):
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    return total


# ===== MediaManager 类 =====


class MediaManager:
    """媒体管理器 —— 拍照、录像和图库文件管理

    通过 camera_manager 获取摄像头帧，使用 OpenCV 进行图像/视频编码，
    将文件存储到本地存储目录，并通过回调函数推送录制状态。

    Attributes:
        storage_dir: 存储根目录
        robot_name: 机器人名称（用于文件命名）
        camera_manager: CameraManager 实例
        logger: 日志记录器
        recording_status_callback: 录制状态推送回调（每 5 秒调用）
        recording_ui_state_callback: 录制 UI 状态推送回调（录制开始/停止时调用）
    """

    def __init__(
        self,
        storage_dir: str,
        robot_name: str,
        camera_manager: Any,
        logger: Optional[Any] = None,
    ):
        self.storage_dir = os.path.abspath(storage_dir)
        self.robot_name = robot_name
        self.camera_manager = camera_manager
        self.logger = logger

        self.photos_dir = os.path.join(self.storage_dir, "photos")
        self.videos_dir = os.path.join(self.storage_dir, "videos")

        # 运行状态
        self.running = False

        # 录制状态
        self._recording = False
        self._recording_task: Optional[asyncio.Task] = None
        self._recording_camera_id: Optional[int] = None
        self._recording_start_time: float = 0.0
        self._recording_segment_start: float = 0.0
        self._current_writer: Optional[cv2.VideoWriter] = None
        self._current_file_path: Optional[str] = None
        self._current_file_start_ts: Optional[str] = None
        self._segment_duration_s: int = 300
        self._recording_fps: int = 20
        self._recording_resolution: str = "720p"
        self._recording_quality: str = "medium"
        self._total_recorded_bytes: int = 0
        self._segment_files: List[Dict[str, Any]] = []
        # 标记是否由录制启动了摄像头流（停止录制时需关闭）
        self._started_stream_for_recording: bool = False

        # 回调函数（由外部注入，用于推送 WebSocket 消息）
        # 签名: async def callback(message: dict) -> None  或  def callback(message: dict) -> None
        self.recording_status_callback: Optional[Callable] = None
        self.recording_ui_state_callback: Optional[Callable] = None
        # 客户端在线检查回调：返回 True 表示至少有一个客户端在线
        # 签名: def callback() -> bool
        self.client_online_check: Optional[Callable[[], bool]] = None

    # ----------------------------------------------------------------
    # 生命周期
    # ----------------------------------------------------------------

    async def start(self) -> None:
        """启动媒体管理器 —— 创建存储目录"""
        os.makedirs(self.photos_dir, exist_ok=True)
        os.makedirs(self.videos_dir, exist_ok=True)
        self.running = True
        if self.logger:
            self.logger.info(
                "MediaManager started (storage: %s)" % self.storage_dir
            )

    async def stop(self) -> None:
        """停止媒体管理器 —— 停止录制并清理资源"""
        self.running = False
        # 停止进行中的录制
        if self._recording:
            await self.stop_recording()
        if self.logger:
            self.logger.info("MediaManager stopped")

    # ----------------------------------------------------------------
    # 拍照
    # ----------------------------------------------------------------

    async def capture(
        self, camera_ids: Optional[List[int]] = None
    ) -> Dict[str, Any]:
        """拍照 —— 对指定摄像头（或全部）各拍一张照片

        调用 camera_manager.get_frame() 获取当前帧，编码为 JPEG 保存到本地，
        并生成 320x240 缩略图 base64 一并返回。

        Args:
            camera_ids: 摄像头 ID 列表，None 表示全部已注册摄像头

        Returns:
            成功: {"success": True, "photos": [{...}]}
            失败: {"success": False, "error": "..."}
        """
        if not self.camera_manager:
            return {"success": False, "error": "Camera manager not available"}

        # 确定目标摄像头列表
        if camera_ids is None:
            target_ids = list(self.camera_manager.cameras.keys())
        else:
            target_ids = list(camera_ids)

        if not target_ids:
            return {"success": False, "error": "No cameras available"}

        # 确保摄像头流已启动（拍照与预览流解耦，必要时自动启动）
        started_streams: List[int] = []
        for cam_id in target_ids:
            active_streams = getattr(self.camera_manager, "active_streams", {})
            if cam_id not in active_streams:
                try:
                    await self.camera_manager.start_stream(cam_id)
                    started_streams.append(cam_id)
                    # 等待第一帧到达（最多 1 秒）
                    for _ in range(10):
                        if self.camera_manager.get_frame(cam_id) is not None:
                            break
                        await asyncio.sleep(0.1)
                except Exception as e:
                    if self.logger:
                        self.logger.warning(
                            "Failed to start stream for camera %s: %s" % (cam_id, e)
                        )

        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        photos: List[Dict[str, Any]] = []

        try:
            for cam_id in target_ids:
                try:
                    photo_info = await self._capture_single(cam_id, ts_str)
                    if photo_info:
                        photos.append(photo_info)
                except Exception as e:
                    if self.logger:
                        self.logger.error(
                            "Capture failed for camera %s: %s" % (cam_id, e),
                            exc_info=True,
                        )
        finally:
            # 停止由拍照启动的摄像头流
            for sid in started_streams:
                try:
                    await self.camera_manager.stop_stream(sid)
                except Exception:
                    pass

        return {
            "success": len(photos) > 0,
            "photos": photos,
        }

    async def _capture_single(
        self, camera_id: int, ts_str: str
    ) -> Optional[Dict[str, Any]]:
        """对单个摄像头拍照

        Args:
            camera_id: 摄像头 ID
            ts_str: 时间戳字符串 (YYYYMMDD_HHmmss)

        Returns:
            照片信息字典，失败返回 None
        """
        # 获取当前帧
        frame = self.camera_manager.get_frame(camera_id)
        if frame is None:
            if self.logger:
                self.logger.warning("No frame from camera %s" % camera_id)
            return None

        loop = asyncio.get_event_loop()

        # 编码为 JPEG (quality=85)
        success, encoded = await loop.run_in_executor(
            None,
            lambda: cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            ),
        )
        if not success:
            if self.logger:
                self.logger.error("JPEG encode failed for camera %s" % camera_id)
            return None

        # 文件命名: {robot_name}-cam{id}-{YYYYMMDD_HHmmss}.jpg
        file_name = "%s-cam%s-%s.jpg" % (self.robot_name, camera_id, ts_str)
        file_path = os.path.join(self.photos_dir, file_name)

        # 写入文件
        await loop.run_in_executor(
            None, _write_bytes, file_path, encoded.tobytes()
        )

        # 获取图片尺寸
        height, width = frame.shape[:2]
        size_bytes = int(encoded.nbytes)

        # 生成缩略图 (320x240 base64)
        thumbnail_b64 = await loop.run_in_executor(
            None, _make_thumbnail_base64, frame
        )

        if self.logger:
            self.logger.info(
                "Photo captured: %s (%dx%d, %d bytes)"
                % (file_name, width, height, size_bytes)
            )

        return {
            "camera_id": camera_id,
            "file_name": file_name,
            "file_path": file_path,
            "thumbnail_base64": thumbnail_b64,
            "size_bytes": size_bytes,
            "width": width,
            "height": height,
            "timestamp": ts_str,
        }

    # ----------------------------------------------------------------
    # 录像
    # ----------------------------------------------------------------

    async def start_recording(
        self,
        camera_id: int,
        quality: str = "medium",
        resolution: str = "720p",
        segment_duration_s: int = DEFAULT_SEGMENT_DURATION_S,
    ) -> Dict[str, Any]:
        """开始循环录像（仅主摄）

        使用 cv2.VideoWriter 录制 MP4 视频，循环录制在后台 asyncio task 中运行。
        每段 1 分钟，最多保留 10 段（超出自动删除最旧段）。
        前端在线则继续录制，前端掉线则自动停止录制。

        Args:
            camera_id: 摄像头 ID（仅主摄）
            quality: 画质 high/medium/low
            resolution: 分辨率 480p/720p/1080p
            segment_duration_s: 单段时长（秒），默认 60（1 分钟）

        Returns:
            成功: {"success": True, "camera_id": ..., "message": ...}
            失败: {"success": False, "error": "..."}
        """
        if self._recording:
            return {"success": False, "error": "Already recording"}

        if not self.camera_manager:
            return {"success": False, "error": "Camera manager not available"}

        # 检查摄像头是否存在
        cameras = getattr(self.camera_manager, "cameras", {})
        if camera_id not in cameras:
            return {
                "success": False,
                "error": "Camera %s not found" % camera_id,
            }

        # 检查磁盘空间
        disk = shutil.disk_usage(self.storage_dir)
        if disk.free < MIN_DISK_SPACE_BYTES:
            return {
                "success": False,
                "error": "Insufficient disk space (%dMB < 500MB)"
                % (disk.free // (1024 * 1024)),
            }

        # 参数映射
        self._recording_fps = QUALITY_FPS_MAP.get(quality, 20)
        self._recording_resolution = resolution
        self._recording_quality = quality
        self._segment_duration_s = segment_duration_s
        self._recording_camera_id = camera_id
        self._recording_start_time = time.time()
        self._recording_segment_start = time.time()
        self._segment_files = []
        self._total_recorded_bytes = 0
        self._started_stream_for_recording = False

        # 确保摄像头流已启动
        active_streams = getattr(self.camera_manager, "active_streams", {})
        if camera_id not in active_streams:
            try:
                await self.camera_manager.start_stream(camera_id)
                self._started_stream_for_recording = True
                # 等待第一帧
                for _ in range(10):
                    if self.camera_manager.get_frame(camera_id) is not None:
                        break
                    await asyncio.sleep(0.1)
            except Exception as e:
                if self.logger:
                    self.logger.warning(
                        "Failed to start stream for recording: %s" % e
                    )

        # 生成文件名（初始无时长，分段结束时重命名加上时长）
        self._current_file_start_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = "%s-cam%s-%s.mp4" % (
            self.robot_name,
            camera_id,
            self._current_file_start_ts,
        )
        self._current_file_path = os.path.join(self.videos_dir, file_name)

        # 创建 VideoWriter
        width, height = RESOLUTION_MAP.get(resolution, (1280, 720))
        loop = asyncio.get_event_loop()
        self._current_writer = await loop.run_in_executor(
            None,
            _create_video_writer,
            self._current_file_path,
            self._recording_fps,
            width,
            height,
        )

        if self._current_writer is None:
            # 清理启动的流
            if self._started_stream_for_recording:
                await self.camera_manager.stop_stream(camera_id)
                self._started_stream_for_recording = False
            return {
                "success": False,
                "error": "Failed to open VideoWriter (no codec available)",
            }

        self._recording = True

        # 推送录制 UI 状态（录制开始）
        await self._push_ui_state(True)

        # 启动录制循环
        self._recording_task = asyncio.create_task(self._recording_loop())

        if self.logger:
            self.logger.info(
                "Recording started: camera=%s, quality=%s, resolution=%s, "
                "fps=%d, segment=%ds"
                % (
                    camera_id,
                    quality,
                    resolution,
                    self._recording_fps,
                    segment_duration_s,
                )
            )

        return {
            "success": True,
            "camera_id": camera_id,
            "message": "Recording started",
        }

    async def _recording_loop(self) -> None:
        """循环录制 —— 在后台 asyncio task 中运行

        每帧从 camera_manager.get_frame() 获取帧并写入 VideoWriter。
        定时（每 5 秒）推送录制状态。
        每段满 1 分钟后自动切新文件，超过 10 段时删除最旧段。
        每 5 秒检查客户端在线状态，掉线则自动停止。
        """
        width, height = RESOLUTION_MAP.get(self._recording_resolution, (1280, 720))
        frame_interval = 1.0 / self._recording_fps
        last_status_push = time.time()
        last_client_check = time.time()

        try:
            while self._recording and self.running and self._current_writer is not None:
                # 检查客户端在线状态（每 5 秒）
                now = time.time()
                if now - last_client_check >= CLIENT_CHECK_INTERVAL_S:
                    last_client_check = now
                    if self.client_online_check is not None:
                        try:
                            online = self.client_online_check()
                        except Exception:
                            online = True  # 检查失败时不停止
                        if not online:
                            if self.logger:
                                self.logger.info(
                                    "Recording auto-stopped: client offline"
                                )
                            break

                # 检查最大段数（单次录制最多 10 段）
                if len(self._segment_files) >= MAX_SEGMENTS:
                    if self.logger:
                        self.logger.info(
                            "Recording auto-stopped: max segments (%d) reached"
                            % MAX_SEGMENTS
                        )
                    break

                # 检查最大录制时长（兜底：10 分钟）
                elapsed = now - self._recording_start_time
                if elapsed >= MAX_RECORDING_DURATION_S:
                    if self.logger:
                        self.logger.info(
                            "Recording auto-stopped: max duration (%ds) reached"
                            % MAX_RECORDING_DURATION_S
                        )
                    break

                # 检查磁盘空间
                disk = shutil.disk_usage(self.storage_dir)
                if disk.free < MIN_DISK_SPACE_BYTES:
                    if self.logger:
                        self.logger.warning(
                            "Recording auto-stopped: low disk space (%dMB)"
                            % (disk.free // (1024 * 1024))
                        )
                    break

                # 检查分段时长
                segment_elapsed = now - self._recording_segment_start
                if segment_elapsed >= self._segment_duration_s:
                    await self._rotate_segment()
                    last_status_push = time.time()

                # 获取帧并写入
                frame = self.camera_manager.get_frame(self._recording_camera_id)
                if frame is not None and self._current_writer is not None:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        _write_video_frame,
                        self._current_writer,
                        frame,
                        width,
                        height,
                    )

                # 每 5 秒推送录制状态
                now = time.time()
                if now - last_status_push >= STATUS_PUSH_INTERVAL_S:
                    await self._push_recording_status()
                    last_status_push = now

                await asyncio.sleep(frame_interval)

        except asyncio.CancelledError:
            if self.logger:
                self.logger.info("Recording loop cancelled")
            # 不 re-raise，让 finally 正常执行清理
        except Exception as e:
            if self.logger:
                self.logger.error(
                    "Recording loop error: %s" % e, exc_info=True
                )
        finally:
            # 确保录制停止并完成文件写入
            await self._finalize_recording()

    async def _rotate_segment(self) -> None:
        """分段录制：关闭当前文件，开启新文件"""
        # 关闭当前 VideoWriter
        if self._current_writer is not None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._current_writer.release)
            self._current_writer = None

        # 完成当前段文件（重命名加上时长）
        segment_duration = int(time.time() - self._recording_segment_start)
        await self._finalize_segment_file(segment_duration)

        # 开始新段
        self._recording_segment_start = time.time()
        self._current_file_start_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_name = "%s-cam%s-%s.mp4" % (
            self.robot_name,
            self._recording_camera_id,
            self._current_file_start_ts,
        )
        self._current_file_path = os.path.join(self.videos_dir, temp_name)

        # 创建新的 VideoWriter
        width, height = RESOLUTION_MAP.get(self._recording_resolution, (1280, 720))
        loop = asyncio.get_event_loop()
        self._current_writer = await loop.run_in_executor(
            None,
            _create_video_writer,
            self._current_file_path,
            self._recording_fps,
            width,
            height,
        )

        if self._current_writer is None:
            # 清理空文件（VideoWriter 可能创建了空文件）
            if self._current_file_path and os.path.exists(self._current_file_path):
                try:
                    os.remove(self._current_file_path)
                except OSError:
                    pass
            self._current_file_path = None
            if self.logger:
                self.logger.error(
                    "Failed to create VideoWriter for new segment, stopping recording"
                )
            # 不设 _recording=False，让 while 条件通过 _current_writer is None 自然退出
            # _finalize_recording 会在 finally 中处理后续清理
        else:
            if self.logger:
                self.logger.info("Rotated to new segment: %s" % temp_name)

    async def _finalize_segment_file(self, duration_s: int) -> None:
        """完成分段文件：重命名加上时长并记录元数据

        将文件从 {name}.mp4 重命名为 {name}-{duration}s.mp4

        Args:
            duration_s: 该段录制时长（秒）
        """
        if not self._current_file_path or not os.path.exists(self._current_file_path):
            return

        loop = asyncio.get_event_loop()

        # 跳过空文件（如摄像头无帧时 VideoWriter 未写入任何数据）
        try:
            if os.path.getsize(self._current_file_path) == 0:
                await loop.run_in_executor(None, os.remove, self._current_file_path)
                if self.logger:
                    self.logger.warning("Skipped empty segment file: %s" % self._current_file_path)
                return
        except OSError:
            pass

        # 重命名：加上时长后缀
        dir_name = os.path.dirname(self._current_file_path)
        final_name = "%s-cam%s-%s-%ds.mp4" % (
            self.robot_name,
            self._recording_camera_id,
            self._current_file_start_ts,
            duration_s,
        )
        final_path = os.path.join(dir_name, final_name)

        try:
            await loop.run_in_executor(
                None, os.rename, self._current_file_path, final_path
            )
        except Exception as e:
            if self.logger:
                self.logger.error("Failed to rename segment file: %s" % e)
            final_path = self._current_file_path
            final_name = os.path.basename(final_path)

        # 获取文件大小
        try:
            size_bytes = os.path.getsize(final_path)
        except OSError:
            size_bytes = 0
        self._total_recorded_bytes += size_bytes

        # 生成封面缩略图
        cover_b64 = await loop.run_in_executor(
            None, _make_video_cover_base64, final_path
        )

        self._segment_files.append(
            {
                "file_name": final_name,
                "file_path": final_path,
                "duration_s": duration_s,
                "size_bytes": size_bytes,
                "resolution": self._recording_resolution,
                "cover_thumbnail_base64": cover_b64,
            }
        )

        if self.logger:
            self.logger.info(
                "Segment finalized: %s (%ds, %d bytes)"
                % (final_name, duration_s, size_bytes)
            )

    async def _finalize_recording(self) -> None:
        """完成录制：关闭 VideoWriter，完成文件，停止流，推送 UI 状态

        此方法可安全重复调用（通过 _recording 标志去重）。
        """
        if not self._recording:
            return

        # 关闭 VideoWriter
        if self._current_writer is not None:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._current_writer.release)
            except Exception:
                pass
            self._current_writer = None

        # 完成当前段文件
        if self._current_file_path and os.path.exists(self._current_file_path):
            segment_duration = int(time.time() - self._recording_segment_start)
            await self._finalize_segment_file(segment_duration)

        # 标记录制结束
        self._recording = False

        # 停止由录制启动的摄像头流
        if self._started_stream_for_recording and self._recording_camera_id is not None:
            try:
                await self.camera_manager.stop_stream(self._recording_camera_id)
            except Exception as e:
                if self.logger:
                    self.logger.debug("Failed to stop camera stream: %s" % e)
            self._started_stream_for_recording = False

        # 推送录制 UI 状态（录制停止）
        await self._push_ui_state(False)

        if self.logger:
            self.logger.info(
                "Recording finalized: segments=%d, total_size=%d bytes"
                % (len(self._segment_files), self._total_recorded_bytes)
            )

    async def stop_recording(self) -> Dict[str, Any]:
        """停止录像

        取消录制后台任务，完成文件写入，返回录制结果。

        Returns:
            成功: {"success": True, "file_name", "file_path", "duration_s",
                   "size_bytes", "resolution", "cover_thumbnail_base64"}
            未在录制: {"success": False, "error": "Not recording"}
        """
        if not self._recording:
            return {"success": False, "error": "Not recording"}

        total_duration = int(time.time() - self._recording_start_time)

        # 取消录制任务
        if self._recording_task is not None and not self._recording_task.done():
            self._recording_task.cancel()
            try:
                await self._recording_task
            except asyncio.CancelledError:
                pass
        self._recording_task = None

        # _finalize_recording 已在 _recording_loop 的 finally 中调用
        # 但如果 task 已自行结束且未被 cancel（如 max duration 触发），也已在 finally 中完成
        if self._recording:
            await self._finalize_recording()

        # 汇总结果
        if self._segment_files:
            last_segment = self._segment_files[-1]
            result: Dict[str, Any] = {
                "success": True,
                "file_name": last_segment["file_name"],
                "file_path": last_segment["file_path"],
                "duration_s": total_duration,
                "size_bytes": self._total_recorded_bytes,
                "resolution": self._recording_resolution,
                "cover_thumbnail_base64": last_segment.get(
                    "cover_thumbnail_base64", ""
                ),
            }
        else:
            result = {
                "success": True,
                "file_name": "",
                "file_path": "",
                "duration_s": total_duration,
                "size_bytes": 0,
                "resolution": self._recording_resolution,
                "cover_thumbnail_base64": "",
            }

        if self.logger:
            self.logger.info(
                "Recording stopped: duration=%ds, segments=%d, total_size=%d bytes"
                % (
                    total_duration,
                    len(self._segment_files),
                    self._total_recorded_bytes,
                )
            )

        # 清理录制状态
        self._recording_camera_id = None
        self._segment_files = []

        return result

    async def _push_recording_status(self) -> None:
        """推送录制状态（每 5 秒调用 recording_status_callback）

        推送 camera_record_status 消息:
            {"type": "camera_record_status",
             "data": {"is_recording", "camera_id", "elapsed_s", "file_size_bytes"}}
        """
        if self.recording_status_callback is None:
            return

        elapsed = int(time.time() - self._recording_start_time)
        file_size = self._total_recorded_bytes
        if self._current_file_path and os.path.exists(self._current_file_path):
            try:
                file_size += os.path.getsize(self._current_file_path)
            except OSError:
                pass

        status_msg = {
            "type": "camera_record_status",
            "data": {
                "is_recording": True,
                "camera_id": self._recording_camera_id,
                "elapsed_s": elapsed,
                "file_size_bytes": file_size,
            },
        }
        try:
            result = self.recording_status_callback(status_msg)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            if self.logger:
                self.logger.debug("recording_status_callback error: %s" % e)

    async def _push_ui_state(self, is_recording: bool) -> None:
        """推送录制 UI 状态（录制开始/停止时调用 recording_ui_state_callback）

        推送 camera_recording_ui_state 消息:
            {"type": "camera_recording_ui_state",
             "data": {"is_recording", "camera_id"}}
        """
        if self.recording_ui_state_callback is None:
            return

        ui_msg = {
            "type": "camera_recording_ui_state",
            "data": {
                "is_recording": is_recording,
                "camera_id": self._recording_camera_id,
            },
        }
        try:
            result = self.recording_ui_state_callback(ui_msg)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            if self.logger:
                self.logger.debug("recording_ui_state_callback error: %s" % e)

    # ----------------------------------------------------------------
    # 图库管理
    # ----------------------------------------------------------------

    async def list_media(
        self,
        media_type: str = "all",
        camera_id: Optional[int] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """查询媒体文件列表

        扫描存储目录，返回按时间倒序排列的文件列表，包含缩略图、文件大小、时间戳。
        同时返回存储空间信息。

        Args:
            media_type: 文件类型 photo/video/all
            camera_id: 按摄像头筛选，None 表示全部
            page: 页码（从 1 开始）
            page_size: 每页数量

        Returns:
            {"total", "page", "files": [...], "storage": {"total_bytes",
             "used_bytes", "available_bytes"}}
        """
        loop = asyncio.get_event_loop()
        files = await loop.run_in_executor(
            None, self._scan_media_files, media_type, camera_id
        )

        # 分页
        total = len(files)
        start = (page - 1) * page_size
        end = start + page_size
        page_files = files[start:end]

        # 存储信息
        disk = shutil.disk_usage(self.storage_dir)
        used_bytes = _get_dir_size(self.storage_dir)

        return {
            "total": total,
            "page": page,
            "files": page_files,
            "storage": {
                "total_bytes": disk.total,
                "used_bytes": used_bytes,
                "available_bytes": disk.free,
            },
        }

    def _scan_media_files(
        self, media_type: str, camera_id: Optional[int]
    ) -> List[Dict[str, Any]]:
        """扫描存储目录，返回按时间倒序排列的文件列表

        Args:
            media_type: photo/video/all
            camera_id: 按摄像头筛选，None 表示全部

        Returns:
            文件信息列表，按修改时间倒序
        """
        files: List[Dict[str, Any]] = []

        # 当前正在录制的文件（不列入列表）
        recording_files = set()
        if self._recording and self._current_file_path:
            recording_files.add(os.path.basename(self._current_file_path))

        cam_filter = None
        if camera_id is not None:
            cam_filter = "-cam%d-" % camera_id

        # 扫描照片
        if media_type in ("all", "photo"):
            if os.path.isdir(self.photos_dir):
                for name in os.listdir(self.photos_dir):
                    if not name.endswith(".jpg"):
                        continue
                    if name in recording_files:
                        continue
                    if cam_filter is not None and cam_filter not in name:
                        continue
                    file_path = os.path.join(self.photos_dir, name)
                    if not os.path.isfile(file_path):
                        continue
                    files.append(
                        self._make_file_info(name, file_path, "photo")
                    )

        # 扫描视频
        if media_type in ("all", "video"):
            if os.path.isdir(self.videos_dir):
                for name in os.listdir(self.videos_dir):
                    if not name.endswith(".mp4"):
                        continue
                    if name in recording_files:
                        continue
                    if cam_filter is not None and cam_filter not in name:
                        continue
                    file_path = os.path.join(self.videos_dir, name)
                    if not os.path.isfile(file_path):
                        continue
                    files.append(
                        self._make_file_info(name, file_path, "video")
                    )

        # 按修改时间倒序
        files.sort(key=lambda f: f.get("_mtime", 0), reverse=True)

        # 移除内部字段
        for f in files:
            f.pop("_mtime", None)

        return files

    def _make_file_info(
        self, file_name: str, file_path: str, media_type: str
    ) -> Dict[str, Any]:
        """构造单个文件的元数据

        Args:
            file_name: 文件名（不含路径）
            file_path: 完整路径
            media_type: photo/video

        Returns:
            文件信息字典
        """
        try:
            size_bytes = os.path.getsize(file_path)
            mtime = os.path.getmtime(file_path)
        except OSError:
            size_bytes = 0
            mtime = 0

        ts_str = datetime.fromtimestamp(mtime).strftime("%Y%m%d_%H%M%S")
        cam_id = self._extract_camera_id(file_name)

        info: Dict[str, Any] = {
            "file_name": file_name,
            "type": media_type,
            "camera_id": cam_id,
            "size_bytes": size_bytes,
            "timestamp": ts_str,
            "_mtime": mtime,
        }

        if media_type == "photo":
            info["thumbnail_base64"] = _make_thumbnail_from_file(file_path)
            # 读取图片尺寸
            img = cv2.imread(file_path)
            if img is not None:
                info["width"] = int(img.shape[1])
                info["height"] = int(img.shape[0])
        else:
            info["thumbnail_base64"] = _make_video_cover_base64(file_path)
            info["duration_s"] = self._extract_video_duration(file_name)

        return info

    def _extract_camera_id(self, file_name: str) -> Optional[int]:
        """从文件名中提取 camera_id

        文件名格式: {robot_name}-cam{id}-{timestamp}...
        例如: wo-bot-cam0-20260701_143025.jpg → 0

        Args:
            file_name: 文件名

        Returns:
            camera_id，无法提取时返回 None
        """
        parts = file_name.split("-")
        for part in parts:
            if part.startswith("cam") and part[3:].isdigit():
                return int(part[3:])
        return None

    def _extract_video_duration(self, file_name: str) -> Optional[int]:
        """从视频文件名中提取时长（秒）

        文件名格式: ...-{duration}s.mp4
        例如: wo-bot-cam0-20260701_143025-300s.mp4 → 300

        Args:
            file_name: 文件名

        Returns:
            时长（秒），无法提取时返回 None
        """
        base = os.path.splitext(file_name)[0]
        parts = base.split("-")
        for part in reversed(parts):
            if part.endswith("s") and part[:-1].isdigit():
                return int(part[:-1])
        return None

    async def delete_media(self, file_names: List[str]) -> Dict[str, Any]:
        """批量删除媒体文件

        Args:
            file_names: 文件名列表（仅文件名，不含路径）

        Returns:
            {"success": bool, "deleted": [...], "failed": [{...}]}
        """
        loop = asyncio.get_event_loop()
        deleted: List[str] = []
        failed: List[Dict[str, str]] = []

        for file_name in file_names:
            # 防止路径遍历攻击：只取文件名部分，忽略任何路径分隔符
            safe_name = os.path.basename(file_name)
            file_path = self.get_media_path(safe_name)

            if file_path is None:
                failed.append({"file_name": safe_name, "error": "File not found"})
                continue

            try:
                await loop.run_in_executor(None, os.remove, file_path)
                deleted.append(safe_name)
                if self.logger:
                    self.logger.info("Deleted media file: %s" % safe_name)
            except Exception as e:
                failed.append({"file_name": safe_name, "error": str(e)})
                if self.logger:
                    self.logger.error(
                        "Failed to delete %s: %s" % (safe_name, e)
                    )

        return {
            "success": len(failed) == 0,
            "deleted": deleted,
            "failed": failed,
        }

    def get_media_path(self, file_name: str) -> Optional[str]:
        """根据文件名获取完整路径

        防止路径遍历攻击：使用 os.path.basename() 去除任何路径分隔符，
        仅在 photos/ 和 videos/ 目录中查找。

        Args:
            file_name: 文件名（仅文件名，不含路径）

        Returns:
            完整文件路径，文件不存在时返回 None
        """
        safe_name = os.path.basename(file_name)

        # 检查 photos 目录
        photo_path = os.path.join(self.photos_dir, safe_name)
        if os.path.isfile(photo_path):
            return photo_path

        # 检查 videos 目录
        video_path = os.path.join(self.videos_dir, safe_name)
        if os.path.isfile(video_path):
            return video_path

        return None

    def get_thumbnail(self, file_name: str) -> Optional[str]:
        """获取文件的 base64 缩略图

        照片: 缩略图 (320x240)
        视频: 封面帧缩略图 (320x240)

        Args:
            file_name: 文件名（仅文件名）

        Returns:
            base64 编码的缩略图字符串，文件不存在或生成失败时返回 None
        """
        file_path = self.get_media_path(file_name)
        if file_path is None:
            return None

        if file_path.endswith(".jpg"):
            thumb = _make_thumbnail_from_file(file_path)
            return thumb if thumb else None
        elif file_path.endswith(".mp4"):
            thumb = _make_video_cover_base64(file_path)
            return thumb if thumb else None

        return None

    # ----------------------------------------------------------------
    # 状态查询
    # ----------------------------------------------------------------

    def is_recording(self) -> bool:
        """是否正在录制"""
        return self._recording

    def get_recording_status(self) -> Dict[str, Any]:
        """获取当前录制状态

        Returns:
            录制中: {"is_recording": True, "camera_id", "elapsed_s",
                     "file_size_bytes"}
            未录制: {"is_recording": False}
        """
        if not self._recording:
            return {"is_recording": False}

        elapsed = int(time.time() - self._recording_start_time)
        file_size = self._total_recorded_bytes
        if self._current_file_path and os.path.exists(self._current_file_path):
            try:
                file_size += os.path.getsize(self._current_file_path)
            except OSError:
                pass

        return {
            "is_recording": True,
            "camera_id": self._recording_camera_id,
            "elapsed_s": elapsed,
            "file_size_bytes": file_size,
        }
