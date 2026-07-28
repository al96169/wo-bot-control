"""
通用外设数据采集框架 — PeripheralRegistry

管理数据槽位配置、三种 Provider（builtin/custom/external）的注册与采集调度。
配置文件驱动，支持热重载，与 collector.py 解耦。

架构:
  PeripheralRegistry
    ├─ load: 解析 peripherals.yaml → 槽位配置
    ├─ probe: 逐槽位探测在线状态
    └─ collect: 逐槽位采集数据
         ├─ DataProviderBuiltin  → 平台内置驱动 (dht11_reader 等)
         ├─ DataProviderCustom   → subprocess 调用用户程序
         ├─ DataProviderExternal → MQTT/HTTP/File/Serial/WebSocket
         └─ (none)               → 返回 None
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import yaml


# ============================================================
# 内置驱动注册表
# ============================================================

BUILTIN_DRIVERS: dict[str, Callable] = {}


def register_builtin_driver(name: str):
    """装饰器：注册内置驱动到全局注册表"""
    def decorator(func):
        BUILTIN_DRIVERS[name] = func
        return func
    return decorator


# ============================================================
# 数据槽位静态定义（元数据，不含 provider 配置）
# ============================================================

SLOT_DEFINITIONS: dict[str, dict] = {
    "dht11":               {"unit": "",     "category": "environment"},
    "gas":                 {"unit": "",     "category": "environment"},
    "light":               {"unit": "lux",  "category": "environment"},
    "co2":                 {"unit": "ppm",  "category": "environment"},
    "pm25":                {"unit": "μg/m³","category": "environment"},
    "ir_transceiver":      {"unit": "",     "category": "smart_home"},
    "battery_voltage":     {"unit": "V",    "category": "power"},
}


# ============================================================
# PeripheralRegistry
# ============================================================

class PeripheralRegistry:
    """外设注册表：加载配置、管理 Provider、调度采集"""

    def __init__(self, config_path: str = "config/peripherals.yaml", logger=None):
        self.config_path = config_path
        self.logger = logger
        self._slots: dict[str, dict] = {}       # slot_name → 合并后的完整配置
        self._providers: dict[str, Any] = {}    # slot_name → DataProvider 实例
        self._cache: dict[str, Any] = {}        # slot_name → 最近一次有效采集值（None 不写入）
        self._cache_times: dict[str, float] = {} # slot_name → 最近采集时间戳
        self._last_success_times: dict[str, float] = {}  # slot_name → 最后一次成功采集时间戳
        self._config_mtime: float = 0.0
        self._load_config()

    # ---- 路径解析 ----

    def _resolve_path(self) -> Path:
        """解析配置文件路径，相对路径基于项目根目录"""
        path = Path(self.config_path)
        if not path.is_absolute():
            project_root = Path(__file__).parent.parent.parent
            path = project_root / self.config_path
        return path

    # ---- 配置加载 ----

    def _load_config(self) -> None:
        """加载 peripherals.yaml"""
        path = self._resolve_path()
        if not path.exists():
            if self.logger:
                self.logger.warning(f"Peripheral config not found: {path}, using defaults (all slots=none)")
            self._init_defaults()
            return

        try:
            with open(path) as f:
                config = yaml.safe_load(f) or {}
        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to load peripheral config: {e}")
            self._init_defaults()
            return

        self._config_mtime = path.stat().st_mtime
        raw_slots = config.get("data_slots", {})

        # 合并静态定义 + 用户配置
        for slot_name, slot_def in SLOT_DEFINITIONS.items():
            user_config = raw_slots.get(slot_name, {})
            provider_type = user_config.get("provider", "none")

            merged = {
                "name": slot_name,
                "unit": slot_def["unit"],
                "category": slot_def["category"],
                "provider": provider_type,
                "driver": user_config.get("driver"),
                "config": user_config.get("config", {}),
                "exec": user_config.get("exec"),
                "args": user_config.get("args", []),
                "timeout_ms": user_config.get("timeout_ms", 5000),
                "interval_sec": user_config.get("interval_sec", 30),
                "source": user_config.get("source"),
            }
            self._slots[slot_name] = merged

        # 为每个已配置的槽位创建 Provider 实例
        for slot_name, slot_cfg in self._slots.items():
            self._create_provider(slot_name, slot_cfg)

        if self.logger:
            configured = sum(1 for s in self._slots.values() if s["provider"] != "none")
            self.logger.info(
                f"PeripheralRegistry: {len(self._slots)} slots loaded, {configured} configured"
            )

    def _init_defaults(self) -> None:
        """初始化默认配置：所有槽位为 none"""
        for slot_name, slot_def in SLOT_DEFINITIONS.items():
            self._slots[slot_name] = {
                "name": slot_name,
                "unit": slot_def["unit"],
                "category": slot_def["category"],
                "provider": "none",
                "driver": None,
                "config": {},
                "exec": None,
                "args": [],
                "timeout_ms": 5000,
                "interval_sec": 30,
                "source": None,
            }

    def _create_provider(self, slot_name: str, slot_cfg: dict) -> None:
        """根据槽位配置创建对应的 Provider 实例"""
        ptype = slot_cfg["provider"]
        if ptype == "builtin":
            driver_name = slot_cfg.get("driver")
            if driver_name and driver_name in BUILTIN_DRIVERS:
                self._providers[slot_name] = DataProviderBuiltin(
                    slot_name, driver_name, slot_cfg, self.logger
                )
            elif self.logger:
                self.logger.warning(
                    f"Builtin driver '{driver_name}' not found for slot '{slot_name}'"
                )
        elif ptype == "custom":
            self._providers[slot_name] = DataProviderCustom(slot_name, slot_cfg, self.logger)
        elif ptype == "external":
            self._providers[slot_name] = DataProviderExternal(slot_name, slot_cfg, self.logger)

    def reload_if_changed(self) -> bool:
        """检测配置文件变更并热重载，返回 True 表示已重载"""
        path = self._resolve_path()
        if not path.exists():
            return False
        mtime = path.stat().st_mtime
        if mtime != self._config_mtime:
            if self.logger:
                self.logger.info("peripherals.yaml changed, hot-reloading...")
            self._providers.clear()
            self._cache.clear()
            self._cache_times.clear()
            self._last_success_times.clear()
            self._load_config()
            return True
        return False

    # ---- 槽位查询 ----

    @property
    def slots(self) -> dict[str, dict]:
        """返回所有槽位配置的副本"""
        return dict(self._slots)

    def get_slot(self, name: str) -> dict | None:
        """查询单个槽位配置"""
        return self._slots.get(name)

    # ---- 数据写入（供 WebSocket API 外部推送） ----

    def write_slot(self, slot_name: str, value: Any) -> bool:
        """外部程序通过 API 写入数据到指定槽位"""
        if slot_name in self._slots:
            self._cache[slot_name] = value
            self._cache_times[slot_name] = time.time()
            return True
        return False

    # ---- 采集 ----

    async def probe_all(self) -> dict[str, str]:
        """探测所有槽位在线状态

        返回 {slot_name: "online"|"offline"|"unconfigured"}
        """
        results: dict[str, str] = {}
        for slot_name, slot_cfg in self._slots.items():
            provider = self._providers.get(slot_name)
            if provider is None:
                results[slot_name] = "unconfigured"
                continue
            try:
                ok = await provider.probe()
                results[slot_name] = "online" if ok else "offline"
            except Exception:
                results[slot_name] = "offline"
        return results

    async def collect_all(self) -> dict[str, Any]:
        """采集所有槽位数据

        返回 {slot_name: value}，带缓存（按各槽位的 interval_sec 控制频率）。
        """
        self.reload_if_changed()
        results: dict[str, Any] = {}
        now = time.time()

        for slot_name, slot_cfg in self._slots.items():
            provider = self._providers.get(slot_name)
            if provider is None:
                results[slot_name] = None
                continue

            interval = slot_cfg.get("interval_sec", 30)
            last_time = self._cache_times.get(slot_name, 0)

            # 缓存命中
            if slot_name in self._cache and (now - last_time) < interval:
                results[slot_name] = self._cache[slot_name]
                continue

            # 采集
            try:
                value = await provider.read()
                self._cache_times[slot_name] = now
                if value is not None:
                    # 成功：更新缓存值和成功时间
                    self._cache[slot_name] = value
                    self._last_success_times[slot_name] = now
                # 失败时保持缓存中的上一次有效值（不写入 None）
                results[slot_name] = self._cache.get(slot_name)
            except Exception as e:
                if self.logger:
                    self.logger.debug(f"Peripheral '{slot_name}' read error: {e}")
                self._cache_times[slot_name] = now
                results[slot_name] = self._cache.get(slot_name)

        return results

    async def collect_environment(self) -> dict[str, float | None]:
        """采集环境类槽位（兼容现有 collector.py 的 environment 结构）

        DHT11 槽位返回 dict {temperature, humidity}，这里拆解为独立字段。
        """
        all_data = await self.collect_all()
        dht11 = all_data.get("dht11")
        if isinstance(dht11, dict):
            return {
                "temperature": dht11.get("temperature"),
                "humidity": dht11.get("humidity"),
                "gas": all_data.get("gas"),
                "light": all_data.get("light"),
            }
        return {
            "temperature": None,
            "humidity": None,
            "gas": all_data.get("gas"),
            "light": all_data.get("light"),
        }

    def get_status(self) -> dict[str, dict]:
        """获取外设状态摘要（随 WebSocket status 推送）

        返回 {slot_name: {state, provider, unit, category}}
        在线判断依据：最近一次采集成功时间，而非临时性失败。
        """
        now = time.time()
        status: dict[str, dict] = {}
        for slot_name, slot_cfg in self._slots.items():
            ptype = slot_cfg["provider"]
            if ptype == "none":
                state = "unconfigured"
            elif slot_name in self._last_success_times:
                # 最近一次成功在合理时间内 → 在线
                interval = slot_cfg.get("interval_sec", 30)
                if (now - self._last_success_times[slot_name]) < max(interval * 3, 120):
                    state = "online"
                else:
                    state = "offline"
            elif slot_name in self._cache and self._cache[slot_name] is not None:
                # 有缓存数据但从未记录成功时间（首次读取前的过渡状态）
                state = "online"
            else:
                state = "offline"

            status[slot_name] = {
                "state": state,
                "provider": ptype,
                "unit": slot_cfg["unit"],
                "category": slot_cfg["category"],
            }
        return status


# ============================================================
# DataProviderBuiltin — 平台内置驱动
# ============================================================

class DataProviderBuiltin:
    """内置驱动 Provider：调用已注册的平台驱动采集数据"""

    DRIVER_BIN_MAP = {
        "dht11": "/opt/wobot/bin/dht11_reader",
    }

    def __init__(self, slot_name: str, driver_name: str, config: dict, logger=None):
        self.slot_name = slot_name
        self.driver_name = driver_name
        self.config = config
        self.logger = logger
        self._driver_config = config.get("config", {})

    async def probe(self) -> bool:
        """探测硬件是否在线"""
        try:
            value = await self.read()
            return value is not None
        except Exception:
            return False

    async def read(self) -> Any:
        """调用注册的驱动函数读取数据"""
        handler = BUILTIN_DRIVERS.get(self.driver_name)
        if handler is None:
            raise ValueError(f"Unknown builtin driver: {self.driver_name}")
        return await handler(self.slot_name, self._driver_config, self.logger)


# ---- 内置驱动实现 ----

@register_builtin_driver("dht11")
async def _dht11_read(slot_name: str, config: dict, logger) -> dict | None:
    """DHT11 温湿度传感器驱动。一次 C 程序调用同时获取温度和湿度，返回 dict。"""

    pin = config.get("pin", 194)
    bin_path = DataProviderBuiltin.DRIVER_BIN_MAP.get(
        "dht11", "/opt/wobot/bin/dht11_reader"
    )

    if not os.path.isfile(bin_path):
        if logger:
            logger.debug(f"DHT11 binary not found: {bin_path}")
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path, str(pin),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)

        if proc.returncode != 0:
            if logger:
                msg = stderr.decode(errors="replace").strip()[-200:]
                logger.debug(f"DHT11 read failed (rc={proc.returncode}): {msg}")
            return None

        data = json.loads(stdout.decode().strip())
        temp = float(data.get("temperature", 0))
        hum = float(data.get("humidity", 0))

        if logger:
            logger.info(f"DHT11: {temp:.1f}°C, {hum:.1f}%")

        return {"temperature": temp, "humidity": hum}

    except asyncio.TimeoutError:
        if logger:
            logger.debug("DHT11 read timeout")
    except (json.JSONDecodeError, ValueError, KeyError) as e:
        if logger:
            logger.debug(f"DHT11 parse error: {e}")
    except Exception as e:
        if logger:
            logger.debug(f"DHT11 read error: {e}")

    return None


# ============================================================
# DataProviderCustom — 用户自写驱动 (subprocess + JSON 契约)
# ============================================================

class DataProviderCustom:
    """自定义 Provider：subprocess 调用用户提供的可执行文件。

    契约：
      - stdout 输出 JSON，可包含一个或多个槽位的值
      - exit code 0 = 成功，非 0 = 失败
    """

    def __init__(self, slot_name: str, config: dict, logger=None):
        self.slot_name = slot_name
        self.logger = logger
        self._exec = config.get("exec", "")
        self._args = list(config.get("args", []))
        self._timeout = max(1.0, (config.get("timeout_ms", 5000) or 5000) / 1000.0)

    async def probe(self) -> bool:
        try:
            value = await self.read()
            return value is not None
        except Exception:
            return False

    async def read(self) -> Any:
        if not self._exec or not os.path.isfile(self._exec):
            if self.logger:
                self.logger.debug(f"Custom exec not found: {self._exec}")
            return None

        try:
            cmd = [self._exec] + self._args
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)

            if proc.returncode != 0:
                if self.logger:
                    msg = stderr.decode(errors="replace").strip()[-200:]
                    self.logger.debug(
                        f"Custom '{self.slot_name}' rc={proc.returncode}: {msg}"
                    )
                return None

            data = json.loads(stdout.decode().strip())
            return self._extract_slot_value(data)

        except asyncio.TimeoutError:
            if self.logger:
                self.logger.debug(f"Custom '{self.slot_name}' timeout")
        except (json.JSONDecodeError, ValueError) as e:
            if self.logger:
                self.logger.debug(f"Custom '{self.slot_name}' JSON error: {e}")
        except Exception as e:
            if self.logger:
                self.logger.debug(f"Custom '{self.slot_name}' error: {e}")

        return None

    def _extract_slot_value(self, data: dict) -> Any:
        """从用户程序输出的 JSON 中提取当前槽位的值。

        优先级：
          1. 精确匹配槽位名 (如 ambient_temperature)
          2. 去掉 ambient_ 前缀匹配 (如 temperature)
          3. 单键 dict 直接返回值
        """
        if not isinstance(data, dict):
            return data
        if self.slot_name in data:
            return data[self.slot_name]
        stripped = self.slot_name.replace("ambient_", "").replace("_", "")
        for key, val in data.items():
            if key.replace("_", "") == stripped:
                return val
        if len(data) == 1:
            return list(data.values())[0]
        # 无法匹配，返回原始数据让上层处理
        return data


# ============================================================
# DataProviderExternal — 外部数据源 (MQTT/HTTP/File/Serial/WebSocket)
# ============================================================

class DataProviderExternal:
    """外部数据源 Provider。

    支持的 source 类型：
      - http:      HTTP GET 轮询，解析 JSON
      - file:      读取本地文件（其他进程写入）
      - mqtt:      MQTT 订阅（需 aiomqtt，后续集成）
      - serial:    串口读取（需 pyserial，后续集成）
      - websocket: 被动接收，由外部通过 API 推入
    """

    SUPPORTED_SOURCES = {"http", "file", "mqtt", "serial", "websocket"}

    def __init__(self, slot_name: str, config: dict, logger=None):
        self.slot_name = slot_name
        self.logger = logger
        self._source = config.get("source", "file")
        self._source_config = config.get("config", {})
        self._timeout = max(1.0, (config.get("timeout_ms", 5000) or 5000) / 1000.0)

    async def probe(self) -> bool:
        try:
            value = await self.read()
            return value is not None
        except Exception:
            return False

    async def read(self) -> Any:
        if self._source == "http":
            return await self._read_http()
        elif self._source == "file":
            return await self._read_file()
        elif self._source == "mqtt":
            return await self._read_mqtt()
        elif self._source == "serial":
            return await self._read_serial()
        elif self._source == "websocket":
            return self._read_websocket()
        else:
            if self.logger:
                self.logger.warning(f"Unknown external source: {self._source}")
            return None

    # ---- HTTP ----

    async def _read_http(self) -> Any:
        url = self._source_config.get("url", "")
        json_path = self._source_config.get("json_path", "")
        if not url:
            return None
        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    return self._extract_path(data, json_path)
        except ImportError:
            # 降级：阻塞式 HTTP（不阻塞事件循环太久）
            import urllib.request
            try:
                with urllib.request.urlopen(url, timeout=int(self._timeout)) as resp:
                    data = json.loads(resp.read().decode())
                    return self._extract_path(data, json_path)
            except Exception:
                return None
        except Exception:
            return None

    # ---- File ----

    async def _read_file(self) -> Any:
        file_path = self._source_config.get("path", "")
        json_path = self._source_config.get("json_path", "")
        if not file_path or not os.path.isfile(file_path):
            return None
        try:
            with open(file_path) as f:
                content = f.read().strip()
            if not content:
                return None
            try:
                data = json.loads(content)
                return self._extract_path(data, json_path)
            except json.JSONDecodeError:
                try:
                    return float(content)
                except ValueError:
                    return content
        except Exception as e:
            if self.logger:
                self.logger.debug(f"File read '{self.slot_name}': {e}")
            return None

    # ---- MQTT / Serial / WebSocket (桩) ----

    async def _read_mqtt(self) -> Any:
        if self.logger:
            self.logger.debug(f"MQTT source not yet implemented for '{self.slot_name}'")
        return None

    async def _read_serial(self) -> Any:
        if self.logger:
            self.logger.debug(f"Serial source not yet implemented for '{self.slot_name}'")
        return None

    def _read_websocket(self) -> Any:
        # WebSocket 是写入模式，外部程序通过 API write_slot() 推送数据到缓存
        return None

    # ---- 工具 ----

    @staticmethod
    def _extract_path(data: Any, path: str) -> Any:
        """按点号分隔路径提取 JSON 值，如 'sensors.temperature.value'"""
        if not path:
            return data
        current = data
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current
