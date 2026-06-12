"""
扩展模块基类
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class ExtensionModule(ABC):
    """扩展模块基类"""

    def __init__(self, module_id: str, config: dict = None, logger=None):
        self.module_id = module_id
        self.config = config or {}
        self.logger = logger
        self.enabled = False
        self.running = False

    @abstractmethod
    async def start(self):
        """启动模块"""
        pass

    @abstractmethod
    async def stop(self):
        """停止模块"""
        pass

    @abstractmethod
    async def handle_command(self, command: str, data: dict) -> dict:
        """处理命令"""
        pass

    def get_info(self) -> dict:
        """获取模块信息"""
        return {
            "id": self.module_id,
            "name": self.config.get("name", self.module_id),
            "version": self.config.get("version", "1.0.0"),
            "status": "running" if self.running else "stopped",
            "enabled": self.enabled,
        }

    def enable(self):
        """启用模块"""
        self.enabled = True

    def disable(self):
        """禁用模块"""
        self.enabled = False


class ModuleManager:
    """模块管理器"""

    def __init__(self, logger=None):
        self.logger = logger
        self.modules: Dict[str, ExtensionModule] = {}

    def register(self, module: ExtensionModule):
        """注册模块"""
        self.modules[module.module_id] = module
        if self.logger:
            self.logger.info(f"Module registered: {module.module_id}")

    def unregister(self, module_id: str):
        """注销模块"""
        if module_id in self.modules:
            del self.modules[module_id]
            if self.logger:
                self.logger.info(f"Module unregistered: {module_id}")

    async def start_module(self, module_id: str):
        """启动模块"""
        if module_id in self.modules:
            module = self.modules[module_id]
            await module.start()
            if self.logger:
                self.logger.info(f"Module started: {module_id}")

    async def stop_module(self, module_id: str):
        """停止模块"""
        if module_id in self.modules:
            module = self.modules[module_id]
            await module.stop()
            if self.logger:
                self.logger.info(f"Module stopped: {module_id}")

    async def start_all(self):
        """启动所有模块"""
        for module_id in self.modules:
            await self.start_module(module_id)

    async def stop_all(self):
        """停止所有模块"""
        for module_id in self.modules:
            await self.stop_module(module_id)

    def get_module_list(self) -> list:
        """获取模块列表"""
        return [module.get_info() for module in self.modules.values()]

    async def send_command(self, module_id: str, command: str, data: dict) -> dict:
        """发送命令到模块"""
        if module_id not in self.modules:
            return {"error": "Module not found"}

        module = self.modules[module_id]
        return await module.handle_command(command, data)
