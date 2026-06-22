"""
摄像头管理模块
管理摄像头采集和视频流
"""

from __future__ import annotations

import asyncio
import glob
import os
import subprocess
from typing import Any

import cv2
import numpy as np

# ===== NumPy YUYV → BGR（绕过 OpenCV cvtColor 在 ARM 上的 SIMD 崩溃） =====


def yuyv_to_bgr(frame_1ch: np.ndarray, width: int, height: int) -> np.ndarray:
    """纯 NumPy YUYV(1ch, H×2W) → BGR(3ch, H×W)，不依赖 cv2.cvtColor"""
    yuyv = frame_1ch.reshape(height, width, 2)  # (H, W, 2)
    y = yuyv[:, :, 0].astype(np.float32)
    uv = yuyv[:, :, 1].astype(np.float32)
    u = np.repeat(uv[:, 0::2] - 128.0, 2, axis=1)
    v = np.repeat(uv[:, 1::2] - 128.0, 2, axis=1)
    r = y + 1.402 * v
    g = y - 0.344 * u - 0.714 * v
    b = y + 1.772 * u
    return np.clip(np.stack([b, g, r], axis=2), 0, 255).astype(np.uint8)


# ================================================================


class CameraManager:
    """摄像头管理器"""

    def __init__(self, config: dict | None = None, logger=None):
        self.config = config or {}
        self.logger = logger

        # 配置
        self.default_camera = self.config.get("default_camera", 0)
        self.resolution = self.config.get("resolution", {"width": 320, "height": 240})
        self.fps = self.config.get("fps", 15)
        self.stream_type = self.config.get("stream_type", "mjpeg")

        # 摄像头列表
        self.cameras: dict[int, dict] = {}
        self.active_streams: dict[int, CameraStream | SharedCameraStream] = {}

        # 初始化
        self._detect_cameras()

    def _detect_cameras(self):
        """检测可用摄像头：画面1(左)=USB, 画面2(右)=CSI
        若只有 1 个物理设备则克隆为 2 个逻辑摄像头（帧共享）"""
        # 清理上次运行遗留的 gst-launch-1.0 孤儿进程（避免 CSI 被占用）
        self._cleanup_orphaned_csi()

        # CSI 摄像头 (GStreamer nvarguscamerasrc，回退到 V4L2 /dev/video0)
        csi_cameras = self._detect_csi_cameras()

        # USB 摄像头（跳过 CSI 占用的 /dev/video0）
        usb_cameras = self._detect_usb_cameras(skip_indices={0})

        # 画面1(左) = Camera 0 = USB, 画面2(右) = Camera 1 = CSI
        if usb_cameras:
            usb_cameras[0]["id"] = 0
            usb_cameras[0]["name"] = "USB Camera"
            self.cameras[0] = usb_cameras[0]

        if csi_cameras:
            csi_cameras[0]["id"] = 1
            csi_cameras[0]["name"] = "CSI Camera"
            self.cameras[1] = csi_cameras[0]

        physical_count = len(self.cameras)

        if physical_count == 1:
            # 只有 1 个物理摄像头 → 克隆为 2 个逻辑摄像头（帧共享）
            source_id = 0 if 0 in self.cameras else 1
            source_cam = self.cameras[source_id]
            clone_id = 1 if source_id == 0 else 0

            clone = dict(source_cam)
            clone["id"] = clone_id
            clone["name"] = f"{source_cam['name']} (shared)"
            clone["shared_from"] = source_id
            self.cameras[clone_id] = clone

            if self.logger:
                self.logger.info("Detected 1 physical camera, cloned to 2 logical cameras")
        elif physical_count == 2:
            if self.logger:
                self.logger.info("Detected 2 cameras: USB(cam0) + CSI(cam1)")
        else:
            if self.logger:
                self.logger.info(f"Detected {physical_count} cameras")

    def _cleanup_orphaned_csi(self):
        """清理上次运行遗留的 gst-launch-1.0 孤儿进程"""
        try:
            # 通过进程名匹配：gst-launch-1.0 且参数含 csi_cam 或 nvarguscamerasrc
            result = subprocess.run(
                ["pgrep", "-f", "gst-launch-1.0.*nvarguscamerasrc"], capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                pids = result.stdout.strip().split("\n")
                for pid in pids:
                    try:
                        os.kill(int(pid), 9)
                    except Exception:
                        pass
                # 清理旧帧文件
                for f in glob.glob("/tmp/csi_cam*_f*.jpg"):
                    try:
                        os.remove(f)
                    except Exception:
                        pass
                if self.logger:
                    self.logger.info(f"Cleaned up {len(pids)} orphaned CSI subprocess(es)")
        except Exception:
            pass

    def _detect_csi_cameras(self) -> list[dict]:
        """检测 CSI 摄像头：GStreamer 直接检测 + v4l2-ctl 回退"""
        cameras = []
        csi_pipeline = (
            "nvarguscamerasrc sensor_id=0 ! "
            "video/x-raw(memory:NVMM),width=640,height=480,framerate=30/1 ! "
            "nvvidconv ! video/x-raw,format=BGRx ! "
            "videoconvert ! video/x-raw,format=BGR ! "
            "appsink drop=1 max-buffers=1"
        )

        # 方式 1: GStreamer 直接检测
        try:
            cap = cv2.VideoCapture(csi_pipeline, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                # 验证能读取一帧
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    cameras.append(
                        {
                            "id": 0,
                            "name": "CSI Camera",
                            "type": "csi",
                            "device": "/dev/video0",
                            "pipeline": csi_pipeline,
                            "status": "available",
                        }
                    )
                    if self.logger:
                        self.logger.info("CSI camera detected via GStreamer")
                    cap.release()
                    return cameras
                cap.release()
            else:
                cap.release()
        except Exception as e:
            if self.logger:
                self.logger.debug(f"CSI GStreamer detection failed: {e}")

        # 方式 2: v4l2-ctl 检测
        for video_dev in ["/dev/video0", "/dev/video1"]:
            try:
                result = subprocess.run(["v4l2-ctl", "-d", video_dev, "-D"], capture_output=True, text=True, timeout=5)
                if "tegra-video" in result.stdout or "imx219" in result.stdout.lower() or "ov5693" in result.stdout.lower():
                    cameras.append(
                        {
                            "id": 0,
                            "name": "CSI Camera",
                            "type": "csi",
                            "device": video_dev,
                            "pipeline": None,
                            "status": "available",
                        }
                    )
                    if self.logger:
                        self.logger.info(f"CSI camera detected via v4l2-ctl: {video_dev}")
                    return cameras
            except Exception:
                pass

        # 方式 3: gst-inspect 检测
        try:
            result = subprocess.run(["gst-inspect-1.0", "nvarguscamerasrc"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                cameras.append(
                    {
                        "id": 0,
                        "name": "CSI Camera (fallback)",
                        "type": "csi",
                        "device": "/dev/video0",
                        "pipeline": None,
                        "status": "available",
                    }
                )
                if self.logger:
                    self.logger.info("CSI camera detected via gst-inspect fallback")
                return cameras
        except Exception:
            pass

        return cameras

    def _detect_usb_cameras(self, skip_indices: set | None = None) -> list[dict[str, Any]]:
        """检测 USB 摄像头"""
        cameras: list[dict[str, Any]] = []
        skip_indices = skip_indices or set()

        # 检测 /dev/video* 设备
        for i in range(10):  # 最多检测 10 个
            if i in skip_indices:
                continue
            try:
                cap = cv2.VideoCapture(i)
                if cap.isOpened():
                    cameras.append(
                        {
                            "id": len(cameras),
                            "name": f"USB Camera {i}",
                            "type": "usb",
                            "device": f"/dev/video{i}",
                            "index": i,
                            "status": "available",
                        }
                    )
                    cap.release()
            except Exception:
                pass

        return cameras

    async def start_stream(self, camera_id: int | None = None) -> dict:
        """启动视频流（支持帧共享：共享摄像头复用源摄像头的帧）
        
        引用计数：每次+1，stop_stream 减到 0 才真正暂停采集。
        防止多客户端场景下，一个客户端关摄像头影响其他客户端。
        """
        if camera_id is None:
            camera_id = self.default_camera

        if camera_id not in self.cameras:
            return {"id": camera_id, "status": "error", "message": "Camera not found"}

        # 引用计数初始化
        if not hasattr(self, "_ref_counts"):
            self._ref_counts: dict[int, int] = {}
        self._ref_counts[camera_id] = self._ref_counts.get(camera_id, 0) + 1

        if camera_id in self.active_streams:
            stream = self.active_streams[camera_id]
            if stream.running:
                if self.logger:
                    self.logger.debug(
                        f"Camera {camera_id} already running, ref_count={self._ref_counts[camera_id]}"
                    )
                return stream.get_info()
            # 热恢复：流存在但已暂停
            await stream.start()
            return stream.get_info()

        camera = self.cameras[camera_id]
        shared_from = camera.get("shared_from")

        # 如果是共享摄像头，且源摄像头已在运行 → 复用其帧
        if shared_from is not None and shared_from in self.active_streams:
            source_stream = self.active_streams[shared_from]
            assert isinstance(source_stream, CameraStream), "Source stream must be CameraStream"
            stream = SharedCameraStream(camera, source_stream, self.logger)
            await stream.start()
            self.active_streams[camera_id] = stream
            return stream.get_info()

        # 正常启动物理摄像头流
        stream = CameraStream(camera, self.resolution, self.fps, self.logger)
        await stream.start()
        self.active_streams[camera_id] = stream

        # 自动启动依赖它的共享摄像头
        self._auto_start_shared_streams(camera_id, stream)

        return stream.get_info()

    def _auto_start_shared_streams(self, source_id: int, source_stream: "CameraStream"):
        """自动启动所有依赖于 source_id 的共享摄像头（创建或热恢复）"""
        for cam_id, cam_info in self.cameras.items():
            if cam_info.get("shared_from") == source_id:
                if cam_id not in self.active_streams:
                    shared_stream = SharedCameraStream(cam_info, source_stream, self.logger)
                    self.active_streams[cam_id] = shared_stream
                    shared_stream.running = True
                    if self.logger:
                        self.logger.info(f"Auto-started shared camera {cam_id}: {cam_info['name']}")
                elif not self.active_streams[cam_id].running:
                    # 热恢复已存在的共享流
                    self.active_streams[cam_id].running = True
                    if self.logger:
                        self.logger.info(f"Auto-resumed shared camera {cam_id}: {cam_info['name']}")

    async def stop_stream(self, camera_id: int):
        """暂停视频流 — 引用计数减到 0 才真正停止采集
        
        多客户端安全：只有当没有客户端引用时才真正暂停硬件采集。
        """
        if not hasattr(self, "_ref_counts"):
            self._ref_counts: dict[int, int] = {}
        
        current = self._ref_counts.get(camera_id, 0)
        if current > 0:
            self._ref_counts[camera_id] = current - 1
        
        if camera_id not in self.active_streams:
            if self.logger:
                self.logger.debug(f"Camera {camera_id} not in active_streams (ref={self._ref_counts.get(camera_id, 0)})")
            return

        # 只有引用计数归零时才真正暂停
        if self._ref_counts.get(camera_id, 0) > 0:
            if self.logger:
                self.logger.info(
                    f"Camera {camera_id} stop deferred: "
                    f"still referenced by {self._ref_counts[camera_id]} client(s)"
                )
            return

        stream = self.active_streams[camera_id]
        await stream.stop()
        # 不删除：保留 stream 对象以支持热恢复

        # 如果是源摄像头被停止，也暂停依赖它的共享流
        dependents = [
            cid
            for cid, info in self.cameras.items()
            if info.get("shared_from") == camera_id and cid in self.active_streams
        ]
        for dep_id in dependents:
            await self.active_streams[dep_id].stop()
            if self.logger:
                self.logger.info(f"Paused dependent shared camera {dep_id}")

    async def stop_all(self):
        """停止所有视频流"""
        for stream in list(self.active_streams.values()):
            await stream.stop()
        self.active_streams.clear()

    async def switch_camera(self, camera_id: int) -> dict:
        """切换摄像头（启动指定摄像头，不关闭其他已运行的流）"""
        return await self.start_stream(camera_id)

    async def get_status(self) -> dict:
        """获取摄像头状态"""
        cameras = []

        for cam_id, cam_info in self.cameras.items():
            info = {
                "id": cam_id,
                "name": cam_info["name"],
                "status": "streaming" if cam_id in self.active_streams else "available",
                "resolution": f"{self.resolution['width']}x{self.resolution['height']}",
            }

            if cam_id in self.active_streams:
                stream = self.active_streams[cam_id]
                info["stream_url"] = stream.get_stream_url()

            cameras.append(info)

        return {"cameras": cameras}

    async def stop(self):
        """彻底停止管理器 — 释放所有设备资源"""
        await self.shutdown()

    async def shutdown(self):
        """释放所有摄像头设备资源"""
        for stream in list(self.active_streams.values()):
            await stream.shutdown()
        self.active_streams.clear()
        if self.logger:
            self.logger.info("All camera devices released")

    def get_frame(self, camera_id: int | None = None) -> np.ndarray | None:
        """获取帧（用于 MJPEG 流和 WebRTC）"""
        if camera_id is None:
            camera_id = self.default_camera

        if camera_id in self.active_streams:
            return self.active_streams[camera_id].get_frame()

        return None


class CameraStream:
    """摄像头视频流（物理采集）"""

    def __init__(self, camera_info: dict, resolution: dict, fps: int, logger=None):
        self.camera_info = camera_info
        self.resolution = resolution
        self.fps = fps
        self.logger = logger

        self.cap: cv2.VideoCapture | None = None
        self._csi_proc: subprocess.Popen | None = None
        self._csi_file_prefix: str | None = None
        self.running = False
        self._capture_task: asyncio.Task | None = None
        self.current_frame: np.ndarray | None = None
        self.stream_port = 8080 + camera_info.get("id", 0)

    async def start(self):
        """启动流 — 支持热恢复（设备已打开）和冷启动"""
        if self.running:
            return

        # 热恢复：设备已打开，直接重启采集循环
        # CSI 需额外检查子进程是否存活
        if self._csi_proc is not None and self._csi_proc.poll() is not None:
            self._csi_proc = None  # 已死，回退冷启动
        if self.cap is not None or self._csi_proc is not None:
            self.running = True
            self._capture_task = asyncio.create_task(self._capture_loop())
            if self.logger:
                self.logger.info(f"Camera stream resumed: {self.camera_info['name']}")
            return

        # 冷启动：打开设备
        try:
            # 硬编码低分辨率+低帧率以适配 Jetson Nano VP8 编码
            w, h, fps = 320, 240, 10
            cam_type = self.camera_info.get("type", "usb")
            device = self.camera_info.get("device", f"/dev/video{self.camera_info.get('index', 0)}")

            self.cap = None

            # ---- 策略 1: GStreamer nvarguscamerasrc（仅 CSI，且检测时 GStreamer 成功） ----
            if cam_type == "csi" and self.camera_info.get("pipeline"):
                pipelines = [
                    f"nvarguscamerasrc sensor_id=0 ! video/x-raw(memory:NVMM),width={w},height={h},framerate={fps}/1 ! nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! video/x-raw,format=BGR ! appsink drop=1 max-buffers=1",
                ]
                for pipeline in pipelines:
                    cap_test = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
                    if cap_test.isOpened():
                        self.cap = cap_test
                        if self.logger:
                            self.logger.info("GStreamer nvarguscamerasrc opened")
                        break
                    cap_test.release()

            # ---- 策略 1.5: CSI 子进程采集（gst-launch-1.0 + multifilesink） ----
            if self.cap is None and cam_type == "csi":
                csi_prefix = f"/tmp/csi_cam{self.camera_info.get('id', 1)}"
                # 注意: shell=True 下括号需要转义，否则 /bin/sh 会当成子 shell
                csi_cmd = (
                    f"gst-launch-1.0 -q "
                    f"nvarguscamerasrc sensor_id=0 ! "
                    f"video/x-raw\\(memory:NVMM\\),width={w},height={h},framerate={fps}/1 ! "
                    f"nvvidconv ! nvjpegenc ! "
                    f"multifilesink location={csi_prefix}_f%d.jpg max-files=3 next-file=0"
                )
                try:
                    proc = subprocess.Popen(
                        csi_cmd,
                        shell=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    # 等待第一批帧写入（CSI 初始化约需 1.5 秒）
                    await asyncio.sleep(2.0)
                    # 检查子进程是否还活着
                    if proc.poll() is not None:
                        stderr_output = proc.stderr.read().decode() if proc.stderr else ""
                        if self.logger:
                            self.logger.warning(
                                f"CSI subprocess exited early with code {proc.returncode}: {stderr_output[:200]}"
                            )
                    files = glob.glob(f"{csi_prefix}_f*.jpg")
                    if files:
                        self._csi_proc = proc
                        self._csi_file_prefix = csi_prefix
                        if self.logger:
                            self.logger.info(f"CSI subprocess capture started ({len(files)} frame files)")
                    else:
                        proc.terminate()
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, proc.wait, 2)
                        if self.logger:
                            self.logger.warning("CSI subprocess: no frames produced, falling back")
                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"CSI subprocess launch failed: {e}")

            # ---- 策略 2: GStreamer v4l2src（CSI 和 USB 通用） ----
            if self.cap is None:
                gst_pipeline = (
                    f"v4l2src device={device} ! "
                    f"videoconvert ! video/x-raw,format=BGR,width={w},height={h},framerate={fps}/1 ! "
                    "appsink drop=1 max-buffers=1"
                )
                cap_test = cv2.VideoCapture(gst_pipeline, cv2.CAP_GSTREAMER)
                if cap_test.isOpened():
                    self.cap = cap_test
                    if self.logger:
                        self.logger.info(f"GStreamer v4l2src opened: {device}")

            # ---- 策略 2: V4L2 回退 ----
            if self.cap is None:
                cap_test = cv2.VideoCapture(self.camera_info.get("index", 0), cv2.CAP_V4L2)
                if cap_test.isOpened():
                    cap_test.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                    cap_test.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                    cap_test.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                    cap_test.set(cv2.CAP_PROP_FPS, fps)
                    cap_test.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                    if cap_test.grab() and cap_test.retrieve()[0]:
                        self.cap = cap_test
                        actual_fourcc = int(cap_test.get(cv2.CAP_PROP_FOURCC))
                        self._yuyv = actual_fourcc == cv2.VideoWriter_fourcc(*"YUYV")
                        if self.logger:
                            self.logger.info(
                                f"V4L2 opened fourcc=0x{actual_fourcc:08x} "
                                f"({'YUYV' if self._yuyv else 'MJPG'}) {w}x{h}@{fps}fps"
                            )
                    else:
                        cap_test.release()

            if (self.cap is None or not self.cap.isOpened()) and self._csi_proc is None:
                raise Exception("Failed to open camera (all strategies failed)")

            self.running = True
            # 把 w/h 存入实例供 _capture_loop 使用
            self._cap_w, self._cap_h, self._cap_fps = w, h, fps
            self._capture_task = asyncio.create_task(self._capture_loop())

            if self.logger:
                fmt = "YUYV(NumPy)" if getattr(self, "_yuyv", False) else "MJPG/BGR"
                self.logger.info(f"Camera stream started: {self.camera_info['name']} {w}x{h}@{fps}fps [{fmt}]")

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to start camera stream: {e}")
            self.running = False

    async def stop(self):
        """暂停流 — 停止采集但不释放设备，支持快速恢复"""
        self.running = False
        # 取消采集任务并等待退出（保持 cap/csi_proc 不动）
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
        self._capture_task = None

        if self.logger:
            self.logger.info(f"Camera stream paused: {self.camera_info['name']}")

    async def shutdown(self):
        """彻底释放设备资源（应用退出时调用）"""
        self.running = False
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
            try:
                await self._capture_task
            except asyncio.CancelledError:
                pass
        self._capture_task = None

        # 终止 CSI 子进程
        if self._csi_proc:
            try:
                self._csi_proc.terminate()
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._csi_proc.wait, 3)
            except Exception:
                try:
                    self._csi_proc.kill()
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, self._csi_proc.wait, 2)
                except Exception:
                    pass
            self._csi_proc = None
            if self._csi_file_prefix:
                for f in glob.glob(f"{self._csi_file_prefix}_f*.jpg"):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

        if self.cap:
            self.cap.release()
            self.cap = None

    async def _capture_loop(self):
        """帧采集循环（使用 yuyv_to_bgr 绕过 ARM SIMD 崩溃）"""
        # CSI 子进程模式：从轮转 JPEG 文件读取
        if self._csi_proc is not None:
            await self._capture_loop_csi()
            return

        w, h, fps = getattr(self, "_cap_w", 320), getattr(self, "_cap_h", 240), getattr(self, "_cap_fps", 10)
        is_yuyv = getattr(self, "_yuyv", False)
        self._frame_seq = 0

        while self.running and self.cap:
            try:
                ret, frame = self.cap.read()
                if ret and frame is not None and frame.size > 0:
                    if is_yuyv and len(frame.shape) == 2 and frame.shape[1] == w * 2:
                        frame = yuyv_to_bgr(frame, w, h)
                    self.current_frame = frame
                    self._frame_seq += 1
                    if self._frame_seq == 1 and self.logger:
                        mean_val = frame.mean(axis=(0, 1))
                        self.logger.info(
                            f"First frame: {w}x{h} mean_BGR=({mean_val[0]:.1f},{mean_val[1]:.1f},{mean_val[2]:.1f})"
                        )
                await asyncio.sleep(1.0 / (fps * 2))

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Capture error: {e}")
                self.running = False  # 确保异常时流被标记为停止，允许后续重启
                break

    async def _capture_loop_csi(self):
        """CSI 子进程帧采集循环：从 gst-launch-1.0 multifilesink 读取最新 JPEG"""
        _, _, fps = getattr(self, "_cap_w", 320), getattr(self, "_cap_h", 240), getattr(self, "_cap_fps", 10)
        prefix = self._csi_file_prefix
        self._frame_seq = 0
        last_file = None

        while self.running and self._csi_proc and self._csi_proc.poll() is None:
            try:
                files = glob.glob(f"{prefix}_f*.jpg")
                if files:
                    try:
                        latest = max(files, key=os.path.getmtime)
                    except (FileNotFoundError, ValueError):
                        # 文件在 glob 和 getmtime 之间被 gst-launch 删除/轮转
                        await asyncio.sleep(1.0 / (fps * 2))
                        continue
                    if latest != last_file:
                        frame = cv2.imread(latest)
                        if frame is not None and frame.size > 0:
                            self.current_frame = frame
                            self._frame_seq += 1
                            last_file = latest
                            if self._frame_seq == 1 and self.logger:
                                mean_val = frame.mean(axis=(0, 1))
                                self.logger.info(
                                    f"First CSI frame: {frame.shape[1]}x{frame.shape[0]} "
                                    f"mean_BGR=({mean_val[0]:.1f},{mean_val[1]:.1f},{mean_val[2]:.1f})"
                                )
                await asyncio.sleep(1.0 / (fps * 2))

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.logger:
                    self.logger.error(f"CSI capture error: {e}")
                self.running = False  # 确保异常时流被标记为停止，允许后续重启
                break

        # 子进程异常退出告警
        if self.running and self._csi_proc and self._csi_proc.poll() is not None and self.logger:
            self.logger.warning(f"CSI subprocess exited with code {self._csi_proc.returncode}")
            self.running = False  # 子进程退出时也标记为停止

    def get_frame(self) -> np.ndarray | None:
        """获取当前帧"""
        return self.current_frame

    def get_info(self) -> dict:
        """获取流信息"""
        return {
            "id": self.camera_info.get("id", 0),
            "name": self.camera_info["name"],
            "status": "streaming" if self.running else "stopped",
            "resolution": f"{self.resolution['width']}x{self.resolution['height']}",
            "fps": self.fps,
            "stream_url": self.get_stream_url(),
        }

    def get_stream_url(self) -> str:
        """获取流 URL"""
        return f"http://0.0.0.0:{self.stream_port}/stream"


class SharedCameraStream:
    """共享摄像头流 — 不独立采集，复用源 CameraStream 的帧"""

    def __init__(self, camera_info: dict, source_stream: "CameraStream", logger=None):
        self.camera_info = camera_info
        self.source: CameraStream | None = source_stream
        self.logger = logger
        self.running = False
        self.resolution = source_stream.resolution
        self.fps = source_stream.fps
        self.stream_port = 8080 + camera_info.get("id", 0)

    async def start(self):
        self.running = True
        if self.logger:
            self.logger.info(
                f"Shared camera stream started: {self.camera_info['name']} "
                f"(from camera {self.camera_info.get('shared_from')})"
            )

    async def stop(self):
        self.running = False
        if self.logger:
            self.logger.info(f"Shared camera stream paused: {self.camera_info['name']}")

    async def shutdown(self):
        """彻底清理（共享摄像头无设备需要释放）"""
        self.running = False
        self.source = None

    def get_frame(self) -> np.ndarray | None:
        """从源流获取当前帧"""
        if self.running and self.source:
            return self.source.current_frame
        return None

    def get_info(self) -> dict:
        """获取流信息"""
        return {
            "id": self.camera_info.get("id", 0),
            "name": self.camera_info["name"],
            "status": "streaming" if self.running else "stopped",
            "resolution": f"{self.resolution['width']}x{self.resolution['height']}",
            "fps": self.fps,
            "stream_url": self.get_stream_url(),
        }

    def get_stream_url(self) -> str:
        """获取流 URL"""
        return f"http://0.0.0.0:{self.stream_port}/stream"
