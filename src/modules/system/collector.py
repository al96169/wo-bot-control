"""
系统信息采集模块
采集电池、CPU、内存、网络等系统状态
"""

import asyncio
import platform
import subprocess
from datetime import datetime

import psutil


class SystemCollector:
    """系统信息采集器"""

    def __init__(self, logger=None):
        self.logger = logger
        self.start_time = datetime.now()

    async def collect(self) -> dict:
        """采集所有系统信息"""
        try:
            battery = await self._collect_battery()
            system = await self._collect_system()
            network = await self._collect_network()

            return {"battery": battery, "system": system, "network": network}
        except Exception as e:
            if self.logger:
                self.logger.error(f"System collection error: {e}")
            return {}

    async def _collect_battery(self) -> dict:
        """采集电池信息"""
        try:
            # 尝试读取电池信息（Jetson 可能没有电池）
            battery = psutil.sensors_battery()

            if battery:
                return {
                    "level": int(battery.percent),
                    "status": "charging" if battery.power_plugged else "discharging",
                    "temperature": None,
                    "voltage": None,
                }

            # Jetson 设备：尝试从系统文件读取
            # 这里返回模拟数据，实际需要根据硬件调整
            return {"level": 100, "status": "plugged", "temperature": 25.0, "voltage": 12.0}

        except Exception:
            return {"level": 100, "status": "unknown", "temperature": None, "voltage": None}

    async def _collect_system(self) -> dict:
        """采集系统资源信息"""
        try:
            # CPU
            cpu_percent = psutil.cpu_percent(interval=0.1)

            # 内存
            memory = psutil.virtual_memory()
            memory_percent = memory.percent

            # 磁盘
            disk = psutil.disk_usage("/")
            disk_percent = disk.percent

            # 运行时间
            uptime = (datetime.now() - self.start_time).total_seconds()

            # CPU 温度（Jetson 特有）
            temperature = await self._get_cpu_temperature()

            return {
                "cpu_percent": round(cpu_percent, 1),
                "memory_percent": round(memory_percent, 1),
                "disk_percent": round(disk_percent, 1),
                "uptime": int(uptime),
                "temperature": temperature,
                "platform": platform.system(),
                "hostname": platform.node(),
            }

        except Exception as e:
            if self.logger:
                self.logger.error(f"System collection error: {e}")
            return {}

    async def _get_cpu_temperature(self) -> float:
        """获取 CPU 温度（Jetson 特有）"""
        try:
            # Jetson Nano 温度文件路径
            temp_paths = [
                "/sys/devices/virtual/thermal/thermal_zone0/temp",
                "/sys/class/thermal/thermal_zone0/temp",
            ]

            for path in temp_paths:
                try:
                    with open(path, "r") as f:
                        temp = int(f.read().strip()) / 1000.0
                        return round(temp, 1)
                except FileNotFoundError:
                    continue

            return None

        except Exception:
            return None

    async def _collect_network(self) -> dict:
        """采集网络信息"""
        try:
            # 获取网络接口信息
            interfaces = psutil.net_if_addrs()
            stats = psutil.net_if_stats()

            # 查找主要网络接口（通常是 wlan0 或 eth0）
            main_interface = None
            for iface in ["wlan0", "eth0", "enp3s0", "wlp3s0"]:
                if iface in interfaces:
                    main_interface = iface
                    break

            if not main_interface:
                main_interface = list(interfaces.keys())[0] if interfaces else None

            result = {"ip": None, "ssid": None, "signal_strength": None, "mac": None}

            if main_interface:
                # 获取 IP 地址
                for addr in interfaces[main_interface]:
                    if addr.family == 2:  # AF_INET
                        result["ip"] = addr.address
                    elif addr.family == 17:  # AF_LINK
                        result["mac"] = addr.address

                # 获取 Wi-Fi 信息（如果是无线接口）
                if main_interface.startswith("w"):
                    wifi_info = await self._get_wifi_info(main_interface)
                    result.update(wifi_info)

            return result

        except Exception as e:
            if self.logger:
                self.logger.error(f"Network collection error: {e}")
            return {}

    async def _get_wifi_info(self, interface: str) -> dict:
        """获取 Wi-Fi 信息"""
        try:
            # 使用 iw 命令获取 Wi-Fi 信息
            result = subprocess.run(["iw", "dev", interface, "link"], capture_output=True, text=True, timeout=2)

            ssid = None
            signal = None

            for line in result.stdout.split("\n"):
                if "SSID:" in line:
                    ssid = line.split("SSID:")[1].strip()
                elif "signal:" in line:
                    signal_str = line.split("signal:")[1].strip()
                    signal = int(signal_str.split()[0])

            return {"ssid": ssid, "signal_strength": signal}

        except Exception:
            return {}

    async def get_detailed_info(self) -> dict:
        """获取详细信息"""
        basic = await self.collect()

        # 添加更多详细信息
        try:
            basic["processes"] = len(psutil.pids())
            basic["boot_time"] = psutil.boot_time()

            # CPU 核心
            basic["cpu_count"] = psutil.cpu_count()
            basic["cpu_count_logical"] = psutil.cpu_count(logical=True)

        except Exception:
            pass

        return basic
