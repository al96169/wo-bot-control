"""
wo-bot-control 主入口
机器人控制端服务软件
"""

import asyncio
import signal
import sys
from pathlib import Path

import yaml

from core.http_api import HttpAPIServer
from core.mdns_service import MDNSService
from core.message_handler import MessageHandler
from core.service_manager import ServiceManager
from core.websocket_server import WebSocketServer

# WebRTC 可选导入（兼容 Python 3.6）
try:
    from core.webrtc_service import WebRTCService

    WEBRTC_AVAILABLE = True
except (ImportError, AttributeError) as e:
    WebRTCService = None
    WEBRTC_AVAILABLE = False
    print(f"Warning: WebRTC not available: {e}")
from modules.motion.controller import MotionController
from modules.system.collector import SystemCollector
from modules.system.power_policy import PowerPolicy

# Camera 可选导入（兼容无opencv环境）
try:
    from modules.vision.camera import CameraManager

    CAMERA_AVAILABLE = True
except ImportError as e:
    CameraManager = None
    CAMERA_AVAILABLE = False
    print(f"Warning: Camera not available: {e}")
# Gimbal 可选导入（兼容无舵机硬件环境）
try:
    from modules.motion.gimbal import create_gimbal

    GIMBAL_AVAILABLE = True
except ImportError as e:
    create_gimbal = None
    GIMBAL_AVAILABLE = False
    print(f"Warning: Gimbal not available: {e}")
from utils.logger import setup_logger


