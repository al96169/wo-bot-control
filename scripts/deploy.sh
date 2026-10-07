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
SUDO_PASSWORD=""   # 远端 sudo 密码；留空则回退用 REMOTE_PASSWORD
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
        --sudo-password)
            # 仅用于远端 sudo，不影响 SSH 认证方式（可用密钥/SSH_ASKPASS 免密登录）
            SUDO_PASSWORD="$2"
            shift 2
            ;;
        --req)
            REQUIREMENTS_FILE="$2"
            shift 2
            ;;
        *)
            echo "未知参数: $1"
            echo "用法: bash scripts/deploy.sh [--jetson] [--host HOST] [--user USER] [--password PASSWORD] [--sudo-password PASSWORD] [--req REQUIREMENTS_FILE]"
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

echo "  -> 部署前巡检（只读，不改动；详见 AGENT.md「零、接手第一步」）..."
if [ -f "${REMOTE_DIR}/scripts/healthcheck.sh" ]; then
    # 只读快照：暴露 L4T 版本错配 / 磁盘写满 / apt·dpkg 元数据损坏 / Argus 失效等隐性状态，
    # 也让"是不是这次部署弄坏的"可判定。失败不阻断部署。
    SUDO_PASS="${SUDO_PASSWORD}" bash "${REMOTE_DIR}/scripts/healthcheck.sh" 2>/dev/null | sed 's/^/     /' || true
else
    echo "     (机器人上还没有 scripts/healthcheck.sh，本次跳过)"
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

echo "  -> 清理旧代码（保留运行时状态）..."

# 运行时状态目录，绝不参与清理：
#   config/  设备配置 + 凭据(.binding_secret/.binding_password) + bindings.json
#   data/    红外码库等业务数据
#   logs/    日志历史
# 说明：logs 可能非常大（曾出现 739MB 的陈旧轮转文件），因此只保留、不打包备份。
KEEP_DIRS="venv config data logs"

# 备份 config/ + data/（体积小），用 tar 以保留 root 属主与权限
STATE_TAR="/tmp/wobot-state-$$.tar.gz"
STATE_ITEMS=""
for item in config data; do
    if [ -e "${REMOTE_DIR}/$item" ]; then
        STATE_ITEMS="$STATE_ITEMS $item"
    fi
done

STATE_OK=true
if [ -n "$STATE_ITEMS" ]; then
    if eval "${SUDO} tar czf ${STATE_TAR} -C ${REMOTE_DIR}${STATE_ITEMS}"; then
        echo "  -> 已备份运行时状态:${STATE_ITEMS}"
    else
        STATE_OK=false
    fi
fi

# 备份失败则中止：否则解压会用仓库里的默认 config 覆盖设备上的真实配置
if [ "$STATE_OK" != true ]; then
    echo "  [错误] 运行时状态备份失败，已中止部署以避免覆盖设备配置"
    echo "         请检查磁盘空间: df -h ${REMOTE_DIR}"
    rm -f "${STATE_TAR}"
    exit 1
fi

# 删除旧代码，但保留 KEEP_DIRS
FIND_ARGS=""
for item in $KEEP_DIRS; do
    FIND_ARGS="$FIND_ARGS ! -name $item"
done
find ${REMOTE_DIR} -mindepth 1 -maxdepth 1 $FIND_ARGS -exec rm -rf {} + 2>/dev/null || true
eval "${SUDO} find ${REMOTE_DIR} -mindepth 1 -maxdepth 1 ${FIND_ARGS} -exec rm -rf {} +" 2>/dev/null || true

echo "  -> 解压部署包..."
tar -xzf /tmp/${PACKAGE_NAME} -C ${REMOTE_DIR}

# 还原运行时状态：设备上的实际配置/凭据/绑定优先于仓库默认值
if [ -n "$STATE_ITEMS" ] && [ -f "$STATE_TAR" ]; then
    if eval "${SUDO} tar xzf ${STATE_TAR} -C ${REMOTE_DIR}"; then
        echo "  -> 已还原运行时状态（设备配置/凭据/绑定优先）"
    else
        echo "  [警告] 运行时状态还原失败，请检查 ${REMOTE_DIR}/config"
    fi
    # tar 由 sudo 创建（root 属主），删除必须用 sudo；且失败不得中止后续部署
    eval "${SUDO} rm -f ${STATE_TAR}" 2>/dev/null || true
fi

