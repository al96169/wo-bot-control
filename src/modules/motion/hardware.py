"""
运动控制硬件输出层
支持 Mock / 串口 / GPIO 三种硬件后端
"""

import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class HardwareInterface(ABC):
    """硬件输出抽象接口"""

    @abstractmethod
    async def set_motor(self, name: str, speed: float) -> None:
        """设置单电机速度 -1.0 ~ 1.0"""
        ...

    async def set_mecanum(self, v_x: float, v_y: float, v_z: float) -> None:
        """麦轮底盘速度控制（默认回退到 set_motor 逐轮控制）"""
        # 默认麦轮运动学分解
        L = 0.5  # 轴距
        W = 0.4  # 轮距
        v_fl = v_x - v_y - v_z * (L + W) / 2
        v_fr = v_x + v_y + v_z * (L + W) / 2
        v_rl = v_x + v_y - v_z * (L + W) / 2
        v_rr = v_x - v_y + v_z * (L + W) / 2
        # 归一化
        max_v = max(abs(v_fl), abs(v_fr), abs(v_rl), abs(v_rr), 1.0)
        await self.set_motor("front_left", v_fl / max_v)
        await self.set_motor("front_right", v_fr / max_v)
        await self.set_motor("rear_left", v_rl / max_v)
        await self.set_motor("rear_right", v_rr / max_v)

    @abstractmethod
    async def set_steering(self, angle: float) -> None:
        """设置转向角度（阿克曼用）-pi/4 ~ pi/4"""
        ...

    @abstractmethod
    async def emergency_stop(self) -> None:
        """硬件急停（断电/刹车）"""
        ...

    @abstractmethod
    async def release(self) -> None:
        """释放急停"""
        ...

    async def close(self) -> None:
        """关闭硬件连接"""
        pass


class MockHardware(HardwareInterface):
    """Mock 硬件后端 - 用于开发/测试，仅打印日志"""

    def __init__(self, name: str = "mock"):
        self.name = name
        self._stopped = False
        self._last_speeds: dict = {}

    async def set_motor(self, name: str, speed: float) -> None:
        if self._stopped:
            logger.debug(f"[{self.name}] set_motor({name}, {speed:.2f}) BLOCKED (emergency)")
            return
        self._last_speeds[name] = speed
        logger.debug(f"[{self.name}] set_motor({name}, {speed:.2f})")

    async def set_steering(self, angle: float) -> None:
        if self._stopped:
            logger.debug(f"[{self.name}] set_steering({angle:.2f}) BLOCKED (emergency)")
            return
        logger.debug(f"[{self.name}] set_steering({angle:.2f})")

    async def emergency_stop(self) -> None:
        self._stopped = True
        logger.warning(f"[{self.name}] HARDWARE EMERGENCY STOP")

    async def release(self) -> None:
        self._stopped = False
        logger.info(f"[{self.name}] Emergency stop released")

    def get_last_speeds(self) -> dict:
        return dict(self._last_speeds)


