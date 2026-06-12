#!/bin/bash
# wo-bot-control 部署脚本

set -e

echo "=== wo-bot-control Deployment Script ==="

# 检查是否为 root 用户
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root or use sudo"
    exit 1
fi

# 安装目录
INSTALL_DIR="/opt/wobot"
SERVICE_NAME="wobot-control"

# 创建安装目录
echo "Creating installation directory..."
mkdir -p $INSTALL_DIR

# 复制文件
echo "Copying files..."
cp -r ./* $INSTALL_DIR/

# 创建虚拟环境
echo "Creating virtual environment..."
cd $INSTALL_DIR
python3 -m venv venv
source venv/bin/activate

# 安装依赖
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# 创建 systemd 服务
echo "Creating systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=wo-bot-control Robot Control Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python src/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 重载 systemd
systemctl daemon-reload

# 启用服务
echo "Enabling service..."
systemctl enable ${SERVICE_NAME}

echo ""
echo "=== Installation Complete ==="
echo "Start service: sudo systemctl start ${SERVICE_NAME}"
echo "Stop service: sudo systemctl stop ${SERVICE_NAME}"
echo "View logs: sudo journalctl -u ${SERVICE_NAME} -f"
