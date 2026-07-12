"""
消息处理器
处理各类 WebSocket 消息
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import re
import select
import subprocess
import threading
import time
from pathlib import Path

from core.service_manager import SERVICE_DEFINITIONS


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
    ):
        self.system_collector = system_collector
        self.motion_controller = motion_controller
        self.camera_manager = camera_manager
        self.config = config or {}
        self.logger = logger or logging.getLogger(__name__)
        self.service_manager = service_manager
        # 持久 Shell 会话工作目录（初始为进程当前目录）
        self._shell_cwd = os.getcwd()
        # 云台速度控制：客户端发速度(-1.0~+1.0)，服务端持续循环移动
        self._gimbal_speed = {"pan": 0.0, "tilt": 0.0}
        self._gimbal_running = threading.Event()
        self._gimbal_task: asyncio.Task | None = None
        # 绑定认证异步任务跟踪（ws_client_id -> [task, ...]）
        self._bind_tasks: dict[str, list[asyncio.Task]] = {}

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
        if hasattr(self, "voice_broadcast_controller") and self.voice_broadcast_controller and self._is_feature_enabled("voice_broadcast"):
            features.append("voice_broadcast")
        status_data["features"] = features
        return {"type": "status", "data": status_data}

    async def _handle_motion(self, data: dict) -> dict:
        """处理运动控制（支持双轴兼容 + 三轴麦轮协议）"""
        if not self._is_feature_enabled("motion"):
            return self._feature_disabled_response("motion")
        if not self.motion_controller:
            return {"type": "error", "data": {"code": 503, "message": "Motion controller not available"}}

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
                self._start_gimbal_loop()
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
            elif action == "move_update":
                # 更新移动速度: {action: "move_update", pan_speed: 0.5, tilt_speed: -0.3}
                self._gimbal_speed["pan"] = float(data.get("pan_speed", 0) or 0)
                self._gimbal_speed["tilt"] = float(data.get("tilt_speed", 0) or 0)
                # 不返回 status，避免大量响应阻塞
                return None
            elif action == "move_end":
                # 停止持续移动
                self._gimbal_speed["pan"] = 0.0
                self._gimbal_speed["tilt"] = 0.0
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

    async def _handle_system(self, data: dict) -> dict:
        """处理系统操作"""
        action = data.get("action")

        if action == "reboot":
            self.logger.warning("System reboot requested")
            asyncio.create_task(self._delayed_reboot())
            return {"type": "system_ack", "data": {"action": "reboot", "status": "pending"}}
        elif action == "shutdown":
            self.logger.warning("System shutdown requested")
            asyncio.create_task(self._delayed_shutdown())
            return {"type": "system_ack", "data": {"action": "shutdown", "status": "pending"}}
        elif action == "restart_service":
            service_id = data.get("service_id", "main")
            self.logger.info(f"Service restart requested: {service_id}")
            if hasattr(self, "service_manager") and self.service_manager:
                if service_id == "main":
                    self.logger.warning("Main service restart requested via systemd")
                    asyncio.create_task(self._delayed_restart_service())
                    return {
                        "type": "system_ack",
                        "data": {"action": "restart_service", "service_id": "main", "status": "pending"},
                    }
                success = await self.service_manager.restart_service(service_id)
                return {
                    "type": "system_ack",
                    "data": {
                        "action": "restart_service",
                        "service_id": service_id,
                        "status": "ok" if success else "failed",
                    },
                }
            else:
                if service_id == "main":
                    self.logger.warning("Main service restart requested via systemd (no service_manager)")
                    asyncio.create_task(self._delayed_restart_service())
                    return {
                        "type": "system_ack",
                        "data": {"action": "restart_service", "service_id": "main", "status": "pending"},
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

    async def _delayed_reboot(self):
        """延迟重启"""
        await asyncio.sleep(2)
        try:
            subprocess.run(["sudo", "reboot"], check=False)
        except Exception as e:
            self.logger.error(f"Reboot failed: {e}", exc_info=True)

    async def _delayed_shutdown(self):
        """延迟关机"""
        await asyncio.sleep(2)
        try:
            subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
        except Exception as e:
            self.logger.error(f"Shutdown failed: {e}", exc_info=True)

    async def _delayed_restart_service(self):
        """延迟重启主服务（通过 systemd）"""
        await asyncio.sleep(2)
        try:
            subprocess.run(["sudo", "systemctl", "restart", "wobot-control"], check=False)
        except Exception as e:
            self.logger.error(f"Restart service failed: {e}", exc_info=True)

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
            pattern_new = re.compile(
                r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] \[([\w.]+)\] (\w+): (.+)$"
            )
            # 旧格式兼容: ts [LEVEL] logger_name: message
            pattern_old = re.compile(
                r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] (\w+): (.+)$"
            )
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

        self.logger.info(f"Device control: {action} -> {'ON' if enabled else 'OFF'}")
        return {"type": "device_control_ack", "data": {"action": action, "enabled": enabled, "status": "ok"}}

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
        sample_rate = data.get("rate")     # 采样率，如 48000
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
            sanitized["binding"] = {
                k: v for k, v in sanitized["binding"].items()
                if k not in ("secret", "password")
            }
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
            import yaml
            from pathlib import Path
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
        if any(c.startswith("motion") for c in diff):
            if self.motion_controller:
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
        if any(c.startswith("camera") for c in diff):
            if self.camera_manager:
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
        if any(c.startswith("gimbal") for c in diff):
            if hasattr(self, "gimbal_controller") and self.gimbal_controller:
                gimbal_cfg = new_config.get("gimbal", {})
                try:
                    gc = self.gimbal_controller
                    for attr in ("pan_invert", "tilt_invert", "pan_min", "pan_max", "tilt_min", "tilt_max",
                                 "pan_center", "tilt_center", "step"):
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
        if any(c.startswith("power_policy") for c in diff):
            if hasattr(self, "power_policy") and self.power_policy:
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
        if any(c == "binding.password" for c in diff):
            if hasattr(self, "binding_manager") and self.binding_manager:
                new_password = new_config.get("binding", {}).get("password", "")
                if new_password:
                    result = self.binding_manager.set_password(new_password)
                    if result.get("success") and self.logger:
                        self.logger.info("[Config] Binding password updated")
                else:
                    if self.logger:
                        self.logger.info("[Config] Binding password unchanged (empty)")

        # binding.password_enabled 变更 → 热更新
        if any(c == "binding.password_enabled" for c in diff):
            if hasattr(self, "binding_manager") and self.binding_manager:
                enabled = bool(new_config.get("binding", {}).get("password_enabled", True))
                self.binding_manager.set_password_enabled(enabled)
                if self.logger:
                    self.logger.info(f"[Config] Password binding → {enabled}")

        return requires_reboot

    async def _broadcast_features_update(self, new_config: dict) -> None:
        """当 features 配置变更时，广播新的 features 列表给所有客户端"""
        if not hasattr(self, "ws_server") or not self.ws_server:
            return
        features_cfg = new_config.get("features", {})
        if not isinstance(features_cfg, dict):
            features_cfg = {}
        def _enabled(k): return bool(features_cfg.get(k, True))

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
        if hasattr(self, "voice_broadcast_controller") and self.voice_broadcast_controller and _enabled("voice_broadcast"):
            features.append("voice_broadcast")

        await self.ws_server.broadcast_message({
            "type": "features_update",
            "data": {"features": features},
        })

    _CONFIG_TOP_KEYS = frozenset({
        "robot", "server", "motion", "camera", "gimbal", "features", "power_policy",
        "app", "mdns", "status", "modules", "logging", "security",
        "compatibility", "debug", "binding", "software_manager",
    })

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
                    if nested_key in cls._CONFIG_TOP_KEYS and nested_key != section_name and isinstance(section[nested_key], dict):
                        if hasattr(logging.getLogger("wobot"), "warning"):
                            logging.getLogger("wobot").warning(
                                f"[Config] Stripped corrupted {section_name}.{nested_key} from incoming config")
                        del section[nested_key]
        return cleaned

    async def _broadcast_config_change(self, diff: list[str]) -> None:
        """广播配置变更通知给所有客户端"""
        if hasattr(self, "ws_server") and self.ws_server:
            changes_summary = ", ".join(diff[:5])
            if len(diff) > 5:
                changes_summary += f" 等 {len(diff)} 项"
            await self.ws_server.broadcast_message({
                "type": "service_message",
                "data": {
                    "subject": "配置已更新",
                    "summary": f"机器人配置变更: {changes_summary}",
                    "body": f"已应用 {len(diff)} 项配置变更",
                    "severity": "info",
                    "source": "config",
                },
            })

    # ------------------------------------------------------------------
    # 客户端绑定认证 (R00035)
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
                self._track_bind_task(ws_client_id, asyncio.create_task(self.tts_engine.speak_pairing_code(session.random_code)))

        elif method == "qr_scan":
            # QR 扫描改为手动触发（前端点击"开始扫描"按钮后发送 bind_start_scan）
            if not (hasattr(self, "qr_scanner") and self.qr_scanner and self.qr_scanner.is_available()):
                return {"type": "error", "data": {"code": 503, "message": "QR 扫描不可用（需要摄像头和 OpenCV）"}}

        elif method == "gimbal":
            # 启动云台转动
            if hasattr(self, "gimbal_controller") and self.gimbal_controller:
                self._track_bind_task(ws_client_id, asyncio.create_task(self._perform_gimbal_sequence(session.gimbal_sequence or [])))
            else:
                return {"type": "error", "data": {"code": 503, "message": "云台不可用"}}

        elif method == "password":
            # 密码绑定：检查是否开启
            if not self.binding_manager.is_password_enabled():
                return {"type": "error", "data": {"code": 403, "message": "密码绑定未开启"}}
            # 无需额外操作，客户端直接输入密码

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
            # 广播通知所有已绑定客户端：绑定列表已更新
            await self._broadcast_bind_list()
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
                "data": {"error": result["error"], "attempts": self.binding_manager._failure_counts.get(ws_client_id, 0)},
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
            # 广播绑定列表更新
            await self._broadcast_bind_list()
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
                self._track_bind_task(ws_client_id, asyncio.create_task(self.tts_engine.speak_pairing_code(session.random_code)))
            else:
                return {"type": "error", "data": {"code": 503, "message": "TTS 不可用"}}
        elif method == "gimbal":
            if hasattr(self, "gimbal_controller") and self.gimbal_controller:
                self._track_bind_task(ws_client_id, asyncio.create_task(self._perform_gimbal_sequence(session.gimbal_sequence or [])))
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
            # 广播绑定列表更新
            await self._broadcast_bind_list()
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
        removed_client_ids = [
            b.get("clientId", "") for b in all_bindings
            if b.get("clientId", "") != current_user_id
        ]
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
        await self.ws_server.broadcast_message({
            "type": "bind_list_update",
            "data": {"bindings": safe_bindings},
        })

    async def _kick_client_by_user_id(self, user_client_id: str, reason: str, exclude_ws_id: str = "") -> None:
        """通过 user_client_id 踢下线对应的 WebSocket 客户端"""
        if not hasattr(self, "ws_server") or not self.ws_server:
            return
        # 查找对应的 ws_client_id
        to_kick = [
            ws_id for ws_id, uid in self.ws_server._client_user_ids.items()
            if uid == user_client_id and ws_id != exclude_ws_id
        ]
        for ws_id in to_kick:
            ws = self.ws_server._ws_clients.get(ws_id)
            if ws:
                try:
                    await ws.send(json.dumps({
                        "type": "force_disconnect",
                        "data": {"reason": reason},
                    }))
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
            self.logger.info(f"[Bind] Cancelled by client: ws={ws_client_id}, token={request_token[:16] if request_token else 'N/A'}...")

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
                await self._send_to_client(ws_client_id, {
                    "type": "bind_failed",
                    "data": {"error": "QR 扫描超时或已取消", "attempts": 0},
                })
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
                await self._send_to_client(ws_client_id, {
                    "type": "bind_success",
                    "data": {
                        "clientToken": result["client_token"],
                        "clientId": result["binding"]["clientId"],
                    },
                })
            else:
                await self._send_to_client(ws_client_id, {
                    "type": "bind_failed",
                    "data": {"error": result["error"], "attempts": 0},
                })
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
