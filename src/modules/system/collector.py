"""
系统信息采集模块
采集电池、CPU、内存、网络等系统状态
"""

from __future__ import annotations

import platform
import subprocess
from datetime import datetime

import psutil


class SystemCollector:
    """系统信息采集器"""

    # 12V 锂电池 (3S LiPo) 电压-电量映射
    # 满电 12.6V → 100%, 标称 11.1V → 50%, 截止 10.5V → 0%
    BATTERY_VOLTAGE_MAX = 12.6
    BATTERY_VOLTAGE_MIN = 10.5

    # 剩余时长估计参数
    ESTIMATION_WINDOW_SECONDS = 3600  # 1 小时历史窗口（锂电池中段电压变化极慢，需要大窗口）
    MIN_DISCHARGE_RATE = 0.0001  # 最小放速率 (V/分钟)，极低值确保锂电平坦区也能估算
    # 当实测放速率不可信时，使用保守估算：~0.002 V/min ≈ 约 17 小时从满到空（10Ah 电池 + Jetson 约 10W 负载）
    FALLBACK_DISCHARGE_RATE = 0.002  # V/分钟

    def __init__(self, logger=None):
        self.logger = logger
        self.start_time = datetime.now()
        self._rosmaster_bot = None  # Rosmaster bot 实例引用（用于读取电池电压）
        self._battery_history: list[tuple[float, float]] = []  # (timestamp, voltage) 用于剩余时长估计

    def set_bot(self, bot) -> None:
        """注入 Rosmaster bot 实例（与运动/云台共享串口），用于读取电池电压"""
        self._rosmaster_bot = bot
        if self.logger:
            self.logger.info("SystemCollector: Rosmaster bot injected for battery monitoring")

    @staticmethod
    def _voltage_to_percent(voltage: float) -> int:
        """将 12V 锂电池电压转换为电量百分比"""
        if voltage >= SystemCollector.BATTERY_VOLTAGE_MAX:
            return 100
        if voltage <= SystemCollector.BATTERY_VOLTAGE_MIN:
            return 0
        return round(
            (voltage - SystemCollector.BATTERY_VOLTAGE_MIN)
            / (SystemCollector.BATTERY_VOLTAGE_MAX - SystemCollector.BATTERY_VOLTAGE_MIN)
            * 100
        )

    def _estimate_remaining_minutes(self, voltage: float, level: int, now: float) -> int | None:
        """根据历史电压变化估算剩余使用时长（分钟），返回 None 表示数据不足。

        锂电池在中段（50%-90%）电压曲线非常平坦，可能长时间不变化。
        策略：优先用实测放速率；若数据不足或不稳定，用保守估算。
        """
        self._battery_history.append((now, voltage))
        # 清理过期数据
        cutoff = now - self.ESTIMATION_WINDOW_SECONDS
        self._battery_history = [(t, v) for t, v in self._battery_history if t >= cutoff]

        if len(self._battery_history) < 10:
            return None  # 历史数据不足

        # 用最早和最新采样点计算电压降速率
        first_time, first_voltage = self._battery_history[0]
        last_time, last_voltage = self._battery_history[-1]
        time_delta_minutes = (last_time - first_time) / 60.0
        voltage_drop = first_voltage - last_voltage  # 正数表示在放电

        # 计算电压降速率
        if time_delta_minutes >= 1.0 and voltage_drop > 0:
            rate = voltage_drop / time_delta_minutes  # V/分钟
            if rate >= self.MIN_DISCHARGE_RATE:
                # 实测放速率可信，直接用
                remaining_voltage = last_voltage - self.BATTERY_VOLTAGE_MIN
                if remaining_voltage <= 0:
                    return 0
                return max(1, round(remaining_voltage / rate))

        # 放速率过低或无法测量 → 使用保守估算
        # 仅在有足够历史（>5分钟）且至少有一次采样后才给出保守值
        if time_delta_minutes >= 5.0:
            remaining_voltage = last_voltage - self.BATTERY_VOLTAGE_MIN
            if remaining_voltage <= 0:
                return 0
            estimated = remaining_voltage / self.FALLBACK_DISCHARGE_RATE
            return max(1, round(estimated))

        return None

    async def collect(self) -> dict:
        """采集所有系统信息"""
        try:
            battery = await self._collect_battery()
            system = await self._collect_system()
            network = await self._collect_network()

            return {"battery": battery, "system": system, "network": network}
        except Exception as e:
            if self.logger:
                self.logger.error(f"System collection error: {e}", exc_info=True)
            return {}

    async def _collect_battery(self) -> dict:
        """采集电池信息"""
        try:
            now = datetime.now().timestamp()
            # 优先从 Rosmaster 串口读取真实电池电压
            if self._rosmaster_bot is not None:
                voltage = self._read_rosmaster_battery_voltage()
                if voltage is not None:
                    level = self._voltage_to_percent(voltage)
                    estimated = self._estimate_remaining_minutes(voltage, level, now)
                    return {
                        "level": level,
                        "status": "discharging",
                        "temperature": None,
                        "voltage": round(voltage, 1),
                        "estimated_minutes": estimated,
                    }

            # 回退: psutil（笔记本/树莓派可能支持）
            battery = psutil.sensors_battery()
            if battery:
                level = int(battery.percent)
                # psutil 无电压数据，用百分比换算为近似电压用于趋势追踪
                approx_voltage = self.BATTERY_VOLTAGE_MIN + level / 100.0 * (
                    self.BATTERY_VOLTAGE_MAX - self.BATTERY_VOLTAGE_MIN
                )
                estimated = self._estimate_remaining_minutes(approx_voltage, level, now)
                return {
                    "level": level,
                    "status": "charging" if battery.power_plugged else "discharging",
                    "temperature": None,
                    "voltage": None,
                    "estimated_minutes": estimated,
                }

            # 最终回退: 无电池数据
            return {"level": 100, "status": "unknown", "temperature": None, "voltage": None, "estimated_minutes": None}

        except Exception:
            return {"level": 100, "status": "unknown", "temperature": None, "voltage": None, "estimated_minutes": None}

    def _read_rosmaster_battery_voltage(self) -> float | None:
        """从 Rosmaster bot 读取电池电压（伏特）。

        Rosmaster_Lib 通过自动上报帧解析电池数据，常见 API：
        - bot.get_battery_voltage() 返回电压值
        - 或直接访问属性
        """
        bot = self._rosmaster_bot
        if bot is None:
            return None

        try:
            # 尝试方法1: get_battery_voltage()
            if hasattr(bot, "get_battery_voltage"):
                voltage = bot.get_battery_voltage()
                if voltage is not None and voltage > 0:
                    return float(voltage)
        except Exception:
            pass

        try:
            # 尝试方法2: 读取属性 battery_voltage
            if hasattr(bot, "battery_voltage"):
                voltage = bot.battery_voltage
                if voltage is not None and voltage > 0:
                    return float(voltage)
        except Exception:
            pass

        try:
            # 尝试方法3: get_battery() 返回字典
            if hasattr(bot, "get_battery"):
                data = bot.get_battery()
                if isinstance(data, dict) and "voltage" in data:
                    return float(data["voltage"])
                if isinstance(data, (int, float)) and data > 0:
                    return float(data)
        except Exception:
            pass

        if self.logger:
            self.logger.debug("SystemCollector: unable to read battery from Rosmaster bot")
        return None

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
                self.logger.error(f"System collection error: {e}", exc_info=True)
            return {}

    async def _get_cpu_temperature(self) -> float | None:
        """获取 CPU 温度（Jetson 特有）"""
        try:
            # Jetson Nano 温度文件路径
            temp_paths = [
                "/sys/devices/virtual/thermal/thermal_zone0/temp",
                "/sys/class/thermal/thermal_zone0/temp",
            ]

            for path in temp_paths:
                try:
                    with open(path) as f:
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
            _ = psutil.net_if_stats()

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
                self.logger.error(f"Network collection error: {e}", exc_info=True)
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
