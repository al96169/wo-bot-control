"""
消息处理器
处理各类 WebSocket 消息
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import pty
import re
import select
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from core.service_manager import SERVICE_DEFINITIONS
from modules.navigation.perception.base import PerceptionResult


class MessageHandler:
    """消息处理器"""

    def __init__(
        self,
        system_collector=None,
        motion_controller=None,
        camera_manager=None,
        config: dict | None = None,
        logger=None,
        service_manager=None,
        mode_manager=None,
        odometry=None,
        safety=None,
        perception=None,
        cruise=None,
        scanner=None,
        patrol=None,
    ):
        self.system_collector = system_collector
        self.motion_controller = motion_controller
        self.camera_manager = camera_manager
        self.config = config or {}
        self.logger = logger or logging.getLogger(__name__)
        self.service_manager = service_manager
        # 导航 S0：运行模式状态机 + 里程计
        self.mode_manager = mode_manager
        self.odometry = odometry
        # 安全底座 S1
        self.safety = safety
        # 纯视觉感知 S2（V1 光流/TTC）
        self.perception = perception
        self.cruise = cruise
        self.scanner = scanner
        self.patrol = patrol
        self._last_cruise: dict | None = None
        # 感知用的摄像头：自主模式下由导航层**自行启动**，不依赖用户打开遥控界面
        nav_cfg = (self.config or {}).get("navigation", {}) or {}
        self._perception_camera_id = int((nav_cfg.get("perception", {}) or {}).get("camera_id", 0))
        self._cam_start_ts = 0.0
        self._cam_start_warned = False
        self._nav_dbg_ts = 0.0
        self._nav_err_ts = 0.0
        self._last_perception: dict | None = None
        self._nav_task: asyncio.Task | None = None
        self._diag_task: asyncio.Task | None = None
        self._wheel_monitor = None
        self._debug_mode_file = "/tmp/wobot_mode"
        self._probe_file = "/tmp/wobot_motion_probe"
        self._probe_active = False
        self._probe_enc0: dict | None = None
        self._probe_t0 = 0.0
        self._probe_last_log = 0.0
        self._last_sent_cmd: tuple | None = None
        # 持久 Shell 会话工作目录（初始为进程当前目录）
        self._shell_cwd = os.getcwd()
        # 云台速度控制：客户端发速度(-1.0~+1.0)，服务端持续循环移动
        self._gimbal_speed = {"pan": 0.0, "tilt": 0.0}
        self._gimbal_running = threading.Event()
        self._gimbal_last_cmd = time.monotonic()  # 云台看门狗：最近一次命令时间
        self._gimbal_task: asyncio.Task | None = None
        # 绑定认证异步任务跟踪（ws_client_id -> [task, ...]）
        self._bind_tasks: dict[str, list[asyncio.Task]] = {}
        # 定期软件更新检查任务
        self._update_check_task: asyncio.Task | None = None
        # 系统操作防重入保护
        self._rebooting = False
        self._shutting_down = False
        self._restarting_service = False

    def start_periodic_tasks(self) -> None:
        """启动定期任务（由 main.py 在系统就绪后调用）"""
        if self._update_check_task is None or self._update_check_task.done():
            self._update_check_task = asyncio.create_task(self._periodic_update_check())
        if self._nav_task is None or self._nav_task.done():
            self._nav_task = asyncio.create_task(self._periodic_navigation())
        if self._diag_task is None or self._diag_task.done():
            self._diag_task = asyncio.create_task(self._periodic_diagnostics())

    async def _periodic_update_check(self, interval: int = 1800) -> None:
        """定期检查软件更新（默认每 30 分钟），广播给所有在线客户端"""
        # 首次启动等待 10 秒（等子服务就绪）
        await asyncio.sleep(10)
        while True:
            try:
                await self._check_and_push_updates()
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"[PeriodicUpdate] check failed: {e}")
            await asyncio.sleep(interval)

    def _is_feature_enabled(self, feature: str) -> bool:
        features_cfg = self.config.get("features", {})
        if not isinstance(features_cfg, dict):
            return True
        return bool(features_cfg.get(feature, True))

    def _feature_disabled_response(self, feature: str) -> dict:
        return {"type": "error", "data": {"code": 403, "message": f"功能 '{feature}' 已被管理员禁用"}}

    async def handle(self, msg_type: str, msg_data: dict) -> dict | None:
        """处理消息"""
        handler = getattr(self, f"_handle_{msg_type}", None)

        if handler:
            return await handler(msg_data)
        else:
            self.logger.warning(f"Unknown message type: {msg_type}")
            return {"type": "error", "data": {"code": 404, "message": f"Unknown message type: {msg_type}"}}

    async def _handle_ping(self, data: dict) -> dict:
        """处理心跳"""
        return {"type": "pong", "data": {"ts": data.get("ts", 0)}}

    async def _handle_subscribe(self, data: dict) -> dict:
        """处理 DataChannel 事件订阅"""
        events = data.get("events", [])
        if self.logger:
            self.logger.info(f"DataChannel subscribe to events: {events}")
        return {"type": "subscribed", "data": {"events": events}}

    async def _handle_get_status(self, data: dict) -> dict:
        """处理状态请求"""
        status_data = {}
        if self.system_collector:
            status_data = await self.system_collector.collect()

        # 省电策略评估：根据当前电量自动切换模式
        if hasattr(self, "power_policy") and self.power_policy:
            battery = status_data.get("battery", {})
            battery_level = battery.get("level", 100) if battery else 100
            await self.power_policy.evaluate(battery_level)
            status_data["power_policy"] = self.power_policy.get_status()
        # 附带 features，确保客户端始终能获取最新功能列表
        # 基础功能（不可禁用）
        features = ["websocket", "exec", "system"]
        # 可配置功能：控制器存在 + config.features 未显式关闭
        if hasattr(self, "motion_controller") and self.motion_controller and self._is_feature_enabled("motion"):
            features.append("motion")
        if hasattr(self, "camera_manager") and self.camera_manager and self._is_feature_enabled("camera"):
            features.append("camera")
        if hasattr(self, "gimbal_controller") and self.gimbal_controller:
            features.append("gimbal")
        if hasattr(self, "dance_controller") and self.dance_controller and self._is_feature_enabled("dance"):
            if self.service_manager:
                svc = self.service_manager.get_service_status("dance")
                if svc and svc.get("status") == "running":
                    features.append("dance")
            else:
                features.append("dance")
        if "music_player" in SERVICE_DEFINITIONS and self._is_feature_enabled("music"):
            features.append("music")
        if (
            hasattr(self, "voice_broadcast_controller")
            and self.voice_broadcast_controller
            and self._is_feature_enabled("voice_broadcast")
        ):
            features.append("voice_broadcast")
        # R00034: 拍照/录像功能（MediaManager 存在 + feature 未禁用）
        if self._get_media_manager() and self._is_feature_enabled("camera_capture"):
            features.append("camera_capture")
        # R00020: 红外遥控（IRController 存在 + feature 未禁用）
        if hasattr(self, "ir_controller") and self.ir_controller and self._is_feature_enabled("ir_remote"):
            features.append("ir_remote")
        status_data["features"] = features

        # R00044: 外设在线状态（随 status 消息周期推送）
        if self.system_collector and hasattr(self.system_collector, "get_peripheral_status"):
            status_data["peripherals"] = self.system_collector.get_peripheral_status()

        return {"type": "status", "data": status_data}

    async def _handle_get_peripheral_history(self, data: dict) -> dict:
        """R00045: 查询传感器历史数据（WebSocket）"""
        slots_raw = data.get("slots", "")
        if isinstance(slots_raw, str):
            slot_list = [s.strip() for s in slots_raw.split(",") if s.strip()]
        elif isinstance(slots_raw, list):
            slot_list = slots_raw
        else:
            return {"type": "error", "data": {"code": 400, "message": "slots is required (string or list)"}}

        if not slot_list:
            return {"type": "error", "data": {"code": 400, "message": "At least one slot name is required"}}

        range_str = data.get("range", "1h")
        from_ts = data.get("from_ts")
        to_ts = data.get("to_ts")

        recorder = getattr(self, "sensor_recorder", None)

        if not recorder:
            return {"type": "error", "data": {"code": 503, "message": "Sensor recorder not available"}}

        try:
            result = await recorder.query(
                slots=slot_list,
                start_ts=from_ts,
                end_ts=to_ts,
                range_str=range_str,
            )
            return {"type": "peripheral_history", "data": result}
        except Exception as e:
            if self.logger:
                self.logger.error(f"Peripheral history query error: {e}")
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_motion(self, data: dict) -> dict:
        """处理运动控制（支持双轴兼容 + 三轴麦轮协议）"""
        if not self._is_feature_enabled("motion"):
            return self._feature_disabled_response("motion")
        if not self.motion_controller:
            return {"type": "error", "data": {"code": 503, "message": "Motion controller not available"}}

        # 控制权仲裁：自主模式下，**手动指令不能覆盖自主决策**。
        # 真实故障：遥控界面即使摇杆不动也会持续发 motion(0,0,0)（约 20Hz），
        # 把自主控制器的直行指令每秒覆盖几十次 → 机器人看起来"完全不动"。
        # 规则：自主模式下
        #   · 零值手动指令 → 忽略（不覆盖自主决策）
        #   · 非零手动输入 → 视为"人接管"：先切回 manual，再执行该指令
        if self.mode_manager is not None and self.mode_manager.is_autonomous():
            try:
                _mv = max(abs(float(data.get(k, 0) or 0)) for k in ("v_x", "v_y", "v_z", "linear", "angular"))
            except (TypeError, ValueError):
                _mv = 0.0
            if _mv < 0.01:
                return {"type": "motion_ack", "data": {"ignored": True, "reason": "自主模式下忽略零值手动指令"}}
            self.mode_manager.force_manual("检测到手动操作，交还控制权")
            if self.logger:
                self.logger.warning("[Arbitration] 自主模式下收到手动输入 → 已切回 manual（接管）")

        # 安全门禁（S1）：安全停车生效期间**拒绝任何运动指令**，
        # 否则用户摇杆可以让被安全停下的机器人重新动起来。
        if self.safety is not None and self.safety.tripped:
            ev = self.safety.active_event
            return {
                "type": "motion_ack",
                "data": {
                    "blocked": True,
                    "reason": ev.reason if ev else "安全停车生效中",
                    "safety": self.safety.get_status(),
                },
            }

        try:
            # 三轴麦轮协议: {v_x, v_y, v_z}（平移摇杆+偏航摇杆合并后发送）
            if "v_x" in data or "v_y" in data or "v_z" in data:
                v_x = float(data.get("v_x", 0) or 0)
                v_y = float(data.get("v_y", 0) or 0)
                v_z = float(data.get("v_z", 0) or 0)
                mode = data.get("mode", "manual")
                await self.motion_controller.set_mecanum_velocity(v_x, v_y, v_z, mode)
                return {"type": "motion_ack", "data": {"v_x": v_x, "v_y": v_y, "v_z": v_z, "mode": mode}}
            # 双轴兼容协议: {linear, angular}
            else:
                linear = float(data.get("linear", 0) or 0)
                angular = float(data.get("angular", 0) or 0)
                mode = data.get("mode", "manual")
                await self.motion_controller.set_velocity(linear, angular, mode)
                return {"type": "motion_ack", "data": {"linear": linear, "angular": angular, "mode": mode}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_motion_stop(self, data: dict) -> dict:
        """处理停止运动"""
        if not self._is_feature_enabled("motion"):
            return self._feature_disabled_response("motion")
        if self.motion_controller:
            await self.motion_controller.stop()
        return {"type": "motion_ack", "data": {"linear": 0, "angular": 0}}

    async def _handle_gimbal(self, data: dict) -> dict | None:
        """处理云台控制"""
        if not self._is_feature_enabled("gimbal"):
            return self._feature_disabled_response("gimbal")
        if not hasattr(self, "gimbal_controller") or not self.gimbal_controller:
            return {"type": "error", "data": {"code": 503, "message": "Gimbal not available"}}

        action = data.get("action", "set_angle")

        try:
            if action == "center":
                self._stop_gimbal_loop()
                await self.gimbal_controller.center()
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
            elif action == "get_state":
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
            elif action == "move":
                # 增量控制 (兼容旧版，每帧一发): {action: "move", pan_delta: -1.0, tilt_delta: 0.5}
                step = float(data.get("step") or self.gimbal_controller.step)
                pan_delta = float(data.get("pan_delta", 0) or 0)
                tilt_delta = float(data.get("tilt_delta", 0) or 0)

                pan_r = await self.gimbal_controller.move_pan(pan_delta, step)
                tilt_r = await self.gimbal_controller.move_tilt(tilt_delta, step)

                state = self.gimbal_controller.get_state()
                resp = {"type": "gimbal_status", "data": state}

                # 限位检测：在 state 上附加 limit 信息
                if pan_r.get("limit") or tilt_r.get("limit"):
                    resp["type"] = "gimbal_limit"
                    if tilt_r.get("limit"):
                        state["limit_axis"] = "tilt"
                        state["limit"] = "max" if tilt_r["tilt"] == self.gimbal_controller.tilt_max else "min"
                    elif pan_r.get("limit"):
                        state["limit_axis"] = "pan"
                        state["limit"] = "max" if pan_r["pan"] == self.gimbal_controller.pan_max else "min"

                return resp
            elif action == "move_begin":
                # 开始持续移动: {action: "move_begin", pan_speed: 0.5, tilt_speed: -0.3}
                self._gimbal_speed["pan"] = float(data.get("pan_speed", 0) or 0)
                self._gimbal_speed["tilt"] = float(data.get("tilt_speed", 0) or 0)
                self._gimbal_last_cmd = time.monotonic()
                self._start_gimbal_loop()
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
            elif action == "move_update":
                # 更新移动速度: {action: "move_update", pan_speed: 0.5, tilt_speed: -0.3}
                self._gimbal_speed["pan"] = float(data.get("pan_speed", 0) or 0)
                self._gimbal_speed["tilt"] = float(data.get("tilt_speed", 0) or 0)
                self._gimbal_last_cmd = time.monotonic()
                # 不返回 status，避免大量响应阻塞
                return None
            elif action == "move_end":
                # 停止持续移动
                self._gimbal_speed["pan"] = 0.0
                self._gimbal_speed["tilt"] = 0.0
                self._gimbal_last_cmd = time.monotonic()
                self._stop_gimbal_loop()
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
            else:
                # 绝对角度控制 (兼容旧版)
                axis = data.get("axis", "pan")
                angle = data.get("angle", 90)
                if axis == "pan":
                    await self.gimbal_controller.set_pan(float(angle))
                elif axis == "tilt":
                    await self.gimbal_controller.set_tilt(float(angle))
                else:
                    return {"type": "error", "data": {"code": 400, "message": f"Unknown axis: {axis}"}}
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    def _start_gimbal_loop(self):
        """启动云台持续移动循环 — 整个循环放进一个 executor 线程，消除逐 tick 调度抖动"""
        if self._gimbal_running.is_set():
            return
        self._gimbal_running.set()
        loop = asyncio.get_event_loop()
        self._gimbal_task = loop.run_in_executor(None, self._gimbal_thread_fn)  # type: ignore[assignment]

    def _stop_gimbal_loop(self):
        """停止云台持续移动循环"""
        self._gimbal_running.clear()
        self._gimbal_task = None

    def _gimbal_thread_fn(self):
        """云台移动线程：全同步执行，小步高频，舵机来不及完成单步→视觉平滑"""
        gc = self.gimbal_controller
        step_per_tick = getattr(gc, "step", 1.5)  # 从配置读取步进角度，默认 1.5

        try:
            while self._gimbal_running.is_set():
                pan_spd = self._gimbal_speed.get("pan", 0)
                tilt_spd = self._gimbal_speed.get("tilt", 0)

                # 看门狗：超过 1.5s 未收到任何云台命令 → 自动停止，兜底丢失的 move_end
                if time.monotonic() - getattr(self, "_gimbal_last_cmd", 0.0) > 1.5:
                    self._gimbal_speed["pan"] = 0.0
                    self._gimbal_speed["tilt"] = 0.0
                    self._stop_gimbal_loop()
                    break

                if pan_spd == 0 and tilt_spd == 0:
                    time.sleep(0.05)
                    continue

                pan_delta = pan_spd * step_per_tick
                tilt_delta = tilt_spd * step_per_tick

                # 限位
                state = gc.get_state()
                p, t = state["pan"], state["tilt"]
                if p <= gc.pan_min and pan_delta < 0 or p >= gc.pan_max and pan_delta > 0:
                    pan_delta = 0
                if t <= gc.tilt_min and tilt_delta < 0 or t >= gc.tilt_max and tilt_delta > 0:
                    tilt_delta = 0

                try:
                    gc.move_pan_tilt_sync(pan_delta, tilt_delta, 1.0)
                except Exception:
                    pass

                time.sleep(0.02)  # 20ms → 50Hz 微步

        except Exception as e:
            if self.logger:
                self.logger.error(f"Gimbal thread error: {e}", exc_info=True)

    async def _handle_motion_config(self, data: dict) -> dict:
        """处理运动配置"""
        if not self._is_feature_enabled("motion"):
            return self._feature_disabled_response("motion")
        if self.motion_controller:
            drive_type = data.get("drive_type")
            max_linear = data.get("max_linear_speed")
            max_angular = data.get("max_angular_speed")

            if drive_type:
                self.motion_controller.set_drive_type(drive_type)
            if max_linear is not None:
                self.motion_controller.max_linear_speed = max_linear
            if max_angular is not None:
                self.motion_controller.max_angular_speed = max_angular

        return {"type": "motion_config_ack", "data": data}

    async def _handle_dance(self, data: dict) -> dict:
        """处理舞蹈控制命令"""
        command = data.get("command", "status")

        # 功能开关检查（只读查询放行）
        if command not in ("status", "list") and not self._is_feature_enabled("dance"):
            return self._feature_disabled_response("dance")

        # 省电模式下跳舞不可用——但允许只读查询（status/list）通过，避免轮询持续弹 403
        if (
            command not in ("status", "list")
            and hasattr(self, "power_policy")
            and self.power_policy
            and self.power_policy.is_eco
        ):
            self.logger.warning(f"跳舞命令被拒绝: 省电模式 (command={command})")
            return {"type": "error", "data": {"code": 403, "message": "电量不足，省电模式下跳舞不可用"}}
        # 检查 service_manager 中的运行状态（防止进程管理器停服后仍响应）
        if self.service_manager:
            svc = self.service_manager.get_service_status("dance")
            if not svc or svc.get("status") != "running":
                return {"type": "error", "data": {"code": 503, "message": "Dance service is not running"}}
        if not hasattr(self, "dance_controller") or not self.dance_controller:
            return {"type": "error", "data": {"code": 503, "message": "Dance controller not available"}}

        command = data.get("command", "status")
        return await self.dance_controller.handle_command(command, data)

    async def _handle_camera(self, data: dict) -> dict:
        """处理摄像头控制命令"""
        if not self._is_feature_enabled("camera"):
            return self._feature_disabled_response("camera")
        if not self.camera_manager:
            return {"type": "error", "data": {"code": 503, "message": "Camera not available"}}

        action = data.get("action")
        camera_id = data.get("camera_id", 0)

        if action in ("start", "stop", "switch"):
            self.logger.info(f"摄像头控制: {action} (camera_id={camera_id})")

        if action == "list":
            status = await self.camera_manager.get_status()
            return {"type": "camera_status", "data": status}
        elif action == "start":
            result = await self.camera_manager.start_stream(camera_id)
            return {"type": "camera_status", "data": result}
        elif action == "stop":
            await self.camera_manager.stop_stream(camera_id)
            return {"type": "camera_status", "data": {"id": camera_id, "status": "stopped"}}
        elif action == "switch":
            result = await self.camera_manager.switch_camera(camera_id)
            return {"type": "camera_status", "data": result}

        return {"type": "error", "data": {"code": 400, "message": "Invalid camera action"}}

    async def _handle_camera_status(self, data: dict) -> dict:
        """获取摄像头状态"""
        if self.camera_manager:
            status = await self.camera_manager.get_status()
            return {"type": "camera_status", "data": status}
        return {"type": "camera_status", "data": {"cameras": []}}

    # ===== R00034: 拍照 / 录像 / 图库管理 =====

    def _get_media_manager(self):
        """获取 MediaManager 实例"""
        return getattr(self, "media_manager", None)

    async def _handle_camera_capture(self, data: dict) -> dict:
        """拍照：camera_capture 消息处理"""
        if not self._is_feature_enabled("camera_capture"):
            return self._feature_disabled_response("camera_capture")
        mm = self._get_media_manager()
        if not mm:
            return {"type": "error", "data": {"code": 503, "message": "Media manager not available"}}
        # 拍照画质从机器人自身配置读取（前端通过 config_set 同步），不从消息中取
        camera_cfg = self.config.get("camera", {})
        quality = camera_cfg.get("capture_quality", "high")
        result = await mm.capture(quality, data.get("camera_ids"))
        return {"type": "camera_capture_result", "data": result}

    async def _handle_camera_record_start(self, data: dict) -> dict:
        """开始录像：camera_record_start 消息处理"""
        if not self._is_feature_enabled("camera_capture"):
            return self._feature_disabled_response("camera_capture")
        mm = self._get_media_manager()
        if not mm:
            return {"type": "error", "data": {"code": 503, "message": "Media manager not available"}}
        camera_id = int(data.get("camera_id", 0))
        # 录像画质从机器人自身配置读取（前端通过 config_set 同步），不从消息中取
        camera_cfg = self.config.get("camera", {})
        quality = camera_cfg.get("record_quality", "high")
        resolution = data.get("resolution", "720p")
        segment_duration_s = int(data.get("segment_duration_s", 300))
        result = await mm.start_recording(camera_id, quality, resolution, segment_duration_s)
        return {"type": "camera_record_result", "data": result}

    async def _handle_camera_record_stop(self, data: dict) -> dict:
        """停止录像：camera_record_stop 消息处理"""
        if not self._is_feature_enabled("camera_capture"):
            return self._feature_disabled_response("camera_capture")
        mm = self._get_media_manager()
        if not mm:
            return {"type": "error", "data": {"code": 503, "message": "Media manager not available"}}
        result = await mm.stop_recording()
        return {"type": "camera_record_result", "data": result}

    async def _handle_camera_record_query(self, data: dict) -> dict:
        """查询当前录制状态：camera_record_query"""
        mm = self._get_media_manager()
        if not mm:
            return {"type": "camera_record_status", "data": {"is_recording": False}}
        is_recording = getattr(mm, "_recording", False)
        camera_id = getattr(mm, "_recording_camera_id", None)
        start_time = getattr(mm, "_recording_start_time", None)
        elapsed_s = int(time.time() - start_time) if (is_recording and start_time) else 0
        total_bytes = getattr(mm, "_total_recorded_bytes", 0)
        segment_count = len(getattr(mm, "_segment_files", []))
        return {
            "type": "camera_record_status",
            "data": {
                "is_recording": is_recording,
                "camera_id": camera_id,
                "elapsed_s": elapsed_s,
                "file_size_bytes": total_bytes,
                "segment_count": segment_count,
            },
        }

    async def _handle_camera_media_list(self, data: dict) -> dict:
        """图库列表：camera_media_list 消息处理"""
        mm = self._get_media_manager()
        if not mm:
            return {"type": "error", "data": {"code": 503, "message": "Media manager not available"}}
        media_type = data.get("type", "all")
        camera_id = data.get("camera_id")
        page = int(data.get("page", 1))
        page_size = int(data.get("page_size", 20))
        result = await mm.list_media(media_type, camera_id, page, page_size)
        return {"type": "camera_media_list_result", "data": result}

    async def _handle_camera_media_delete(self, data: dict) -> dict:
        """删除媒体文件：camera_media_delete 消息处理"""
        mm = self._get_media_manager()
        if not mm:
            return {"type": "error", "data": {"code": 503, "message": "Media manager not available"}}
        file_names = data.get("file_names", [])
        result = await mm.delete_media(file_names)
        return {"type": "camera_media_delete_result", "data": result}

    async def _handle_camera_media_download(self, data: dict) -> dict:
        """请求下载文件：camera_media_download 消息处理
        分块传输：将文件分成 64KB 的块，通过 DC 逐块发送。
        前端收到所有块后组装成 blob。
        """
        mm = self._get_media_manager()
        if not mm:
            return {"type": "error", "data": {"code": 503, "message": "Media manager not available"}}
        file_name = data.get("file_name", "")
        media_path = mm.get_media_path(file_name)
        if not media_path:
            return {"type": "error", "data": {"code": 404, "message": "File not found"}}
        file_size = os.path.getsize(media_path)
        thumbnail = mm.get_thumbnail(file_name)

        # 限制 100MB
        if file_size > 100 * 1024 * 1024:
            return {
                "type": "camera_media_download_data",
                "data": {
                    "file_name": file_name,
                    "size_bytes": file_size,
                    "error": "File too large (>100MB)",
                },
            }

        import base64

        CHUNK_SIZE = 48 * 1024  # 48KB raw → ~64KB base64（DC 消息安全大小）
        total_chunks = (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE

        # 优先使用 DataChannel 分块传输。
        # 1) DC 消息处理期间（_dc_client_id 有值）→ 定向发送
        # 2) WS 收到请求但已建立 DC（client DC 可能因 server DC 覆盖而丢消息）→ 广播分块
        dc_client_id = getattr(self, "_dc_client_id", None)
        webrtc_svc = getattr(self, "_webrtc_service", None)
        if webrtc_svc:
            targets = [dc_client_id] if dc_client_id else []
            if not targets:
                # 从 webrtc_service 收集所有 open 的 DC 客户端（WS 请求场景）
                for cid, _dc in webrtc_svc.get_open_data_channels().items():
                    targets.append(cid)
            if targets:
                for target in targets:
                    # 发送开始消息
                    await webrtc_svc.send_message(
                        target,
                        {
                            "type": "camera_media_download_start",
                            "data": {
                                "file_name": file_name,
                                "size_bytes": file_size,
                                "total_chunks": total_chunks,
                                "thumbnail_base64": thumbnail,
                            },
                        },
                    )
                    # 逐块读取并发送
                    with open(media_path, "rb") as f:
                        chunk_index = 0
                        while True:
                            chunk = f.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            chunk_b64 = base64.b64encode(chunk).decode("ascii")
                            await webrtc_svc.send_message(
                                target,
                                {
                                    "type": "camera_media_download_chunk",
                                    "data": {
                                        "file_name": file_name,
                                        "chunk_index": chunk_index,
                                        "total_chunks": total_chunks,
                                        "data": chunk_b64,
                                    },
                                },
                            )
                            chunk_index += 1
                    # 发送完成消息
                    await webrtc_svc.send_message(
                        target,
                        {
                            "type": "camera_media_download_end",
                            "data": {
                                "file_name": file_name,
                                "size_bytes": file_size,
                                "total_chunks": total_chunks,
                            },
                        },
                    )
                # 返回 None（不通过 send_message 再发一次）
                return {"type": "camera_media_download_done", "data": {"file_name": file_name}}

        # 无 DC 上下文（通过 WebSocket），回退到单次 base64（小文件）
        if file_size <= 2 * 1024 * 1024:
            with open(media_path, "rb") as f:
                file_data = f.read()
            return {
                "type": "camera_media_download_data",
                "data": {
                    "file_name": file_name,
                    "size_bytes": file_size,
                    "file_base64": base64.b64encode(file_data).decode("ascii"),
                    "thumbnail_base64": thumbnail,
                },
            }
        # 大文件无法通过 WS 分块，返回错误
        return {
            "type": "camera_media_download_data",
            "data": {
                "file_name": file_name,
                "size_bytes": file_size,
                "error": "File too large for WebSocket transfer, use DataChannel",
            },
        }

    async def _handle_camera_stream_quality(self, data: dict) -> dict:
        """切换摄像头直播画质"""
        mode = data.get("mode", "high")
        if mode not in ("auto", "high", "medium", "low"):
            return {"type": "error", "data": {"code": 400, "message": f"Invalid quality mode: {mode}"}}

        # 如果是自动模式且没有活跃的 WebRTC 连接，降级为 medium
        webrtc_svc = getattr(self, "_webrtc_service", None)
        dc_client_id = getattr(self, "_dc_client_id", None)

        if webrtc_svc and dc_client_id:
            result = webrtc_svc.set_client_quality(dc_client_id, mode)
            if result:
                # 提取第一个 track 的信息作为响应
                first_track = next(iter(result.get("tracks", {}).values()), None)
                res_info = first_track.get("resolution", {}) if first_track else {}
                return {
                    "type": "camera_stream_quality_ack",
                    "data": {
                        "mode": mode,
                        "resolution": res_info,
                    },
                }

        # 无活跃 WebRTC 连接（通过 WS 路径）
        if mode == "auto":
            mode = "medium"  # 自动模式无连接时默认中画质
        return {
            "type": "camera_stream_quality_ack",
            "data": {"mode": mode, "resolution": {}},
        }

    async def _handle_system(self, data: dict) -> dict:
        """处理系统操作"""
        action = data.get("action")

        if action == "reboot":
            if self._rebooting:
                return {"type": "error", "data": {"code": 429, "message": "Reboot already in progress"}}
            self._rebooting = True
            self.logger.warning("System reboot requested")
            asyncio.create_task(self._delayed_system_command(["sudo", "reboot"], "Reboot", "_rebooting"))
            return {"type": "system_ack", "data": {"action": "reboot", "status": "pending"}}
        elif action == "shutdown":
            if self._shutting_down:
                return {"type": "error", "data": {"code": 429, "message": "Shutdown already in progress"}}
            self._shutting_down = True
            self.logger.warning("System shutdown requested")
            asyncio.create_task(
                self._delayed_system_command(["sudo", "systemctl", "poweroff"], "Shutdown", "_shutting_down")
            )
            return {"type": "system_ack", "data": {"action": "shutdown", "status": "pending"}}
        elif action == "restart_service":
            service_id = data.get("service_id", "main")
            self.logger.info(f"Service restart requested: {service_id}")

            if service_id == "main":
                if self._restarting_service:
                    return {"type": "error", "data": {"code": 429, "message": "Service restart already in progress"}}
                self._restarting_service = True
                self.logger.warning("Main service restart requested via systemd")
                asyncio.create_task(
                    self._delayed_system_command(
                        ["sudo", "systemctl", "restart", "wobot-control"],
                        "Restart service",
                        "_restarting_service",
                    )
                )
                return {
                    "type": "system_ack",
                    "data": {"action": "restart_service", "service_id": "main", "status": "pending"},
                }

            if hasattr(self, "service_manager") and self.service_manager:
                success = await self.service_manager.restart_service(service_id)
                return {
                    "type": "system_ack",
                    "data": {
                        "action": "restart_service",
                        "service_id": service_id,
                        "status": "ok" if success else "failed",
                    },
                }
            return {
                "type": "system_ack",
                "data": {"action": "restart_service", "status": "pending", "service_id": service_id},
            }

        return {"type": "error", "data": {"code": 400, "message": "Invalid system action"}}

    async def _handle_service_status(self, data: dict) -> dict:
        """获取所有子服务状态"""
        if hasattr(self, "service_manager") and self.service_manager:
            services = self.service_manager.get_all_services_status()
            return {"type": "service_status", "data": {"services": services}}
        return {"type": "service_status", "data": {"services": []}}

    async def _handle_service_control(self, data: dict) -> dict:
        """控制子服务启停"""
        service_id = data.get("service_id", "")
        action = data.get("action", "")  # start | stop | restart
        if not service_id or not action:
            return {"type": "error", "data": {"code": 400, "message": "service_id and action required"}}

        if not hasattr(self, "service_manager") or not self.service_manager:
            return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

        if service_id == "main":
            return {
                "type": "service_control_ack",
                "data": {
                    "service_id": "main",
                    "action": action,
                    "status": "main_not_restartable",
                    "message": "主服务不可通过面板控制",
                },
            }

        if action == "start":
            success = await self.service_manager.start_service(service_id)
        elif action == "stop":
            success = await self.service_manager.stop_service(service_id)
        elif action == "restart":
            success = await self.service_manager.restart_service(service_id)
        else:
            return {"type": "error", "data": {"code": 400, "message": f"Unknown action: {action}"}}

        # 操作成功后附带更新后的服务列表，确保前端即时更新状态
        services = self.service_manager.get_all_services_status() if success else []
        return {
            "type": "service_control_ack",
            "data": {
                "service_id": service_id,
                "action": action,
                "status": "ok" if success else "failed",
                "services": services,
            },
        }

    async def _delayed_system_command(self, cmd: list[str], action_name: str, flag_attr: str):
        """通用延迟系统命令执行（防重入保护）"""
        await asyncio.sleep(2)
        try:
            # 强制刷新所有日志 handler，确保日志落盘
            for handler in self.logger.handlers:
                handler.flush()
            subprocess.run(cmd, check=False)
        except Exception as e:
            self.logger.error(f"{action_name} failed: {e}", exc_info=True)
        finally:
            setattr(self, flag_attr, False)

    async def _handle_exec(self, data: dict) -> dict:
        """执行命令（持久 Shell 会话，保持工作目录）"""
        command = data.get("command", "").strip()
        timeout = data.get("timeout", 5000) / 1000

        if command:
            self.logger.warning(f"远程命令执行: {command} (cwd={self._shell_cwd}, timeout={timeout:.1f}s)")

        if not command:
            return {"type": "exec_result", "data": {"stdout": "", "stderr": "", "return_code": 0}}

        # 特殊处理 cd —— 直接修改会话工作目录
        cmd_parts = command.split(maxsplit=1)
        if cmd_parts[0] == "cd":
            target = cmd_parts[1] if len(cmd_parts) > 1 else os.path.expanduser("~")
            # 展开 ~ / ~user 等
            target = os.path.expanduser(target)
            try:
                new_cwd = os.path.abspath(os.path.join(self._shell_cwd, target))
                if os.path.isdir(new_cwd):
                    self._shell_cwd = new_cwd
                    return {
                        "type": "exec_result",
                        "data": {
                            "stdout": f"已切换到 {self._shell_cwd}",
                            "stderr": "",
                            "return_code": 0,
                            "cwd": self._shell_cwd,
                        },
                    }
                else:
                    return {
                        "type": "exec_result",
                        "data": {"stdout": "", "stderr": f"cd: {target}: No such file or directory", "return_code": 1},
                    }
            except Exception as e:
                return {"type": "exec_result", "data": {"stdout": "", "stderr": str(e), "return_code": 1}}

        # export 环境变量 —— 保留在会话中（此处简化：原样传给 shell）
        # 其余命令在持久 CWD 下执行（使用 PTY 模拟真实终端，ls 等命令会多列输出）
        try:
            master_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                f"cd {self._shell_cwd} && {command}",
                shell=True,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                cwd=self._shell_cwd,
            )
            os.close(slave_fd)
            output = b""
            while True:
                ready, _, _ = select.select([master_fd], [], [], timeout)
                if ready:
                    try:
                        chunk = os.read(master_fd, 4096)
                        if not chunk:
                            break
                        output += chunk
                    except OSError:
                        break
                else:
                    break
            os.close(master_fd)
            proc.wait()
            # 移除 ANSI 转义序列（可选：保留颜色需要前端支持）
            output_text = output.decode("utf-8", errors="replace")
            # 去除末尾的 \r\n 序列带来的重复换行
            output_text = re.sub(r"\r\n", "\n", output_text)
            return {
                "type": "exec_result",
                "data": {
                    "stdout": output_text,
                    "stderr": "",
                    "return_code": proc.returncode,
                    "cwd": self._shell_cwd,
                },
            }
        except subprocess.TimeoutExpired:
            return {"type": "exec_result", "data": {"stdout": "", "stderr": "Command timeout", "return_code": -1}}
        except Exception as e:
            return {"type": "exec_result", "data": {"stdout": "", "stderr": str(e), "return_code": 1}}

    async def _handle_logs(self, data: dict) -> dict:
        """获取日志（基于行号游标的增量读取）

        请求参数:
          mode: "tail" (默认) 取最新 N 条 | "since" 取 since_line 之后的行
          since_line: since 模式下的起始行号（0-based，不含该行）
          limit: 最多返回条数（默认 200）
          level: 级别过滤（"" 表示不过滤）

        响应:
          logs: 日志条目数组（每条带 line_no 字段）
          total_lines: 服务端日志文件当前总行数
          has_more: since 模式下是否还有更多未读日志
          next_since: 下次 since 请求的起始行号
        """
        mode = data.get("mode", "tail")
        since_line = int(data.get("since_line", 0))
        before_line = int(data.get("before_line", 0))
        limit = int(data.get("limit", 200))
        level = data.get("level", "")

        log_file_path = self.config.get("logging", {}).get("file", "logs/wobot.log")
        log_file = Path(log_file_path)
        if not log_file.is_absolute():
            # 与 setup_logger 保持一致：相对路径基于 cwd 解析
            log_file = Path.cwd() / log_file_path
        logs = []
        total_lines = 0
        has_more = False
        next_since = 0

        if log_file.exists():
            with open(log_file, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            total_lines = len(all_lines)

            if mode == "since" and since_line > 0:
                # 增量模式：取 since_line 之后的行
                start = since_line
                end = min(start + limit, total_lines)
                recent = all_lines[start:end]
                has_more = end < total_lines
                next_since = end
            elif mode == "before" and before_line > 0:
                # 向上加载历史：取 before_line 之前的 limit 条
                start = max(0, before_line - limit)
                end = before_line
                recent = all_lines[start:end]
                has_more = start > 0
                next_since = total_lines  # 不改变增量游标
            else:
                # tail 模式：取最新 limit 条
                start = max(0, total_lines - limit)
                recent = all_lines[start:total_lines]
                has_more = False
                next_since = total_lines

            # 新格式: ts [LEVEL] [logger_name] module: message
            pattern_new = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] \[([\w.]+)\] (\w+): (.+)$")
            # 旧格式兼容: ts [LEVEL] logger_name: message
            pattern_old = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] (\w+): (.+)$")
            for i, raw in enumerate(recent):
                line_no = start + i
                raw = raw.strip()
                m = pattern_new.match(raw)
                if m:
                    ts, lvl, logger_name, _module, msg = m.groups()
                    source = logger_name
                else:
                    m = pattern_old.match(raw)
                    if m:
                        ts, lvl, source, msg = m.groups()
                    else:
                        continue
                if level and level.lower() != lvl.lower():
                    continue
                logs.append(
                    {
                        "line_no": line_no,
                        "timestamp": ts,
                        "level": lvl,
                        "source": source,
                        "message": msg,
                    }
                )

        return {
            "type": "logs",
            "data": {
                "logs": logs,
                "total_lines": total_lines,
                "has_more": has_more,
                "next_since": next_since,
                "mode": mode,
            },
        }

    async def _handle_software_list(self, data: dict) -> dict:
        """获取软件列表（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command("software_manager", "list", data)
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    async def _handle_software_available(self, data: dict) -> dict:
        """获取白名单内未安装软件列表（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command("software_manager", "available", data)
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    async def _handle_software_install(self, data: dict) -> dict:
        """安装软件包（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command(
                "software_manager", "install", data, timeout=120.0
            )
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    async def _handle_software_check_updates(self, data: dict) -> dict:
        """检查软件更新（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command("software_manager", "check_updates", data)
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    async def _handle_module_list(self, data: dict) -> dict:
        """获取模块列表（动态扫描）"""
        modules = [
            {
                "id": "motion",
                "name": "运动控制",
                "version": "1.0.0",
                "status": "running" if self.motion_controller else "disabled",
                "enabled": self.motion_controller is not None,
            },
            {
                "id": "vision",
                "name": "视觉模块",
                "version": "1.0.0",
                "status": "running" if self.camera_manager else "disabled",
                "enabled": self.camera_manager is not None,
            },
            {
                "id": "system",
                "name": "系统信息",
                "version": "1.0.0",
                "status": "running" if self.system_collector else "disabled",
                "enabled": self.system_collector is not None,
            },
        ]
        # 尝试导入 extension 模块管理器
        try:
            from modules.extension.base import ModuleManager

            mgr = ModuleManager()
            mods = mgr.list_modules()
            for m in mods:
                modules.append(
                    {
                        "id": m.get("id", ""),
                        "name": m.get("name", ""),
                        "version": m.get("version", "1.0.0"),
                        "status": m.get("status", "unknown"),
                        "enabled": m.get("enabled", False),
                    }
                )
        except Exception:
            pass
        return {"type": "module_list", "data": {"modules": modules}}

    async def _handle_module_control(self, data: dict) -> dict:
        """模块控制"""
        module_id = data.get("module_id")
        action = data.get("action")

        self.logger.info(f"Module control: {module_id} -> {action}")
        return {"type": "module_control_ack", "data": {"module_id": module_id, "action": action, "status": "ok"}}

    async def _handle_device_control(self, data: dict) -> dict:
        """处理设备控制（寻找设备/手电/充电/静音/省电等）"""
        action = data.get("action", "")
        enabled = data.get("enabled", True)

        # 省电模式通过 PowerPolicy 处理
        if action == "eco":
            return await self._handle_power_policy_toggle(data)

        # 寻找设备：触发/停止声光提示
        if action in ("find_device", "find"):
            return await self._handle_find_device(enabled)

        # T017 一键静音：硬件混音器层静音所有音频输出
        if action == "mute":
            return await self._handle_audio_mute(enabled)

        # 其余动作尚未实现（如手电/去充电）：明确回执 unsupported，避免前端误判成功
        self.logger.warning(f"Unsupported device control: {action} -> {'ON' if enabled else 'OFF'}")
        return {
            "type": "device_control_ack",
            "data": {
                "action": action,
                "enabled": enabled,
                "status": "unsupported",
                "message": f"设备动作 '{action}' 尚未实现",
            },
        }

    async def _handle_find_device(self, enabled: bool) -> dict:
        """处理寻找设备（声光提示）"""
        if not hasattr(self, "find_device_controller") or not self.find_device_controller:
            return {"type": "error", "data": {"code": 503, "message": "Find device not available"}}

        if enabled:
            status = await self.find_device_controller.start_find()
        else:
            status = await self.find_device_controller.stop_find()
        self.logger.info(f"Find device: {'ON' if enabled else 'OFF'} (active={status.get('active')})")
        return {
            "type": "device_control_ack",
            "data": {
                "action": "find_device",
                "enabled": status.get("active", False),
                "status": "ok",
                **status,
            },
        }

    # ---------- 一键静音 (T017) ----------

    async def _handle_audio_mute(self, enabled: bool) -> dict:
        """T017 一键静音：静音/恢复所有音频输出（硬件混音器层）"""
        if not hasattr(self, "audio_output") or not self.audio_output:
            return {"type": "error", "data": {"code": 503, "message": "Audio output not available"}}

        if not self.audio_output.is_available():
            return {
                "type": "error",
                "data": {"code": 501, "message": "本设备不支持一键静音（缺少音频硬件控制）"},
            }

        state = await self.audio_output.set_mute(bool(enabled))
        if state.get("error"):
            self.logger.warning(f"Audio mute partial failure: {state.get('error')}")
        # 无论成功与否都广播真实状态，供前端纠正乐观 UI
        await self._broadcast_audio_mute(state)
        return {"type": "audio_mute_status", "data": state}

    async def _handle_audio_mute_status(self, data: dict) -> dict:
        """查询一键静音当前状态"""
        if not hasattr(self, "audio_output") or not self.audio_output:
            return {"type": "error", "data": {"code": 503, "message": "Audio output not available"}}
        return {"type": "audio_mute_status", "data": self.audio_output.get_state()}

    async def _broadcast_audio_mute(self, state: dict) -> None:
        """广播静音状态给所有客户端"""
        if hasattr(self, "ws_server") and self.ws_server:
            await self.ws_server.broadcast_message({"type": "audio_mute_status", "data": state})

    # ---------- 导航 S0：运行模式与里程计 ----------

    async def _broadcast_control_mode(self) -> None:
        """广播运行模式状态（含安全降级原因 —— 降级绝不允许静默）"""
        if self.mode_manager is None:
            return
        if hasattr(self, "ws_server") and self.ws_server:
            await self.ws_server.broadcast_message(
                {"type": "control_mode_status", "data": self.mode_manager.get_status()}
            )

    async def _handle_control_mode_set(self, data: dict) -> dict:
        """切换运行模式：manual / semi / auto

        安全要点：
          - 未绑定的客户端在 websocket_server 层已被拦下（AUTH_ALLOWED_TYPES 默认拒绝）
          - semi/auto 需配置允许 + 二次确认，防止误触让机器人自主运动
          - **切回 manual 必须立即停车**（让权即停），这是 S0 的硬性验收项
        """
        if self.mode_manager is None:
            return {"type": "control_mode_ack", "data": {"ok": False, "message": "模式管理未启用"}}

        payload = data or {}
        result = self.mode_manager.request(
            payload.get("mode"),
            confirmed=bool(payload.get("confirmed", False)),
            reason=payload.get("reason"),
        )

        if result.get("ok"):
            # 让权即停：回到手动后不再保持之前的运动指令
            if self.mode_manager.is_manual() and self.motion_controller:
                try:
                    await self.motion_controller.stop()
                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"切回 manual 时停车失败: {e}")
            await self._broadcast_control_mode()

        return {"type": "control_mode_ack", "data": result}

    async def _handle_control_mode_get(self, data: dict) -> dict:
        """查询当前运行模式与降级状态"""
        if self.mode_manager is None:
            return {"type": "control_mode_status", "data": {"mode": "manual", "available": False}}
        status = dict(self.mode_manager.get_status())
        status["available"] = True
        return {"type": "control_mode_status", "data": status}

    async def _handle_odometry_get(self, data: dict) -> dict:
        """查询位姿与 IMU（单次）"""
        if self.odometry is None:
            return {"type": "odometry_status", "data": {"available": False}}
        return {
            "type": "odometry_status",
            "data": {
                "available": self.odometry.available,
                "pose": self.odometry.get_pose(),
                "imu": self.odometry.get_imu(),
                "status": self.odometry.get_status(),
            },
        }

    async def _handle_odometry_reset(self, data: dict) -> dict:
        """重置位姿原点（标定与调试用）"""
        if self.odometry is None:
            return {"type": "odometry_reset_ack", "data": {"ok": False, "message": "里程计不可用"}}
        payload = data or {}
        self.odometry.reset(
            float(payload.get("x", 0.0) or 0.0),
            float(payload.get("y", 0.0) or 0.0),
            float(payload.get("theta", 0.0) or 0.0),
        )
        return {"type": "odometry_reset_ack", "data": {"ok": True, "pose": self.odometry.get_pose()}}

    async def _ensure_perception_camera(self) -> bool:
        """确保感知摄像头有帧 —— 需要时由导航层自己启动流。

        为什么必须自己启动：自主巡航不应依赖"用户打开了遥控界面"。
        原先感知直接读 camera_manager.get_frame()，而流只有 UI 订阅才会启动，
        于是无人开界面时感知永远拿不到帧，巡航根本无法工作。
        """
        if self.camera_manager is None:
            return False
        if self.camera_manager.get_frame(self._perception_camera_id) is not None:
            return True

        now = time.monotonic()
        if now - self._cam_start_ts < 5.0:  # 失败重试冷却，避免刷屏
            return False
        self._cam_start_ts = now
        try:
            await self.camera_manager.start_stream(self._perception_camera_id)
            if self.logger:
                self.logger.info("[Nav] 自主模式：已自动启动摄像头 %s 供感知使用", self._perception_camera_id)
            return True
        except Exception as e:
            if self.logger and not self._cam_start_warned:
                self._cam_start_warned = True
                self.logger.warning("[Nav] 自动启动摄像头失败（将重试）: %s", e)
            return False

    def _update_perception(self) -> PerceptionResult | None:
        """跑一次视觉感知并把可信度上报给安全层（S2）。

        fail-safe 语义：拿不到帧 → 上报不可信（原因"摄像头未开启"），
        由安全层在**自主模式**下按"有障碍"处理，而不是闷头往前开。
        """
        src = self.perception
        if src is None:
            return None
        try:
            frame = self.camera_manager.get_frame(self._perception_camera_id) if self.camera_manager else None
        except Exception:
            frame = None

        if frame is None:
            src.reset()
            if self.safety is not None:
                self.safety.report_perception(False, "无视频帧（摄像头未开启或流未启动）")
            self._last_perception = {"ok": False, "reason": "无视频帧", "source": src.name}
            return None

        gyro = None
        if self.odometry is not None:
            g = (self.odometry.get_imu() or {}).get("gyro") or {}
            if g:
                gyro = (float(g.get("x", 0.0)), float(g.get("y", 0.0)), float(g.get("z", 0.0)))

        res = src.process(frame, gyro=gyro, now=time.monotonic())
        if self.safety is not None:
            self.safety.report_perception(res.ok, res.reason)
        self._last_perception = res.to_dict()
        return res

    async def _motion_probe(self) -> bool:
        """调试用运动探针：文件存在时强制持续直行，并记录编码器增量。

        为什么需要：验证"自主模式能否真的驱动轮子"时，若受感知/暗光影响会一直停车，
        永远发不出前进指令。探针绕过感知，直接发固定速度，用编码器增量给出铁证。
        用法：echo 0.4 > /tmp/wobot_motion_probe   （内容为速度）
        """
        if self.motion_controller is None:
            return False
        try:
            if not os.path.exists(self._probe_file):
                if self._probe_active:
                    self._probe_active = False
                    self.logger.warning("[Probe] 运动探针结束")
                return False
            with open(self._probe_file, encoding="utf-8") as fp:
                raw = (fp.read() or "0.3").strip()
        except Exception:
            return False
        # 支持 "vx" 或 "vx,vy,vz"（用逗号分隔），便于逐个方向探测轮子响应
        parts = [x.strip() for x in raw.split(",")]
        try:
            vx = float(parts[0]) if parts and parts[0] else 0.0
            vy = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
            vz = float(parts[2]) if len(parts) > 2 and parts[2] else 0.0
        except ValueError:
            return False
        vx = max(-1.0, min(1.0, vx))
        vy = max(-1.0, min(1.0, vy))
        vz = max(-5.0, min(5.0, vz))
        speed = vx
        # 单轮直驱模式：文件内容 "wheel:<名称>,<速度>"，用于逐个轮子排查硬件
        single_wheel = None
        if raw.startswith("wheel:"):
            spec = raw.split(":", 1)[1]
            nm, _, sp = spec.partition(",")
            single_wheel = nm.strip()
            try:
                speed = max(-1.0, min(1.0, float(sp or "0.5")))
            except ValueError:
                speed = 0.5
        # ⚠️ 必须自己采样一次：探针走的是 `continue` 分支，会跳过主循环的 odometry.sample()，
        # 否则 get_raw_encoders() 读到的是**陈旧值**，看起来"没动"（曾因此得出错误结论）。
        if self.odometry is not None:
            try:
                self.odometry.sample()
            except Exception:
                pass
        # 四轮直驱模式（绕过麦轮分解，用于单轮排障）：
        # 文件内容 "motors:50,0,0,0" → 底层 Rosmaster.set_motor(s1,s2,s3,s4)
        # 注意：硬件抽象层不支持单轮（会打印 warning 后忽略），必须走底层 bot。
        motors = None
        if raw.startswith("motors:"):
            try:
                vals = [float(x) for x in raw.split(":", 1)[1].split(",")[:4]]
                while len(vals) < 4:
                    vals.append(0.0)
                motors = vals
            except ValueError:
                motors = None

        if not self._probe_active:
            self._probe_active = True
            self._probe_enc0 = self.odometry.get_raw_encoders() if self.odometry else None
            self._probe_t0 = time.monotonic()
            if motors is not None:
                self.logger.warning("[Probe] 四轮直驱：%s", motors)
            elif single_wheel:
                self.logger.warning("[Probe] 单轮驱动：%s speed=%.2f", single_wheel, speed)
            else:
                self.logger.warning("[Probe] 运动探针开始：vx=%.2f vy=%.2f vz=%.2f", vx, vy, vz)
        try:
            if motors is not None:
                bot = getattr(self.motion_controller.hardware, "_bot", None)
                if bot is not None and hasattr(bot, "set_motor"):
                    bot.set_motor(motors[0], motors[1], motors[2], motors[3])
                elif self.logger:
                    self.logger.warning("[Probe] 底层 bot 不可用，无法四轮直驱")
            elif single_wheel:
                await self.motion_controller.hardware.set_motor(single_wheel, speed)
            else:
                await self.motion_controller.set_mecanum_velocity(vx, vy, vz, "manual")
        except Exception as e:
            self.logger.warning(f"[Probe] 发送失败: {e}")
        # 每 2 秒报一次编码器增量
        now = time.monotonic()
        if now - getattr(self, "_probe_last_log", 0.0) >= 2.0:
            self._probe_last_log = now
            enc = self.odometry.get_raw_encoders() if self.odometry else None
            if enc and self._probe_enc0:
                d = [enc[k] - self._probe_enc0[k] for k in ("m1", "m2", "m3", "m4")]
                self.logger.warning("[Probe] t=%.1fs 编码器增量=%s", now - self._probe_t0, d)
        return True

    def _check_debug_mode_override(self) -> None:
        """调试用：通过文件切换运行模式。

        为什么需要：验证自主模式要反复切换，不该每次都让人点界面。
        安全性：只读**本机文件**，不新增任何网络接口；文件不存在则完全不生效。
        用法：echo semi > /tmp/wobot_mode
        """
        if self.mode_manager is None:
            return
        try:
            if not os.path.exists(self._debug_mode_file):
                return
            with open(self._debug_mode_file, encoding="utf-8") as fp:
                want = (fp.read() or "").strip().lower()
        except Exception:
            return
        if want not in ("manual", "semi", "auto") or want == self.mode_manager.mode:
            return
        res = self.mode_manager.request(want, confirmed=True, reason="调试覆盖文件")
        if self.logger:
            self.logger.info("[Debug] 模式覆盖文件 → %s（%s）", want, res.get("message"))

    def _hw_checks(self) -> list:
        """主服务独占的硬件检查（串口/摄像头/云台/IMU）。

        为什么放在主服务：这些设备只有主服务能读（串口与摄像头被独占），
        诊断子服务读不到。由主服务算好后**合并进诊断报告**再广播，
        避免两个服务互相轮询形成循环依赖。
        """
        checks: list[dict] = []
        # 配置在函数开头取一次（曾在 odometry 分支内定义，导致无 odometry 时 NameError）
        nav_cfg = (self.config or {}).get("navigation", {}) or {}
        # ---- 四轮（编码器反馈）----
        # 逐轮输出：只坏一个轮子时，面板应只标那一轮，而不是整项报异常。
        # ⚠️ 采样节奏：本方法每 2 秒调用一次，因此判定窗口必须 > 2 秒，
        # 否则窗口内永远只有 1 个样本 → 状态停在 unknown → 误报"全部异常"。
        if self.odometry is not None:
            try:
                enc = self.odometry.get_raw_encoders()
                from modules.navigation.wheel_health import WheelHealthMonitor

                # 通用性：该驱动类型"应有"的轮子 vs 实际读到的编码器 —— 不一致本身就是诊断信息
                expected = getattr(self.motion_controller, "wheel_names", ()) or ()
                enc_keys = [k for k in enc if k != "changed"]
                if expected and len(enc_keys) != len(expected):
                    checks.append(
                        {
                            "name": "轮子配置",
                            "kind": "chassis",
                            "status": "warn",
                            "failure_class": "cfg",
                            "detail": (
                                f"驱动类型 {getattr(self.motion_controller, 'drive_type', '?')} "
                                f"应有 {len(expected)} 个轮子，实际读到 {len(enc_keys)} 个编码器"
                            ),
                            "data": {"expected": list(expected), "encoders": enc_keys},
                        }
                    )
                # 逻辑轮名（与编码器数量一致时用于展示，否则退回编码器键名）
                if expected and len(expected) == len(enc_keys):
                    display = [f"{expected[i]}（{enc_keys[i]}）" for i in range(len(enc_keys))]
                else:
                    display = list(enc_keys)

                # ⚠️ 轮子列表必须**每次**都算出来（曾在"首次创建"分支里算，
                # 导致后续调用 NameError: wheel_names referenced before assignment）
                wheel_names = list(
                    (nav_cfg.get("odometry", {}) or {}).get("wheels")
                    or getattr(self.odometry, "wheel_names", None)
                    or enc.keys()
                )
                if self._wheel_monitor is None:
                    self._wheel_monitor = WheelHealthMonitor(
                        {"window_s": 3.0, "cmd_threshold": 0.05, "move_threshold": 30, "wheels": wheel_names},
                        self.logger,
                    )
                cmd = (
                    getattr(self.motion_controller, "current_linear", 0.0) or 0.0,
                    0.0,
                    getattr(self.motion_controller, "current_angular", 0.0) or 0.0,
                )
                st = self._wheel_monitor.update(cmd, enc)
                for idx, w in enumerate(wheel_names, start=1):
                    label = display[idx - 1] if idx - 1 < len(display) else f"轮子 {idx}（{w}）"
                    state = st["states"].get(w, "unknown")
                    delta = st["deltas"].get(w, 0)
                    if state == "ok":
                        c_status, c_class = "ok", None
                        c_detail = f"正常（Δ{delta}）"
                    elif state in ("stall", "frozen", "erratic"):
                        c_status = "fail"
                        c_class = "link" if state == "frozen" else "hw"
                        reason = {
                            "stall": "有指令但无位移（卡滞/电机/驱动）",
                            "frozen": "数据冻结（上报链路）",
                            "erratic": "计数跳变（编码器信号）",
                        }.get(state, state)
                        c_detail = f"{reason}，Δ{delta}"
                    else:
                        # 数据不足（如刚启动、或长时间静止）—— 不能判为故障
                        c_status, c_class = "warn", None
                        c_detail = f"数据不足（Δ{delta}），无运动指令时属正常"
                    checks.append(
                        {
                            "name": label,
                            "kind": "motor",
                            "status": c_status,
                            "failure_class": c_class,
                            "detail": c_detail,
                            "data": {"delta": delta, "state": state},
                        }
                    )
            except Exception as e:
                checks.append(
                    {
                        "name": "轮子/编码器",
                        "kind": "motor",
                        "status": "fail",
                        "failure_class": "link",
                        "detail": f"检查失败: {e}",
                        "data": {},
                    }
                )
        else:
            checks.append(
                {
                    "name": "轮子/编码器",
                    "kind": "motor",
                    "status": "fail",
                    "failure_class": "link",
                    "detail": "无里程计来源",
                    "data": {},
                }
            )

        # ---- 摄像头（支持多路：camera_ids 可配置；默认只查感知用的那一路）----
        # 通用性：不同机型摄像头数量不同，不能假定只有一路
        cam_ids = (nav_cfg.get("perception", {}) or {}).get("camera_ids") or [self._perception_camera_id]
        cam_reported = 0
        for cid in cam_ids:
            if self.camera_manager is None:
                checks.append(
                    {
                        "name": f"摄像头 {cid}",
                        "kind": "camera",
                        "status": "fail",
                        "failure_class": "cfg",
                        "detail": "无摄像头管理器",
                        "data": {"camera_id": cid},
                    }
                )
                continue
            try:
                frame = self.camera_manager.get_frame(cid)
                if frame is not None:
                    cw, ch = (frame.shape[1], frame.shape[0]) if hasattr(frame, "shape") else (0, 0)
                    checks.append(
                        {
                            "name": f"摄像头 {cid}",
                            "kind": "camera",
                            "status": "ok",
                            "failure_class": None,
                            "detail": f"有帧 {cw}x{ch}",
                            "data": {"camera_id": cid},
                        }
                    )
                    cam_reported += 1
                else:
                    checks.append(
                        {
                            "name": f"摄像头 {cid}",
                            "kind": "camera",
                            "status": "fail",
                            "failure_class": "cfg",
                            "detail": "无帧（未启动/设备被占用/已掉线）",
                            "data": {"camera_id": cid},
                        }
                    )
            except Exception as e:
                checks.append(
                    {
                        "name": f"摄像头 {cid}",
                        "kind": "camera",
                        "status": "fail",
                        "failure_class": "link",
                        "detail": f"检查失败: {e}",
                        "data": {"camera_id": cid},
                    }
                )
        if cam_reported == 0 and cam_ids:
            pass  # 已在上面逐路记录失败，不再重复报

        # ---- 云台 ----
        if self.gimbal_controller is not None and hasattr(self.gimbal_controller, "set_pan"):
            checks.append(
                {
                    "name": "云台",
                    "kind": "gimbal",
                    "status": "ok",
                    "failure_class": None,
                    "detail": "set_pan 可用",
                    "data": {},
                }
            )
        else:
            checks.append(
                {
                    "name": "云台",
                    "kind": "gimbal",
                    "status": "fail",
                    "failure_class": "cfg",
                    "detail": "无云台控制器或缺少 set_pan",
                    "data": {},
                }
            )

        # ---- IMU（读数全 0 = 无该硬件）----
        imu_detail = "无里程计来源"
        imu_status: str = "fail"
        imu_class: str | None = "link"
        if self.odometry is not None:
            try:
                imu = self.odometry.get_imu() or {}
                g, acc = imu.get("gyro") or {}, imu.get("accel") or {}
                mag = sum(abs(float(g.get(k, 0.0) or 0.0)) for k in ("x", "y", "z"))
                mag += sum(abs(float(acc.get(k, 0.0) or 0.0)) for k in ("x", "y", "z"))
                if mag <= 1e-9:
                    imu_detail, imu_class = "读数为全 0（疑似无陀螺仪硬件）", "hw"
                else:
                    imu_status, imu_class, imu_detail = "ok", None, f"读数合计 {mag:.4f}"
            except Exception as e:
                imu_detail = f"检查失败: {e}"
        checks.append(
            {
                "name": "IMU",
                "kind": "imu",
                "status": imu_status,
                "failure_class": imu_class,
                "detail": imu_detail,
                "data": {},
            }
        )

        # ---- 电池（读数可信性）----
        # 阈值来自配置：不同机型电芯数不同，不能写死 10.5V
        batt_cfg = (self.config or {}).get("battery", {}) or {}
        _batt_min = float(batt_cfg.get("min_voltage", 10.5))
        if not batt_cfg.get("enabled", True):
            checks.append(
                {
                    "name": "电池",
                    "kind": "battery",
                    "status": "ok",
                    "failure_class": None,
                    "detail": "该机型未配置电池检测（不适用）",
                    "data": {},
                }
            )
            return checks
        v = self._read_battery_voltage()
        if v is None:
            checks.append(
                {
                    "name": "电池",
                    "kind": "battery",
                    "status": "warn",
                    "failure_class": "link",
                    "detail": "读不到电压（传感器/上报链路）",
                    "data": {},
                }
            )
        elif v < _batt_min:
            # 关键启发式：电压读数偏低但系统仍在正常运行 → 读数本身可疑
            checks.append(
                {
                    "name": "电池",
                    "kind": "battery",
                    "status": "warn",
                    "failure_class": "link",
                    "detail": f"读数 {v:.2f}V 偏低，但系统正常运行 → 读数可能不可信（若充电器常连更可疑）",
                    "data": {"voltage": round(v, 2)},
                }
            )
        else:
            checks.append(
                {
                    "name": "电池",
                    "kind": "battery",
                    "status": "ok",
                    "failure_class": None,
                    "detail": f"{v:.2f}V",
                    "data": {"voltage": round(v, 2)},
                }
            )
        # ---- 外设（从 PeripheralRegistry **动态枚举**）----
        # 通用性：不同机型外设完全不同。这里"有什么查什么"：
        #   provider == "none" → 该机型没配这个外设（不适用，不是故障）
        #   已配置 → 检查是否真的拿到数据
        reg = None
        if self.system_collector is not None:
            reg = getattr(self.system_collector, "_peripheral_registry", None)
        if reg is None:
            checks.append(
                {
                    "name": "外设",
                    "kind": "peripheral",
                    "status": "ok",
                    "failure_class": None,
                    "detail": "未启用外设注册表（该机型无扩展外设）",
                    "data": {},
                }
            )
        else:
            try:
                slots = dict(getattr(reg, "slots", {}) or {})
                configured = {n: c for n, c in slots.items() if (c or {}).get("provider", "none") != "none"}
                if not configured:
                    checks.append(
                        {
                            "name": "外设",
                            "kind": "peripheral",
                            "status": "ok",
                            "failure_class": None,
                            "detail": f"共 {len(slots)} 个槽位均未配置（该机型无扩展外设）",
                            "data": {"slots": list(slots)},
                        }
                    )
                else:
                    status = {}
                    try:
                        status = dict(reg.get_status() or {})
                    except Exception:
                        status = {}
                    for nm, cfgv in configured.items():
                        st = status.get(nm) or {}
                        has_data = bool(st) and any(k in st for k in ("value", "data", "raw"))
                        checks.append(
                            {
                                "name": f"外设 {nm}",
                                "kind": "peripheral",
                                "status": "ok" if has_data else "warn",
                                "failure_class": None if has_data else "link",
                                "detail": (
                                    f"provider={cfgv.get('provider')}，有数据"
                                    if has_data
                                    else f"provider={cfgv.get('provider')}，暂无数据（可能未采样）"
                                ),
                                "data": {"provider": cfgv.get("provider")},
                            }
                        )
            except Exception as e:
                checks.append(
                    {
                        "name": "外设",
                        "kind": "peripheral",
                        "status": "fail",
                        "failure_class": "link",
                        "detail": f"枚举失败: {e}",
                        "data": {},
                    }
                )
        return checks

    async def _periodic_diagnostics(self, interval: float = 2.0) -> None:
        """定期把**独立诊断子服务**的报告通过 WS 广播出去。

        为什么在服务端取、用 WS 推：
          1. 本项目的约定是 WebSocket 消息（前端不做 REST 轮询）
          2. 从服务端取可避免浏览器跨源/CORS 问题
          3. 诊断服务独立运行，主服务只是"转发"它的结论
        """

        def _fetch() -> dict | None:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8090/diagnostics", timeout=3) as resp:
                    return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
            except Exception:
                return None

        loop = asyncio.get_event_loop()
        while True:
            try:
                rep = await loop.run_in_executor(None, _fetch)
                # 把主服务独占的硬件检查合并进去（诊断服务读不到这些设备）
                try:
                    hw = self._hw_checks()
                except Exception:
                    hw = []
                if rep is None:
                    rep = {"overall": "warn", "counts": {}, "checks": []}
                if isinstance(rep, dict):
                    checks = list(rep.get("checks") or []) + hw
                    rep["checks"] = checks
                    st = [c.get("status") for c in checks]
                    rep["counts"] = {
                        "ok": st.count("ok"),
                        "warn": st.count("warn"),
                        "fail": st.count("fail"),
                    }
                    rep["overall"] = "fail" if "fail" in st else ("warn" if "warn" in st else "ok")
                if hasattr(self, "ws_server") and self.ws_server:
                    await self.ws_server.broadcast_message(
                        {
                            "type": "hw_diagnostics",
                            "data": rep if rep is not None else {"overall": "fail", "error": "诊断服务不可达"},
                        }
                    )
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"[Diag] 广播失败: {e}")
            await asyncio.sleep(interval)

    def _read_battery_voltage(self) -> float | None:
        """尽力读取电池电压（读不到返回 None，安全检查会跳过该项）"""
        c = self.system_collector
        if c is None:
            return None
        for name in ("get_latest_battery_voltage", "latest_battery_voltage"):
            fn = getattr(c, name, None)
            if callable(fn):
                try:
                    v = float(fn())
                    if v > 0:
                        return v
                except Exception:
                    pass
        fn = getattr(c, "_read_rosmaster_battery_voltage", None)
        if callable(fn):
            try:
                v = float(fn())
                if v > 0:
                    return v
            except Exception:
                pass
        return None

    async def _periodic_navigation(self) -> None:
        """S0/S1 周期任务：~25Hz 采样里程计并做安全检查，~5Hz 广播位姿。

        为什么采样与广播分开：编码器是**累计计数**，dt 直接参与积分，
        采样必须保持稳定节奏；而前端展示 5Hz 足够，避免把 WebSocket 刷爆。
        安全检查每轮都跑（安全不能等 200ms 的广播周期）。
        """
        if self.odometry is None or not self.odometry.available:
            if self.logger:
                self.logger.info("[Nav] 里程计不可用（未共享到 Rosmaster bot），跳过后台采样")
            return

        sample_interval = 0.04  # ~25Hz，与扩展板自动上报刷新率一致
        push_every = 5  # 每 5 次采样推送一次 → ~5Hz
        n = 0
        degraded_reported = False
        if self.logger:
            self.logger.info("[Nav] 里程计采样任务启动（~25Hz，广播 ~5Hz；安全监管已启用）")

        while True:
            try:
                # 0) 调试：允许通过文件切换模式（我用于自主验证，不需要人工点界面）
                self._check_debug_mode_override()

                # 0.5) 调试：运动探针优先，绕过感知直接验证运动链路
                if await self._motion_probe():
                    await asyncio.sleep(0.04)
                    continue

                # 1) 心跳：证明导航/决策链还活着（看门狗判据）
                if self.safety is not None:
                    self.safety.heartbeat()

                # 2) 采样
                self.odometry.sample()

                # 3) S2 感知：**仅在自主模式下**运行。
                #    manual 时避障层只保留急停（用户自己看路），不跑视觉，
                #    既省算力，也避免"暗光就锁死手动驾驶"。
                if self.perception is not None and self.mode_manager is not None and self.mode_manager.is_autonomous():
                    await self._ensure_perception_camera()
                    perc = self._update_perception()

                    # V3 云台主动扫视：转云台 → 停稳 → 测量 → 记录方位图
                    scan_hint = None
                    if self.scanner is not None and (
                        self.patrol is None or self.patrol.get_status()["state"] in ("observe", "stop")
                    ):
                        act = self.scanner.tick(time.monotonic())
                        if act is not None:
                            if act.get("action") == "measure":
                                # 停稳后测一次该方向（转动中 TTC 被判据门控暂停，必须停稳再测）
                                self.scanner.record(self._update_perception())
                            elif self.gimbal_controller is not None:
                                # 注意方法名：GimbalController 是 set_pan/set_tilt
                                # （set_angles 只存在于硬件后端类，曾因此云台完全不动）
                                try:
                                    await self.gimbal_controller.set_pan(act["pan"])
                                except Exception as e:
                                    if self.logger:
                                        self.logger.warning(f"[Scan] 云台指令失败: {e}")
                        scan_hint = self.scanner.best_offset()

                    # 感知 → 决策 → 运动
                    # 优先用巡逻控制器（走一段→停下观察→再走）；没有则退回连续行进
                    if self.patrol is not None and self.motion_controller is not None:
                        pstate = self.patrol.get_status()["state"]
                        # 只在停下观察/停车时才扫视（行进中扫视既无用也浪费）
                        if self.scanner is not None and pstate in ("observe", "stop"):
                            scan_hint = self.scanner.best_offset()
                        cmd = self.patrol.step(
                            perc,
                            heading=(self.odometry.get_pose().get("theta") if self.odometry else None),
                            openness=(self.scanner.openness() if self.scanner else None),
                            scan_rounds=(self.scanner.rounds if self.scanner else None),
                        )
                        cmd["action"] = cmd.get("state", "-")
                        self._last_cruise = cmd
                    elif self.cruise is not None and self.motion_controller is not None:
                        cmd = self.cruise.step(perc, hint_offset=scan_hint)
                        self._last_cruise = cmd
                        try:
                            # 记录"实际发出的指令"：之前只有状态机意图，
                            # 排查"不动"时看不到真正发出去的值与频率。
                            sent = (cmd["vx"], cmd["wz"])
                            if sent != self._last_sent_cmd:
                                self._last_sent_cmd = sent
                                if self.logger:
                                    self.logger.info(
                                        "[Nav] 实际发送 vx=%.2f wz=%.2f（%s）",
                                        cmd["vx"],
                                        cmd["wz"],
                                        (cmd.get("reason") or "")[:30],
                                    )
                            await self.motion_controller.set_mecanum_velocity(cmd["vx"], 0.0, cmd["wz"], "auto")
                        except Exception as e:
                            if self.logger:
                                self.logger.warning(f"[Cruise] 运动指令失败: {e}")

                    # 周期性诊断（~2s 一条）：自主行为不能"静默"——
                    # 曾因缺少这条日志，排查"不运动"时完全看不到感知与决策输出。
                    now_dbg = time.monotonic()
                    if now_dbg - self._nav_dbg_ts >= 2.0:
                        self._nav_dbg_ts = now_dbg
                        _enc_vals = "-"
                        if self.odometry is not None:
                            try:
                                _e = self.odometry.get_raw_encoders()
                                _enc_vals = f"({_e['m1']},{_e['m2']},{_e['m3']},{_e['m4']})"
                            except Exception:
                                pass
                        frame_now = (
                            self.camera_manager.get_frame(self._perception_camera_id) if self.camera_manager else None
                        )
                        if self.logger:
                            self.logger.info(
                                "[Nav] mode=%s cam%d有帧=%s 航向=%.1f° ttc_min=%.2f 编码器=%s 光流=%.2fpx/帧 感知ok=%s(%s) 决策=%s(%s)",
                                self.mode_manager.mode,
                                self._perception_camera_id,
                                frame_now is not None,
                                math.degrees(self.odometry.get_pose().get("theta", 0.0) if self.odometry else 0.0),
                                float(getattr(perc, "ttc_min", 0.0) or 0.0),
                                _enc_vals,
                                float(getattr(perc, "flow_px", 0.0) or 0.0),
                                bool(getattr(perc, "ok", False)),
                                (getattr(perc, "reason", "") or "")[:40],
                                (self._last_cruise or {}).get("action", "-"),
                                (self._last_cruise or {}).get("reason", "")[:30],
                            )
                            if self.scanner is not None and self.scanner.openness():
                                self.logger.info("[Scan] 方位图: %s", self.scanner.get_status()["openness"])

                # 4) 安全判定（用最新位姿/IMU/电压/指令）
                if self.safety is not None:
                    if hasattr(self, "ws_server") and self.ws_server:
                        self.safety.set_link(getattr(self.ws_server, "client_count", 1) > 0)

                    pose = self.odometry.get_pose()
                    cmd_linear = (
                        getattr(self.motion_controller, "current_linear", 0.0) if self.motion_controller else 0.0
                    )
                    ev = self.safety.check(
                        pose=pose,
                        imu=self.odometry.get_imu(),
                        battery_v=self._read_battery_voltage(),
                        command_linear=cmd_linear,
                    )

                    if ev is not None:
                        # 持续压住硬件（安全停车不是"发一次就完"，要能对抗后续指令）
                        if self.motion_controller:
                            try:
                                await self.motion_controller.stop()
                            except Exception:
                                pass
                        if self.mode_manager is not None and not self.mode_manager.is_manual():
                            self.mode_manager.force_manual(ev.reason)
                            await self._broadcast_control_mode()
                        if not degraded_reported:
                            degraded_reported = True
                            await self._broadcast_safety()
                            if self.logger:
                                self.logger.warning(f"[Safety] 已停车并降级：{ev.reason}")
                    else:
                        degraded_reported = False
            except Exception as e:
                # ⚠️ 必须是 WARNING（限频）：曾记在 DEBUG，导致自主分支静默失败时
                # 日志一片空白，"机器人不动"完全无法排查。
                now_err = time.monotonic()
                if self.logger and now_err - self._nav_err_ts >= 5.0:
                    self._nav_err_ts = now_err
                    self.logger.warning(f"[Nav] 导航循环异常: {type(e).__name__}: {e}", exc_info=True)

            n += 1
            if n % push_every == 0 and hasattr(self, "ws_server") and self.ws_server:
                try:
                    await self.ws_server.broadcast_message(
                        {
                            "type": "odometry_status",
                            "data": {
                                "available": True,
                                "pose": self.odometry.get_pose(),
                                "imu": self.odometry.get_imu(),
                                "perception": self._last_perception,
                            },
                        }
                    )
                except Exception:
                    pass  # 广播失败不应影响采样
            await asyncio.sleep(sample_interval)

    # ---------- 安全底座（S1）----------

    async def _broadcast_safety(self) -> None:
        """广播安全状态（含触发原因）"""
        if self.safety is None or not (hasattr(self, "ws_server") and self.ws_server):
            return
        await self.ws_server.broadcast_message({"type": "safety_status", "data": self.safety.get_status()})

    async def _handle_emergency_stop(self, data: dict) -> dict:
        """一键急停（UI → 服务端 → 硬件 全链路）。

        ⚠️ 历史缺陷：前端一直发送 `emergency_stop`，但服务端从来没有对应处理器，
        日志里长期是 `Unknown message type: emergency_stop`（返回 404）——
        也就是说**这个按钮过去从未生效**。本处理器把它接通：
            安全层置位 → 硬件急停 → 降级到 manual（带原因）→ 广播
        """
        reason = str((data or {}).get("reason") or "用户一键急停")
        stopped = False
        if self.motion_controller:
            try:
                await self.motion_controller.emergency_stop()
                stopped = True
            except Exception as e:
                if self.logger:
                    self.logger.error(f"急停执行失败: {e}")

        if self.safety is not None:
            self.safety.trip("external", reason)
            if self.mode_manager is not None and not self.mode_manager.is_manual():
                self.mode_manager.force_manual(reason)
            await self._broadcast_safety()
            await self._broadcast_control_mode()

        if self.logger:
            self.logger.warning(f"收到一键急停请求：{reason}（硬件已停: {stopped}）")
        return {"type": "emergency_stop_ack", "data": {"ok": True, "reason": reason, "stopped": stopped}}

    async def _handle_emergency_release(self, data: dict) -> dict:
        """解除急停（需用户显式操作，安全层不自动恢复）"""
        if self.safety is None:
            return {"type": "emergency_release_ack", "data": {"ok": False, "message": "安全模块未启用"}}
        cleared = self.safety.clear(str((data or {}).get("reason") or "用户解除急停"))
        # 解除后允许再次运动（清掉控制器的急停标志）
        if self.motion_controller:
            try:
                await self.motion_controller.stop()
            except Exception:
                pass
        await self._broadcast_safety()
        return {"type": "emergency_release_ack", "data": {"ok": cleared}}

    def _selftest_env(self):
        """给执行器自检框架准备环境（缺什么就不传，由框架判定"该机型不支持"）"""

        class _Env:
            pass

        env = _Env()
        env.motion = self.motion_controller
        env.odometry = self.odometry
        env.gimbal = self.gimbal_controller
        if self.camera_manager is not None:
            env.grab_frame = lambda: self.camera_manager.get_frame(self._perception_camera_id)
        return env

    async def _handle_actuator_selftest_list(self, data: dict) -> dict:
        """列出可用的执行器自检项（供面板生成按钮）"""
        from modules.navigation.actuator_selftest import list_tests

        env = self._selftest_env()
        from modules.navigation.actuator_selftest import SELF_TESTS

        out = []
        for item in list_tests():
            inst = SELF_TESTS[item["id"]]()
            ok, why = inst.available(env)
            out.append({**item, "available": ok, "reason": why})
        return {"type": "actuator_selftest_list", "data": {"tests": out}}

    async def _handle_actuator_selftest(self, data: dict) -> dict:
        """执行器自检（通用）。

        与旧 wheel_selftest 的区别：走统一框架，云台/轮子/将来的其它执行器共用同一套
        "保存-指令-测量-恢复"流程与同一份结果结构。
        ⚠️ 会动（云台微动 / 轮子短距离移动），必须显式触发。
        """
        from modules.navigation.actuator_selftest import SELF_TESTS

        payload = data or {}
        test_id = str(payload.get("test") or "gimbal")
        cls = SELF_TESTS.get(test_id)
        if cls is None:
            return {"type": "actuator_selftest_result", "data": {"ok": False, "error": f"未知自检项: {test_id}"}}

        inst = cls()
        env = self._selftest_env()
        avail, why = inst.available(env)
        if not avail:
            result = {"name": inst.name, "ok": False, "status": "skip", "detail": why, "steps": []}
        else:
            try:
                res = await inst.run(env)
                result = res.to_dict()
            except Exception as e:
                result = {"name": inst.name, "ok": False, "status": "fail", "detail": f"执行失败: {e}", "steps": []}

        if self.logger:
            self.logger.warning("[Selftest] %s", result)
        if hasattr(self, "ws_server") and self.ws_server:
            await self.ws_server.broadcast_message({"type": "actuator_selftest_result", "data": result})
        return {"type": "actuator_selftest_result", "data": result}

    async def _handle_wheel_selftest(self, data: dict) -> dict:
        """轮子自检：短暂驱动一次，用编码器增量判断**哪个轮子不转**。

        ⚠️ 这是**主动**自检 —— 会让机器人真的移动：
          · 必须显式触发（面板按钮 / 手动命令），不在开机时自动跑
          · 默认 1.2 秒、速度 0.2，位移约 24cm
          · 要求机器人着地、前方有空间
        判定：某轮增量明显小于"最大增量"的 30% → 判该轮不动。
        """
        if self.logger:
            # 入口就记一条：用于区分"消息没送到"与"送到了但没执行"
            self.logger.warning(
                "[Selftest] 收到自检请求 data=%s motion=%s odometry=%s",
                data,
                self.motion_controller is not None,
                self.odometry is not None,
            )
        if self.motion_controller is None or self.odometry is None:
            return {"type": "wheel_selftest_result", "data": {"ok": False, "error": "运动/里程计不可用"}}

        payload = data or {}
        dur = max(0.5, min(3.0, float(payload.get("duration", 1.2))))
        speed = max(0.05, min(0.5, float(payload.get("speed", 0.2))))

        try:
            self.odometry.sample()
            enc0 = self.odometry.get_raw_encoders()
            t0 = time.monotonic()
            while time.monotonic() - t0 < dur:
                self.odometry.sample()
                await self.motion_controller.set_mecanum_velocity(speed, 0.0, 0.0, "manual")
                await asyncio.sleep(0.04)
        finally:
            try:
                await self.motion_controller.stop()
            except Exception:
                pass

        self.odometry.sample()
        enc1 = self.odometry.get_raw_encoders()
        deltas = {k: int(enc1.get(k, 0)) - int(enc0.get(k, 0)) for k in enc0 if k != "changed"}
        mx = max([abs(v) for v in deltas.values()] or [0])
        rows = []
        for w, v in deltas.items():
            ok = mx > 0 and abs(v) >= 0.3 * mx
            rows.append({"wheel": w, "delta": v, "ok": ok})
        bad = [r["wheel"] for r in rows if not r["ok"]]
        result = {
            "ok": not bad,
            "tested_s": round(dur, 2),
            "speed": round(speed, 2),
            "rows": rows,
            "bad_wheels": bad,
            "note": "全轮无位移可能是上报链路或驱动未生效" if mx == 0 else "",
        }
        if self.logger:
            self.logger.warning("[Selftest] 轮子自检结果: %s", result)
        if hasattr(self, "ws_server") and self.ws_server:
            await self.ws_server.broadcast_message({"type": "wheel_selftest_result", "data": result})
        return {"type": "wheel_selftest_result", "data": result}

    async def _handle_safety_status(self, data: dict) -> dict:
        """查询安全状态"""
        if self.safety is None:
            return {"type": "safety_status", "data": {"available": False}}
        status = dict(self.safety.get_status())
        status["available"] = True
        return {"type": "safety_status", "data": status}

    # ---------- 红外遥控 (IR Remote, R00020) ----------

    async def _handle_ir_device_list(self, data: dict) -> dict:
        """获取红外设备列表"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("device_list", data)

    async def _handle_ir_device_add(self, data: dict) -> dict:
        """新增红外设备"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("device_add", data)

    async def _handle_ir_device_delete(self, data: dict) -> dict:
        """删除红外设备"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("device_delete", data)

    async def _handle_ir_device_update(self, data: dict) -> dict:
        """更新红外设备"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("device_update", data)

    async def _handle_ir_button_list(self, data: dict) -> dict:
        """获取红外按键列表"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("button_list", data)

    async def _handle_ir_learn_start(self, data: dict) -> dict:
        """开始红外学习"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("learn_start", data)

    async def _handle_ir_button_rename(self, data: dict) -> dict:
        """重命名红外按键"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("button_rename", data)

    async def _handle_ir_button_delete(self, data: dict) -> dict:
        """删除红外按键"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("button_delete", data)

    async def _handle_ir_send(self, data: dict) -> dict:
        """发射红外信号"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("send", data)

    async def _handle_ir_codes_export(self, data: dict) -> dict:
        """导出红外码库"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("codes_export", data)

    async def _handle_ir_codes_import(self, data: dict) -> dict:
        """导入红外码库"""
        if not hasattr(self, "ir_controller") or not self.ir_controller:
            return {"type": "error", "data": {"code": 503, "message": "IR controller not available"}}
        return await self.ir_controller.handle_command("codes_import", data)

    async def _handle_power_policy_toggle(self, data: dict) -> dict:
        """处理省电模式切换"""
        if not hasattr(self, "power_policy") or not self.power_policy:
            return {"type": "error", "data": {"code": 503, "message": "Power policy not available"}}

        old_mode = self.power_policy.get_status().get("mode", "normal")
        await self.power_policy.toggle()
        new_mode = self.power_policy.get_status().get("mode", "normal")
        self.logger.info(f"省电模式手动切换: {old_mode} → {new_mode}")
        return {
            "type": "power_policy_status",
            "data": self.power_policy.get_status(),
        }

    async def _handle_voice_broadcast(self, data: dict) -> dict:
        """处理客户端喊话音频消息（由 WebSocket 二进制帧解析后调用）"""
        if not self._is_feature_enabled("voice_broadcast"):
            return self._feature_disabled_response("voice_broadcast")
        if not hasattr(self, "voice_broadcast_controller") or not self.voice_broadcast_controller:
            return {"type": "error", "data": {"code": 503, "message": "Voice broadcast not available"}}

        mode = data.get("mode", "record")
        audio_data = data.get("_audio_data")
        audio_format = data.get("format")  # "pcm_s16le" 表示原始 PCM
        sample_rate = data.get("rate")  # 采样率，如 48000
        if isinstance(audio_data, str):
            # 如果是从 JSON 中传来的 base64，解码为 bytes（仅用于测试）
            import base64

            audio_data = base64.b64decode(audio_data)

        if not isinstance(audio_data, bytes):
            return {"type": "error", "data": {"code": 400, "message": "Missing audio data"}}

        self.logger.info(f"喊话: {len(audio_data)}B (mode={mode}, format={audio_format}, rate={sample_rate})")
        return await self.voice_broadcast_controller.play_audio(audio_data, mode, audio_format, sample_rate)

    async def _handle_power_policy_status(self, data: dict) -> dict:
        """获取省电策略状态"""
        if not hasattr(self, "power_policy") or not self.power_policy:
            return {"type": "error", "data": {"code": 503, "message": "Power policy not available"}}
        return {
            "type": "power_policy_status",
            "data": self.power_policy.get_status(),
        }

    async def _handle_power_policy_config(self, data: dict) -> dict:
        """获取/设置省电策略配置"""
        if not hasattr(self, "power_policy") or not self.power_policy:
            return {"type": "error", "data": {"code": 503, "message": "Power policy not available"}}

        action = data.get("action", "get")
        if action == "set":
            threshold = data.get("threshold")
            if threshold is not None:
                await self.power_policy.set_threshold(int(threshold))
            return {
                "type": "power_policy_config",
                "data": self.power_policy.get_status(),
            }
        # get
        return {
            "type": "power_policy_config",
            "data": self.power_policy.get_status(),
        }

    async def _handle_power_policy_simulate(self, data: dict) -> dict:
        """模拟电量用于测试省电自动切换（调试用）。level=-1 清除模拟。"""
        if not hasattr(self, "power_policy") or not self.power_policy:
            return {"type": "error", "data": {"code": 503, "message": "Power policy not available"}}

        level = data.get("level")
        if level is None or level == -1:
            self.power_policy.set_simulated_battery_level(None)
        else:
            level = int(level)
            self.power_policy.set_simulated_battery_level(level)
            # 直接根据模拟电量强制切换模式
            if level <= self.power_policy._threshold:
                await self.power_policy.set_mode(self.power_policy.MODE_ECO, from_auto=True)
            elif level >= self.power_policy.AUTO_EXIT_THRESHOLD:
                await self.power_policy.set_mode(self.power_policy.MODE_NORMAL, from_auto=True)

        return {
            "type": "power_policy_status",
            "data": self.power_policy.get_status(),
        }

    async def _handle_software_uninstall(self, data: dict) -> dict:
        """卸载软件（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command(
                "software_manager", "uninstall", data, timeout=120.0
            )
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    async def _handle_software_upgrade(self, data: dict) -> dict:
        """升级软件（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command(
                "software_manager", "upgrade", data, timeout=120.0
            )
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    # ---- WiFi 管理 ----

    async def _handle_wifi_scan(self, data: dict) -> dict:
        """扫描附近 WiFi 网络

        策略：
        1. nmcli dev status 获取当前连接（无需 root）
        2. iwlist 扫描附近网络（无需 root），nmcli 老版本 rescan 需要 sudo
        3. 合并去重，当前连接的信号强度从 nmcli 补充
        """
        try:
            # 1. 获取当前连接信息 (nmcli)
            current_ssid = None
            current_device = None
            current_signal = 0
            try:
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in result.stdout.strip().split("\n"):
                    parts = line.split(":")
                    if len(parts) >= 4 and parts[1] == "wifi" and parts[2] == "connected":
                        current_device = parts[0]
                        current_ssid = parts[3]
                        break
            except Exception:
                pass

            # 当前连接信号强度 (from nmcli)
            if current_ssid:
                try:
                    r2 = subprocess.run(
                        ["nmcli", "-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    for line in r2.stdout.strip().split("\n"):
                        parts = line.split(":")
                        if len(parts) >= 2 and parts[0] == "*" and parts[1].isdigit():
                            current_signal = int(parts[1])
                            break
                except Exception:
                    pass

            # 2. iwlist 扫描附近 WiFi（无需 sudo）
            networks = []
            seen = set()
            scan_output = ""

            try:
                # 先用 nmcli rescan 试一下（新版 nmcli 支持，静默忽略错误）
                subprocess.run(
                    ["nmcli", "dev", "wifi", "rescan"],
                    capture_output=True,
                    timeout=5,
                )
                # 等待扫描完成
                await asyncio.sleep(2)
            except Exception:
                pass

            # 主扫描引擎：iwlist（Jetson Nano 上 nmcli 1.10 不支持 --rescan 且 rescan 需要 sudo）
            try:
                iw_result = subprocess.run(
                    ["/sbin/iwlist", "wlan0", "scanning"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                scan_output = iw_result.stdout
            except Exception:
                pass

            # 备用：nmcli list（可能只返回已连接网络）
            if not scan_output.strip():
                try:
                    nm_result = subprocess.run(
                        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    scan_output = nm_result.stdout
                    # nmcli 格式
                    for line in scan_output.strip().split("\n"):
                        if not line:
                            continue
                        parts = line.split(":")
                        if len(parts) < 2:
                            continue
                        ssid = parts[0].strip()
                        if not ssid or ssid in seen:
                            continue
                        seen.add(ssid)
                        signal = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                        security = parts[2].strip() if len(parts) > 2 else "--"
                        networks.append(
                            {
                                "ssid": ssid,
                                "signal": signal,
                                "security": security,
                                "connected": ssid == current_ssid,
                            }
                        )
                except Exception:
                    pass
            else:
                # iwlist 格式解析
                # Cell 01 - Address: XX:XX:XX:XX:XX:XX
                #           ESSID:"name"
                #           Encryption key:on/off
                #           Quality=39/100  Signal level=-86 dBm
                #           IE: IEEE 802.11i/WPA2 ...
                import re

                cells = scan_output.split("Cell ")
                for cell in cells[1:]:  # 跳过第一个空元素
                    essid_match = re.search(r'ESSID:"([^"]*)"', cell)
                    if not essid_match:
                        continue
                    ssid = essid_match.group(1)
                    if not ssid or ssid in seen:
                        continue
                    seen.add(ssid)

                    # 信号强度: Quality=X/100 或 Signal level=-XX dBm
                    sig_match = re.search(r"Signal level=(-\d+)\s*dBm", cell)
                    if sig_match:
                        dbm = int(sig_match.group(1))
                        # dBm → 0-100: -30dBm≈100%, -90dBm≈0%
                        signal = max(0, min(100, 2 * (dbm + 100)))
                    else:
                        qual_match = re.search(r"Quality=(\d+)/", cell)
                        signal = int(qual_match.group(1)) if qual_match else 0

                    # 安全类型
                    encryption = re.search(r"Encryption key:(on|off)", cell)
                    if encryption and encryption.group(1) == "on":
                        if "WPA2" in cell or "IEEE 802.11i" in cell:
                            security = "WPA2"
                        elif "WPA " in cell:
                            security = "WPA"
                        else:
                            security = "WEP"
                    else:
                        security = "--"

                    is_connected = ssid == current_ssid
                    # 当前连接的信号优先用 nmcli 的值
                    if is_connected and current_signal > 0:
                        signal = current_signal

                    networks.append(
                        {
                            "ssid": ssid,
                            "signal": signal,
                            "security": security,
                            "connected": is_connected,
                        }
                    )

            # 确保当前连接在列表中
            if current_ssid and current_ssid not in seen and current_device:
                networks.append(
                    {
                        "ssid": current_ssid,
                        "signal": current_signal,
                        "security": "--",
                        "connected": True,
                    }
                )

            # 按信号强度排序
            networks.sort(key=lambda n: n["signal"], reverse=True)  # type: ignore[arg-type, return-value]

            return {
                "type": "wifi_scan_result",
                "data": {
                    "current_ssid": current_ssid,
                    "current_device": current_device,
                    "networks": networks,
                },
            }
        except Exception as e:
            self.logger.error(f"WiFi scan failed: {e}", exc_info=True)
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_wifi_connect(self, data: dict) -> dict:
        """连接 WiFi 网络"""
        ssid = data.get("ssid", "").strip()
        password = data.get("password", "").strip()
        if not ssid:
            return {"type": "error", "data": {"code": 400, "message": "SSID required"}}

        self.logger.info(f"WiFi connect: {ssid}")
        try:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
            if password:
                cmd += ["password", password]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            ok = result.returncode == 0
            return {
                "type": "wifi_connect_result",
                "data": {
                    "ssid": ssid,
                    "status": "connected" if ok else "failed",
                    "output": result.stdout.strip() if not ok else "",
                    "error": result.stderr.strip() if not ok else "",
                },
            }
        except subprocess.TimeoutExpired:
            return {"type": "wifi_connect_result", "data": {"ssid": ssid, "status": "timeout"}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    # ---- 音乐播放 ----

    def _is_music_service_running(self) -> bool:
        """检查音乐播放服务是否在运行"""
        if not self.service_manager:
            return False
        state = self.service_manager.get_service_status("music_player")
        return state is not None and state.get("status") == "running"

    def _music_unavailable_response(self, msg_type: str = "music_status") -> dict:
        """音乐服务不可用时的统一响应，避免 error 类型触发前端 Toast"""
        return {"type": msg_type, "data": {"status": "stopped", "active_source": "none"}}

    async def _forward_music_command(self, cmd: str, data: dict, resp_type: str = "music_action") -> dict:
        """转发命令到音乐子进程，若功能被禁用或服务不可用则返回友好响应"""
        if not self._is_feature_enabled("music"):
            return {"type": "error", "data": {"code": 403, "message": "功能 'music' 已被管理员禁用"}}
        if not self._is_music_service_running():
            return self._music_unavailable_response(resp_type)
        result = await self.service_manager.send_subprocess_command("music_player", cmd, data)
        # 防御性检查：子进程可能在状态检查和实际通信之间退出
        if result.get("type") == "error":
            return self._music_unavailable_response(resp_type)
        return result

    async def _handle_music_play(self, data: dict) -> dict:
        """播放音乐"""
        return await self._forward_music_command("play", data, "music_action")

    async def _handle_music_pause(self, data: dict) -> dict:
        """暂停音乐"""
        return await self._forward_music_command("pause", data, "music_action")

    async def _handle_music_stop(self, data: dict) -> dict:
        """停止音乐"""
        return await self._forward_music_command("stop", data, "music_action")

    async def _handle_music_resume(self, data: dict) -> dict:
        """恢复音乐"""
        return await self._forward_music_command("resume", data, "music_action")

    async def _handle_music_next(self, data: dict) -> dict:
        """下一首"""
        return await self._forward_music_command("next", data, "music_action")

    async def _handle_music_previous(self, data: dict) -> dict:
        """上一首"""
        return await self._forward_music_command("previous", data, "music_action")

    async def _handle_music_seek(self, data: dict) -> dict:
        """跳转进度"""
        return await self._forward_music_command("seek", data, "music_action")

    async def _handle_music_volume(self, data: dict) -> dict:
        """设置音量"""
        # 省电模式下限制音量上限 ≤50%
        if hasattr(self, "power_policy") and self.power_policy and self.power_policy.is_eco:
            requested = data.get("volume", 50)
            if isinstance(requested, (int, float)) and requested > 50:
                data = dict(data)  # 不修改原始 dict
                data["volume"] = 50
        return await self._forward_music_command("set_volume", data, "music_action")

    async def _handle_music_status(self, data: dict) -> dict:
        """获取播放状态"""
        return await self._forward_music_command("get_status", data, "music_status")

    async def _handle_music_list(self, data: dict) -> dict:
        """获取歌曲列表"""
        return await self._forward_music_command("list_songs", data, "music_list")

    async def _handle_music_playlist_add(self, data: dict) -> dict:
        """添加到播放队列"""
        return await self._forward_music_command("playlist_add", data, "music_action")

    async def _handle_music_playlist_remove(self, data: dict) -> dict:
        """从播放队列移除"""
        return await self._forward_music_command("playlist_remove", data, "music_action")

    async def _handle_music_playlist_clear(self, data: dict) -> dict:
        """清空播放队列"""
        return await self._forward_music_command("playlist_clear", data, "music_action")

    async def _handle_music_stream_start(self, data: dict) -> dict:
        """启动推流"""
        return await self._forward_music_command("stream_start", data, "stream_start")

    async def _handle_music_stream_stop(self, data: dict) -> dict:
        """停止推流"""
        return await self._forward_music_command("stream_stop", data, "stream_stop")

    async def _handle_wifi_disconnect(self, data: dict) -> dict:
        """断开 WiFi 连接"""
        self.logger.info("WiFi disconnect requested")
        try:
            # 获取当前 WiFi 设备名
            device = data.get("device", "")
            if not device:
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in result.stdout.strip().split("\n"):
                    parts = line.split(":")
                    if len(parts) >= 3 and parts[1] == "wifi" and parts[2] == "connected":
                        device = parts[0]
                        break

            if device:
                subprocess.run(["nmcli", "dev", "disconnect", device], capture_output=True, timeout=10)

            return {"type": "wifi_disconnect_result", "data": {"status": "disconnected"}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    # ------------------------------------------------------------------
    # 机器人详细配置 (R00033)
    # ------------------------------------------------------------------

    # 敏感配置字段：config_get 返回时剔除
    _CONFIG_SENSITIVE_KEYS = {"secret", "token", "password", "client_token"}

    def _sanitize_config(self, config: dict) -> dict:
        """剔除配置中的敏感字段"""
        import copy

        sanitized = copy.deepcopy(config)
        # 剔除顶层敏感字段
        for key in list(sanitized.keys()):
            if key in self._CONFIG_SENSITIVE_KEYS:
                del sanitized[key]
        # 剔除 security.token
        if "security" in sanitized and "token" in sanitized["security"]:
            del sanitized["security"]["token"]
        # 剔除 binding 中的 secret 和 password
        if "binding" in sanitized:
            sanitized["binding"] = {k: v for k, v in sanitized["binding"].items() if k not in ("secret", "password")}
        return sanitized

    async def _handle_config_get(self, data: dict) -> dict:
        """获取当前完整配置（已剔除敏感字段）"""
        sanitized = self._sanitize_config(self.config)
        return {"type": "config_get_ack", "data": sanitized}

    async def _handle_config_set(self, data: dict) -> dict:
        """提交配置修改：校验、持久化、热重载"""
        new_config = data.get("config", {})
        if not new_config:
            return {"type": "error", "data": {"code": 400, "message": "Missing config data"}}

        # 清理 new_config 中可能循环污染的子模块嵌套
        new_config = self._clean_nested_blocks(new_config)

        try:
            # 1. 对比差异
            diff = self._compute_config_diff(self.config, new_config)
            if not diff:
                return {"type": "config_set_ack", "data": {"success": True, "changes": [], "requires_reboot": False}}

            # 2. 合并配置（深度合并，保留未修改的字段）
            merged = self._deep_merge_config(self.config, new_config)

            # 3. 持久化写入 config.yaml
            from pathlib import Path

            import yaml

            config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(merged, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            if self.logger:
                self.logger.info(f"[Config] Written to {config_path}")

            # 4. 热重载受影响的模块
            requires_reboot = await self._apply_config_changes(diff, merged)

            # 5. 更新内存中的配置
            self.config = merged

            # 6. 广播配置变更通知
            await self._broadcast_config_change(diff)

            return {
                "type": "config_set_ack",
                "data": {
                    "success": True,
                    "changes": diff,
                    "requires_reboot": requires_reboot,
                    "reboot_reason": "advertised_ip 变更需要重启服务" if requires_reboot else "",
                },
            }
        except Exception as e:
            if self.logger:
                self.logger.error(f"[Config] config_set failed: {e}", exc_info=True)
            return {"type": "error", "data": {"code": 500, "message": f"配置应用失败: {e}"}}

    def _compute_config_diff(self, old: dict, new: dict, prefix: str = "") -> list[str]:
        """计算新旧配置差异，返回变更路径列表"""
        changes = []
        for key, new_val in new.items():
            full_key = f"{prefix}.{key}" if prefix else key
            old_val = old.get(key)
            if isinstance(new_val, dict) and isinstance(old_val, dict):
                changes.extend(self._compute_config_diff(old_val, new_val, full_key))
            elif new_val != old_val:
                changes.append(full_key)
        return changes

    def _deep_merge_config(self, base: dict, overlay: dict) -> dict:
        """深度合并配置：overlay 覆盖 base"""
        import copy

        result = copy.deepcopy(base)
        for key, val in overlay.items():
            if isinstance(val, dict) and key in result and isinstance(result[key], dict):
                result[key] = self._deep_merge_config(result[key], val)
            else:
                result[key] = copy.deepcopy(val)
        return result

    async def _apply_config_changes(self, diff: list[str], new_config: dict) -> bool:
        """根据差异热重载受影响的模块，返回是否需要重启"""
        requires_reboot = False

        # features 变更 → 广播新的 features 列表并重置功能门控
        if any(c.startswith("features") for c in diff):
            if self.logger:
                self.logger.info("[Config] Features changed, broadcasting updated features")
            await self._broadcast_features_update(new_config)

        # motion 变更 → 调用 MotionController setter
        if any(c.startswith("motion") for c in diff) and self.motion_controller:
            motion_cfg = new_config.get("motion", {})
            if "drive_type" in motion_cfg:
                self.motion_controller.set_drive_type(motion_cfg["drive_type"])
                if self.logger:
                    self.logger.info(f"[Config] Motion drive_type → {motion_cfg['drive_type']}")
            if "max_linear_speed" in motion_cfg:
                self.motion_controller.max_linear_speed = motion_cfg["max_linear_speed"]
            if "max_angular_speed" in motion_cfg:
                self.motion_controller.max_angular_speed = motion_cfg["max_angular_speed"]

        # camera 变更 → 重启受影响的摄像头
        if any(c.startswith("camera") for c in diff) and self.camera_manager:
            try:
                camera_cfg = new_config.get("camera", {})
                if hasattr(self.camera_manager, "apply_config"):
                    await self.camera_manager.apply_config(camera_cfg)
                if self.logger:
                    self.logger.info("[Config] Camera config applied")
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"[Config] Camera reload failed: {e}")

        # gimbal 变更 → 重新初始化云台参数
        if any(c.startswith("gimbal") for c in diff) and hasattr(self, "gimbal_controller") and self.gimbal_controller:
            gimbal_cfg = new_config.get("gimbal", {})
            try:
                gc = self.gimbal_controller
                for attr in (
                    "pan_invert",
                    "tilt_invert",
                    "pan_min",
                    "pan_max",
                    "tilt_min",
                    "tilt_max",
                    "pan_center",
                    "tilt_center",
                    "step",
                ):
                    if attr in gimbal_cfg:
                        setattr(gc, attr, gimbal_cfg[attr])
                if self.logger:
                    self.logger.info("[Config] Gimbal config applied")
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"[Config] Gimbal reload failed: {e}")

        # server.advertised_ip 变更 → 需要重启
        if any("advertised_ip" in c for c in diff):
            requires_reboot = True
            if self.logger:
                self.logger.info("[Config] advertised_ip changed, requires reboot")

        # power_policy 变更 → 更新省电策略阀值
        if any(c.startswith("power_policy") for c in diff) and hasattr(self, "power_policy") and self.power_policy:
            pp_cfg = new_config.get("power_policy", {})
            if "threshold" in pp_cfg:
                threshold = int(pp_cfg["threshold"])
                threshold = max(10, min(50, threshold))
                self.power_policy._threshold = threshold
                if self.logger:
                    self.logger.info(f"[Config] Power policy threshold → {threshold}%")

        # binding.methods 变更 → 热更新绑定方式开关
        if any(c.startswith("binding.methods") for c in diff):
            if hasattr(self, "binding_manager") and self.binding_manager:
                methods = new_config.get("binding", {}).get("methods", {})
                if methods:
                    self.binding_manager.set_methods(methods)
            # 同时更新外设检测器的 config，使 get_available_methods 使用新配置
            if hasattr(self.ws_server, "peripheral_detector") and self.ws_server.peripheral_detector:
                self.ws_server.peripheral_detector.config = new_config

        # binding.password 变更 → 重新哈希密码
        if any(c == "binding.password" for c in diff) and hasattr(self, "binding_manager") and self.binding_manager:
            new_password = new_config.get("binding", {}).get("password", "")
            if new_password:
                result = self.binding_manager.set_password(new_password)
                if result.get("success") and self.logger:
                    self.logger.info("[Config] Binding password updated")
            else:
                if self.logger:
                    self.logger.info("[Config] Binding password unchanged (empty)")

        # binding.password_enabled 变更 → 热更新
        if (
            any(c == "binding.password_enabled" for c in diff)
            and hasattr(self, "binding_manager")
            and self.binding_manager
        ):
            enabled = bool(new_config.get("binding", {}).get("password_enabled", True))
            self.binding_manager.set_password_enabled(enabled)
            if self.logger:
                self.logger.info(f"[Config] Password binding → {enabled}")

        # software_manager 配置变更 → 重启 software_manager 子进程使新配置生效
        if (
            any(c.startswith("software_manager") for c in diff)
            and hasattr(self, "service_manager")
            and self.service_manager
        ):
            try:
                if self.logger:
                    self.logger.info("[Config] software_manager config changed, restarting subprocess...")
                await self.service_manager.restart_service("software_manager")
                if self.logger:
                    self.logger.info("[Config] software_manager subprocess restarted")
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"[Config] software_manager restart failed: {e}")

        return requires_reboot

    async def _broadcast_features_update(self, new_config: dict) -> None:
        """当 features 配置变更时，广播新的 features 列表给所有客户端"""
        if not hasattr(self, "ws_server") or not self.ws_server:
            return
        features_cfg = new_config.get("features", {})
        if not isinstance(features_cfg, dict):
            features_cfg = {}

        def _enabled(k):
            return bool(features_cfg.get(k, True))

        features = ["websocket", "exec", "system"]
        if hasattr(self, "motion_controller") and self.motion_controller and _enabled("motion"):
            features.append("motion")
        if hasattr(self, "camera_manager") and self.camera_manager and _enabled("camera"):
            features.append("camera")
        if hasattr(self, "gimbal_controller") and self.gimbal_controller and _enabled("gimbal"):
            features.append("gimbal")
        if hasattr(self, "dance_controller") and self.dance_controller and _enabled("dance"):
            features.append("dance")
        if "music_player" in SERVICE_DEFINITIONS and _enabled("music"):
            features.append("music")
        if (
            hasattr(self, "voice_broadcast_controller")
            and self.voice_broadcast_controller
            and _enabled("voice_broadcast")
        ):
            features.append("voice_broadcast")
        # R00020: 红外遥控
        if hasattr(self, "ir_controller") and self.ir_controller and _enabled("ir_remote"):
            features.append("ir_remote")

        await self.ws_server.broadcast_message(
            {
                "type": "features_update",
                "data": {"features": features},
            }
        )

    _CONFIG_TOP_KEYS = frozenset(
        {
            "robot",
            "server",
            "motion",
            "camera",
            "gimbal",
            "features",
            "power_policy",
            "app",
            "mdns",
            "status",
            "modules",
            "logging",
            "security",
            "compatibility",
            "debug",
            "binding",
            "software_manager",
        }
    )

    @classmethod
    def _clean_nested_blocks(cls, config: dict) -> dict:
        """移除配置子块中错误嵌套的其他顶级块（防止 config_set 循环污染）

        例如 gimbal 下面不应该出现 robot 配置块。
        只移除 dict 类型的值，scalar（如 features 中的 bool 标志）不受影响。
        """
        import copy

        cleaned = copy.deepcopy(config)
        for section_name in list(cleaned.keys()):
            section = cleaned.get(section_name)
            if isinstance(section, dict):
                for nested_key in list(section.keys()):
                    if (
                        nested_key in cls._CONFIG_TOP_KEYS
                        and nested_key != section_name
                        and isinstance(section[nested_key], dict)
                    ):
                        if hasattr(logging.getLogger("wobot"), "warning"):
                            logging.getLogger("wobot").warning(
                                f"[Config] Stripped corrupted {section_name}.{nested_key} from incoming config"
                            )
                        del section[nested_key]
        return cleaned

    async def _broadcast_config_change(self, diff: list[str]) -> None:
        """广播配置变更通知给所有客户端"""
        if hasattr(self, "ws_server") and self.ws_server:
            changes_summary = ", ".join(diff[:5])
            if len(diff) > 5:
                changes_summary += f" 等 {len(diff)} 项"
            await self.ws_server.broadcast_message(
                {
                    "type": "service_message",
                    "data": {
                        "subject": "配置已更新",
                        "summary": f"机器人配置变更: {changes_summary}",
                        "body": f"已应用 {len(diff)} 项配置变更",
                        "severity": "info",
                        "source": "config",
                    },
                }
            )

    # ------------------------------------------------------------------
    # 客户端绑定认证 (R00035)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 帐号绑定证明 (R00041: Web端通过帐号服务器远程绑定设备)
    # ------------------------------------------------------------------

    async def _handle_binding_proof_request(self, data: dict) -> dict:
        """处理来自前端的绑定证明请求（OAuth2 绑定流程步骤4A）

        前端发送:
            { type: "binding_proof_request", accountId: "logto_user_xxx", clientId: "persistent_client_id" }

        机器人生成 HMAC-SHA256 证明 → 前端用于调用 POST /api/devices/bind
        """
        account_id = data.get("accountId", "")
        client_id = data.get("clientId", "")

        if not account_id or not client_id:
            return {"type": "error", "data": {"code": 400, "message": "Missing accountId or clientId"}}

        # 检查功能是否启用
        if not self._is_feature_enabled("binding"):
            return self._feature_disabled_response("binding")

        if not hasattr(self, "account_client") or not self.account_client:
            return {
                "type": "binding_proof_response",
                "data": {"success": False, "error": "account_client_not_available"},
            }

        try:
            result = await self.account_client.generate_binding_proof(account_id, client_id)
            if result is None:
                return {
                    "type": "binding_proof_response",
                    "data": {"success": False, "error": "no_binding_for_client"},
                }
            return {
                "type": "binding_proof_response",
                "data": {
                    "success": True,
                    "payload": result["payload"],
                    "proof": result["proof"],
                },
            }
        except Exception as e:
            self.logger.error(f"[BindingProof] Error: {e}", exc_info=True)
            return {
                "type": "binding_proof_response",
                "data": {"success": False, "error": str(e)[:200]},
            }

    # ------------------------------------------------------------------
    # 本地绑定方法
    # ------------------------------------------------------------------

    async def _handle_bind_request(self, data: dict) -> dict:
        """处理绑定请求：创建会话并启动验证方式"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}

        ws_client_id = data.get("_ws_client_id", "")
        request_token = data.get("requestToken", "")
        user_client_id = data.get("clientId", "")
        client_name = data.get("clientName", "未命名设备")
        method = data.get("method", "")

        if not request_token or not user_client_id or not method:
            return {"type": "error", "data": {"code": 400, "message": "Missing requestToken/clientId/method"}}

        # 检查冷却期
        if self.binding_manager._check_cooldown(ws_client_id):
            return {"type": "error", "data": {"code": 429, "message": "验证失败次数过多，请稍后再试"}}

        # 检查绑定上限
        if not self.binding_manager.can_add_binding():
            return {"type": "error", "data": {"code": 403, "message": "已达到最大绑定客户端数"}}

        # 清理该客户端的旧会话
        self.binding_manager.cleanup_client_sessions(ws_client_id)

        # 创建会话（后端生成 requestToken）
        session = self.binding_manager.create_session(ws_client_id, user_client_id, client_name)
        # 启动验证方式（使用后端生成的 requestToken）
        session = self.binding_manager.start_method(session.request_token, method)
        if session is None:
            return {"type": "error", "data": {"code": 400, "message": "无效或已过期的 requestToken"}}

        # 后端生成的 requestToken，覆盖前端传来的值
        request_token = session.request_token

        # 根据方式执行对应操作
        ack_data: dict = {"requestToken": request_token, "method": method}

        if method == "display":
            # 屏幕显示：暂时只记录日志（渲染待实现）
            if self.logger:
                self.logger.info(f"[Bind] Display code: {session.random_code} (display rendering not implemented)")

        elif method == "tts":
            # TTS 播报配对数字
            if hasattr(self, "tts_engine") and self.tts_engine:
                self._track_bind_task(
                    ws_client_id, asyncio.create_task(self.tts_engine.speak_pairing_code(session.random_code))
                )

        elif method == "qr_scan":
            # QR 扫描改为手动触发（前端点击"开始扫描"按钮后发送 bind_start_scan）
            if not (hasattr(self, "qr_scanner") and self.qr_scanner and self.qr_scanner.is_available()):
                return {"type": "error", "data": {"code": 503, "message": "QR 扫描不可用（需要摄像头和 OpenCV）"}}

        elif method == "gimbal":
            # 启动云台转动
            if hasattr(self, "gimbal_controller") and self.gimbal_controller:
                self._track_bind_task(
                    ws_client_id, asyncio.create_task(self._perform_gimbal_sequence(session.gimbal_sequence or []))
                )
            else:
                return {"type": "error", "data": {"code": 503, "message": "云台不可用"}}

        elif method == "password" and not self.binding_manager.is_password_enabled():
            return {"type": "error", "data": {"code": 403, "message": "密码绑定未开启"}}

        return {"type": "bind_request_ack", "data": ack_data}

    async def _handle_bind_verify(self, data: dict) -> dict:
        """处理绑定验证：校验随机码并创建绑定"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}

        ws_client_id = data.get("_ws_client_id", "")
        request_token = data.get("requestToken", "")
        random_code = str(data.get("randomCode", "")).strip()

        if not request_token or not random_code:
            return {"type": "error", "data": {"code": 400, "message": "Missing requestToken/randomCode"}}

        result = self.binding_manager.verify(request_token, random_code, ws_client_id)
        if result["success"]:
            # 绑定成功，TTS 播报
            if hasattr(self, "tts_engine") and self.tts_engine:
                asyncio.create_task(self.tts_engine.speak_bind_success())
            # 更新当前连接的绑定状态，无需重连
            if hasattr(self, "ws_server") and self.ws_server:
                self.ws_server._client_bound[ws_client_id] = True
                self.ws_server._client_user_ids[ws_client_id] = result["binding"]["clientId"]
                if self.logger:
                    self.logger.info(f"[{ws_client_id}] Binding verified, client marked as bound")
            # 广播通知所有已绑定客户端：绑定列表已更新 + 推送软件更新
            await self._on_client_bound(ws_client_id)
            return {
                "type": "bind_success",
                "data": {
                    "clientToken": result["client_token"],
                    "clientId": result["binding"]["clientId"],
                },
            }
        else:
            return {
                "type": "bind_failed",
                "data": {
                    "error": result["error"],
                    "attempts": self.binding_manager._failure_counts.get(ws_client_id, 0),
                },
            }

    async def _handle_bind_password(self, data: dict) -> dict:
        """密码绑定验证：客户端输入机器人密码完成绑定"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}

        ws_client_id = data.get("_ws_client_id", "")
        request_token = data.get("requestToken", "")
        password = str(data.get("password", ""))

        if not request_token or not password:
            return {"type": "error", "data": {"code": 400, "message": "Missing requestToken/password"}}

        result = self.binding_manager.verify_password(request_token, password, ws_client_id)
        if result["success"]:
            # 绑定成功，TTS 播报
            if hasattr(self, "tts_engine") and self.tts_engine:
                asyncio.create_task(self.tts_engine.speak_bind_success())
            # 更新当前连接的绑定状态
            if hasattr(self, "ws_server") and self.ws_server:
                self.ws_server._client_bound[ws_client_id] = True
                self.ws_server._client_user_ids[ws_client_id] = result["binding"]["clientId"]
                if self.logger:
                    self.logger.info(f"[{ws_client_id}] Password binding verified, client marked as bound")
            # 广播绑定列表更新 + 推送软件更新
            await self._on_client_bound(ws_client_id)
            return {
                "type": "bind_success",
                "data": {
                    "clientToken": result["client_token"],
                    "clientId": result["binding"]["clientId"],
                },
            }
        else:
            return {
                "type": "bind_failed",
                "data": {"error": result["error"], "remaining": result.get("remaining", 0)},
            }

    async def _handle_bind_replay(self, data: dict) -> dict:
        """重播当前验证方式（TTS/云台/屏幕）"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}

        ws_client_id = data.get("_ws_client_id", "")
        request_token = data.get("requestToken", "")
        session = self.binding_manager.get_session(request_token)
        if session is None:
            return {"type": "error", "data": {"code": 400, "message": "会话不存在或已过期"}}

        method = session.method
        if self.logger:
            self.logger.info(f"[Bind] Replay requested: method={method}, token={request_token[:16]}...")

        if method == "tts":
            if hasattr(self, "tts_engine") and self.tts_engine:
                self._track_bind_task(
                    ws_client_id, asyncio.create_task(self.tts_engine.speak_pairing_code(session.random_code))
                )
            else:
                return {"type": "error", "data": {"code": 503, "message": "TTS 不可用"}}
        elif method == "gimbal":
            if hasattr(self, "gimbal_controller") and self.gimbal_controller:
                self._track_bind_task(
                    ws_client_id, asyncio.create_task(self._perform_gimbal_sequence(session.gimbal_sequence or []))
                )
            else:
                return {"type": "error", "data": {"code": 503, "message": "云台不可用"}}
        elif method == "display":
            # display 方式重新显示数字（渲染待实现）
            if self.logger:
                self.logger.info(f"[Bind] Replay display code: {session.random_code}")
        else:
            return {"type": "error", "data": {"code": 400, "message": f"不支持重播: {method}"}}

        return {"type": "bind_replay_ack", "data": {"method": method}}

    async def _handle_bind_start_scan(self, data: dict) -> dict:
        """手动触发 QR 扫描"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}

        ws_client_id = data.get("_ws_client_id", "")
        request_token = data.get("requestToken", "")
        session = self.binding_manager.get_session(request_token)
        if session is None:
            return {"type": "error", "data": {"code": 400, "message": "会话不存在或已过期"}}

        if session.method != "qr_scan":
            return {"type": "error", "data": {"code": 400, "message": "当前方式不支持扫码"}}

        if hasattr(self, "qr_scanner") and self.qr_scanner and self.qr_scanner.is_available():
            self._track_bind_task(ws_client_id, asyncio.create_task(self._qr_scan_loop(request_token, ws_client_id)))
            if self.logger:
                self.logger.info(f"[Bind] QR scan started manually: token={request_token[:16]}...")
            return {"type": "bind_scan_started", "data": {"requestToken": request_token}}
        else:
            return {"type": "error", "data": {"code": 503, "message": "QR 扫描不可用"}}

    async def _handle_bind_list(self, data: dict) -> dict:
        """获取已绑定客户端列表"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}
        bindings = self.binding_manager.get_bindings()
        # 不返回 clientToken（安全）
        safe_bindings = [
            {
                "clientId": b.get("clientId"),
                "clientName": b.get("clientName"),
                "boundAt": b.get("boundAt"),
                "lastSeen": b.get("lastSeen"),
            }
            for b in bindings
        ]
        return {"type": "bind_list_ack", "data": {"bindings": safe_bindings}}

    async def _handle_bind_password_config(self, data: dict) -> dict:
        """获取密码绑定配置状态（需认证）"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}
        config = self.binding_manager.get_password_config()
        return {"type": "bind_password_config_ack", "data": config}

    async def _handle_bind_password_update(self, data: dict) -> dict:
        """更新密码绑定配置：修改密码 / 开关密码绑定（需认证）"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}
        password = str(data.get("password", ""))
        enabled = data.get("enabled")
        # 修改密码
        if password:
            result = self.binding_manager.set_password(password)
            if not result["success"]:
                return {"type": "bind_password_update_ack", "data": result}
        # 开关密码绑定
        if enabled is not None:
            self.binding_manager.set_password_enabled(bool(enabled))
        return {"type": "bind_password_update_ack", "data": {"success": True}}

    async def _handle_bind_share_create(self, data: dict) -> dict:
        """生成分享绑定码"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}
        share_info = self.binding_manager.create_share_code()
        return {
            "type": "bind_share_created",
            "data": {
                "code": share_info["code"],
                "expires_in": 120,
            },
        }

    async def _handle_bind_share_use(self, data: dict) -> dict:
        """使用分享绑定码完成绑定"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}
        code = data.get("shareCode", "")
        ws_client_id = data.get("_ws_client_id", "")
        user_client_id = data.get("clientId", "")
        client_name = data.get("clientName", "")
        if not code or not user_client_id:
            return {"type": "bind_failed", "data": {"error": "缺少分享码或客户端ID"}}
        result = self.binding_manager.use_share_code(code, ws_client_id, user_client_id, client_name)
        if result["success"]:
            # 更新当前连接的绑定状态
            if hasattr(self, "ws_server") and self.ws_server:
                self.ws_server._client_bound[ws_client_id] = True
                self.ws_server._client_user_ids[ws_client_id] = result["binding"]["clientId"]
                if self.logger:
                    self.logger.info(f"[{ws_client_id}] Share code binding verified, client marked as bound")
            # 语音播报绑定成功
            if hasattr(self, "tts_engine") and self.tts_engine:
                asyncio.create_task(self.tts_engine.speak_bind_success())
            # 广播绑定列表更新 + 推送软件更新
            await self._on_client_bound(ws_client_id)
            return {
                "type": "bind_success",
                "data": {
                    "clientId": result["binding"]["clientId"],
                    "clientToken": result["client_token"],
                },
            }
        return {"type": "bind_failed", "data": {"error": result["error"]}}

    async def _handle_bind_remove(self, data: dict) -> dict:
        """移除指定客户端的绑定"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}
        target_client_id = data.get("clientId", "")
        if not target_client_id:
            return {"type": "error", "data": {"code": 400, "message": "Missing clientId"}}
        removed = self.binding_manager.remove_binding(target_client_id)
        if removed:
            # 踢下线被移除的客户端
            await self._kick_client_by_user_id(target_client_id, "绑定已被移除")
            # 广播通知所有客户端
            await self._broadcast_bind_list()
            return {"type": "bind_remove_ack", "data": {"clientId": target_client_id, "status": "removed"}}
        else:
            return {"type": "error", "data": {"code": 404, "message": "Binding not found"}}

    async def _handle_bind_remove_all(self, data: dict) -> dict:
        """移除所有绑定（排除当前请求者自身的绑定）"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}
        ws_client_id = data.get("_ws_client_id", "")
        # 获取当前请求者的 user_client_id，不移除其绑定
        current_user_id = ""
        if hasattr(self, "ws_server") and self.ws_server:
            current_user_id = self.ws_server._client_user_ids.get(ws_client_id, "")
        # 获取所有要移除的 client_id（排除当前请求者）
        all_bindings = self.binding_manager.get_bindings()
        removed_client_ids = [b.get("clientId", "") for b in all_bindings if b.get("clientId", "") != current_user_id]
        # 逐个移除（保留当前请求者的绑定）
        count = 0
        for cid in removed_client_ids:
            if self.binding_manager.remove_binding(cid):
                count += 1
        # 踢下线所有被移除的客户端
        for cid in removed_client_ids:
            await self._kick_client_by_user_id(cid, "绑定已被移除", exclude_ws_id=ws_client_id)
        # 广播通知
        await self._broadcast_bind_list()
        return {"type": "bind_remove_all_ack", "data": {"count": count}}

    async def _broadcast_bind_list(self) -> None:
        """向所有已绑定的客户端广播最新绑定列表"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return
        if not hasattr(self, "ws_server") or not self.ws_server:
            return
        bindings = self.binding_manager.get_bindings()
        safe_bindings = [
            {
                "clientId": b.get("clientId", ""),
                "clientName": b.get("clientName", ""),
                "boundAt": b.get("boundAt", ""),
                "lastSeen": b.get("lastSeen", ""),
            }
            for b in bindings
        ]
        await self.ws_server.broadcast_message(
            {
                "type": "bind_list_update",
                "data": {"bindings": safe_bindings},
            }
        )

    async def _check_and_push_updates(self, ws_client_id: str = "") -> None:
        """检测软件更新并主动推送给客户端

        客户端绑定时触发：检查 software_manager 子服务是否有可更新软件，
        如有则推送给指定客户端（无 ws_client_id 时广播给所有在线客户端）。
        """
        if not self.service_manager:
            return
        try:
            result = await self.service_manager.send_subprocess_command("software_manager", "check_updates", {})
            if result and result.get("type") == "software_updates_available":
                updates = result.get("data", {}).get("updates", [])
                if updates:
                    msg = {
                        "type": "software_updates_available",
                        "data": {"updates": updates},
                    }
                    if ws_client_id:
                        # 只推送给刚绑定的客户端
                        await self._send_to_client(ws_client_id, msg)
                    else:
                        await self.ws_server.broadcast_message(msg)
        except Exception as e:
            if self.logger:
                self.logger.debug(f"[SoftwareUpdate] check_updates failed: {e}")

    async def _on_client_bound(self, ws_client_id: str) -> None:
        """客户端绑定成功后的统一回调：广播绑定列表 + 推送软件更新"""
        await self._broadcast_bind_list()
        # 异步检查软件更新，不阻塞绑定响应
        asyncio.create_task(self._check_and_push_updates(ws_client_id))

    async def _kick_client_by_user_id(self, user_client_id: str, reason: str, exclude_ws_id: str = "") -> None:
        """通过 user_client_id 踢下线对应的 WebSocket 客户端"""
        if not hasattr(self, "ws_server") or not self.ws_server:
            return
        # 查找对应的 ws_client_id
        to_kick = [
            ws_id
            for ws_id, uid in self.ws_server._client_user_ids.items()
            if uid == user_client_id and ws_id != exclude_ws_id
        ]
        for ws_id in to_kick:
            ws = self.ws_server._ws_clients.get(ws_id)
            if ws:
                try:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "force_disconnect",
                                "data": {"reason": reason},
                            }
                        )
                    )
                    await ws.close()
                    if self.logger:
                        self.logger.info(f"[{ws_id}] Kicked: {reason} (user_client_id={user_client_id})")
                except Exception as e:
                    if self.logger:
                        self.logger.warning(f"[{ws_id}] Failed to kick: {e}")

    async def _perform_gimbal_sequence(self, sequence: list[str]) -> None:
        """执行云台随机转动序列（用于绑定认证方式4）

        时序优化：
        - 开局回中后停留 0.8s 让用户看清起始位置
        - 每个方向：转动后停留 1.0s（足够观察），回中后停留 0.3s（短暂停顿区分组）
        - 总时长约 0.8 + 5*(1.0+0.3) = 7.3s（原 12.5s）
        """
        if not hasattr(self, "gimbal_controller") or not self.gimbal_controller:
            return
        gc = self.gimbal_controller
        try:
            # 开局回中
            await gc.center()
            await asyncio.sleep(0.8)

            for direction in sequence:
                # 检查任务是否被取消
                if asyncio.current_task() and asyncio.current_task().cancelled():  # type: ignore[union-attr]
                    return

                if direction in ("左", "右"):
                    # 水平转动：pan 轴
                    pan_center = getattr(gc, "pan_center", 90)
                    pan_min = getattr(gc, "pan_min", 0)
                    pan_max = getattr(gc, "pan_max", 180)
                    pan_invert = getattr(gc, "pan_invert", False)

                    if direction == "左":
                        target = pan_center - 30 if not pan_invert else pan_center + 30
                    else:
                        target = pan_center + 30 if not pan_invert else pan_center - 30
                    target = max(pan_min, min(pan_max, target))
                    await gc.set_pan(float(target))
                    if self.logger:
                        self.logger.info(f"[Bind] Gimbal move: {direction} → pan={target}")
                else:
                    # 垂直转动：tilt 轴
                    tilt_center = getattr(gc, "tilt_center", 90)
                    tilt_min = getattr(gc, "tilt_min", 0)
                    tilt_max = getattr(gc, "tilt_max", 180)
                    tilt_invert = getattr(gc, "tilt_invert", False)

                    if direction == "上":
                        target = tilt_center + 30 if not tilt_invert else tilt_center - 30
                    else:
                        target = tilt_center - 30 if not tilt_invert else tilt_center + 30
                    target = max(tilt_min, min(tilt_max, target))
                    await gc.set_tilt(float(target))
                    if self.logger:
                        self.logger.info(f"[Bind] Gimbal move: {direction} → tilt={target}")

                # 转动后停留（让用户观察方向）
                await asyncio.sleep(1.0)

                # 回中（短暂停顿，区分下一组）
                await gc.center()
                await asyncio.sleep(0.3)

            if self.logger:
                self.logger.info("[Bind] Gimbal sequence completed")
        except asyncio.CancelledError:
            # 被取消时回中
            try:
                await gc.center()
            except Exception:
                pass
            if self.logger:
                self.logger.info("[Bind] Gimbal sequence cancelled")
        except Exception as e:
            if self.logger:
                self.logger.error(f"[Bind] Gimbal sequence error: {e}", exc_info=True)

    def _track_bind_task(self, ws_client_id: str, task: asyncio.Task) -> None:
        """跟踪绑定相关异步任务"""
        if ws_client_id not in self._bind_tasks:
            self._bind_tasks[ws_client_id] = []
        self._bind_tasks[ws_client_id].append(task)
        # 清理已完成任务
        self._bind_tasks[ws_client_id] = [t for t in self._bind_tasks[ws_client_id] if not t.done()]

    def _cancel_bind_tasks(self, ws_client_id: str) -> None:
        """取消指定客户端的所有绑定任务"""
        tasks = self._bind_tasks.pop(ws_client_id, [])
        for task in tasks:
            if not task.done():
                task.cancel()
                if self.logger:
                    self.logger.info(f"[Bind] Cancelled task for {ws_client_id}")

    async def _handle_bind_cancel(self, data: dict) -> dict:
        """取消当前绑定会话（前端返回时调用）"""
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return {"type": "error", "data": {"code": 503, "message": "Binding not available"}}

        ws_client_id = data.get("_ws_client_id", "")
        request_token = data.get("requestToken", "")

        # 取消异步任务（云台序列/QR扫描/TTS）
        self._cancel_bind_tasks(ws_client_id)

        # 清理会话
        if request_token:
            self.binding_manager.cleanup_session(request_token)

        if self.logger:
            self.logger.info(
                f"[Bind] Cancelled by client: ws={ws_client_id}, token={request_token[:16] if request_token else 'N/A'}..."
            )

        return {"type": "bind_cancel_ack", "data": {}}

    async def _qr_scan_loop(self, request_token: str, ws_client_id: str) -> None:
        """QR 扫描后台循环：扫描到 QR 码后自动完成绑定"""
        if not hasattr(self, "qr_scanner") or not self.qr_scanner:
            return
        if not hasattr(self, "binding_manager") or not self.binding_manager:
            return

        try:
            qr_data = await self.qr_scanner.scan_once(timeout=self.binding_manager._session_timeout)
            if qr_data is None:
                # 超时或取消
                await self._send_to_client(
                    ws_client_id,
                    {
                        "type": "bind_failed",
                        "data": {"error": "QR 扫描超时或已取消", "attempts": 0},
                    },
                )
                return

            # 验证 QR 数据
            result = self.binding_manager.verify_qr(qr_data, ws_client_id)
            if result["success"]:
                # TTS 播报
                if hasattr(self, "tts_engine") and self.tts_engine:
                    asyncio.create_task(self.tts_engine.speak_bind_success())
                # 更新当前连接的绑定状态
                if hasattr(self, "ws_server") and self.ws_server:
                    self.ws_server._client_bound[ws_client_id] = True
                    self.ws_server._client_user_ids[ws_client_id] = result["binding"]["clientId"]
                    if self.logger:
                        self.logger.info(f"[{ws_client_id}] QR binding verified, client marked as bound")
                # 广播绑定列表更新 + 推送软件更新（异步不阻塞）
                asyncio.create_task(self._on_client_bound(ws_client_id))
                await self._send_to_client(
                    ws_client_id,
                    {
                        "type": "bind_success",
                        "data": {
                            "clientToken": result["client_token"],
                            "clientId": result["binding"]["clientId"],
                        },
                    },
                )
            else:
                await self._send_to_client(
                    ws_client_id,
                    {
                        "type": "bind_failed",
                        "data": {"error": result["error"], "attempts": 0},
                    },
                )
        except asyncio.CancelledError:
            if self.logger:
                self.logger.info(f"[Bind] QR scan cancelled for {ws_client_id}")
        except Exception as e:
            if self.logger:
                self.logger.error(f"[Bind] QR scan loop error: {e}", exc_info=True)

    async def _send_to_client(self, ws_client_id: str, message: dict) -> None:
        """向指定 WebSocket 客户端发送消息"""
        if hasattr(self, "ws_server") and self.ws_server:
            await self.ws_server.send_to_client(ws_client_id, message)
