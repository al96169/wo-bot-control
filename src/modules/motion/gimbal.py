"""
二轴云台控制模块
支持 PCA9685 I2C 舵机驱动板 / Jetson GPIO PWM / Mock 三种后端
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class GimbalInterface(ABC):
    """云台硬件抽象接口"""

    @abstractmethod
    async def set_angle(self, channel: int, angle: float) -> None:
        """设置舵机角度 (0-180)"""
        ...

    @abstractmethod
    async def release(self) -> None:
        """释放所有舵机（停止 PWM 信号）"""
        ...

    async def close(self) -> None:
        """关闭硬件连接"""
        pass


class MockGimbal(GimbalInterface):
    """Mock 云台后端 - 仅打印日志"""

    def __init__(self, name: str = "mock-gimbal"):
        self.name = name
        self._angles: dict = {0: 90, 1: 90}

    async def set_angle(self, channel: int, angle: float) -> None:
        angle = max(0, min(180, angle))
        self._angles[channel] = angle
        axis = "pan" if channel == 0 else "tilt"
        logger.debug(f"[{self.name}] set_{axis}({angle:.1f}°)")

    async def release(self) -> None:
        logger.info(f"[{self.name}] Gimbal released")

    def get_angles(self) -> dict:
        return dict(self._angles)


class PCA9685Gimbal(GimbalInterface):
    """PCA9685 I2C 舵机驱动板云台

    参数:
        bus: I2C 总线号 (Jetson 通常为 1)
        address: PCA9685 I2C 地址 (默认 0x40)
        pan_channel: 水平舵机通道 (默认 0)
        tilt_channel: 俯仰舵机通道 (默认 1)
        min_pulse: 最小脉冲宽度 μs (默认 500)
        max_pulse: 最大脉冲宽度 μs (默认 2500)
        freq: PWM 频率 Hz (默认 50，标准舵机频率)
    """

    def __init__(
        self,
        bus: int = 1,
        address: int = 0x40,
        pan_channel: int = 0,
        tilt_channel: int = 1,
        min_pulse: int = 500,
        max_pulse: int = 2500,
        freq: int = 50,
    ):
        self.bus = bus
        self.address = address
        self.pan_channel = pan_channel
        self.tilt_channel = tilt_channel
        self.min_pulse = min_pulse
        self.max_pulse = max_pulse
        self.freq = freq
        self._pca = None
        self._current_angles = {pan_channel: 90, tilt_channel: 90}

    def _ensure_init(self):
        if self._pca is None:
            try:
                import board
                import busio
                from adafruit_pca9685 import PCA9685
                from adafruit_motor import servo

                i2c = busio.I2C(board.SCL, board.SDA)
                self._pca = PCA9685(i2c, address=self.address)
                self._pca.frequency = self.freq

                # 创建舵机对象
                self._pan_servo = servo.Servo(
                    self._pca.channels[self.pan_channel],
                    min_pulse=self.min_pulse,
                    max_pulse=self.max_pulse,
                )
                self._tilt_servo = servo.Servo(
                    self._pca.channels[self.tilt_channel],
                    min_pulse=self.min_pulse,
                    max_pulse=self.max_pulse,
                )

                # 初始位置 90°
                self._pan_servo.angle = 90
                self._tilt_servo.angle = 90

                logger.info(
                    f"PCA9685 gimbal initialized: "
                    f"bus={self.bus}, addr=0x{self.address:02X}, "
                    f"pan=ch{self.pan_channel}, tilt=ch{self.tilt_channel}"
                )
            except ImportError as e:
                logger.warning(
                    f"PCA9685 libraries not available ({e}). "
                    f"Install: pip install adafruit-circuitpython-pca9685 adafruit-circuitpython-motor adafruit-circuitpython-busdevice"
                )
                raise
            except Exception as e:
                logger.error(f"PCA9685 init failed: {e}")
                raise

    async def set_angle(self, channel: int, angle: float) -> None:
        self._ensure_init()
        if self._pca is None:
            return

        angle = max(0, min(180, angle))
        self._current_angles[channel] = angle

        try:
            if channel == self.pan_channel:
                self._pan_servo.angle = angle
            elif channel == self.tilt_channel:
                self._tilt_servo.angle = angle
        except Exception as e:
            logger.error(f"PCA9685 set_angle(ch={channel}, {angle}°) failed: {e}")

    async def release(self) -> None:
        if self._pca:
            try:
                self._pca.deinit()
            except Exception:
                pass
            self._pca = None
        logger.info("PCA9685 gimbal released")

    async def close(self) -> None:
        await self.release()


class GPIOPWMGimbal(GimbalInterface):
    """Jetson GPIO PWM 直接控制舵机

    参数:
        pan_pin: 水平舵机 PWM 引脚 (BOARD 编号)
        tilt_pin: 俯仰舵机 PWM 引脚 (BOARD 编号)
        freq: PWM 频率 Hz (默认 50)
    """

    def __init__(self, pan_pin: int = 32, tilt_pin: int = 33, freq: int = 50):
        self.pan_pin = pan_pin
        self.tilt_pin = tilt_pin
        self.freq = freq
        self._gpio = None
        self._pan_pwm = None
        self._tilt_pwm = None
        self._current_angles = {0: 90, 1: 90}

    def _ensure_init(self):
        if self._gpio is None:
            try:
                import Jetson.GPIO as GPIO
                GPIO.setmode(GPIO.BOARD)
                self._gpio = GPIO

                GPIO.setup(self.pan_pin, GPIO.OUT)
                GPIO.setup(self.tilt_pin, GPIO.OUT)

                self._pan_pwm = GPIO.PWM(self.pan_pin, self.freq)
                self._tilt_pwm = GPIO.PWM(self.tilt_pin, self.freq)
                self._pan_pwm.start(self._angle_to_duty(90))
                self._tilt_pwm.start(self._angle_to_duty(90))

                logger.info(
                    f"GPIO PWM gimbal initialized: "
                    f"pan=pin{self.pan_pin}, tilt=pin{self.tilt_pin}, freq={self.freq}Hz"
                )
            except ImportError:
                logger.error("Jetson.GPIO not found. Only available on Jetson platforms.")
                raise

    def _angle_to_duty(self, angle: float) -> float:
        """将角度 (0-180) 转换为占空比 (2.5% - 12.5% for 50Hz)"""
        # 标准舵机: 0.5ms=0° → 1.5ms=90° → 2.5ms=180°
        # 50Hz 周期 = 20ms, duty = pulse_ms / 20 * 100
        pulse_ms = 0.5 + (angle / 180.0) * 2.0  # 0.5ms ~ 2.5ms
        return pulse_ms / 20.0 * 100.0  # 2.5% ~ 12.5%

    async def set_angle(self, channel: int, angle: float) -> None:
        self._ensure_init()
        if self._gpio is None:
            return

        angle = max(0, min(180, angle))
        self._current_angles[channel] = angle
        duty = self._angle_to_duty(angle)

        try:
            if channel == 0:
                self._pan_pwm.ChangeDutyCycle(duty)
            elif channel == 1:
                self._tilt_pwm.ChangeDutyCycle(duty)
        except Exception as e:
            logger.error(f"GPIO PWM set_angle(ch={channel}, {angle}°) failed: {e}")

    async def release(self) -> None:
        if self._pan_pwm:
            self._pan_pwm.stop()
        if self._tilt_pwm:
            self._tilt_pwm.stop()
        if self._gpio:
            self._gpio.cleanup()
            self._gpio = None
        logger.info("GPIO PWM gimbal released")

    async def close(self) -> None:
        await self.release()


class GimbalController:
    """云台控制器 - 管理二轴云台"""

    def __init__(self, config: dict = None, logger_instance=None):
        self.config = config or {}
        self.logger = logger_instance or logger

        # 硬件后端
        self._hardware: GimbalInterface = self._create_hardware()

        # 当前角度
        self.pan_angle = 90.0  # 水平 (0=左, 180=右)
        self.tilt_angle = 90.0  # 俯仰 (0=下, 180=上)

        # 角度限制
        self.pan_min = self.config.get("pan_min", 0)
        self.pan_max = self.config.get("pan_max", 180)
        self.tilt_min = self.config.get("tilt_min", 30)
        self.tilt_max = self.config.get("tilt_max", 150)

    def _create_hardware(self) -> GimbalInterface:
        """根据配置创建硬件后端"""
        gimbal_type = self.config.get("gimbal_type", "mock")

        if gimbal_type == "pca9685":
            return PCA9685Gimbal(
                bus=self.config.get("i2c_bus", 1),
                address=self.config.get("i2c_address", 0x40),
                pan_channel=self.config.get("pan_channel", 0),
                tilt_channel=self.config.get("tilt_channel", 1),
            )
        elif gimbal_type == "gpio_pwm":
            return GPIOPWMGimbal(
                pan_pin=self.config.get("pan_pin", 32),
                tilt_pin=self.config.get("tilt_pin", 33),
            )
        else:
            return MockGimbal(name=self.config.get("robot", {}).get("id", "wobot"))

    async def set_pan(self, angle: float) -> None:
        """设置水平角度 (0-180, 0=最左, 180=最右, 90=居中)"""
        angle = max(self.pan_min, min(self.pan_max, angle))
        self.pan_angle = angle
        await self._hardware.set_angle(0, angle)

    async def set_tilt(self, angle: float) -> None:
        """设置俯仰角度 (0-180, 0=最下, 180=最上, 90=水平)"""
        angle = max(self.tilt_min, min(self.tilt_max, angle))
        self.tilt_angle = angle
        await self._hardware.set_angle(1, angle)

    async def center(self) -> None:
        """云台居中"""
        await self.set_pan(90)
        await self.set_tilt(90)
        self.logger.info("Gimbal centered (pan=90°, tilt=90°)")

    async def stop(self) -> None:
        """停止云台（保持当前位置，释放硬件）"""
        await self._hardware.release()

    def get_state(self) -> dict:
        """获取当前状态"""
        return {
            "pan": self.pan_angle,
            "tilt": self.tilt_angle,
            "pan_range": {"min": self.pan_min, "max": self.pan_max},
            "tilt_range": {"min": self.tilt_min, "max": self.tilt_max},
        }


def create_gimbal(config: dict, logger_instance=None) -> GimbalController:
    """工厂函数：根据配置创建云台控制器"""
    return GimbalController(config.get("gimbal", {}), logger_instance)
