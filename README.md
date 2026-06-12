# wo-bot-control

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)

机器人控制端服务软件，运行在 Jetson 设备上，负责接收手机端控制指令，控制机器人运动，回传机器人信息。

**作者**: Antonio Leung  
**GitHub**: https://github.com/al96169/wo-bot-control  
**包名**: com.antonioleung.wobot.control

## 功能特性

- **主控服务端**: WebSocket + HTTP API
- **设备发现**: mDNS (Avahi) 局域网零配置发现
- **系统信息采集**: 电池、CPU、内存、网络状态
- **运动控制**: 支持多种驱动方式（麦轮、差速、阿克曼）
- **视觉模块**: 摄像头采集与视频流回传
- **扩展模块**: 可插拔的功能模块系统

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 运行服务

```bash
python src/main.py
```

### 配置

编辑 `config/config.yaml` 进行自定义配置。

## 项目结构

```
wo-bot-control/
├── src/
│   ├── core/           # 核心服务（WebSocket、mDNS）
│   ├── modules/        # 功能模块
│   │   ├── motion/     # 运动控制
│   │   ├── vision/     # 视觉模块
│   │   ├── system/     # 系统信息
│   │   └── extension/  # 扩展模块
│   ├── utils/          # 工具函数
│   └── main.py         # 入口文件
├── config/             # 配置文件
├── scripts/            # 部署脚本
├── docs/               # 文档
└── tests/              # 测试
```

## 通信协议

详见 [docs/protocol.md](docs/protocol.md)

## 相关项目

- [wo-bot-web-debug](https://github.com/al96169/wo-bot-web-debug) - 网页调试控制台
- [wo-bot-app](https://github.com/al96169/wo-bot-app) - 移动端 App（Flutter）

## License

MIT License - Copyright (c) 2024 Antonio Leung
