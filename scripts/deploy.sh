#!/bin/bash
# wo-bot-control 远程一键部署脚本
# 用法: bash scripts/deploy.sh [--jetson] [--host HOST] [--user USER] [--password PASSWORD]
#
# 流程: 本地打包 -> SCP 推送到机器人 -> 远程解压 -> 安装依赖 -> 重启 systemd 服务

set -e

# ============================================================
# 默认参数（局域网 Jetson 设备）
# ============================================================
REMOTE_HOST="192.168.1.47"
REMOTE_USER="trae"
REMOTE_PASSWORD=""
REMOTE_DIR="/opt/wobot"
SERVICE_NAME="wobot-control"
REQUIREMENTS_FILE="requirements-jetson.txt"  # Jetson Python 3.7 兼容

# ============================================================
# 解析命令行参数
# ============================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --jetson)
            REMOTE_HOST="192.168.1.47"
            REMOTE_USER="trae"
            REQUIREMENTS_FILE="requirements-jetson.txt"
            shift
            ;;
        --host)
            REMOTE_HOST="$2"
            shift 2
            ;;
        --user)
            REMOTE_USER="$2"
            shift 2
            ;;
        --password)
            REMOTE_PASSWORD="$2"
            shift 2
            ;;
        --req)
            REQUIREMENTS_FILE="$2"
            shift 2
            ;;
        *)
            echo "未知参数: $1"
            echo "用法: bash scripts/deploy.sh [--jetson] [--host HOST] [--user USER] [--password PASSWORD] [--req REQUIREMENTS_FILE]"
            exit 1
            ;;
    esac
done

# ============================================================
# 路径计算
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

PACKAGE_NAME="wobot-control-deploy-$(date +%Y%m%d-%H%M%S).tar.gz"
PACKAGE_PATH="/tmp/${PACKAGE_NAME}"

echo "========================================"
echo "  wo-bot-control 远程部署"
echo "========================================"
echo "目标主机: ${REMOTE_USER}@${REMOTE_HOST}"
echo "远程目录: ${REMOTE_DIR}"
echo "依赖文件: ${REQUIREMENTS_FILE}"
echo ""

# ============================================================
# Step 1: 本地打包
# ============================================================
echo "[1/5] 打包项目文件..."

tar -czf "$PACKAGE_PATH" \
    --exclude='venv' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='logs' \
    --exclude='*.log' \
    --exclude='.git' \
    --exclude='.idea' \
    --exclude='.vscode' \
    --exclude='*.swp' \
    --exclude='*.swo' \
    --exclude='.DS_Store' \
    --exclude='.env' \
    --exclude='.pytest_cache' \
    --exclude='.mypy_cache' \
    --exclude='.ruff_cache' \
    --exclude='config/local.yaml' \
    --exclude='*.tar.gz' \
    --exclude='tmp' \
    -C "$PROJECT_DIR" .

PACKAGE_SIZE=$(du -h "$PACKAGE_PATH" | cut -f1)
echo "  打包完成: ${PACKAGE_PATH} (${PACKAGE_SIZE})"

# ============================================================
# Step 2: 检查 sshpass
# ============================================================
echo ""
echo "[2/5] 检查 SSH 工具..."

SSH_CMD="ssh"
SCP_CMD="scp"

if [ -n "$REMOTE_PASSWORD" ]; then
    if command -v sshpass &> /dev/null; then
        SSH_CMD="sshpass -p '${REMOTE_PASSWORD}' ssh -o StrictHostKeyChecking=no"
        SCP_CMD="sshpass -p '${REMOTE_PASSWORD}' scp -o StrictHostKeyChecking=no"
        echo "  使用 sshpass 免交互登录"
    else
        echo "  [警告] 未安装 sshpass，将使用交互式 SSH（需要手动输入密码）"
        echo "  安装 sshpass: brew install sshpass (macOS) 或 apt install sshpass (Linux)"
    fi
else
    echo "  未提供密码，使用默认 SSH 配置（密钥或交互式）"
fi

# ============================================================
# Step 3: SCP 推送包到远程
# ============================================================
echo ""
echo "[3/5] 推送部署包到远程..."