# ---- 编译本地 C 工具 ----
echo "  -> 编译 C 工具..."
mkdir -p ${REMOTE_DIR}/bin
for cfile in ${REMOTE_DIR}/src/tools/dht11/dht11_reader.c; do
    if [ -f "$cfile" ]; then
        toolname=$(basename "$cfile" .c)
        gcc -O2 -o "${REMOTE_DIR}/bin/${toolname}" "$cfile" 2>&1 || echo "  [警告] ${toolname} 编译失败"
        echo "  -> ${toolname} 已编译"
    fi
done

# ---- 音频硬件自动检测 & 配置 ----
echo "  -> 运行音频硬件自动配置..."
if [ -f "${REMOTE_DIR}/scripts/setup_audio.sh" ]; then
    eval "${SUDO} bash ${REMOTE_DIR}/scripts/setup_audio.sh" || echo "  [警告] 音频配置失败（非致命，可能已配置过），继续部署"
    echo "  -> 音频配置步骤完成"
else
    echo "  [警告] setup_audio.sh 不存在，跳过音频配置"
fi

echo "  -> 检查依赖文件..."
cd ${REMOTE_DIR}
if [ ! -f "${REQUIREMENTS_FILE}" ]; then
    echo "  [警告] ${REQUIREMENTS_FILE} 不存在，回退到 requirements.txt"
    REQUIREMENTS_FILE="requirements.txt"
fi

if [ -d "${REMOTE_DIR}/venv" ]; then
    echo "  -> venv 已存在，跳过依赖安装"
    source ${REMOTE_DIR}/venv/bin/activate
else
    echo "  -> 创建虚拟环境（Python 3.7）..."
    python3.7 -m venv ${REMOTE_DIR}/venv 2>/dev/null || python3 -m venv ${REMOTE_DIR}/venv
    source ${REMOTE_DIR}/venv/bin/activate
    echo "  -> 安装依赖..."
    pip install --upgrade pip -q
    pip install -r ${REQUIREMENTS_FILE} -q
    echo "  -> 依赖安装完成"
fi

# 安装 Rosmaster_Lib 硬件驱动（如果 Jetson 上存在）
if [ -d "/home/jetson/py_install/Rosmaster_Lib" ]; then
    echo "  -> 安装 Rosmaster_Lib..."
    cp -r /home/jetson/py_install/Rosmaster_Lib ${REMOTE_DIR}/venv/lib/python3*/site-packages/ 2>/dev/null || true
fi

# aiortc 在本机（Jetson + OpenSSL 1.1.1）**不需要任何补丁**：
#   2026-10-07 实测 SSL_CTX_set_read_ahead 存在且返回 0（原始断言 `== 0` 本来就通过）、
#   BIO_ctrl_pending 存在且可用 —— 原始 _write_ssl 本来就正确。
#
# 历史教训：deploy.sh 曾在此用 `hasattr` 三元表达式"保护" set_read_ahead，
# 但那是错的 —— 它把 `_openssl_assert(lib.SSL_CTX_set_read_ahead(ctx, 1) == 0)`
# 改成了 `_openssl_assert(lib.SSL_CTX_set_read_ahead(ctx, 1) if hasattr(...) else 0)`，
# 断言语义从"返回 0"变成"等于 1"，于是必然抛
#   DtlsError: OpenSSL call failed → 前端 "WebRTC negotiation failed"
# 且该补丁幂等守卫写错字符串，每次部署重复叠加，最终把文件改成语法错误。
#
# 因此这里不再打补丁，改为跑一次 DTLS 回环自检（SDP→ICE→DTLS→DataChannel），
# 一旦这一层被破坏就能在部署阶段立刻发现。
echo "  -> WebRTC DTLS 自检..."
if [ -f "${REMOTE_DIR}/scripts/webrtc_selftest.py" ]; then
    ${REMOTE_DIR}/venv/bin/python ${REMOTE_DIR}/scripts/webrtc_selftest.py \
        || echo "  [警告] DTLS 自检未通过，WebRTC 可能不可用（检查 aiortc 是否被改动）"
else
    echo "  [跳过] 未找到 scripts/webrtc_selftest.py"
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

# 注意：参数必须写在 ssh 命令行上；写在 heredoc 结束标记之后会被本地 shell 当成命令执行，
# 导致远端脚本收不到 $1..$5（REMOTE_DIR 为空时 find 会在远端 home 目录里删文件）。
eval "${SSH_CMD} ${REMOTE_USER}@${REMOTE_HOST} 'bash -s' '${REMOTE_DIR}' '${PACKAGE_NAME}' '${SERVICE_NAME}' '${REQUIREMENTS_FILE}' '${SUDO_PASSWORD:-$REMOTE_PASSWORD}'" <<SCRIPT
${REMOTE_SCRIPT}
SCRIPT

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
