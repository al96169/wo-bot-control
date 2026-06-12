"""
消息处理器
处理各类 WebSocket 消息
"""

import asyncio
import os
import pty
import re
import select
import subprocess
import time
from pathlib import Path
from typing import Optional


class MessageHandler:
    """消息处理器"""

    def __init__(
        self,
        system_collector=None,
        motion_controller=None,
        camera_manager=None,
        config: dict = None,
        logger=None,
    ):
        self.system_collector = system_collector
        self.motion_controller = motion_controller
        self.camera_manager = camera_manager
        self.config = config or {}
        self.logger = logger
        # 持久 Shell 会话工作目录（初始为进程当前目录）
        self._shell_cwd = os.getcwd()

    async def handle(self, msg_type: str, msg_data: dict) -> Optional[dict]:
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

    async def _handle_get_status(self, data: dict) -> dict:
        """处理状态请求"""
        if self.system_collector:
            status = await self.system_collector.collect()
            return {"type": "status", "data": status}
        return {"type": "status", "data": {}}

    async def _handle_motion(self, data: dict) -> dict:
        """处理运动控制"""
        if not self.motion_controller:
            return {"type": "error", "data": {"code": 503, "message": "Motion controller not available"}}

        linear = data.get("linear", 0.0)
        angular = data.get("angular", 0.0)
        mode = data.get("mode", "manual")

        try:
            await self.motion_controller.set_velocity(linear, angular, mode)
            return {"type": "motion_ack", "data": {"linear": linear, "angular": angular, "mode": mode}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_motion_stop(self, data: dict) -> dict:
        """处理停止运动"""
        if self.motion_controller:
            await self.motion_controller.stop()
        return {"type": "motion_ack", "data": {"linear": 0, "angular": 0}}

    async def _handle_gimbal(self, data: dict) -> dict:
        """处理云台控制"""
        if not hasattr(self, 'gimbal_controller') or not self.gimbal_controller:
            return {"type": "error", "data": {"code": 503, "message": "Gimbal not available"}}

        axis = data.get("axis", "pan")
        angle = data.get("angle", 90)
        action = data.get("action", "set_angle")

        try:
            if action == "center":
                await self.gimbal_controller.center()
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
            elif action == "get_state":
                return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
            elif axis == "pan":
                await self.gimbal_controller.set_pan(float(angle))
            elif axis == "tilt":
                await self.gimbal_controller.set_tilt(float(angle))
            else:
                return {"type": "error", "data": {"code": 400, "message": f"Unknown axis: {axis}"}}

            return {"type": "gimbal_status", "data": self.gimbal_controller.get_state()}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_emergency_stop(self, data: dict) -> dict:
        """处理急停"""
        if self.motion_controller:
            await self.motion_controller.emergency_stop()
        self.logger.warning("Emergency stop triggered!")
        return {"type": "emergency_stop_ack", "data": {}}

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
            self.logger.info("Service restart requested")
            return {"type": "system_ack", "data": {"action": "restart_service", "status": "pending"}}

        return {"type": "error", "data": {"code": 400, "message": "Invalid system action"}}

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
                    return {"type": "exec_result", "data": {"stdout": f"已切换到 {self._shell_cwd}", "stderr": "", "return_code": 0, "cwd": self._shell_cwd}}
                else:
                    return {"type": "exec_result", "data": {"stdout": "", "stderr": f"cd: {target}: No such file or directory", "return_code": 1}}
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
            output = b''
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
            output_text = output.decode('utf-8', errors='replace')
            # 去除末尾的 \r\n 序列带来的重复换行
            output_text = re.sub(r'\r\n', '\n', output_text)
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
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                recent = all_lines[-lines:]
                pattern = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) \[(\w+)\] (\w+): (.+)$')
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
        """获取软件列表"""
        try:
            result = subprocess.run(["dpkg", "-l"], capture_output=True, text=True, timeout=10)
            packages = []
            for line in result.stdout.split("\n")[5:]:  # 跳过头部
                parts = line.split()
                if len(parts) >= 3:
                    packages.append({"name": parts[1], "version": parts[2], "status": "installed"})

            return {"type": "software_list", "data": {"packages": packages[:50]}}  # 限制返回数量
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_software_search(self, data: dict) -> dict:
        """搜索可安装的软件包（apt-cache search）"""
        keyword = data.get("keyword", "").strip()
        if not keyword:
            return {"type": "error", "data": {"code": 400, "message": "Search keyword required"}}
        try:
            result = subprocess.run(
                ["apt-cache", "search", keyword],
                capture_output=True, text=True, timeout=15
            )
            packages = []
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                # 格式: "package-name - Description"
                parts = line.split(" - ", 1)
                if len(parts) >= 2:
                    packages.append({"name": parts[0].strip(), "description": parts[1].strip()})
                elif parts:
                    packages.append({"name": parts[0].strip(), "description": ""})
            return {"type": "software_search_result", "data": {"keyword": keyword, "packages": packages[:30]}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_software_install(self, data: dict) -> dict:
        """安装软件"""
        package = data.get("package")
        source = data.get("source", "apt")
        if not package:
            return {"type": "error", "data": {"code": 400, "message": "Package name required"}}
        self.logger.info(f"Software install requested: {package} from {source}")
        try:
            if source == "apt":
                result = subprocess.run(["apt-get", "install", "-y", package], capture_output=True, text=True, timeout=120)
                ok = result.returncode == 0
                return {"type": "software_install_ack", "data": {"package": package, "status": "installed" if ok else "failed", "output": result.stdout[:500] if not ok else ""}}
            elif source == "pip":
                result = subprocess.run(["pip", "install", package, "--break-system-packages"], capture_output=True, text=True, timeout=120)
                ok = result.returncode == 0
                return {"type": "software_install_ack", "data": {"package": package, "status": "installed" if ok else "failed", "output": result.stdout[:500] if not ok else ""}}
            return {"type": "software_install_ack", "data": {"package": package, "status": f"source '{source}' not supported"}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_module_list(self, data: dict) -> dict:
        """获取模块列表（动态扫描）"""
        modules = [
            {"id": "motion", "name": "运动控制", "version": "1.0.0", "status": "running" if self.motion_controller else "disabled", "enabled": self.motion_controller is not None},
            {"id": "vision", "name": "视觉模块", "version": "1.0.0", "status": "running" if self.camera_manager else "disabled", "enabled": self.camera_manager is not None},
            {"id": "system", "name": "系统信息", "version": "1.0.0", "status": "running" if self.system_collector else "disabled", "enabled": self.system_collector is not None},
        ]
        # 尝试导入 extension 模块管理器
        try:
            from modules.extension.base import ModuleManager
            mgr = ModuleManager()
            mods = mgr.list_modules()
            for m in mods:
                modules.append({
                    "id": m.get("id", ""),
                    "name": m.get("name", ""),
                    "version": m.get("version", "1.0.0"),
                    "status": m.get("status", "unknown"),
                    "enabled": m.get("enabled", False),
                })
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
        self.logger.info(f"Device control: {action} -> {'ON' if enabled else 'OFF'}")
        return {"type": "device_control_ack", "data": {"action": action, "enabled": enabled, "status": "ok"}}

    async def _handle_software_uninstall(self, data: dict) -> dict:
        """卸载软件"""
        package = data.get("package", "")
        if not package:
            return {"type": "error", "data": {"code": 400, "message": "Package name required"}}
        self.logger.info(f"Software uninstall: {package}")
        try:
            result = subprocess.run(["apt-get", "remove", "-y", package], capture_output=True, text=True, timeout=60)
            ok = result.returncode == 0
            return {"type": "software_uninstall_ack", "data": {"package": package, "status": "uninstalled" if ok else "failed"}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

    async def _handle_software_upgrade(self, data: dict) -> dict:
        """升级软件"""
        package = data.get("package", "")
        if not package:
            return {"type": "error", "data": {"code": 400, "message": "Package name required"}}
        self.logger.info(f"Software upgrade: {package}")
        try:
            result = subprocess.run(["apt-get", "upgrade", "-y", package], capture_output=True, text=True, timeout=120)
            ok = result.returncode == 0
            return {"type": "software_upgrade_ack", "data": {"package": package, "status": "upgraded" if ok else "failed"}}
        except Exception as e:
            return {"type": "error", "data": {"code": 500, "message": str(e)}}

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
                    capture_output=True, text=True, timeout=5,
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
                        capture_output=True, text=True, timeout=10,
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
                    capture_output=True, timeout=5,
                )
                # 等待扫描完成
                await asyncio.sleep(2)
            except Exception:
                pass

            # 主扫描引擎：iwlist（Jetson Nano 上 nmcli 1.10 不支持 --rescan 且 rescan 需要 sudo）
            try:
                iw_result = subprocess.run(
                    ["/sbin/iwlist", "wlan0", "scanning"],
                    capture_output=True, text=True, timeout=15,
                )
                scan_output = iw_result.stdout
            except Exception:
                pass

            # 备用：nmcli list（可能只返回已连接网络）
            if not scan_output.strip():
                try:
                    nm_result = subprocess.run(
                        ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                        capture_output=True, text=True, timeout=10,
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
                        networks.append({
                            "ssid": ssid,
                            "signal": signal,
                            "security": security,
                            "connected": ssid == current_ssid,
                        })
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
                    sig_match = re.search(r'Signal level=(-\d+)\s*dBm', cell)
                    if sig_match:
                        dbm = int(sig_match.group(1))
                        # dBm → 0-100: -30dBm≈100%, -90dBm≈0%
                        signal = max(0, min(100, 2 * (dbm + 100)))
                    else:
                        qual_match = re.search(r'Quality=(\d+)/', cell)
                        signal = int(qual_match.group(1)) if qual_match else 0

                    # 安全类型
                    encryption = re.search(r'Encryption key:(on|off)', cell)
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

                    networks.append({
                        "ssid": ssid,
                        "signal": signal,
                        "security": security,
                        "connected": is_connected,
                    })

            # 确保当前连接在列表中
            if current_ssid and current_ssid not in seen and current_device:
                networks.append({
                    "ssid": current_ssid,
                    "signal": current_signal,
                    "security": "--",
                    "connected": True,
                })

            # 按信号强度排序
            networks.sort(key=lambda n: n["signal"], reverse=True)

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

    async def _handle_wifi_disconnect(self, data: dict) -> dict:
        """断开 WiFi 连接"""
        self.logger.info("WiFi disconnect requested")
        try:
            # 获取当前 WiFi 设备名
            device = data.get("device", "")
            if not device:
                result = subprocess.run(
                    ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev", "status"],
                    capture_output=True, text=True, timeout=5,
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