eval "${SCP_CMD} ${PACKAGE_PATH} ${REMOTE_USER}@${REMOTE_HOST}:/tmp/${PACKAGE_NAME}"
echo "  推送完成"

# ============================================================
# Step 4: 远程部署
# ============================================================
echo ""
echo "[4/5] 远程解压并安装..."

REMOTE_SCRIPT=$(cat <<'DEPLOY_EOF'
#!/bin/bash
set -e

REMOTE_DIR="$1"
PACKAGE_NAME="$2"
SERVICE_NAME="$3"
REQUIREMENTS_FILE="$4"
SUDO_PASSWORD="$5"
SUDO=""
if [ -n "$SUDO_PASSWORD" ]; then
    SUDO="echo '${SUDO_PASSWORD}' | sudo -S"
fi

echo "  -> 停止现有服务..."
eval "${SUDO} systemctl stop ${SERVICE_NAME}" 2>/dev/null || true

# 确保端口释放：杀掉所有占用端口的旧进程（可能有多个）
echo "  -> 释放旧端口..."
eval "${SUDO} fuser -k 8765/tcp" 2>/dev/null || true
eval "${SUDO} fuser -k 8000/tcp" 2>/dev/null || true
sleep 1

# 清除可能存在的旧 cron @reboot 任务（防止开机重复启动）
echo "  -> 清理旧 cron 任务..."
crontab -l 2>/dev/null | grep -v "wo-bot-control" | crontab - 2>/dev/null || true

echo "  -> 创建目标目录..."
mkdir -p ${REMOTE_DIR}

echo "  -> 清理旧文件..."
eval "${SUDO} rm -rf ${REMOTE_DIR}/*" 2>/dev/null || true
# 如果 sudo rm 失败（权限不足），手动删除可删的文件
rm -rf ${REMOTE_DIR}/* 2>/dev/null || true

echo "  -> 解压部署包..."
tar -xzf /tmp/${PACKAGE_NAME} -C ${REMOTE_DIR}

echo "  -> 检查依赖文件..."
cd ${REMOTE_DIR}
if [ ! -f "${REQUIREMENTS_FILE}" ]; then
    echo "  [警告] ${REQUIREMENTS_FILE} 不存在，回退到 requirements.txt"
    REQUIREMENTS_FILE="requirements.txt"
fi

echo "  -> 创建虚拟环境（Python 3.7）..."
python3.7 -m venv venv 2>/dev/null || python3 -m venv venv
source venv/bin/activate

echo "  -> 安装依赖..."
pip install --upgrade pip -q
pip install -r ${REQUIREMENTS_FILE} -q
echo "  -> 依赖安装完成"

# 安装 Rosmaster_Lib 硬件驱动（如果 Jetson 上存在）
if [ -d "/home/jetson/py_install/Rosmaster_Lib" ]; then
    echo "  -> 安装 Rosmaster_Lib..."
    cp -r /home/jetson/py_install/Rosmaster_Lib ${REMOTE_DIR}/venv/lib/python3*/site-packages/ 2>/dev/null || true
fi

# Monkey-patch: aiortc 在 OpenSSL 1.1.1 上有多个不兼容的 ctypes 调用
echo "  -> 应用 aiortc OpenSSL 兼容补丁..."
AIORTC_DTLS="${REMOTE_DIR}/venv/lib/python3*/site-packages/aiortc/rtcdtlstransport.py"
if [ -f "$(ls ${AIORTC_DTLS} 2>/dev/null | head -1)" ]; then
    AIORTC_FILE=$(ls ${AIORTC_DTLS} | head -1)
    ${PYTHON_BIN} -c "
import os, re
f = open('${AIORTC_FILE}')
content = f.read(); f.close()
if '# NOTE: BIO_ctrl_pending is not available' in content:
    print('aiortc: already patched')
else:
    # Patch 1: SSL_CTX_set_read_ahead 不存在 → hasattr guard
    content = content.replace(
        'lib.SSL_CTX_set_read_ahead(ctx, 1)',
        'lib.SSL_CTX_set_read_ahead(ctx, 1) if hasattr(lib, \"SSL_CTX_set_read_ahead\") else 0'
    )
    # Patch 2: Replace BIO_ctrl_pending/BIO_ctrl check with direct BIO_read
    # cryptography binding on OpenSSL 1.1.x doesn't expose BIO_ctrl*.
    # BIO_read on a memory BIO returns 0 when empty → safe to call directly.
    old_match = r'pending = lib\.(?:BIO_ctrl_pending|BIO_ctrl)\(self\.write_bio[^)]*\)\s*\n\s*if pending > 0:\s*\n\s*result = lib\.BIO_read\(\s*\n\s*self\.write_bio, self\.write_cdata, len\(self\.write_cdata\)\s*\n\s*\)\s*\n\s*(?:await self\.transport\._send|self\.__tx_bytes)'
    if re.search(r'BIO_ctrl_pending|BIO_ctrl\(self\.write_bio', content):
        content = re.sub(
            r'pending = lib\.(?:BIO_ctrl_pending|BIO_ctrl)\(self\.write_bio[^)]*\)',
            'pass  # patched: cryptography binding lacks BIO_ctrl* on OpenSSL 1.1.x',
            content
        )
        content = re.sub(
            r'if pending > 0:\s*\n\s*result = lib\.BIO_read\(',
            'result = lib.BIO_read(',
            content
        )
        content = re.sub(
            r'(\s*result = lib\.BIO_read\(\s*\n\s*self\.write_bio.*?\))\s*\n\s*(await self\.transport)',
            r'\1\n            if result > 0:\n                \2',
            content,
            flags=re.DOTALL
        )
    # Clean up any leftover _bio_ctrl_pending fallback from previous patches
    content = re.sub(
        r'\n# WORKAROUND.*?_bio_ctrl_pending[^\n]*\n.*?BIO_ctrl\(bio, 10, 0, None\)[^\n]*\n\s*',
        '\n',
        content,
        flags=re.DOTALL
    )
    # backup
    with open('${AIORTC_FILE}.pydl', 'w') as bf:
        bf.write(open('${AIORTC_FILE}').read())
    with open('${AIORTC_FILE}', 'w') as f2:
        f2.write(content)
    print('aiortc: patched (SSL_CTX_set_read_ahead + skip BIO_ctrl)')
"
fi

# 检查 systemd 服务文件是否存在，不存在则创建
if [ ! -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    echo "  -> 创建 systemd 服务..."
    eval "${SUDO} tee /etc/systemd/system/${SERVICE_NAME}.service" > /dev/null << EOF
[Unit]
Description=wo-bot-control Robot Control Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${REMOTE_DIR}
ExecStart=${REMOTE_DIR}/venv/bin/python src/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    eval "${SUDO} systemctl daemon-reload"
    eval "${SUDO} systemctl enable ${SERVICE_NAME}"
    echo "  -> systemd 服务已创建并启用"
fi

echo "  -> 启动服务..."
eval "${SUDO} systemctl reset-failed ${SERVICE_NAME}" 2>/dev/null || true
eval "${SUDO} systemctl start ${SERVICE_NAME}"

echo "  -> 清理临时文件..."
rm -f /tmp/${PACKAGE_NAME}

echo "  -> 部署完成！"
DEPLOY_EOF
)

eval "${SSH_CMD} ${REMOTE_USER}@${REMOTE_HOST} 'bash -s' <<SCRIPT
${REMOTE_SCRIPT}
SCRIPT
${REMOTE_DIR} ${PACKAGE_NAME} ${SERVICE_NAME} ${REQUIREMENTS_FILE} ${REMOTE_PASSWORD}"

# ============================================================
# Step 5: 清理 & 验证
# ============================================================
echo ""
echo "[5/5] 清理本地临时文件 & 验证服务状态..."

rm -f "$PACKAGE_PATH"
echo "  本地临时包已清理"

sleep 2
echo ""
echo "--- 远程服务状态 ---"
eval "${SSH_CMD} ${REMOTE_USER}@${REMOTE_HOST} 'systemctl status ${SERVICE_NAME} --no-pager -l' || true"

echo ""
echo "========================================"
echo "  部署完成！"
echo "  查看日志: ssh ${REMOTE_USER}@${REMOTE_HOST} journalctl -u ${SERVICE_NAME} -f"
echo "========================================"
