"""
二轴云台控制模块
支持 PCA9685 I2C / Jetson GPIO PWM / Rosmaster 串口 / Mock 四种后端
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("wobot.motion.gimbal")


class GimbalInterface(ABC):
    """云台硬件抽象接口"""

    @abstractmethod
    async def set_angle(self, channel: int, angle: float) -> None:
        """设置舵机角度 (0-180)"""
        ...

    async def set_angles(self, pan_angle: float, tilt_angle: float) -> None:
        """同时设置水平和俯仰角度（默认回退到两次 set_angle）"""
        await self.set_angle(0, pan_angle)
        await self.set_angle(1, tilt_angle)

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
                from adafruit_motor import servo
                from adafruit_pca9685 import PCA9685

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
                logger.error(f"PCA9685 init failed: {e}", exc_info=True)
                raise

    async def set_angle(self, channel: int, angle: float) -> None:
        self._ensure_init()
        if self._pca is None:
            return

        angle = int(max(0, min(180, angle)))
        self._current_angles[channel] = angle

        try:
            if channel == self.pan_channel:
                self._pan_servo.angle = angle
            elif channel == self.tilt_channel:
                self._tilt_servo.angle = angle
        except Exception as e:
            logger.error(f"PCA9685 set_angle(ch={channel}, {angle}°) failed: {e}", exc_info=True)

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
                    f"GPIO PWM gimbal initialized: pan=pin{self.pan_pin}, tilt=pin{self.tilt_pin}, freq={self.freq}Hz"
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
            logger.error(f"GPIO PWM set_angle(ch={channel}, {angle}°) failed: {e}", exc_info=True)

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


class RosmasterGimbal(GimbalInterface):
    """通过亚博 Rosmaster_Lib 串口驱动库控制 PWM 舵机

    参数:
        com: 串口设备路径 (默认 /dev/myserial)
        car_type: 小车类型 (默认 1)
        pan_channel: 水平舵机 servo_id (默认 4)
        tilt_channel: 俯仰舵机 servo_id (默认 3)
    """

    def __init__(self, com: str = "/dev/myserial", car_type: int = 1, pan_channel: int = 4, tilt_channel: int = 3):
        self.com = com
        self.car_type = car_type
        self.pan_channel = pan_channel
        self.tilt_channel = tilt_channel
        self._bot = None
        self._current_angles = {0: 90, 1: 90}

    def _ensure_init(self) -> bool:
        """初始化硬件，返回 True 表示成功（_bot 可用）"""
        if self._bot is not None:
            return True
        try:
            from Rosmaster_Lib import Rosmaster

            self._bot = Rosmaster(car_type=self.car_type, com=self.com)
            self._bot.set_car_type(self.car_type)
            # 启动接收线程（串口缓冲区必须被消费，否则通信会堵塞）。
            # __uart_state 是类变量，多实例共享同一个串口时不会重复创建线程。
            self._bot.create_receive_threading()
            self._bot.set_pwm_servo(self.pan_channel, 90)
            self._bot.set_pwm_servo(self.tilt_channel, 90)
            logger.info(
                f"Rosmaster gimbal initialized: com={self.com}, "
                f"car_type={self.car_type}, pan=servo{self.pan_channel}, tilt=servo{self.tilt_channel}"
            )
            return True
        except ImportError:
            logger.error("Rosmaster_Lib not found. Install the Yahboom Rosmaster driver library.")
            return False
        except Exception as e:
            # 不抛异常，保留 _bot=None 允许下次调用时重试
            # 典型场景：ROS 串口板在服务启动后才插入
            logger.warning(f"Rosmaster gimbal init failed (will retry): {e}")
            return False

    async def set_angle(self, channel: int, angle: float) -> None:
        if not self._ensure_init():
            return

        angle = int(max(0, min(180, angle)))
        self._current_angles[channel] = angle
        servo_id = self.pan_channel if channel == 0 else self.tilt_channel

        try:
            await asyncio.get_event_loop().run_in_executor(None, self._bot.set_pwm_servo, servo_id, int(angle))
        except Exception as e:
            logger.error(f"Rosmaster set_angle(ch={channel}, servo={servo_id}, {angle}°) failed: {e}", exc_info=True)

    async def set_angles(self, pan_angle: float, tilt_angle: float) -> None:
        """同时设置 pan 和 tilt — 单次 run_in_executor 避免双倍开销"""
        if not self._ensure_init():
            return

        pan = int(max(0, min(180, pan_angle)))
        tilt = int(max(0, min(180, tilt_angle)))
        self._current_angles[0] = pan
        self._current_angles[1] = tilt

        def _write_both():
            self._bot.set_pwm_servo(self.pan_channel, int(pan))
            self._bot.set_pwm_servo(self.tilt_channel, int(tilt))

        try:
            await asyncio.get_event_loop().run_in_executor(None, _write_both)
        except Exception as e:
            logger.error(f"Rosmaster set_angles(pan={pan}°, tilt={tilt}°) failed: {e}", exc_info=True)

    def set_angles_sync(self, pan_angle: float, tilt_angle: float) -> None:
        """同步版双轴写入 — 用于 executor 线程内直接调用，避免逐 tick executor 调度抖动
        round() 替代 int() 消除 0.75°/tick 时的节拍效应（每4tick卡一次），
        set_pwm_servo_all 单包发送替代两次 set_pwm_servo 节省 2ms sleep"""
        if not self._ensure_init():
            return
        self._current_angles[0] = int(max(0, min(180, pan_angle)))
        self._current_angles[1] = int(max(0, min(180, tilt_angle)))
        # 四路角度: s1,s2 未用=255, s3=tilt, s4=pan
        s1 = s2 = 255
        s3 = round(tilt_angle) if self.tilt_channel == 3 else 255
        s4 = round(pan_angle) if self.pan_channel == 4 else 255
        self._bot.set_pwm_servo_all(s1, s2, s3, s4)

    async def release(self) -> None:
        if self._bot:
            self._bot = None
        logger.info("Rosmaster gimbal released")

    async def close(self) -> None:
        await self.release()


class GimbalController:
    """云台控制器 - 管理二轴云台"""

    def __init__(self, config: dict | None = None, logger_instance=None):
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

        # 回中位置（可配置，默认为范围中点）
        self.pan_center = self.config.get("pan_center", (self.pan_min + self.pan_max) / 2)
        self.tilt_center = self.config.get("tilt_center", (self.tilt_min + self.tilt_max) / 2)

        # 方向反转
        self.pan_invert = self.config.get("pan_invert", False)
        self.tilt_invert = self.config.get("tilt_invert", False)

        # 限位回调: async fn(axis: str, limit: float, direction: str)
        self.on_limit: Callable[..., Any] | None = None

    def _create_hardware(self) -> GimbalInterface:
        """根据配置创建硬件后端"""
        gimbal_type = self.config.get("gimbal_type", "mock")

        if gimbal_type == "rosmaster":
            return RosmasterGimbal(
                com=self.config.get("com", "/dev/myserial"),
                car_type=self.config.get("car_type", 1),
                pan_channel=self.config.get("pan_channel", 4),
                tilt_channel=self.config.get("tilt_channel", 3),
            )
        elif gimbal_type == "pca9685":
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
        actual = 180 - angle if self.pan_invert else angle
        # 先写硬件，成功后再更新内部状态
        await self._hardware.set_angle(0, actual)
        old_angle, self.pan_angle = self.pan_angle, angle
        await self._check_limit("pan", old_angle, angle)

    async def set_tilt(self, angle: float) -> None:
        """设置俯仰角度 (0-180, 0=最下, 180=最上, 90=水平)"""
        angle = max(self.tilt_min, min(self.tilt_max, angle))
        actual = 180 - angle if self.tilt_invert else angle
        # 先写硬件，成功后再更新内部状态
        await self._hardware.set_angle(1, actual)
        old_angle, self.tilt_angle = self.tilt_angle, angle
        await self._check_limit("tilt", old_angle, angle)

    async def _check_limit(self, axis: str, old: float, new: float):
        """检查是否到达限位，触发回调"""
        if not self.on_limit:
            return
        if axis == "pan":
            if new == self.pan_min and old > self.pan_min:
                await self.on_limit("pan", self.pan_min, "min")
            elif new == self.pan_max and old < self.pan_max:
                await self.on_limit("pan", self.pan_max, "max")
        elif axis == "tilt":
            if new == self.tilt_min and old > self.tilt_min:
                await self.on_limit("tilt", self.tilt_min, "min")
            elif new == self.tilt_max and old < self.tilt_max:
                await self.on_limit("tilt", self.tilt_max, "max")

    async def move_pan(self, delta: float, step: float = 1.0) -> dict:
        """增量移动水平角度，返回 {changed, pan, tilt, limit?}"""
        if abs(delta) < 0.01:
            return {"changed": False, "pan": self.pan_angle, "tilt": self.tilt_angle}
        new_angle = self.pan_angle + delta * step
        old = self.pan_angle
        clamped = max(self.pan_min, min(self.pan_max, new_angle))
        if abs(clamped - old) < 0.01:
            return {"changed": False, "pan": self.pan_angle, "tilt": self.tilt_angle, "limit": True}
        actual = 180 - clamped if self.pan_invert else clamped
        # 先写硬件，成功后再更新内部状态
        await self._hardware.set_angle(0, actual)
        self.pan_angle = clamped
        result = {"changed": True, "pan": self.pan_angle, "tilt": self.tilt_angle}
        if clamped == self.pan_min and old > self.pan_min:
            result["limit"] = True
            if self.on_limit:
                await self.on_limit("pan", self.pan_min, "min")
        elif clamped == self.pan_max and old < self.pan_max:
            result["limit"] = True
            if self.on_limit:
                await self.on_limit("pan", self.pan_max, "max")
        return result

    async def move_tilt(self, delta: float, step: float = 1.0) -> dict:
        """增量移动俯仰角度，返回 {changed, pan, tilt, limit?}"""
        if abs(delta) < 0.01:
            return {"changed": False, "pan": self.pan_angle, "tilt": self.tilt_angle}
        new_angle = self.tilt_angle + delta * step
        old = self.tilt_angle
        clamped = max(self.tilt_min, min(self.tilt_max, new_angle))
        if abs(clamped - old) < 0.01:
            return {"changed": False, "pan": self.pan_angle, "tilt": self.tilt_angle, "limit": True}
        actual = 180 - clamped if self.tilt_invert else clamped
        # 先写硬件，成功后再更新内部状态
        await self._hardware.set_angle(1, actual)
        self.tilt_angle = clamped
        result = {"changed": True, "pan": self.pan_angle, "tilt": self.tilt_angle}
        if clamped == self.tilt_min and old > self.tilt_min:
            result["limit"] = True
            if self.on_limit:
                await self.on_limit("tilt", self.tilt_min, "min")
        elif clamped == self.tilt_max and old < self.tilt_max:
            result["limit"] = True
            if self.on_limit:
                await self.on_limit("tilt", self.tilt_max, "max")
        return result

    async def move_pan_tilt(self, pan_delta: float, tilt_delta: float, step: float = 1.0) -> dict:
        """同时增量移动两轴 — 单次串口写入，避免两次 run_in_executor 延迟叠加"""
        # 计算新角度并限位
        pan_new = self.pan_angle + pan_delta * step
        tilt_new = self.tilt_angle + tilt_delta * step
        pan_clamped = max(self.pan_min, min(self.pan_max, pan_new))
        tilt_clamped = max(self.tilt_min, min(self.tilt_max, tilt_new))

        pan_old, tilt_old = self.pan_angle, self.tilt_angle

        pan_actual = 180 - pan_clamped if self.pan_invert else pan_clamped
        tilt_actual = 180 - tilt_clamped if self.tilt_invert else tilt_clamped

        # 先写硬件，成功后再更新内部状态
        await self._hardware.set_angles(pan_actual, tilt_actual)
        self.pan_angle = pan_clamped
        self.tilt_angle = tilt_clamped

        result = {"changed": True, "pan": self.pan_angle, "tilt": self.tilt_angle}
        # 限位检查
        if (pan_clamped == self.pan_min and pan_old > self.pan_min) or (
            pan_clamped == self.pan_max and pan_old < self.pan_max
        ):
            result["limit"] = True
        if (tilt_clamped == self.tilt_min and tilt_old > self.tilt_min) or (
            tilt_clamped == self.tilt_max and tilt_old < self.tilt_max
        ):
            result["limit"] = True
        return result

    def move_pan_tilt_sync(self, pan_delta: float, tilt_delta: float, step: float = 1.0) -> bool:
        """同步版双轴增量移动 — 用于 executor 线程内直接调用"""
        pan_new = self.pan_angle + pan_delta * step
        tilt_new = self.tilt_angle + tilt_delta * step
        pan_clamped = max(self.pan_min, min(self.pan_max, pan_new))
        tilt_clamped = max(self.tilt_min, min(self.tilt_max, tilt_new))

        pan_actual = 180 - pan_clamped if self.pan_invert else pan_clamped
        tilt_actual = 180 - tilt_clamped if self.tilt_invert else tilt_clamped

        # 先写硬件，成功后再更新内部状态
        self._hardware.set_angles_sync(pan_actual, tilt_actual)
        self.pan_angle = pan_clamped
        self.tilt_angle = tilt_clamped
        return True

    async def center(self) -> None:
        """云台回中 — 使用可配置的 pan_center / tilt_center"""
        await self.set_pan(self.pan_center)
        await self.set_tilt(self.tilt_center)
        self.logger.info(f"Gimbal centered (pan={self.pan_center}°, tilt={self.tilt_center}°)")

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
    gimbal_config = config.get("gimbal", {})
    # 传递 robot 信息给 MockGimbal 使用
    robot_config = config.get("robot", {})
    gimbal_config.setdefault("robot", robot_config)
    return GimbalController(gimbal_config, logger_instance)
