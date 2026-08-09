"""
系统信息采集模块
采集电池、CPU、内存、网络、环境温湿度等系统状态

外设数据（温湿度等）通过 PeripheralRegistry 声明式配置采集，
支持三种 Provider：builtin / custom / external，配置文件驱动热重载。
"""

from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime
from pathlib import Path

import psutil

from core.peripheral_registry import PeripheralRegistry


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

    # 累计运行时长持久化文件路径
    RUNTIME_STATS_FILE = Path("data/runtime_stats.json")

    def __init__(self, logger=None, peripheral_registry: PeripheralRegistry | None = None, sensor_recorder=None):
        self.logger = logger
        self.start_time = datetime.now()
        self._rosmaster_bot = None  # Rosmaster bot 实例引用（用于读取电池电压）
        self._battery_history: list[tuple[float, float]] = []  # (timestamp, voltage) 用于剩余时长估计
        # 外设采集注册表（声明式配置，支持 builtin/custom/external 三种 Provider）
        self._peripheral_registry = peripheral_registry or PeripheralRegistry(logger=logger)
        # 传感器数据持久化记录器（R00045）
        self._sensor_recorder = sensor_recorder
        # R00046: 累计运行时长（从持久化文件加载）
        self._total_runtime_seconds: int = self._load_total_runtime()

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
            environment = await self._collect_environment()

            # R00045: 持久化外设数据到 SQLite
            await self._record_peripheral_data()

            # R00046: 设备详情信息
            device_info = {
                "hostname": system.get("hostname", ""),
                "os": self._get_os_info(),
                "kernel": self._get_kernel_version(),
                "cpu_model": self._get_cpu_model(),
                "cpu_count": psutil.cpu_count() or 0,
                "ip": network.get("ip"),
                "mac": network.get("mac"),
                "bluetooth_mac": network.get("bluetooth_mac"),
                "uptime": system.get("uptime", 0),
                "total_runtime": system.get("total_runtime", 0),
            }

            return {
                "battery": battery,
                "system": system,
                "network": network,
                "environment": environment,
                "device_info": device_info,
            }
        except Exception as e:
            if self.logger:
                self.logger.error(f"System collection error: {e}", exc_info=True)
            return {}

    async def _record_peripheral_data(self) -> None:
        """将最新外设采集数据写入传感器记录器"""
        if self._sensor_recorder is None:
            return
        try:
            all_data = await self._peripheral_registry.collect_all()
            # 过滤掉 None 值
            valid_data = {k: v for k, v in all_data.items() if v is not None}
            if valid_data:
                # 构建槽位元数据（含单位）
                slot_metas = {
                    name: {"unit": slot.get("unit", "")} for name, slot in self._peripheral_registry._slots.items()
                }
                await self._sensor_recorder.write_batch(valid_data, slot_metas)
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Peripheral data recording skipped: {e}")

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

            # R00046: 累计运行时长
            total_runtime = self.get_total_runtime_seconds()

            # CPU 温度（Jetson 特有）
            temperature = await self._get_cpu_temperature()

            return {
                "cpu_percent": round(cpu_percent, 1),
                "memory_percent": round(memory_percent, 1),
                "disk_percent": round(disk_percent, 1),
                "uptime": int(uptime),
                "total_runtime": total_runtime,
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
            stats = psutil.net_if_stats()

            # 排除虚拟/回环接口
            virtual_prefixes = ("lo", "docker", "br-", "veth", "virbr", "tunl", "flannel")

            # 查找主要网络接口（优先 wlan0/eth0，其次选第一个非虚拟且 carrier-up 的接口）
            main_interface = None
            for iface in ["wlan0", "eth0", "enp3s0", "wlp3s0"]:
                if iface in interfaces and iface not in virtual_prefixes:
                    main_interface = iface
                    break

            if not main_interface:
                for iface_name in interfaces:
                    if iface_name in virtual_prefixes:
                        continue
                    if iface_name in stats and stats[iface_name].isup:
                        main_interface = iface_name
                        break

            if not main_interface and interfaces:
                main_interface = list(interfaces.keys())[0]

            result: dict = {"ip": None, "ssid": None, "signal_strength": None, "mac": None, "bluetooth_mac": None}

            if main_interface:
                # 获取 IP 地址和 MAC
                for addr in interfaces[main_interface]:
                    if addr.family == 2:  # AF_INET
                        result["ip"] = addr.address
                    elif addr.family == 17:  # AF_LINK
                        result["mac"] = addr.address

                # 获取 Wi-Fi 信息（如果是无线接口）
                if main_interface.startswith("w"):
                    wifi_info = await self._get_wifi_info(main_interface)
                    result.update(wifi_info)

            # 蓝牙 MAC 地址
            result["bluetooth_mac"] = self._get_bluetooth_mac()

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

    @staticmethod
    def _get_bluetooth_mac() -> str | None:
        """读取蓝牙控制器 MAC 地址。

        优先从 /sys/class/bluetooth/hci0/address 读取，
        回退到 hciconfig 命令。无蓝牙模块时返回 None。
        """
        # 方法1: sysfs
        bt_sys_path = "/sys/class/bluetooth/hci0/address"
        try:
            with open(bt_sys_path) as f:
                mac = f.read().strip()
                if mac and len(mac) == 17:
                    return mac.upper()
        except (FileNotFoundError, PermissionError):
            pass

        # 方法2: hciconfig
        try:
            result = subprocess.run(["hciconfig", "hci0", "address"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                for line in result.stdout.split("\n"):
                    if "BD Address:" in line:
                        parts = line.split("BD Address:")
                        if len(parts) > 1:
                            mac = parts[1].strip().split()[0]
                            if mac and len(mac) == 17:
                                return mac.upper()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return None

    # ---- R00046: 累计运行时长管理 ----

    def _load_total_runtime(self) -> int:
        """从持久化文件加载累计运行时长（秒）"""
        try:
            if self.RUNTIME_STATS_FILE.exists():
                with open(self.RUNTIME_STATS_FILE) as f:
                    data = json.load(f)
                    return int(data.get("total_runtime_seconds", 0))
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Failed to load runtime stats: {e}")
        return 0

    def _save_total_runtime(self) -> None:
        """将累计运行时长保存到持久化文件"""
        try:
            self.RUNTIME_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
            current_session = (datetime.now() - self.start_time).total_seconds()
            total = self._total_runtime_seconds + int(current_session)
            with open(self.RUNTIME_STATS_FILE, "w") as f:
                json.dump(
                    {
                        "total_runtime_seconds": total,
                        "last_start_time": self.start_time.isoformat(),
                        "last_stop_time": datetime.now().isoformat(),
                    },
                    f,
                )
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Failed to save runtime stats: {e}")

    def get_total_runtime_seconds(self) -> int:
        """获取累计运行总时长（秒）= 历史累计 + 本次会话"""
        current_session = int((datetime.now() - self.start_time).total_seconds())
        return self._total_runtime_seconds + current_session

    async def _collect_environment(self) -> dict:
        """采集环境数据（温湿度、燃气、光照等）。

        通过 PeripheralRegistry 声明式采集，支持三种 Provider：
          - builtin: 平台内置驱动（如 DHT11）
          - custom:  用户自写驱动（subprocess + JSON 契约）
          - external: 外部数据源（MQTT/HTTP/File/Serial/WS）
        缓存由 PeripheralRegistry 内部管理，按各槽位的 interval_sec 控制频率。
        """
        return await self._peripheral_registry.collect_environment()

    def get_peripheral_status(self) -> dict[str, dict]:
        """获取外设在线状态（随 WebSocket status 推送）"""
        return self._peripheral_registry.get_status()

    def shutdown(self) -> None:
        """服务停止时调用，保存累计运行时长"""
        self._save_total_runtime()
        if self.logger:
            self.logger.info("SystemCollector: runtime stats saved on shutdown")

    @staticmethod
    def _get_os_info() -> str:
        """获取操作系统描述"""
        try:
            result = subprocess.run(["lsb_release", "-ds"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return f"{platform.system()} {platform.release()}"

    @staticmethod
    def _get_kernel_version() -> str:
        """获取内核版本"""
        try:
            result = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return platform.release()

    @staticmethod
    def _get_cpu_model() -> str:
        """获取 CPU 型号"""
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":")[1].strip()
        except Exception:
            pass
        return platform.processor() or "Unknown"

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """将秒数格式化为 'Xd Xh Xm' 形式"""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

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
