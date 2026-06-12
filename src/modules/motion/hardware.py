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
                self._serial, _ = await serial_asyncio.open_serial_connection(
                    url=self.port, baudrate=self.baudrate
                )
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
        motor_map = {"front_left": 0x01, "front_right": 0x02, "rear_left": 0x03, "rear_right": 0x04, "left": 0x01, "right": 0x02}
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

    def __init__(self, pins: dict = None):
        # 默认引脚映射 (Jetson GPIO)
        self.pins = pins or {
            "front_left":  {"pwm": 32, "dir1": 33, "dir2": 35},
            "front_right": {"pwm": 36, "dir1": 37, "dir2": 38},
            "rear_left":   {"pwm": 40, "dir1": 41, "dir2": 43},
            "rear_right":  {"pwm": 12, "dir1": 13, "dir2": 15},
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


def create_hardware(config: dict) -> HardwareInterface:
    """根据配置创建硬件后端实例"""
    hw_type = config.get("hardware_type", "mock")

    if hw_type == "serial":
        return SerialHardware(
            port=config.get("serial_port", "/dev/ttyTHS0"),
            baudrate=config.get("serial_baudrate", 115200),
        )
    elif hw_type == "gpio":
        return GPIOHardware(pins=config.get("gpio_pins"))
    else:
        return MockHardware(name=config.get("robot", {}).get("id", "wobot"))