class WoBotControl:
    """wo-bot-control 主控制类"""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config = self._load_config(config_path)
        self.logger = setup_logger(self.config.get("logging", {}))

        # 核心组件
        self.ws_server = None
        self.mdns_service = None
        self.message_handler = None
        self.service_manager = None

        # 功能模块
        self.system_collector = None
        self.motion_controller = None
        self.camera_manager = None
        self.gimbal_controller = None
        self.http_server = None
        self.webrtc_service = None
        self.dance_controller = None
        self.voice_broadcast_controller = None
        self.power_policy = None

        # 运行状态
        self.running = False

    def _load_config(self, config_path: str) -> dict:
        """加载配置文件"""
        path = Path(config_path)
        if not path.exists():
            # 尝试从相对路径加载
            path = Path(__file__).parent.parent / config_path

        if path.exists():
            with open(path, encoding="utf-8") as f:
                return yaml.safe_load(f) or self._get_default_config()
        else:
            return self._get_default_config()

    def _get_default_config(self) -> dict:
        """获取默认配置"""
        return {
            "robot": {"id": "wobot-001", "name": "My Robot", "model": "jetson-nano", "version": "1.0.0"},
            "server": {"host": "0.0.0.0", "port": 8765, "http_port": 8000},
            "mdns": {"enabled": True, "service_type": "_wobot._tcp.local.", "port": 8765},
            "status": {"update_interval": 1.0},
            "motion": {"drive_type": "mecanum", "max_linear_speed": 1.0, "max_angular_speed": 1.0},
            "camera": {
                "enabled": True,
                "default_camera": 0,
                "resolution": {"width": 320, "height": 240},
                "fps": 15,
            },
            "gimbal": {
                "enabled": False,
                "gimbal_type": "rosmaster",
                "com": "/dev/myserial",
                "car_type": 1,
                "pan_channel": 4,
                "tilt_channel": 3,
                "pan_min": 0,
                "pan_max": 180,
                "tilt_min": 30,
                "tilt_max": 150,
            },
            "logging": {"level": "INFO"},
        }

    async def start(self):
        """启动服务"""
        self.logger.info(f"Starting wo-bot-control v{self.config['robot']['version']}")
        self.running = True

        # 初始化功能模块
        await self._init_modules()

        # 初始化消息处理器
        self.message_handler = MessageHandler(
            system_collector=self.system_collector,
            motion_controller=self.motion_controller,
            camera_manager=self.camera_manager,
            config=self.config,
            logger=self.logger,
        )

        # 注入云台控制器到消息处理器
        if self.gimbal_controller:
            self.message_handler.gimbal_controller = self.gimbal_controller

        # 注入舞蹈控制器到消息处理器
        if self.dance_controller:
            self.message_handler.dance_controller = self.dance_controller

        # 注入喊话控制器到消息处理器
        if self.voice_broadcast_controller:
            self.message_handler.voice_broadcast_controller = self.voice_broadcast_controller

        # 初始化服务进程管理器（负责守护所有子服务）
        self.service_manager = ServiceManager(
            config=self.config,
            message_callback=self._on_service_message,
        )
        self.logger.info("Service manager initialized")

        # 注入 service_manager 到 message_handler
        self.message_handler.service_manager = self.service_manager

        # 注入 power_policy 到 message_handler
        if self.power_policy:
            self.message_handler.power_policy = self.power_policy

            # 设置模式变更回调：通过 WebSocket 广播通知所有客户端
            async def on_power_mode_change(from_mode: str, to_mode: str):
                if self.ws_server:
                    message = {
                        "type": "service_message",
                        "data": {
                            "subject": "省电模式变更" if to_mode == "eco" else "恢复正常模式",
                            "summary": f"机器人已{'进入省电模式' if to_mode == 'eco' else '恢复正常模式'}",
                            "body": f"电量策略自动切换: {from_mode} → {to_mode}",
                            "severity": "warning" if to_mode == "eco" else "info",
                            "source": "power_policy",
                        },
                    }
                    await self.ws_server.broadcast_message(message)
                    # 同步广播 power_policy_status
                    await self.ws_server.broadcast_message(
                        {
                            "type": "power_policy_status",
                            "data": self.power_policy.get_status(),
                        }
                    )

            self.power_policy.set_on_mode_change(on_power_mode_change)
            self.logger.info("Power policy injected into message handler")

        # 注册进程内服务
        if self.webrtc_service:
            self.service_manager.register_in_process_service("webrtc", self.webrtc_service)
        if self.dance_controller:
            self.service_manager.register_in_process_service("dance", self.dance_controller)
        if self.voice_broadcast_controller:
            self.voice_broadcast_controller._service_manager = self.service_manager
            self.service_manager.register_in_process_service("voice_broadcast", self.voice_broadcast_controller)

        # 启动所有子服务
        await self.service_manager.start_all()

        # 初始化 WebRTC 服务（可选）
        if WEBRTC_AVAILABLE and WebRTCService:
            self.webrtc_service = WebRTCService(
                message_handler=self.message_handler,
                camera_manager=self.camera_manager,
                robot_info=self.config.get("robot", {}),
                config=self.config,
                logger=self.logger,
            )
            self.logger.info("WebRTC service initialized")
        else:
            self.webrtc_service = None
            self.logger.warning("WebRTC service disabled (aiortc not available)")

        # 启动 WebSocket 信令服务器
        server_config = self.config.get("server", {})
        self.ws_server = WebSocketServer(
            host=server_config.get("host", "0.0.0.0"),
            port=server_config.get("port", 8765),
            message_handler=self.message_handler,
            robot_info=self.config.get("robot", {}),
            webrtc_service=self.webrtc_service,
            gimbal_controller=self.gimbal_controller,
            service_manager=self.service_manager,
            config=self.config,
            logger=self.logger,
        )

        # 启动 mDNS 服务发现
        mdns_config = self.config.get("mdns", {})
        if mdns_config.get("enabled", True):
            try:
                self.mdns_service = MDNSService(
                    robot_info=self.config.get("robot", {}),
                    port=mdns_config.get("port", 8765),
                    logger=self.logger,
                )
                await self.mdns_service.start()
            except Exception as e:
                self.logger.warning(f"mDNS 启动失败（非致命）: {e}")

        # 启动 WebSocket 服务器（内部每秒自动广播状态给订阅客户端）
        await self.ws_server.start()

        # 启动 HTTP API 服务器（提供 MJPEG 流、截图等）
        http_port = self.config.get("server", {}).get("http_port", 8000)
        self.http_server = HttpAPIServer(
            host="0.0.0.0",
            port=http_port,
            system_collector=self.system_collector,
            camera_manager=self.camera_manager,
            message_handler=self.message_handler,
            config=self.config,
            logger=self.logger,
        )
        await self.http_server.start()

        # 阻塞保持服务器运行
        await self.ws_server.serve_forever()

    async def _init_modules(self):
        """初始化功能模块"""
        # 系统信息采集
        self.system_collector = SystemCollector(self.logger)
        self.logger.info("System collector initialized")

        # 省电策略引擎
        self.power_policy = PowerPolicy()
        self.logger.info("Power policy initialized")

        # 云台控制（先初始化，因为运动控制需要共享其 Rosmaster Bot 串口实例）
        gimbal_config = self.config.get("gimbal", {})
        if gimbal_config.get("enabled", False) and GIMBAL_AVAILABLE and create_gimbal:
            try:
                self.gimbal_controller = create_gimbal(self.config, self.logger)
                self.logger.info("Gimbal controller initialized")
            except Exception as e:
                self.gimbal_controller = None
                self.logger.warning(f"Gimbal controller init failed: {e}")
        else:
            self.gimbal_controller = None
            self.logger.info("Gimbal controller disabled")

        # 运动控制（与云台共享 Rosmaster Bot，避免重复打开串口）
        motion_config = self.config.get("motion", {})
        shared_bot = None
        if self.gimbal_controller is not None:
            hw = getattr(self.gimbal_controller, "_hardware", None)
            if hw is not None and hasattr(hw, "_ensure_init"):
                hw._ensure_init()  # 触发懒加载，创建 Rosmaster Bot
                shared_bot = getattr(hw, "_bot", None)
                if shared_bot is not None:
                    self.logger.info("Motion will share Rosmaster Bot instance with gimbal")

        if shared_bot is not None:
            from modules.motion.hardware import create_hardware

            motion_hw = create_hardware(motion_config, bot=shared_bot)
            self.motion_controller = MotionController(motion_config, self.logger, hardware=motion_hw)
            # 将 shared_bot 注入 SystemCollector，用于读取真实电池电压
            if self.system_collector:
                self.system_collector.set_bot(shared_bot)
        else:
            self.motion_controller = MotionController(motion_config, self.logger)
        self.logger.info("Motion controller initialized")

        # 摄像头管理（可选）
        camera_config = self.config.get("camera", {})
        if camera_config.get("enabled", True) and CAMERA_AVAILABLE and CameraManager:
            self.camera_manager = CameraManager(camera_config, self.logger)
            self.logger.info("Camera manager initialized")
        else:
            self.camera_manager = None
            if not CAMERA_AVAILABLE:
                self.logger.warning("Camera manager disabled (opencv not available)")

        # 舞蹈控制（始终可用，依赖运动控制器）
        try:
            from modules.extension.dance import DanceController

            self.dance_controller = DanceController(
                motion_controller=self.motion_controller,
                logger=self.logger,
            )
            await self.dance_controller.start()
            self.logger.info("Dance controller initialized")
        except Exception as e:
            self.dance_controller = None
            self.logger.warning(f"Dance controller init failed: {e}")

        # 喊话控制（进程内服务，依赖 service_manager 服务已启动后注入）
        try:
            from modules.extension.voice_broadcast import VoiceBroadcastController

            self.voice_broadcast_controller = VoiceBroadcastController(
                power_policy=self.power_policy,
                logger=self.logger,
            )
            await self.voice_broadcast_controller.start()
            self.logger.info("Voice broadcast controller initialized")
        except Exception as e:
            self.voice_broadcast_controller = None
            self.logger.warning(f"Voice broadcast controller init failed: {e}")

    async def stop(self):
        """停止服务"""
        self.logger.info("Stopping wo-bot-control...")
        self.running = False

        # 停止服务进程管理器（先停止子服务）
        if self.service_manager:
            await self.service_manager.stop_all()

        # 停止各组件
        if self.webrtc_service:
            await self.webrtc_service.stop()

        if self.ws_server:
            await self.ws_server.stop()

        if self.http_server:
            await self.http_server.stop()

        if self.mdns_service:
            await self.mdns_service.stop()

        if self.camera_manager:
            await self.camera_manager.stop()

        if self.gimbal_controller:
            await self.gimbal_controller.stop()

        if self.dance_controller:
            await self.dance_controller.stop()

        if self.voice_broadcast_controller:
            await self.voice_broadcast_controller.stop()

        self.logger.info("wo-bot-control stopped")

    async def _on_service_message(self, message: dict) -> None:
        """服务管理器消息回调：将子服务异常通知转发给所有 WebSocket 客户端"""
        if self.ws_server:
            await self.ws_server.broadcast_message(
                {
                    "type": "service_message",
                    "data": message,
                }
            )

    def handle_signal(self, signum, frame):
        """处理信号（Python 3.6 兼容：用 ensure_future + call_soon_threadsafe）"""
        if self.logger:
            self.logger.info(f"Received signal {signum}")
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.stop()))


async def main():
    """主函数"""
    # 创建控制实例
    control = WoBotControl()

    # 注册信号处理
    signal.signal(signal.SIGINT, control.handle_signal)
    signal.signal(signal.SIGTERM, control.handle_signal)

    try:
        await control.start()
    except KeyboardInterrupt:
        await control.stop()
    except Exception as e:
        control.logger.error(f"Fatal error: {e}")
        await control.stop()
        sys.exit(1)


if __name__ == "__main__":
    # Python 3.6 兼容：asyncio.run() 在 3.7+ 才有
    if hasattr(asyncio, "run"):
        asyncio.run(main())
    else:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
