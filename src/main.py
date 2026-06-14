"""
wo-bot-control 主入口
机器人控制端服务软件
"""

import asyncio
import signal
import sys
from pathlib import Path

import yaml

from core.websocket_server import WebSocketServer
from core.mdns_service import MDNSService
from core.message_handler import MessageHandler
from core.http_api import HttpAPIServer
# WebRTC 可选导入（兼容 Python 3.6）
try:
    from core.webrtc_service import WebRTCService
    WEBRTC_AVAILABLE = True
except ImportError as e:
    WebRTCService = None
    WEBRTC_AVAILABLE = False
    print(f"Warning: WebRTC not available: {e}")
from modules.system.collector import SystemCollector
from modules.motion.controller import MotionController
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

        # 功能模块
        self.system_collector = None
        self.motion_controller = None
        self.camera_manager = None
        self.gimbal_controller = None
        self.http_server = None
        self.webrtc_service = None

        # 运行状态
        self.running = False

    def _load_config(self, config_path: str) -> dict:
        """加载配置文件"""
        path = Path(config_path)
        if not path.exists():
            # 尝试从相对路径加载
            path = Path(__file__).parent.parent / config_path

        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
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
                "enabled": False, "gimbal_type": "rosmaster",
                "com": "/dev/myserial", "car_type": 1,
                "pan_channel": 4, "tilt_channel": 3,
                "pan_min": 0, "pan_max": 180, "tilt_min": 30, "tilt_max": 150,
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

        # 运动控制
        motion_config = self.config.get("motion", {})
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

        # 云台控制（可选）
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

    async def stop(self):
        """停止服务"""
        self.logger.info("Stopping wo-bot-control...")
        self.running = False

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

        self.logger.info("wo-bot-control stopped")

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
    if hasattr(asyncio, 'run'):
        asyncio.run(main())
    else:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(main())