class SerialHardware(HardwareInterface):
    """串口硬件后端 - 通过 UART 与底层控制板通信"""

    def __init__(self, port: str = "/dev/ttyTHS0", baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._serial = None
        self._stopped = False
        self._lock = asyncio.Lock()

    async def _ensure_serial(self):
        if self._serial is None:
            try:
                import serial_asyncio

                self._serial, _ = await serial_asyncio.open_serial_connection(url=self.port, baudrate=self.baudrate)
                logger.info(f"Serial connected: {self.port} @ {self.baudrate}")
            except ImportError:
                logger.error("pyserial-asyncio not installed. Run: pip install pyserial-asyncio")
                raise
            except Exception as e:
                logger.error(f"Serial open failed ({self.port}): {e}")
                raise

    async def _write(self, data: bytes):
        await self._ensure_serial()
        if self._serial is None:
            return
        async with self._lock:
            self._serial.write(data)
            await self._serial.drain()

    async def set_motor(self, name: str, speed: float) -> None:
        if self._stopped:
            logger.warning(f"Serial: set_motor({name}) blocked by emergency stop")
            return
        # 协议格式: 0xAA [motor_id] [speed_byte] 0x55
        motor_map = {
            "front_left": 0x01,
            "front_right": 0x02,
            "rear_left": 0x03,
            "rear_right": 0x04,
            "left": 0x01,
            "right": 0x02,
        }
        mid = motor_map.get(name)
        if mid is None:
            logger.warning(f"Serial: unknown motor name '{name}'")
            return
        speed_val = max(-100, min(100, int(speed * 100)))
        speed_byte = speed_val & 0xFF if speed_val >= 0 else (256 + speed_val) & 0xFF
        await self._write(bytes([0xAA, mid, speed_byte, 0x55]))

    async def set_steering(self, angle: float) -> None:
        if self._stopped:
            return
        val = int(angle * 180 / 3.14159)
        val = max(-45, min(45, val))
        val_byte = val & 0xFF if val >= 0 else (256 + val) & 0xFF
        await self._write(bytes([0xAA, 0x10, val_byte, 0x55]))

    async def emergency_stop(self) -> None:
        self._stopped = True
        await self._write(bytes([0xAA, 0xFF, 0x00, 0x55]))
        logger.warning("Serial: HARDWARE EMERGENCY STOP sent")

    async def release(self) -> None:
        self._stopped = False
        await self._write(bytes([0xAA, 0xFE, 0x01, 0x55]))
        logger.info("Serial: Emergency stop released")

    async def close(self) -> None:
        if self._serial:
            self._serial.close()
            self._serial = None


class GPIOHardware(HardwareInterface):
    """Jetson GPIO 硬件后端 - 通过 PWM 控制电机"""

    def __init__(self, pins: dict | None = None):
        # 默认引脚映射 (Jetson GPIO)
        self.pins = pins or {
            "front_left": {"pwm": 32, "dir1": 33, "dir2": 35},
            "front_right": {"pwm": 36, "dir1": 37, "dir2": 38},
            "rear_left": {"pwm": 40, "dir1": 41, "dir2": 43},
            "rear_right": {"pwm": 12, "dir1": 13, "dir2": 15},
        }
        self._gpio = None
        self._pwm_channels: dict = {}
        self._stopped = False

    def _ensure_gpio(self):
        if self._gpio is None:
            try:
                import Jetson.GPIO as GPIO

                GPIO.setmode(GPIO.BOARD)
                self._gpio = GPIO

                for wheel_name, pin_set in self.pins.items():
                    pwm_pin = pin_set["pwm"]
                    dir1_pin = pin_set["dir1"]
                    dir2_pin = pin_set["dir2"]

                    GPIO.setup(pwm_pin, GPIO.OUT)
                    GPIO.setup(dir1_pin, GPIO.OUT)
                    GPIO.setup(dir2_pin, GPIO.OUT)

                    pwm = GPIO.PWM(pwm_pin, 1000)  # 1kHz
                    pwm.start(0)
                    self._pwm_channels[wheel_name] = {
                        "pwm": pwm,
                        "dir1": dir1_pin,
                        "dir2": dir2_pin,
                    }

                logger.info(f"GPIO initialized: {len(self.pins)} motors configured")
            except ImportError:
                logger.error("Jetson.GPIO not found. Only available on Jetson platforms.")
                raise

    async def set_motor(self, name: str, speed: float) -> None:
        self._ensure_gpio()
        if self._stopped:
            return
        ch = self._pwm_channels.get(name)
        if ch is None:
            logger.warning(f"GPIO: unknown motor name '{name}'")
            return

        GPIO = self._gpio
        duty = abs(speed) * 100  # 0-100%

        if speed > 0:
            GPIO.output(ch["dir1"], GPIO.HIGH)
            GPIO.output(ch["dir2"], GPIO.LOW)
        elif speed < 0:
            GPIO.output(ch["dir1"], GPIO.LOW)
            GPIO.output(ch["dir2"], GPIO.HIGH)
        else:
            GPIO.output(ch["dir1"], GPIO.LOW)
            GPIO.output(ch["dir2"], GPIO.LOW)

        ch["pwm"].ChangeDutyCycle(duty)

    async def set_steering(self, angle: float) -> None:
        # GPIO 后端一般通过差速实现转向，steering 仅阿克曼底盘需要
        logger.debug(f"GPIO steering not implemented (use differential): {angle:.2f}")

    async def emergency_stop(self) -> None:
        self._ensure_gpio()
        self._stopped = True
        for ch in self._pwm_channels.values():
            ch["pwm"].ChangeDutyCycle(0)
            self._gpio.output(ch["dir1"], self._gpio.LOW)
            self._gpio.output(ch["dir2"], self._gpio.LOW)
        logger.warning("GPIO: HARDWARE EMERGENCY STOP (all motors off)")

    async def release(self) -> None:
        self._stopped = False
        logger.info("GPIO: Emergency stop released")

    async def close(self) -> None:
        if self._gpio:
            for ch in self._pwm_channels.values():
                ch["pwm"].stop()
            self._gpio.cleanup()
            self._gpio = None
            self._pwm_channels.clear()


class RosmasterMotion(HardwareInterface):
    """Rosmaster 串口运动控制后端 — 通过 Rosmaster_Lib 库直接控制麦轮底盘

    使用 Rosmaster.set_car_motion(v_x, v_y, v_z) 发送三轴速度指令。
    X3 底盘: v_x/v_y=[-1.0, 1.0], v_z=[-5, 5]
    """

    def __init__(self, com: str = "/dev/ttyUSB1", car_type: int = 1):
        self.com = com
        self.car_type = car_type
        self._bot = None
        self._stopped = False
        self._lock = asyncio.Lock()  # 保护串口写入，防止多线程并发写串口

    def _ensure_init(self) -> bool:
        if self._bot is not None:
            return True
        try:
            from Rosmaster_Lib import Rosmaster

            self._bot = Rosmaster(car_type=self.car_type, com=self.com)
            # 写入 car_type 到板子（必须，否则板子用默认运动学参数）
            self._bot.set_car_type(self.car_type)
            # 不调 set_auto_report_state(False) — 自动上报必须保持开启，
            # 电池/编码器/速度等数据依赖自动上报帧解析
            # 必须启动接收线程，否则串口接收缓冲区会堵塞，导致所有通信失效
            self._bot.create_receive_threading()
            logger.info(f"Rosmaster motion initialized: com={self.com}, car_type={self.car_type}")
            return True
        except ImportError:
            logger.error("Rosmaster_Lib not found.")
            raise
        except Exception as e:
            logger.warning(f"Rosmaster motion init failed (will retry): {e}")
            return False

    async def set_mecanum(self, v_x: float, v_y: float, v_z: float) -> None:
        """通过 Rosmaster 库直接发送三轴麦轮速度"""
        if self._stopped:
            logger.warning("Rosmaster motion blocked by emergency stop")
            return
        if not self._ensure_init():
            raise RuntimeError(f"Rosmaster hardware not ready (serial: {self.com})")

        # X3 范围限制
        v_x = max(-1.0, min(1.0, v_x))
        v_y = max(-1.0, min(1.0, v_y))
        v_z = max(-5.0, min(5.0, v_z))

        async with self._lock:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._bot.set_car_motion, v_x, v_y, v_z)
            except Exception as e:
                logger.error(f"Rosmaster set_car_motion failed: {e}")

    async def set_motor(self, name: str, speed: float) -> None:
        """回退到 Rosmaster.set_motor(s1, s2, s3, s4)"""
        if self._stopped:
            return
        if not self._ensure_init():
            raise RuntimeError(f"Rosmaster hardware not ready (serial: {self.com})")
        # 暂不支持单轮控制，记录警告
        logger.warning(f"Rosmaster set_motor({name}) not individually supported, use set_mecanum")

    async def set_steering(self, angle: float) -> None:
        pass  # 麦轮无需转向

    async def emergency_stop(self) -> None:
        self._stopped = True
        if self._bot:
            try:
                self._bot.set_car_motion(0, 0, 0)
            except Exception:
                pass
        logger.warning("Rosmaster motion: EMERGENCY STOP")

    async def release(self) -> None:
        self._stopped = False
        logger.info("Rosmaster motion: Emergency stop released")

    async def close(self) -> None:
        self._bot = None


def create_hardware(config: dict) -> HardwareInterface:
    """根据配置创建硬件后端实例"""
    hw_type = config.get("hardware_type", "mock")

    if hw_type == "rosmaster":
        return RosmasterMotion(
            com=config.get("serial_port", "/dev/ttyUSB1"),
            car_type=config.get("car_type", 1),
        )
    elif hw_type == "serial":
        return SerialHardware(
            port=config.get("serial_port", "/dev/ttyTHS0"),
            baudrate=config.get("serial_baudrate", 115200),
        )
    elif hw_type == "gpio":
        return GPIOHardware(pins=config.get("gpio_pins"))
    else:
        return MockHardware(name=config.get("robot", {}).get("id", "wobot"))
