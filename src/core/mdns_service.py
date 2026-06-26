"""
mDNS 服务发现
使用 zeroconf 在局域网广播服务
"""

from __future__ import annotations

import asyncio
import socket
import traceback

from zeroconf import ServiceInfo, Zeroconf


def _get_local_ipv4_addresses() -> list[str]:
    """获取本机所有非环回 IPv4 地址"""
    result = []
    try:
        # 优先通过连接一个外部地址来获取出口网卡的 IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                result.append(ip)
        finally:
            s.close()
    except Exception:
        pass

    # 兜底：遍历 hostname / 所有接口
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ip = info[4][0]
            if ip and not str(ip).startswith("127.") and ip not in result:
                result.append(ip)
    except Exception:
        pass
    return result or []


class MDNSService:
    """mDNS 服务发现"""

    def __init__(self, robot_info: dict, port: int = 8765, logger=None):
        self.robot_info = robot_info
        self.port = port
        self.logger = logger

        self.zeroconf: Zeroconf | None = None
        self.service_info: ServiceInfo | None = None

    async def start(self):
        """启动 mDNS 服务"""
        try:
            loop = asyncio.get_event_loop()

            # Zeroconf() 构造也是同步阻塞，放到线程池
            self.zeroconf = await loop.run_in_executor(None, Zeroconf)

            robot_id = self.robot_info.get("id", "wobot-001")
            robot_name = self.robot_info.get("name", "My Robot")
            model = self.robot_info.get("model", "jetson-nano")
            version = self.robot_info.get("version", "1.0.0")

            service_name = f"{robot_id}._wobot._tcp.local."

            # 显式获取本机 IP，供 zeroconf 在 A 记录里广播
            addresses = _get_local_ipv4_addresses()
            if self.logger:
                self.logger.info(f"mDNS: local IP addresses detected: {addresses}")

            properties = {
                "name": robot_name,
                "model": model,
                "version": version,
                "id": robot_id,
            }

            self.service_info = ServiceInfo(
                type_="_wobot._tcp.local.",
                name=service_name,
                port=self.port,
                properties=properties,
                addresses=addresses,  # type: ignore[arg-type]
            )

            # register_service 也是同步阻塞，放到线程池
            await loop.run_in_executor(None, self.zeroconf.register_service, self.service_info)  # type: ignore[union-attr]
            if self.logger:
                self.logger.info(f"mDNS service registered: {service_name} on {addresses}:{self.port}")

        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to start mDNS service: {type(e).__name__}: {e}")
                self.logger.error(f"mDNS traceback: {traceback.format_exc()}")
            # 资源清理
            try:
                if self.zeroconf:
                    self.zeroconf.close()
            except Exception:
                pass
            self.zeroconf = None

    async def stop(self):
        """停止 mDNS 服务"""
        if self.zeroconf:
            try:
                if self.service_info:
                    self.zeroconf.unregister_service(self.service_info)
                self.zeroconf.close()
                if self.logger:
                    self.logger.info("mDNS service stopped")
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Error stopping mDNS service: {e}")
