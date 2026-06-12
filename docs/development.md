# wo-bot-control 开发指南

## 项目结构

```
wo-bot-control/
├── src/
│   ├── core/                    # 核心服务
│   │   ├── websocket_server.py  # WebSocket 服务器
│   │   ├── mdns_service.py      # mDNS 服务发现
│   │   └── message_handler.py   # 消息处理器
│   ├── modules/                 # 功能模块
│   │   ├── motion/              # 运动控制
│   │   │   └── controller.py
│   │   ├── vision/              # 视觉模块
│   │   │   └── camera.py
│   │   ├── system/              # 系统信息
│   │   │   └── collector.py
│   │   └── extension/           # 扩展模块
│   │       └── base.py
│   ├── utils/                   # 工具函数
│   │   └── logger.py
│   └── main.py                  # 入口文件
├── config/
│   └── config.yaml              # 配置文件
├── scripts/
│   ├── install.sh               # 安装脚本
│   └── start.sh                 # 启动脚本
├── docs/
│   ├── protocol.md              # 通信协议
│   └── deployment.md            # 部署指南
├── tests/                       # 测试
├── requirements.txt             # 依赖
└── README.md
```

## 开发环境设置

```bash
# 克隆仓库
git clone https://github.com/wo-bot/wo-bot-control.git
cd wo-bot-control

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装开发依赖
pip install -r requirements.txt
pip install pytest pytest-asyncio httpx
```

## 添加新模块

### 1. 创建模块目录

```bash
mkdir -p src/modules/my_module
```

### 2. 实现模块类

```python
# src/modules/my_module/my_module.py
from modules.extension.base import ExtensionModule

class MyModule(ExtensionModule):
    def __init__(self, config=None, logger=None):
        super().__init__("my_module", config, logger)

    async def start(self):
        self.running = True
        # 启动逻辑

    async def stop(self):
        self.running = False
        # 停止逻辑

    async def handle_command(self, command: str, data: dict) -> dict:
        # 处理命令
        if command == "do_something":
            return {"result": "ok"}
        return {"error": "unknown command"}
```

### 3. 注册模块

在 `main.py` 中添加：

```python
from modules.my_module.my_module import MyModule

# 在 _init_modules 中
my_module = MyModule(config.get("my_module", {}), self.logger)
self.module_manager.register(my_module)
```

## 添加新消息类型

### 1. 在 protocol.md 中定义

```markdown
#### my_command - 我的命令
```json
{
  "type": "my_command",
  "data": {
    "param": "value"
  }
}
```
```

### 2. 在 message_handler.py 中实现

```python
async def _handle_my_command(self, data: dict) -> dict:
    param = data.get("param")
    # 处理逻辑
    return {"type": "my_command_ack", "data": {"result": "ok"}}
```

## 测试

### 单元测试

```python
# tests/test_motion.py
import pytest
from modules.motion.controller import MotionController

@pytest.mark.asyncio
async def test_motion_stop():
    controller = MotionController()
    await controller.stop()
    assert controller.current_linear == 0
    assert controller.current_angular == 0
```

### 运行测试

```bash
pytest tests/ -v
```

## 调试

### 启用详细日志

```yaml
logging:
  level: "DEBUG"
```

### 使用 Python 调试器

```python
import pdb; pdb.set_trace()
```

## 代码风格

- 使用 Python 类型提示
- 遵循 PEP 8
- 使用 async/await 进行异步操作
- 添加文档字符串

## 贡献

1. Fork 仓库
2. 创建功能分支
3. 提交更改
4. 创建 Pull Request
