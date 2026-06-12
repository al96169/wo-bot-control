"""
wo-bot-control 测试
"""

import pytest
import asyncio
import json
import sys
import os

# 添加 src 到路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


class TestMotionController:
    """运动控制器测试"""

    @pytest.mark.asyncio
    async def test_initialization(self):
        """测试初始化"""
        from modules.motion.controller import MotionController

        controller = MotionController()
        assert controller.drive_type == "mecanum"
        assert controller.max_linear_speed == 1.0
        assert controller.max_angular_speed == 1.0

    @pytest.mark.asyncio
    async def test_stop(self):
        """测试停止"""
        from modules.motion.controller import MotionController

        controller = MotionController()
        await controller.stop()
        assert controller.current_linear == 0.0
        assert controller.current_angular == 0.0

    @pytest.mark.asyncio
    async def test_set_velocity(self):
        """测试设置速度"""
        from modules.motion.controller import MotionController

        controller = MotionController()
        await controller.set_velocity(0.5, 0.3)
        assert controller.current_linear == 0.5
        assert controller.current_angular == 0.3

    @pytest.mark.asyncio
    async def test_velocity_limits(self):
        """测试速度限制"""
        from modules.motion.controller import MotionController

        controller = MotionController()
        await controller.set_velocity(2.0, 2.0)  # 超出范围
        assert controller.current_linear <= 1.0
        assert controller.current_angular <= 1.0

    @pytest.mark.asyncio
    async def test_emergency_stop(self):
        """测试急停"""
        from modules.motion.controller import MotionController

        controller = MotionController()
        await controller.set_velocity(0.5, 0.3)
        await controller.emergency_stop()
        assert controller.emergency_stopped == True
        assert controller.current_linear == 0.0

    @pytest.mark.asyncio
    async def test_drive_type(self):
        """测试驱动类型"""
        from modules.motion.controller import MotionController

        controller = MotionController()
        controller.set_drive_type("differential")
        assert controller.drive_type == "differential"

    def test_wheel_speed_calculation(self):
        """测试轮速计算"""
        from modules.motion.controller import MotionController

        controller = MotionController()

        # 麦轮驱动
        speeds = controller._calculate_wheel_speeds(0.5, 0.0)
        assert "front_left" in speeds
        assert "front_right" in speeds

        # 差速驱动
        controller.set_drive_type("differential")
        speeds = controller._calculate_wheel_speeds(0.5, 0.0)
        assert "left" in speeds
        assert "right" in speeds


class TestSystemCollector:
    """系统信息采集测试"""

    @pytest.mark.asyncio
    async def test_collect(self):
        """测试采集"""
        from modules.system.collector import SystemCollector

        collector = SystemCollector()
        status = await collector.collect()

        assert "battery" in status
        assert "system" in status
        assert "network" in status

    @pytest.mark.asyncio
    async def test_battery_info(self):
        """测试电池信息"""
        from modules.system.collector import SystemCollector

        collector = SystemCollector()
        battery = await collector._collect_battery()

        assert "level" in battery
        assert "status" in battery

    @pytest.mark.asyncio
    async def test_system_info(self):
        """测试系统信息"""
        from modules.system.collector import SystemCollector

        collector = SystemCollector()
        system = await collector._collect_system()

        assert "cpu_percent" in system
        assert "memory_percent" in system
        assert "disk_percent" in system


class TestMessageHandler:
    """消息处理器测试"""

    @pytest.mark.asyncio
    async def test_handle_ping(self):
        """测试心跳"""
        from core.message_handler import MessageHandler

        handler = MessageHandler()
        response = await handler.handle("ping", {})
        assert response["type"] == "pong"

    @pytest.mark.asyncio
    async def test_handle_motion(self):
        """测试运动控制"""
        from core.message_handler import MessageHandler
        from modules.motion.controller import MotionController

        controller = MotionController()
        handler = MessageHandler(motion_controller=controller)

        response = await handler.handle("motion", {"linear": 0.5, "angular": 0.3})
        assert response["type"] == "motion_ack"

    @pytest.mark.asyncio
    async def test_handle_motion_stop(self):
        """测试停止运动"""
        from core.message_handler import MessageHandler
        from modules.motion.controller import MotionController

        controller = MotionController()
        handler = MessageHandler(motion_controller=controller)

        response = await handler.handle("motion_stop", {})
        assert response["type"] == "motion_ack"

    @pytest.mark.asyncio
    async def test_handle_unknown(self):
        """测试未知消息"""
        from core.message_handler import MessageHandler

        handler = MessageHandler()
        response = await handler.handle("unknown_type", {})
        assert response["type"] == "error"


class TestExtensionModule:
    """扩展模块测试"""

    def test_module_base(self):
        """测试模块基类"""
        from modules.extension.base import ExtensionModule

        class TestModule(ExtensionModule):
            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

            async def handle_command(self, command, data):
                return {"result": "ok"}

        module = TestModule("test_module")
        info = module.get_info()

        assert info["id"] == "test_module"
        assert info["status"] == "stopped"

    def test_module_manager(self):
        """测试模块管理器"""
        from modules.extension.base import ModuleManager, ExtensionModule

        class TestModule(ExtensionModule):
            async def start(self):
                self.running = True

            async def stop(self):
                self.running = False

            async def handle_command(self, command, data):
                return {"result": "ok"}

        manager = ModuleManager()
        module = TestModule("test_module")

        manager.register(module)
        assert "test_module" in manager.modules

        manager.unregister("test_module")
        assert "test_module" not in manager.modules


class TestProtocol:
    """协议测试"""

    def test_message_format(self):
        """测试消息格式"""
        message = {
            "type": "motion",
            "timestamp": 1699999999000,
            "data": {
                "linear": 0.5,
                "angular": 0.3
            }
        }

        # 验证 JSON 序列化
        json_str = json.dumps(message)
        parsed = json.loads(json_str)

        assert parsed["type"] == "motion"
        assert "timestamp" in parsed
        assert "data" in parsed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
