"""
红外遥控扩展模块
与智家物联红外学习模块通信，实现红外码库学习、存储与发射。

通信总线: 串口 (/dev/ttyTHS1, 115200, 8N1)
帧格式:   帧头 68H + 长度(2B, 低前高后) + 模块地址(1B) + 功能码(1B)
          + 数据域(NB) + 校验和(1B) + 帧尾 16H
校验和:   (模块地址 + 功能码 + 数据域) 各字节求和 mod 256
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from .base import ExtensionModule

# pyserial 可选导入（兼容无串口硬件的开发环境）
try:
    import serial as _pyserial

    _SERIAL_AVAILABLE = True
except ImportError:  # pragma: no cover - 开发环境兼容
    _pyserial = None
    _SERIAL_AVAILABLE = False

# ---------- Python 3.7 兼容: asyncio.to_thread 在 3.9+ 才有 ----------
if hasattr(asyncio, "to_thread"):
    _to_thread = asyncio.to_thread
else:
    def _to_thread(func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, lambda: func(*args, **kwargs))

# ---------- 帧常量 ----------
FRAME_HEAD = 0x68
FRAME_TAIL = 0x16

# ---------- 功能码 (AFN) ----------
AFN_REPORT = 0x02  # 上报帧（学习成功后模块主动发送）
AFN_GET_BAUDRATE = 0x04  # 获取波特率
AFN_GET_ADDR = 0x06  # 获取模块地址
AFN_ENTER_LEARN = 0x10  # 进入内部学习模式（带索引 0~6）
AFN_EXIT_LEARN = 0x11  # 退出学习模式
AFN_SEND_CODE = 0x12  # 发送内部存储编码（带索引）
AFN_READ_CODE = 0x18  # 读取内部存储编码（带索引）

# 模块内部存储组数（索引 0~6）
MAX_INDEX = 7


class IRController(ExtensionModule):
    """红外遥控控制器"""

    def __init__(self, config: dict | None = None, logger=None):
        super().__init__("ir", config=config, logger=logger)
        self._serial = None
        self._lock: asyncio.Lock | None = None
        self._addr: int = int(self.config.get("module_addr", 0x00))
        self._learn_timeout: int = int(self.config.get("learn_timeout", 30))
        self._data_dir: Path = Path(self.config.get("data_dir", "data/ir_codes"))

    # ---------- 生命周期 ----------

    async def start(self):
        self.running = True
        self.enabled = True
        self._lock = asyncio.Lock()

        # 码库存储目录
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"IR data dir create failed: {self._data_dir} - {e}")

        # 打开串口（同步操作，放到线程中）
        port = self.config.get("port", "/dev/ttyTHS1")
        baudrate = int(self.config.get("baudrate", 115200))
        await _to_thread(self._open_serial, port, baudrate)

        if self.logger:
            status = "open" if self._serial else "unavailable"
            self.logger.info(f"IRController started (port={port}@{baudrate}, serial={status}, dir={self._data_dir})")

    async def stop(self):
        self.running = False
        self.enabled = False
        if self._serial:
            await _to_thread(self._close_serial)
        if self.logger:
            self.logger.info("IRController stopped")

    def _open_serial(self, port: str, baudrate: int) -> None:
        """打开串口（同步，在线程中调用）"""
        if not _SERIAL_AVAILABLE:
            if self.logger:
                self.logger.warning("pyserial not installed, IR serial unavailable")
            self._serial = None
            return
        try:
            self._serial = _pyserial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1,
            )
            if self.logger:
                self.logger.info(f"IR serial port opened: {port}@{baudrate}")
        except Exception as e:
            self._serial = None
            if self.logger:
                self.logger.warning(f"IR serial port open failed: {port} - {e}")

    def _close_serial(self) -> None:
        """关闭串口（同步，在线程中调用）"""
        try:
            if self._serial:
                self._serial.close()
        except Exception:
            pass
        self._serial = None

    # ---------- 串口通信层 ----------

    def build_frame(self, addr: int, afn: int, data: bytes = b"") -> bytes:
        """组帧: 帧头 + 长度(2B低前高后) + 模块地址 + 功能码 + 数据域 + 校验和 + 帧尾

        长度 = 整帧所有字节的长度 (帧头 + 长度 + 模块地址 + 功能码 + 数据域 + 校验和 + 帧尾)
        校验和 = (模块地址 + 功能码 + 数据域) 各字节求和 mod 256
        """
        payload = bytes([addr & 0xFF, afn & 0xFF]) + bytes(data)
        # 整帧总长度 = 帧头(1) + 长度(2) + payload + 校验和(1) + 帧尾(1)
        total_length = 5 + len(payload)
        checksum = sum(payload) % 256
        return (
            bytes([FRAME_HEAD])
            + total_length.to_bytes(2, "little")
            + payload
            + bytes([checksum])
            + bytes([FRAME_TAIL])
        )

    def read_frame(self, ser, timeout: float = 2.0) -> tuple[int, int, bytes] | None:
        """读取并解析完整帧（同步，在线程中调用）

        返回 (模块地址, 功能码, 数据域) 或 None（超时/校验失败）
        """
        if ser is None:
            return None
        try:
            ser.timeout = timeout
            # 1. 寻找帧头 0x68
            while True:
                b = ser.read(1)
                if not b:
                    return None  # 超时
                if b[0] == FRAME_HEAD:
                    break

            # 2. 读取长度（2B, 低前高后）= 整帧总长度
            length_bytes = ser.read(2)
            if len(length_bytes) < 2:
                return None
            total_length = int.from_bytes(length_bytes, "little")
            if total_length < 7:  # 最小帧: 头(1)+长度(2)+地址(1)+功能码(1)+校验(1)+尾(1)
                return None

            # 3. 读取数据域 (模块地址 + 功能码 + 数据域)
            #    payload 长度 = 总长度 - 帧头(1) - 长度(2) - 校验和(1) - 帧尾(1)
            payload_length = total_length - 5
            payload = ser.read(payload_length)
            if len(payload) < payload_length:
                return None

            # 4. 读取校验和
            checksum_byte = ser.read(1)
            if len(checksum_byte) < 1:
                return None

            # 5. 读取帧尾
            tail = ser.read(1)
            if len(tail) < 1:
                return None
            if tail[0] != FRAME_TAIL:
                return None  # 帧尾错误

            # 6. 校验和验证
            expected = sum(payload) % 256
            if expected != checksum_byte[0]:
                if self.logger:
                    self.logger.warning(
                        f"IR frame checksum mismatch: expected={expected:#04x} got={checksum_byte[0]:#04x}"
                    )
                return None

            addr = payload[0]
            afn = payload[1]
            data = payload[2:]
            if self.logger:
                hex_str = " ".join(f"{b:02X}" for b in (bytes([FRAME_HEAD]) + total_length.to_bytes(2, "little") + payload + checksum_byte + tail))
                self.logger.debug(f"IR RX: {hex_str}")
            return (addr, afn, data)
        except Exception as e:
            if self.logger:
                self.logger.warning(f"IR read_frame error: {e}")
            return None

    def _send_frame_sync(self, addr: int, afn: int, data: bytes = b"") -> None:
        """发送一帧（同步，在线程中调用）"""
        if self._serial is None:
            raise RuntimeError("IR serial port not available")
        frame = self.build_frame(addr, afn, data)
        if self.logger:
            hex_str = " ".join(f"{b:02X}" for b in frame)
            self.logger.debug(f"IR TX: {hex_str}")
        self._serial.write(frame)
        self._serial.flush()

    def _read_frame_sync(self, timeout: float = 2.0) -> tuple[int, int, bytes] | None:
        """读取一帧（同步，在线程中调用）"""
        return self.read_frame(self._serial, timeout)

    async def _transaction(
        self, afn: int, data: bytes = b"", timeout: float = 2.0
    ) -> tuple[int, int, bytes] | None:
        """发送请求并读取一帧响应（线程安全，asyncio.Lock 保护串口访问）"""
        async with self._lock:
            return await _to_thread(self._transaction_sync, afn, data, timeout)

    def _transaction_sync(
        self, afn: int, data: bytes, timeout: float
    ) -> tuple[int, int, bytes] | None:
        """请求-响应事务（同步）"""
        # 清空输入缓冲区中的残留数据
        try:
            if self._serial and self._serial.in_waiting > 0:
                self._serial.read(self._serial.in_waiting)
        except Exception:
            pass
        self._send_frame_sync(self._addr, afn, data)
        return self._read_frame_sync(timeout)

    def _wait_report_sync(self, timeout: float) -> tuple[int, int, bytes] | None:
        """循环等待上报帧 (AFN=02H)，直到超时（同步）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            frame = self.read_frame(self._serial, min(remaining, 1.0))
            if frame is None:
                continue
            _addr, afn, _data = frame
            if afn == AFN_REPORT:
                return frame
        return None

    def _require_serial(self) -> dict | None:
        """检查串口是否可用，不可用则返回错误响应"""
        if self._serial is None:
            return {
                "type": "error",
                "data": {"code": 503, "message": "IR serial port not available"},
            }
        return None

    # ---------- 功能码实现 ----------

    async def get_baudrate(self) -> dict | None:
        """AFN=04H 获取波特率"""
        err = self._require_serial()
        if err:
            return None
        resp = await self._transaction(AFN_GET_BAUDRATE, b"", timeout=2.0)
        if resp is None:
            return None
        _addr, _afn, data = resp
        return {"raw": list(data)}

    async def get_module_addr(self) -> int | None:
        """AFN=06H 获取模块地址"""
        err = self._require_serial()
        if err:
            return None
        resp = await self._transaction(AFN_GET_ADDR, b"", timeout=2.0)
        if resp is None:
            return None
        _addr, _afn, data = resp
        return data[0] if data else None

    async def _learn_transaction(self, index: int, timeout: int) -> bytes | None:
        """学习事务：进入学习模式 → 等待上报 → 退出学习 → 读取编码

        线程安全：整个事务在 asyncio.Lock 内完成，避免学习期间被其他串口操作打断。
        """
        async with self._lock:
            return await _to_thread(self._learn_sync, index, timeout)

    def _learn_sync(self, index: int, timeout: int) -> bytes | None:
        """学习流程（同步，在线程中调用）"""
        # 0. 清空串口输入缓冲区，避免残留数据干扰
        try:
            if self._serial and self._serial.in_waiting > 0:
                discarded = self._serial.read(self._serial.in_waiting)
                if self.logger:
                    self.logger.debug(f"IR learn: flushed {len(discarded)} bytes from input buffer")
        except Exception:
            pass

        # 1. 进入内部学习模式
        if self.logger:
            self.logger.info(f"IR learn: sending AFN=10H (enter learn mode), index={index}")
        self._send_frame_sync(self._addr, AFN_ENTER_LEARN, bytes([index & 0xFF]))

        # 1.5 读取应答帧 (AFN=01H)，确认模块已接受命令
        ack = self._read_frame_sync(2.0)
        if ack is not None:
            _addr, ack_afn, ack_data = ack
            if self.logger:
                self.logger.info(f"IR learn: ACK received AFN={ack_afn:#04x} data={ack_data.hex()}")
            if ack_afn == 0x01 and len(ack_data) >= 1 and ack_data[0] == 0x01:
                if self.logger:
                    self.logger.warning("IR learn: module rejected command (status=1, busy or error)")
                # 模块拒绝，直接退出
                try:
                    self._send_frame_sync(self._addr, AFN_EXIT_LEARN, b"")
                except Exception:
                    pass
                return None
        else:
            if self.logger:
                self.logger.warning("IR learn: no ACK received for AFN=10H, continuing anyway...")

        # 2. 等待上报帧（模块学习成功后主动发送 AFN=02H）
        if self.logger:
            self.logger.info(f"IR learn: waiting for report (AFN=02H), timeout={timeout}s")
        report = self._wait_report_sync(float(timeout))
        if self.logger:
            self.logger.info(f"IR learn: report={'received' if report else 'timeout'}")

        # 3. 无论是否成功都退出学习模式
        try:
            self._send_frame_sync(self._addr, AFN_EXIT_LEARN, b"")
            # 读取并丢弃退出学习的 ACK 帧 (AFN=01H)，避免后续读取编码时读到残留的 ACK
            exit_ack = self._read_frame_sync(1.0)
            if exit_ack and self.logger:
                _a, _f, _d = exit_ack
                self.logger.debug(f"IR learn: exit ACK consumed AFN={_f:#04x}")
        except Exception:
            pass

        if report is None:
            return None  # 学习超时

        # 4. 读取内部存储编码
        if self.logger:
            self.logger.info(f"IR learn: reading stored code, index={index}")
        self._send_frame_sync(self._addr, AFN_READ_CODE, bytes([index & 0xFF]))
        code_frame = self._read_frame_sync(2.0)
        if code_frame is None:
            if self.logger:
                self.logger.warning("IR learn: failed to read stored code")
            return None
        _addr, _afn, payload = code_frame
        if self.logger:
            self.logger.info(f"IR learn: code read success, length={len(payload)}")
        return payload

    async def send_code(self, index: int) -> bool:
        """AFN=12H 发送内部存储编码（带索引）"""
        err = self._require_serial()
        if err:
            return False
        resp = await self._transaction(AFN_SEND_CODE, bytes([index & 0xFF]), timeout=5.0)
        return resp is not None

    # ---------- 码库存储 ----------

    def _device_path(self, device_id: str) -> Path:
        return self._data_dir / f"{device_id}.json"

    def _load_device(self, device_id: str) -> dict | None:
        return self._load_device_file(self._device_path(device_id))

    def _load_device_file(self, path: Path) -> dict | None:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            if self.logger:
                self.logger.warning(f"Failed to load device {path}: {e}")
            return None

    def _save_device(self, device: dict) -> None:
        path = self._device_path(device["device_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(device, f, ensure_ascii=False, indent=2)

    def _iter_all_devices(self) -> list[dict]:
        devices = []
        for path in sorted(self._data_dir.glob("*.json")):
            dev = self._load_device_file(path)
            if dev:
                devices.append(dev)
        return devices

    def _alloc_index(self) -> int | None:
        """分配一个空闲的内部存储索引 (0~6)。

        模块内部存储只有 7 组（索引 0~6），全局共享，需扫描所有设备已用索引。
        """
        used: set[int] = set()
        for dev in self._iter_all_devices():
            for b in dev.get("buttons", []):
                idx = b.get("index")
                if isinstance(idx, int):
                    used.add(idx)
        for i in range(MAX_INDEX):
            if i not in used:
                return i
        return None

    @staticmethod
    def _new_device_id(category: str) -> str:
        return f"dev_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    @staticmethod
    def _new_button_id() -> str:
        return f"btn_{uuid.uuid4().hex[:8]}"

    # ---------- 命令处理 ----------

    async def handle_command(self, command: str, data: dict) -> dict:
        handler = getattr(self, f"_cmd_{command}", None)
        if handler:
            try:
                return await handler(data)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"IR command '{command}' failed: {e}", exc_info=True)
                return {"type": "error", "data": {"code": 500, "message": str(e)}}
        return {
            "type": "error",
            "data": {"code": 404, "message": f"Unknown IR command: {command}"},
        }

    async def _cmd_device_list(self, data: dict) -> dict:
        """返回所有设备列表（含按键信息，不含 code_hex）"""
        devices = []
        for dev in self._iter_all_devices():
            buttons = [
                {
                    "id": b.get("id", ""),
                    "name": b.get("name", ""),
                    "index": b.get("index", 0),
                    "code_length": b.get("code_length", 0),
                    "type": b.get("type", "stateless"),
                }
                for b in dev.get("buttons", [])
            ]
            devices.append(
                {
                    "device_id": dev.get("device_id", ""),
                    "name": dev.get("name", ""),
                    "brand": dev.get("brand", ""),
                    "model": dev.get("model", ""),
                    "category": dev.get("category", ""),
                    "state": dev.get("state", "off"),
                    "button_count": len(buttons),
                    "buttons": buttons,
                }
            )
        return {"type": "ir_device_list_result", "data": {"devices": devices}}

    async def _cmd_device_add(self, data: dict) -> dict:
        """新增设备 {name, brand, model, category}"""
        name = data.get("name", "未命名设备")
        brand = data.get("brand", "")
        model = data.get("model", "")
        category = data.get("category", "other")
        device_id = self._new_device_id(category)
        device = {
            "device_id": device_id,
            "name": name,
            "brand": brand,
            "model": model,
            "category": category,
            "state": "off",
            "buttons": [],
        }
        self._save_device(device)
        if self.logger:
            self.logger.info(f"IR device added: {device_id} ({name})")
        return {"type": "ir_device_add_result", "data": {"success": True, "device": device}}

    async def _cmd_device_delete(self, data: dict) -> dict:
        """删除设备 {device_id}"""
        device_id = data.get("device_id")
        if not device_id:
            return {"type": "error", "data": {"code": 400, "message": "缺少 device_id"}}
        path = self._device_path(device_id)
        if not path.exists():
            return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}
        path.unlink()
        if self.logger:
            self.logger.info(f"IR device deleted: {device_id}")
        return {"type": "ir_device_delete_result", "data": {"device_id": device_id, "success": True}}

    async def _cmd_device_update(self, data: dict) -> dict:
        """更新设备 {device_id, name?, brand?, model?, category?, state?}"""
        device_id = data.get("device_id")
        if not device_id:
            return {"type": "error", "data": {"code": 400, "message": "缺少 device_id"}}
        device = self._load_device(device_id)
        if not device:
            return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}
        for field in ("name", "brand", "model", "category", "state"):
            if field in data:
                device[field] = data[field]
        self._save_device(device)
        if self.logger:
            self.logger.info(f"IR device updated: {device_id}")
        return {"type": "ir_device_update_result", "data": {"success": True, "device": device}}

    async def _cmd_button_list(self, data: dict) -> dict:
        """获取按键列表 {device_id}"""
        device_id = data.get("device_id")
        if not device_id:
            return {"type": "error", "data": {"code": 400, "message": "缺少 device_id"}}
        device = self._load_device(device_id)
        if not device:
            return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}
        return {
            "type": "ir_button_list_result",
            "data": {"device_id": device_id, "buttons": device.get("buttons", [])},
        }

    async def _cmd_learn_start(self, data: dict) -> dict:
        """开始学习 {device_id, button_name?, button_type?}

        流程: 分配索引 → 进入学习模式 → 等待上报（超时 learn_timeout 秒）
              → 退出学习 → 读取编码 → 存储为按键
        """
        device_id = data.get("device_id")
        if not device_id:
            return {"type": "error", "data": {"code": 400, "message": "缺少 device_id"}}
        device = self._load_device(device_id)
        if not device:
            return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}

        err = self._require_serial()
        if err:
            return err

        # 分配内部存储索引（0~6，全局共享）
        index = self._alloc_index()
        if index is None:
            return {
                "type": "error",
                "data": {"code": 409, "message": "模块内部存储已满（最多 7 组编码）"},
            }

        if self.logger:
            self.logger.info(f"IR learn start: device={device_id} index={index}")

        # 执行学习事务（异步等待上报，超时 learn_timeout 秒）
        code_bytes = await self._learn_transaction(index, self._learn_timeout)
        if code_bytes is None:
            return {
                "type": "ir_learn_result",
                "data": {
                    "device_id": device_id,
                    "success": False,
                    "message": f"学习超时（{self._learn_timeout}s）或读取编码失败",
                },
            }

        code_hex = " ".join(f"{b:02X}" for b in code_bytes)
        button_name = data.get("button_name", f"按键{len(device.get('buttons', [])) + 1}")
        button_type = data.get("button_type", "stateless")
        button = {
            "id": self._new_button_id(),
            "name": button_name,
            "index": index,
            "code_hex": code_hex,
            "code_length": len(code_bytes),
            "type": button_type,
        }
        device.setdefault("buttons", []).append(button)
        self._save_device(device)

        if self.logger:
            self.logger.info(
                f"IR learn success: device={device_id} button={button['id']} "
                f"index={index} length={button['code_length']}"
            )
        return {
            "type": "ir_learn_result",
            "data": {"device_id": device_id, "success": True, "button": button},
        }

    async def _cmd_button_rename(self, data: dict) -> dict:
        """重命名按键 {device_id, button_id, name}"""
        device_id = data.get("device_id")
        button_id = data.get("button_id")
        name = data.get("name")
        if not device_id or not button_id or not name:
            return {"type": "error", "data": {"code": 400, "message": "缺少 device_id/button_id/name"}}
        device = self._load_device(device_id)
        if not device:
            return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}
        button = next((b for b in device.get("buttons", []) if b.get("id") == button_id), None)
        if not button:
            return {"type": "error", "data": {"code": 404, "message": f"按键不存在: {button_id}"}}
        button["name"] = name
        self._save_device(device)
        return {"type": "ir_button_rename_result", "data": {"success": True, "device_id": device_id, "button": button}}

    async def _cmd_button_delete(self, data: dict) -> dict:
        """删除按键 {device_id, button_id}"""
        device_id = data.get("device_id")
        button_id = data.get("button_id")
        if not device_id or not button_id:
            return {"type": "error", "data": {"code": 400, "message": "缺少 device_id/button_id"}}
        device = self._load_device(device_id)
        if not device:
            return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}
        before = len(device.get("buttons", []))
        device["buttons"] = [b for b in device.get("buttons", []) if b.get("id") != button_id]
        if len(device["buttons"]) == before:
            return {"type": "error", "data": {"code": 404, "message": f"按键不存在: {button_id}"}}
        self._save_device(device)
        return {
            "type": "ir_button_delete_result",
            "data": {"device_id": device_id, "button_id": button_id, "success": True},
        }

    async def _cmd_send(self, data: dict) -> dict:
        """发射信号 {device_id, button_id} → 发 AFN=12H"""
        device_id = data.get("device_id")
        button_id = data.get("button_id")
        if not device_id or not button_id:
            return {"type": "error", "data": {"code": 400, "message": "缺少 device_id/button_id"}}
        device = self._load_device(device_id)
        if not device:
            return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}
        button = next((b for b in device.get("buttons", []) if b.get("id") == button_id), None)
        if not button:
            return {"type": "error", "data": {"code": 404, "message": f"按键不存在: {button_id}"}}

        err = self._require_serial()
        if err:
            return err

        index = int(button.get("index", 0))
        ok = await self.send_code(index)
        if not ok:
            return {
                "type": "error",
                "data": {"code": 500, "message": "红外发射失败（模块无响应）"},
            }

        # toggle 类型按键切换设备状态
        if button.get("type") == "toggle":
            device["state"] = "on" if device.get("state") != "on" else "off"
            self._save_device(device)

        if self.logger:
            self.logger.info(f"IR send: device={device_id} button={button_id} index={index}")
        return {
            "type": "ir_send_result",
            "data": {
                "device_id": device_id,
                "button_id": button_id,
                "success": True,
                "new_state": device.get("state"),
            },
        }

    async def _cmd_codes_export(self, data: dict) -> dict:
        """导出 {device_id?} → 返回 JSON 数据

        不指定 device_id 时导出全部设备。
        """
        device_id = data.get("device_id")
        if device_id:
            device = self._load_device(device_id)
            if not device:
                return {"type": "error", "data": {"code": 404, "message": f"设备不存在: {device_id}"}}
            return {"type": "ir_export_result", "data": {"success": True, "devices": [device]}}
        return {"type": "ir_export_result", "data": {"success": True, "devices": self._iter_all_devices()}}

    async def _cmd_codes_import(self, data: dict) -> dict:
        """导入 {data, conflict_policy}

        conflict_policy: skip(跳过, 默认) | overwrite(覆盖) | rename(重命名)
        data: {"devices": [...]} 或单个设备对象
        """
        import_data: Any = data.get("data")
        conflict_policy = data.get("conflict_policy", "skip")
        if not import_data or not isinstance(import_data, dict):
            return {"type": "error", "data": {"code": 400, "message": "缺少导入数据 data"}}

        devices = import_data.get("devices", [])
        if not devices and "device_id" in import_data:
            devices = [import_data]
        if not isinstance(devices, list):
            return {"type": "error", "data": {"code": 400, "message": "data.devices 格式无效"}}

        result = {"imported": 0, "skipped": 0, "overwritten": 0, "renamed": 0, "errors": []}
        imported_devices: list[dict] = []
        for dev in devices:
            if not isinstance(dev, dict) or not dev.get("device_id"):
                result["errors"].append("无效的设备数据（缺少 device_id）")
                continue
            device_id = dev["device_id"]
            path = self._device_path(device_id)
            exists = path.exists()

            if exists:
                if conflict_policy == "skip":
                    result["skipped"] += 1
                    continue
                if conflict_policy == "rename":
                    device_id = self._new_device_id(dev.get("category", "other"))
                    dev["device_id"] = device_id
                    result["renamed"] += 1
                # overwrite: 直接覆盖
            try:
                self._save_device(dev)
                imported_devices.append(dev)
                if exists and conflict_policy == "overwrite":
                    result["overwritten"] += 1
                else:
                    result["imported"] += 1
            except Exception as e:
                result["errors"].append(f"{device_id}: {e}")

        if self.logger:
            self.logger.info(f"IR codes import: {result}")
        return {
            "type": "ir_import_result",
            "data": {
                "success": len(result["errors"]) == 0,
                "imported_count": result["imported"] + result["overwritten"] + result["renamed"],
                "devices": imported_devices,
                "detail": result,
            },
        }
