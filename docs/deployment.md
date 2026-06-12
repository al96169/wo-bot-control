# wo-bot-control 部署指南

## 环境要求

- Python 3.10+
- Jetson Nano / Jetson Xavier 或其他 Linux 设备
- 可选：ROS2（用于机器人集成）

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/wo-bot/wo-bot-control.git
cd wo-bot-control
```

### 2. 安装依赖

```bash
# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 3. 配置

编辑 `config/config.yaml`：

```yaml
robot:
  id: "wobot-001"
  name: "My Robot"
  model: "jetson-nano"

server:
  host: "0.0.0.0"
  port: 8765
```

### 4. 运行

```bash
python src/main.py
```

## 系统服务部署

### 使用安装脚本

```bash
sudo bash scripts/install.sh
```

### 手动配置 systemd

创建服务文件 `/etc/systemd/system/wobot-control.service`：

```ini
[Unit]
Description=wo-bot-control Robot Control Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/wobot
ExecStart=/opt/wobot/venv/bin/python src/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable wobot-control
sudo systemctl start wobot-control
```

## Jetson 特定配置

### 1. 摄像头权限

```bash
# 添加用户到 video 组
sudo usermod -aG video $USER

# 设置摄像头权限
sudo chmod 666 /dev/video*
```

### 2. GPIO 权限

```bash
# 添加用户到 gpio 组
sudo usermod -aG gpio $USER
```

### 3. 性能模式（可选）

```bash
# 设置最大性能模式
sudo nvpmodel -m 0
sudo jetson_clocks
```

### 4. Swap 配置（推荐）

```bash
# 增加 swap（如果内存不足）
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

## 网络配置

### mDNS 发现

确保 Avahi 服务运行：

```bash
sudo apt install avahi-daemon
sudo systemctl start avahi-daemon
```

### 防火墙

开放必要端口：

```bash
# WebSocket 端口
sudo ufw allow 8765/tcp

# HTTP API 端口
sudo ufw allow 8000/tcp

# mDNS
sudo ufw allow 5353/udp
```

## ROS2 集成（可选）

如果需要与 ROS2 集成：

```bash
# 安装 ROS2（如果尚未安装）
# 参考: https://docs.ros.org/en/humble/Installation.html

# 安装 ROS2 Python 包
pip install rclpy geometry_msgs
```

在配置中启用 ROS2：

```yaml
motion:
  ros_enabled: true
  ros_topic: "/cmd_vel"
```

## 测试

### 单元测试

```bash
pytest tests/
```

### 手动测试

使用 wo-bot-web-debug 连接测试：

1. 启动 wo-bot-control
2. 打开 wo-bot-web-debug
3. 扫描设备或手动输入 IP
4. 连接并测试功能

## 日志

日志文件位置：`logs/wobot.log`

查看日志：

```bash
tail -f logs/wobot.log
```

或使用 journalctl：

```bash
sudo journalctl -u wobot-control -f
```

## 故障排除

### 摄像头无法打开

```bash
# 检查摄像头设备
ls -la /dev/video*

# 检查权限
v4l2-ctl --list-devices
```

### mDNS 发现不工作

```bash
# 检查 Avahi 服务
sudo systemctl status avahi-daemon

# 测试 mDNS
avahi-browse -at
```

### WebSocket 连接失败

```bash
# 检查端口是否被占用
sudo netstat -tulpn | grep 8765

# 检查防火墙
sudo ufw status
```

## 更新

```bash
cd /opt/wobot
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart wobot-control
```
