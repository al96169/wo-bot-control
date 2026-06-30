"""
消息处理器
处理各类 WebSocket 消息
"""

from __future__ import annotations

import asyncio
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
        features = ["websocket", "exec", "motion", "system", "camera"]
        if hasattr(self, "gimbal_controller") and self.gimbal_controller:
            features.append("gimbal")
        if hasattr(self, "dance_controller") and self.dance_controller:
            # 通过 service_manager 检查 dance 服务是否实际在运行
            if self.service_manager:
                svc = self.service_manager.get_service_status("dance")
                if svc and svc.get("status") == "running":
                    features.append("dance")
            else:
                features.append("dance")
        # 检查音乐播放服务是否配置
        if "music_player" in SERVICE_DEFINITIONS:
            features.append("music")
        # 检查喊话服务
        if hasattr(self, "voice_broadcast_controller") and self.voice_broadcast_controller:
            features.append("voice_broadcast")
        status_data["features"] = features
        return {"type": "status", "data": status_data}

    async def _handle_motion(self, data: dict) -> dict:
        """处理运动控制（支持双轴兼容 + 三轴麦轮协议）"""
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
        if self.motion_controller:
            await self.motion_controller.stop()
        return {"type": "motion_ack", "data": {"linear": 0, "angular": 0}}

    async def _handle_gimbal(self, data: dict) -> dict | None:
        """处理云台控制"""
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
                step = float(data.get("step", 1.0))
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
        step_per_tick = 1.5  # 满速约 75°/s (1.5° × 50Hz)

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
                self.logger.error(f"Gimbal thread error: {e}")

    async def _handle_motion_config(self, data: dict) -> dict:
        """处理运动配置"""
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

        # 省电模式下跳舞不可用——但允许只读查询（status/list）通过，避免轮询持续弹 403
        if (
            command not in ("status", "list")
            and hasattr(self, "power_policy")
            and self.power_policy
            and self.power_policy.is_eco
        ):
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
        """处理摄像头控制"""
        if not self.camera_manager:
            return {"type": "error", "data": {"code": 503, "message": "Camera not available"}}

        action = data.get("action")
        camera_id = data.get("camera_id", 0)

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
            self.logger.error(f"Reboot failed: {e}")

    async def _delayed_shutdown(self):
        """延迟关机"""
        await asyncio.sleep(2)
        try:
            subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)
        except Exception as e:
            self.logger.error(f"Shutdown failed: {e}")

    async def _delayed_restart_service(self):
        """延迟重启主服务（通过 systemd）"""
        await asyncio.sleep(2)
        try:
            subprocess.run(["sudo", "systemctl", "restart", "wobot-control"], check=False)
        except Exception as e:
            self.logger.error(f"Restart service failed: {e}")

    async def _handle_exec(self, data: dict) -> dict:
        """执行命令（持久 Shell 会话，保持工作目录）"""
        command = data.get("command", "").strip()
        timeout = data.get("timeout", 5000) / 1000

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
        """获取日志"""
        lines = data.get("lines", 100)
        level = data.get("level", "")

        log_file_path = self.config.get("logging", {}).get("file", "logs/wobot.log")
        log_file = Path(log_file_path)
        if not log_file.is_absolute():
            log_file = Path(__file__).parent.parent / log_file_path
        logs = []

        if log_file.exists():
            with open(log_file, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                recent = all_lines[-lines:]
                pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] (\w+): (.+)$")
                for raw in recent:
                    raw = raw.strip()
                    m = pattern.match(raw)
                    if m:
                        ts, lvl, source, msg = m.groups()
                        if level and level.lower() != lvl.lower():
                            continue
                        logs.append({"timestamp": ts, "level": lvl, "source": source, "message": msg})
        return {"type": "logs", "data": {"logs": logs}}

    async def _handle_software_list(self, data: dict) -> dict:
        """获取软件列表（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command("software_manager", "list", data)
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    async def _handle_software_search(self, data: dict) -> dict:
        """搜索软件包（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command("software_manager", "search", data)
        return {"type": "error", "data": {"code": 503, "message": "Service manager not available"}}

    async def _handle_software_install(self, data: dict) -> dict:
        """安装软件包（转发至 software_manager 子服务）"""
        if self.service_manager:
            return await self.service_manager.send_subprocess_command(
                "software_manager", "install", data, timeout=120.0
            )
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

        self.logger.info(f"Device control: {action} -> {'ON' if enabled else 'OFF'}")
        return {"type": "device_control_ack", "data": {"action": action, "enabled": enabled, "status": "ok"}}

    async def _handle_power_policy_toggle(self, data: dict) -> dict:
        """处理省电模式切换"""
        if not hasattr(self, "power_policy") or not self.power_policy:
            return {"type": "error", "data": {"code": 503, "message": "Power policy not available"}}

        await self.power_policy.toggle()
        return {
            "type": "power_policy_status",
            "data": self.power_policy.get_status(),
        }

    async def _handle_voice_broadcast(self, data: dict) -> dict:
        """处理客户端喊话音频消息（由 WebSocket 二进制帧解析后调用）"""
        if not hasattr(self, "voice_broadcast_controller") or not self.voice_broadcast_controller:
            return {"type": "error", "data": {"code": 503, "message": "Voice broadcast not available"}}

        mode = data.get("mode", "record")
        audio_data = data.get("_audio_data")
        if isinstance(audio_data, str):
            # 如果是从 JSON 中传来的 base64，解码为 bytes（仅用于测试）
            import base64

            audio_data = base64.b64decode(audio_data)

        if not isinstance(audio_data, bytes):
            return {"type": "error", "data": {"code": 400, "message": "Missing audio data"}}

        return await self.voice_broadcast_controller.play_audio(audio_data, mode)

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
            self.logger.error(f"WiFi scan failed: {e}")
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
        """转发命令到音乐子进程，若服务不可用则返回友好响应（不触发前端 Toast）"""
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
