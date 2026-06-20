"""
运动控制模块
控制机器人运动
"""

import math

from .hardware import HardwareInterface, create_hardware


class MotionController:
    """运动控制器"""

    def __init__(self, config: dict = None, logger=None, hardware: HardwareInterface = None):
        self.config = config or {}
        self.logger = logger

        # 硬件后端（默认从配置创建）
        self.hardware = hardware or create_hardware(self.config)

        # 驱动类型
        self.drive_type = self.config.get("drive_type", "mecanum")

        # 速度限制
        self.max_linear_speed = self.config.get("max_linear_speed", 1.0)
        self.max_angular_speed = self.config.get("max_angular_speed", 1.0)

        # 当前状态
        self.current_linear = 0.0
        self.current_angular = 0.0
        self.current_mode = self.config.get("default_mode", "manual")

        # 急停状态
        self.emergency_stopped = False

        # ROS2 相关（可选）
        self.ros_enabled = False
        self.ros_publisher = None

    def set_drive_type(self, drive_type: str):
        """设置驱动类型"""
        valid_types = ["mecanum", "differential", "ackermann"]
        if drive_type in valid_types:
            self.drive_type = drive_type
            if self.logger:
                self.logger.info(f"Drive type set to: {drive_type}")
        else:
            if self.logger:
                self.logger.warning(f"Invalid drive type: {drive_type}")

    async def set_velocity(self, linear: float, angular: float, mode: str = None):
        """设置速度（兼容旧版双轴协议）"""
        if self.emergency_stopped:
            if self.logger:
                self.logger.warning("Motion blocked: emergency stop active")
            return

        linear = max(-1.0, min(1.0, linear))
        angular = max(-1.0, min(1.0, angular))
        linear *= self.max_linear_speed
        angular *= self.max_angular_speed

        self.current_linear = linear
        self.current_angular = angular
        if mode:
            self.current_mode = mode

        # 麦轮驱动优先使用 set_mecanum（硬件原生支持）
        if self.drive_type == "mecanum":
            await self.hardware.set_mecanum(linear, 0.0, angular)
        else:
            wheel_speeds = self._calculate_wheel_speeds(linear, angular)
            await self._send_to_hardware(wheel_speeds)

        if self.logger:
            self.logger.debug(f"Velocity set: linear={linear:.2f}, angular={angular:.2f}")

    async def set_mecanum_velocity(self, v_x: float, v_y: float, v_z: float, mode: str = None):
        """设置麦轮三轴速度 (v_x=前后, v_y=左右平移, v_z=旋转)"""
        if self.emergency_stopped:
            if self.logger:
                self.logger.warning("Motion blocked: emergency stop active")
            return

        v_x = max(-1.0, min(1.0, v_x))
        v_y = max(-1.0, min(1.0, v_y))
        v_z = max(-5.0, min(5.0, v_z))

        self.current_linear = v_x
        self.current_angular = v_z
        if mode:
            self.current_mode = mode

        await self.hardware.set_mecanum(v_x, v_y, v_z)

        if self.logger:
            self.logger.debug(f"Mecanum velocity: v_x={v_x:.2f}, v_y={v_y:.2f}, v_z={v_z:.2f}")

    def _calculate_wheel_speeds(self, linear: float, angular: float) -> dict:
        """根据驱动类型计算轮速"""
        if self.drive_type == "mecanum":
            # 麦轮驱动：全向移动
            # 假设轮距和轴距
            L = 0.5  # 轴距
            W = 0.4  # 轮距

            # 麦轮运动学
            v1 = linear - angular * (L + W) / 2  # 前左
            v2 = linear + angular * (L + W) / 2  # 前右
            v3 = linear - angular * (L + W) / 2  # 后左
            v4 = linear + angular * (L + W) / 2  # 后右

            return {"front_left": v1, "front_right": v2, "rear_left": v3, "rear_right": v4}

        elif self.drive_type == "differential":
            # 差速驱动
            L = 0.5  # 轮距

            v_left = linear - angular * L / 2
            v_right = linear + angular * L / 2

            return {"left": v_left, "right": v_right}

        elif self.drive_type == "ackermann":
            # 阿克曼转向（汽车式）
            L = 0.5  # 轴距

            if abs(angular) < 0.001:
                # 直行
                return {"left": linear, "right": linear, "steering": 0}
            else:
                # 转向
                steering = math.atan(angular * L / (linear + 0.001))
                steering = max(-math.pi / 4, min(math.pi / 4, steering))
                return {"left": linear, "right": linear, "steering": steering}

        return {}

    async def _send_to_hardware(self, wheel_speeds: dict):
        """发送到硬件"""
        if self.ros_enabled and self.ros_publisher:
            from geometry_msgs.msg import Twist
            twist = Twist()
            twist.linear.x = self.current_linear
            twist.angular.z = self.current_angular
            self.ros_publisher.publish(twist)
            return

        # 通过硬件抽象层发送
        for wheel, speed in wheel_speeds.items():
            if wheel == "steering":
                await self.hardware.set_steering(speed)
            else:
                await self.hardware.set_motor(wheel, speed)

    async def stop(self):
        """停止运动"""
        await self.set_velocity(0, 0)

    async def emergency_stop(self):
        """急停"""
        self.emergency_stopped = True
        self.current_linear = 0.0
        self.current_angular = 0.0
        await self.hardware.emergency_stop()
        if self.logger:
            self.logger.warning("Emergency stop activated!")

    async def release_emergency_stop(self):
        """释放急停"""
        self.emergency_stopped = False
        await self.hardware.release()
        if self.logger:
            self.logger.info("Emergency stop released")

    def get_status(self) -> dict:
        """获取运动状态"""
        return {
            "linear": self.current_linear,
            "angular": self.current_angular,
            "mode": self.current_mode,
            "drive_type": self.drive_type,
            "emergency_stopped": self.emergency_stopped,
            "max_linear_speed": self.max_linear_speed,
            "max_angular_speed": self.max_angular_speed,
        }

    # ============ ROS2 集成（可选） ============

    def enable_ros(self):
        """启用 ROS2 集成"""
        try:
            import rclpy
            from geometry_msgs.msg import Twist

            rclpy.init()
            self.ros_node = rclpy.create_node("wobot_motion")
            self.ros_publisher = self.ros_node.create_publisher(Twist, "/cmd_vel", 10)
            self.ros_enabled = True

            if self.logger:
                self.logger.info("ROS2 motion enabled")

        except ImportError:
            if self.logger:
                self.logger.warning("ROS2 not available")

    def disable_ros(self):
        """禁用 ROS2 集成"""
        self.ros_enabled = False
        if hasattr(self, "ros_node"):
            self.ros_node.destroy_node()
